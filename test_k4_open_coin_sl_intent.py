"""K-4 RED/GREEN — open_coin must share ensure_sl_coverage's SL-placement intent.

Runs against the REAL sl_placement.SlPlacementStore (K-3). Proves that routing
open_coin's SL placement through the SAME (symbol, '<side>:<entryPrice>') intent
makes the open_coin <-> ensure_sl_coverage race place exactly ONE SL, without
waiting for the <=60s deterministic dedupe backstop.
"""
import threading
import time
import multiprocessing as mp

from sl_placement import SlPlacementStore, ACQUIRED

SYMBOL = "BTCUSDT"
ENTRY = "50000.0"
TOKEN = "LONG:" + ENTRY  # identical formula both code paths use


class FakeExchange:
    def __init__(self):
        self._l = threading.Lock()
        self.sl = []

    def place_algo_sl(self, symbol, by):
        time.sleep(0.03)  # widen the window so a broken guard double-places
        with self._l:
            self.sl.append((symbol, by))
        return {"algoId": 1000 + len(self.sl)}

    def count(self, symbol):
        with self._l:
            return sum(1 for (s, _b) in self.sl if s == symbol)


def test_without_intent_double_sl(tmp_path):
    """RED: open_coin places directly (the current gap) while ensure_sl_coverage
    places via the intent -> DUPLICATE SL on one position."""
    ex = FakeExchange()
    store = SlPlacementStore(str(tmp_path / "risk_ledger.db"))
    store.init_schema()
    bar = threading.Barrier(2)

    def open_coin_naive():       # today's open_coin: no acquire()
        bar.wait()
        ex.place_algo_sl(SYMBOL, "open_coin")

    def coverage():              # ensure_sl_coverage: already uses the intent
        bar.wait()
        if store.acquire(SYMBOL, TOKEN) == ACQUIRED:
            ex.place_algo_sl(SYMBOL, "ensure_sl_coverage")
            store.mark_placed(SYMBOL, TOKEN)

    t1 = threading.Thread(target=open_coin_naive)
    t2 = threading.Thread(target=coverage)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ex.count(SYMBOL) == 2  # the defect K-4 fixes


def test_with_intent_single_sl(tmp_path):
    """GREEN: open_coin wired to the SAME intent/token -> exactly one SL."""
    ex = FakeExchange()
    store = SlPlacementStore(str(tmp_path / "risk_ledger.db"))
    store.init_schema()
    bar = threading.Barrier(2)
    placed_by = []

    def placer(by):
        bar.wait()
        if store.acquire(SYMBOL, TOKEN) == ACQUIRED:   # K-4: open_coin does this too now
            r = ex.place_algo_sl(SYMBOL, by)
            if r.get("algoId"):
                store.mark_placed(SYMBOL, TOKEN)
            else:
                store.release(SYMBOL, TOKEN)
            placed_by.append(by)
        # else BUSY -> skip; open_coin's sl_status verification confirms the other's SL

    t1 = threading.Thread(target=placer, args=("open_coin",))
    t2 = threading.Thread(target=placer, args=("ensure_sl_coverage",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ex.count(SYMBOL) == 1
    assert len(placed_by) == 1


def _mp_worker(db, by, bar, sl_list):
    bar.wait()
    st = SlPlacementStore(db)
    if st.acquire(SYMBOL, TOKEN) == ACQUIRED:
        time.sleep(0.05)
        sl_list.append(by)
        st.mark_placed(SYMBOL, TOKEN)


def test_cross_process_single_sl(tmp_path):
    """Stronger: two real OS processes on the same ledger -> one SL."""
    db = str(tmp_path / "risk_ledger.db")
    SlPlacementStore(db).init_schema()
    ctx = mp.get_context("fork")
    bar = ctx.Barrier(2)
    mgr = ctx.Manager()
    sl = mgr.list()
    p1 = ctx.Process(target=_mp_worker, args=(db, "open_coin", bar, sl))
    p2 = ctx.Process(target=_mp_worker, args=(db, "ensure_sl_coverage", bar, sl))
    p1.start(); p2.start(); p1.join(10); p2.join(10)
    assert len(list(sl)) == 1


def test_new_position_epoch_supersedes(tmp_path):
    """A genuinely new position (different entryPrice -> different token) is NOT
    blocked by a prior PLACED intent."""
    store = SlPlacementStore(str(tmp_path / "risk_ledger.db"))
    store.init_schema()
    assert store.acquire(SYMBOL, "LONG:50000.0") == ACQUIRED
    store.mark_placed(SYMBOL, "LONG:50000.0")
    # same symbol reopens later at a new entry -> new epoch -> may acquire again
    assert store.acquire(SYMBOL, "LONG:51234.5") == ACQUIRED
