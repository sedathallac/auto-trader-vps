"""Faz 2 — Execution Engine robustness: a created-but-UNFILLED/errored executor must
not leak its reserved slot or leave a running executor.

Root cause: open_coin's fill-check (`if filled < 1 or err > 0`) blacklisted and
returned WITHOUT releasing the atomic reservation or stopping the HB executor. A
leaked PENDING reservation counts toward MAX_OPEN_POSITIONS (used = exchange ∪
reservations) until reconcile's TTL — two fill-failures could block ALL new entries
for up to a loop interval. The fix stops the executor (keep_position=True, so any
partial fill becomes a position ensure_sl_coverage will protect) and releases the
slot (exchange positions are still counted in the cap, so releasing never
under-counts). Real open_coin call chain; only Binance/HB faked.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from position_reservation import PositionReserver

SYM = "XAUUSDT"
PAIR = "XAU-USDT"


class FakeBinance:
    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.001"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 1000.0

    def set_leverage(self, symbol, lev):
        return {"leverage": lev}

    def position_risk(self):
        return []                      # no open positions

    def place_algo_sl(self, *a, **k):
        return {"algoId": 1}


class FakeHB:
    def __init__(self):
        self.created = []
        self.stopped = []

    def active_executors(self):
        return [] if not self.created else \
            [{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "EX1",
              "custom_info": {"order_ids": []}}]

    def create_executor(self, symbol, amount, lev, sl, tp, tl, side=1):
        self.created.append(symbol)
        return {"executor_id": "EX1"}

    def get_executor(self, eid):
        return {"filled_amount_quote": 0.0, "error_count": 8}   # UNFILLED + errors

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


class Faz2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self.reserver = PositionReserver(self.db, 2)
        self.reserver.init_schema()
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2
        self._orig = {}
        for name, val in [
            ("check_liquidity", lambda b, s: (True, "ok")),
            ("check_correlation_limit", lambda s, b: True),
            ("decide_side", lambda coin: (1, "LONG")),
            ("calculate_atr", lambda b, s, interval="15m", period=14: 0.02),
            ("calculate_position_size", lambda bal, coin: (50.0, 1.0)),
            ("add_blacklist", lambda *a, **k: None),
        ]:
            self._orig[name] = getattr(auto_trader, name)
            setattr(auto_trader, name, val)
        self._orig_sleep = auto_trader.time.sleep
        auto_trader.time.sleep = lambda *a, **k: None

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(auto_trader, n, v)
        auto_trader.time.sleep = self._orig_sleep
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "3900", "quoteVolume": "1000000",
                "_score": 0.7, "priceChangePercent": "2"}


class TestExecutionRobustness(Faz2Base):
    def test_unfilled_executor_releases_reservation_and_stops(self):
        fb = FakeBinance()
        hb = FakeHB()
        r = auto_trader.open_coin(fb, hb, self._coin(), risk=None,
                                  reserver=self.reserver, coordinator=None)
        self.assertFalse(r)
        # robustness: the leaked reservation must be freed...
        self.assertEqual(self.reserver.active_reservations(), [],
                         "reservation leaked on fill-failure")
        # ...and the unfilled/errored executor stopped (keep_position=True).
        self.assertEqual(hb.stopped, [("EX1", True)],
                         "unfilled executor not stopped")

    def test_slot_reusable_after_fill_failure(self):
        # after a fill-failure, both slots must be free again (no cap starvation).
        fb = FakeBinance()
        auto_trader.open_coin(fb, FakeHB(), self._coin(), risk=None,
                              reserver=self.reserver, coordinator=None)
        self.assertEqual(self.reserver.used_slots([]), 0)   # full capacity restored


if __name__ == "__main__":
    unittest.main()
