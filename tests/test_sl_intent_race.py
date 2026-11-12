"""K-4 RED/GREEN: prevent duplicate SL when open_coin races ensure_sl_coverage.

* ``test_race_without_intent_places_double_sl`` reproduces the defect (RED
  condition): the naive check-then-place path produces TWO stop-losses.
* ``test_race_with_intent_places_single_sl`` proves the fix (GREEN): the same
  race routed through ``ensure_sl_placed`` produces exactly ONE stop-loss.
"""

import threading
import time

from risk.ledger import RiskLedger
from risk.sl_intent import ensure_sl_placed

SYMBOL = "BTC_USDT"
EPOCH = 1


class FakeExchange:
    """Thread-safe stand-in for the real SL endpoint; counts placements."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sl_orders = []  # (symbol, epoch, by)

    def has_sl(self, symbol, epoch):
        with self._lock:
            return any(s == symbol and e == epoch for (s, e, _b) in self.sl_orders)

    def place_sl(self, symbol, epoch, by):
        time.sleep(0.02)  # widen the race window (simulated network latency)
        with self._lock:
            self.sl_orders.append((symbol, epoch, by))
        return {"symbol": symbol, "epoch": epoch, "by": by}

    def count_sl(self, symbol, epoch):
        with self._lock:
            return sum(1 for (s, e, _b) in self.sl_orders if s == symbol and e == epoch)


def _naive_place(ex, by, barrier):
    """TOCTOU path with NO intent lock -- reproduces the K-4 bug."""
    barrier.wait()
    if not ex.has_sl(SYMBOL, EPOCH):        # check
        ex.place_sl(SYMBOL, EPOCH, by)      # ...then act -> race


def test_race_without_intent_places_double_sl():
    """RED: open_coin and ensure_sl_coverage without the lock -> 2 SLs."""
    ex = FakeExchange()
    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=_naive_place, args=(ex, "open_coin", barrier))
    t2 = threading.Thread(target=_naive_place, args=(ex, "ensure_sl_coverage", barrier))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ex.count_sl(SYMBOL, EPOCH) == 2  # the defect: duplicate SL


def test_race_with_intent_places_single_sl(tmp_path):
    """GREEN: same race through ensure_sl_placed() -> exactly 1 SL."""
    ex = FakeExchange()
    ledger = RiskLedger(db_path=str(tmp_path / "risk_ledger.db"))
    barrier = threading.Barrier(2)
    results = {}

    def actor(by, algo_id):
        barrier.wait()
        placed, _ = ensure_sl_placed(
            SYMBOL, EPOCH,
            place_sl_fn=lambda s, e: ex.place_sl(s, e, by),
            ledger=ledger, algo_id=algo_id, owner=by,
        )
        results[by] = placed

    t1 = threading.Thread(target=actor, args=("open_coin", 100))
    t2 = threading.Thread(target=actor, args=("ensure_sl_coverage", 200))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert ex.count_sl(SYMBOL, EPOCH) == 1             # prevention works
    assert sum(1 for v in results.values() if v) == 1  # exactly one actor placed
    assert ledger.current_owner(SYMBOL, EPOCH) is not None  # intent recorded


def test_second_pass_is_idempotent(tmp_path):
    """A later ensure_sl_coverage pass must not place a second SL."""
    ex = FakeExchange()
    ledger = RiskLedger(db_path=str(tmp_path / "risk_ledger.db"))

    placed1, _ = ensure_sl_placed(
        SYMBOL, EPOCH, place_sl_fn=lambda s, e: ex.place_sl(s, e, "open_coin"),
        ledger=ledger, algo_id=100, owner="open_coin",
    )
    placed2, _ = ensure_sl_placed(
        SYMBOL, EPOCH, place_sl_fn=lambda s, e: ex.place_sl(s, e, "ensure_sl_coverage"),
        ledger=ledger, algo_id=200, owner="ensure_sl_coverage",
    )

    assert placed1 is True
    assert placed2 is False
    assert ex.count_sl(SYMBOL, EPOCH) == 1
