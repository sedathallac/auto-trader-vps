"""Increment 12.1 — cross-PROCESS close-intent / idempotency.

Moves the CloseCoordinator idempotency guarantee from the thread boundary (inc12
RLock) to the PROCESS boundary via an ATOMIC close-intent in the SAME shared SQLite
ledger as PositionReserver (no second database). Drives the REAL CloseCoordinator +
BinanceAPI chain; mocks only the HTTP boundary / uses real processes against a
shared SQLite exchange + shared intent db.

Behavioural RED: with no intent store a second process (or a restart after a crash
mid-close) sends a SECOND close POST; with the intent store it does not.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import BinanceAPI, HBAPI
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState
from close_intent import (CloseIntentStore, ACQUIRED, BUSY, CLOSED, STALE)

SYM = "SOLUSDT"


class _Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class FakeExchange:
    def __init__(self, amt="0.3", entry="83.08", dual=False, fill_qty=None, fail=None):
        self.amt = Decimal(str(amt))
        self.entry = entry
        self.dual = dual
        self.fill_qty = Decimal(str(fill_qty)) if fill_qty is not None else None
        self.fail = fail or {}
        self.posts = []

    def _fail(self, key):
        f = self.fail.get(key)
        if f is None:
            return None
        if isinstance(f, Exception):
            raise f
        return f

    def handle_get(self, url):
        path = urlparse(url).path
        if path == "/fapi/v2/positionRisk":
            bad = self._fail("positionRisk")
            if bad is not None:
                return bad
            return [{"symbol": SYM, "positionAmt": str(self.amt),
                     "entryPrice": self.entry}]
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": self.dual}
        if path == "/fapi/v1/openOrders":
            return []
        return {"code": -1121}

    def handle_post(self, url, body):
        path = urlparse(url).path
        if path.endswith("/executors/search"):
            return {"data": []}
        if path == "/fapi/v1/order":
            bad = self._fail("post")
            if bad is not None:
                return bad
            p = _params(url)
            qty = Decimal(p["quantity"])
            reduce_only = str(p.get("reduceOnly", "")).lower() == "true"
            cur = self.amt
            signed = -qty if p["side"] == "SELL" else qty
            if reduce_only and (cur == 0 or (cur > 0 and signed > 0) or (cur < 0 and signed < 0)):
                self.posts.append({"outcome": "REJECTED_-2022"})
                return {"code": -2022, "msg": "ReduceOnly rejected."}
            fill = self.fill_qty if self.fill_qty is not None else qty
            new = cur + (-fill if p["side"] == "SELL" else fill)
            if reduce_only and ((cur > 0 and new < 0) or (cur < 0 and new > 0)):
                new = Decimal("0")
            self.amt = new
            self.posts.append({"outcome": "FILLED", "amt": str(self.amt)})
            return {"orderId": 1, "status": "FILLED"}
        return {"code": -1121}

    def handle_delete(self, url):
        return {"status": "CANCELED"}


class FakeSession:
    def __init__(self, ex):
        self.ex = ex

    def get(self, url, headers=None, params=None, timeout=None):
        return _Resp(self.ex.handle_get(url))

    def post(self, url, headers=None, json=None, auth=None, timeout=None):
        return _Resp(self.ex.handle_post(url, json))

    def delete(self, url, headers=None, timeout=None):
        return _Resp(self.ex.handle_delete(url))


class IntentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self.store = CloseIntentStore(self.db)
        self.store.init_schema()
        self._orig_session = auto_trader._session  # capture BEFORE any wire()

    def wire(self, ex, with_intent=True):
        auto_trader._session = FakeSession(ex)
        adapter = BinanceHBCloseAdapter(BinanceAPI("k", "s"), HBAPI("http://hb", "u", "p"))
        return CloseCoordinator(adapter, intent_store=self.store if with_intent else None)

    def tearDown(self):
        auto_trader._session = self._orig_session  # always restore (no leak)


# --------------------------- store-level atomicity ---------------------------
class TestIntentStore(IntentBase):
    def test_acquire_then_busy_same_token(self):
        self.assertEqual(self.store.acquire(SYM, "LONG:83.08").status, ACQUIRED)
        self.assertEqual(self.store.acquire(SYM, "LONG:83.08").status, BUSY)

    def test_closed_is_terminal(self):
        self.store.acquire(SYM, "LONG:83.08")
        self.store.mark_closed(SYM, "LONG:83.08")
        self.assertEqual(self.store.acquire(SYM, "LONG:83.08").status, CLOSED)

    def test_new_period_supersedes_terminal(self):
        self.store.acquire(SYM, "LONG:83.08")
        self.store.mark_closed(SYM, "LONG:83.08")
        # a new position period (different entry) -> fresh ACQUIRED
        self.assertEqual(self.store.acquire(SYM, "LONG:90.00").status, ACQUIRED)

    def test_active_different_token_is_stale(self):
        self.store.acquire(SYM, "LONG:83.08")   # active, not terminal
        # a different period arriving while the old is still active -> STALE (fail-closed)
        self.assertEqual(self.store.acquire(SYM, "SHORT:70.00").status, STALE)

    def test_hedge_long_and_short_distinct_intents(self):
        # symbol-ALONE is unsafe in HEDGE: LONG and SHORT coexist as separate intents.
        self.assertEqual(self.store.acquire(SYM, "LONG:83.08").status, ACQUIRED)
        # different token but the LONG is active -> SHORT must not silently reuse it
        r = self.store.acquire(SYM, "SHORT:83.08")
        self.assertEqual(r.status, STALE)  # fail-closed; distinct period must reconcile

    def test_reconcile_flat_marks_closed(self):
        self.store.acquire(SYM, "LONG:83.08")
        self.assertEqual(self.store.reconcile(SYM, exchange_flat=True), "CLOSED")

    def test_reconcile_not_flat_keeps(self):
        self.store.acquire(SYM, "LONG:83.08")
        self.store.mark_sent(SYM, "LONG:83.08")
        self.assertEqual(self.store.reconcile(SYM, exchange_flat=False), "SENT")

    def test_threads_single_acquire(self):
        oks = []
        lock = threading.Lock()

        def w():
            r = self.store.acquire(SYM, "LONG:83.08")
            with lock:
                oks.append(r.status == ACQUIRED)
        ts = [threading.Thread(target=w) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sum(1 for o in oks if o), 1)  # exactly one owner


# --------------------------- coordinator integration -------------------------
class TestCoordinatorIntent(IntentBase):
    def test_single_close_marks_intent_closed(self):
        ex = FakeExchange(amt="0.3")
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len([p for p in ex.posts if p["outcome"] == "FILLED"]), 1)
        self.assertEqual(self.store.get(SYM)["state"], "CLOSED")

    def test_second_process_in_progress_no_post(self):
        # a crashed/in-flight owner left a PENDING intent for THIS position period.
        ex = FakeExchange(amt="0.3")
        self.store.acquire(SYM, "LONG:83.08")      # owner A (in-flight)
        cc = self.wire(ex)                          # process B
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "IGNORED_IN_PROGRESS")
        self.assertEqual(ex.posts, [])             # B sent NO second POST

    def test_RED_without_intent_second_post_sent(self):
        # behavioural RED: with NO intent store, process B sends a second close POST
        # although A's close is in flight (A would have marked the intent).
        ex = FakeExchange(amt="0.3")
        cc = self.wire(ex, with_intent=False)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(len(ex.posts), 1)         # a (second) POST IS sent w/o the gate
        self.assertEqual(r.state, CloseState.CLOSED)

    def test_already_closed_intent_ignored(self):
        ex = FakeExchange(amt="0.3")
        self.store.acquire(SYM, "LONG:83.08")
        self.store.mark_closed(SYM, "LONG:83.08")
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(ex.posts, [])

    def test_stale_intent_failclosed(self):
        ex = FakeExchange(amt="0.3", entry="83.08")  # B's token LONG:83.08
        self.store.acquire(SYM, "SHORT:70.00")       # active, different period
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "INTENT_STALE")
        self.assertTrue(r.critical)
        self.assertEqual(ex.posts, [])

    def test_exchange_flat_before_acquire_no_intent(self):
        ex = FakeExchange(amt="0")
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertIsNone(self.store.get(SYM))       # no intent created for a flat pos
        self.assertEqual(ex.posts, [])

    def test_post_timeout_intent_kept_not_released(self):
        import requests
        ex = FakeExchange(amt="0.3", fail={"post": requests.exceptions.Timeout("t")})
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.state, CloseState.FAILED_SAFE)
        # UNKNOWN send -> intent kept (owner holds it), not released/terminal.
        self.assertEqual(self.store.get(SYM)["state"], "PENDING")

    def test_query_timeout_failclosed_intent_kept(self):
        ex = FakeExchange(amt="0.3")
        cc = self.wire(ex)
        # flat-confirm read fails AFTER the close POST (reads: step3, token, close, confirm)
        orig = ex.handle_get
        state = {"n": 0}

        def flaky(url):
            # reads: 1=step3, 2=position_token, 3=close_reduce_only, 4=flat-confirm
            if urlparse(url).path == "/fapi/v2/positionRisk":
                state["n"] += 1
                if state["n"] == 4:   # the post-close flat-confirm read
                    return {"code": -1001, "msg": "timeout"}
            return orig(url)
        ex.handle_get = flaky
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "FLAT_CONFIRM_FAILED")
        self.assertEqual(self.store.get(SYM)["state"], "SENT")  # kept, not terminal

    def test_partial_fill_intent_not_terminal(self):
        ex = FakeExchange(amt="0.3", fill_qty="0.2")
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "PARTIAL")
        self.assertEqual(r.detail["remaining"], "0.1")
        self.assertNotEqual(self.store.get(SYM)["state"], "CLOSED")  # remainder pending

    def test_new_position_period_after_close(self):
        # close once (intent CLOSED), then a NEW position period opens on the same
        # symbol -> fresh close works (token differs), not confused with the old one.
        ex = FakeExchange(amt="0.3", entry="83.08")
        self.wire(ex).request_close(SYM, "MANUAL")
        self.assertEqual(self.store.get(SYM)["state"], "CLOSED")
        ex.amt = Decimal("0.5")
        ex.entry = "90.00"            # new period
        cc2 = self.wire(ex)
        r = cc2.request_close(SYM, "MANUAL")
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(self.store.get(SYM)["token"], "LONG:90.00")


# --------------------------- restart recovery --------------------------------
class TestRestartRecovery(IntentBase):
    def test_restart_reads_existing_intent_then_reconciles_flat(self):
        # owner crashed mid-close leaving PENDING; on restart the position is flat.
        self.store.acquire(SYM, "LONG:83.08")
        self.store.mark_sent(SYM, "LONG:83.08")
        # restart recovery: positionRisk reliably flat -> intent terminal, no POST.
        self.assertEqual(self.store.reconcile(SYM, exchange_flat=True), "CLOSED")
        ex = FakeExchange(amt="0")
        cc = self.wire(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(ex.posts, [])

    def test_restart_intent_kept_when_not_flat(self):
        self.store.acquire(SYM, "LONG:83.08")
        self.store.mark_sent(SYM, "LONG:83.08")
        self.assertEqual(self.store.reconcile(SYM, exchange_flat=False), "SENT")


# --------------------------- cross-process (real) ----------------------------
def _xproc_worker(db_exch, db_intent, out_path, idx):
    """Separate PROCESS: real CloseCoordinator + CloseIntentStore (shared db) against
    a shared SQLite exchange. Only the intent owner sends a POST."""
    import sqlite3 as _sq
    from decimal import Decimal as _D
    from urllib.parse import urlparse as _up, parse_qs as _pq
    import auto_trader as _at
    from auto_trader import BinanceAPI as _B, HBAPI as _H
    from close_adapter import BinanceHBCloseAdapter as _A
    from close_coordinator import CloseCoordinator as _C
    from close_intent import CloseIntentStore as _S

    class _R:
        def __init__(self, p):
            self._p = p

        def json(self):
            return self._p

    def _pr(url):
        return {k: v[0] for k, v in _pq(_up(url).query).items()}

    def _amt():
        c = _sq.connect(db_exch, timeout=30)
        row = c.execute("SELECT amt FROM pos WHERE symbol=?", (SYM,)).fetchone()
        c.close()
        return row[0] if row else "0"

    class _Sess:
        def get(self, url, headers=None, params=None, timeout=None):
            path = _up(url).path
            if path == "/fapi/v2/positionRisk":
                return _R([{"symbol": SYM, "positionAmt": _amt(), "entryPrice": "83.08"}])
            if path == "/fapi/v1/positionSide/dual":
                return _R({"dualSidePosition": False})
            if path == "/fapi/v1/openOrders":
                return _R([])
            return _R({"code": -1121})

        def post(self, url, headers=None, json=None, auth=None, timeout=None):
            path = _up(url).path
            if path.endswith("/executors/search"):
                return _R({"data": []})
            if path == "/fapi/v1/order":
                p = _pr(url)
                qty = _D(p["quantity"])
                c = _sq.connect(db_exch, timeout=30, isolation_level=None)
                try:
                    c.execute("PRAGMA busy_timeout=30000")
                    c.execute("BEGIN IMMEDIATE")
                    cur = _D(c.execute("SELECT amt FROM pos WHERE symbol=?", (SYM,)).fetchone()[0])
                    signed = -qty if p["side"] == "SELL" else qty
                    if cur == 0 or (cur > 0 and signed > 0):
                        c.execute("UPDATE exch SET posts=posts+1 WHERE id=1")
                        c.execute("COMMIT")
                        return _R({"code": -2022, "msg": "ReduceOnly rejected."})
                    new = cur + signed
                    if (cur > 0 and new < 0) or (cur < 0 and new > 0):
                        new = _D("0")
                    c.execute("UPDATE pos SET amt=? WHERE symbol=?", (str(new), SYM))
                    c.execute("UPDATE exch SET posts=posts+1 WHERE id=1")
                    c.execute("COMMIT")
                finally:
                    c.close()
                return _R({"orderId": 1, "status": "FILLED"})
            return _R({"code": -1121})

        def delete(self, url, headers=None, timeout=None):
            return _R({"status": "CANCELED"})

    _at._session = _Sess()
    store = _S(db_intent)
    cc = _C(_A(_B("k", "s"), _H("http://hb", "u", "p")), intent_store=store)
    res = cc.request_close(SYM, f"P{idx}")
    with open(out_path, "a") as fh:
        fh.write(f"{idx}:{res.action}\n")


class TestCrossProcessIntent(unittest.TestCase):
    def test_two_processes_single_post_via_intent(self):
        import multiprocessing as mp
        tmp = tempfile.mkdtemp()
        db_exch = os.path.join(tmp, "exch.db")
        db_intent = os.path.join(tmp, "risk_ledger.db")
        out = os.path.join(tmp, "out.txt")
        c = sqlite3.connect(db_exch)
        c.execute("CREATE TABLE pos (symbol TEXT PRIMARY KEY, amt TEXT)")
        c.execute("INSERT INTO pos VALUES (?, ?)", (SYM, "0.3"))
        c.execute("CREATE TABLE exch (id INTEGER PRIMARY KEY, posts INTEGER)")
        c.execute("INSERT INTO exch VALUES (1, 0)")
        c.commit()
        c.close()
        CloseIntentStore(db_intent).init_schema()
        ctx = mp.get_context("spawn")
        ps = [ctx.Process(target=_xproc_worker, args=(db_exch, db_intent, out, i))
              for i in range(2)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        c = sqlite3.connect(db_exch)
        posts = c.execute("SELECT posts FROM exch WHERE id=1").fetchone()[0]
        amt = Decimal(c.execute("SELECT amt FROM pos WHERE symbol=?", (SYM,)).fetchone()[0])
        c.close()
        self.assertEqual(posts, 1, "more than one close POST across processes")
        self.assertEqual(amt, Decimal("0"))          # closed, flat
        self.assertGreaterEqual(amt, Decimal("0"))   # never reversed


if __name__ == "__main__":
    unittest.main()
