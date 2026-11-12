#!/usr/bin/env python3
"""Apply K-4 wiring to open_coin in auto_trader.py (in place, with a backup).

Wraps open_coin's SL placement in the SAME cross-process SlPlacementStore intent
that ensure_sl_coverage already uses, so the two paths can never both place an SL
for one position. Strategy / leverage / scanning / sizing / MAX_OPEN untouched.

Run from the repo root:  python3 apply_k4_open_coin.py
Idempotent: refuses to run twice (detects the K-4 marker).
"""
import sys
import ast

FILE = "auto_trader.py"

# -- stable ASCII anchors bounding the region to replace (open_coin only) ----------
A = '    # Faz2.1 (K-1/K-2) pre-open reconciliation: cancel any leftover algo SL for this'
B = '    algo_id = sr.get("algoId") if isinstance(sr, dict) else None'

NEW = '''    # === K-4: open_coin SL-placement intent (full prevention of the open_coin <->
    # ensure_sl_coverage double-SL race) ===
    # Acquire the SAME cross-process SlPlacementStore intent (K-3) that
    # ensure_sl_coverage uses, keyed on (symbol, '<side>:<entryPrice>') from exchange
    # truth, so the two paths can never both place an SL for this position epoch --
    # without waiting for the <=60s deterministic dedupe. Done BEFORE the pre-open
    # reconciliation: if another process owns this placement we must not cancel its
    # SL nor place a duplicate; we skip and let the sl_status verification below
    # confirm it (or fail-closed emergency close, exactly as today).
    try:
        from sl_placement import SlPlacementStore, ACQUIRED as _SL_ACQ
        _oc_store = SlPlacementStore(SL_LEDGER_DB)
        _oc_store.init_schema()
    except Exception as _e:
        log.warning(f"K-4 SlPlacementStore kurulamadi: {_e}")
        _oc_store = None
        _SL_ACQ = "ACQUIRED"
    _oc_tok = None
    if _oc_store is not None:
        try:
            _oc_pr = binance.position_risk()
            _oc_p = next((q for q in _oc_pr if q.get("symbol") == symbol
                          and float(q["positionAmt"]) != 0), None) \\
                if isinstance(_oc_pr, list) else None
            if _oc_p is not None:
                _oc_tok = ("LONG" if side == 1 else "SHORT") + ":" + str(_oc_p.get("entryPrice"))
        except Exception as _e:
            log.warning(f"K-4 intent token alinamadi (positionRisk): {_e}")
            _oc_tok = None
    _oc_owned = (_oc_store is None or _oc_tok is None
                 or _oc_store.acquire(symbol, _oc_tok) == _SL_ACQ)

    if not _oc_owned:
        log.info(f"  BUSY: {symbol} SL yerlestirme baska process'te - open_coin atladi, dogrulamaya birakildi")
        sr = None
        algo_id = None
    else:
        # Faz2.1 (K-1/K-2) pre-open reconciliation: cancel any leftover algo SL for this
        # symbol (orphan from a prior/closed position) BEFORE placing the new one, so a
        # freshly opened position can never end up with a DUPLICATE SL.
        try:
            for _o in binance.open_algo_orders_for(symbol):
                _aid = _o.get("algoId")
                if _aid:
                    binance.cancel_algo_order(_aid)
                    log.info(f"  pre-open: eski algo SL iptal (algoId={_aid}) - tek SL garantisi")
        except Exception as e:
            log.warning(f"  pre-open SL reconciliation hatasi: {e}")

        sr = binance.place_algo_sl(symbol, trig, amount, side)
        algo_id = sr.get("algoId") if isinstance(sr, dict) else None
        if _oc_store is not None and _oc_tok is not None:
            if isinstance(sr, dict) and sr.get("algoId"):
                _oc_store.mark_placed(symbol, _oc_tok)
            else:
                _oc_store.release(symbol, _oc_tok)   # failure -> reflect reality/retry
    # === /K-4 ==='''


def apply(src: str) -> str:
    if "=== K-4: open_coin SL-placement intent" in src:
        raise SystemExit("K-4 wiring already present in open_coin — nothing to do.")
    ia = src.find(A)
    ib = src.find(B)
    if ia == -1 or ib == -1:
        raise SystemExit(
            "Anchor not found — open_coin SL block does not match expected text.\n"
            f"  start anchor found: {ia != -1}\n  end anchor found: {ib != -1}\n"
            "Apply the K-4 block manually (see the chat message)."
        )
    if src.count(A) != 1 or src.count(B) != 1:
        raise SystemExit("Anchor is not unique — aborting to avoid a wrong edit.")
    ib_end = ib + len(B)
    return src[:ia] + NEW + src[ib_end:]


def main() -> int:
    with open(FILE, encoding="utf-8") as f:
        src = f.read()
    out = apply(src)
    ast.parse(out)  # fail loudly if the result is not valid Python
    with open(FILE + ".k4.bak", "w", encoding="utf-8") as f:
        f.write(src)
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"OK: K-4 wiring applied to {FILE} (backup: {FILE}.k4.bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
