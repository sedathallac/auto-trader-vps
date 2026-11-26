"""Regression + concurrency tests for atomic position-slot reservation.

Root cause (TOCTOU): auto_trader admits a new position by reading an
eventually-consistent external counter — Hummingbot active_executors() with
status==RUNNING (open_coin:722, run_once:1001, C6 in-loop:1044) — and only then
calls create_executor (open_coin:820). A freshly created executor is not RUNNING
immediately, so two candidates (same process next iteration, or two processes)
both read used < MAX_OPEN_POSITIONS and both open -> the 2-position cap is
breached. There is no atomic reserve-before-act.

Fix: PositionReserver reserves a slot atomically (SQLite BEGIN IMMEDIATE) in the
shared risk_ledger db BEFORE create_executor, counting distinct exchange
positions UNION outstanding reservations against MAX_OPEN_POSITIONS. On confirmed
failure the reservation is released; on UNKNOWN it is KEPT; reconcile() resolves
reservations against real exchange positions (promote/ release) and gives restart
recovery.

RED before the module exists; GREEN after. The naive-path test documents the bug.
"""
import os
import sqlite3
import tempfile
import shutil
import sys
import subprocess
import threading
import time
import unittest


MAX = 2


def _fresh_db():
    d = tempfile.mkdtemp()
    return d, os.path.join(d, "risk_ledger.db")


# --------------------------------------------------------------------------
# Vulnerability proof: the naive check-then-act (no atomic reserve) can exceed
# the cap under concurrency. This asserts the BUG exists (documents root cause).
# --------------------------------------------------------------------------
class TestNaiveCheckThenActIsUnsafe(unittest.TestCase):
    def test_naive_path_can_exceed_cap(self):
        d, db = _fresh_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE positions (symbol TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()

            # Deterministic interleaving (no sleep): every thread finishes its CHECK before any thread does its ACT.
            # The old version widened the TOCTOU window with time.sleep(0.02), a race against the scheduler: under
            # CPU contention a late thread ran its check after the others had already inserted, saw the cap reached
            # and the test failed (25/25 on a loaded host). The second barrier makes the unsafe window exact.
            start = threading.Barrier(4)
            all_checked = threading.Barrier(4)

            def naive_open(symbol):
                c = sqlite3.connect(db, timeout=5)
                start.wait(timeout=30)
                used = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
                admitted = used < MAX                # check
                all_checked.wait(timeout=30)         # <- TOCTOU window: all checks done, no act yet
                if admitted:
                    try:
                        c.execute("INSERT INTO positions (symbol) VALUES (?)", (symbol,))  # act
                        c.commit()
                    except sqlite3.IntegrityError:
                        pass
                c.close()

            threads = [threading.Thread(target=naive_open, args=(f"S{i}",)) for i in range(4)]
            for t in threads: t.start()
            for t in threads: t.join()

            c = sqlite3.connect(db)
            total = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            c.close()
            # The unguarded path admits MORE than MAX -> the bug we must fix.
            self.assertGreater(total, MAX,
                               "naive check-then-act unexpectedly held the cap")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# PositionReserver behavioural spec (RED until implemented).
# --------------------------------------------------------------------------
class TestReserverBasics(unittest.TestCase):
    def setUp(self):
        from position_reservation import PositionReserver
        self.d, self.db = _fresh_db()
        self.r = PositionReserver(self.db, MAX)
        self.r.init_schema()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_reserve_up_to_max_then_reject(self):
        a = self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        b = self.r.reserve("BBBUSDT", exchange_open_symbols=[])
        c = self.r.reserve("CCCUSDT", exchange_open_symbols=[])
        self.assertTrue(a.ok)
        self.assertTrue(b.ok)
        self.assertFalse(c.ok, "cap breached: 3rd reservation granted")
        self.assertEqual(self.r.used_slots(exchange_open_symbols=[]), MAX)

    def test_release_frees_a_slot(self):
        a = self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        self.r.reserve("BBBUSDT", exchange_open_symbols=[])
        self.assertFalse(self.r.reserve("CCCUSDT", exchange_open_symbols=[]).ok)
        self.r.release(a.reservation_id, reason="failed")
        self.assertTrue(self.r.reserve("CCCUSDT", exchange_open_symbols=[]).ok)

    def test_counts_existing_exchange_positions(self):
        # One real exchange position already open -> only 1 slot left.
        ex = ["SOLUSDT"]
        self.assertTrue(self.r.reserve("AAAUSDT", exchange_open_symbols=ex).ok)
        self.assertFalse(self.r.reserve("BBBUSDT", exchange_open_symbols=ex).ok)

    def test_no_duplicate_symbol(self):
        self.assertTrue(self.r.reserve("AAAUSDT", exchange_open_symbols=[]).ok)
        dup = self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        self.assertFalse(dup.ok, "same symbol reserved twice")
        # already-open symbol cannot be reserved either
        self.assertFalse(self.r.reserve("SOLUSDT", exchange_open_symbols=["SOLUSDT"]).ok)


class TestUnknownAndReconcile(unittest.TestCase):
    def setUp(self):
        from position_reservation import PositionReserver
        self.d, self.db = _fresh_db()
        self.r = PositionReserver(self.db, MAX)
        self.r.init_schema()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_unknown_keeps_reservation(self):
        res = self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        # UNKNOWN outcome: caller does NOT release. Slot stays counted.
        self.assertEqual(self.r.used_slots(exchange_open_symbols=[]), 1)
        # A second slot is still available but the reserved one is held.
        self.assertTrue(res.ok)

    def test_reconcile_promotes_when_position_appears(self):
        self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        # The position actually opened (UNKNOWN resolved to created).
        rep = self.r.reconcile(exchange_open_symbols=["AAAUSDT"], ttl_seconds=0)
        # reservation deleted (promoted to real position), not double counted.
        self.assertEqual(self.r.used_slots(exchange_open_symbols=["AAAUSDT"]), 1)
        self.assertIn("AAAUSDT", [x["symbol"] for x in rep.promoted])

    def test_reconcile_releases_stale_without_position(self):
        self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        # TTL passed and the symbol never appeared on the exchange -> release.
        rep = self.r.reconcile(exchange_open_symbols=[], ttl_seconds=0)
        self.assertEqual(self.r.used_slots(exchange_open_symbols=[]), 0)
        self.assertIn("AAAUSDT", [x["symbol"] for x in rep.released])

    def test_reconcile_keeps_fresh_pending(self):
        self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        # Fresh reservation (within TTL) and no position yet -> keep (in-flight).
        self.r.reconcile(exchange_open_symbols=[], ttl_seconds=3600)
        self.assertEqual(self.r.used_slots(exchange_open_symbols=[]), 1)

    def test_restart_recovery(self):
        from position_reservation import PositionReserver
        self.r.reserve("AAAUSDT", exchange_open_symbols=[])
        # Simulate restart: brand new instance on the same db file.
        r2 = PositionReserver(self.db, MAX)
        r2.init_schema()
        self.assertEqual(r2.used_slots(exchange_open_symbols=[]), 1)  # persisted
        # The position did open; reconcile promotes it.
        r2.reconcile(exchange_open_symbols=["AAAUSDT"], ttl_seconds=0)
        self.assertEqual(r2.used_slots(exchange_open_symbols=["AAAUSDT"]), 1)


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        from position_reservation import PositionReserver
        self.d, self.db = _fresh_db()
        PositionReserver(self.db, MAX).init_schema()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_threads_never_exceed_cap(self):
        from position_reservation import PositionReserver
        results = []
        barrier = threading.Barrier(10)

        def worker(i):
            r = PositionReserver(self.db, MAX)
            barrier.wait()
            res = r.reserve(f"S{i}USDT", exchange_open_symbols=[])
            results.append(res.ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(1 for ok in results if ok), MAX,
                         f"granted {sum(results)} reservations, cap is {MAX}")

    def test_two_processes_never_exceed_cap(self):
        # 10 parallel candidates split across TWO processes contending on one db.
        worker = os.path.join(self.d, "worker.py")
        with open(worker, "w") as f:
            f.write(
                "import sys, json\n"
                "sys.path.insert(0, %r)\n" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))) +
                "from position_reservation import PositionReserver\n"
                "db, maxp, tag, n = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])\n"
                "r = PositionReserver(db, maxp)\n"
                "ok = 0\n"
                "for i in range(n):\n"
                "    res = r.reserve(f'{tag}{i}USDT', exchange_open_symbols=[])\n"
                "    if res.ok: ok += 1\n"
                "print(json.dumps({'ok': ok}))\n"
            )
        p1 = subprocess.Popen([sys.executable, worker, self.db, str(MAX), "A", "5"],
                              stdout=subprocess.PIPE)
        p2 = subprocess.Popen([sys.executable, worker, self.db, str(MAX), "B", "5"],
                              stdout=subprocess.PIPE)
        o1, _ = p1.communicate(timeout=30)
        o2, _ = p2.communicate(timeout=30)
        import json
        granted = json.loads(o1)["ok"] + json.loads(o2)["ok"]
        self.assertEqual(granted, MAX,
                         f"two processes granted {granted} reservations, cap is {MAX}")


