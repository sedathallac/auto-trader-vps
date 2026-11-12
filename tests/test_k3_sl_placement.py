"""K-3 — cross-process SL-placement idempotency + deterministic dedupe backstop.

Root cause (live): run_once and orphan_killer both call ensure_sl_coverage, and a
freshly opened position can transiently look unprotected to both -> both place an SL
-> duplicate. Fix: (A) a cross-process placement intent (SlPlacementStore, same
risk_ledger.db, BEGIN IMMEDIATE) so only the owner places; (B) a deterministic dedupe
backstop that keeps the MAX algoId and cancels the rest (two processes can never
cancel both). Includes a REAL two-process race test.
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import _dedupe_algo_sl, ensure_sl_coverage
from sl_placement import SlPlacementStore, ACQUIRED, BUSY

SYM = "SOLUSDT"


# --------------------------- SlPlacementStore --------------------------------
class TestSlPlacementStore(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "risk_ledger.db")
        self.s = SlPlacementStore(self.db)
        self.s.init_schema()

    def test_acquire_then_busy(self):
        self.assertEqual(self.s.acquire(SYM, "LONG:81.27"), ACQUIRED)
        self.assertEqual(self.s.acquire(SYM, "LONG:81.27"), BUSY)

    def test_placed_still_busy(self):
        self.s.acquire(SYM, "LONG:81.27")
        self.s.mark_placed(SYM, "LONG:81.27")
        self.assertEqual(self.s.acquire(SYM, "LONG:81.27"), BUSY)

    def test_new_epoch_supersedes(self):
        self.s.acquire(SYM, "LONG:81.27")
        self.s.mark_placed(SYM, "LONG:81.27")
        self.assertEqual(self.s.acquire(SYM, "SHORT:90.00"), ACQUIRED)   # reopened

    def test_release_allows_reacquire(self):
        self.s.acquire(SYM, "LONG:81.27")
        self.s.release(SYM, "LONG:81.27")
        self.assertEqual(self.s.acquire(SYM, "LONG:81.27"), ACQUIRED)

    def test_threads_single_owner(self):
        oks = []
        lock = threading.Lock()

        def w():
            r = self.s.acquire(SYM, "LONG:81.27")
            with lock:
                oks.append(r == ACQUIRED)
        ts = [threading.Thread(target=w) for _ in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sum(1 for o in oks if o), 1)   # exactly one placer


def _xproc_worker(db, out, idx):
    from sl_placement import SlPlacementStore as _S, ACQUIRED as _A
    r = _S(db).acquire(SYM, "LONG:81.27")
    with open(out, "a") as fh:
        fh.write(f"{idx}:{r}:{r==_A}\n")


class TestCrossProcessPlacement(unittest.TestCase):
    def test_two_processes_single_owner(self):
        import multiprocessing as mp
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "risk_ledger.db")
        out = os.path.join(tmp, "out.txt")
        SlPlacementStore(db).init_schema()
        ctx = mp.get_context("spawn")
        ps = [ctx.Process(target=_xproc_worker, args=(db, out, i)) for i in range(2)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        lines = open(out).read().strip().splitlines()
        owners = sum(1 for ln in lines if ln.endswith(":True"))
        self.assertEqual(owners, 1, f"more than one process acquired placement: {lines}")


# --------------------------- deterministic dedupe ----------------------------
def _algo(aid, qty="0.3", typ="STOP_MARKET", algotype="CONDITIONAL"):
    return {"algoId": aid, "orderType": typ, "algoType": algotype, "symbol": SYM,
            "side": "SELL", "reduceOnly": True, "quantity": qty, "algoStatus": "NEW"}


class _DedupeBinance:
    def __init__(self, algos):
        self._algos = list(algos)
        self.cancelled = []
        self._lock = threading.Lock()

    def open_algo_orders_for(self, symbol):
        with self._lock:
            return list(self._algos)

    def cancel_algo_order(self, aid):
        with self._lock:
            self.cancelled.append(aid)
            self._algos = [o for o in self._algos if o["algoId"] != aid]
        return {"algoId": aid, "code": "200"}


class TestDedupe(unittest.TestCase):
    def test_keeps_max_algoid_cancels_rest(self):
        fb = _DedupeBinance([_algo(100), _algo(200), _algo(150)])
        c = _dedupe_algo_sl(fb, SYM)
        self.assertEqual(sorted(c), [100, 150])        # cancelled the non-max
        self.assertEqual(fb.cancelled and max(o["algoId"] for o in fb._algos), 200)

    def test_single_sl_noop(self):
        fb = _DedupeBinance([_algo(100)])
        self.assertEqual(_dedupe_algo_sl(fb, SYM), [])
        self.assertEqual(fb.cancelled, [])

    def test_concurrent_dedupe_never_cancels_all(self):
        # two threads dedupe the same double -> the MAX (200) always survives.
        fb = _DedupeBinance([_algo(100), _algo(200)])

        def w():
            _dedupe_algo_sl(fb, SYM)
        ts = [threading.Thread(target=w) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        remaining = [o["algoId"] for o in fb._algos]
        self.assertIn(200, remaining)                  # max survives -> never unprotected
        self.assertNotIn(100, remaining)


# --------------------------- ensure_sl_coverage integration ------------------
def _pos(amt):
    return {"symbol": SYM, "positionAmt": str(amt), "entryPrice": "81.27"}


class _CovBinance:
    """Open SOL LONG with a DOUBLE SL -> ensure_sl_coverage must dedupe to one."""
    def __init__(self):
        self._algos = [_algo(100), _algo(200)]
        self.cancelled = []
        self.placed = []

    def position_risk(self):
        return [_pos("0.3")]

    def position_mode(self):
        return {"dualSidePosition": False}

    def open_algo_orders_for(self, s):
        return list(self._algos)

    def open_algo_orders_raw(self, s):
        return list(self._algos)

    def cancel_algo_order(self, aid):
        self.cancelled.append(aid)
        self._algos = [o for o in self._algos if o["algoId"] != aid]
        return {"algoId": aid, "code": "200"}

    def filters(self, s):
        return {"PRICE_FILTER": {"tickSize": "0.01"}}

    def place_algo_sl(self, *a, **k):
        self.placed.append(a)
        return {"algoId": 999}


class TestEnsureSlCoverageDedupe(unittest.TestCase):
    def setUp(self):
        auto_trader._OPEN_UNPROTECTED.clear()

    def tearDown(self):
        auto_trader._OPEN_UNPROTECTED.clear()

    def test_double_sl_is_deduped(self):
        fb = _CovBinance()
        ensure_sl_coverage(fb)
        self.assertEqual(fb.cancelled, [100])          # non-max cancelled, 200 kept
        self.assertEqual(fb.placed, [])                # still PRESENT -> no new SL


if __name__ == "__main__":
    unittest.main()
