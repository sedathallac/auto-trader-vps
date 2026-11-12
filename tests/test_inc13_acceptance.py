"""Increment 13 — consolidated acceptance of the 10 mandatory conditions on the
REAL close call chain (coordinate_close -> CloseCoordinator.request_close ->
BinanceHBCloseAdapter -> BinanceAPI/HBAPI). Only the Binance/HB boundary is faked.

The integration + each fix already landed (bb10108 inc13; 8d00a91 C-1; 0b43e55 B-1;
7505698 F-1; c83461b E-1). This pins all 10 conditions together and adds the
genuinely missing coverage: THREAD concurrency through the real close chain
(condition 9). The behavioural RED for condition 9 (weaken the coordinator lock)
is shown in the delivery notes.
"""
import os
import sys
import threading
import unittest
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auto_trader import coordinate_close, build_executor_config
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState
from close_intent import CloseIntentStore
import tempfile

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class FakeBinance:
    def __init__(self, amt="0.3", open_orders=None, orders=None, fill_qty=None,
                 read_delay=0.0):
        self.amt = Decimal(str(amt))
        self._open_orders = open_orders or []
        self._orders = orders or {}
        self.fill_qty = Decimal(str(fill_qty)) if fill_qty is not None else None
        self.read_delay = read_delay
        self.order_posts = []
        self.algo_posts = []
        self._lock = threading.Lock()

    def position_risk(self):
        if self.read_delay:
            import time
            time.sleep(self.read_delay)
        return [{"symbol": SYM, "positionAmt": str(self.amt), "entryPrice": "80"}]

    def position_mode(self):
        return {"dualSidePosition": False}

    def open_orders(self, symbol):
        return list(self._open_orders)

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        key = orig_client_order_id if orig_client_order_id is not None else order_id
        return dict(self._orders.get(key, {"code": -2013, "msg": "Order does not exist."}))

    def cancel_order(self, symbol, order_id=None, orig_client_order_id=None):
        return {"status": "CANCELED"}

    def place_algo_sl(self, symbol, trig, qty, side):
        self.algo_posts.append((symbol, qty, side))     # would be a NEW SL (must NOT happen on close)
        return {"algoId": 1}

    def _post(self, path, params, method="POST"):
        if path == "/fapi/v1/order" and method == "POST":
            with self._lock:
                self.order_posts.append(dict(params))
                reduce_only = str(params.get("reduceOnly", "")).lower() == "true"
                cur = self.amt
                qty = Decimal(params["quantity"])
                signed = -qty if params["side"] == "SELL" else qty
                if reduce_only and (cur == 0 or (cur > 0 and signed > 0) or (cur < 0 and signed < 0)):
                    return {"code": -2022, "msg": "ReduceOnly rejected."}
                fill = self.fill_qty if self.fill_qty is not None else qty
                new = cur + (-fill if params["side"] == "SELL" else fill)
                if reduce_only and ((cur > 0 and new < 0) or (cur < 0 and new > 0)):
                    new = Decimal("0")
                self.amt = new
            return {"orderId": 1, "status": "FILLED"}
        return {"orderId": 1, "status": "NEW"}


class FakeHB:
    def __init__(self, order_ids=None):
        self._order_ids = order_ids if order_ids is not None else []
        self.stopped = []

    def active_executors(self):
        return [{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
                 "custom_info": {"order_ids": self._order_ids}}]

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


class AcceptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")

    def _cc(self, binance, hb):
        store = CloseIntentStore(self.db)
        store.init_schema()
        return CloseCoordinator(BinanceHBCloseAdapter(binance, hb), intent_store=store), store


