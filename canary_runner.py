#!/usr/bin/env python3
"""BLOK29 — LIVE CANARY RUNTIME LAUNCHER (deployment tutkalı).

37e3497 çekirdeğini (canary_live/canary_risk/canary_liquidation/
canary_exchange) DEĞİŞTİRMEDEN çalıştırır. Bu dosya yalnızca:
  - env kimlik bilgilerinden BinanceFuturesClient kurar,
  - salt-okunur GET'lerle filtre/katman/marj/bakiye okur ve
    LiquidationModel'i GERÇEK veriyle inşa eder,
  - T0'ı ilk başlangıçta kalıcı olarak kaydeder,
  - SIGTERM/SIGINT ile systemd için düzgün kapanış sağlar.

Emir yetkisi: canary_live.run() içindeki giriş zinciri, tüm kapılar
(LIQUIDATION_RISK_UNVERIFIED, feed, mutabakat, ilk-pozisyon, T0)
geçilmedikçe emir GÖNDERMEZ (fail-closed).

Ortam değişkenleri:
  BINANCE_KEY / BINANCE_SECRET  (zorunlu)
  CANARY_DB     (varsayılan /var/lib/futures-canary/canary_live.db)
  CANARY_LOG_DIR (varsayılan /var/log/futures-canary)
"""
import json
import os
import signal
import sqlite3
import sys
import time

from canary_exchange import BinanceFuturesClient, ExchangeError
from canary_live import LiveCanary, SYMBOLS
from canary_liquidation import (LiquidationModel, parse_leverage_brackets,
                                POSITION_MODE_ONEWAY, POSITION_MODE_HEDGE)


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


def _build_filters(ex):
    """H1 evreni için gerçek exchangeInfo filtreleri (public GET)."""
    info = ex.exchange_info()
    filters = {}
    for s in info.get("symbols", []):
        if s.get("symbol") not in SYMBOLS:
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
    (§5/§6 — hepsi imzalı salt-okunur GET)."""
    lb = ex.leverage_brackets()
    pr = ex.position_risk()
    pm = ex.position_mode()
    bal = ex.balance()
    margin_modes = {}
    leverages = {}
    for r in pr:
        sym = r.get("symbol")
        if sym in SYMBOLS:
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

    # salt-okunur kurulum verileri (kapılar burada da fail-closed:
    # eksik veri → model UNVERIFIED → canary_live girişi bloklar)
    filters = _build_filters(ex)
    liq, acct_info = _build_liq_model(ex)
    verify = {s: liq.verify(s)[0] for s in SYMBOLS}

    lc = LiveCanary(db_path, ex, symbols=SYMBOLS, t0_ms=t0,
                    filters=filters, liq_model=liq, log_dir=log_dir)
    if first_start:
        lc._set_state("t0", str(t0))
    lc._log("live_orders.jsonl", {
        "t": int(time.time() * 1000), "kind": "RUNNER_START",
        "first_start": first_start, "t0": t0,
        "symbols": SYMBOLS, "filters_loaded": len(filters),
        "liq_verified": verify, "account": acct_info,
        "entry_policy": lc.entry_policy,
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
