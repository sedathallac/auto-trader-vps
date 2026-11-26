#!/usr/bin/env python3
"""BLOK29 — LIVE CANARY RUNNER LAUNCHER (deployment tutkalı).

37e3497 çekirdeğini (canary_live/canary_risk/canary_liquidation/
canary_exchange) DEĞİŞTİRMEDEN çalıştırır. Bu dosya yalnızca:
  - env kimlik bilgilerinden BinanceFuturesClient kurar,
  - salt-okunur GET'lerle filtre/katman/marj/bakiye okur ve
    LiquidationModel'i GERÇEK veriyle inşa eder,
  - T0'ı ilk başlangıçta kalıcı olarak kaydeder,
  - SIGTERM/SIGINT ile systemd için düzgün kapanış sağlar.

GÖREV (2026-11-26) — CANARY AI BRAIN ANA SİNYAL KAYNAĞI:
  - İşlem evreni artık sabit 8 sembol DEĞİL: beynin eligible contract
    discovery çıktısı (futures-brain ile AYNI contract_discovery hattı)
    ana sembol evrenidir (~519 LIVE_ELIGIBLE sözleşme; sınır YOK).
  - signal_source=BRAIN_PRIMARY: giriş adayları yalnız futures-brain
    servisinin BRAIN_DB'sinden (salt-okunur) gelir; eski H1 strateji
    döngüsü bağımsız emir AÇAMAZ.
  - Beyin önerisi emir değildir: suggested_notional politika tavanıyla
    kırpılır; son kararı bağımsız CROSS risk kapısı verir.

Emir yetkisi: canary_live.run() içindeki giriş zinciri, tüm kapılar
(LIQUIDATION_RISK_UNVERIFIED, feed, mutabakat, ilk-pozisyon, T0, ENTRY
PAUSE, yön kilitleri) geçilmedikçe emir GÖNDERMEZ (fail-closed).

Ortam değişkenleri:
  BINANCE_KEY / BINANCE_SECRET  (zorunlu)
  CANARY_DB     (varsayılan /var/lib/futures-canary/canary_live.db)
  CANARY_LOG_DIR (varsayılan /var/log/futures-canary)
  BRAIN_DB      (varsayılan /var/lib/futures-canary-brain/brain.db —
                 futures-brain.service ile AYNI dosya, salt-okunur)
  BRAIN_DECISION_MAX_AGE_MS (varsayılan 5h — eski beyin döngüsünden aday
                 VERİLMEZ, fail-closed)
"""
import json
import os
import signal
import sqlite3
import sys
import time

from canary_exchange import BinanceFuturesClient, ExchangeError
from canary_live import LiveCanary, CLIENT_PREFIX
from canary_liquidation import (LiquidationModel, parse_leverage_brackets,
                                POSITION_MODE_ONEWAY, POSITION_MODE_HEDGE)
from canary_account_adapter import BinanceHttpTransport
from canary_account_risk_gate import AccountRiskGate
from canary_brain_adapter import (BrainDecisionSource,
                                  SIGNAL_SOURCE_BRAIN, DEFAULT_MAX_AGE_MS)
from canary_brain import eligible_symbols, MODEL_VERSION
from contract_discovery import discover_contracts


def _load_t0(db_path):
    """T0 kalıcılığı (§7): restart sonrası geçmiş sinyal yeni emir
    üretemez — T0 ilk başlangıçta yazılır, sonrasında DB'den okunur."""
    if os.path.exists(db_path):
        try:
            con = sqlite3.connect(db_path)
            try:
                row = con.execute(
                    "SELECT value FROM live_state WHERE key='t0'"
                ).fetchone()
                if row:
                    return int(row[0]), False
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return int(time.time() * 1000), True


def _build_universe(ex):
    """Ana sembol evreni — BEYNİN eligible contract discovery çıktısı.

    futures-brain.service ile AYNI hattı kullanır (contract_discovery.
    discover_contracts + canary_brain.eligible_symbols): fapi
    exchangeInfo'dan PERPETUAL + USDT quote + USDT margin + TRADING
    sözleşmeler. Sabit 8 sembol listesi BURADA KULLANILMAZ; aday sınırı
    (Top-8/10/20) YOK. Evren kurulamazsa boş döner → çağıran FATAL ile
    durur (eski 8 sembole sessiz geri dönüş YOK — fail-closed)."""
    info = ex.exchange_info()
    res = discover_contracts(fapi_info=info, dapi_info=None)
    return eligible_symbols(res), res


