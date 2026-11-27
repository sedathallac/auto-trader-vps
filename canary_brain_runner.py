#!/usr/bin/env python3
"""canary_brain_runner.py — CANARY AI BRAIN gölge tarama servisi (VPS).

TAM EVREN tarayıcı: Binance USDⓈ-M LIVE_ELIGIBLE sözleşmelerin tamamını
(keşif anında 519; sınır YOK) her 4h bar kapanışından sonra tarar ve her
sembol+yön için PredictionRecord üretir (canary_brain.PredictionRecord).
EMİR YOKTUR; canlı giriş zincirine, canary_live'a, canlı canary DB'sine
YAZMAZ. Araştırma belleği AYRI SQLite dosyasıdır (BRAIN_DB).

TASARIM — DURUMSUZ DÖNGÜ (stateless-per-cycle):
  Her döngü sıfırdan inşa edilir: exchangeInfo keşfi → piyasa anlık
  görüntüleri (24hr/bookTicker/premiumIndex — HEPSİ tek-istek TAM liste) →
  likidite/spread kapısı → kalan semboller için 410 barlık kline replay
  (donmuş H1 motoru + BLOK32 kalite modeli) → iki yön değerlendirmesi →
  adaylar için dinamik boyutlama (canary_brain_sizing). Restart/stale-state
  hata sınıfları YOKTUR; her döngü kendi kanıtını taşır.

RATE LİMİT (ölçülmüş gerçek ağırlıklar, 2026-11-26):
  exchangeInfo=1, ticker/24hr(TÜM)=35, bookTicker(TÜM)=2,
  premiumIndex(TÜM)=10, klines limit=410=2 (limit≤499 → 2).
  Kaynak: X-MBX-USED-WEIGHT-1M delta ölçümü (yerel makineden public GET).
  Bütçe: kayan 60s penceresinde BRAIN_WEIGHT_BUDGET_1M (varsayılan 400;
  IP başına 2400/dk limitinin çok altında — aynı IP'teki futures-canary
  servisi premiumIndex'i ~120/dk tüketir). Kontrolsüz polling YOKTUR:
  bütün çağrılar RateScheduler token-bucket'ından geçer.

HESAP VERİSİ (opsiyonel, salt-okunur): BINANCE_KEY/SECRET varsa
B.1 transport'u ile /fapi/v3/balance (ağırlık 5) okunur — availableBalance
boyutlama tavanı için. Yoksa/yanıtsızsa boyutlama tavanı UNVERIFIED
işaretlenir (fail-open YALNIZ gölge kayıt içindir; canlı emir YOKTUR).

Korelasyon girdisi: açık canary pozisyonları canary DB'sindenSalt-OKUNUR
(mode=ro) okunur; korelasyon aday/tutulan sembollerin motor close
geçmişinden (son 180 log-getiri) Pearson olarak hesaplanır. Okunamazsa
held_positions=[] + uyarı.

CLI:
  python3 canary_brain_runner.py --once [--json]   # tek döngü
  python3 canary_brain_runner.py --serve           # 4h bar kapanışına hizalı
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import deque
from typing import Optional

import requests

from contract_discovery import (discover_contracts, LIVE_ELIGIBLE,
                                VENUE_FAPI)
from canary_brain import (BrainScanner, gate_market_data,
                          eligible_symbols, cycle_decision,
                          DECISION_CANDIDATE, DECISION_REJECTED,
                          roundtrip_cost_pct,
                          calibration_confidence, MODEL_VERSION)
from canary_brain_sizing import SizingBrain
from h1_adapter import H1SignalEngine
import canary_quality as cq

BAR_MS = 4 * 3600 * 1000
BASE = "https://fapi.binance.com"

# ölçülmüş endpoint ağırlıkları (modül docstring'i — tekrar ölçme gerekmez;
# scheduler bunları bütçeler)
W_EXCHANGE_INFO = 1
W_TICKER_24H = 35
W_BOOK_TICKER = 2
W_PREMIUM = 10
W_KLINES_410 = 2

KLINES_LIMIT = 410            # lookback 180 + alt pencereler + sigma/vol
MARKET_SNAPSHOT_DELAY_S = 60  # bar kapanışı sonrası anlık görüntü gecikmesi


class RateScheduler:
    """Kayan 60s penceresinde ağırlık token-bucket (kontrollü polling).
    clock/sleep enjekte edilebilir (testlerde sahte zaman)."""

    def __init__(self, budget_per_min: int, sleep=None, clock=None):
        self.budget = int(budget_per_min)
        self._win = deque()          # (ts_ms, weight)
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self.used_total = 0
        self.calls = 0
        self.last_used_weight_1m = None

    def _prune(self, now_ms):
        while self._win and now_ms - self._win[0][0] > 60_000:
            self._win.popleft()

    def acquire(self, weight: int):
        """Ağırlık yerleştirir; bütçe aşılacaksa pencere boşalana dek bekler."""
        while True:
            now = int(self._clock() * 1000)
            self._prune(now)
            used = sum(w for _, w in self._win)
            if used + weight <= self.budget:
                self._win.append((now, weight))
                self.used_total += weight
                self.calls += 1
                return
            oldest = self._win[0][0]
            wait_s = (oldest + 60_000 - now) / 1000.0 + 0.05
            self._sleep(max(0.05, wait_s))

    def note_used_weight(self, header_value: Optional[str]):
        """Gerçek X-MBX-USED-WEIGHT-1M telemetrisi (bütçe ayrı tutulur —
        paylaşılan IP'te başka trafik olabilir; scheduler kendi penceresini
        aşmaz, header yalnız İZLEME içindir)."""
        if header_value:
            try:
                self.last_used_weight_1m = int(header_value)
            except (TypeError, ValueError):
                pass


class BrainDB:
    """Araştırma belleği — canlı canary DB'sinden TAMAMEN AYRI dosya."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS brain_cycles (
        cycle_id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL, decision TEXT NOT NULL,
        universe_eligible INTEGER, market_data_covered INTEGER,
        liquid INTEGER, history_warm INTEGER, scored INTEGER,
        candidates INTEGER, long_candidates INTEGER, short_candidates INTEGER,
        rejected INTEGER, no_trade_symbols INTEGER,
        model_version TEXT, funnel_json TEXT, notes_json TEXT);
    CREATE TABLE IF NOT EXISTS prediction_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL, ts INTEGER NOT NULL,
        symbol TEXT NOT NULL, direction INTEGER NOT NULL,
        decision TEXT NOT NULL, record_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_pr_cycle ON prediction_records(cycle_id);
    CREATE INDEX IF NOT EXISTS idx_pr_sym ON prediction_records(symbol, ts);
    """

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    def next_cycle_id(self) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(cycle_id),0) FROM brain_cycles").fetchone()
        return int(row[0]) + 1

    def write_cycle(self, summary: dict, records):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO brain_cycles VALUES (?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?)",
                (summary["cycle_id"], summary["ts"], summary["decision"],
                 summary["universe_eligible"], summary["market_data_covered"],
                 summary["liquid"], summary["history_warm"],
                 summary["scored"], summary["candidates"],
                 summary["long_candidates"], summary["short_candidates"],
                 summary["rejected"], summary["no_trade_symbols"],
                 summary["model_version"],
                 json.dumps(summary["funnel"]), json.dumps(summary["notes"])))
            for r in records:
                self.db.execute(
                    "INSERT INTO prediction_records (cycle_id, ts, symbol,"
                    " direction, decision, record_json) VALUES (?,?,?,?,?,?)",
                    (r.cycle_id, r.ts, r.symbol, r.direction, r.decision,
                     json.dumps(r.to_dict())))

    def close(self):
        self.db.close()


class BrainRunner:
    def __init__(self, *, db_path: str, log_dir: str,
                 weight_budget_1m: int = 400,
                 min_quote_volume_24h: float = 5_000_000.0,
                 max_spread_pct: float = 0.10,
                 market_max_age_ms: int = 120_000,
                 stop_sigma_mult: float = 2.0,
                 canary_db_path: Optional[str] = None,
                 account_key: Optional[str] = None,
                 account_secret: Optional[str] = None,
                 session=None, sleep=None, now_ms=None):
        self.session = session or requests.Session()
        self.sched = RateScheduler(weight_budget_1m, sleep=sleep)
        # serve() döngü uykusu da enjekte edilebilir (testte sahte saat)
        self._serve_sleep = sleep or time.sleep
        self.scanner = BrainScanner(min_quote_volume_24h=min_quote_volume_24h,
                                    max_spread_pct=max_spread_pct)
        self.sizing = SizingBrain(stop_sigma_mult=stop_sigma_mult)
        self.market_max_age_ms = market_max_age_ms
        self.canary_db_path = canary_db_path
        self.account_key = account_key
        self.account_secret = account_secret
        self.db = BrainDB(db_path)
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    # ---- ağ (bütün çağrılar scheduler'dan) --------------------------------
    def _get(self, path, params=None, weight=1):
        self.sched.acquire(weight)
        r = self.session.get(BASE + path, params=params, timeout=30)
        self.sched.note_used_weight(r.headers.get("X-MBX-USED-WEIGHT-1M"))
        r.raise_for_status()
        return r.json()

    def _log(self, name, obj):
        with open(os.path.join(self.log_dir, name), "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---- döngü adımları ------------------------------------------------------
    def _universe(self):
        info = self._get("/fapi/v1/exchangeInfo", None, W_EXCHANGE_INFO)
        res = discover_contracts(fapi_info=info, dapi_info=None)
        return eligible_symbols(res), res

    def _market_snapshots(self):
        tickers = self._get("/fapi/v1/ticker/24hr", None, W_TICKER_24H)
        books = self._get("/fapi/v1/ticker/bookTicker", None, W_BOOK_TICKER)
        premium = self._get("/fapi/v1/premiumIndex", None, W_PREMIUM)
        return ({t["symbol"]: t for t in tickers if "symbol" in t},
                {b["symbol"]: b for b in books if "symbol" in b},
                {p["symbol"]: p for p in premium if "symbol" in p})

    def _klines_closed(self, symbol, now_ms):
        rows = self._get("/fapi/v1/klines",
                         {"symbol": symbol, "interval": "4h",
                          "limit": KLINES_LIMIT}, W_KLINES_410)
        out = []
        for k in rows:
            open_ts, close_time = int(k[0]), int(k[6])
            if close_time <= now_ms:      # yalnız TAM kapanmış barlar
                out.append((open_ts, float(k[4]), float(k[7])))
        return out

    def _account_snapshot(self):
        """Opsiyonel salt-okunur bakiye (VPS'te imzalı GET; yerelde -2015
        olur → None döner, kayıt UNVERIFIED işaretlenir)."""
        if not (self.account_key and self.account_secret):
            return None
        try:
            from canary_account_adapter import BinanceHttpTransport
            from canary_account_model import (parse_balance_v3,
                                              SnapshotMetadata)
            t = BinanceHttpTransport(self.account_key, self.account_secret)
            self.sched.acquire(5)          # /fapi/v3/balance ağırlığı
            resp = t.signed_get("/fapi/v3/balance")
            meta = SnapshotMetadata(
                source_name="brain-runner", endpoint="/fapi/v3/balance",
                request_started_at_ms=resp.request_started_at_ms,
                response_received_at_ms=resp.response_received_at_ms,
                local_receive_time_ms=resp.response_received_at_ms,
                source_update_time_ms=None,
                note="brain hesap okumasi (salt-okunur)")
            bal = parse_balance_v3(resp.body, meta)
            usdt = bal.usdt()
            if usdt is None:
                return None
            return {"available_balance": usdt.availableBalance,
                    "cross_wallet": usdt.crossWalletBalance,
                    "balance": usdt.balance}
        except Exception:
            return None

    def _held_positions(self):
        """Açık canary pozisyonları — canlı DB'den SALT-OKUNUR (mode=ro).
        Yazma YOKTUR. Okunamazsa [] + uyarı."""
        if not self.canary_db_path or not os.path.exists(self.canary_db_path):
            return [], "canary_db yok"
        try:
            con = sqlite3.connect(
                f"file:{self.canary_db_path}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT symbol FROM positions WHERE status IN "
                    "('OPEN','SAFE_EXIT')").fetchall()
                return [r[0] for r in rows], None
            finally:
                con.close()
        except sqlite3.Error as e:
            return [], f"canary_db okunamadı: {e}"

    # ---- ana döngü -------------------------------------------------------------
    def run_cycle(self) -> dict:
        t_start = self._now_ms()
        cycle_id = self.db.next_cycle_id()
        notes = []

        # 1) evren
        symbols, discovery = self._universe()
        # 2) piyasa anlık görüntüleri
        tickers, books, premium = self._market_snapshots()
        now_ms = self._now_ms()

        # 3) kapılar + funnel
        gates = {}
        liquid = []
        for s in symbols:
            g = gate_market_data(
                s, ticker=tickers.get(s), book=books.get(s),
                premium=premium.get(s), now_ms=now_ms,
                max_age_ms=self.market_max_age_ms,
                min_quote_volume_24h=self.scanner.min_quote_volume_24h,
                max_spread_pct=self.scanner.max_spread_pct)
            gates[s] = g
            if g.eligible:
                liquid.append(s)

        # 4) kline replay (yalnız likit semboller — kontrolsüz polling YOK)
        engine = H1SignalEngine(symbols=liquid)
        quality = cq.QualityModel(engine)
        warm = {}
        fetch_errors = {}
        for s in liquid:
            try:
                bars = self._klines_closed(s, now_ms)
            except Exception as e:
                fetch_errors[s] = f"{type(e).__name__}"
                continue
            for (open_ts, close, qv) in bars:
                engine.end_bar(s, open_ts, close)
                st = engine._state(s)
                sig = math.sqrt(st["var"]) if st["var"] is not None \
                    and st["var"] > 0 else None
                quality.feed(s, qv, sig)
            warm[s] = engine.warm(s)

        warm_syms = [s for s in liquid if warm.get(s)]

        # 5) hesap + tutulan pozisyonlar (boyutlama girdileri)
        account = self._account_snapshot()
        if account is None:
            notes.append("hesap verisi yok — boyutlama marjin tavanı UNVERIFIED")
        held_syms, held_err = self._held_positions()
        if held_err:
            notes.append(held_err)
        held_risk = {s: 5.0 for s in held_syms}   # pozisyon başına riskli
        #    bütçe (5 USDT operatör limiti) — yaklaşım, kayda geçer

        # korelasyon: tutulan sembollerin close geçmişleri (aynı replay'den)
        held_closes = {s: engine._state(s)["closes"][-181:]
                       for s in held_syms if s in engine.symbols
                       and engine._state(s)["n"] > 181}

        def _corr(a_closes, b_closes):
            n = min(len(a_closes), len(b_closes)) - 1
            if n < 60:
                return None
            ra = [math.log(a_closes[i + 1] / a_closes[i])
                  for i in range(n)]
            rb = [math.log(b_closes[i + 1] / b_closes[i])
                  for i in range(n)]
            ma, mb = sum(ra) / n, sum(rb) / n
            cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
            va = sum((x - ma) ** 2 for x in ra)
            vb = sum((y - mb) ** 2 for y in rb)
            if va <= 0 or vb <= 0:
                return None
            return cov / math.sqrt(va * vb)

        # 6) her sembol+yön değerlendirmesi
        records = []
        candidates = []
        for s in warm_syms:
            st = engine._state(s)
            closes, n = st["closes"], st["n"]
            m = (closes[-1] / closes[n - 1 - engine.lookback] - 1.0
                 if n > engine.lookback else None)
            sigma_bar = math.sqrt(st["var"]) if st["var"] and st["var"] > 0 \
                else None
            for d in (1, -1):
                q = quality.compute(s, d)
                sizing_out = None
                # aday olabilecek yönlerde (kalite geçti + veri tamam)
                # maliyet/boyutlama hesapla — kayda denetim izi olarak girer
                if q is not None and q.get("quality_pass") and \
                        gates[s].eligible and sigma_bar:
                    costs = roundtrip_cost_pct(
                        gates[s].spread_pct, gates[s].last_funding_rate,
                        cq_model_hold_bars(d))
                    if costs is not None:
                        held = []
                        for hs, hr in held_risk.items():
                            corr = None
                            if s != hs and hs in held_closes:
                                corr = _corr(closes, held_closes[hs])
                            held.append((corr, hr))
                        sizing_out = self.sizing.size(
                            price=gates[s].price, sigma_bar=sigma_bar,
                            roundtrip_cost_pct=costs,
                            position_budget_remaining=5.0,
                            total_budget_remaining=10.0,
                            confidence=calibration_confidence(d),
                            held_positions=held,
                            available_balance=(account or {}).get(
                                "available_balance"),
                            brackets=None,     # bracket entegrasyonu takip;
                                                # canlı emir yokken tavan
                                                # UNVERIFIED kalabilir
                            )
                rec = self.scanner.evaluate(
                    s, d, cycle_id=cycle_id, now_ms=now_ms, gate=gates[s],
                    quality=q, sigma_bar=sigma_bar, momentum=m,
                    warm=warm.get(s, False), sizing=sizing_out)
                records.append(rec)
                if rec.decision == DECISION_CANDIDATE:
                    candidates.append(rec)

        decision = cycle_decision(records)
        n_long = sum(1 for r in candidates if r.direction > 0)
        n_short = sum(1 for r in candidates if r.direction < 0)
        n_rej = sum(1 for r in records if r.decision == DECISION_REJECTED)
        scored_syms = set(r.symbol for r in records)
        n_noTrade_syms = sum(
            1 for s in scored_syms
            if all(r.decision != DECISION_CANDIDATE
                   for r in records if r.symbol == s))

        # ret nedeni dağılımı (başlıca nedenler — §9)
        reason_counts = {}
        for r in records:
            for reason in r.rejection_reasons:
                key = reason.split(":")[0]
                reason_counts[key] = reason_counts.get(key, 0) + 1

        funnel = {
            "trading_contracts_fapi": sum(
                1 for coin in discovery.coins for c in coin.contracts
                if c.venue == VENUE_FAPI),
            "universe_eligible": len(symbols),
            "market_data_covered": len([s for s in symbols
                                        if s in tickers and s in books
                                        and s in premium]),
            "liquid": len(liquid),
            "kline_fetched": len(warm) + len(fetch_errors),
            "kline_errors": len(fetch_errors),
            "history_warm": len(warm_syms),
            "scored_symbols": len(scored_syms),
            "records": len(records),
            "candidates": len(candidates),
            "long_candidates": n_long,
            "short_candidates": n_short,
            "rejected_records": n_rej,
            "rejection_reason_counts": dict(sorted(
                reason_counts.items(), key=lambda kv: -kv[1])),
        }

        summary = {"cycle_id": cycle_id, "ts": now_ms, "decision": decision,
                   "universe_eligible": len(symbols),
                   "market_data_covered": funnel["market_data_covered"],
                   "liquid": len(liquid), "history_warm": len(warm_syms),
                   "scored": len(scored_syms), "candidates": len(candidates),
                   "long_candidates": n_long, "short_candidates": n_short,
                   "rejected": n_rej, "no_trade_symbols": n_noTrade_syms,
                   "model_version": MODEL_VERSION, "funnel": funnel,
                   "notes": notes}

        self.db.write_cycle(summary, records)
        self._log("brain_cycles.jsonl", {
            "t": now_ms, "kind": "BRAIN_CYCLE", **summary,
            "account_data": bool(account), "held_positions": held_syms,
            "scheduler_weight_used": self.sched.used_total,
            "scheduler_calls": self.sched.calls,
            "last_used_weight_1m_header": self.sched.last_used_weight_1m,
            "duration_ms": self._now_ms() - t_start})
        return summary

    def serve(self):
        """4h bar kapanışına hizalı sürekli döngü (kapanış+60s).

        KÖK NEDEN DÜZELTMESİ (2026-11-27): eski sürüm hedefi daima
        '(sıradaki sınır)+60s' olarak hesaplıyor, uyanınca da BAŞTAN
        hesaplıyordu. Uyanış anında `now` her zaman geçerli barın içinde
        olduğundan sıradaki sınır BİR SONRAKİ bara atlar; wait_s daima
        >60s kalır ve run_cycle MATEMATİKSEL olarak hiç çağrılamazdı.
        Kanıt: canlı sürece 14+ saatte 0,2s CPU, 0 döngü, DB'deki 2
        döngü elle --once ile atılmıştı; py-spy locals wait_s=7199,999
        gösterdi (hedef 04:01 iken now 02:01'de bir sonraki hedef
        yine 04:01+4h olur). Yeni semantik: bar-içi konum ≥60s VE
        taranmamış YENİ kapanmış veri barı varsa döngü atılır; her veri
        barı yalnız BİR kez taranır (restart/downtime sonrası kaçan
        son kapanmış bar yakalanır — geçmiş barlar yeniden oynatılmaz).
        """
        last_data_bar = self._last_cycled_data_bar()
        while True:
            action, payload = serve_next_action(self._now_ms(),
                                                last_data_bar)
            if action == "sleep":
                self._serve_sleep(payload)      # uzun beklemede parçalı uyan
                continue
            try:
                start_bar = self._now_ms() // BAR_MS
                self.run_cycle()
                last_data_bar = start_bar - 1   # taranan veri barı
            except Exception as e:
                self._log("brain_cycles.jsonl", {
                    "t": self._now_ms(), "kind": "BRAIN_CYCLE_ERROR",
                    "error": f"{type(e).__name__}: {e}"})
                self._serve_sleep(300)          # aynı veri barı için retry

    def _last_cycled_data_bar(self):
        """DB'deki son döngüden taranan son KAPANMIŞ veri barı.
        Döngü ts'i her zaman çalıştığı barın içindedir; o anda en yeni
        kapanmış veri barı bir önceki bardır (ts//BAR_MS − 1). DB boşsa
        −1 (ilk fırsatta tarar). Okuma hatasında ŞU ANKI veri barı
        döner (fail-closed: en fazla bir bar atlanır — aynı veri barı
        asla iki kez taranıp mükerrer aday üretmez)."""
        try:
            row = self.db.db.execute(
                "SELECT MAX(ts) FROM brain_cycles").fetchone()
            ts = row[0] if row else None
            return (ts // BAR_MS) - 1 if ts else -1
        except sqlite3.Error:
            return (int(time.time() * 1000) // BAR_MS) - 1


def cq_model_hold_bars(direction: int) -> float:
    """Yön başına kalibre OOS ortalama tutma süresi (bar) — maliyet modeli
    girdisi (canary_brain.CALIBRATION_8SYM tek kaynak)."""
    from canary_brain import CALIBRATION_8SYM
    return CALIBRATION_8SYM[direction]["oos_hold_bars"]


def serve_next_action(now_ms, last_cycled_data_bar=-1):
    """serve() döngüsünün tek adım kararı (saf fonksiyon — birim test edilir).

    Döndürür:
      ("cycle", None)  — bar-içi konum ≥ MARKET_SNAPSHOT_DELAY_S geçmiş VE
                         son kapanmış veri barı henüz taranmamış (run_cycle
                         çağrılmalı; o bar kapanış+60s anlamına gelir);
      ("sleep", saniye) — sonraki kapanış+gecikmeye kalan süre
                          (≤3600s parçalı uyku).

    Eski kod hatası (2026-11-27 kök neden, serve docstring'inde kanıtlı):
    hedef daima (now//BAR_MS+1)*BAR_MS+gecikme olarak hesaplandığından
    wait_s daima >gecikme kalıyordu ve döngü hiç tetiklenemiyordu.
    """
    bar = now_ms // BAR_MS
    pos_ms = now_ms - bar * BAR_MS
    delay_ms = MARKET_SNAPSHOT_DELAY_S * 1000
    data_bar = bar - 1          # bu bar içinde en yeni KAPANMIŞ veri barı
    if pos_ms >= delay_ms and data_bar > last_cycled_data_bar:
        return "cycle", None
    if pos_ms >= delay_ms:
        wait_ms = (BAR_MS - pos_ms) + delay_ms
    else:
        wait_ms = delay_ms - pos_ms
    return "sleep", min(wait_ms / 1000.0, 3600.0)


def main():
    ap = argparse.ArgumentParser(description="CANARY AI BRAIN gölge tarayıcı")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    runner = BrainRunner(
        db_path=os.environ.get("BRAIN_DB",
                               "/var/lib/futures-canary-brain/brain.db"),
        log_dir=os.environ.get("BRAIN_LOG_DIR",
                               "/var/log/futures-canary-brain"),
        weight_budget_1m=int(os.environ.get("BRAIN_WEIGHT_BUDGET_1M",
                                            "400")),
        min_quote_volume_24h=float(os.environ.get(
            "BRAIN_MIN_QUOTE_VOLUME_24H", "5000000")),
        max_spread_pct=float(os.environ.get("BRAIN_MAX_SPREAD_PCT", "0.10")),
        market_max_age_ms=int(os.environ.get("BRAIN_MARKET_MAX_AGE_MS",
                                             "120000")),
        stop_sigma_mult=float(os.environ.get("BRAIN_STOP_SIGMA_MULT",
                                             "2.0")),
        canary_db_path=os.environ.get(
            "BRAIN_CANARY_DB", "/var/lib/futures-canary/canary_live.db"),
        account_key=os.environ.get("BINANCE_KEY"),
        account_secret=os.environ.get("BINANCE_SECRET"))

    if args.once:
        s = runner.run_cycle()
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=1))
        else:
            print(json.dumps(s["funnel"], ensure_ascii=False, indent=1))
            print("decision:", s["decision"])
        return 0
    if args.serve:
        runner.serve()
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
