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


class TestHbOfflineOnewayGateBypass(unittest.TestCase):
    """HB_OFFLINE=1: ONEWAY doğrulaması yalnız yeni-giriş kapısında atlanır; varsayılan (0) davranış değişmez."""
    def setUp(self):
        self._saved = (at.HB_OFFLINE, at.ensure_sl_coverage, at._verify_oneway, at._exchange_open_symbols)
        at.ensure_sl_coverage = lambda b: None
        at._exchange_open_symbols = lambda b: set()
        self.verify_calls = []
        at._verify_oneway = lambda b: (self.verify_calls.append(1), (False, "UNKNOWN"))[1]   # -2015 benzeri
        at._OPEN_UNPROTECTED.clear()

    def tearDown(self):
        at.HB_OFFLINE, at.ensure_sl_coverage, at._verify_oneway, at._exchange_open_symbols = self._saved
        at._OPEN_UNPROTECTED.clear()

    def test_offline_assumes_oneway_without_calling_exchange(self):
        at.HB_OFFLINE = True
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=False, stale=False)
        self.assertTrue(allow); self.assertEqual((r["mode"], r["mode_ok"]), ("ONEWAY", True))
        self.assertEqual(self.verify_calls, [])

    def test_offline_still_blocked_by_stale_or_unprotected(self):
        at.HB_OFFLINE = True
        self.assertFalse(at.protect_then_gate(object(), _FakeReserver(), drift=False, stale=True)[0])
        at._OPEN_UNPROTECTED["X"] = None
        self.assertFalse(at.protect_then_gate(object(), _FakeReserver(), drift=False, stale=False)[0])

    def test_default_off_unknown_mode_still_fails_closed(self):
        at.HB_OFFLINE = False
        allow, r = at.protect_then_gate(binance=object(), reserver=_FakeReserver(), drift=False, stale=False)
        self.assertFalse(allow); self.assertEqual(r["mode"], "UNKNOWN"); self.assertEqual(self.verify_calls, [1])

    def test_verify_oneway_itself_unchanged_under_offline(self):
        at.HB_OFFLINE = True
        class B:
            def position_mode(self): raise RuntimeError("-2015")
        at._verify_oneway = self._saved[2]
        self.assertEqual(at._verify_oneway(B()), (False, "UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