class TestWiringHelpers(unittest.TestCase):
    """Seam between open_coin and the reserver (fault/UNKNOWN handling)."""

    def test_should_keep_on_success(self):
        import auto_trader as at
        self.assertTrue(at._reservation_should_keep({"executor_id": "X1"}))
        self.assertTrue(at._reservation_should_keep(
            {"executor_id": "X1", "resolved_after": "HTTP 503"}))

    def test_should_keep_on_unknown(self):
        import auto_trader as at
        # UNKNOWN: a position may have been created -> keep the slot.
        self.assertTrue(at._reservation_should_keep(
            {"error": "UNKNOWN create outcome (HTTP 503); resolve failed: x",
             "outcome": "UNKNOWN"}))

    def test_release_on_confirmed_failure(self):
        import auto_trader as at
        # NOT_CREATED / pre-create refusal / plain rejection -> release the slot.
        self.assertFalse(at._reservation_should_keep(
            {"error": "UNKNOWN create outcome (HTTP 500); new executors=[]",
             "outcome": "NOT_CREATED"}))
        self.assertFalse(at._reservation_should_keep(
            {"error": "pre-create executor snapshot failed, refusing: boom"}))
        self.assertFalse(at._reservation_should_keep({"code": -1, "msg": "rejected"}))

    def test_exchange_open_symbols(self):
        import auto_trader as at

        class _B:
            def __init__(self, resp): self._resp = resp
            def position_risk(self): return self._resp

        ok = _B([{"symbol": "SOLUSDT", "positionAmt": "-0.3"},
                 {"symbol": "BTCUSDT", "positionAmt": "0"},
                 {"symbol": "ETHUSDT", "positionAmt": "0.5"}])
        self.assertEqual(at._exchange_open_symbols(ok), {"SOLUSDT", "ETHUSDT"})
        # API failure -> None (caller must fail-closed)
        self.assertIsNone(at._exchange_open_symbols(_B({"code": -1})))


if __name__ == "__main__":
    unittest.main()
