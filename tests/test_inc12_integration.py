"""Increment 12 — concurrency / restart / timeout / UNKNOWN integration.

Drives the REAL call chain (CloseCoordinator -> BinanceHBCloseAdapter ->
BinanceAPI/HBAPI; ensure_sl_coverage; PositionMigrator; PositionReserver) and
mocks ONLY the external HTTP boundary (`_session`) or, for cross-process, a
SQLite-backed shared exchange. Proves:
  * concurrent close on one coordinator -> a SINGLE close POST (needs a lock),
  * cross-process close safety comes from the EXCHANGE (reduceOnly -2022), not a
    Python lock,
  * restart recovery re-queries exchange truth for every handoff state,
  * every timeout/UNKNOWN fails closed: no uncontrolled POST, block new entries,
  * _OPEN_UNPROTECTED is cleared only on reliable exchange-flat confirmation.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import BinanceAPI, HBAPI, ensure_sl_coverage, should_block_new_entries
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState
from position_reservation import PositionReserver

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class FakeExchange:
    """Thread-safe in-memory ONEWAY exchange (the real exchange is atomic). Models
    the reduceOnly -2022 clamp. `fail` maps an endpoint key to a callable/Exception
    to inject timeouts / error dicts."""
    def __init__(self, amt="0.3", dual=False, fill_qty=None, read_delay=0.0,
                 fail=None, algo=None, cancel_terminalizes=True):
        self.amt = Decimal(str(amt))
        self.reported_amt = None
        self.dual = dual
        self.fill_qty = Decimal(str(fill_qty)) if fill_qty is not None else None
        self.read_delay = read_delay
        self.fail = fail or {}
        self.algo = algo or {}
        self.cancel_terminalizes = cancel_terminalizes
        self.open_orders = {}
        self.order_db = []
        self.executors = []
        self.posts = []
        self.stops = []
        self._oid = 5000
        self._lock = threading.Lock()

    def _maybe_fail(self, key):
        f = self.fail.get(key)
        if f is None:
            return None
        if isinstance(f, Exception):
            raise f
        return f  # an error-dict payload

    def handle_get(self, url):
        path = urlparse(url).path
        p = _params(url)
        if path == "/fapi/v2/positionRisk":
            bad = self._maybe_fail("positionRisk")
            if bad is not None:
                return bad
            if self.read_delay:
                import time
                time.sleep(self.read_delay)
            shown = self.reported_amt if self.reported_amt is not None else self.amt
            return [{"symbol": SYM, "positionAmt": str(shown), "entryPrice": "83.08"}]
        if path == "/fapi/v1/positionSide/dual":
            bad = self._maybe_fail("mode")
            return bad if bad is not None else {"dualSidePosition": self.dual}
        if path == "/fapi/v1/openOrders":
            bad = self._maybe_fail("openOrders")
            return bad if bad is not None else list(self.open_orders.get(p.get("symbol"), []))
        if path == "/fapi/v1/order":
            bad = self._maybe_fail("query")
            if bad is not None:
                return bad
            oid, coid = p.get("orderId"), p.get("origClientOrderId")
            for o in self.order_db:
                if (oid and str(o.get("orderId")) == str(oid)) or \
                   (coid and o.get("clientOrderId") == coid):
                    return dict(o)
            return {"code": -2013, "msg": "Order does not exist."}
        if path == "/fapi/v1/openAlgoOrders":
            bad = self._maybe_fail("algo")
            return bad if bad is not None else list(self.algo.get(p.get("symbol"), []))
        return {"code": -1121, "msg": f"unrouted GET {path}"}

    def _place_order(self, p):
        side = p["side"]
        qty = Decimal(p["quantity"])
        reduce_only = str(p.get("reduceOnly", "")).lower() == "true"
        with self._lock:
            cur = self.amt
            signed = -qty if side == "SELL" else qty
            if reduce_only:
                if cur == 0 or (cur > 0 and signed > 0) or (cur < 0 and signed < 0):
                    self.posts.append({"side": side, "reduceOnly": True,
                                       "outcome": "REJECTED_-2022"})
                    return {"code": -2022, "msg": "ReduceOnly Order is rejected."}
                fill = self.fill_qty if self.fill_qty is not None else qty
                new = cur + (-fill if side == "SELL" else fill)
                if (cur > 0 and new < 0) or (cur < 0 and new > 0):
                    new = Decimal("0")
                self.amt = new
            else:
                self.amt = cur + signed
            oid = self._oid
            self._oid += 1
            self.posts.append({"side": side, "reduceOnly": reduce_only,
                               "outcome": "FILLED", "result_amt": str(self.amt)})
        return {"orderId": oid, "status": "FILLED", "symbol": p.get("symbol")}

    def handle_post(self, url, body):
        path = urlparse(url).path
        if path.endswith("/executors/search"):
            if body == {"is_active": True}:
                return {"data": [e for e in self.executors if e.get("status") == "RUNNING"]}
            return {"data": list(self.executors)}
        if "/executors/" in path and path.endswith("/stop"):
            self.stops.append(body)
            return {"status": "ok"}
        if path == "/fapi/v1/order":
            bad = self._maybe_fail("post")
            if bad is not None:
                return bad
            return self._place_order(_params(url))
        return {"code": -1121, "msg": f"unrouted POST {path}"}

    def handle_delete(self, url):
        bad = self._maybe_fail("cancel")
        if bad is not None:
            return bad
        p = _params(url)
        for o in self.order_db:
            if (p.get("orderId") and str(o.get("orderId")) == str(p["orderId"])) or \
               (p.get("origClientOrderId") and o.get("clientOrderId") == p["origClientOrderId"]):
                if self.cancel_terminalizes:
                    o["status"] = "CANCELED"
                return {"orderId": o.get("orderId"), "status": o["status"]}
        return {"code": -2011, "msg": "Unknown order sent."}


class FakeSession:
    def __init__(self, ex):
        self.ex = ex

    def get(self, url, headers=None, params=None, timeout=None):
        return _Resp(self.ex.handle_get(url))

    def post(self, url, headers=None, json=None, auth=None, timeout=None):
        return _Resp(self.ex.handle_post(url, json))

    def delete(self, url, headers=None, timeout=None):
        return _Resp(self.ex.handle_delete(url))


def _coordinator(ex):
    binance = BinanceAPI("k", "s")
    hb = HBAPI("http://hb.local", "u", "p")
    return CloseCoordinator(BinanceHBCloseAdapter(binance, hb))


class IntegBase(unittest.TestCase):
    def wire(self, ex):
        self._orig = auto_trader._session
        auto_trader._session = FakeSession(ex)
        return _coordinator(ex)

    def tearDown(self):
        if getattr(self, "_orig", None) is not None:
            auto_trader._session = self._orig
        auto_trader._OPEN_UNPROTECTED.clear()


# --------------------------- concurrency (threads) ---------------------------
class TestConcurrentClose(IntegBase):
    def test_single_close_baseline(self):
        ex = FakeExchange(amt="0.3")
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertTrue(r.ok)
        self.assertEqual(len([p for p in ex.posts if p["outcome"] == "FILLED"]), 1)

    def test_concurrent_close_single_post(self):
        # Two threads race the SAME coordinator. Exactly one close POST must be sent
        # and the position must never reverse. Needs request_close to be atomic.
        ex = FakeExchange(amt="0.3", read_delay=0.03)
        cc = self.wire(ex)
        barrier = threading.Barrier(2)
        out = []

        def worker():
            barrier.wait()
            out.append(cc.request_close(SYM, "RACE"))
        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(ex.posts), 1, f"more than one close POST: {ex.posts}")
        self.assertGreaterEqual(ex.amt, Decimal("0"))      # never reversed SHORT
        actions = sorted(r.action for r in out)
        self.assertIn("IGNORED_ALREADY_CLOSED", actions)   # one served from idempotency


# --------------------------- restart recovery --------------------------------
class TestRestartRecovery(IntegBase):
    def _fresh_after_restart(self, ex):
        # a restart loses in-memory coordinator state; a NEW coordinator must
        # reconcile from exchange truth.
        return self.wire(ex)

    def test_restart_exchange_flat_no_resend(self):
        # position was closed before restart -> fresh coordinator sees flat.
        ex = FakeExchange(amt="0")
        cc = self._fresh_after_restart(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(ex.posts, [])

    def test_restart_exchange_open_closes_once(self):
        # position still open after restart -> close exactly once, flat-confirmed.
        ex = FakeExchange(amt="0.3")
        cc = self._fresh_after_restart(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(len(ex.posts), 1)

    def test_restart_old_hb_order_mapping_lost_unknown(self):
        # restart: open order exists but no HB executor maps it -> UNKNOWN, no close.
        ex = FakeExchange(amt="0.3")
        ex.open_orders[SYM] = [{"symbol": SYM, "clientOrderId": "X", "orderId": 9,
                                "status": "NEW"}]
        cc = self._fresh_after_restart(ex)  # executors empty -> mapping lost
        r = cc.request_close(SYM, "RECONCILE")
        self.assertFalse(r.ok)
        self.assertTrue(r.critical)
        self.assertEqual(ex.posts, [])

    def test_restart_never_closed_without_flat(self):
        # a non-reduceOnly fill is impossible via the coordinator; partial -> not CLOSED.
        ex = FakeExchange(amt="0.3", fill_qty="0.2")
        cc = self._fresh_after_restart(ex)
        r = cc.request_close(SYM, "RECONCILE")
        self.assertNotEqual(r.state, CloseState.CLOSED)
        self.assertEqual(r.action, "PARTIAL")


# --------------------------- timeout / UNKNOWN matrix ------------------------
class TestTimeoutUnknown(IntegBase):
    def _timeout(self):
        import requests
        return requests.exceptions.Timeout("t")

    def test_positionrisk_timeout_failsafe(self):
        ex = FakeExchange(amt="0.3", fail={"positionRisk": self._timeout()})
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertTrue(r.critical)
        self.assertEqual(ex.posts, [])

    def test_post_timeout_failsafe(self):
        ex = FakeExchange(amt="0.3", fail={"post": self._timeout()})
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(r.state, CloseState.FAILED_SAFE)

    def test_algo_query_timeout_unknown_blocks(self):
        ex = FakeExchange(amt="0.3", fail={"algo": self._timeout()})
        self.wire(ex)
        from auto_trader import _verify_oneway  # noqa: F401
        fb = _FakeBinanceSL({SYM: self._timeout()})
        ensure_sl_coverage(fb)
        self.assertEqual(fb.placed, [])
        self.assertTrue(should_block_new_entries())

    def test_cancel_timeout_unresolved_no_close(self):
        ex = FakeExchange(amt="0.3", fail={"cancel": self._timeout()},
                          cancel_terminalizes=False)
        e = {"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
             "custom_info": {"order_ids": ["C1"]}}
        ex.executors = [e]
        ex.open_orders[SYM] = [{"symbol": SYM, "clientOrderId": "C1", "orderId": 1,
                                "status": "NEW"}]
        ex.order_db = [{"symbol": SYM, "clientOrderId": "C1", "orderId": 1,
                        "status": "NEW"}]
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertEqual(ex.posts, [])

    def test_error_dict_positionrisk_failsafe(self):
        ex = FakeExchange(amt="0.3", fail={"positionRisk": {"code": -1001, "msg": "e"}})
        cc = self.wire(ex)
        r = cc.request_close(SYM, "MANUAL")
        self.assertFalse(r.ok)
        self.assertEqual(ex.posts, [])


# --------------------------- SL / migration integration ----------------------
def _pos(amt, symbol=SYM):
    return {"symbol": symbol, "positionAmt": str(amt), "entryPrice": "83.08"}


def _stop(symbol=SYM, status="NEW", typ="STOP_MARKET", side="SELL",
          position_side="BOTH", reduce_only="true", quantity="0.3", close_position=None):
    o = {"symbol": symbol, "type": typ, "algoStatus": status, "side": side,
         "positionSide": position_side, "reduceOnly": reduce_only, "quantity": quantity}
    if close_position is not None:
        o["closePosition"] = close_position
    return o


class _FakeBinanceSL:
    def __init__(self, algo, positions=None, dual=False):
        self._algo = algo
        self._positions = positions or [_pos("0.3")]
        self._dual = dual
        self.placed = []

    def position_risk(self):
        return list(self._positions)

    def position_mode(self):
        return {"dualSidePosition": self._dual}

    def open_algo_orders_raw(self, sym):
        v = self._algo.get(sym, [])
        if isinstance(v, Exception):
            raise v
        return v

    def filters(self, sym):
        return {"PRICE_FILTER": {"tickSize": "0.01"}}

    def _get(self, path, params=None, signed=False):
        return []

    def place_algo_sl(self, sym, trig, qty, side):
        self.placed.append(sym)
        return {"algoId": 1}


class TestSlMigrationIntegration(IntegBase):
    def _run(self, fb):
        auto_trader._OPEN_UNPROTECTED.clear()
        ensure_sl_coverage(fb)
        return fb

    def test_present_no_second_sl(self):
        fb = self._run(_FakeBinanceSL({SYM: [_stop()]}))
        self.assertEqual(fb.placed, [])

    def test_absent_oneway_places(self):
        fb = self._run(_FakeBinanceSL({SYM: []}))
        self.assertEqual(len(fb.placed), 1)

    def test_unknown_no_sl_critical(self):
        fb = self._run(_FakeBinanceSL({SYM: RuntimeError("x")}))
        self.assertEqual(fb.placed, [])
        self.assertTrue(should_block_new_entries())

    def test_hedge_valid_present(self):
        o = _stop(position_side="LONG", side="SELL", reduce_only="false",
                  quantity="0", close_position="true")
        fb = self._run(_FakeBinanceSL({SYM: [o]}, dual=True))
        self.assertEqual(fb.placed, [])

    def test_hedge_absent_failclosed(self):
        fb = self._run(_FakeBinanceSL({SYM: []}, dual=True))
        self.assertEqual(fb.placed, [])
        self.assertTrue(should_block_new_entries())

    def test_unrelated_algo_not_coverage(self):
        fb = self._run(_FakeBinanceSL({SYM: [_stop(typ="TAKE_PROFIT_MARKET")]}))
        self.assertEqual(len(fb.placed), 1)

    def test_wrong_side_not_present(self):
        fb = self._run(_FakeBinanceSL({SYM: [_stop(side="BUY")]}))
        self.assertEqual(len(fb.placed), 1)

    # RC-B: a stale _OPEN_UNPROTECTED entry for a now-FLAT symbol must be cleared on
    # reliable exchange-flat confirmation, so it does not block new entries forever.
    def test_open_unprotected_cleared_on_flat(self):
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED["OLDSYM"] = -2021   # left over from a closed pos
        fb = _FakeBinanceSL({SYM: [_stop()]}, positions=[_pos("0.3")])  # OLDSYM is flat
        ensure_sl_coverage(fb)
        self.assertNotIn("OLDSYM", auto_trader._OPEN_UNPROTECTED)
        self.assertFalse(should_block_new_entries())

    def test_open_unprotected_kept_when_read_unreliable(self):
        # positionRisk unreliable (not a list) -> do NOT clear (no reliable flat).
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED["OLDSYM"] = -2021

        class _Bad(_FakeBinanceSL):
            def position_risk(self):
                return {"code": -1, "msg": "unavailable"}
        ensure_sl_coverage(_Bad({SYM: []}))
        self.assertIn("OLDSYM", auto_trader._OPEN_UNPROTECTED)  # kept: no reliable flat


# --------------------------- MAX_OPEN_POSITIONS ------------------------------
class TestMaxOpenIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "resv.db")
        self.r = PositionReserver(self.db, max_positions=2)
        self.r.init_schema()

    def test_two_slots_then_cap(self):
        self.assertTrue(self.r.reserve("A", []).ok)
        self.assertTrue(self.r.reserve("B", []).ok)
        third = self.r.reserve("C", [])
        self.assertFalse(third.ok)
        self.assertEqual(third.reason, "cap_reached")

    def test_parallel_threads_never_exceed_two(self):
        results = []
        lock = threading.Lock()

        def worker(sym):
            res = self.r.reserve(sym, [])
            with lock:
                results.append(res.ok)
        ts = [threading.Thread(target=worker, args=(f"S{i}",)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sum(1 for ok in results if ok), 2)  # never a 3rd

    def test_unknown_reservation_not_released_until_reconcile(self):
        res = self.r.reserve("A", [])
        self.assertTrue(res.ok)
        # UNKNOWN create outcome -> caller does NOT release; slot stays held.
        self.assertEqual(len(self.r.active_reservations()), 1)
        # restart reconcile against exchange truth: A never opened + stale -> released.
        rep = self.r.reconcile([], ttl_seconds=0.0)
        self.assertEqual(len(rep.released), 1)
        self.assertEqual(self.r.active_reservations(), [])

    def test_restart_reconcile_promotes_real_position(self):
        self.r.reserve("A", [])
        rep = self.r.reconcile(["A"])   # A now open on exchange
        self.assertEqual(len(rep.promoted), 1)
        self.assertEqual(self.r.active_reservations(), [])


# --------------------------- cross-process close -----------------------------
# Shared SQLite "exchange" so two PROCESSES hit one atomic reduceOnly boundary.
def _sqlite_exchange_init(db):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS pos (symbol TEXT PRIMARY KEY, amt TEXT)")
    conn.execute("INSERT OR REPLACE INTO pos VALUES (?, ?)", (SYM, "0.3"))
    conn.commit()
    conn.close()


def _xproc_close_worker(db, result_path, idx):
    """Runs in a separate PROCESS: real CloseCoordinator against a SQLite-backed
    exchange whose POST applies an ATOMIC reduceOnly clamp (BEGIN IMMEDIATE)."""
    import sqlite3 as _sq
    from decimal import Decimal as _D
    from urllib.parse import urlparse as _up, parse_qs as _pq
    import auto_trader as _at
    from auto_trader import BinanceAPI as _B, HBAPI as _H
    from close_adapter import BinanceHBCloseAdapter as _A
    from close_coordinator import CloseCoordinator as _C

    class _R:
        def __init__(self, p):
            self._p = p

        def json(self):
            return self._p

    def _pr(url):
        return {k: v[0] for k, v in _pq(_up(url).query).items()}

    class _Sess:
        def get(self, url, headers=None, params=None, timeout=None):
            path = _up(url).path
            if path == "/fapi/v2/positionRisk":
                c = _sq.connect(db, timeout=30)
                row = c.execute("SELECT amt FROM pos WHERE symbol=?", (SYM,)).fetchone()
                c.close()
                amt = row[0] if row else "0"
                return _R([{"symbol": SYM, "positionAmt": amt, "entryPrice": "83.08"}])
            if path == "/fapi/v1/positionSide/dual":
                return _R({"dualSidePosition": False})
            if path == "/fapi/v1/openOrders":
                return _R([])
            return _R({"code": -1121})

        def post(self, url, headers=None, json=None, auth=None, timeout=None):
            path = _up(url).path
            if path.endswith("/executors/search"):
                return _R({"data": []})
            if path == "/fapi/v1/order":
                p = _pr(url)
                qty = _D(p["quantity"])
                reduce_only = str(p.get("reduceOnly", "")).lower() == "true"
                c = _sq.connect(db, timeout=30, isolation_level=None)
                try:
                    c.execute("PRAGMA busy_timeout=30000")
                    c.execute("BEGIN IMMEDIATE")
                    row = c.execute("SELECT amt FROM pos WHERE symbol=?", (SYM,)).fetchone()
                    cur = _D(row[0])
                    signed = -qty if p["side"] == "SELL" else qty
                    if reduce_only and (cur == 0 or (cur > 0 and signed > 0)
                                        or (cur < 0 and signed < 0)):
                        c.execute("COMMIT")
                        return _R({"code": -2022, "msg": "ReduceOnly rejected."})
                    new = cur + signed
                    if reduce_only and ((cur > 0 and new < 0) or (cur < 0 and new > 0)):
                        new = _D("0")
                    c.execute("UPDATE pos SET amt=? WHERE symbol=?", (str(new), SYM))
                    c.execute("COMMIT")
                finally:
                    c.close()
                return _R({"orderId": 1, "status": "FILLED"})
            return _R({"code": -1121})

        def delete(self, url, headers=None, timeout=None):
            return _R({"status": "CANCELED"})

    _at._session = _Sess()
    cc = _C(_A(_B("k", "s"), _H("http://hb", "u", "p")))
    res = cc.request_close(SYM, f"P{idx}")
    with open(result_path, "a") as fh:
        fh.write(f"{idx}:{res.action}:{res.ok}\n")


class TestCrossProcessClose(unittest.TestCase):
    def test_two_processes_single_effective_close_no_reverse(self):
        import multiprocessing as mp
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "exch.db")
        res_path = os.path.join(tmp, "out.txt")
        _sqlite_exchange_init(db)
        ctx = mp.get_context("spawn")
        ps = [ctx.Process(target=_xproc_close_worker, args=(db, res_path, i))
              for i in range(2)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        # final exchange position must be flat and NEVER negative (no reverse SHORT)
        conn = sqlite3.connect(db)
        amt = Decimal(conn.execute("SELECT amt FROM pos WHERE symbol=?", (SYM,)).fetchone()[0])
        conn.close()
        self.assertEqual(amt, Decimal("0"), "cross-process close left a non-flat/rev pos")
        self.assertGreaterEqual(amt, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
