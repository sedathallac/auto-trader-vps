"""Increment 8 — orphan_killer must NOT skip SL coverage for open positions when
the orphan check list (to_check) is empty.

Root cause (orphan_killer.scan_and_kill): ensure_sl_coverage(b) is only reached at
the very end, after the orphan scan+cancel. When to_check is empty (open positions
exist but there are no closed-symbol orphans to clean) the function returns early
at `if not to_check: return`, so open positions' SL coverage is skipped entirely
(the SOLUSDT class: an open SOL position is in open_syms, excluded from to_check,
early return, SL never checked).

Fix: run ensure_sl_coverage for open positions independent of the orphan list.
RED on current code (scenario 1 fails: SL coverage not called); GREEN after.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orphan_killer as ok
import auto_trader as at


class _FakeBinance:
    def __init__(self, positions, algo=None):
        self._positions = positions
        self._algo = algo or {}
        self.cancels = []
    def position_risk(self):
        return self._positions
    def open_algo_orders_for(self, sym):
        return self._algo.get(sym, [])
    def cancel_algo_order(self, aid):
        self.cancels.append(aid)
        return {"code": "200"}


def _pos(sym, amt):
    return {"symbol": sym, "positionAmt": str(amt)}


class _Base(unittest.TestCase):
    def setUp(self):
        self.calls = {"ensure": 0}
        self._cov = at.ensure_sl_coverage
        self._hb = ok.check_heartbeat
        self._load = ok.load_tracked
        self._save = ok.save_tracked
        self._sleep = ok.time.sleep
        at.ensure_sl_coverage = lambda b: self.calls.__setitem__("ensure", self.calls["ensure"] + 1)
        ok.check_heartbeat = lambda: True
        ok.load_tracked = lambda: set()
        ok.save_tracked = lambda s: None
        ok.time.sleep = lambda *a, **k: None

    def tearDown(self):
        at.ensure_sl_coverage = self._cov
        ok.check_heartbeat = self._hb
        ok.load_tracked = self._load
        ok.save_tracked = self._save
        ok.time.sleep = self._sleep


class TestOrphanKillerProtection(_Base):
    def test_empty_tocheck_open_position_runs_sl_coverage(self):
        # open SOL, tracked == open -> to_check == [] -> MUST still run SL coverage
        b = _FakeBinance([_pos("SOLUSDT", "-0.3")])
        ok.scan_and_kill(binance=b)
        self.assertEqual(self.calls["ensure"], 1, "SL coverage skipped for open position")

    def test_empty_tocheck_no_open_no_work(self):
        b = _FakeBinance([_pos("SOLUSDT", "0")])  # no open positions
        ok.scan_and_kill(binance=b)
        self.assertEqual(self.calls["ensure"], 0, "unnecessary SL coverage with no open position")

    def test_orphan_and_open_both_run(self):
        # open SOL + a closed-symbol orphan (BTC has a stray algo order)
        ok.load_tracked = lambda: {"BTCUSDT"}
        b = _FakeBinance([_pos("SOLUSDT", "-0.3")],
                         algo={"BTCUSDT": [{"algoId": 55, "triggerPrice": "1"}]})
        ok.scan_and_kill(binance=b)
        self.assertEqual(self.calls["ensure"], 1)        # protection ran for open SOL
        self.assertIn(55, b.cancels)                     # orphan cancelled

    def test_sl_coverage_runs_when_present_or_missing(self):
        # orphan_killer's job is to CALL protection; the no-double-SL / UNKNOWN
        # handling lives in ensure_sl_coverage (tested in inc9). Here: called once.
        b = _FakeBinance([_pos("SOLUSDT", "-0.3"), _pos("ETHUSDT", "0.5")])
        ok.scan_and_kill(binance=b)
        self.assertEqual(self.calls["ensure"], 1)        # one call covers all open

    def test_multiple_open_positions_single_coverage_call(self):
        b = _FakeBinance([_pos("SOLUSDT", "-0.3"), _pos("ETHUSDT", "0.5"), _pos("BTCUSDT", "0.01")])
        ok.scan_and_kill(binance=b)
        self.assertEqual(self.calls["ensure"], 1)

    def test_positionrisk_error_uncertain_no_coverage_no_crash(self):
        b = _FakeBinance({"code": -1130, "msg": "positionRisk down"})  # not a list
        ok.scan_and_kill(binance=b)  # must not raise
        self.assertEqual(self.calls["ensure"], 0)        # uncertain -> no action claimed

    def test_positionrisk_exception_uncertain(self):
        class _B(_FakeBinance):
            def position_risk(self):
                raise TimeoutError("read timeout")
        # scan_and_kill should not raise out (defensive) OR the caller loop catches;
        # here we assert it doesn't call coverage on an unknown position state.
        try:
            ok.scan_and_kill(binance=_B([]))
        except TimeoutError:
            pass
        self.assertEqual(self.calls["ensure"], 0)


if __name__ == "__main__":
    unittest.main()
