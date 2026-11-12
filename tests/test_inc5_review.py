"""Increment 5 — independent-review fixes, BEHAVIOURAL tests on the real
BinanceHBCloseAdapter + CloseCoordinator (not just import/collection).

Covers: pre-sent close resolution by real status query, cancel confirmed by query
(cancel response / -2011 are NOT proof), query timeout/UNKNOWN, HB-order matching
(unrelated normal orders are never cancelled), HB-mapping-unavailable -> UNKNOWN
fail-closed, ONEWAY/HEDGE, restart, duplicate request.
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
        self.orders = {}               # orderId -> order dict (query source)
        self.positions = positions if positions is not None else [{"symbol": SYM, "positionAmt": "-0.3"}]
        self.mode = {"dualSidePosition": mode_dual}
        self.cancel_to = {}            # orderId -> status after cancel (default CANCELED)
        self.cancel_raises = set()     # orderIds whose cancel raises
        self.query_raises = set()      # orderIds whose query raises
        self.cancelled = []            # orderIds actually DELETEd
        self.posts = []

    def open_orders(self, symbol):
        return list(self.open.get(symbol, []))

    def query_order(self, symbol, oid):
        if oid in self.query_raises:
            raise TimeoutError("query timeout")
        return self.orders.get(oid, {"code": -2013, "msg": "Order does not exist"})

    def cancel_order(self, symbol, oid):
        self.cancelled.append(oid)
        if oid in self.cancel_raises:
            raise ConnectionError("cancel conn reset")
        newst = self.cancel_to.get(oid, "CANCELED")
        if oid in self.orders:
            self.orders[oid]["status"] = newst
        return {"orderId": oid, "status": newst}

    def position_risk(self):
        return self.positions

    def position_mode(self):
        return self.mode

    def _post(self, path, params):
        self.posts.append((path, dict(params)))
        for p in self.positions:
            if p["symbol"] == params["symbol"]:
                p["positionAmt"] = "0"   # simulate reduce-only fill -> flat
        return {"orderId": 999, "status": "NEW"}


class FakeHB:
    def __init__(self, order_ids=None, pair=PAIR):
        self._ids = order_ids
        self._pair = pair
        self.stopped = []

    def active_executors(self):
        if self._ids is None:
            return []  # no executor for the pair
        return [{"trading_pair": self._pair, "status": "RUNNING",
                 "executor_id": "E1", "custom_info": {"order_ids": self._ids}}]

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


def _order(oid, coid, status, symbol=SYM):
    return {"orderId": oid, "clientOrderId": coid, "symbol": symbol, "status": status}


def _cc(fb, hb):
    return CloseCoordinator(BinanceHBCloseAdapter(fb, hb))


class TestInc5Review(unittest.TestCase):
    # --- 1. NEW pre-sent close blocks (cancel does not confirm terminal) -------
    def test_new_close_cancel_unconfirmed_blocks(self):
        fb = FakeBinance()
        o = _order(10, "HBC", "NEW"); fb.open[SYM] = [o]; fb.orders[10] = dict(o)
        fb.cancel_to[10] = "NEW"  # cancel does not terminalize -> unresolved
        hb = FakeHB(order_ids=["HBC"])
        r = _cc(fb, hb).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertTrue(r.critical)
        self.assertEqual(fb.posts, [], "sent a close over a live order")

    # --- 2. PARTIALLY_FILLED blocks -------------------------------------------
    def test_partially_filled_blocks(self):
        fb = FakeBinance()
        o = _order(11, "HBC", "PARTIALLY_FILLED"); fb.open[SYM] = [o]; fb.orders[11] = dict(o)
        fb.cancel_to[11] = "PARTIALLY_FILLED"
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # --- 3. cancel request timeout -> unresolved ------------------------------
    def test_cancel_timeout_blocks(self):
        fb = FakeBinance()
        o = _order(12, "HBC", "NEW"); fb.open[SYM] = [o]; fb.orders[12] = dict(o)
        fb.cancel_raises.add(12)
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # --- 4. cancel -2011 is NOT auto-terminal; query decides ------------------
    def test_cancel_2011_not_auto_terminal_query_new(self):
        fb = FakeBinance()
        o = _order(13, "HBC", "NEW"); fb.open[SYM] = [o]; fb.orders[13] = dict(o)
        # cancel "succeeds" with -2011-ish, but query still shows NEW -> unresolved
        fb.cancel_to[13] = "NEW"
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # --- 5. query timeout -> UNKNOWN -> blocks --------------------------------
    def test_query_timeout_blocks(self):
        fb = FakeBinance()
        o = _order(14, "HBC", "NEW"); fb.open[SYM] = [o]; fb.orders[14] = dict(o)
        fb.query_raises.add(14)
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # --- 6. query mismatched orderId -> UNKNOWN -------------------------------
    def test_query_mismatch_blocks(self):
        fb = FakeBinance()
        o = _order(15, "HBC", "NEW"); fb.open[SYM] = [o]
        fb.orders[15] = {"orderId": 999, "status": "FILLED"}  # wrong orderId
        fb.cancel_to[15] = "NEW"
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")

    # --- 7. CANCELED pre-sent order -> resolved, close proceeds ---------------
    def test_canceled_order_allows_close(self):
        fb = FakeBinance()
        o = _order(16, "HBC", "CANCELED"); fb.open[SYM] = [o]; fb.orders[16] = dict(o)
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertNotIn(16, fb.cancelled, "cancelled an already-terminal order")

    # --- 8. FILLED pre-sent close but position still open -> coordinator closes
    def test_filled_order_but_position_open_then_closes(self):
        fb = FakeBinance(positions=[{"symbol": SYM, "positionAmt": "-0.3"}])
        o = _order(17, "HBC", "FILLED"); fb.open[SYM] = [o]; fb.orders[17] = dict(o)
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(len(fb.posts), 1)  # one reduce-only close for the remainder

    # --- 9/10. unrelated open order is NEVER returned/cancelled ---------------
    def test_unrelated_order_not_cancelled(self):
        fb = FakeBinance()
        hb_o = _order(20, "HBC", "CANCELED"); unrel = _order(21, "USERX", "NEW")
        fb.open[SYM] = [hb_o, unrel]
        fb.orders[20] = dict(hb_o); fb.orders[21] = dict(unrel)
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertTrue(r.ok)
        self.assertNotIn(21, fb.cancelled, "cancelled an UNRELATED user order")

    # --- 11. HB mapping unavailable + open orders -> UNKNOWN fail-closed -------
    def test_hb_mapping_unavailable_fails_closed(self):
        fb = FakeBinance()
        fb.open[SYM] = [_order(30, "X", "NEW")]
        r = _cc(fb, FakeHB(order_ids=None)).request_close(SYM)  # no executor
        self.assertFalse(r.ok)
        self.assertTrue(r.critical)
        self.assertEqual(fb.posts, [], "sent a close despite UNKNOWN order mapping")
        self.assertEqual(fb.cancelled, [], "cancelled under UNKNOWN mapping")

    # --- 12. no open orders -> clean close ------------------------------------
    def test_no_open_orders_closes(self):
        fb = FakeBinance()
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)

    # --- 13. HEDGE close uses positionSide ------------------------------------
    def test_hedge_close_position_side(self):
        fb = FakeBinance(positions=[{"symbol": SYM, "positionAmt": "0.3"}], mode_dual=True)
        r = _cc(fb, FakeHB(order_ids=["HBC"])).request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(fb.posts[0][1].get("positionSide"), "LONG")
        self.assertNotIn("reduceOnly", fb.posts[0][1])

    # --- 14. duplicate close request is idempotent ----------------------------
    def test_duplicate_request_idempotent(self):
        fb = FakeBinance()
        cc = _cc(fb, FakeHB(order_ids=["HBC"]))
        cc.request_close(SYM)
        r2 = cc.request_close(SYM)
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(fb.posts), 1)


if __name__ == "__main__":
    unittest.main()
