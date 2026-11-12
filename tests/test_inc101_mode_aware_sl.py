"""Increment 10.1 — mode-aware SL validation against the real Binance contract.

Production places the SL via place_algo_sl: algoType=CONDITIONAL, type=STOP_MARKET,
side=SELL(long)/BUY(short), quantity=abs(positionAmt) (FULL), reduceOnly=true, NO
positionSide/closePosition (a ONEWAY reduceOnly STOP_MARKET). sl_status accepts that
AND the other forms Binance actually supports, and PRESENT requires FULL coverage:

  ONEWAY : positionSide BOTH/absent, close-direction side, and (closePosition=true OR
           reduceOnly=true with quantity >= full position).
  HEDGE  : positionSide == this side, close-direction side, STOP_MARKET,
           closePosition=true, reduceOnly NOT true (reduceOnly is rejected in hedge).

The 12 required scenarios (incl. HEDGE migration end-to-end) are asserted GREEN;
the behavioural RED (loosening to the pre-10.1 'any live reduceOnly STOP = PRESENT')
is shown in the delivery notes.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import (BinanceHBCloseAdapter, SL_PRESENT, SL_ABSENT, SL_UNKNOWN)
from close_coordinator import ONEWAY, HEDGE
from migration import migrate_all, block_new_entries, MIGRATED, UNPROTECTED

SYM = "SOLUSDT"


class RawBinance:
    def __init__(self, raw):
        self._raw = raw

    def open_algo_orders_raw(self, symbol):
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _sl(orders, position_side, mode, position_amt):
    return BinanceHBCloseAdapter(RawBinance(orders), hb=None).sl_status(
        SYM, position_side, mode=mode, position_amt=position_amt)


def _oneway_reduceonly(qty="0.3"):
    return {"symbol": SYM, "type": "STOP_MARKET", "algoStatus": "NEW",
            "side": "BUY", "positionSide": "BOTH", "reduceOnly": "true",
            "closePosition": "false", "quantity": qty}


def _oneway_closepos():
    return {"symbol": SYM, "type": "STOP_MARKET", "algoStatus": "NEW",
            "side": "BUY", "positionSide": "BOTH", "closePosition": "true"}


def _hedge_closepos(position_side, side):
    return {"symbol": SYM, "type": "STOP_MARKET", "algoStatus": "NEW",
            "side": side, "positionSide": position_side, "closePosition": "true"}


class TestModeAwareSl(unittest.TestCase):
    # 1. ONEWAY reduceOnly SL covering the full position -> PRESENT (the prod form).
    def test_oneway_reduceonly_full_present(self):
        self.assertEqual(_sl([_oneway_reduceonly("0.3")], "SHORT", ONEWAY, "0.3"),
                         SL_PRESENT)

    # 2. ONEWAY closePosition SL -> PRESENT.
    def test_oneway_closeposition_present(self):
        self.assertEqual(_sl([_oneway_closepos()], "SHORT", ONEWAY, "0.3"), SL_PRESENT)

    # 3. HEDGE LONG closePosition SL -> PRESENT (side SELL, positionSide LONG).
    def test_hedge_long_closeposition_present(self):
        self.assertEqual(_sl([_hedge_closepos("LONG", "SELL")], "LONG", HEDGE, "0.3"),
                         SL_PRESENT)

    # 4. HEDGE SHORT closePosition SL -> PRESENT (side BUY, positionSide SHORT).
    def test_hedge_short_closeposition_present(self):
        self.assertEqual(_sl([_hedge_closepos("SHORT", "BUY")], "SHORT", HEDGE, "0.3"),
                         SL_PRESENT)

    # 5. HEDGE reduceOnly combo is rejected by Binance -> not a valid SL -> ABSENT.
    def test_hedge_reduceonly_rejected(self):
        bad = {"symbol": SYM, "type": "STOP_MARKET", "algoStatus": "NEW",
               "side": "SELL", "positionSide": "LONG", "reduceOnly": "true",
               "quantity": "0.3"}
        self.assertEqual(_sl([bad], "LONG", HEDGE, "0.3"), SL_ABSENT)

    # 6. Wrong positionSide (hedge SL guards the other side) -> not PRESENT.
    def test_wrong_position_side(self):
        self.assertEqual(_sl([_hedge_closepos("LONG", "SELL")], "SHORT", HEDGE, "0.3"),
                         SL_ABSENT)

    # 7. Wrong side (close direction) -> not PRESENT.
    def test_wrong_side(self):
        # SHORT position needs a BUY stop; a SELL stop does not protect it.
        bad = _oneway_reduceonly("0.3"); bad["side"] = "SELL"
        self.assertEqual(_sl([bad], "SHORT", ONEWAY, "0.3"), SL_ABSENT)

    # 8. Insufficient quantity (partial) -> not full coverage -> ABSENT.
    def test_insufficient_quantity(self):
        self.assertEqual(_sl([_oneway_reduceonly("0.1")], "SHORT", ONEWAY, "0.3"),
                         SL_ABSENT)

    def test_missing_quantity(self):
        o = _oneway_reduceonly("0.3"); o.pop("quantity")
        self.assertEqual(_sl([o], "SHORT", ONEWAY, "0.3"), SL_ABSENT)

    # 9. Terminal algo SL -> not PRESENT.
    def test_terminal_sl(self):
        o = _oneway_reduceonly("0.3"); o["algoStatus"] = "FILLED"
        self.assertEqual(_sl([o], "SHORT", ONEWAY, "0.3"), SL_ABSENT)

    # 10. UNKNOWN API response -> UNKNOWN.
    def test_api_error_unknown(self):
        self.assertEqual(_sl({"code": -1, "msg": "err"}, "SHORT", ONEWAY, "0.3"),
                         SL_UNKNOWN)


class _HedgeBinance:
    """HEDGE-mode exchange: a LONG 0.3 position, no pre-sent orders, configurable
    raw algo response."""
    def __init__(self, algo_raw, amt="0.3"):
        self._algo_raw = algo_raw
        self._amt = amt
    def position_risk(self):
        return [{"symbol": SYM, "positionAmt": self._amt}]
    def position_mode(self):
        return {"dualSidePosition": True}  # HEDGE
    def open_orders(self, symbol):
        return []
    def open_algo_orders_raw(self, symbol):
        if isinstance(self._algo_raw, Exception):
            raise self._algo_raw
        return self._algo_raw


class _NoHB:
    def active_executors(self):
        return []


class TestHedgeMigration(unittest.TestCase):
    # 11. HEDGE migration with a valid LONG closePosition SL -> MIGRATED.
    def test_hedge_valid_sl_migrated(self):
        adapter = BinanceHBCloseAdapter(
            _HedgeBinance([_hedge_closepos("LONG", "SELL")]), _NoHB())
        res = migrate_all(adapter, [SYM])
        self.assertEqual(res[SYM].state, MIGRATED)
        self.assertEqual(res[SYM].mode, HEDGE)
        self.assertFalse(block_new_entries(res))

    # 12. HEDGE migration with an invalid SL (reduceOnly combo) -> UNPROTECTED.
    def test_hedge_invalid_sl_unprotected(self):
        bad = {"symbol": SYM, "type": "STOP_MARKET", "algoStatus": "NEW",
               "side": "SELL", "positionSide": "LONG", "reduceOnly": "true",
               "quantity": "0.3"}
        adapter = BinanceHBCloseAdapter(_HedgeBinance([bad]), _NoHB())
        res = migrate_all(adapter, [SYM])
        self.assertEqual(res[SYM].state, UNPROTECTED)
        self.assertTrue(res[SYM].critical)
        self.assertTrue(block_new_entries(res))


if __name__ == "__main__":
    unittest.main()
