"""canary_shadow.py — BLOK27.3 §7: denetimli Shadow Canary gözlemi.

GERÇEK EMİR GÖNDERMEZ. Testnet/mainnet emri YOK. Yaptığı iş:
  1. Canlı PUBLIC futures verisi: klines(4h) 8 H1 sembolü + Mark Price
     (REST_SAMPLED_MARK_PRICE — premiumIndex; WS tick akışı DEĞİL)
  2. H1 canlı sinyal üretimi (h1_adapter H1SignalEngine — donmuş
     parametreler; paper_runner ile AYNI bar akışı semantiği:
     warmup replay execute'siz, kararlar sonraki bar açılışında)
  3. Shadow emir değerlendirmesi: her flip kararını RiskManager
     kapısından geçirir (canary_risk — operatör sınırları; shadow
     pozisyonlar YEREL canary DB'sinde, borsaya HİÇBİR istek yazılmaz)
  4. Risk azaltan kapanışlar HALT'ta da işlenir (kural 9)
  5. Paper run ile hedef karşılaştırması (paper DB salt-okunur)
  6. Mark gecikme/boşluk raporu (t−E skew)

Paper_runner ile aynı koruma semantiği:
  - restart sonrası dedup: son işlenen bar ts kalıcıdır (canary_state);
    replay edilen barlar execute'siz, yalnız YENİ barlar shadow icra
  - STALE_BAR_MS: kararı 8 saatten eski bir bar açılışından icra etmek
    ölçümü bozar → journal + icra YOK (fail-closed)

Kayıtlar: ~/futures-research/blok27/canary/
  canary_shadow.db        — RiskManager durumu (shadow pozisyonlar + dedup)
  cycles.jsonl            — her cycle özeti (append-only)
  shadow_orders.jsonl     — shadow kararlar + risk kapısı sonucu
"""
import argparse
import json
import math
import time
from pathlib import Path

import requests

from canary_risk import RiskManager, OrderRejected
from h1_adapter import H1SignalEngine
from paper_broker import FILL_MODEL

SYMBOLS = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT", "DOGEUSDT",
           "LINKUSDT", "SOLUSDT", "XRPUSDT"]
KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BAR_MS = 4 * 3600 * 1000
WARMUP_BARS = 400                # paper_runner ile aynı
STALE_BAR_MS = 8 * 3600 * 1000   # paper_runner ile aynı
CANARY_NOTIONAL = 50.0           # operatör: pozisyon başına 50 USDT notional
PAPER_DB = Path("~/futures-research/blok25/results/paper_ledger.db"
                ).expanduser()
DEFAULT_DIR = Path("~/futures-research/blok27/canary").expanduser()


def _get(url, params=None, timeout=10):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_klines(symbol, interval="4h", limit=WARMUP_BARS + 10):
    """Public klines: [open_ts, open, high, low, close, ..., qv] →
    paper_runner formatı (ot, o, c, qv)."""
    rows = _get(KLINE_URL, {"symbol": symbol, "interval": interval,
                            "limit": limit})
    return [(int(k[0]), float(k[1]), float(k[4]), float(k[7])) for k in rows]


def fetch_marks(symbols=SYMBOLS):
    """REST_SAMPLED_MARK_PRICE: premiumIndex (tek istek, tüm semboller).
    Dönüş: {symbol: (mark_px, ts_ms)} + alım süresi + t−E skew."""
    t0 = time.time()
    rows = _get(PREMIUM_URL)
    recv_ms = int(time.time() * 1000)
    latency_s = time.time() - t0
    out, skew = {}, {}
    for row in rows:
        if row["symbol"] in symbols:
            out[row["symbol"]] = (float(row["markPrice"]), int(row["time"]))
            skew[row["symbol"]] = (recv_ms - int(row["time"])) / 1000
    return out, skew, latency_s


def impact_bp(notional, qv):
    """paper_broker._impact_bp ile aynı: qv biliniyorsa sqrt-impact,
    bilinmiyorsa cap (konservatif)."""
    if qv is None or qv <= 0:
        return FILL_MODEL["impact_max_bp"]
    return min(FILL_MODEL["impact_max_bp"],
               FILL_MODEL["impact_coeff"] * 1e4 * math.sqrt(notional / qv))


