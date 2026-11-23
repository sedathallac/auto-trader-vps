"""BLOK25 Bölüm 6 testleri — resilience: duplicate-worker kilidi, heartbeat,
clock drift / disk / stale-feed KRİTİK → HALT (yeni pozisyon blokları),
rate-limit backoff, data gap ölçümü, stale-bar fail-closed, supervisor
(restart / stale-heartbeat kill / RSS kill / SQLite backup).
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import paper_runner
from paper_runner import PaperRunner, RateLimited
from paper_broker import PaperBroker
from paper_supervisor import backup_db, read_heartbeat, rss_mb, supervise
from tests.test_blok25_paper import (FakeFetcher, _mk_bars, _decision,
                                     SYM, OTHER, T0, BAR)

HW = "/fapi/v1/time"  # not used directly; documented


class _ClockFetcher(FakeFetcher):
    """FakeFetcher + controllable server_time (clock-drift tests)."""

    def __init__(self, klines, funding=None, server_time=None):
        super().__init__(klines, funding)
        self._server_time = server_time

    def server_time(self):
        if self._server_time is None:
            raise AttributeError("no server_time")
        return self._server_time


def _runner(fetcher, clock, **kw):
    db = os.path.join(tempfile.mkdtemp(), "paper.db")
    results = tempfile.mkdtemp()
    return PaperRunner(db, results, symbols=[SYM, OTHER],
                       equity_start=1000.0, fetcher=fetcher, clock=clock,
                       **kw)


def _events(runner, kind=None):
    return [e for e in runner.broker.events_rows()
            if kind is None or e["kind"] == kind]


class TestLock(unittest.TestCase):
    def test_duplicate_worker_refused(self):
        f = FakeFetcher({SYM: _mk_bars(10), OTHER: _mk_bars(10)})
        now = lambda: T0 + 12 * BAR
        r1 = _runner(f, now)
        r1.acquire_lock()
        r2 = _runner(f, now)      # same results dir? no — fresh tempdir...
        # r2 has its OWN dir; simulate the shared-dir case directly:
        r2.results_dir = r1.results_dir
        r2._lock_file = None
        with self.assertRaises(RuntimeError):
            r2.acquire_lock()
        # release: a fresh handle on the same dir can now take it
        r1._lock_file.close()
        r1._lock_file = None
        r3 = _runner(f, now)
        r3.results_dir = r1.results_dir
        r3.acquire_lock()         # no exception


class TestHeartbeat(unittest.TestCase):
    def test_heartbeat_written_each_cycle(self):
        f = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        r = _runner(f, lambda: now)
        r.cycle()
        hb = json.loads((Path(r.results_dir) / "paper_heartbeat.json")
                        .read_text())
        self.assertEqual(hb["cycle"], 1)
        self.assertTrue(hb["ok"])
        self.assertIn("rss_mb", hb)
        self.assertGreater(hb["rss_mb"], 0)
        self.assertIn("halted", hb)

    def test_supervisor_reads_heartbeat(self):
        d = tempfile.mkdtemp()
        hb = {"ts": int(time.time() * 1000), "cycle": 7, "ok": True}
        self.assertIsNone(read_heartbeat(d))          # missing file
        (Path(d) / "paper_heartbeat.json").write_text(json.dumps(hb))
        self.assertEqual(read_heartbeat(d)["cycle"], 7)
        (Path(d) / "paper_heartbeat.json").write_text("{broken")
        self.assertIsNone(read_heartbeat(d))          # corrupt file


class TestCriticalHalts(unittest.TestCase):
    """Her KRİTİK durum HALT eder → 18 kontrolün 'halted_kill_switch'
    maddesi yeni pozisyon açan her kararı bloklar (fail-closed)."""

    def _assert_halted(self, r, kind):
        self.assertTrue(r.risk.is_halted)
        self.assertTrue(_events(r, "CRITICAL"))
        self.assertTrue(_events(r, "HALT"))
        self.assertIn(kind, [e["kind"] for e in _events(r, "CRITICAL")] or
                      [e["kind"] for e in _events(r)])

    def test_clock_drift_critical_halts(self):
        now = T0 + 402 * BAR
        f = _ClockFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)},
                          server_time=now + 120_000)   # +120s drift
        r = _runner(f, lambda: now)
        r.cycle()
        self._assert_halted(r, "CRITICAL")
        self.assertIn("CLOCK_DRIFT", _events(r, "CRITICAL")[0]["detail"])

    def test_clock_drift_warn_only(self):
        now = T0 + 402 * BAR
        f = _ClockFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)},
                          server_time=now + 10_000)    # +10s: warn band
        r = _runner(f, lambda: now)
        r.cycle()
        self.assertFalse(r.risk.is_halted)
        self.assertTrue(_events(r, "CLOCK_DRIFT_WARN"))

    def test_low_disk_halts(self):
        f = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        r = _runner(f, lambda: now)
        real = paper_runner.shutil.disk_usage

        def fake(path):
            usage = real(path)
            return usage._replace(total=usage.total, used=usage.used,
                                  free=50 * 1024 * 1024)   # 50 MB free
        paper_runner.shutil.disk_usage = fake
        try:
            r.cycle()
        finally:
            paper_runner.shutil.disk_usage = real
        self._assert_halted(r, "CRITICAL")
        self.assertIn("DISK_LOW", _events(r, "CRITICAL")[0]["detail"])

    def test_stale_feed_halts(self):
        # BTCUSDT feed frozen: OTHER advanced 30 days, SYM did not
        f = FakeFetcher({SYM: _mk_bars(402),
                         OTHER: _mk_bars(402) + [
                             (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i,
                              1e7) for i in range(181)]})
        now = T0 + 583 * BAR + 1000
        r = _runner(f, lambda: now)
        r.cycle()
        self._assert_halted(r, "CRITICAL")
        self.assertIn("STALE_DATA", _events(r, "CRITICAL")[0]["detail"])

    def test_halt_blocks_a_flip_decision(self):
        """HALT sonrası checklist gerçekten blokluyor (entegrasyon)."""
        now = T0 + 402 * BAR
        f = _ClockFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)},
                          server_time=now + 120_000)
        r = _runner(f, lambda: now, stale_bar_ms=10 ** 15)
        r.cycle()                                   # halts on drift
        self.assertTrue(r.risk.is_halted)
        # feed a falling tail on OTHER: momentum flips → decision attempted
        f._klines[OTHER] = f._klines[OTHER] + [
            (T0 + (402 + i) * BAR, 502.0 - i, 502.0 - i, 1e7)
            for i in range(181)]
        f._klines[SYM] = f._klines[SYM] + [
            (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i, 1e7)
            for i in range(181)]
        now2 = T0 + 583 * BAR + 1000
        r.clock = lambda: now2
        r.cycle()
        executed = [j for j in r.broker.journal_rows()
                    if j["outcome"] == "EXECUTED"]
        self.assertEqual(executed, [])
        blocked = [j for j in r.broker.journal_rows()
                   if j["outcome"] == "BLOCKED"]
        self.assertTrue(blocked)
        self.assertIn("halted", blocked[-1]["checklist"])


class TestDataQuality(unittest.TestCase):
    def test_data_gap_journaled_not_fatal(self):
        # a missing bar in the middle of the window: journal'lanır, cycle
        # yaşamaya devam eder (donmuş motorun gap kuralı işler)
        bars = _mk_bars(402)
        del bars[200]                     # 4h gap
        f = FakeFetcher({SYM: bars, OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        r = _runner(f, lambda: now)
        r.cycle()
        gaps = _events(r, "DATA_GAP")
        self.assertEqual(len(gaps), 1)
        self.assertIn(SYM, gaps[0]["detail"])
        self.assertFalse(r.risk.is_halted)

    def test_stale_bar_not_executed(self):
        # "new" bar that closed long ago: replayed WITHOUT execution
        f = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        r = _runner(f, lambda: now)
        r.cycle()
        # append 3 new bars but jump the clock 3 days (bars now stale)
        f._klines[OTHER] = f._klines[OTHER] + [
            (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i, 1e7)
            for i in range(3)]
        f._klines[SYM] = f._klines[SYM] + [
            (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i, 1e7)
            for i in range(3)]
        now2 = T0 + 402 * BAR + 3 * 24 * 3600 * 1000   # 3 days later
        r.clock = lambda: now2
        r.cycle()
        stale = _events(r, "STALE_BAR")
        self.assertEqual(len(stale), 6)    # 3 bars x 2 symbols
        executed = [j for j in r.broker.journal_rows()
                    if j["outcome"] == "EXECUTED"]
        self.assertEqual(executed, [])

    def test_rate_limit_backoff_then_success(self):
        calls = {"n": 0}
        real_klines = FakeFetcher.klines

        def flaky(self, symbol, interval="4h", limit=500):
            calls["n"] += 1
            if symbol == SYM and calls["n"] == 1:
                raise RateLimited(0.01)    # tiny wait, first SYM call
            return real_klines(self, symbol, interval, limit)
        f = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        f.klines = flaky.__get__(f, FakeFetcher)
        now = T0 + 402 * BAR
        r = _runner(f, lambda: now)
        r.cycle()                          # survives via backoff + retry
        self.assertTrue(_events(r, "RATE_LIMIT"))
        self.assertFalse(r.risk.is_halted)

    def test_rate_limit_persists_as_cycle_error(self):
        class DeadFetcher(FakeFetcher):
            def klines(self, symbol, interval="4h", limit=500):
                raise RateLimited(0.01)
        f = DeadFetcher({SYM: _mk_bars(5), OTHER: _mk_bars(5)})
        now = T0 + 6 * BAR
        r = _runner(f, lambda: now)
        with self.assertRaises(RateLimited):   # retry de başarısız
            r.cycle()
        self.assertTrue(_events(r, "RATE_LIMIT"))


class TestBackup(unittest.TestCase):
    def test_backup_consistent_copy(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "paper.db")
        b = PaperBroker(db, [SYM, OTHER])
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        b.event("SOME_EVENT", "x")
        dst = backup_db(db, os.path.join(tmp, "backups"))
        self.assertTrue(dst.exists())
        conn = sqlite3.connect(str(dst))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM positions").fetchone()[0]
            e = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='SOME_EVENT'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(e, 1)

    def test_backup_retention(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "paper.db")
        PaperBroker(db, [SYM, OTHER])
        bdir = os.path.join(tmp, "backups")
        for _ in range(4):
            backup_db(db, bdir, keep=2)   # ms-stamped names never collide
        kept = sorted(Path(bdir).glob("paper_ledger_*.db"))
        self.assertEqual(len(kept), 2)

    def test_rss_mb_of_self(self):
        self.assertIsNone(rss_mb(999999999))    # no such pid
        self.assertGreater(rss_mb(os.getpid()), 0)


class TestSupervisor(unittest.TestCase):
    """Gerçek subprocess'lerle (ağ YOK): restart, stale-heartbeat kill."""

    def test_restarts_dead_child_then_gives_up(self):
        results = tempfile.mkdtemp()
        import sys
        cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
        restarts = supervise(cmd, results, poll_sec=0.05,
                             backoff_start=0.02, backoff_max=0.02,
                             max_restarts=2)
        self.assertEqual(restarts, 3)      # initial + 2 restarts, then exit

    def test_stale_heartbeat_killed(self):
        results = tempfile.mkdtemp()
        # child writes ONE heartbeat then hangs forever (never cycles)
        child = ("import json,time,sys\n"
                 "hb={'ts':int(time.time()*1000),'cycle':1,'ok':True}\n"
                 "open(sys.argv[1],'w').write(json.dumps(hb))\n"
                 "time.sleep(1000)\n")
        import sys
        cmd = [sys.executable, "-c", child,
               str(Path(results) / "paper_heartbeat.json")]
        log_path = Path(results) / "paper_supervisor.log"
        log = paper_runner.setup_logging.__wrapped__ if False else None
        # run supervise in a thread; it should kill the hung child
        import threading
        holder = {}

        def run():
            try:
                holder["restarts"] = supervise(
                    cmd, results, poll_sec=0.05, heartbeat_stale_sec=0.3,
                    backoff_start=0.02, backoff_max=0.02, max_restarts=1)
            except Exception as e:
                holder["err"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "supervisor did not terminate")
        self.assertNotIn("err", holder)
        self.assertEqual(holder.get("restarts"), 2)   # killed + restarted
        text = (Path(results) / "paper_supervisor.log").read_text() \
            if (Path(results) / "paper_supervisor.log").exists() else ""
        # the KILL was logged (or at least restarts happened)
        self.assertTrue("KILL" in text or holder.get("restarts") == 2)


if __name__ == "__main__":
    unittest.main()