def _build_filters(ex, symbols):
    """Evren sembolleri için gerçek exchangeInfo filtreleri (public GET).
    Evren dışı sembol süzgesi YOK — beyin evreninin tamamı işlenebilir;
    hiçbir uygun sembol eski 8 sembol listesi nedeniyle dışarıda KALMAZ."""
    info = ex.exchange_info()
    wanted = set(symbols)
    filters = {}
    for s in info.get("symbols", []):
        if s.get("symbol") not in wanted:
            continue
        f = {x["filterType"]: x for x in s.get("filters", [])}
        if "PRICE_FILTER" not in f or "LOT_SIZE" not in f:
            continue
        filters[s["symbol"]] = {
            "tickSize": f["PRICE_FILTER"]["tickSize"],
            "stepSize": f["LOT_SIZE"]["stepSize"],
            "minQty": f["LOT_SIZE"]["minQty"],
            "minNotional": str(
                f.get("MIN_NOTIONAL", {}).get("notional", "0")),
            "quantityPrecision": str(s.get("quantityPrecision", 8)),
            "pricePrecision": str(s.get("pricePrecision", 8)),
        }
    return filters


def _build_liq_model(ex):
    """Gerçek leverageBracket + positionRisk + positionSide/dual + bakiye
    (§5/§6 — hepsi imzalı salt-okunur GET). positionRisk'ten sembol
    süzgesi YOK: pozisyonu olan her sembol (yabancı dahil) görünür."""
    lb = ex.leverage_brackets()
    pr = ex.position_risk()
    pm = ex.position_mode()
    bal = ex.balance()
    margin_modes = {}
    leverages = {}
    for r in pr:
        sym = r.get("symbol")
        if sym:
            margin_modes[sym] = str(r.get("marginType", "")).lower()
            try:
                leverages[sym] = int(r.get("leverage", 0))
            except (TypeError, ValueError):
                pass
    wallet = None
    for b in bal:
        if b.get("asset") == "USDT":
            # cross likidasyon için etkin cüzdan: crossWalletBalance
            wallet = float(b.get("crossWalletBalance")
                           or b.get("balance") or 0.0)
    one_way = (pm.get("dualSidePosition") is False)
    mode = POSITION_MODE_ONEWAY if one_way else POSITION_MODE_HEDGE
    liq = LiquidationModel(
        brackets_by_symbol=parse_leverage_brackets(lb),
        margin_modes=margin_modes, leverages=leverages,
        position_mode=mode, wallet_usdt=wallet)
    return liq, {"one_way": one_way, "wallet_usdt": wallet,
                 "margin_modes": margin_modes, "leverages": leverages}


