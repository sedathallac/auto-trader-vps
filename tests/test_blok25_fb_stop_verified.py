"""BLOK25 F-b — an executor stop is VERIFIED (TERMINATED != assumed), and an
UNKNOWN stop fails closed.

Root cause (BLOK23 F-b): every stop_executor response (open_coin not-filled
path, reconcile auto-heal, close adapter) was accepted without checking — a
failed stop left a RUNNING executor that could still place its own close order
and race the reduce-only POST. Also: a TERMINATED response was never
distinguished from a flat position.

Layers:
  1. HBAPI.stop_executor_verified against a fake HTTP boundary: verified stop,
     still-RUNNING after retries, search outage, stop POST failure.
  2. CloseCoordinator via the real adapter: UNKNOWN -> STOP_FAILED, NO close
     POST (fail-closed); verified stop -> close proceeds.
  3. Attribution after a verified stop: the stopped executor's tracked orders
     are captured BEFORE the stop, so step 2 of the handoff can still resolve
     them (a live tracked order blocks the close -> UNRESOLVED_ORDER, not
     ORDERS_QUERY_FAILED).
  4. open_coin not-filled path: STOPPED -> reservation released; UNKNOWN ->
     reservation KEPT (a partial fill may be a live position).
  5. Restart/recovery: after the executor is genuinely stopped, a FRESH
     coordinator/adapter completes the close.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import BinanceAPI, HBAPI
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState
from position_reservation import PositionReserver

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


class _Resp:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p


class FakeHBExchange:
    """HTTP-boundary fake for HBAPI: executors with statuses, a configurable
    stop outcome. `stop_terminalizes` False models the F-b failure (the stop
    POST 'succeeds' but the executor stays RUNNING)."""

    def __init__(self, executors=None, stop_terminalizes=True,
                 stop_raises=False, search_raises=False):
        self.executors = list(executors or [])
        self.stop_terminalizes = stop_terminalizes
        self.stop_raises = stop_raises
        self.search_raises = search_raises
        self.stops = []

    def handle_post(self, url, body):
        path = urlparse(url).path
        if path.endswith("/executors/search"):
            if self.search_raises:
                raise TimeoutError("search outage")
            if body == {"is_active": True}:
                return {"data": [e for e in self.executors
                                 if e.get("status") == "RUNNING"]}
            return {"data": list(self.executors)}
        if "/executors/" in path and path.endswith("/stop"):
            self.stops.append((path, body))
            if self.stop_raises:
                raise TimeoutError("stop POST outage")
            if self.stop_terminalizes:
                eid = path.split("/executors/")[1].rsplit("/stop", 1)[0]
                for e in self.executors:
                    if e.get("executor_id") == eid:
                        e["status"] = "TERMINATED"
            return {"status": "ok"}
        return {"code": -1121}


class FakeSession:
    def __init__(self, ex):
        self.ex = ex

    def post(self, url, headers=None, json=None, auth=None, timeout=None):
        return _Resp(self.ex.handle_post(url, json))


def _exec(eid="E1", status="RUNNING", order_ids=None):
    e = {"trading_pair": PAIR, "status": status, "executor_id": eid}
    e["custom_info"] = {"order_ids": order_ids if order_ids is not None else []}
    return e


class TestHBAPIVerifiedStop(unittest.TestCase):
    def setUp(self):
        self._orig = auto_trader._session

    def tearDown(self):
        auto_trader._session = self._orig

    def _hb(self, ex):
        auto_trader._session = FakeSession(ex)
        return HBAPI("http://hb", "u", "p")

    def test_verified_stop(self):
        ex = FakeHBExchange(executors=[_exec()])
        r = self._hb(ex).stop_executor_verified("E1", keep_position=True,
                                                attempts=2, delay=0)
        self.assertEqual(r["outcome"], "STOPPED")
        self.assertEqual(r["status"], "TERMINATED")
        self.assertEqual(len(ex.stops), 1)
        self.assertEqual(ex.stops[0][1]["keep_position"], True)

    def test_still_running_is_unknown(self):
        # F-b core: the stop POST returns ok but the executor stays RUNNING
        ex = FakeHBExchange(executors=[_exec()], stop_terminalizes=False)
        r = self._hb(ex).stop_executor_verified("E1", attempts=2, delay=0)
        self.assertEqual(r["outcome"], "UNKNOWN")
        self.assertEqual(len(ex.stops), 2)          # it did retry

    def test_stop_post_outage_is_unknown(self):
        ex = FakeHBExchange(executors=[_exec()], stop_raises=True)
        r = self._hb(ex).stop_executor_verified("E1", attempts=2, delay=0)
        self.assertEqual(r["outcome"], "UNKNOWN")
        self.assertIn("TimeoutError", str(r["response"].get("error")))

    def test_search_outage_is_unknown(self):
        # even a successful stop POST cannot be VERIFIED without the read-back
        ex = FakeHBExchange(executors=[_exec()], search_raises=True)
        r = self._hb(ex).stop_executor_verified("E1", attempts=2, delay=0)
        self.assertEqual(r["outcome"], "UNKNOWN")

    def test_terminated_before_stop_is_stopped(self):
        ex = FakeHBExchange(executors=[_exec(status="TERMINATED")])
        r = self._hb(ex).stop_executor_verified("E1", attempts=1, delay=0)
        self.assertEqual(r["outcome"], "STOPPED")


class FakeBinance:
    def __init__(self, amt="0.3"):
        self.amt = Decimal(str(amt))
        self.posts = []

    def position_risk(self):
        return [{"symbol": SYM, "positionAmt": str(self.amt),
                 "entryPrice": "80"}]

    def position_mode(self):
        return {"dualSidePosition": False}

    def open_orders(self, s):
        return []

    def open_algo_orders_raw(self, s):
        return []

    def _post(self, path, params, method="POST"):
        self.posts.append(dict(params))
        if str(params.get("reduceOnly", "")).lower() == "true":
            self.amt = Decimal("0")
        return {"orderId": 1, "status": "FILLED"}


class TestCoordinatorFailClosed(unittest.TestCase):
    def setUp(self):
        self._orig = auto_trader._session

    def tearDown(self):
        auto_trader._session = self._orig

    def _cc(self, ex, amt="0.3"):
        auto_trader._session = FakeSession(ex)
        fb = FakeBinance(amt=amt)
        return CloseCoordinator(BinanceHBCloseAdapter(fb, HBAPI("http://hb", "u", "p"))), fb

    def test_unverified_stop_no_close_post(self):
        ex = FakeHBExchange(executors=[_exec()], stop_terminalizes=False)
        cc, fb = self._cc(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "STOP_FAILED")
        self.assertTrue(r.critical)
        self.assertEqual(fb.posts, [], "a close POST was sent despite an unverified stop")

    def test_verified_stop_close_proceeds(self):
        ex = FakeHBExchange(executors=[_exec()])
        cc, fb = self._cc(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len(fb.posts), 1)
        self.assertEqual(fb.posts[0].get("reduceOnly"), "true")

    def test_stopped_executors_orders_still_attributed(self):
        # regression for the F-b follow-up: a verified stop removes the executor
        # from the is_active list, but its tracked live order must still be
        # resolved (captured before the stop) — NOT an attribution-unknown.
        ex = FakeHBExchange(executors=[_exec(order_ids=["C1"])])
        # a live open order C1 that can never be terminalized
        class _B(FakeBinance):
            def open_orders(self, s):
                return [{"symbol": SYM, "clientOrderId": "C1", "orderId": 9,
                         "status": "NEW"}]

            def query_order(self, s, order_id=None, orig_client_order_id=None):
                return {"orderId": 9, "status": "NEW"}

            def cancel_order(self, s, order_id=None, orig_client_order_id=None):
                return {"orderId": 9, "status": "NEW"}   # cancel does not take

        auto_trader._session = FakeSession(ex)
        cc = CloseCoordinator(BinanceHBCloseAdapter(_B(), HBAPI("http://hb", "u", "p")))
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "UNRESOLVED_ORDER",
                         "attribution was lost after the verified stop")
        self.assertTrue(r.critical)

    def test_restart_recovery_after_genuine_stop(self):
        # the process died after the verified stop but before the close; a FRESH
        # coordinator sees the executor already TERMINATED and completes cleanly
        ex = FakeHBExchange(executors=[_exec(status="TERMINATED")])
        cc, fb = self._cc(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(ex.stops, [])          # nothing left to stop
        self.assertEqual(len(fb.posts), 1)


class TestOpenCoinNotFilled(unittest.TestCase):
    """F-b on the open_coin not-filled path: STOPPED -> slot released;
    UNKNOWN -> slot KEPT (fail-closed; reconcile resolves later)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2
        self._orig = {}
        for n, v in [("check_liquidity", lambda b, s: (True, "ok")),
                     ("check_correlation_limit", lambda s, b: True),
                     ("decide_side", lambda c: (1, "LONG")),
                     ("calculate_atr", lambda b, s, interval="15m", period=14: 0.02),
                     ("calculate_position_size", lambda bal, c: (24.0, 1.0)),
                     ("add_blacklist", lambda *a, **k: None),
                     ("get_real_slippage", lambda b, s: 0.0)]:
            self._orig[n] = getattr(auto_trader, n)
            setattr(auto_trader, n, v)
        self._orig_db = auto_trader.SL_LEDGER_DB
        auto_trader.SL_LEDGER_DB = os.path.join(self.tmp, "risk_ledger.db")
        self._unprot = dict(auto_trader._OPEN_UNPROTECTED)
        auto_trader._OPEN_UNPROTECTED.clear()
        self._sleep = auto_trader.time.sleep
        auto_trader.time.sleep = lambda *a, **k: None
        self._hb_offline = auto_trader.HB_OFFLINE
        auto_trader.HB_OFFLINE = False

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(auto_trader, n, v)
        auto_trader.SL_LEDGER_DB = self._orig_db
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._unprot)
        auto_trader.time.sleep = self._sleep
        auto_trader.HB_OFFLINE = self._hb_offline
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "81.27", "quoteVolume": "1e9",
                "_score": 0.7, "priceChangePercent": "-2"}

    class B:
        def filters(self, s):
            return {"LOT_SIZE": {"stepSize": "0.001"},
                    "PRICE_FILTER": {"tickSize": "0.01"},
                    "MIN_NOTIONAL": {"notional": "5"}}

        def get_available_balance(self, a):
            return 1000.0

        def set_leverage(self, s, l):
            return {"leverage": l}

        def position_risk(self):
            return [{"symbol": SYM, "positionAmt": "0", "entryPrice": "0"}]

        def position_mode(self):
            return {"dualSidePosition": False}

        def open_algo_orders_raw(self, s):
            return []

    class HB:
        """Never fills (filled=0) with a configurable verified-stop outcome."""

        def __init__(self, outcome="STOPPED"):
            self.outcome = outcome
            self.created = []

        def active_executors(self):
            return []

        def create_executor(self, *a, **k):
            self.created.append(a)
            return {"executor_id": "E1"}

        def get_executor(self, eid):
            return {"filled_amount_quote": 0, "error_count": 0}

        def stop_executor_verified(self, eid, keep_position=True,
                                   attempts=3, delay=2.0):
            return {"status": "TERMINATED" if self.outcome == "STOPPED"
                    else "RUNNING", "outcome": self.outcome}

    def _run(self, outcome):
        reserver = PositionReserver(os.path.join(self.tmp, "risk_ledger.db"),
                                    max_positions=auto_trader.MAX_OPEN_POSITIONS)
        reserver.init_schema()
        r = auto_trader.open_coin(self.B(), self.HB(outcome), self._coin(),
                                  risk=None, reserver=reserver, coordinator=None)
        # a fresh reserver view over the same db = what reconcile would see
        view = PositionReserver(os.path.join(self.tmp, "risk_ledger.db"),
                                max_positions=auto_trader.MAX_OPEN_POSITIONS)
        rows = {(x["symbol"], x["state"]) for x in view.active_reservations()
                if x["state"] != "RELEASED"}
        return r, rows

    def test_stopped_releases_slot(self):
        r, live = self._run("STOPPED")
        self.assertFalse(r)                      # not filled -> no position
        self.assertEqual(live, set())            # slot released

    def test_unknown_keeps_slot(self):
        r, live = self._run("UNKNOWN")
        self.assertFalse(r)
        self.assertEqual(live, {(SYM, "PENDING")},   # slot KEPT (fail-closed)
                         "an UNKNOWN stop released the slot")


if __name__ == "__main__":
    unittest.main()
