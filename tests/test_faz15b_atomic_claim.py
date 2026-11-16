"""FAZ15b — atomik finalizasyon claim'i (gerçek iki thread, gerçek geçici pending dosyası).

Kök neden: `_finalize_and_confirm` deneme sayacını kilitsiz artırıyordu. Aynı eid için
iki thread aynı anda `_finalize_open()` çağırabiliyor, SL iki kez yerleştirilebiliyor,
attempts iki kez artıyor ve "3 deneme" sınırı anlamsızlaşıyordu. Claim; kontrol +
artırma + in_progress + persist'i tek `_PENDING_LOCK` kritik bölgesinde yapar.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


class _Binance:
    def position_risk(self):
        return [{"symbol": "SOLUSDT", "positionAmt": "-1.0", "entryPrice": "80.0"}]

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.1"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}


class _HB:
    def __init__(self, ex):
        self._ex = ex

    def get_executor(self, eid):
        return self._ex

    def stop_executor(self, eid, keep_position=True):
        return {"status": "ok"}


class ClaimBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "pending.json")
        self._orig = (auto_trader.PENDING_FILLS_JSON, list(auto_trader._PENDING_LOADED),
                      dict(auto_trader._PENDING_FILLS), auto_trader._ENTRY_GATE_CLOSED[0])
        auto_trader.PENDING_FILLS_JSON = auto_trader.Path(self.path)
        auto_trader._PENDING_LOADED[0] = True
        auto_trader._PENDING_FILLS = {}
        auto_trader._ENTRY_GATE_CLOSED[0] = None

    def tearDown(self):
        auto_trader.PENDING_FILLS_JSON = self._orig[0]
        auto_trader._PENDING_LOADED[0] = self._orig[1][0]
        auto_trader._PENDING_FILLS = self._orig[2]
        auto_trader._ENTRY_GATE_CLOSED[0] = self._orig[3]

    def _seed(self, **over):
        rec = {"symbol": "SOLUSDT", "side": 2, "amount": 1.0, "sl_pct": 0.01,
               "tp_pct": 0.02, "price": 80.0, "deadline": time.time() + 300,
               "finalization_attempts": 2, "finalization_in_progress": False,
               "finalization_phase": "pre_side_effect", "finalization_complete": False,
               "finalization_blocked": False, "next_attempt_at": time.time() - 1}
        rec.update(over)
        auto_trader._PENDING_FILLS = {"E1": rec}
        with open(self.path, "w") as f:
            json.dump({"E1": rec}, f)
        return rec

    def _disk(self):
        with open(self.path) as f:
            return json.load(f)


class TestConcurrentFinalization(ClaimBase):
    def test_concurrent_finalization_attempts_respect_cap(self):
        self._seed()
        started = threading.Event()
        release = threading.Event()
        calls = []
        orig = auto_trader._finalize_open

        def _fake_finalize(*a, **k):
            calls.append(a[2] if len(a) > 2 else "?")
            started.set()
            release.wait(5)
            return True

        auto_trader._finalize_open = _fake_finalize
        results = {}
        try:
            def _run(tag):
                results[tag] = auto_trader._process_pending_fills(
                    _Binance(), _HB({"filled_amount_quote": 200.0}), None, None, None)

            t1 = threading.Thread(target=_run, args=("t1",))
            t1.start()
            self.assertTrue(started.wait(5), "thread 1 finalizer'ı çağırmadı")
            t2 = threading.Thread(target=_run, args=("t2",))
            t2.start()
            t2.join(5)
            self.assertFalse(t2.is_alive(), "thread 2 bloklandı (kilit ağ boyunca tutuldu)")
            release.set()
            t1.join(5)
            self.assertFalse(t1.is_alive())
        finally:
            auto_trader._finalize_open = orig

        self.assertEqual(len(calls), 1, "finalizer tam 1 kez çağrılmalı")
        self.assertEqual(results["t2"]["waiting"], 1, "thread 2 waiting dönmeli")
        self.assertEqual(results["t2"]["filled"], 0)
        self.assertEqual(results["t2"]["failed"], 0)
        disk = self._disk()
        self.assertNotIn("E1", disk, "başarılı finalizasyon sonrası kayıt düşmeli")

    def test_attempts_incremented_once_under_concurrency(self):
        self._seed()
        started = threading.Event()
        release = threading.Event()
        seen = []
        orig = auto_trader._finalize_open

        def _fake_finalize(*a, **k):
            seen.append(int(auto_trader._PENDING_FILLS["E1"]["finalization_attempts"]))
            started.set()
            release.wait(5)
            return False

        auto_trader._finalize_open = _fake_finalize
        try:
            t1 = threading.Thread(target=lambda: auto_trader._process_pending_fills(
                _Binance(), _HB({"filled_amount_quote": 200.0}), None, None, None))
            t1.start()
            self.assertTrue(started.wait(5))
            auto_trader._process_pending_fills(
                _Binance(), _HB({"filled_amount_quote": 200.0}), None, None, None)
            release.set()
            t1.join(5)
        finally:
            auto_trader._finalize_open = orig
        self.assertEqual(seen, [3], "attempts tam 3 olmalı, ikinci kez artmamalı")
        self.assertEqual(self._disk()["E1"]["finalization_attempts"], 3)

    def test_no_premature_exhausted_while_third_attempt_running(self):
        self._seed()
        started = threading.Event()
        release = threading.Event()
        orig = auto_trader._finalize_open

        def _fake_finalize(*a, **k):
            started.set()
            release.wait(5)
            return True

        auto_trader._finalize_open = _fake_finalize
        try:
            t1 = threading.Thread(target=lambda: auto_trader._process_pending_fills(
                _Binance(), _HB({"filled_amount_quote": 200.0}), None, None, None))
            t1.start()
            self.assertTrue(started.wait(5))
            out = auto_trader._process_pending_fills(
                _Binance(), _HB({"filled_amount_quote": 200.0}), None, None, None)
            self.assertFalse(auto_trader._PENDING_FILLS["E1"].get("finalization_blocked"),
                             "üçüncü deneme sürerken BLOKLU olmamalı")
            self.assertEqual(out["waiting"], 1)
            release.set()
            t1.join(5)
        finally:
            auto_trader._finalize_open = orig


class TestClaimSemantics(ClaimBase):
    def test_claim_increments_and_persists(self):
        self._seed()
        d, ctx = auto_trader._claim_finalization_attempt("E1")
        self.assertEqual(d, "claimed")
        self.assertEqual(ctx["finalization_attempts"], 3)
        self.assertTrue(ctx["finalization_in_progress"])
        self.assertEqual(self._disk()["E1"]["finalization_attempts"], 3)
        self.assertTrue(self._disk()["E1"]["finalization_in_progress"])

    def test_second_claim_waits_without_increment(self):
        self._seed()
        auto_trader._claim_finalization_attempt("E1")
        d, ctx = auto_trader._claim_finalization_attempt("E1")
        self.assertEqual(d, "waiting")
        self.assertEqual(ctx["finalization_attempts"], 3)

    def test_exhausted_after_completed_attempt_blocks(self):
        self._seed(finalization_attempts=3)
        d, ctx = auto_trader._claim_finalization_attempt("E1")
        self.assertEqual(d, "blocked")
        self.assertTrue(ctx["finalization_blocked"])
        self.assertTrue(self._disk()["E1"]["finalization_blocked"])

    def test_backoff_waits(self):
        self._seed(next_attempt_at=time.time() + 300)
        d, _ = auto_trader._claim_finalization_attempt("E1")
        self.assertEqual(d, "waiting")

    def test_persist_failure_blocks_finalizer_and_closes_gate(self):
        self._seed()
        orig = auto_trader._persist_pending_fills
        auto_trader._persist_pending_fills = lambda: False
        try:
            d, ctx = auto_trader._claim_finalization_attempt("E1")
        finally:
            auto_trader._persist_pending_fills = orig
        self.assertEqual(d, "error")
        self.assertFalse(ctx["finalization_in_progress"])
        self.assertEqual(ctx["finalization_attempts"], 2, "persist başarısızsa artmamalı")
        self.assertIsNotNone(auto_trader._entry_gate_closed())

    def test_release_complete_clears_claim(self):
        self._seed()
        _, ctx = auto_trader._claim_finalization_attempt("E1")
        auto_trader._release_finalization_claim("E1", ctx, complete=True)
        disk = self._disk()["E1"]
        self.assertFalse(disk["finalization_in_progress"])
        self.assertTrue(disk["finalization_complete"])
        self.assertNotIn("finalization_claimed_at", disk)

    def test_release_failure_sets_backoff(self):
        self._seed()
        _, ctx = auto_trader._claim_finalization_attempt("E1")
        auto_trader._release_finalization_claim("E1", ctx, error="boom")
        disk = self._disk()["E1"]
        self.assertFalse(disk["finalization_in_progress"])
        self.assertEqual(disk["last_error"], "boom")
        self.assertGreater(disk["next_attempt_at"], time.time())


class TestRestartRecovery(ClaimBase):
    def _load(self):
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}
        return auto_trader._load_pending_fills()

    def test_pre_side_effect_claim_cleared_attempts_kept(self):
        self._seed(finalization_in_progress=True, finalization_attempts=2,
                   finalization_phase="pre_side_effect")
        got = self._load()
        self.assertFalse(got["E1"]["finalization_in_progress"])
        self.assertEqual(got["E1"]["finalization_attempts"], 2)
        self.assertFalse(got["E1"].get("finalization_blocked"))

    def test_side_effect_started_crash_blocks(self):
        self._seed(finalization_in_progress=True, finalization_phase="side_effect_started")
        got = self._load()
        self.assertTrue(got["E1"]["finalization_blocked"])
        self.assertEqual(got["E1"]["last_error"], "crash_after_side_effect")

    def test_ambiguous_phase_keeps_claim(self):
        self._seed(finalization_in_progress=True, finalization_phase=None)
        got = self._load()
        self.assertTrue(got["E1"]["finalization_in_progress"],
                        "belirsiz durumda claim otomatik temizlenmemeli")


class TestSideEffectMarker(ClaimBase):
    def test_marker_written_before_mutation_without_k4(self):
        self._seed()
        auto_trader._claim_finalization_attempt("E1")
        self.assertTrue(auto_trader._mark_side_effect_started("E1"))
        self.assertEqual(self._disk()["E1"]["finalization_phase"], "side_effect_started")

    def test_marker_failure_prevents_sl_mutation(self):
        self._seed()
        auto_trader._claim_finalization_attempt("E1")
        orig = auto_trader._persist_pending_fills
        auto_trader._persist_pending_fills = lambda: False
        try:
            ok = auto_trader._finalize_open(
                _Binance(), _HB(None), "SOLUSDT", 2, 1.0, 0.01, 0.02, 80.0,
                _Binance().filters("SOLUSDT"), None, None, "E1")
        finally:
            auto_trader._persist_pending_fills = orig
        self.assertFalse(ok, "marker yazılamazsa finalize False dönmeli")


if __name__ == "__main__":
    unittest.main()
