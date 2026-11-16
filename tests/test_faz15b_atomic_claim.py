"""FAZ15b v4 — pending store: envelope, revision, transaction, claim, write-in-progress.

Kapsam: atomik finalizasyon claim'i, envelope şema reddi, nested transaction,
mutator saflığı (AST), state integrity hash, lock/flock/disk hata yolları ve
write-in-progress okuyucu kapıları. Tüm testler geçici dizinde çalışır.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader

MUTATORS = ("_mut_claim", "_mut_mark_side_effect", "_mut_release",
            "_mut_register", "_mut_drop")
ALLOWED_CALLS = {"_norm_symbol", "dict", "int", "float", "str", "bool", "len",
                 "set", "sorted", "isinstance", "min", "max", "abs", "round",
                 "list", "tuple", "copy", "deepcopy"}
ALLOWED_METHODS = {"setdefault", "pop", "get", "keys", "items", "values",
                   "copy", "deepcopy", "append", "add", "update"}


class _Binance:
    def __init__(self, positions=None):
        self.calls = []
        self._positions = positions if positions is not None else []

    def position_risk(self):
        self.calls.append("position_risk")
        return list(self._positions)

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.1"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}


class _HB:
    def __init__(self, ex=None):
        self._ex = ex
        self.stopped = []

    def get_executor(self, eid):
        return self._ex

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}

    def active_executors(self):
        return []


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "pending.json")
        self._orig = {
            "path": auto_trader.PENDING_FILLS_JSON,
            "flock": auto_trader.PENDING_FLOCK_PATH,
            "loaded": list(auto_trader._PENDING_LOADED),
            "fills": dict(auto_trader._PENDING_FILLS),
            "rev": auto_trader._PENDING_REVISION[0],
            "healthy": auto_trader._PENDING_STORE_HEALTHY[0],
            "err": auto_trader._PENDING_STORE_ERROR[0],
            "blocked": auto_trader._GLOBAL_ENTRY_BLOCKED[0],
            "reason": auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0],
            "wip": auto_trader._PENDING_WRITE_IN_PROGRESS[0],
            "timeout": auto_trader.PERSIST_LOCK_TIMEOUT_S,
        }
        auto_trader.PENDING_FILLS_JSON = auto_trader.Path(self.path)
        auto_trader.PENDING_FLOCK_PATH = auto_trader.Path(self.path).with_suffix(".lock")
        auto_trader._PENDING_LOADED[0] = True
        auto_trader._PENDING_FILLS = {}
        auto_trader._PENDING_REVISION[0] = 0
        auto_trader._PENDING_STORE_HEALTHY[0] = True
        auto_trader._PENDING_STORE_ERROR[0] = None
        auto_trader._GLOBAL_ENTRY_BLOCKED[0] = False
        auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = None
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = False
        auto_trader._PENDING_TX_LOCAL.active = False

    def tearDown(self):
        auto_trader.PENDING_FILLS_JSON = self._orig["path"]
        auto_trader.PENDING_FLOCK_PATH = self._orig["flock"]
        auto_trader._PENDING_LOADED[0] = self._orig["loaded"][0]
        auto_trader._PENDING_FILLS = self._orig["fills"]
        auto_trader._PENDING_REVISION[0] = self._orig["rev"]
        auto_trader._PENDING_STORE_HEALTHY[0] = self._orig["healthy"]
        auto_trader._PENDING_STORE_ERROR[0] = self._orig["err"]
        auto_trader._GLOBAL_ENTRY_BLOCKED[0] = self._orig["blocked"]
        auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = self._orig["reason"]
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = self._orig["wip"]
        auto_trader.PERSIST_LOCK_TIMEOUT_S = self._orig["timeout"]
        auto_trader._PENDING_TX_LOCAL.active = False

    def _write_envelope(self, pending, revision=1):
        with open(self.path, "w") as f:
            json.dump({"schema_version": 1, "revision": revision,
                       "pending": pending}, f)
        auto_trader._PENDING_REVISION[0] = revision

    def _disk(self):
        with open(self.path) as f:
            data = json.load(f)
        return data["pending"]

    def _seed(self, **over):
        rec = {"symbol": "SOLUSDT", "side": 2, "amount": 1.0, "sl_pct": 0.01,
               "tp_pct": 0.02, "price": 80.0, "deadline": time.time() + 300,
               "finalization_attempts": 2, "finalization_in_progress": False,
               "finalization_phase": "pre_side_effect", "finalization_complete": False,
               "finalization_blocked": False, "next_attempt_at": time.time() - 1}
        rec.update(over)
        auto_trader._PENDING_FILLS = {"E1": rec}
        self._write_envelope({"E1": rec}, revision=1)
        return rec


class TestConcurrentFinalization(StoreBase):
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
        self.assertNotIn("E1", self._disk(), "başarılı finalizasyon sonrası kayıt düşmeli")

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


class TestClaimSemantics(StoreBase):
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
        orig = auto_trader._persist_envelope
        auto_trader._persist_envelope = lambda *a, **k: False
        try:
            d, ctx = auto_trader._claim_finalization_attempt("E1")
        finally:
            auto_trader._persist_envelope = orig
        self.assertEqual(d, "error")
        self.assertEqual(ctx, None)
        self.assertEqual(auto_trader._PENDING_FILLS["E1"]["finalization_attempts"], 2,
                         "persist başarısızsa artmamalı")
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertFalse(auto_trader._PENDING_WRITE_IN_PROGRESS[0])

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


class TestRestartRecovery(StoreBase):
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


class TestSideEffectMarker(StoreBase):
    def test_marker_written_before_mutation_without_k4(self):
        self._seed()
        auto_trader._claim_finalization_attempt("E1")
        self.assertTrue(auto_trader._mark_side_effect_started("E1"))
        self.assertEqual(self._disk()["E1"]["finalization_phase"], "side_effect_started")

    def test_marker_failure_prevents_sl_mutation(self):
        self._seed()
        auto_trader._claim_finalization_attempt("E1")
        orig = auto_trader._persist_envelope
        auto_trader._persist_envelope = lambda *a, **k: False
        try:
            ok = auto_trader._finalize_open(
                _Binance(), _HB(None), "SOLUSDT", 2, 1.0, 0.01, 0.02, 80.0,
                _Binance().filters("SOLUSDT"), None, None, "E1")
        finally:
            auto_trader._persist_envelope = orig
        self.assertFalse(ok, "marker yazılamazsa finalize False dönmeli")


class TestEnvelopeSchema(StoreBase):
    def test_envelope_extra_keys_rejected(self):
        body = {"schema_version": 1, "revision": 5, "pending": {}, "extra": 1}
        with open(self.path, "w") as f:
            json.dump(body, f)
        before = open(self.path, "rb").read()
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}
        auto_trader._load_pending_fills()
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertEqual(auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0], "MALFORMED_ENVELOPE")
        self.assertEqual(open(self.path, "rb").read(), before,
                         "orijinal dosya byte düzeyinde korunmalı")

    def test_partial_reserved_keys_rejected(self):
        with open(self.path, "w") as f:
            json.dump({"schema_version": 1, "pending": {}}, f)
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._load_pending_fills()
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertEqual(auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0], "MALFORMED_ENVELOPE")

    def test_legacy_reserved_collision_rejected(self):
        with open(self.path, "w") as f:
            json.dump({"pending": {"symbol": "SOLUSDT"}}, f)
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._load_pending_fills()
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])

    def test_legacy_flat_accepted_revision_zero(self):
        with open(self.path, "w") as f:
            json.dump({"E1": {"symbol": "SOLUSDT", "deadline": time.time() + 300}}, f)
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}
        got = auto_trader._load_pending_fills()
        self.assertIn("E1", got)
        self.assertEqual(auto_trader._PENDING_REVISION[0], 0)
        self.assertTrue(auto_trader._PENDING_STORE_HEALTHY[0])

    def test_empty_envelope_with_revision_accepted(self):
        self._write_envelope({}, revision=5)
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}
        got = auto_trader._load_pending_fills()
        self.assertEqual(got, {})
        self.assertEqual(auto_trader._PENDING_REVISION[0], 5)
        self.assertTrue(auto_trader._PENDING_STORE_HEALTHY[0])

    def test_bool_and_float_revision_rejected(self):
        for bad in (True, 5.0):
            with open(self.path, "w") as f:
                json.dump({"schema_version": 1, "revision": bad, "pending": {}}, f)
            auto_trader._PENDING_LOADED[0] = False
            auto_trader._PENDING_STORE_HEALTHY[0] = True
            auto_trader._GLOBAL_ENTRY_BLOCKED[0] = False
            auto_trader._load_pending_fills()
            self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0], f"revision={bad!r}")


class TestTransaction(StoreBase):
    def test_nested_transaction_rejected(self):
        self._seed()
        seen = {}

        def _nested(pending, now):
            try:
                auto_trader._pending_transaction(auto_trader._mut_drop, "E1")
                seen["raised"] = False
            except auto_trader.PendingTxError:
                seen["raised"] = True
            return pending, True

        ok, _ = auto_trader._pending_transaction(_nested)
        self.assertTrue(ok)
        self.assertTrue(seen["raised"], "nested transaction reddedilmeli")
        self.assertFalse(auto_trader._PENDING_WRITE_IN_PROGRESS[0])
        self.assertFalse(getattr(auto_trader._PENDING_TX_LOCAL, "active", False))

    def test_pending_transaction_no_lock_inversion_deadlock(self):
        script = (
            "import sys, threading, time\n"
            "sys.path.insert(0, %r)\n"
            "import auto_trader as at\n"
            "at._PENDING_FILLS = {'E1': {'symbol': 'SOLUSDT', 'finalization_attempts': 0}}\n"
            "def _m(p, now):\n"
            "    return p, True\n"
            "def _worker():\n"
            "    for _ in range(50):\n"
            "        at._pending_transaction(_m)\n"
            "ts = [threading.Thread(target=_worker) for _ in range(4)]\n"
            "[t.start() for t in ts]\n"
            "[t.join(3) for t in ts]\n"
            "print('DONE' if not any(t.is_alive() for t in ts) else 'STUCK')\n"
        ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=5)
        self.assertIn("DONE", proc.stdout, f"deadlock: {proc.stdout} {proc.stderr}")

    def test_mutator_no_external_calls(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "auto_trader.py")
        tree = ast.parse(open(src_path).read())
        found = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in MUTATORS:
                found[node.name] = node
        self.assertEqual(sorted(found), sorted(MUTATORS), "mutator listesi eksik")
        problems = []
        for name, fn in found.items():
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if isinstance(f, ast.Name):
                    if f.id not in ALLOWED_CALLS:
                        problems.append(f"{name}:{sub.lineno} yasak çağrı {f.id}()")
                elif isinstance(f, ast.Attribute):
                    if f.attr not in ALLOWED_METHODS:
                        problems.append(f"{name}:{sub.lineno} yasak attribute çağrısı "
                                        f".{f.attr}()")
                else:
                    problems.append(f"{name}:{sub.lineno} bilinmeyen çağrı biçimi")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_waiting_result_preserves_count(self):
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "SOLUSDT"},
                                      "E2": {"symbol": "XRPUSDT"}}
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = True
        b, hb = _Binance(), _HB({"filled_amount_quote": 200.0})
        out = auto_trader._process_pending_fills(b, hb, None, None, None)
        self.assertEqual(out, {"filled": 0, "adopted": 0, "cancelled": 0,
                               "waiting": 2, "failed": 0, "released": 0})
        self.assertEqual(b.calls, [], "write-in-progress sırasında borsa çağrısı yok")
        self.assertEqual(hb.stopped, [], "executor durdurma yok")


class TestStateIntegrity(StoreBase):
    def test_state_integrity_hash_mismatch_rejected(self):
        self._seed()
        auto_trader._PENDING_REVISION[0] = 1
        with open(self.path, "w") as f:
            json.dump({"schema_version": 1, "revision": 1,
                       "pending": {"E1": {"symbol": "SOLUSDT",
                                          "finalization_attempts": 99}}}, f)
        ok = auto_trader._check_pending_revision()
        self.assertFalse(ok)
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertEqual(auto_trader._PENDING_STORE_ERROR[0], "MALFORMED_STATE")
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertEqual(auto_trader._PENDING_FILLS["E1"]["finalization_attempts"], 2,
                         "eski memory diskin üzerine yazılmamalı")

    def test_revision_rollback_blocks(self):
        self._seed()
        auto_trader._PENDING_REVISION[0] = 7
        with open(self.path, "w") as f:
            json.dump({"schema_version": 1, "revision": 3, "pending": {}}, f)
        self.assertFalse(auto_trader._check_pending_revision())
        self.assertIn("REVISION_ROLLBACK", auto_trader._PENDING_STORE_ERROR[0])

    def test_disk_revision_ahead_reloads(self):
        self._seed()
        auto_trader._PENDING_REVISION[0] = 1
        with open(self.path, "w") as f:
            json.dump({"schema_version": 1, "revision": 9,
                       "pending": {"E9": {"symbol": "BNBUSDT"}}}, f)
        self.assertTrue(auto_trader._check_pending_revision())
        self.assertIn("E9", auto_trader._PENDING_FILLS)
        self.assertEqual(auto_trader._PENDING_REVISION[0], 9)


class TestFailurePaths(StoreBase):
    def test_persist_lock_acquire_failure(self):
        self._seed()
        auto_trader.PERSIST_LOCK_TIMEOUT_S = 0.1
        held = threading.Event()
        release_ev = threading.Event()

        def _hold():
            auto_trader._PERSIST_LOCK.acquire()
            held.set()
            release_ev.wait(5)
            auto_trader._PERSIST_LOCK.release()

        t = threading.Thread(target=_hold)
        t.start()
        self.assertTrue(held.wait(5))
        try:
            ok = auto_trader._register_pending_fill("E2", {"symbol": "BNBUSDT"})
        finally:
            release_ev.set()
            t.join(5)
        self.assertFalse(ok)
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertFalse(auto_trader._PENDING_WRITE_IN_PROGRESS[0])
        self.assertIn("E1", auto_trader._PENDING_FILLS, "önceki state korunmalı")
        self.assertNotIn("E2", auto_trader._PENDING_FILLS)

    def test_flock_acquire_failure(self):
        self._seed()
        orig = auto_trader._acquire_flock
        auto_trader._acquire_flock = lambda timeout=None: None
        try:
            ok = auto_trader._register_pending_fill("E2", {"symbol": "BNBUSDT"})
        finally:
            auto_trader._acquire_flock = orig
        self.assertFalse(ok)
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertFalse(auto_trader._PENDING_WRITE_IN_PROGRESS[0])
        self.assertIn("E1", auto_trader._PENDING_FILLS)

    def test_disk_read_failure(self):
        self._seed()
        orig = auto_trader.Path.read_text

        def _boom(self, *a, **k):
            raise OSError(5, "EIO")

        auto_trader.Path.read_text = _boom
        try:
            ok = auto_trader._check_pending_revision()
        finally:
            auto_trader.Path.read_text = orig
        self.assertFalse(ok)
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertFalse(auto_trader._PENDING_WRITE_IN_PROGRESS[0])
        self.assertIn("E1", auto_trader._PENDING_FILLS, "sessizce boş state'e çevrilmemeli")


class TestWriteInProgressGates(StoreBase):
    def test_occupied_slots_blocks_during_write_in_progress(self):
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = True
        slots, pairs = auto_trader._occupied_slots(_Binance(), _HB())
        self.assertIsNone(slots)
        self.assertIsNone(pairs)

    def test_pending_symbol_sets_blocks_during_write_in_progress(self):
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = True
        keep, fin = auto_trader._pending_symbol_sets()
        self.assertIsNone(keep)
        self.assertIsNone(fin)

    def test_protect_then_gate_blocks_during_write_in_progress(self):
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = True
        allow, reasons = auto_trader.protect_then_gate(_Binance(), None, False, False)
        self.assertFalse(allow)


if __name__ == "__main__":
    unittest.main()
