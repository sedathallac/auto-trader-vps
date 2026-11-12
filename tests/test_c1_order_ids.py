"""C-1 fix — None entries in HB executor order_ids must not block the handoff.

A K1=B executor disables close/TP barriers, so get_custom_info().order_ids is
[open_id, None, None]: the None slots are DISABLED barriers (no order was ever
placed), NOT unresolvable orders. The old _hb_order_ids did `ids |= set(oids)`,
turning None into a stub whose order_terminal/cancel both fail -> UNRESOLVED_ORDER
-> request_close refused the close (position left open/unprotected). The fix drops
None/empty slots while STILL resolving every real tracked id (an unknown/active
real order still blocks -> fail-closed). Real call chain; only Binance/HB faked.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


class FakeBinance:
    """Open SOL LONG; a reduce-only close flattens it. query_order returns the
    status registered for an (orderId|clientOrderId)."""
    def __init__(self, amt="0.3", open_orders=None, orders=None):
        self.amt = Decimal(str(amt))
        self._open_orders = open_orders or []
        self._orders = orders or {}      # clientOrderId -> status dict
        self.posts = []

    def position_risk(self):
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

    def _post(self, path, params, method="POST"):
        if path == "/fapi/v1/order" and method == "POST":
            self.posts.append(dict(params))
            if str(params.get("reduceOnly", "")).lower() == "true":
                self.amt = Decimal("0")
            return {"orderId": 1, "status": "FILLED"}
        return {"orderId": 1, "status": "NEW"}


class FakeHB:
    def __init__(self, order_ids, present=True):
        self._order_ids = order_ids
        self._present = present
        self.stopped = []

    def active_executors(self):
        if not self._present:
            return []
        return [{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
                 "custom_info": {"order_ids": self._order_ids}}]

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


def _cc(binance, hb):
    return CloseCoordinator(BinanceHBCloseAdapter(binance, hb))


class TestC1OrderIds(unittest.TestCase):
    # 1. [open_id, None, None] + ENTRY FILLED -> close NOT blocked by the None slots.
    def test_none_slots_filled_entry_closes(self):
        fb = FakeBinance(open_orders=[], orders={"OPEN": {"clientOrderId": "OPEN",
                                                          "status": "FILLED"}})
        hb = FakeHB(["OPEN", None, None])
        r = _cc(fb, hb).request_close(SYM, "SL_UNVERIFIED")
        self.assertTrue(r.ok, f"close wrongly blocked: {r.action} {r.reason}")
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len(fb.posts), 1)

    # 2. ENTRY NEW (still live, cancel cannot terminalise) -> handoff not completed.
    def test_entry_new_not_terminal_blocks(self):
        fb = FakeBinance(open_orders=[{"clientOrderId": "OPEN", "orderId": 7,
                                       "status": "NEW", "symbol": SYM}],
                         orders={"OPEN": {"clientOrderId": "OPEN", "status": "NEW"}})
        hb = FakeHB(["OPEN", None, None])
        r = _cc(fb, hb).request_close(SYM, "X")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(fb.posts, [])

    # 3. ENTRY PARTIALLY_FILLED -> the entry order is cancelled (terminal), then the
    #    REAL remaining position (0.2) is read from the exchange and closed.
    def test_entry_partial_then_reread(self):
        fb = FakeBinance(amt="0.2",   # real filled remainder after a partial entry
                         open_orders=[{"clientOrderId": "OPEN", "orderId": 7,
                                       "status": "PARTIALLY_FILLED", "symbol": SYM}],
                         orders={"OPEN": {"clientOrderId": "OPEN", "orderId": 7,
                                          "status": "CANCELED"}})
        hb = FakeHB(["OPEN", None, None])
        r = _cc(fb, hb).request_close(SYM, "X")
        self.assertTrue(r.ok)
        self.assertEqual(len(fb.posts), 1)   # closes the real remaining amount
        self.assertEqual(fb.amt, Decimal("0"))

    # 4. CLOSE NEW (a pre-sent close still live) -> no new close sent.
    def test_close_order_new_blocks(self):
        fb = FakeBinance(open_orders=[{"clientOrderId": "CLOSE", "orderId": 8,
                                       "status": "NEW", "symbol": SYM}],
                         orders={"OPEN": {"clientOrderId": "OPEN", "status": "FILLED"},
                                 "CLOSE": {"clientOrderId": "CLOSE", "status": "NEW"}})
        hb = FakeHB(["OPEN", "CLOSE", None])
        r = _cc(fb, hb).request_close(SYM, "X")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(fb.posts, [])

    # 5. TP NEW -> not handed off until resolved.
    def test_tp_order_new_blocks(self):
        fb = FakeBinance(open_orders=[{"clientOrderId": "TP", "orderId": 9,
                                       "status": "NEW", "symbol": SYM}],
                         orders={"OPEN": {"clientOrderId": "OPEN", "status": "FILLED"},
                                 "TP": {"clientOrderId": "TP", "status": "NEW"}})
        hb = FakeHB(["OPEN", None, "TP"])
        r = _cc(fb, hb).request_close(SYM, "X")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(fb.posts, [])

    # 6. Ambiguous id match (query returns a different clientOrderId) -> fail-closed.
    def test_ambiguous_id_match_unknown(self):
        fb = FakeBinance(open_orders=[{"clientOrderId": "OPEN", "orderId": 7,
                                       "status": "NEW", "symbol": SYM}],
                         orders={"OPEN": {"clientOrderId": "DIFFERENT", "status": "FILLED"}})
        hb = FakeHB(["OPEN", None, None])
        r = _cc(fb, hb).request_close(SYM, "X")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")  # identity mismatch -> not terminal
        self.assertEqual(fb.posts, [])

    # 7. Executor record not found but an open order exists -> UNKNOWN, fail-closed.
    def test_executor_not_found_unknown(self):
        fb = FakeBinance(open_orders=[{"clientOrderId": "X", "orderId": 1,
                                       "status": "NEW", "symbol": SYM}])
        hb = FakeHB([], present=False)
        r = _cc(fb, hb).request_close(SYM, "X")
        self.assertFalse(r.ok)
        self.assertTrue(r.critical)
        self.assertEqual(fb.posts, [])

    # None is never queried as a real order id (no stub for None).
    def test_none_not_queried(self):
        fb = FakeBinance(open_orders=[], orders={"OPEN": {"clientOrderId": "OPEN",
                                                          "status": "FILLED"}})
        adapter = BinanceHBCloseAdapter(fb, FakeHB(["OPEN", None, None]))
        pending = adapter.protective_orders(SYM)
        coids = [o.get("clientOrderId") for o in pending]
        self.assertNotIn(None, coids)       # no None stub
        self.assertEqual(coids, ["OPEN"])


if __name__ == "__main__":
    unittest.main()
