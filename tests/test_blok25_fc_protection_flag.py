"""BLOK25 F-c — an unverifiable SL-coverage check BLOCKS new entries (fail-closed),
and drift never disables protection.

Root cause (BLOK23 F-c): when ensure_sl_coverage hit a positionRisk failure it
only logged a WARNING and returned; _OPEN_UNPROTECTED stayed empty, so new
entries remained ALLOWED while open positions were effectively unverified.

Layers:
  1. should_block_new_entries purity: unprotected map OR protection_unverified.
  2. ensure_sl_coverage: positionRisk error dict / exception -> flag set; a later
     reliable read clears it.
  3. protect_then_gate: the flag blocks new entries and appears in the reasons;
     drift=True still runs the protection check (never skipped).
  4. Restart/recovery semantics: the flag is process-local state that only a
     RELIABLE coverage read clears — an unreliable one keeps it set.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import (ensure_sl_coverage, protect_then_gate,
                         should_block_new_entries)

SYM = "SOLUSDT"


class FlakyBinance:
    """position_risk returns a scripted sequence: each call pops the head.
    Values: list (reliable), dict (API error), Exception (raises)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def position_risk(self):
        self.calls += 1
        v = self.script.pop(0) if self.script else []
        if isinstance(v, Exception):
            raise v
        return v


class TestPureGate(unittest.TestCase):
    def test_empty_map_and_no_flag_allows(self):
        self.assertFalse(should_block_new_entries({}, False))

    def test_unprotected_symbol_blocks(self):
        self.assertTrue(should_block_new_entries({SYM: -2021}, False))

    def test_protection_unverified_blocks(self):
        # F-c core: the map can be EMPTY while coverage itself is unverifiable
        self.assertTrue(should_block_new_entries({}, True))

    def test_both_block(self):
        self.assertTrue(should_block_new_entries({SYM: None}, True))


class TestCoverageFlagLifecycle(unittest.TestCase):
    def setUp(self):
        self._flag = auto_trader._PROTECTION_UNVERIFIED
        self._unprot = dict(auto_trader._OPEN_UNPROTECTED)
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._PROTECTION_UNVERIFIED = False

    def tearDown(self):
        auto_trader._PROTECTION_UNVERIFIED = self._flag
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._unprot)

    def test_error_dict_sets_flag(self):
        b = FlakyBinance([{"code": -1001, "msg": "Internal error"}])
        ensure_sl_coverage(b)
        self.assertTrue(auto_trader._PROTECTION_UNVERIFIED)
        self.assertTrue(should_block_new_entries())

    def test_exception_sets_flag(self):
        b = FlakyBinance([TimeoutError("positionRisk read timeout")])
        ensure_sl_coverage(b)
        self.assertTrue(auto_trader._PROTECTION_UNVERIFIED)

    def test_reliable_read_clears_flag(self):
        b = FlakyBinance([{"code": -1001}, []])   # outage, then reliable flat
        ensure_sl_coverage(b)
        self.assertTrue(auto_trader._PROTECTION_UNVERIFIED)
        ensure_sl_coverage(b)
        self.assertFalse(auto_trader._PROTECTION_UNVERIFIED)

    def test_unreliable_read_keeps_flag(self):
        b = FlakyBinance([{"code": -1001}, {"code": -1001}])
        ensure_sl_coverage(b)
        ensure_sl_coverage(b)
        self.assertTrue(auto_trader._PROTECTION_UNVERIFIED)


class TestProtectThenGate(unittest.TestCase):
    def setUp(self):
        self._flag = auto_trader._PROTECTION_UNVERIFIED
        self._unprot = dict(auto_trader._OPEN_UNPROTECTED)
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._PROTECTION_UNVERIFIED = False
        # _verify_oneway needs a mode read; give the fakes a valid ONEWAY one
        self._orig_vo = auto_trader._verify_oneway
        auto_trader._verify_oneway = lambda b: (True, "ONEWAY")
        self._orig_eos = auto_trader._exchange_open_symbols
        auto_trader._exchange_open_symbols = lambda b: set()

    def tearDown(self):
        auto_trader._PROTECTION_UNVERIFIED = self._flag
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._unprot)
        auto_trader._verify_oneway = self._orig_vo
        auto_trader._exchange_open_symbols = self._orig_eos

    def test_outage_blocks_new_entries(self):
        b = FlakyBinance([{"code": -1001}])
        allow, reasons = protect_then_gate(b, reserver=None, drift=False,
                                           stale=False)
        self.assertFalse(allow)
        self.assertTrue(reasons["protection_unverified"])
        self.assertTrue(reasons["unprotected"])

    def test_recovery_allows_again(self):
        b = FlakyBinance([{"code": -1001}, []])
        protect_then_gate(b, reserver=None, drift=False, stale=False)
        allow, reasons = protect_then_gate(b, reserver=None, drift=False,
                                           stale=False)
        self.assertTrue(allow)
        self.assertFalse(reasons["protection_unverified"])

    def test_drift_still_runs_protection(self):
        # F-c core invariant: a reconciliation drift blocks NEW ENTRIES but the
        # protection check for OPEN positions still runs (never skipped)
        b = FlakyBinance([{"code": -1001}])
        allow, reasons = protect_then_gate(b, reserver=None, drift=True,
                                           stale=False)
        self.assertFalse(allow)
        self.assertTrue(reasons["drift"])
        self.assertEqual(b.calls, 1, "ensure_sl_coverage was skipped under drift")
        self.assertTrue(auto_trader._PROTECTION_UNVERIFIED)

    def test_healthy_allows(self):
        b = FlakyBinance([[]])
        allow, reasons = protect_then_gate(b, reserver=None, drift=False,
                                           stale=False)
        self.assertTrue(allow)
        self.assertFalse(reasons["unprotected"])


if __name__ == "__main__":
    unittest.main()
