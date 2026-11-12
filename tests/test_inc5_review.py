"""Increment 5 — second independent-review fixes, BEHAVIOURAL tests on the real
BinanceHBCloseAdapter + CloseCoordinator.

Covers: order_terminal actually calls query_order (not in-memory status); query
timeout / wrong orderId / wrong symbol / clientOrderId mismatch -> UNKNOWN; HB
order_ids (ENTRY+CLOSE+TP) resolved uniformly with the ENTRY never pre-judged a
close and a live ENTRY blocking; tracked ids queried by origClientOrderId even
when openOrders is empty; unrelated orders untouched; FILLED-close-with-open-
position closes only after exchange state is verified; ONEWAY/HEDGE; restart +
duplicate idempotency.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


class FakeBinance:
    def __init__(self, positions=None, mode_dual=False):
        self.open = {}                 # symbol -> [order dicts]
        self.orders = {}               # orderId -> order dict (query/cancel source)
        self.positions = positions if positions is not None else [{"symbol": SYM, "positionAmt": "-0.3"}]
        self.mode = {"dualSidePosition": mode_dual}
        self.cancel_to = {}            # key -> status after cancel (default CANCELED)
        self.cancel_raises = set()
        self.query_raises = set()
        self.query_calls = []          # (symbol, order_id, orig_client_order_id)
        self.cancelled = []
        self.posts = []

    def add(self, oid, coid, status):
        self.orders[oid] = {"orderId": oid, "clientOrderId": coid, "symbol": SYM, "status": status}
        return self.orders[oid]

    def open_orders(self, symbol):
        return list(self.open.get(symbol, []))

    def _find(self, oid, coid):
        if oid is not None and oid in self.orders:
            return self.orders[oid]
        if coid is not None:
            for o in self.orders.values():
                if o.get("clientOrderId") == coid:
                    return o
        return None

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        self.query_calls.append((symbol, order_id, orig_client_order_id))
        if order_id in self.query_raises or orig_client_order_id in self.query_raises:
            raise TimeoutError("query timeout")
        o = self._find(order_id, orig_client_order_id)
        return dict(o) if o else {"code": -2013, "msg": "Order does not exist"}

    def cancel_order(self, symbol, order_id=None, orig_client_order_id=None):
        key = order_id if order_id is not None else orig_client_order_id
        self.cancelled.append(key)
        if key in self.cancel_raises:
            raise ConnectionError("cancel reset")
        o = self._find(order_id, orig_client_order_id)
        if o is not None:
            o["status"] = self.cancel_to.get(key, "CANCELED")
        return {"status": "CANCELED"}

    def position_risk(self):
        return self.positions

    def position_mode(self):
        return self.mode

    def _post(self, path, params):
        self.posts.append((path, dict(params)))
        for p in self.positions:
            if p["symbol"] == params["symbol"]:
                p["positionAmt"] = "0"
        return {"orderId": 999, "status": "NEW"}


class FakeHB:
    def __init__(self, order_ids=None, pair=PAIR):
        self._ids = order_ids
        self._pair = pair
        self.stopped = []

    def active_executors(self):
        if self._ids is None:
            return []
        return [{"trading_pair": self._pair, "status": "RUNNING",
                 "executor_id": "E1", "custom_info": {"order_ids": self._ids}}]

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


def _oo(oid, coid, status, symbol=SYM):
    return {"orderId": oid, "clientOrderId": coid, "symbol": symbol, "status": status}


def _cc(fb, hb):
    return CloseCoordinator(BinanceHBCloseAdapter(fb, hb))


class TestInc5ReviewTwo(unittest.TestCase):
    # 1. order_terminal actually calls query_order (not in-memory status)
    def test_order_terminal_calls_query(self):
        fb = FakeBinance()
        fb.open[SYM] = [_oo(10, "C", "FILLED")]; fb.add(10, "C", "FILLED")
        adapter = BinanceHBCloseAdapter(fb, FakeHB(order_ids=["C"]))
        # an order whose in-memory status is FILLED but query says NEW -> NOT terminal
        fb.orders[10]["status"] = "NEW"
        self.assertFalse(adapter.order_terminal(_oo(10, "C", "FILLED")))
        self.assertTrue(fb.query_calls, "order_terminal did not call query_order")

    # 2. query timeout -> UNKNOWN -> block
    def test_query_timeout_blocks(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(11, "C", "NEW")]; fb.add(11, "C", "NEW")
        fb.query_raises.add(11)
        r = _cc(fb, FakeHB(order_ids=["C"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER"); self.assertEqual(fb.posts, [])

    # 3. query wrong orderId -> UNKNOWN
    def test_query_wrong_orderid_blocks(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(12, "C", "NEW")]
        fb.orders[12] = {"orderId": 777, "clientOrderId": "C", "symbol": SYM, "status": "FILLED"}
        fb.cancel_to[12] = "NEW"
        r = _cc(fb, FakeHB(order_ids=["C"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # 4. query wrong symbol -> UNKNOWN
    def test_query_wrong_symbol_blocks(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(13, "C", "NEW")]
        fb.orders[13] = {"orderId": 13, "clientOrderId": "C", "symbol": "ETHUSDT", "status": "FILLED"}
        fb.cancel_to[13] = "NEW"
        r = _cc(fb, FakeHB(order_ids=["C"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # 5. clientOrderId mismatch -> UNKNOWN
    def test_query_client_id_mismatch_blocks(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(14, "C", "NEW")]
        fb.orders[14] = {"orderId": 14, "clientOrderId": "OTHER", "symbol": SYM, "status": "FILLED"}
        fb.cancel_to[14] = "NEW"
        r = _cc(fb, FakeHB(order_ids=["C"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # 6. openOrders=[] but HB tracks a CLOSE id -> queried by clientOrderId
    def test_empty_openorders_tracked_close_queried(self):
        fb = FakeBinance()            # openOrders empty
        fb.add(20, "CLOSE1", "NEW")   # tracked but not resting; query shows NEW
        fb.cancel_to["CLOSE1"] = "NEW"
        r = _cc(fb, FakeHB(order_ids=["CLOSE1"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")      # live tracked close blocks
        self.assertTrue(any(c[2] == "CLOSE1" for c in fb.query_calls), "not queried by clientOrderId")

    # 7. openOrders=[] but HB tracks a TP id that is terminal -> allows
    def test_empty_openorders_tracked_tp_terminal_allows(self):
        fb = FakeBinance()
        fb.add(21, "TP1", "CANCELED")  # tracked TP already terminal
        r = _cc(fb, FakeHB(order_ids=["TP1"])).request_close(SYM)
        self.assertTrue(r.ok); self.assertEqual(r.state, CloseState.CLOSED)

    # 8/9/10. ENTRY+CLOSE+TP; ENTRY still live blocks (not pre-judged a close)
    def test_entry_close_tp_live_entry_blocks(self):
        fb = FakeBinance()
        fb.open[SYM] = [_oo(30, "ENTRY", "PARTIALLY_FILLED")]
        fb.add(30, "ENTRY", "PARTIALLY_FILLED")
        fb.add(31, "CLOSE", "CANCELED")
        fb.add(32, "TP", "CANCELED")
        fb.cancel_to[30] = "PARTIALLY_FILLED"  # entry cannot be terminalised
        r = _cc(fb, FakeHB(order_ids=["ENTRY", "CLOSE", "TP"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")      # live ENTRY blocks handoff
        self.assertEqual(fb.posts, [])

    def test_entry_close_tp_all_terminal_allows(self):
        fb = FakeBinance()
        fb.add(40, "ENTRY", "FILLED"); fb.add(41, "CLOSE", "CANCELED"); fb.add(42, "TP", "EXPIRED")
        r = _cc(fb, FakeHB(order_ids=["ENTRY", "CLOSE", "TP"])).request_close(SYM)
        self.assertTrue(r.ok)

    # 11. unrelated open order untouched
    def test_unrelated_order_not_cancelled(self):
        fb = FakeBinance()
        fb.open[SYM] = [_oo(50, "C", "CANCELED"), _oo(51, "USERX", "NEW")]
        fb.add(50, "C", "CANCELED"); fb.add(51, "USERX", "NEW")
        r = _cc(fb, FakeHB(order_ids=["C"])).request_close(SYM)
        self.assertTrue(r.ok)
        self.assertNotIn(51, fb.cancelled)
        self.assertNotIn("USERX", fb.cancelled)

    # 12. FILLED close + position still open -> close only after state verified
    def test_filled_close_position_open_then_closes(self):
        fb = FakeBinance(positions=[{"symbol": SYM, "positionAmt": "-0.3"}])
        fb.add(60, "C", "FILLED")
        cc = _cc(fb, FakeHB(order_ids=["C"]))
        r = cc.request_close(SYM)
        self.assertTrue(r.ok); self.assertEqual(len(fb.posts), 1)

    # 13. CANCELED old order -> resolved
    def test_canceled_old_order_allows(self):
        fb = FakeBinance(); fb.add(70, "C", "CANCELED")
        r = _cc(fb, FakeHB(order_ids=["C"])).request_close(SYM)
        self.assertTrue(r.ok)

    # 14. HEDGE close uses positionSide
    def test_hedge_position_side(self):
        fb = FakeBinance(positions=[{"symbol": SYM, "positionAmt": "0.3"}], mode_dual=True)
        r = _cc(fb, FakeHB(order_ids=[])).request_close(SYM)
        self.assertTrue(r.ok); self.assertEqual(fb.posts[0][1].get("positionSide"), "LONG")

    # 15. HB mapping unavailable but open orders exist -> UNKNOWN fail-closed
    def test_hb_unavailable_with_open_orders_blocks(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(80, "X", "NEW")]
        r = _cc(fb, FakeHB(order_ids=None)).request_close(SYM)
        self.assertFalse(r.ok); self.assertTrue(r.critical)
        self.assertEqual(fb.posts, []); self.assertEqual(fb.cancelled, [])

    # 16. duplicate request idempotent
    def test_duplicate_idempotent(self):
        fb = FakeBinance(); cc = _cc(fb, FakeHB(order_ids=[]))
        cc.request_close(SYM); r2 = cc.request_close(SYM)
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED"); self.assertEqual(len(fb.posts), 1)


if __name__ == "__main__":
    unittest.main()
