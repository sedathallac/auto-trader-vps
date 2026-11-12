"""Core invariants for the single close authority (K1=B CloseCoordinator).

These test the coordinator against a mock exchange that models ONEWAY netting +
reduce-only clamping (Binance -2022: a reduce-only order can never cross zero).
Covers the SOLUSDT double-SELL class, idempotency, flat confirmation, HANDOFF
fail-closed on unresolved orders, ONEWAY vs HEDGE, the reverse guard and partial
close. Integration with the real BinanceAPI/HBAPI + fault injection follows.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_coordinator import CloseCoordinator, CloseState, ONEWAY, HEDGE


class MockExchange:
    def __init__(self, pos, mode=ONEWAY, protective=None, close_behavior="reduce_only"):
        self._pos = Decimal(str(pos))
        self._mode = mode
        self._protective = list(protective or [])
        self._cb = close_behavior
        self.stop_called = 0
        self.close_calls = []

    def position_amt(self, symbol):
        return self._pos

    def position_mode(self):
        return self._mode

    def protective_orders(self, symbol):
        return list(self._protective)

    def order_terminal(self, o):
        return bool(o.get("terminal", False))

    def cancel_order(self, o):
        return bool(o.get("cancel_terminal", True))

    def stop_executor(self, symbol):
        self.stop_called += 1

    def close_reduce_only(self, symbol, qty, position_side=None):
        self.close_calls.append((Decimal(str(qty)), position_side))
        q = Decimal(str(qty))
        if self._cb == "reduce_only":
            # clamp toward zero, never cross (true reduce-only semantics)
            if self._pos > 0:
                self._pos = max(Decimal("0"), self._pos - q)
            elif self._pos < 0:
                self._pos = min(Decimal("0"), self._pos + q)
        elif self._cb == "partial":
            if self._pos > 0:
                self._pos = self._pos - (min(q, self._pos) / 2)
            elif self._pos < 0:
                self._pos = self._pos + (min(q, -self._pos) / 2)
        elif self._cb == "reverse":
            self._pos = -self._pos  # buggy non-reduce-only: flips the position
        elif self._cb == "noop":
            pass
        return q


SYM = "SOLUSDT"


class TestCloseCoordinator(unittest.TestCase):
    def test_oneway_close_flattens(self):
        ex = MockExchange("0.3", mode=ONEWAY)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM, "TAKE_PROFIT")
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(r.action, "SENT_REDUCE_ONLY")
        self.assertEqual(ex._pos, Decimal("0"))
        self.assertEqual(ex.close_calls[0][1], None)  # no positionSide in ONEWAY

    def test_reduce_only_cannot_reverse_then_idempotent(self):
        # The SOLUSDT class: even an oversized close clamps to flat, and a second
        # (delayed) trigger is ignored -> no reverse SHORT can appear.
        ex = MockExchange("0.3", mode=ONEWAY)
        cc = CloseCoordinator(ex)
        r1 = cc.request_close(SYM, "STOP_LOSS")       # first (authoritative) close
        r2 = cc.request_close(SYM, "HB_TIME_LIMIT")   # delayed second trigger
        self.assertEqual(r1.action, "SENT_REDUCE_ONLY")
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(ex.close_calls), 1, "a second close order was sent")
        self.assertEqual(ex._pos, Decimal("0"))       # never flips to short

    def test_already_flat_is_closed_no_order(self):
        ex = MockExchange("0", mode=ONEWAY)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(len(ex.close_calls), 0)

    def test_unresolved_order_blocks_close(self):
        # A pre-existing close/TP order that cannot be confirmed terminal -> the
        # coordinator must NOT send a second close (fail-closed, critical).
        ex = MockExchange("0.3", mode=ONEWAY,
                          protective=[{"id": 1, "terminal": False, "cancel_terminal": False}])
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertTrue(r.critical)
        self.assertEqual(len(ex.close_calls), 0)
        self.assertEqual(cc.state_of(SYM), CloseState.HANDOFF_PENDING)

    def test_terminal_or_cancelled_orders_allow_close(self):
        ex = MockExchange("0.3", mode=ONEWAY,
                          protective=[{"id": 1, "terminal": True},
                                      {"id": 2, "terminal": False, "cancel_terminal": True}])
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)

    def test_hedge_uses_position_side_not_reduce_only(self):
        ex = MockExchange("0.3", mode=HEDGE)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(ex.close_calls[0][1], "LONG")  # positionSide targeted

    def test_hedge_short_targets_short_side(self):
        ex = MockExchange("-0.3", mode=HEDGE)
        cc = CloseCoordinator(ex)
        cc.request_close(SYM)
        self.assertEqual(ex.close_calls[0][1], "SHORT")

    def test_reverse_guard_fails_safe(self):
        # A buggy close that flips the position must be caught by flat-confirm.
        ex = MockExchange("0.3", mode=ONEWAY, close_behavior="reverse")
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "REVERSED")
        self.assertTrue(r.critical)
        self.assertEqual(cc.state_of(SYM), CloseState.FAILED_SAFE)
        self.assertNotIn(SYM, cc._closed)

    def test_partial_close_preserves_remaining(self):
        ex = MockExchange("0.3", mode=ONEWAY, close_behavior="partial")
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "PARTIAL")
        self.assertTrue(r.critical)
        self.assertGreater(ex._pos, Decimal("0"))  # remaining exposure kept
        self.assertEqual(len(ex.close_calls), 1, "did not blindly resend")

    def test_stop_executor_called_before_close(self):
        ex = MockExchange("0.3", mode=ONEWAY)
        cc = CloseCoordinator(ex)
        cc.request_close(SYM)
        self.assertEqual(ex.stop_called, 1)  # HB executor stopped (keep_position=True)

    def test_short_position_closed(self):
        ex = MockExchange("-0.3", mode=ONEWAY)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(ex._pos, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
