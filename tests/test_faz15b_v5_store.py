"""FAZ15b v5 — pending store bütünlük testleri (gerçek iki proses dahil).

Kapsam: transaction içi disk uzlaştırması (lost update), startup recovery kilidi,
revision/hash uyuşmazlığı, tehlikeli kayıtların TTL'den korunması, dizin fsync
hatası ve confirm retry politikası. Tüm testler geçici dizinde çalışır.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD_TX = r"""
import json, sys
sys.path.insert(0, {root!r})
import auto_trader as at
at.PENDING_FILLS_JSON = at.Path({path!r})
at.PENDING_FLOCK_PATH = at.Path({path!r}).with_suffix(".lock")
at._PENDING_LOADED[0] = True
at._PENDING_FILLS = {{}}
at._PENDING_REVISION[0] = 0
at._PENDING_STORE_HEALTHY[0] = True
at._GLOBAL_ENTRY_BLOCKED[0] = False
at._PENDING_WRITE_IN_PROGRESS[0] = False
for i in range({n}):
    ok = at._register_pending_fill({tag!r} + "-" + str(i),
                                   {{"symbol": "SOLUSDT", "deadline": 0}})
    if not ok:
        print("FAIL", i)
        sys.exit(1)
print("OK")
"""

_CHILD_LOAD = r"""
import sys
sys.path.insert(0, {root!r})
import auto_trader as at
at.PENDING_FILLS_JSON = at.Path({path!r})
at.PENDING_FLOCK_PATH = at.Path({path!r}).with_suffix(".lock")
at._PENDING_LOADED[0] = False
at._PENDING_FILLS = {{}}
at._PENDING_REVISION[0] = 0
at._PENDING_STORE_HEALTHY[0] = False
at._GLOBAL_ENTRY_BLOCKED[0] = False
got = at._load_pending_fills()
print("OK" if at._PENDING_STORE_HEALTHY[0] else "UNHEALTHY")
print("REV", at._PENDING_REVISION[0])
print("KEYS", ",".join(sorted(got)))
"""


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "pending.json")
        self._saved = {
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
        }
        auto_trader.PENDING_FILLS_JSON = auto_trader.Path(self.path)
        auto_trader.PENDING_FLOCK_PATH = auto_trader.Path(self.path).with_suffix(".lock")
        auto_trader._PENDING_FILLS = {}
        auto_trader._PENDING_REVISION[0] = 0
        auto_trader._PENDING_LOADED[0] = True
        auto_trader._PENDING_STORE_HEALTHY[0] = True
        auto_trader._PENDING_STORE_ERROR[0] = None
        auto_trader._GLOBAL_ENTRY_BLOCKED[0] = False
        auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = None
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = False
        auto_trader._PENDING_TX_LOCAL.active = False

    def tearDown(self):
        auto_trader.PENDING_FILLS_JSON = self._saved["path"]
        auto_trader.PENDING_FLOCK_PATH = self._saved["flock"]
        auto_trader._PENDING_LOADED[0] = self._saved["loaded"][0]
        auto_trader._PENDING_FILLS = self._saved["fills"]
        auto_trader._PENDING_REVISION[0] = self._saved["rev"]
        auto_trader._PENDING_STORE_HEALTHY[0] = self._saved["healthy"]
        auto_trader._PENDING_STORE_ERROR[0] = self._saved["err"]
        auto_trader._GLOBAL_ENTRY_BLOCKED[0] = self._saved["blocked"]
        auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = self._saved["reason"]
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = self._saved["wip"]
        auto_trader._PENDING_TX_LOCAL.active = False

    def _write(self, pending, revision=0):
        with open(self.path, "w") as f:
            json.dump({"schema_version": 1, "revision": revision,
                       "pending": pending}, f)

    def _disk(self):
        with open(self.path) as f:
            return json.load(f)


class TestTwoProcessLostUpdate(StoreBase):
    def test_two_processes_preserve_all_writes_and_revision(self):
        self._write({}, revision=0)
        n = 12
        procs = []
        for tag in ("A", "B"):
            script = _CHILD_TX.format(root=ROOT, path=self.path, n=n, tag=tag)
            procs.append(subprocess.Popen([sys.executable, "-c", script],
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True))
        outs = [p.communicate(timeout=120) for p in procs]
        for p, (out, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, f"child failed: {out} {err}")
            self.assertIn("OK", out)
        env = self._disk()
        keys = set(env["pending"])
        expected = {f"{tag}-{i}" for tag in ("A", "B") for i in range(n)}
        self.assertEqual(keys, expected, "lost update: eksik kayıt var")
        self.assertEqual(env["revision"], 2 * n,
                         "revision toplam başarılı transaction sayısı olmalı")


class TestTwoProcessStartupRace(StoreBase):
    def test_concurrent_recovery_does_not_lose_or_corrupt(self):
        rec = {"symbol": "NEARUSDT", "side": 2, "amount": 62.0, "sl_pct": 0.01,
               "tp_pct": 0.02, "price": 3.19, "deadline": time.time() + 300,
               "finalization_attempts": 2, "finalization_in_progress": True,
               "finalization_phase": "pre_side_effect",
               "finalization_complete": False, "finalization_blocked": False}
        self._write({"E1": rec}, revision=4)
        procs = []
        for _ in range(2):
            script = _CHILD_LOAD.format(root=ROOT, path=self.path)
            procs.append(subprocess.Popen([sys.executable, "-c", script],
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True))
        outs = [p.communicate(timeout=120) for p in procs]
        for p, (out, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, f"child failed: {out} {err}")
            self.assertIn("OK", out, f"store unhealthy: {out} {err}")
            self.assertIn("KEYS E1", out)
        env = self._disk()
        self.assertIn("E1", env["pending"], "recovery kaydı kaybolmamalı")
        self.assertFalse(env["pending"]["E1"]["finalization_in_progress"])
        self.assertEqual(env["pending"]["E1"]["finalization_attempts"], 2)
        self.assertGreaterEqual(env["revision"], 4)
        self.assertLessEqual(env["revision"], 6)


class TestTransactionReconcile(StoreBase):
    def test_disk_ahead_is_adopted_before_mutator(self):
        self._write({"E9": {"symbol": "BNBUSDT"}}, revision=7)
        auto_trader._PENDING_REVISION[0] = 2
        ok = auto_trader._register_pending_fill("E1", {"symbol": "SOLUSDT"})
        self.assertTrue(ok)
        env = self._disk()
        self.assertIn("E9", env["pending"], "disk kaydı korunmalı")
        self.assertIn("E1", env["pending"])
        self.assertEqual(env["revision"], 8)

    def test_hash_mismatch_blocks_mutator(self):
        self._write({"E1": {"symbol": "SOLUSDT", "finalization_attempts": 1}}, revision=3)
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "SOLUSDT",
                                             "finalization_attempts": 99}}
        auto_trader._PENDING_REVISION[0] = 3
        before = open(self.path, "rb").read()
        ok = auto_trader._register_pending_fill("E2", {"symbol": "BNBUSDT"})
        self.assertFalse(ok)
        self.assertEqual(auto_trader._PENDING_STORE_ERROR[0], "MALFORMED_STATE")
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertEqual(open(self.path, "rb").read(), before,
                         "mutator çalışmamalı, dosya değişmemeli")
        self.assertNotIn("E2", auto_trader._PENDING_FILLS)

    def test_revision_rollback_blocks_mutator(self):
        self._write({}, revision=2)
        auto_trader._PENDING_REVISION[0] = 9
        ok = auto_trader._register_pending_fill("E1", {"symbol": "SOLUSDT"})
        self.assertFalse(ok)
        self.assertIn("REVISION_ROLLBACK", auto_trader._PENDING_STORE_ERROR[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])

    def test_unreadable_disk_blocks_mutator(self):
        self._write({}, revision=1)
        auto_trader._PENDING_REVISION[0] = 1
        with mock.patch.object(auto_trader.Path, "read_text",
                               side_effect=OSError(5, "EIO")):
            ok = auto_trader._register_pending_fill("E1", {"symbol": "SOLUSDT"})
        self.assertFalse(ok)
        self.assertIn("DISK_READ_FAILED", auto_trader._PENDING_STORE_ERROR[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])


class TestRecoveryKeepsDangerous(StoreBase):
    def _load(self):
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}
        return auto_trader._load_pending_fills()

    def test_side_effect_started_with_stale_deadline_is_kept(self):
        rec = {"symbol": "NEARUSDT", "deadline": time.time() - 99999,
               "finalization_in_progress": True,
               "finalization_phase": "side_effect_started",
               "finalization_attempts": 1}
        self._write({"E1": rec}, revision=1)
        got = self._load()
        self.assertIn("E1", got, "tehlikeli kayıt TTL yüzünden silinmemeli")
        self.assertTrue(got["E1"]["finalization_blocked"])

    def test_blocked_record_with_stale_deadline_is_kept(self):
        rec = {"symbol": "NEARUSDT", "deadline": time.time() - 99999,
               "finalization_blocked": True}
        self._write({"E1": rec}, revision=1)
        self.assertIn("E1", self._load())

    def test_confirm_blocked_record_with_stale_deadline_is_kept(self):
        rec = {"symbol": "NEARUSDT", "deadline": time.time() - 99999,
               "confirm_blocked": True, "res_id": 3}
        self._write({"E1": rec}, revision=1)
        self.assertIn("E1", self._load())

    def test_plain_stale_record_is_dropped(self):
        rec = {"symbol": "SOLUSDT", "deadline": time.time() - 99999}
        self._write({"E1": rec}, revision=1)
        self.assertNotIn("E1", self._load())

    def test_recovery_bumps_revision(self):
        rec = {"symbol": "SOLUSDT", "deadline": time.time() - 99999}
        self._write({"E1": rec}, revision=5)
        self._load()
        self.assertEqual(self._disk()["revision"], 6)


class TestDirFsyncFailure(StoreBase):
    def test_dir_fsync_failure_fails_persist(self):
        real_fsync = os.fsync

        def _fsync(fd):
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError(5, "EIO")
            return real_fsync(fd)

        with mock.patch.object(auto_trader.os, "fsync", side_effect=_fsync):
            ok = auto_trader._persist_envelope({}, 1)
        self.assertFalse(ok, "dizin fsync hatası persist'i başarısız saymalı")

    def test_dir_fsync_failure_marks_store_unhealthy(self):
        real_fsync = os.fsync

        def _fsync(fd):
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError(5, "EIO")
            return real_fsync(fd)

        with mock.patch.object(auto_trader.os, "fsync", side_effect=_fsync):
            ok = auto_trader._register_pending_fill("E1", {"symbol": "SOLUSDT"})
        self.assertFalse(ok)
        self.assertFalse(auto_trader._PENDING_STORE_HEALTHY[0])
        self.assertTrue(auto_trader._GLOBAL_ENTRY_BLOCKED[0])
        self.assertFalse(auto_trader._PENDING_WRITE_IN_PROGRESS[0])


class _FlakyReserver:
    def __init__(self):
        self.calls = 0
        self.fail = True

    def confirm(self, rid):
        self.calls += 1
        if self.fail:
            raise RuntimeError("db locked")
        return True


class TestConfirmRetryPolicy(StoreBase):
    def _ctx(self):
        return {"symbol": "NEARUSDT", "side": 2, "amount": 62.0, "sl_pct": 0.01,
                "tp_pct": 0.02, "price": 3.19, "res_id": 7,
                "deadline": time.time() + 300,
                "finalization_complete": True, "finalization_attempts": 1,
                "finalization_in_progress": False,
                "finalization_phase": "side_effect_started",
                "finalization_blocked": False}

    def test_counter_and_backoff_persist(self):
        auto_trader._PENDING_FILLS = {"E1": self._ctx()}
        auto_trader._persist_envelope(auto_trader._PENDING_FILLS, 1)
        auto_trader._PENDING_REVISION[0] = 1
        out = {"waiting": 0}
        r = _FlakyReserver()
        auto_trader._pending_confirm("E1", dict(self._ctx()), r, out)
        disk = self._disk()["pending"]["E1"]
        self.assertEqual(disk["confirm_attempts"], 1)
        self.assertGreater(disk["confirm_next_at"], time.time())
        self.assertEqual(out["waiting"], 1)
        self.assertIn("E1", self._disk()["pending"], "kayıt silinmemeli")

    def test_backoff_blocks_immediate_retry(self):
        auto_trader._PENDING_FILLS = {"E1": self._ctx()}
        auto_trader._persist_envelope(auto_trader._PENDING_FILLS, 1)
        auto_trader._PENDING_REVISION[0] = 1
        r = _FlakyReserver()
        auto_trader._pending_confirm("E1", dict(self._ctx()), r, {"waiting": 0})
        self.assertEqual(r.calls, 1)
        out = {"waiting": 0}
        auto_trader._pending_confirm("E1", dict(self._ctx()), r, out)
        self.assertEqual(r.calls, 1, "backoff sırasında confirm çağrılmamalı")
        self.assertEqual(out["waiting"], 1)

    def test_max_attempts_blocks_and_keeps_record(self):
        ctx = self._ctx()
        ctx["confirm_attempts"] = auto_trader.CONFIRM_MAX_ATTEMPTS
        auto_trader._PENDING_FILLS = {"E1": ctx}
        auto_trader._persist_envelope(auto_trader._PENDING_FILLS, 1)
        auto_trader._PENDING_REVISION[0] = 1
        r = _FlakyReserver()
        out = {"waiting": 0}
        auto_trader._pending_confirm("E1", dict(ctx), r, out)
        disk = self._disk()["pending"]["E1"]
        self.assertTrue(disk["confirm_blocked"])
        self.assertEqual(r.calls, 0, "maksimumda confirm çağrılmamalı")
        self.assertIn("E1", self._disk()["pending"])

    def test_confirm_blocked_survives_restart(self):
        ctx = self._ctx()
        ctx["confirm_attempts"] = auto_trader.CONFIRM_MAX_ATTEMPTS
        auto_trader._PENDING_FILLS = {"E1": ctx}
        auto_trader._persist_envelope(auto_trader._PENDING_FILLS, 1)
        auto_trader._PENDING_REVISION[0] = 1
        auto_trader._pending_confirm("E1", dict(ctx), _FlakyReserver(), {"waiting": 0})
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}
        got = auto_trader._load_pending_fills()
        self.assertTrue(got["E1"]["confirm_blocked"])
        self.assertEqual(got["E1"]["confirm_attempts"],
                         auto_trader.CONFIRM_MAX_ATTEMPTS)

    def test_success_drops_pending_after_confirm(self):
        auto_trader._PENDING_FILLS = {"E1": self._ctx()}
        auto_trader._persist_envelope(auto_trader._PENDING_FILLS, 1)
        auto_trader._PENDING_REVISION[0] = 1
        r = _FlakyReserver()
        r.fail = False
        out = {"waiting": 0}
        ok = auto_trader._pending_confirm("E1", dict(self._ctx()), r, out)
        self.assertTrue(ok)
        self.assertNotIn("E1", self._disk()["pending"])
        self.assertEqual(out["waiting"], 0)


if __name__ == "__main__":
    unittest.main()
