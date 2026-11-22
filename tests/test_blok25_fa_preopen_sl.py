"""BLOK25 F-a — pre-open leftover-algo-SL cleanup is VERIFIED, and a close never
leaves a leftover algo SL behind.

Root cause (BLOK23 F-a): the open_coin pre-open reconciliation fired
cancel_algo_order() and IGNORED the response; emergency closes left the exchange
SL authority's algo SL alive after the position was flat, so it could fire on a
FUTURE position.

Layers:
  1. cancel_symbol_algo_sl_verified unit + failure injection (outage, failed
     cancel, order-still-live, unaddressable order, malformed payload).
  2. open_coin integration: an unverifiable cancel -> NO new SL placed (fail-
     closed), symbol flagged OPEN_UNPROTECTED, SL-placement intent released.
  3. CloseCoordinator integration: leftover algo SL after a close is resolved
     (cancelled+verified) or the close fails safe (ALGO_QUERY_FAILED /
     ALGO_LEFTOVER_UNRESOLVED) — never CLOSED with a leftover live.
  4. Restart/recovery: a crash mid-cancel converges on the next cycle.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import cancel_symbol_algo_sl_verified
from close_coordinator import CloseCoordinator, CloseState, ONEWAY

SYM = "SOLUSDT"


def _stop(algo_id=786683, status="NEW"):
    return {"algoId": algo_id, "orderType": "STOP_MARKET", "algoType": "CONDITIONAL",
            "symbol": SYM, "side": "SELL", "positionSide": "BOTH", "reduceOnly": True,
            "quantity": "0.3", "algoStatus": status, "triggerPrice": "80.29"}


class FakeBinanceSL:
    """Minimal algo-order-book fake for cancel_symbol_algo_sl_verified.

    fail modes: 'raw_raise' (openAlgoOrders outage), 'raw_error' (error dict),
    'cancel_raise' (cancel POST throws), 'no_effect' (cancel 'succeeds' but the
    order stays live), 'no_id' (live STOP without algoId)."""

    def __init__(self, orders=None, fail=None):
        self.orders = list(orders or [])
        self.fail = fail
        self.cancel_calls = []

    def open_algo_orders_raw(self, symbol):
        if self.fail == "raw_raise":
            raise TimeoutError("read timeout")
        if self.fail == "raw_error":
            return {"code": -1001, "msg": "Internal error"}
        return list(self.orders)

    def cancel_algo_order(self, algo_id):
        self.cancel_calls.append(algo_id)
        if self.fail == "cancel_raise":
            raise TimeoutError("cancel timeout")
        if self.fail != "no_effect":
            self.orders = [o for o in self.orders if o.get("algoId") != algo_id]
        return {"algoId": algo_id, "code": "200"}


class TestVerifiedCancel(unittest.TestCase):
    # ---- regression: verified cancel of a leftover live STOP ----
    def test_leftover_cancelled_and_verified(self):
        b = FakeBinanceSL(orders=[_stop()])
        self.assertTrue(cancel_symbol_algo_sl_verified(b, SYM))
        self.assertEqual(b.cancel_calls, [786683])
        self.assertEqual(b.orders, [])

    def test_no_orders_is_verified_true(self):
        self.assertTrue(cancel_symbol_algo_sl_verified(FakeBinanceSL(), SYM))

    def test_non_stop_orders_untouched(self):
        # a resting TAKE_PROFIT_MARKET is NOT a STOP -> not cancelled by the
        # SL cleanup (it is the close coordinator's job to resolve it)
        tp = {"algoId": 5, "orderType": "TAKE_PROFIT_MARKET", "algoStatus": "NEW"}
        b = FakeBinanceSL(orders=[tp])
        self.assertTrue(cancel_symbol_algo_sl_verified(b, SYM))
        self.assertEqual(b.cancel_calls, [])

    def test_dead_status_not_live(self):
        b = FakeBinanceSL(orders=[_stop(status="CANCELED")])
        self.assertTrue(cancel_symbol_algo_sl_verified(b, SYM))
        self.assertEqual(b.cancel_calls, [])

    # ---- failure injection: every unverifiable step -> False ----
    def test_outage_on_read_is_false(self):
        self.assertFalse(cancel_symbol_algo_sl_verified(
            FakeBinanceSL(orders=[_stop()], fail="raw_raise"), SYM))

    def test_error_dict_on_read_is_false(self):
        self.assertFalse(cancel_symbol_algo_sl_verified(
            FakeBinanceSL(orders=[_stop()], fail="raw_error"), SYM))

    def test_cancel_exception_is_false(self):
        b = FakeBinanceSL(orders=[_stop()], fail="cancel_raise")
        self.assertFalse(cancel_symbol_algo_sl_verified(b, SYM))

    def test_cancel_no_effect_is_false(self):
        # the classic F-a residue: cancel 'succeeds' but the order survives ->
        # must NOT be treated as cleaned (re-query catches it)
        b = FakeBinanceSL(orders=[_stop()], fail="no_effect")
        self.assertFalse(cancel_symbol_algo_sl_verified(b, SYM))
        self.assertTrue(b.cancel_calls)      # it did try

    def test_live_stop_without_algo_id_is_false(self):
        o = _stop(); o["algoId"] = None
        self.assertFalse(cancel_symbol_algo_sl_verified(
            FakeBinanceSL(orders=[o]), SYM))

    def test_malformed_element_is_false(self):
        self.assertFalse(cancel_symbol_algo_sl_verified(
            FakeBinanceSL(orders=["not-a-dict"]), SYM))

    # ---- restart/recovery: crash mid-cancel converges next cycle ----
    def test_recovery_after_crash_mid_cancel(self):
        b = FakeBinanceSL(orders=[_stop()])
        b.cancel_algo_order = lambda aid: (_ for _ in ()).throw(
            TimeoutError("process crashed mid-cancel"))
        self.assertFalse(cancel_symbol_algo_sl_verified(b, SYM))  # cycle 1: fail-closed
        b.__dict__.pop("cancel_algo_order")                       # 'restart': healed
        self.assertTrue(cancel_symbol_algo_sl_verified(b, SYM))   # cycle 2: converges


# ---------------- open_coin integration (fail-closed, no blind SL) -----------
class _OpenCoinBase(unittest.TestCase):
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
        self._orig_unprot = dict(auto_trader._OPEN_UNPROTECTED)
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
        auto_trader._OPEN_UNPROTECTED.update(self._orig_unprot)
        auto_trader.time.sleep = self._sleep
        auto_trader.HB_OFFLINE = self._hb_offline
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "81.27", "quoteVolume": "1e9",
                "_score": 0.7, "priceChangePercent": "-2"}


class _Binance(_OpenCoinBase):
    """open_coin path fake: fills, then a live position; configurable leftover
    algo SL book + cancel behaviour (fail modes as FakeBinanceSL)."""

    class B:
        def __init__(self, fail=None, leftover=None):
            self.amt = Decimal("0.3")
            self.fail = fail
            self.leftover = list(leftover or [])
            self.placed = []
            self.cancel_calls = []

        def filters(self, s):
            return {"LOT_SIZE": {"stepSize": "0.001"},
                    "PRICE_FILTER": {"tickSize": "0.01"},
                    "MIN_NOTIONAL": {"notional": "5"}}

        def get_available_balance(self, a):
            return 1000.0

        def set_leverage(self, s, l):
            return {"leverage": l}

        def position_risk(self):
            return [{"symbol": SYM, "positionAmt": str(self.amt),
                     "entryPrice": "81.27"}]

        def position_mode(self):
            return {"dualSidePosition": False}

        def open_algo_orders_raw(self, s):
            if self.fail == "raw_raise":
                raise TimeoutError("read timeout")
            return list(self.leftover)

        def cancel_algo_order(self, aid):
            self.cancel_calls.append(aid)
            if self.fail == "cancel_raise":
                raise TimeoutError("cancel timeout")
            if self.fail != "no_effect":
                self.leftover = [o for o in self.leftover
                                 if o.get("algoId") != aid]
            return {"algoId": aid, "code": "200"}

        def place_algo_sl(self, s, trig, qty, side):
            self.placed.append((trig, qty, side))
            return {"algoId": 878098}

    class HB:
        def __init__(self):
            self.created = []

        def active_executors(self):
            return []

        def create_executor(self, *a, **k):
            self.created.append(a)
            return {"executor_id": "E1"}

        def get_executor(self, eid):
            return {"filled_amount_quote": 24.4, "error_count": 0}


class TestOpenCoinFailClosed(_OpenCoinBase):
    def _run(self, fail, leftover):
        b = _Binance.B(fail=fail, leftover=leftover)
        hb = _Binance.HB()
        r = auto_trader.open_coin(b, hb, self._coin(), risk=None, reserver=None,
                                  coordinator=None)
        return b, r

    def test_verified_cancel_then_place(self):
        b, r = self._run(None, [_stop()])
        self.assertEqual(b.cancel_calls, [786683])      # leftover cancelled
        self.assertEqual(len(b.placed), 1)              # and the new SL placed
        self.assertEqual(auto_trader._OPEN_UNPROTECTED, {})

    def test_unverified_cancel_no_new_sl(self):
        # F-a: cancel 'succeeds' but the leftover survives -> NO new SL next to it
        b, r = self._run("no_effect", [_stop()])
        self.assertEqual(b.placed, [], "a new SL was placed next to a leftover")
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)

    def test_cancel_outage_no_new_sl(self):
        b, r = self._run("cancel_raise", [_stop()])
        self.assertEqual(b.placed, [])
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)

    def test_read_outage_no_new_sl(self):
        b, r = self._run("raw_raise", [_stop()])
        self.assertEqual(b.placed, [])
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)


# ---------------- CloseCoordinator leftover resolution ------------------------
class MockExchange:
    """Coordinator-level mock with a configurable leftover algo book."""

    def __init__(self, pos="0.3", algo_leftovers=None, algo_query_raises=False,
                 cancel_algo_ok=True):
        self._pos = Decimal(str(pos))
        self.algo_leftovers = list(algo_leftovers or [])
        self.algo_query_raises = algo_query_raises
        self.cancel_algo_ok = cancel_algo_ok
        self.close_calls = []
        self.stop_called = 0

    def position_amt(self, s):
        return self._pos

    def position_mode(self):
        return ONEWAY

    def protective_orders(self, s):
        return []

    def order_terminal(self, o):
        return True

    def cancel_order(self, o):
        return True

    def stop_executor(self, s):
        self.stop_called += 1

    def protective_algo_orders(self, s):
        if self.algo_query_raises:
            raise TimeoutError("openAlgoOrders outage")
        return list(self.algo_leftovers)

    def cancel_algo_order_verified(self, o):
        if not self.cancel_algo_ok:
            return False
        self.algo_leftovers = [x for x in self.algo_leftovers
                               if x.get("algoId") != o.get("algoId")]
        return True

    def close_reduce_only(self, s, q, position_side=None):
        self.close_calls.append(Decimal(str(q)))
        self._pos = Decimal("0")
        return Decimal(str(q))


class TestCloseLeftoverAlgo(unittest.TestCase):
    def test_close_resolves_leftover_algo_sl(self):
        # regression: after the position is flat the leftover algo SL is cancelled
        # + verified, so it can never fire on a future position
        ex = MockExchange(pos="0.3", algo_leftovers=[_stop()])
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM, "EMERGENCY")
        self.assertTrue(r.ok)
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(ex.algo_leftovers, [])

    def test_leftover_unresolvable_fails_safe(self):
        ex = MockExchange(pos="0.3", algo_leftovers=[_stop()], cancel_algo_ok=False)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM, "EMERGENCY")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ALGO_LEFTOVER_UNRESOLVED")
        self.assertTrue(r.critical)

    def test_algo_query_outage_fails_safe(self):
        ex = MockExchange(pos="0.3", algo_query_raises=True)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM, "EMERGENCY")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ALGO_QUERY_FAILED")
        self.assertTrue(r.critical)

    def test_already_flat_with_leftover_still_resolves(self):
        # the F-a emergency-close residue: position flat, algo SL alive
        ex = MockExchange(pos="0", algo_leftovers=[_stop()])
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertTrue(r.ok)
        self.assertEqual(ex.algo_leftovers, [])

    def test_already_flat_leftover_unresolvable_not_closed(self):
        ex = MockExchange(pos="0", algo_leftovers=[_stop()], cancel_algo_ok=False)
        cc = CloseCoordinator(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertFalse(r.ok)
        self.assertEqual(r.action, "ALGO_LEFTOVER_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
