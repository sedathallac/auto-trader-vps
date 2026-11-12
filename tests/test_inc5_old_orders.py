"""Increment 5 — resolve pre-sent _close_order / _take_profit_limit_order before
the new close authority is activated (no double close).

classify_order_terminal is the order-status decision. The coordinator tests bind
it: a pre-sent NEW / PARTIALLY_FILLED order (or a cancel whose outcome is UNKNOWN)
blocks the handoff with a CRITICAL alarm and sends NO new close; a terminal
(CANCELED/FILLED) order lets the handoff proceed, and CLOSED still requires the
exchange flat confirmation.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import classify_order_terminal
from close_coordinator import CloseCoordinator, CloseState, ONEWAY, HEDGE

SYM = "SOLUSDT"


class TestClassifyOrderTerminal(unittest.TestCase):
    def test_new_not_terminal(self):
        self.assertFalse(classify_order_terminal({"orderId": 1, "status": "NEW"}))

    def test_partially_filled_not_terminal(self):
        self.assertFalse(classify_order_terminal({"status": "PARTIALLY_FILLED"}))

    def test_canceled_terminal(self):
        self.assertTrue(classify_order_terminal({"status": "CANCELED"}))
        self.assertTrue(classify_order_terminal({"status": "CANCELLED"}))

    def test_filled_terminal(self):
        self.assertTrue(classify_order_terminal({"status": "FILLED"}))

    def test_expired_rejected_terminal(self):
        self.assertTrue(classify_order_terminal({"status": "EXPIRED"}))
        self.assertTrue(classify_order_terminal({"status": "REJECTED"}))

    def test_missing_or_unknown_not_terminal(self):
        self.assertFalse(classify_order_terminal({}))
        self.assertFalse(classify_order_terminal({"status": ""}))
        self.assertFalse(classify_order_terminal(None))

    def test_status_only_not_algostatus(self):
        # classify_order_terminal now reads the REGULAR `status` field only.
        self.assertTrue(classify_order_terminal({"status": "FILLED"}))
        self.assertFalse(classify_order_terminal({"status": "PENDING_CANCEL"}))
        # an algoStatus-only dict is not a regular order here -> not terminal
        self.assertFalse(classify_order_terminal({"algoStatus": "FILLED"}))


class MockEx:
    def __init__(self, pos, mode=ONEWAY, protective=None, cancel_result=True):
        self._pos = Decimal(str(pos)); self._mode = mode
        self._protective = list(protective or [])
        self._cancel_result = cancel_result
        self.close_calls = []; self.stopped = 0; self.cancel_calls = []
    def position_amt(self, s): return self._pos
    def position_mode(self): return self._mode
    def protective_orders(self, s): return list(self._protective)
    def order_terminal(self, o): return classify_order_terminal(o)   # REAL classifier
    def cancel_order(self, o): self.cancel_calls.append(o); return self._cancel_result
    def stop_executor(self, s): self.stopped += 1
    def close_reduce_only(self, s, q, position_side=None):
        self.close_calls.append((Decimal(str(q)), position_side))
        if self._pos > 0: self._pos = max(Decimal("0"), self._pos - Decimal(str(q)))
        elif self._pos < 0: self._pos = min(Decimal("0"), self._pos + Decimal(str(q)))
        return Decimal(str(q))


class TestHandoffResolvesOldOrders(unittest.TestCase):
    def _block(self, status, cancel_result):
        ex = MockEx("0.3", protective=[{"orderId": 1, "symbol": SYM, "status": status}],
                    cancel_result=cancel_result)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        return r, ex

    def test_new_order_uncancellable_blocks(self):
        r, ex = self._block("NEW", cancel_result=False)   # cancel outcome UNKNOWN
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertTrue(r.critical)
        self.assertEqual(len(ex.close_calls), 0, "double close: new close sent over a live order")

    def test_partially_filled_blocks(self):
        r, ex = self._block("PARTIALLY_FILLED", cancel_result=False)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(len(ex.close_calls), 0)

    def test_cancel_unknown_blocks(self):
        # cancel request sent but outcome not confirmed terminal -> not resolved
        r, ex = self._block("NEW", cancel_result=False)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertTrue(ex.cancel_calls, "cancel not attempted")

    def test_new_order_cancelled_then_closes(self):
        r, ex = self._block("NEW", cancel_result=True)    # cancel confirmed terminal
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len(ex.close_calls), 1)

    def test_terminal_order_allows_close(self):
        ex = MockEx("0.3", protective=[{"orderId": 1, "symbol": SYM, "status": "CANCELED"}])
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(ex.cancel_calls, [], "cancelled an already-terminal order")

    def test_filled_order_allows_close(self):
        ex = MockEx("0.3", protective=[{"orderId": 1, "symbol": SYM, "status": "FILLED"}])
        r = CloseCoordinator(ex).request_close(SYM)
        self.assertTrue(r.ok)

    def test_restart_fresh_coordinator_blocks_on_pending(self):
        # a brand-new coordinator (process restart) must re-resolve, not assume gone
        ex = MockEx("-0.3", protective=[{"orderId": 9, "symbol": SYM, "status": "NEW"}],
                    cancel_result=False)
        r = CloseCoordinator(ex).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(len(ex.close_calls), 0)

    def test_oneway_after_resolution_reduce_only(self):
        ex = MockEx("0.3", mode=ONEWAY,
                    protective=[{"orderId": 1, "symbol": SYM, "status": "FILLED"}])
        CloseCoordinator(ex).request_close(SYM)
        self.assertEqual(ex.close_calls[0][1], None)  # reduceOnly path, no positionSide

    def test_hedge_after_resolution_position_side(self):
        ex = MockEx("0.3", mode=HEDGE,
                    protective=[{"orderId": 1, "symbol": SYM, "status": "FILLED"}])
        CloseCoordinator(ex).request_close(SYM)
        self.assertEqual(ex.close_calls[0][1], "LONG")


if __name__ == "__main__":
    unittest.main()
