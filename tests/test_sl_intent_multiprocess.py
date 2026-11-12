"""Cross-PROCESS proof for K-4 (not just cross-thread).

K-3/K-4 is about cross-process idempotency: two independent OS processes
(e.g. the main bot loop running open_coin, and a separate orphan-killer process
running ensure_sl_coverage) hitting the SAME on-disk SQLite ledger must still
produce exactly one SL. Threads can be serialized by the GIL; separate
processes cannot, so this is the stronger test. It exercises the real
BEGIN IMMEDIATE + WAL behaviour across process boundaries.
"""

import multiprocessing as mp
import time

from risk.ledger import RiskLedger
from risk.sl_intent import ensure_sl_placed

SYMBOL = "ETH_USDT"
EPOCH = 7


def _worker(db_path, by, algo_id, barrier, sl_list):
    barrier.wait()  # release both processes at the same instant

    def place(symbol, epoch):
        time.sleep(0.05)  # widen the window so a broken lock would double-place
        sl_list.append((symbol, epoch, by))
        return True

    ensure_sl_placed(
        SYMBOL, EPOCH, place_sl_fn=place,
        algo_id=algo_id, owner=by, db_path=db_path,
    )


def test_cross_process_single_sl(tmp_path):
    db = str(tmp_path / "risk_ledger.db")
    RiskLedger(db_path=db)  # create schema in the parent first

    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    mgr = ctx.Manager()
    sl_list = mgr.list()

    p1 = ctx.Process(target=_worker, args=(db, "open_coin", 100, barrier, sl_list))
    p2 = ctx.Process(target=_worker, args=(db, "ensure_sl_coverage", 200, barrier, sl_list))
    p1.start(); p2.start()
    p1.join(10); p2.join(10)

    placements = list(sl_list)
    assert len(placements) == 1, f"cross-process double SL: {placements}"

    owner = RiskLedger(db_path=db).current_owner(SYMBOL, EPOCH)
    assert owner is not None  # intent persisted on disk