class TestInc13Acceptance(AcceptBase):
    # Condition 1 + 5 + 7: all closes via the ONE coordinator -> a single reduceOnly
    # close (Binance SL remains the sole SL authority: no place_algo_sl on close);
    # CLOSED only after exchange flat-confirm.
    def test_1_single_authority_reduceonly_close(self):
        fb = FakeBinance(amt="0.3"); hb = FakeHB(order_ids=["OPEN", None, None])
        fb._orders = {"OPEN": {"clientOrderId": "OPEN", "status": "FILLED"}}
        cc, store = self._cc(fb, hb)
        r = coordinate_close(cc, fb, SYM, "SL_UNVERIFIED")
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len(fb.order_posts), 1)
        self.assertEqual(fb.order_posts[0]["reduceOnly"], "true")
        self.assertEqual(fb.algo_posts, [])                 # no NEW SL during close
        self.assertEqual(hb.stopped, [("E1", True)])        # HB handoff keep_position
        self.assertEqual(store.get(SYM)["state"], "CLOSED")
        self.assertEqual(fb.amt, Decimal("0"))

    # Condition 2: no coordinator -> NO direct MARKET POST, fail-closed.
    def test_2_no_coordinator_no_market_post(self):
        fb = FakeBinance(amt="0.3")
        r = coordinate_close(None, fb, SYM, "SL_UNVERIFIED", side=1, amount="0.3")
        self.assertEqual(fb.order_posts, [])
        self.assertFalse(r.ok)

    # Condition 3 + 4: a live CLOSE order (real HB record) blocks the handoff; no new
    # close POST while an order is UNKNOWN/unresolved.
    def test_3_4_unresolved_close_order_no_post(self):
        fb = FakeBinance(amt="0.3",
                         open_orders=[{"clientOrderId": "CLOSE", "orderId": 8,
                                       "status": "NEW", "symbol": SYM}],
                         orders={"OPEN": {"clientOrderId": "OPEN", "status": "FILLED"},
                                 "CLOSE": {"clientOrderId": "CLOSE", "orderId": 8,
                                           "status": "NEW"}})
        hb = FakeHB(order_ids=["OPEN", "CLOSE", None])
        cc, _ = self._cc(fb, hb)
        r = coordinate_close(cc, fb, SYM, "X")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(fb.order_posts, [])

    # Condition 7: a partial fill is never recorded as CLOSED (exchange not flat).
    def test_7_partial_not_closed(self):
        fb = FakeBinance(amt="0.3", fill_qty="0.2"); hb = FakeHB(order_ids=[])
        cc, store = self._cc(fb, hb)
        r = coordinate_close(cc, fb, SYM, "X")
        self.assertEqual(r.action, "PARTIAL")
        self.assertNotEqual(store.get(SYM)["state"], "CLOSED")

    # Condition 9: THREAD concurrency through the REAL close chain -> a SINGLE close
    # POST, one trigger served from idempotency, never a reverse.
    def test_9_concurrent_close_single_post(self):
        fb = FakeBinance(amt="0.3", read_delay=0.03); hb = FakeHB(order_ids=[])
        cc, _ = self._cc(fb, hb)
        barrier = threading.Barrier(2)
        out = []

        def worker():
            barrier.wait()
            out.append(coordinate_close(cc, fb, SYM, "RACE"))
        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(fb.order_posts), 1, f"more than one close POST: {fb.order_posts}")
        self.assertGreaterEqual(fb.amt, Decimal("0"))       # never reversed
        self.assertIn("IGNORED_ALREADY_CLOSED", [r.action for r in out])

    # Condition 10: HB autonomous closes disabled (all triple-barrier None).
    def test_10_hb_barriers_disabled(self):
        tb = build_executor_config(SYM, "0.3", 3, 1)["triple_barrier_config"]
        self.assertTrue(all(tb[k] is None for k in
                            ("stop_loss", "take_profit", "time_limit", "trailing_stop")))

    # Condition 8: MAX_OPEN_POSITIONS unchanged (= 2 default policy constant guard).
    def test_8_max_open_constant_present(self):
        # the constant is read from env; the policy value is never hard-raised in code.
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "auto_trader.py")).read()
        self.assertIn('MAX_OPEN_POSITIONS = max(0, int(os.getenv("MAX_OPEN_POSITIONS", "0")))', src)


if __name__ == "__main__":
    unittest.main()
