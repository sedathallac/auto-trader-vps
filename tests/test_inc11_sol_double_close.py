"""Increment 11 — SOLUSDT real-code double-close / reverse-SHORT regression.

Historical incident class (see BOT_KNOWLEDGE_BASE/findings/2026-11-11_sol_double_close):
a SOLUSDT LONG was closed by a first SELL, then a SECOND SELL filled on the now-flat
position and opened a reverse SHORT. (The ONEWAY reverse is KISMEN in the archive;
not asserted here as fact.)

This drives the REAL call chain — CloseCoordinator -> BinanceHBCloseAdapter ->
BinanceAPI / HBAPI — and mocks ONLY the external HTTP boundary (the module-level
`_session`). The fake session is a stateful ONEWAY exchange that models Binance
netting AND the reduceOnly clamp (-2022: a reduceOnly order can never cross zero),
so the real order-sending path (BinanceAPI._post building real params) is exercised.

Core acceptance: after the first close no second closing order is sent, and the
coordinator's path can never produce a reverse SHORT. The behavioural RED (weaken
the coordinator idempotency + a stale-positionRisk race -> a SECOND close is sent)
is shown in the delivery notes.
"""
import os
import sys
import unittest
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import BinanceAPI, HBAPI
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class FakeExchange:
    """Stateful SOLUSDT ONEWAY exchange behind the HTTP boundary.

    `amt` is the REAL signed position; `reported_amt`, when set, is what
    positionRisk reports (models a settle/replay race where the read is stale).
    """
    def __init__(self, amt="0.3", dual=False, fill_qty=None,
                 cancel_terminalizes=True, position_read_error=False):
        self.amt = Decimal(str(amt))
        self.reported_amt = None
        self.dual = dual
        self.fill_qty = Decimal(str(fill_qty)) if fill_qty is not None else None
        self.cancel_terminalizes = cancel_terminalizes
        self.position_read_error = position_read_error
        self.open_orders = {}        # symbol -> [regular order dicts]
        self.algo = {}               # symbol -> [algo order dicts]
        self.order_db = []           # regular orders, for query/cancel
        self.executors = []          # HB executors
        self.posts = []              # every POST /fapi/v1/order attempt (closes)
        self.stops = []              # HB stop calls
        self._oid = 5000

    # ---- GET ----
    def _position_risk(self):
        if self.position_read_error:
            return {"code": -1001, "msg": "Internal error; positionRisk unavailable"}
        shown = self.reported_amt if self.reported_amt is not None else self.amt
        return [{"symbol": SYM, "positionAmt": str(shown), "entryPrice": "83.08"}]

    def _query_order(self, p):
        oid = p.get("orderId")
        coid = p.get("origClientOrderId")
        for o in self.order_db:
            if (oid is not None and str(o.get("orderId")) == str(oid)) or \
               (coid is not None and o.get("clientOrderId") == coid):
                return dict(o)
        return {"code": -2013, "msg": "Order does not exist."}

    def handle_get(self, url):
        path = urlparse(url).path
        p = _params(url)
        if path == "/fapi/v2/positionRisk":
            return self._position_risk()
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": self.dual}
        if path == "/fapi/v1/openOrders":
            return list(self.open_orders.get(p.get("symbol"), []))
        if path == "/fapi/v1/order":
            return self._query_order(p)
        if path == "/fapi/v1/openAlgoOrders":
            return list(self.algo.get(p.get("symbol"), []))
        return {"code": -1121, "msg": f"unrouted GET {path}"}

    # ---- POST ----
    def _place_order(self, p):
        side = p["side"]
        qty = Decimal(p["quantity"])
        reduce_only = str(p.get("reduceOnly", "")).lower() == "true"
        cur = self.amt
        signed = -qty if side == "SELL" else qty
        outcome = "FILLED"
        if reduce_only:
            # Binance -2022: a reduceOnly order can only reduce toward zero and never
            # cross it; on a flat position (or wrong direction) it is REJECTED.
            if cur == 0 or (cur > 0 and signed > 0) or (cur < 0 and signed < 0):
                self.posts.append({"side": side, "qty": str(qty),
                                   "reduceOnly": True, "outcome": "REJECTED_-2022",
                                   "result_amt": str(self.amt)})
                return {"code": -2022, "msg": "ReduceOnly Order is rejected."}
            fill = self.fill_qty if self.fill_qty is not None else qty
            delta = -fill if side == "SELL" else fill
            new = cur + delta
            if (cur > 0 and new < 0) or (cur < 0 and new > 0):
                new = Decimal("0")    # clamp at zero, never cross
            self.amt = new
        else:
            # non-reduceOnly ONEWAY netting: CAN cross zero -> reverse (the HB bug).
            self.amt = cur + signed
        oid = self._oid
        self._oid += 1
        self.posts.append({"side": side, "qty": str(qty), "reduceOnly": reduce_only,
                           "outcome": outcome, "result_amt": str(self.amt)})
        return {"orderId": oid, "status": "FILLED", "symbol": p.get("symbol"),
                "side": side, "executedQty": str(qty)}

    def handle_post(self, url, body):
        path = urlparse(url).path
        if path.endswith("/executors/search"):
            if body == {"is_active": True}:
                return {"data": [e for e in self.executors
                                 if e.get("status") == "RUNNING"]}
            return {"data": list(self.executors)}
        if "/executors/" in path and path.endswith("/stop"):
            self.stops.append((path, body))
            return {"status": "ok"}
        if path == "/fapi/v1/order":
            return self._place_order(_params(url))
        return {"code": -1121, "msg": f"unrouted POST {path}"}

    # ---- DELETE ----
    def handle_delete(self, url):
        p = _params(url)
        if urlparse(url).path == "/fapi/v1/order":
            for o in self.order_db:
                if (p.get("orderId") and str(o.get("orderId")) == str(p["orderId"])) or \
                   (p.get("origClientOrderId") and o.get("clientOrderId") == p["origClientOrderId"]):
                    if self.cancel_terminalizes:
                        o["status"] = "CANCELED"
                    return {"orderId": o.get("orderId"), "status": o["status"]}
            return {"code": -2011, "msg": "Unknown order sent."}
        return {"code": -1121, "msg": "unrouted DELETE"}