def fill_px(ref_price, direction, notional, qv):
    """paper_broker._fill ile aynı: bar-açılış referanslı gerçekçi fill.
    direction +1 alır (yukarı), -1 satar (aşağı)."""
    bp = FILL_MODEL["half_spread_bp"] + FILL_MODEL["slippage_bp"] \
        + impact_bp(notional, qv)
    return ref_price * (1 + direction * bp / 1e4), bp


def commission(notional):
    return notional * FILL_MODEL["commission_bp"] / 1e4


class ShadowCanary:
    def __init__(self, directory=DEFAULT_DIR, paper_db=PAPER_DB):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rm = RiskManager(str(self.dir / "canary_shadow.db"))
        self.engine = H1SignalEngine(SYMBOLS)
        self.paper_db = paper_db
        self.symbols = SYMBOLS
        # restart-güvenli dedup: son işlenen bar ts kalıcı (kural 7)
        self.last_bar = {}
        for s in SYMBOLS:
            v = self.rm._get_state(f"last_bar:{s}")
            if v is not None:
                self.last_bar[s] = int(v)
        self.cycle_n = 0
        self._warmed = set()       # bu süreçte motora beslenen semboller

    # ---- kayıtlar ----------------------------------------------------------
    def _log(self, name, obj):
        with open(self.dir / name, "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def _set_last(self, symbol, ot):
        self.last_bar[symbol] = ot
        self.rm._set_state(f"last_bar:{symbol}", str(ot))

    # ---- paper karşılaştırması (salt-okunur) ---------------------------------
    def paper_targets(self):
        """Paper run'ın son recon kaydından hedef yönler (mode=ro)."""
        import sqlite3
        try:
            db = sqlite3.connect(f"file:{self.paper_db}?mode=ro", uri=True)
            row = db.execute("SELECT items FROM recon ORDER BY rowid DESC "
                             "LIMIT 1").fetchone()
            db.close()
            if not row:
                return {}
            out = {}
            for it in json.loads(row[0]):
                d = it.get("detail", "")
                if "target " in d and " vs " in d:
                    try:
                        out[it["symbol"]] = int(d.split("target ")[1]
                                                .split(" ")[0])
                    except (ValueError, IndexError):
                        pass
            return out
        except Exception:
            return {}

    # ---- funding (gerçek oranlar, public; bar-sınırı yerleşimi) ---------------
    def apply_funding(self, klines_by_sym):
        """Açık shadow pozisyonlara gerçek funding yerleşimleri: paper
        semantiği cf = -dir*rate*units*(yerleşimden önceki son bar kapanışı).
        INSERT OR REPLACE → cycle'lar arası idempotent."""
        now_ms = int(time.time() * 1000)
        for pos in self.rm.open_positions():
            s = pos["symbol"]
            closes = sorted((ot, c) for (ot, o, c, qv)
                            in klines_by_sym.get(s, []))
            try:
                rows = _get(FUNDING_URL, {"symbol": s,
                                          "startTime": pos["entry_ts"],
                                          "limit": 100})
            except Exception as e:
                self._log("shadow_orders.jsonl",
                          {"t": now_ms, "kind": "FUNDING_FETCH_ERROR",
                           "symbol": s, "error": str(e)})
                continue
            for fr in rows:
                ts = int(fr["fundingTime"])
                if ts <= pos["entry_ts"]:
                    continue
                # yerleşimden önceki son KAPALI bar kapanışı
                px = None
                for (ot, c) in closes:
                    if ot + BAR_MS <= ts:
                        px = c
                    else:
                        break
                if px is None:
                    continue
                cf = -pos["direction"] * float(fr["fundingRate"]) \
                    * pos["units"] * px
                self.rm.note_funding(pos["pos_id"], ts, cf)

    # ---- H1 sinyal + shadow karar ----------------------------------------------
    def evaluate(self, now_ms, klines_by_sym):
        """Paper_runner.cycle semantiği: (1) soğuk başlangıç (ilk start veya
        restart): ot ≤ kalıcı last bar → warmup replay execute'siz (motor
        durumu inşası; ilk start'ta tüm geçmiş warmup'tur — shadow flat
        başlar, backfill YOK); (2) ot > last YENİ bar → karar begin_bar'da
        (bu barın AÇILIŞINDA) shadow icra — 8h stale kuralıyla. Ilık
        süreçte ot ≤ last barlar motora YENİDEN BESLENMEZ (her bar işlem
        başına tam bir kez beslenir; paper'ın her-cycle tam replay'i kuyruk
        tutarlı olduğundan aynı canlı sinyali üretir — buradaki sürüm
        bundan daha temiz, birebir eşdeğer). merged-timeline sıralı."""
        decisions = []
        warmup_events, live_events = [], []
        newly_warmed = set()
        for s, kl in klines_by_sym.items():
            last = self.last_bar.get(s)
            cold = s not in self._warmed
            for (ot, o, c, qv) in kl:
                if ot + BAR_MS > now_ms:          # açık bar asla girdi değil
                    continue
                if last is not None and ot <= last and not cold:
                    continue                       # bu süreçte beslendi
                if cold and (last is None or ot <= last):
                    warmup_events.append((ot, s, o, c, qv))
                    newly_warmed.add(s)
                else:
                    live_events.append((ot, s, o, c, qv))
        warmup_events.sort(key=lambda e: (e[0], e[1]))
        live_events.sort(key=lambda e: (e[0], e[1]))
        warm_last = {}
        for (ot, s, o, c, qv) in warmup_events:
            self.engine.begin_bar(s, o)           # execute YOK (replay)
            self.engine.end_bar(s, ot, c)
            warm_last[s] = ot
        # warmup son barı kalıcıdedup'a yazılır (paper _warmup gibi):
        # ilk start'ta geçmiş asla sonradan canlı icra edilmesin
        for s, ot in warm_last.items():
            self._warmed.add(s)
            self._set_last(s, ot)
        for (ot, s, o, c, qv) in live_events:
            self._set_last(s, ot)
            pend = self.engine.begin_bar(s, o)    # flip bu bar açılışında
            if pend is not None:
                pend.execution_ts = ot
                pend.ref_price = o
                if now_ms - ot > STALE_BAR_MS:
                    self._log("shadow_orders.jsonl",
                              {"t": now_ms, "kind": "STALE_BAR",
                               "symbol": s, "bar_ts": ot,
                               "age_h": round((now_ms - ot) / 3600000, 1),
                               "execute": False,
                               "detail": "fail-closed: eski açılıştan icra "
                                         "yok (paper_runner kuralı)"})
                else:
                    decisions.extend(self._shadow_flip(s, pend.direction,
                                                       o, qv, ot, now_ms))
            self.engine.end_bar(s, ot, c)
            self._warmed.add(s)
        if warmup_events:
            self._log("shadow_orders.jsonl",
                      {"t": now_ms, "kind": "WARMUP_DONE",
                       "bars_replayed": len(warmup_events),
                       "execute": False})
        return decisions

    def _shadow_flip(self, symbol, direction, ref_px, qv, bar_ts, now_ms):
        """Flip: önce kapat (risk azaltan — HALT'ta da serbest), sonra yeni
        yön için risk kapısından geç (RED olabilir — kayda geçer). Borsaya
        HİÇBİR istek yazılmaz: yalnız yerel canary DB."""
        out = []
        # 1) mevcut shadow pozisyonu kapat (flip'in çıkış bacağı)
        for pos in self.rm.open_positions():
            if pos["symbol"] != symbol or pos["direction"] == direction:
                continue
            close_notional = abs(pos["units"]) * ref_px
            exit_px, _ = fill_px(ref_px, -pos["direction"], close_notional,
                                 qv)
            comm = commission(close_notional)
            realized = self.rm.close_position(pos["pos_id"], exit_px,
                                              now_ms, comm)
            out.append({"t": now_ms, "kind": "SHADOW_CLOSE",
                        "symbol": symbol, "pos_id": pos["pos_id"],
                        "exit_px": round(exit_px, 6),
                        "exit_comm": round(comm, 6),
                        "realized": round(realized, 6), "bar_ts": bar_ts,
                        "ref_px": ref_px})
        # 2) yeni pozisyon — operatör limitleri kapıda (atomik submit)
        rec = {"t": now_ms, "kind": "SHADOW_OPEN", "symbol": symbol,
               "direction": direction, "notional": CANARY_NOTIONAL,
               "bar_ts": bar_ts, "ref_px": ref_px}
        if direction != 0:
            try:
                entry_px, bp = fill_px(ref_px, direction, CANARY_NOTIONAL,
                                       qv)
                comm = commission(CANARY_NOTIONAL)
                pid = self.rm.submit_order(symbol, direction,
                                           CANARY_NOTIONAL, entry_px,
                                           now_ms, comm)
                rec.update({"result": "ACCEPTED", "pos_id": pid,
                            "entry_px": round(entry_px, 6),
                            "fill_bp": round(bp, 2),
                            "entry_comm": round(comm, 6)})
            except OrderRejected as e:
                rec["result"] = f"REJECTED: {e}"
        else:
            rec["result"] = "FLAT"                 # hedef 0: yalnız kapanış
        out.append(rec)
        return out

    # ---- bir cycle ---------------------------------------------------------------
    def cycle(self):
        self.cycle_n += 1
        now_ms = int(time.time() * 1000)
        cyc = {"t": now_ms, "cycle": self.cycle_n,
               "rm_status": self.rm.status}
        # 1) mark güncelle (REST_SAMPLED) — risk kapısı ve zarar hesabı için
        try:
            marks, skew, latency = fetch_marks()
            for s, (px, ts) in marks.items():
                self.rm.update_mark(s, px, ts)
            cyc["mark_latency_s"] = round(latency, 3)
            cyc["mark_skew_s"] = {k: round(v, 2) for k, v in
                                  sorted(skew.items())}
            cyc["marks_n"] = len(marks)
        except Exception as e:
            cyc["mark_error"] = f"{type(e).__name__}: {e}"
        # 2) klines
        klines, kerr = {}, []
        for s in SYMBOLS:
            try:
                klines[s] = fetch_klines(s)
            except Exception as e:
                kerr.append(f"{s}:{type(e).__name__}")
        cyc["kline_errors"] = kerr
        # 3) funding önce (paper: yerleşim flip'ten önce), sonra barlar
        if klines:
            self.apply_funding(klines)
            decisions = self.evaluate(now_ms, klines)
        else:
            decisions = []
        # 4) limit uygulama (SAFE_EXIT / HALT / FAIL_CLOSED raporu)
        cyc["risk_actions"] = self.rm.enforce(now_ms)
        # 5) paper karşılaştırması: motor hedefi vs paper hedefi
        paper = self.paper_targets()
        cyc["paper_vs_shadow"] = {s: (paper.get(s), self.engine.direction(s))
                                  for s in SYMBOLS if s in paper}
        cyc["engine_dirs"] = {s: self.engine.direction(s) for s in SYMBOLS}
        cyc["decisions_n"] = len(decisions)
        cyc["open_positions"] = len(self.rm.open_positions())
        # 6) kayıt
        self._log("cycles.jsonl", cyc)
        for d in decisions:
            self._log("shadow_orders.jsonl", d)
        return cyc


def main():
    p = argparse.ArgumentParser(description="BLOK27.3 Shadow Canary "
                                            "(EMİR YOK — salt gözlem)")
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p.add_argument("--interval", type=float, default=300.0)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    sc = ShadowCanary(args.dir)
    if args.once:
        print(json.dumps(sc.cycle(), indent=1, default=str))
        return 0
    while True:
        try:
            sc.cycle()
        except Exception as e:
            sc._log("cycles.jsonl", {"t": int(time.time() * 1000),
                                     "cycle_error": f"{type(e).__name__}: {e}"})
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
