"""Faz2 — sl_status must recognise the REAL live Binance openAlgoOrders format.

LIVE BUG (observed in production 2026-11-12): a correctly-placed ONEWAY reduceOnly
STOP_MARKET SL was classified ABSENT, so every new position was emergency-closed.
Root cause: the live openAlgoOrders entry carries the stop type in `orderType`
('STOP_MARKET') with `algoType` = 'CONDITIONAL'; sl_status read only `type`/`algoType`
-> picked 'CONDITIONAL' -> 'STOP' not in it -> skipped -> ABSENT. The fixtures used
`type: STOP_MARKET` and never caught it. Fix: read `orderType` first.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import BinanceHBCloseAdapter, SL_PRESENT

SYM = "SOLUSDT"


class RawBinance:
    def __init__(self, raw):
        self._raw = raw

    def open_algo_orders_raw(self, symbol):
        return self._raw


def _live_algo():
    # exact shape returned by GET /fapi/v1/openAlgoOrders in production
    return {"algoId": 2000001660786683, "clientAlgoId": "zzTTngZrSinfn8XmdKACHt",
            "algoType": "CONDITIONAL", "orderType": "STOP_MARKET", "symbol": SYM,
            "side": "SELL", "positionSide": "BOTH", "timeInForce": "GTC",
            "quantity": "0.3", "algoStatus": "NEW", "triggerPrice": "80.41",
            "closePosition": False, "reduceOnly": True, "workingType": "CONTRACT_PRICE"}


class TestLiveSlFormat(unittest.TestCase):
    def _sl(self, order, side="LONG", amt="0.3"):
        a = BinanceHBCloseAdapter(RawBinance([order]), hb=None)
        return a.sl_status(SYM, side, mode="ONEWAY", position_amt=amt)

    def test_live_oneway_reduceonly_stop_is_present(self):
        # a LONG 0.3 position's protective SELL STOP_MARKET reduceOnly qty=0.3 -> PRESENT
        self.assertEqual(self._sl(_live_algo()), SL_PRESENT)

    def test_orderType_field_is_read(self):
        # even without `type`/`algoType`=STOP, orderType must drive classification
        o = _live_algo(); o.pop("algoType", None)
        self.assertEqual(self._sl(o), SL_PRESENT)

    def test_legacy_type_field_still_works(self):
        # the old fixture shape (type: STOP_MARKET) must keep working
        o = {"symbol": SYM, "type": "STOP_MARKET", "algoStatus": "NEW", "side": "SELL",
             "positionSide": "BOTH", "reduceOnly": "true", "quantity": "0.3"}
        self.assertEqual(self._sl(o), SL_PRESENT)


if __name__ == "__main__":
    unittest.main()
