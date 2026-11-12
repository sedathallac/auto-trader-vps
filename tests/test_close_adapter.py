"""Real BinanceAPI/HBAPI adapter for the CloseCoordinator (items 1 + 2).

Mapping tests + HTTP-fault behaviour: on API error / timeout / bad payload the
adapter RAISES, and the end-to-end fault test proves the CloseCoordinator then
fails closed (FAILED_SAFE) instead of mistaking an outage for a closed position.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import BinanceHBCloseAdapter, CloseAdapterError
from close_coordinator import CloseCoordinator, CloseState, ONEWAY, HEDGE

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


class FakeBinance:
    def __init__(self, pos=None, mode_dual=False, algo=None, cancel=None):
        self._pos = pos                 # signed float or None
        self._mode = {"dualSidePosition": mode_dual} if mode_dual is not None else None
        self._algo = algo if algo is not None else []
        self._cancel = cancel if cancel is not None else {"code": "200"}
        self.posts = []
        self.raise_on_position_risk = None

    def position_risk(self):
        if self.raise_on_position_risk:
            raise self.raise_on_position_risk
        if self._pos is None:
            return {"code": -1, "msg": "positionRisk error"}
        return [{"symbol": SYM, "positionAmt": str(self._pos)}]

    def position_mode(self):
        return self._mode

    def open_algo_orders_for(self, symbol):
        return self._algo

    def cancel_algo_order(self, aid):
        return self._cancel

    def _post(self, path, params):
        self.posts.append((path, dict(params)))
        # simulate a reduce-only / positionSide MARKET fill -> position goes flat
        if self._pos is not None:
            self._pos = 0.0
        return {"orderId": 1, "status": "NEW"}


class FakeHB:
    def __init__(self, execs=None):
        self._execs = execs if execs is not None else []
        self.stopped = []

    def active_executors(self):
        return self._execs

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


class TestAdapterMapping(unittest.TestCase):
    def test_position_amt(self):
        a = BinanceHBCloseAdapter(FakeBinance(pos=-0.3), FakeHB())
        self.assertEqual(a.position_amt(SYM), Decimal("-0.3"))

    def test_position_amt_absent_is_zero(self):
        fb = FakeBinance(pos=0.0)
        fb._pos = None  # force error? no — test symbol-absent
        fb2 = FakeBinance(pos=0.0)
        fb2._pos = 0.0
        # a list without SYM:
        class _B(FakeBinance):
            def position_risk(self):
                return [{"symbol": "BTCUSDT", "positionAmt": "1"}]
        self.assertEqual(BinanceHBCloseAdapter(_B(), FakeHB()).position_amt(SYM), Decimal("0"))

    def test_position_amt_error_raises(self):
        a = BinanceHBCloseAdapter(FakeBinance(pos=None), FakeHB())  # error dict
        with self.assertRaises(CloseAdapterError):
            a.position_amt(SYM)

    def test_position_amt_timeout_propagates(self):
        import requests
        fb = FakeBinance(pos=-0.3)
        fb.raise_on_position_risk = requests.exceptions.Timeout("read timeout")
        with self.assertRaises(Exception):
            BinanceHBCloseAdapter(fb, FakeHB()).position_amt(SYM)

    def test_position_mode(self):
        self.assertEqual(BinanceHBCloseAdapter(FakeBinance(pos=0.0, mode_dual=False), FakeHB()).position_mode(), ONEWAY)
        self.assertEqual(BinanceHBCloseAdapter(FakeBinance(pos=0.0, mode_dual=True), FakeHB()).position_mode(), HEDGE)

    def test_position_mode_unverifiable_raises(self):
        fb = FakeBinance(pos=0.0); fb._mode = {"code": -1}
        with self.assertRaises(CloseAdapterError):
            BinanceHBCloseAdapter(fb, FakeHB()).position_mode()

    def test_protective_orders_error_raises(self):
        fb = FakeBinance(pos=0.0); fb._algo = {"code": -1, "msg": "err"}
        with self.assertRaises(CloseAdapterError):
            BinanceHBCloseAdapter(fb, FakeHB()).protective_orders(SYM)

    def test_order_terminal_and_cancel(self):
        a = BinanceHBCloseAdapter(FakeBinance(pos=0.0), FakeHB())
        self.assertTrue(a.order_terminal({"algoStatus": "FILLED"}))
        self.assertFalse(a.order_terminal({"algoStatus": "NEW"}))
        self.assertTrue(a.cancel_order({"algoId": 7}))  # cancel returns code 200
        amb = BinanceHBCloseAdapter(FakeBinance(pos=0.0, cancel={"code": -1}), FakeHB())
        self.assertFalse(amb.cancel_order({"algoId": 7}))  # ambiguous -> not terminal

    def test_stop_executor_keep_position_true(self):
        hb = FakeHB(execs=[{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1"}])
        BinanceHBCloseAdapter(FakeBinance(pos=-0.3), hb).stop_executor(SYM)
        self.assertEqual(hb.stopped, [("E1", True)])

    def test_close_reduce_only_oneway(self):
        fb = FakeBinance(pos=0.3)
        BinanceHBCloseAdapter(fb, FakeHB()).close_reduce_only(SYM, Decimal("0.3"), position_side=None)
        path, params = fb.posts[0]
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params.get("reduceOnly"), "true")
        self.assertNotIn("positionSide", params)

    def test_close_reduce_only_hedge(self):
        fb = FakeBinance(pos=0.3)
        BinanceHBCloseAdapter(fb, FakeHB()).close_reduce_only(SYM, Decimal("0.3"), position_side="LONG")
        _, params = fb.posts[0]
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params.get("positionSide"), "LONG")
        self.assertNotIn("reduceOnly", params)


class TestCoordinatorWithRealAdapterFaults(unittest.TestCase):
    def test_api_failure_fails_closed_not_flat(self):
        # positionRisk error -> adapter raises -> coordinator FAILED_SAFE, NOT CLOSED.
        fb = FakeBinance(pos=None)  # position_risk returns error dict
        hb = FakeHB(execs=[])
        cc = CloseCoordinator(BinanceHBCloseAdapter(fb, hb))
        r = cc.request_close(SYM, "TAKE_PROFIT")
        self.assertFalse(r.ok)
        self.assertEqual(r.state, CloseState.FAILED_SAFE)
        self.assertTrue(r.critical)
        self.assertEqual(len(fb.posts), 0, "a close order was sent despite API failure")

    def test_full_flow_closes_and_confirms_flat(self):
        fb = FakeBinance(pos=-0.3, mode_dual=False)
        hb = FakeHB(execs=[{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1"}])
        cc = CloseCoordinator(BinanceHBCloseAdapter(fb, hb))
        r = cc.request_close(SYM, "MANUAL")
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(hb.stopped, [("E1", True)])      # HB stopped keep_position
        self.assertEqual(len(fb.posts), 1)                # exactly one close
        self.assertEqual(fb.posts[0][1].get("reduceOnly"), "true")
        # delayed duplicate trigger -> ignored, no second order
        r2 = cc.request_close(SYM, "HB_LATE_TP")
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(fb.posts), 1)


if __name__ == "__main__":
    unittest.main()
