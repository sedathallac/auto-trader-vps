"""Increment 10 — safe migration/handoff of already-open positions.

The migrator classifies each open position as MIGRATED / UNPROTECTED / UNKNOWN /
NOT_OPEN using EXCHANGE TRUTH only, never closing it and never auto-placing an SL.
Behavioural RED (shown by weakening the SL-UNKNOWN halt + the re-read) in the
delivery notes; here the 15 required scenarios are asserted GREEN.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from migration import (PositionMigrator, migrate_all, block_new_entries,
                       NOT_OPEN, MIGRATED, UNPROTECTED, UNKNOWN, ONEWAY, HEDGE)

SYM = "SOLUSDT"


class MockEx:
    def __init__(self, amt, mode=ONEWAY, protective=None, sl="PROTECTED",
                 stop_raises=False):
        self._amt = list(amt) if isinstance(amt, list) else [amt]
        self._mode = mode
        self._protective = protective if protective is not None else []
        self._sl = sl
        self._stop_raises = stop_raises
        self.stopped = 0
        self.amt_calls = 0
        self.cancelled = []

    def position_amt(self, s):
        v = self._amt[min(self.amt_calls, len(self._amt) - 1)]
        self.amt_calls += 1
        if isinstance(v, Exception):
            raise v
        return Decimal(str(v))

    def position_mode(self):
        if isinstance(self._mode, Exception):
            raise self._mode
        return self._mode

    def stop_executor(self, s):
        if self._stop_raises:
            raise RuntimeError("stop failed")
        self.stopped += 1

    def protective_orders(self, s):
        if isinstance(self._protective, Exception):
            raise self._protective
        return list(self._protective)

    def order_terminal(self, o):
        return bool(o.get("terminal"))

    def cancel_order(self, o):
        self.cancelled.append(o)
        return bool(o.get("cancel_ok", True))

    def sl_status(self, s):
        if isinstance(self._sl, Exception):
            raise self._sl
        return self._sl


def _m(ex):
    return PositionMigrator(ex).migrate(SYM)


class TestMigration(unittest.TestCase):
    def test_open_with_valid_sl_migrated(self):
        r = _m(MockEx(-0.3, sl="PROTECTED"))
        self.assertEqual(r.state, MIGRATED)
        self.assertEqual(r.side, "SHORT")

    def test_open_no_sl_unprotected(self):
        r = _m(MockEx(0.3, sl="UNPROTECTED"))
        self.assertEqual(r.state, UNPROTECTED)
        self.assertTrue(r.critical)

    def test_open_sl_unknown_halts(self):
        r = _m(MockEx(-0.3, sl="UNKNOWN"))
        self.assertEqual(r.state, UNKNOWN)
        self.assertTrue(r.critical)

    def test_executor_present_position_present_stops_and_migrates(self):
        ex = MockEx(-0.3, sl="PROTECTED")
        r = PositionMigrator(ex).migrate(SYM)
        self.assertEqual(r.state, MIGRATED)
        self.assertEqual(ex.stopped, 1)  # HB neutralised keep_position=True

    def test_executor_lost_position_present_not_auto_safe(self):
        # executor gone (stop is a no-op in the mock) but position open + no SL
        r = _m(MockEx(-0.3, sl="UNPROTECTED"))
        self.assertEqual(r.state, UNPROTECTED)  # NOT auto-assumed safe

    def test_local_closed_but_exchange_open(self):
        # migrate ignores local state; exchange says open -> not NOT_OPEN
        r = _m(MockEx(-0.3, sl="PROTECTED"))
        self.assertNotEqual(r.state, NOT_OPEN)

    def test_local_open_but_exchange_flat(self):
        r = _m(MockEx(0, sl="PROTECTED"))
        self.assertEqual(r.state, NOT_OPEN)

    def test_oneway_and_hedge(self):
        self.assertEqual(_m(MockEx(0.3, mode=ONEWAY)).mode, ONEWAY)
        self.assertEqual(_m(MockEx(0.3, mode=HEDGE)).mode, HEDGE)

    def test_unknown_mode_halts(self):
        r = _m(MockEx(0.3, mode="SOMETHING"))
        self.assertEqual(r.state, UNKNOWN)

    def test_multiple_positions(self):
        class _Multi:
            def __init__(self): self.pos = {"SOLUSDT": "-0.3", "ETHUSDT": "0"}
            def position_amt(self, s): return Decimal(self.pos[s])
            def position_mode(self): return ONEWAY
            def stop_executor(self, s): pass
            def protective_orders(self, s): return []
            def order_terminal(self, o): return True
            def cancel_order(self, o): return True
            def sl_status(self, s): return "PROTECTED"
        res = migrate_all(_Multi(), ["SOLUSDT", "ETHUSDT"])
        self.assertEqual(res["SOLUSDT"].state, MIGRATED)
        self.assertEqual(res["ETHUSDT"].state, NOT_OPEN)

    def test_partial_fill_reread(self):
        # position -0.3 then, after order resolution, -0.2 remains
        ex = MockEx([-0.3, -0.2], sl="PROTECTED",
                    protective=[{"orderId": 1, "terminal": False, "cancel_ok": True}])
        r = PositionMigrator(ex).migrate(SYM)
        self.assertEqual(r.state, MIGRATED)
        self.assertEqual(r.amt, Decimal("-0.2"))  # remainder re-read

    def test_closed_during_resolution_not_open(self):
        ex = MockEx([-0.3, 0], sl="PROTECTED",
                    protective=[{"orderId": 1, "terminal": False, "cancel_ok": True}])
        r = PositionMigrator(ex).migrate(SYM)
        self.assertEqual(r.state, NOT_OPEN)  # a close filled during resolution

    def test_old_order_unresolved_unknown(self):
        ex = MockEx(-0.3, sl="PROTECTED",
                    protective=[{"orderId": 1, "terminal": False, "cancel_ok": False}])
        r = PositionMigrator(ex).migrate(SYM)
        self.assertEqual(r.state, UNKNOWN)
        self.assertTrue(r.critical)

    def test_position_query_exception_unknown(self):
        r = _m(MockEx([TimeoutError("read timeout")]))
        self.assertEqual(r.state, UNKNOWN)

    def test_stop_failed_unknown(self):
        r = _m(MockEx(-0.3, stop_raises=True))
        self.assertEqual(r.state, UNKNOWN)

    def test_re_migration_idempotent_same_result(self):
        ex = MockEx(-0.3, sl="PROTECTED")
        mg = PositionMigrator(ex)
        self.assertEqual(mg.migrate(SYM).state, MIGRATED)
        self.assertEqual(mg.migrate(SYM).state, MIGRATED)  # re-run -> same, no close

    def test_block_new_entries_when_unsafe(self):
        safe = {"A": PositionMigrator(MockEx(0.3, sl="PROTECTED")).migrate(SYM)}
        self.assertFalse(block_new_entries(safe))
        unsafe = {"B": PositionMigrator(MockEx(0.3, sl="UNPROTECTED")).migrate(SYM)}
        self.assertTrue(block_new_entries(unsafe))


if __name__ == "__main__":
    unittest.main()