class FakeSession:
    def __init__(self, ex):
        self.ex = ex

    def get(self, url, headers=None, params=None, timeout=None):
        return _Resp(self.ex.handle_get(url))

    def post(self, url, headers=None, json=None, auth=None, timeout=None):
        return _Resp(self.ex.handle_post(url, json))

    def delete(self, url, headers=None, timeout=None):
        return _Resp(self.ex.handle_delete(url))


def _running_executor():
    return {"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
            "custom_info": {"order_ids": []}}


class SolBase(unittest.TestCase):
    def _wire(self, ex):
        self._orig_session = auto_trader._session
        auto_trader._session = FakeSession(ex)
        binance = BinanceAPI("k", "s")
        hb = HBAPI("http://hb.local", "u", "p")
        adapter = BinanceHBCloseAdapter(binance, hb)
        return CloseCoordinator(adapter), ex

    def tearDown(self):
        if getattr(self, "_orig_session", None) is not None:
            auto_trader._session = self._orig_session


class TestSolDoubleClose(SolBase):
    # 1 + 10. LONG 0.3 -> one reduceOnly SELL 0.3 -> flat; never a reverse SHORT.
    def test_single_close_flat_no_reverse(self):
        cc, ex = self._wire(FakeExchange(amt="0.3", dual=False))
        ex.executors = [_running_executor()]
        r = cc.request_close(SYM, "MANUAL")
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len(ex.posts), 1)
        self.assertEqual(ex.posts[0]["side"], "SELL")
        self.assertTrue(ex.posts[0]["reduceOnly"])
        self.assertEqual(ex.amt, Decimal("0"))          # flat
        self.assertGreaterEqual(ex.amt, Decimal("0"))   # never reversed SHORT
        self.assertEqual(len(ex.stops), 1)              # HB stopped keep_position=True
        self.assertEqual(ex.stops[0][1], {"close_type": "EARLY_STOP",
                                          "keep_position": True})

    # 2 + 5. A duplicate / second independent close trigger sends NO second order.
    def test_second_trigger_ignored(self):
        cc, ex = self._wire(FakeExchange(amt="0.3"))
        cc.request_close(SYM, "TAKE_PROFIT")
        r2 = cc.request_close(SYM, "HB_LATE_TP")
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(ex.posts), 1)              # still exactly one close

    def test_two_independent_requests_single_close(self):
        cc, ex = self._wire(FakeExchange(amt="0.3"))
        a = cc.request_take_profit(SYM)
        b = cc.request_time_limit(SYM)
        self.assertTrue(a.ok)
        self.assertEqual(b.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(ex.posts), 1)

    # 3. First close's flat-confirm is UNKNOWN -> not CLOSED, and a later retry
    #    (positionRisk now readable + flat) sends NO new order.
    def test_unknown_first_result_no_second_send(self):
        cc, ex = self._wire(FakeExchange(amt="0.3"))
        # the close fills (amt -> 0) but the flat-confirm read errors out
        orig = ex._position_risk
        state = {"n": 0}

        def flaky():
            # reads: 1=coordinator step3, 2=adapter close_reduce_only, 3=flat-confirm
            state["n"] += 1
            if state["n"] == 3:      # the post-close flat-confirm read errors out
                return {"code": -1001, "msg": "read timeout"}
            return orig()
        ex._position_risk = flaky
        r1 = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r1.ok)
        self.assertEqual(r1.action, "FLAT_CONFIRM_FAILED")
        self.assertEqual(len(ex.posts), 1)              # one close attempt
        ex._position_risk = orig                        # read recovers, truly flat
        r2 = cc.request_close(SYM, "RETRY")
        self.assertEqual(r2.action, "ALREADY_FLAT")
        self.assertEqual(len(ex.posts), 1)              # NO second close

    # 4. First close partially fills -> the real remaining amount is read, no resend.
    def test_partial_fill_reads_remainder(self):
        cc, ex = self._wire(FakeExchange(amt="0.3", fill_qty="0.2"))
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "PARTIAL")
        self.assertEqual(r.detail["remaining"], "0.1")  # real remainder, not assumed
        self.assertEqual(ex.amt, Decimal("0.1"))
        self.assertEqual(len(ex.posts), 1)              # no blind resend

    # 6. A still-live old HB close order blocks the handoff -> no close is sent.
    def test_old_hb_order_blocks_close(self):
        cc, ex = self._wire(FakeExchange(amt="0.3", cancel_terminalizes=False))
        e = _running_executor(); e["custom_info"] = {"order_ids": ["C1"]}
        ex.executors = [e]
        ex.open_orders[SYM] = [{"symbol": SYM, "clientOrderId": "C1",
                                "orderId": 111, "status": "NEW"}]
        ex.order_db = [{"symbol": SYM, "clientOrderId": "C1", "orderId": 111,
                        "status": "NEW"}]
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertTrue(r.critical)
        self.assertEqual(len(ex.posts), 0)              # refused to send a second close

    # 7. Restart: a FRESH coordinator reconciles from exchange truth; position was
    #    already closed before restart (exchange flat) -> no resend.
    def test_restart_reconciles_no_resend(self):
        cc, ex = self._wire(FakeExchange(amt="0"))      # already flat on the exchange
        r = cc.request_close(SYM, "RECONCILE")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(len(ex.posts), 0)

    # 8. Exchange flat while local state still believes OPEN -> safe (no order).
    def test_exchange_flat_local_open_safe(self):
        cc, ex = self._wire(FakeExchange(amt="0"))
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(len(ex.posts), 0)
        self.assertEqual(ex.amt, Decimal("0"))

    # 9. Exchange position OPEN while local state believes CLOSED -> exchange truth
    #    is used; a fresh coordinator does NOT treat it as closed and reads the open
    #    position (migration also classifies it open, never NOT_OPEN).
    def test_exchange_open_local_closed_not_treated_closed(self):
        from migration import PositionMigrator, NOT_OPEN
        cc, ex = self._wire(FakeExchange(amt="0.3"))
        ex.algo[SYM] = []  # no SL -> migration will flag open+unprotected, never flat
        res = PositionMigrator(cc.ex).migrate(SYM)
        self.assertNotEqual(res.state, NOT_OPEN)        # exchange-open never == closed
        self.assertEqual(res.amt, Decimal("0.3"))

    # Idempotency under a stale-positionRisk race (the RED target): a replayed
    # trigger arrives while positionRisk still reports the old amount. The guard
    # must ignore it BEFORE any read, so no second close is attempted.
    def test_duplicate_trigger_stale_race_single_close(self):
        cc, ex = self._wire(FakeExchange(amt="0.3"))
        r1 = cc.request_close(SYM, "SL_FILL")
        self.assertTrue(r1.ok)
        ex.reported_amt = Decimal("0.3")          # settle/replay race: stale read
        r2 = cc.request_close(SYM, "WS_REPLAY")
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(ex.posts), 1)        # no second SELL despite stale read
        self.assertGreaterEqual(ex.amt, Decimal("0"))

    # 10 (explicit). The fake truly reverses on a NON-reduceOnly SELL (the historical
    #    mechanism), proving the coordinator's reduceOnly path is what prevents it.
    def test_non_reduceonly_reverses_but_coordinator_does_not(self):
        cc, ex = self._wire(FakeExchange(amt="0"))
        # historical HB-style close: non-reduceOnly SELL on a FLAT position -> SHORT
        binance = cc.ex.binance
        binance._post("/fapi/v1/order", {"symbol": SYM, "side": "SELL",
                                         "type": "MARKET", "quantity": "0.3"})
        self.assertEqual(ex.amt, Decimal("-0.3"))       # reverse SHORT reproduced
        # the coordinator, on that same flat position, sends NO reduceOnly order
        ex.amt = Decimal("0"); ex.posts.clear()
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(len(ex.posts), 0)
        self.assertEqual(ex.amt, Decimal("0"))


