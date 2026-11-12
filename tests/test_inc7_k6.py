"""Increment 7 — K6: reconciliation drift must NOT disable the protection check.

new_entry_allowed is the pure new-entry gate. protect_then_gate binds to the real
flow: it runs ensure_sl_coverage (protection) REGARDLESS of drift/stale, then
decides new-entry. Tests spy on ensure_sl_coverage to prove protection runs even
under drift, while new entries are blocked.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at


class TestNewEntryAllowed(unittest.TestCase):
    def test_all_clear(self):
        self.assertTrue(at.new_entry_allowed(drift=False, stale=False, mode_ok=True, unprotected=False))

    def test_drift_blocks(self):
        self.assertFalse(at.new_entry_allowed(drift=True, stale=False, mode_ok=True, unprotected=False))

    def test_stale_blocks(self):
        self.assertFalse(at.new_entry_allowed(drift=False, stale=True, mode_ok=True, unprotected=False))

    def test_mode_not_ok_blocks(self):
        self.assertFalse(at.new_entry_allowed(drift=False, stale=False, mode_ok=False, unprotected=False))

    def test_unprotected_blocks(self):
        self.assertFalse(at.new_entry_allowed(drift=False, stale=False, mode_ok=True, unprotected=True))


class _Reconcile:
    promoted = []
    released = []
    kept = []


class _FakeReserver:
    def reconcile(self, eos):
        return _Reconcile()


class TestProtectThenGate(unittest.TestCase):
    def setUp(self):
        self.calls = {"ensure": 0}
        self._ensure = at.ensure_sl_coverage
        self._verify = at._verify_oneway
        self._eos = at._exchange_open_symbols
        at.ensure_sl_coverage = lambda b: self.calls.__setitem__("ensure", self.calls["ensure"] + 1)
        at._exchange_open_symbols = lambda b: set()
        at._OPEN_UNPROTECTED.clear()

    def tearDown(self):
        at.ensure_sl_coverage = self._ensure
        at._verify_oneway = self._verify
        at._exchange_open_symbols = self._eos
        at._OPEN_UNPROTECTED.clear()

    def _set_mode(self, ok, mode="ONEWAY"):
        at._verify_oneway = lambda b: (ok, mode)

    def test_no_drift_sl_present_allows(self):
        self._set_mode(True)
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=False, stale=False)
        self.assertEqual(self.calls["ensure"], 1)
        self.assertTrue(allow)

    def test_drift_runs_protection_but_blocks_entry(self):
        self._set_mode(True)
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=True, stale=False)
        self.assertEqual(self.calls["ensure"], 1, "protection skipped on drift")
        self.assertFalse(allow, "new entry allowed despite drift")

    def test_drift_with_unprotected_runs_protection(self):
        self._set_mode(True)
        at._OPEN_UNPROTECTED["SOLUSDT"] = -2021  # SL missing/unprotected
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=True, stale=False)
        self.assertEqual(self.calls["ensure"], 1)
        self.assertFalse(allow)
        self.assertTrue(r["unprotected"])

    def test_drift_with_mode_unknown_runs_protection(self):
        self._set_mode(False, "UNKNOWN")  # API timeout / HEDGE / unverifiable
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=True, stale=False)
        self.assertEqual(self.calls["ensure"], 1)
        self.assertFalse(allow)
        self.assertFalse(r["mode_ok"])

    def test_stale_runs_protection_blocks_entry(self):
        self._set_mode(True)
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=False, stale=True)
        self.assertEqual(self.calls["ensure"], 1)
        self.assertFalse(allow)


if __name__ == "__main__":
    unittest.main()
