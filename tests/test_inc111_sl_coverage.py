"""Increment 11.1 — RC-2 fix: ensure_sl_coverage uses mode-aware sl_status.

RC-2 (inc11 finding): ensure_sl_coverage counted ANY algo order for the symbol as
SL coverage. Now it routes each open position through the inc10.1 sl_status
(PRESENT/ABSENT/UNKNOWN, full mode-aware validation). These tests drive the REAL
ensure_sl_coverage() and mock only the Binance boundary.

  PRESENT -> protected, no second SL placed.
  ABSENT (ONEWAY) -> placed via the EXISTING place_algo_sl mechanism (-2021 /
          OPEN_UNPROTECTED / CRITICAL preserved).
  UNKNOWN -> never place a blind SL; OPEN_UNPROTECTED + block new entries.
  HEDGE without a valid existing SL -> fail-closed (no unsupported order sent).

Behavioural RED (revert the detection to 'any algo order = covered') is shown in
the delivery notes.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import ensure_sl_coverage, should_block_new_entries

SYM = "SOLUSDT"


def _pos(amt, symbol=SYM):
    return {"symbol": symbol, "positionAmt": str(amt), "entryPrice": "83.08"}


def _stop(symbol=SYM, status="NEW", typ="STOP_MARKET", side="SELL",
          position_side="BOTH", reduce_only="true", quantity="0.3",
          close_position=None):
    o = {"symbol": symbol, "type": typ, "algoStatus": status, "side": side,
         "positionSide": position_side, "reduceOnly": reduce_only,
         "quantity": quantity}
    if close_position is not None:
        o["closePosition"] = close_position
    return o


class FakeBinance:
    def __init__(self, positions, algo, dual=False, mode_error=False,
                 place_result=None):
        self._positions = positions
        self._algo = algo                 # {sym: list|dict|Exception}
        self._dual = dual
        self._mode_error = mode_error
        self._place_result = place_result or {"algoId": 999}
        self.placed = []

    def position_risk(self):
        return list(self._positions)

    def position_mode(self):
        if self._mode_error:
            return {"code": -1, "msg": "mode unavailable"}
        return {"dualSidePosition": self._dual}

    def open_algo_orders_raw(self, sym):
        v = self._algo.get(sym, [])
        if isinstance(v, Exception):
            raise v
        return v

    def open_algo_orders_for(self, sym):   # legacy path (used only by the RED revert)
        v = self._algo.get(sym, [])
        return v if isinstance(v, list) else []

    def filters(self, sym):
        return {"PRICE_FILTER": {"tickSize": "0.01"}}

    def _get(self, path, params=None, signed=False):
        return []                          # klines empty -> ATR falls back to default

    def place_algo_sl(self, sym, trig, qty, side):
        self.placed.append({"sym": sym, "trig": trig, "qty": qty, "side": side})
        r = self._place_result
        return r(sym) if callable(r) else r


class SlCoverageBase(unittest.TestCase):
    def setUp(self):
        auto_trader._OPEN_UNPROTECTED.clear()

    def tearDown(self):
        auto_trader._OPEN_UNPROTECTED.clear()

    def run_cov(self, fb):
        ensure_sl_coverage(fb)
        return fb


class TestSlCoveragePresent(SlCoverageBase):
    # 1. Valid ONEWAY reduceOnly STOP_MARKET covering the full position -> PRESENT.
    def test_oneway_reduceonly_present_no_place(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop()]}))
        self.assertEqual(fb.placed, [])
        self.assertNotIn(SYM, auto_trader._OPEN_UNPROTECTED)

    # 2. Valid ONEWAY closePosition STOP -> PRESENT.
    def test_oneway_closeposition_present(self):
        o = _stop(reduce_only="false", quantity="0", close_position="true")
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [o]}))
        self.assertEqual(fb.placed, [])

    # 11. Valid SL present -> no DUPLICATE SL sent.
    def test_present_no_duplicate(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop()]}))
        self.assertEqual(len(fb.placed), 0)


class TestSlCoverageAbsentPlaces(SlCoverageBase):
    # 3. A TAKE_PROFIT algo order is NOT accepted as an SL -> SL placed.
    def test_take_profit_not_coverage(self):
        tp = _stop(typ="TAKE_PROFIT_MARKET")
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [tp]}))
        self.assertEqual(len(fb.placed), 1)

    # 4. Terminal STOP_MARKET -> not PRESENT -> SL placed.
    def test_terminal_stop_not_present(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop(status="CANCELLED")]}))
        self.assertEqual(len(fb.placed), 1)

    # 5. Wrong side -> not PRESENT -> SL placed.
    def test_wrong_side(self):
        # LONG position needs a SELL stop; a BUY stop does not protect it.
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop(side="BUY")]}))
        self.assertEqual(len(fb.placed), 1)

    # 6. Wrong positionSide (ONEWAY must be BOTH/absent) -> not PRESENT -> SL placed.
    def test_wrong_position_side(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop(position_side="LONG")]}))
        self.assertEqual(len(fb.placed), 1)

    # 7. Insufficient quantity -> not full coverage -> SL placed.
    def test_insufficient_quantity(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop(quantity="0.1")]}))
        self.assertEqual(len(fb.placed), 1)

    # 12. ABSENT -> existing safety mechanism: on success the alarm clears.
    def test_absent_places_and_clears_alarm(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: []}))
        self.assertEqual(len(fb.placed), 1)
        self.assertNotIn(SYM, auto_trader._OPEN_UNPROTECTED)
        self.assertFalse(should_block_new_entries())

    # 12b. ABSENT + place rejected with -2021 -> OPEN_UNPROTECTED, block, no retry loop.
    def test_absent_minus2021_blocks(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: []},
                                      place_result={"code": -2021}))
        self.assertEqual(len(fb.placed), 1)
        self.assertEqual(auto_trader._OPEN_UNPROTECTED.get(SYM), -2021)
        self.assertTrue(should_block_new_entries())


class TestSlCoverageUnknownFailClosed(SlCoverageBase):
    # 8. SL API timeout -> UNKNOWN -> no blind SL, block new entries.
    def test_timeout_unknown(self):
        import requests
        fb = self.run_cov(FakeBinance([_pos("0.3")],
                                      {SYM: requests.exceptions.Timeout("t")}))
        self.assertEqual(fb.placed, [])
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)
        self.assertTrue(should_block_new_entries())

    # 9. SL API exception -> UNKNOWN.
    def test_exception_unknown(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: RuntimeError("boom")}))
        self.assertEqual(fb.placed, [])
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)

    # 10. API error dict -> UNKNOWN.
    def test_error_dict_unknown(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: {"code": -1, "msg": "e"}}))
        self.assertEqual(fb.placed, [])
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)

    # mode itself unverifiable -> fail-closed (no SL placed, block).
    def test_mode_unverifiable_failclosed(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [_stop()]}, mode_error=True))
        self.assertEqual(fb.placed, [])
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)


class TestSlCoverageHedge(SlCoverageBase):
    # 13. HEDGE with a valid existing closePosition SL -> PRESENT, no place.
    def test_hedge_valid_present(self):
        o = _stop(position_side="LONG", side="SELL", reduce_only="false",
                  quantity="0", close_position="true")
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: [o]}, dual=True))
        self.assertEqual(fb.placed, [])
        self.assertNotIn(SYM, auto_trader._OPEN_UNPROTECTED)

    # 14. HEDGE without a valid SL -> fail-closed: NO unsupported order, block.
    def test_hedge_absent_failclosed_no_order(self):
        fb = self.run_cov(FakeBinance([_pos("0.3")], {SYM: []}, dual=True))
        self.assertEqual(fb.placed, [])                 # never sends ONEWAY SL in HEDGE
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)
        self.assertTrue(should_block_new_entries())


class TestSlCoverageMultiple(SlCoverageBase):
    # 15. Multiple open positions evaluated separately: only the ABSENT one is placed.
    def test_multiple_positions_separate(self):
        positions = [_pos("0.3", "SOLUSDT"), _pos("-0.5", "ETHUSDT")]
        algo = {"SOLUSDT": [_stop(symbol="SOLUSDT")],          # PRESENT
                "ETHUSDT": []}                                 # ABSENT -> place
        fb = self.run_cov(FakeBinance(positions, algo))
        self.assertEqual(len(fb.placed), 1)
        self.assertEqual(fb.placed[0]["sym"], "ETHUSDT")
        self.assertNotIn("SOLUSDT", auto_trader._OPEN_UNPROTECTED)


if __name__ == "__main__":
    unittest.main()