class TestSlCoverageRootCause(unittest.TestCase):
    """RC-2 — FIXED in inc11.1: ensure_sl_coverage no longer treats ANY algo order
    as coverage; it validates mode-aware full protection via inc10.1 sl_status. The
    inc11 finding is kept here as a locked regression (a TAKE_PROFIT algo order must
    NOT count as an SL). Full RC-2 behaviour is covered in test_inc111_sl_coverage."""

    class _FakeBinanceSL:
        def __init__(self, algo):
            self._algo = algo
            self.placed = []
        def position_risk(self):
            return [{"symbol": SYM, "positionAmt": "0.3", "entryPrice": "83.08"}]
        def position_mode(self):
            return {"dualSidePosition": False}       # ONEWAY
        def open_algo_orders_raw(self, sym):
            return list(self._algo)
        def open_algo_orders_for(self, sym):
            return list(self._algo)
        def filters(self, sym):
            return {"PRICE_FILTER": {"tickSize": "0.01"}}
        def _get(self, path, params=None, signed=False):
            return []
        def place_algo_sl(self, sym, trig, qty, side):
            self.placed.append((sym, trig, qty, side))
            return {"algoId": 1}

    def setUp(self):
        auto_trader._OPEN_UNPROTECTED.clear()

    def tearDown(self):
        auto_trader._OPEN_UNPROTECTED.clear()

    def test_nonstop_algo_order_not_coverage_sl_placed(self):
        # Only a TAKE_PROFIT algo order exists -> NOT a real SL -> an SL is placed.
        fb = self._FakeBinanceSL([{"symbol": SYM, "type": "TAKE_PROFIT_MARKET",
                                   "algoStatus": "WORKING"}])
        auto_trader.ensure_sl_coverage(fb)
        self.assertEqual(len(fb.placed), 1)          # RC-2 fixed: no longer "covered"


if __name__ == "__main__":
    unittest.main()