def main():
    db_path = os.environ.get("CANARY_DB",
                             "/var/lib/futures-canary/canary_live.db")
    log_dir = os.environ.get("CANARY_LOG_DIR", "/var/log/futures-canary")
    key = os.environ.get("BINANCE_KEY")
    secret = os.environ.get("BINANCE_SECRET")
    if not key or not secret:
        print("FATAL: BINANCE_KEY/BINANCE_SECRET yok", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    ex = BinanceFuturesClient(key, secret)
    t0, first_start = _load_t0(db_path)

    # ANA SEMBOL EVRENİ (görev §1): beynin eligible contract discovery
    # çıktısı (~519). Boşsa BAŞLAMA — eski 8 sembole geri dönüş YOK.
    universe, discovery = _build_universe(ex)
    if not universe:
        print("FATAL: beyin evreni kurulamadı (discovery boş) — eski 8 "
              "sembol listesine geri DÖNÜLMÜYOR", file=sys.stderr)
        return 2

    # salt-okunur kurulum verileri (kapılar burada da fail-closed:
    # eksik veri → model UNVERIFIED → canary_live girişi bloklar)
    filters = _build_filters(ex, universe)
    liq, acct_info = _build_liq_model(ex)
    verify = {s: liq.verify(s)[0] for s in universe}

    # beyin karar kaynağı — futures-brain.service BRAIN_DB (salt-okunur;
    # execution beynin araştırma belleğine ASLA yazmaz)
    brain_db = os.environ.get("BRAIN_DB",
                              "/var/lib/futures-canary-brain/brain.db")
    brain = BrainDecisionSource(
        db_path=brain_db,
        max_age_ms=int(os.environ.get("BRAIN_DECISION_MAX_AGE_MS",
                                      str(DEFAULT_MAX_AGE_MS))),
        expected_model_version=MODEL_VERSION)

    lc = LiveCanary(db_path, ex, symbols=universe, t0_ms=t0,
                    filters=filters, liq_model=liq, log_dir=log_dir,
                    signal_source=SIGNAL_SOURCE_BRAIN, brain_source=brain)
    if first_start:
        lc._set_state("t0", str(t0))

    # B.1 İŞ 4: hesap-seviyesi CROSS risk kapısını bağla.
    # Eşikler YALNIZ operatör konfiginden (env); ayarlı DEĞİLSE kapı
    # güvenli varsayılan BLOCK üretir (kaynak kodda eşik icat EDİLMEZ).
    # Öneriler (dayanak canary_account_risk_gate docstring):
    #   CANARY_ACCOUNT_MAX_AGE_MS=30000, CANARY_ACCOUNT_MARGIN_BUFFER_USDT=50
    # (görev §3: öneri operatör onayı OLMADAN canlı ayara YAZILMAZ)
    def _cfg_int(name):
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return None
        try:
            return int(raw)
        except ValueError:
            lc._log("live_orders.jsonl", {
                "t": int(time.time() * 1000), "kind": "ACCOUNT_GATE_CONFIG",
                "error": f"{name} tamsayı değil — None (BLOCK varsayılanı)"})
            return None

    def _cfg_float(name):
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return None
        try:
            return float(raw)
        except ValueError:
            lc._log("live_orders.jsonl", {
                "t": int(time.time() * 1000), "kind": "ACCOUNT_GATE_CONFIG",
                "error": f"{name} sayı değil — None (BLOCK varsayılanı)"})
            return None

    gate = AccountRiskGate(
        BinanceHttpTransport(key, secret),
        brackets_by_symbol=liq.brackets,   # kurulumda fetch edildi (ekstra çağrı YOK)
        client_prefix=CLIENT_PREFIX,
        max_age_ms=_cfg_int("CANARY_ACCOUNT_MAX_AGE_MS"),
        margin_buffer_usdt=_cfg_float("CANARY_ACCOUNT_MARGIN_BUFFER_USDT"))
    lc.rm.account_gate = gate
    lc._log("live_orders.jsonl", {
        "t": int(time.time() * 1000), "kind": "ACCOUNT_GATE_ATTACHED",
        "max_age_ms_configured": gate.max_age_ms is not None,
        "margin_buffer_configured": gate.margin_buffer_usdt is not None,
        "bracket_symbols": len(gate.brackets_by_symbol),
        "note": "esikler operatör env konfiginden; yoksa guvenli "
                "varsayilan BLOCK (canli giris izni DEGILDIR)"})
    brain_cycle = brain.latest_cycle()
    lc._log("live_orders.jsonl", {
        "t": int(time.time() * 1000), "kind": "RUNNER_START",
        "first_start": first_start, "t0": t0,
        "signal_source": SIGNAL_SOURCE_BRAIN,
        "brain_model_version_expected": MODEL_VERSION,
        "brain_db": brain_db,
        "brain_latest_cycle": brain_cycle,
        "universe_symbols": len(universe),
        "universe_discovery": {
            "coins": discovery.coin_count,
            "contracts_fapi": sum(
                1 for coin in discovery.coins for c in coin.contracts
                if c.venue == "fapi"),
            "eligible": len(universe)},
        "filters_loaded": len(filters),
        "liq_verified": verify, "account": acct_info,
        "entry_policy": lc.entry_policy,
        "entry_paused": lc.entry_paused,
        "commit": os.environ.get("CANARY_COMMIT", "unspecified")})

    def _shutdown(signum, frame):
        lc._log("live_orders.jsonl", {"t": int(time.time() * 1000),
                                      "kind": "RUNNER_SIGNAL",
                                      "signal": signum})
        lc.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    return lc.run()


if __name__ == "__main__":
    sys.exit(main())
