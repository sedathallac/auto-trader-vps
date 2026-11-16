"""FAZ15 — C6 slot/rezervasyon/pozisyon bütünlüğü ve pending lifecycle testleri.

42 test metodu, 0 SKIP. Tüm DB'ler geçici dizinde; canlı state'e yazılmaz.
"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from position_reservation import PositionReserver


# ---------------------------------------------------------------- fakes
class FakeBinance:
    def __init__(self, positions=None, raise_on_risk=False, not_list=False,
                 order=None, raise_on_order=False):
        self._positions = positions if positions is not None else []
        self._raise = raise_on_risk
        self._not_list = not_list
        self._order = order
        self._raise_order = raise_on_order
        self.calls = []

    def position_risk(self):
        self.calls.append("position_risk")
        if self._raise:
            raise RuntimeError("positionRisk down")
        if self._not_list:
            return {"code": -1, "msg": "err"}
        return list(self._positions)

    def filters(self, symbol):
        return {"PRICE_FILTER": {"tickSize": "0.01"},
                "LOT_SIZE": {"stepSize": "0.1"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        self.calls.append("query_order")
        if self._raise_order:
            raise RuntimeError("query down")
        return self._order


class FakeHB:
    def __init__(self, executor=None, raise_on_get=False, executors=None,
                 raise_on_active=False, stop_raises=False):
        self._ex = executor
        self._raise_get = raise_on_get
        self._executors = executors or []
        self._raise_active = raise_on_active
        self._stop_raises = stop_raises
        self.stopped = []

    def get_executor(self, eid):
        if self._raise_get:
            raise RuntimeError("hb down")
        return self._ex

    def active_executors(self):
        if self._raise_active:
            raise RuntimeError("hb down")
        return list(self._executors)

    def stop_executor(self, eid, keep_position=True):
        if self._stop_raises:
            raise RuntimeError("stop failed")
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


def pos(symbol, amt):
    return {"symbol": symbol, "positionAmt": str(amt), "entryPrice": "1.0"}


class _Resp:
    """Minimal HTTP response for the /executors/search fake."""

    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


class FakeEntryBinance:
    """Boundary fake for the REAL open_coin entry flow (no network)."""

    def __init__(self):
        self.order = None
        self.open = []
        self.order_queries = []  # her query_order çağrısının AYRI parametreleri

    def position_risk(self):
        return []

    def filters(self, symbol):
        return {"PRICE_FILTER": {"tickSize": "0.01"},
                "LOT_SIZE": {"stepSize": "0.1"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def account_snapshot(self):
        return auto_trader.AccountSnapshot(equity=1000.0, available_balance=1000.0,
                                           can_trade=True, raw={})

    def set_leverage(self, symbol, lev):
        return {"leverage": lev}

    def order_book(self, symbol, limit=20):
        return {"bids": [["3.195", "5000"]], "asks": [["3.196", "5000"]]}

    def open_orders(self, symbol):
        return list(self.open)

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        # orderId ve origClientOrderId AYNI parametre değildir; hangi değerinin
        # hangisine gittiği kayıt altında (asla takas edilemezler).
        self.order_queries.append({"order_id": order_id,
                                   "orig_client_order_id": orig_client_order_id})
        return self.order


class FakeEntryHB:
    """HB boundary fake — DOĞRULANMIŞ sözleşme (kurulu imaj kaynakları):
    custom_info.order_ids, aktif TrackedOrder'larin CONNECTOR-ÜRETIMI CLIENT
    emir kimliklerini taşır (exchange_py_base.buy()/sell() client id döndürür;
    exchange orderId DEĞİL). create_executor yalnız executor_id döndürür;
    get_executor tekil kaydı döndürür; stop -> TERMINATED."""

    def __init__(self, order_ids=(), status="NOT_STARTED", filled=0.0):
        self.order_ids = list(order_ids)
        self.status = status
        self.filled = filled
        self.stopped = []

    def active_executors(self):
        return []

    def create_executor(self, symbol, amount, lev, sl, tp, tl, side=1):
        # CreateExecutorResponse: emir kimliği YOK — yalnız executor kimliği.
        return {"executor_id": "E1"}

    def get_executor(self, eid):
        return {"executor_id": eid, "status": self.status,
                "filled_amount_quote": self.filled, "error_count": 0,
                "custom_info": {"order_ids": list(self.order_ids)}}

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        self.status = "TERMINATED"
        return {"status": "ok"}


class FakeRisk:
    health_ok = True
    migration_required = False
    is_halted = False
    halt_reason = ""
    daily_trades = 0
    consecutive_losses = 0
    current_drawdown = 0.0
    margin_usage = 0.0
    cfg = {"max_leverage": 20}

    def approve_trade(self, *a, **k):
        return 200.0, "ok"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self._saved = (auto_trader.HB_OFFLINE, dict(auto_trader._PENDING_FILLS),
                       auto_trader.PENDING_FILLS_JSON,
                       list(auto_trader._PENDING_LOADED))
        auto_trader.HB_OFFLINE = False
        auto_trader._PENDING_FILLS = {}
        auto_trader.PENDING_FILLS_JSON = auto_trader.Path(
            os.path.join(self.tmp, "pending.json"))
        auto_trader._PENDING_LOADED[0] = True

    def tearDown(self):
        (auto_trader.HB_OFFLINE, fills, auto_trader.PENDING_FILLS_JSON,
         loaded) = self._saved
        auto_trader._PENDING_FILLS = fills
        auto_trader._PENDING_LOADED[0] = loaded[0]

    def reserver(self, cap=5):
        r = PositionReserver(self.db, cap)
        r.init_schema()
        return r


# ------------------------------------------------- 9: _occupied_slots
class TestOccupiedSlots(Base):
    def test_exchange_positions_counted(self):
        slots, _ = auto_trader._occupied_slots(
            FakeBinance([pos("NEARUSDT", -62), pos("BNBUSDT", -0.33)]), FakeHB())
        self.assertEqual(slots, {"NEARUSDT", "BNBUSDT"})

    def test_pending_fills_counted(self):
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "WLDUSDT"}}
        slots, _ = auto_trader._occupied_slots(FakeBinance([]), FakeHB())
        self.assertIn("WLDUSDT", slots)

    def test_ghost_executor_not_counted(self):
        hb = FakeHB(executors=[{"status": "RUNNING", "trading_pair": "ENA-USDT"}])
        slots, pairs = auto_trader._occupied_slots(FakeBinance([]), hb)
        self.assertEqual(slots, set())
        self.assertEqual(pairs, {"ENA-USDT"})

    def test_position_risk_exception_returns_none(self):
        slots, pairs = auto_trader._occupied_slots(
            FakeBinance(raise_on_risk=True), FakeHB())
        self.assertIsNone(slots)
        self.assertIsNone(pairs)

    def test_position_risk_not_list_returns_none(self):
        slots, _ = auto_trader._occupied_slots(FakeBinance(not_list=True), FakeHB())
        self.assertIsNone(slots)

    def test_hb_executor_list_failure_returns_none(self):
        slots, _ = auto_trader._occupied_slots(
            FakeBinance([]), FakeHB(raise_on_active=True))
        self.assertIsNone(slots)

    def test_invalid_pending_ctx_returns_none(self):
        auto_trader._PENDING_FILLS = {"E1": "not-a-dict"}
        slots, _ = auto_trader._occupied_slots(FakeBinance([]), FakeHB())
        self.assertIsNone(slots)

    def test_blank_pending_symbol_returns_none(self):
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "   "}}
        slots, _ = auto_trader._occupied_slots(FakeBinance([]), FakeHB())
        self.assertIsNone(slots)

    def test_flat_positions_not_counted(self):
        slots, _ = auto_trader._occupied_slots(
            FakeBinance([pos("NEARUSDT", 0)]), FakeHB())
        self.assertEqual(slots, set())


# --------------------------------------- 8: _exchange_open_symbols
class TestExchangeOpenSymbols(Base):
    def test_returns_open_symbols(self):
        got = auto_trader._exchange_open_symbols(
            FakeBinance([pos("NEARUSDT", -62), pos("ENA-USDT", 5)]))
        self.assertEqual(got, {"NEARUSDT", "ENAUSDT"})

    def test_flat_excluded(self):
        got = auto_trader._exchange_open_symbols(FakeBinance([pos("NEARUSDT", 0)]))
        self.assertEqual(got, set())

    def test_exception_returns_none(self):
        self.assertIsNone(auto_trader._exchange_open_symbols(
            FakeBinance(raise_on_risk=True)))

    def test_non_list_returns_none(self):
        self.assertIsNone(auto_trader._exchange_open_symbols(
            FakeBinance(not_list=True)))

    def test_non_dict_element_returns_none(self):
        self.assertIsNone(auto_trader._exchange_open_symbols(
            FakeBinance([pos("NEARUSDT", 1), "junk"])))

    def test_missing_position_amt_returns_none(self):
        self.assertIsNone(auto_trader._exchange_open_symbols(
            FakeBinance([{"symbol": "NEARUSDT"}])))

    def test_non_numeric_position_amt_returns_none(self):
        self.assertIsNone(auto_trader._exchange_open_symbols(
            FakeBinance([{"symbol": "NEARUSDT", "positionAmt": "abc"}])))

    def test_nan_position_amt_returns_none(self):
        self.assertIsNone(auto_trader._exchange_open_symbols(
            FakeBinance([{"symbol": "NEARUSDT", "positionAmt": "nan"}])))


# ------------------------------------- 4: reconcile keep/finalizing
class TestReconcileKeepSymbols(Base):
    def _age(self, rid, seconds):
        import sqlite3
        conn = sqlite3.connect(self.db)
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                            time.gmtime(time.time() - seconds))
        conn.execute("UPDATE position_reservations SET created_at=? WHERE id=?",
                     (old, rid))
        conn.commit()
        conn.close()

    def test_keep_symbols_preserved_past_ttl(self):
        r = self.reserver()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        self._age(rid, 9999)
        rep = r.reconcile(set(), ttl_seconds=120, keep_symbols={"NEARUSDT"})
        self.assertEqual(rep.released, [])
        self.assertEqual([x["symbol"] for x in rep.kept], ["NEARUSDT"])
        self.assertEqual(len(r.active_reservations()), 1)

    def test_finalizing_symbols_preserved_past_ttl(self):
        r = self.reserver()
        rid = r.reserve("WLDUSDT", set()).reservation_id
        self._age(rid, 9999)
        rep = r.reconcile(set(), ttl_seconds=120, finalizing_symbols={"WLDUSDT"})
        self.assertEqual(rep.released, [])
        self.assertEqual(len(r.active_reservations()), 1)

    def test_confirmed_not_ttl_deleted(self):
        r = self.reserver()
        rid = r.reserve("BNBUSDT", set()).reservation_id
        r.confirm(rid)
        self._age(rid, 9999)
        rep = r.reconcile(set(), ttl_seconds=120)
        self.assertEqual(rep.released, [])
        self.assertEqual(len(r.active_reservations()), 1)

    def test_pending_ttl_not_auto_released(self):
        r = self.reserver()
        rid = r.reserve("ENAUSDT", set()).reservation_id
        self._age(rid, 9999)
        rep = r.reconcile(set(), ttl_seconds=120)
        self.assertEqual(rep.released, [])
        self.assertEqual(len(rep.stale), 1)
        self.assertEqual(len(r.active_reservations()), 1)


# ------------------------------------------------- 3: concurrency
class TestConcurrent(Base):
    def test_cap_two_enforced(self):
        r = self.reserver(cap=2)
        self.assertTrue(r.reserve("AAAUSDT", set()).ok)
        self.assertTrue(r.reserve("BBBUSDT", set()).ok)
        third = r.reserve("CCCUSDT", set())
        self.assertFalse(third.ok)
        self.assertEqual(third.reason, "cap_reached")

    def test_duplicate_symbol_rejected(self):
        r = self.reserver(cap=2)
        self.assertTrue(r.reserve("AAAUSDT", set()).ok)
        dup = r.reserve("AAAUSDT", set())
        self.assertFalse(dup.ok)
        self.assertEqual(dup.reason, "duplicate_symbol")

    def test_two_reservers_share_cap(self):
        a, b = self.reserver(cap=2), self.reserver(cap=2)
        self.assertTrue(a.reserve("AAAUSDT", set()).ok)
        self.assertTrue(b.reserve("BBBUSDT", set()).ok)
        self.assertFalse(a.reserve("CCCUSDT", set()).ok)
        self.assertFalse(b.reserve("DDDUSDT", set()).ok)


# --------------------------------------------- 2: C6 gate source
class TestC6GatesSource(Base):
    def _src(self, name):
        import inspect
        return inspect.getsource(getattr(auto_trader, name))

    def test_process_candidates_uses_occupied_slots(self):
        src = self._src("process_candidates")
        self.assertIn("_occupied_slots", src)
        self.assertIn("_slots is None", src)


# ------------------------------------------- 6: protect_then_gate
class TestProtectThenGate(Base):
    def setUp(self):
        super().setUp()
        self._orig = (auto_trader.ensure_sl_coverage, auto_trader._verify_oneway,
                      auto_trader.should_block_new_entries)
        auto_trader.ensure_sl_coverage = lambda b: None
        auto_trader._verify_oneway = lambda b: (True, "ONEWAY")
        auto_trader.should_block_new_entries = lambda: False

    def tearDown(self):
        (auto_trader.ensure_sl_coverage, auto_trader._verify_oneway,
         auto_trader.should_block_new_entries) = self._orig
        super().tearDown()

    def test_reconcile_exception_blocks_entry(self):
        class Boom(PositionReserver):
            def reconcile(self, *a, **k):
                raise RuntimeError("db locked")
        r = Boom(self.db, 5)
        r.init_schema()
        allow, info = auto_trader.protect_then_gate(FakeBinance([]), r, False, False)
        self.assertFalse(allow)
        self.assertFalse(info["reconcile_ok"])

    def test_exchange_open_none_blocks_entry(self):
        allow, info = auto_trader.protect_then_gate(
            FakeBinance(raise_on_risk=True), self.reserver(), False, False)
        self.assertFalse(allow)
        self.assertFalse(info["reconcile_ok"])

    def test_keep_symbols_passed_to_reconcile(self):
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "NEARUSDT"}}
        seen = {}

        class Rec(PositionReserver):
            def reconcile(self, exch, ttl_seconds=120.0, keep_symbols=None,
                          finalizing_symbols=None):
                seen["keep"] = set(keep_symbols or ())
                seen["fin"] = set(finalizing_symbols or ())
                return super().reconcile(exch, ttl_seconds, keep_symbols,
                                         finalizing_symbols)
        r = Rec(self.db, 5)
        r.init_schema()
        auto_trader.protect_then_gate(FakeBinance([]), r, False, False)
        self.assertEqual(seen["keep"], {"NEARUSDT"})

    def test_finalizing_symbols_passed_to_reconcile(self):
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "WLDUSDT",
                                             "finalization_complete": True}}
        seen = {}

        class Rec(PositionReserver):
            def reconcile(self, exch, ttl_seconds=120.0, keep_symbols=None,
                          finalizing_symbols=None):
                seen["fin"] = set(finalizing_symbols or ())
                return super().reconcile(exch, ttl_seconds, keep_symbols,
                                         finalizing_symbols)
        r = Rec(self.db, 5)
        r.init_schema()
        auto_trader.protect_then_gate(FakeBinance([]), r, False, False)
        self.assertEqual(seen["fin"], {"WLDUSDT"})

    def test_mode_unknown_blocks_entry(self):
        auto_trader._verify_oneway = lambda b: (False, "UNKNOWN")
        allow, info = auto_trader.protect_then_gate(
            FakeBinance([]), self.reserver(), False, False)
        self.assertFalse(allow)
        self.assertEqual(info["mode"], "UNKNOWN")

    def test_protection_runs_even_on_drift(self):
        called = []
        auto_trader.ensure_sl_coverage = lambda b: called.append(1)
        allow, _ = auto_trader.protect_then_gate(
            FakeBinance([]), self.reserver(), True, True)
        self.assertEqual(called, [1])
        self.assertFalse(allow)


# --------------------------------------- 3: normalization consistency
class TestNormalizationConsistency(Base):
    def test_strips_and_uppercases(self):
        self.assertEqual(auto_trader._norm_symbol("  ena-usdt  "), "ENAUSDT")

    def test_separators_removed(self):
        self.assertEqual(auto_trader._norm_symbol("ENA_USDT"), "ENAUSDT")
        self.assertEqual(auto_trader._norm_symbol("ENA-USDT"), "ENAUSDT")

    def test_empty_invalid(self):
        self.assertEqual(auto_trader._norm_symbol(None), "")
        self.assertEqual(auto_trader._norm_symbol("   "), "")


# --------------------------------- 7: pending finalize lifecycle
class TestPendingFinalizeLifecycle(Base):
    def setUp(self):
        super().setUp()
        self.order = []
        self._orig_fin = auto_trader._finalize_open
        self._orig_drop = auto_trader._drop_pending_fill
        self.fin_result = True
        self.fin_raises = False

        def _fin(*a, **k):
            self.order.append("finalize")
            if self.fin_raises:
                raise RuntimeError("sl placement failed")
            return self.fin_result
        auto_trader._finalize_open = _fin

        def _drop(eid):
            self.order.append("drop")
            return self._orig_drop(eid)
        auto_trader._drop_pending_fill = _drop

    def tearDown(self):
        auto_trader._finalize_open = self._orig_fin
        auto_trader._drop_pending_fill = self._orig_drop
        super().tearDown()

    def _ctx(self, **kw):
        base = {"symbol": "NEARUSDT", "side": 2, "amount": 62.0, "sl_pct": 0.0108,
                "tp_pct": 0.0215, "price": 3.196, "deadline": time.time() + 300,
                "res_id": None}
        base.update(kw)
        return base

    def _reserver_recording(self):
        order = self.order

        class Rec(PositionReserver):
            def confirm(self, rid):
                order.append("confirm")
                return super().confirm(rid)
        r = Rec(self.db, 5)
        r.init_schema()
        return r

    def test_finalize_true_persist_confirm_drop_order(self):
        r = self._reserver_recording()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(res_id=rid)}
        hb = FakeHB(executor={"filled_amount_quote": 200.0, "error_count": 0})
        out = auto_trader._process_pending_fills(FakeBinance([]), hb, None, None, r)
        self.assertEqual(self.order, ["finalize", "confirm", "drop"])
        self.assertEqual(out["filled"], 1)
        self.assertNotIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(r.active_reservations()[0]["state"], "CONFIRMED")

    def test_finalize_exception_keeps_pending_and_reservation(self):
        r = self._reserver_recording()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(res_id=rid)}
        self.fin_raises = True
        hb = FakeHB(executor={"filled_amount_quote": 200.0, "error_count": 0})
        out = auto_trader._process_pending_fills(
            FakeBinance([pos("NEARUSDT", -62)]), hb, None, None, r)
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        self.assertFalse(auto_trader._PENDING_FILLS["E1"]["finalization_complete"])
        self.assertEqual(len(r.active_reservations()), 1)
        self.assertEqual(out["failed"], 1)
        self.assertNotIn("drop", self.order)

    def test_finalize_false_position_open_keeps(self):
        r = self._reserver_recording()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(res_id=rid)}
        self.fin_result = False
        hb = FakeHB(executor={"filled_amount_quote": 200.0, "error_count": 0})
        out = auto_trader._process_pending_fills(
            FakeBinance([pos("NEARUSDT", -62)]), hb, None, None, r)
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(len(r.active_reservations()), 1)
        self.assertEqual(out["waiting"], 1)

    def test_finalize_false_flat_keeps_reservation(self):
        r = self._reserver_recording()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(res_id=rid)}
        self.fin_result = False
        hb = FakeHB(executor={"filled_amount_quote": 200.0, "error_count": 0})
        out = auto_trader._process_pending_fills(FakeBinance([]), hb, None, None, r)
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(len(r.active_reservations()), 1)
        self.assertEqual(out["waiting"], 1)

    def test_confirm_failure_retries_confirm_only(self):
        class Flaky(PositionReserver):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fail = True

            def confirm(self, rid):
                self.order = getattr(self, "order", [])
                if self.fail:
                    raise RuntimeError("db locked")
                return super().confirm(rid)
        r = Flaky(self.db, 5)
        r.init_schema()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(res_id=rid)}
        hb = FakeHB(executor={"filled_amount_quote": 200.0, "error_count": 0})
        _orig_backoff = auto_trader.CONFIRM_RETRY_BACKOFF_S
        auto_trader.CONFIRM_RETRY_BACKOFF_S = 0.0
        try:
            out1 = auto_trader._process_pending_fills(FakeBinance([]), hb, None, None, r)
            self.assertIn("E1", auto_trader._PENDING_FILLS)
            self.assertTrue(auto_trader._PENDING_FILLS["E1"]["finalization_complete"])
            self.assertEqual(out1["waiting"], 1)
            self.assertEqual(self.order.count("finalize"), 1)

            # protect_then_gate reconciliation runs after pending processing.
            # An exchange position must NOT delete a reservation while its
            # finalization is complete but confirm is still awaiting retry.
            rep = r.reconcile(
                {"NEARUSDT"}, finalizing_symbols={"NEARUSDT"})
            self.assertEqual(rep.promoted, [],
                             "pending confirm rezervasyonu reconcile tarafından silindi")
            self.assertEqual(len(r.active_reservations()), 1,
                             "confirm retry öncesi rezervasyon korunmalı")

            r.fail = False
            out2 = auto_trader._process_pending_fills(FakeBinance([]), hb, None, None, r)
            self.assertEqual(self.order.count("finalize"), 1,
                             "finalizer tekrar çalışmamalı")
            self.assertNotIn("E1", auto_trader._PENDING_FILLS)
            self.assertEqual(out2["waiting"], 0)
        finally:
            auto_trader.CONFIRM_RETRY_BACKOFF_S = _orig_backoff

    def test_executor_missing_queries_exchange_first(self):
        r = self._reserver_recording()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(res_id=rid)}
        b = FakeBinance([pos("NEARUSDT", -62)])
        out = auto_trader._process_pending_fills(b, FakeHB(executor=None),
                                                 None, None, r)
        self.assertEqual(b.calls[0], "position_risk")
        self.assertEqual(out["adopted"], 1)
        self.assertEqual(self.order[0], "finalize")


# ------------------------- 1: active_executors cursor pagination
class TestActiveExecutorsPagination(Base):
    """Hummingbot API sözleşmesi (kurulu imaj kaynaklarında doğrulandı:
    routers/executors.py:78-157, models/executors.py:317-343, services/
    executor_service.py _paginated): POST /executors/search gövdesi
    {limit (vars. 100), cursor, status, ...listefiltreleri}; yanıt
    {data, pagination:{limit, has_more, next_cursor, total_count}}.
    `offset` ve `is_active` FİLTRE ALANI YOKTUR — eski gövde {is_active:True}
    hiç filtrelemez, ilk 100 satırı tüm liste sanırdı. cursor =
    "{created_at}:{executor_id}", azalan sıralama; kayıp cursor sunucuda
    başa döner (istemci tekrar yakalamalı)."""

    ROWS_150 = [{"executor_id": f"E{i:03d}", "status": "RUNNING",
                 "created_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"}
                for i in range(150)]

    def _faithful_server(self, rows, calls, dup_page2=False):
        """Status-faithful /executors/search fake (sunucu semantiği)."""
        def _post(url, json=None, auth=None, timeout=None):
            body = dict(json or {})
            calls.append(body)
            limit = int(body.get("limit") or 100)
            cursor = body.get("cursor")
            status_rows = [r for r in rows if r["status"] == body.get("status")]
            ordered = sorted(status_rows, key=lambda r: (r["created_at"],
                                                         f"{r['created_at']}:{r['executor_id']}"),
                             reverse=True)
            start = 0
            if cursor:
                for i, r in enumerate(ordered):
                    if f"{r['created_at']}:{r['executor_id']}" == cursor:
                        start = i + 1
                        break
            page = ordered[start:start + limit]
            has_more = start + limit < len(ordered)
            next_cursor = (f"{page[-1]['created_at']}:{page[-1]['executor_id']}"
                           if page and has_more else None)
            data = [dict(r) for r in page]
            if dup_page2 and cursor:
                # memory+DB merge sunucuda mükemmel olmayabilir: aynı satırı
                # ikinci sayfaya bir kez daha sızdır (dedupe zorunlu mu?).
                data.insert(0, dict(page[0]))
            return _Resp({"data": data,
                          "pagination": {"limit": limit, "has_more": has_more,
                                         "next_cursor": next_cursor,
                                         "total_count": len(ordered)}})
        return _post

    def test_collects_all_pages_via_cursor(self):
        rows = (list(self.ROWS_150)
                + [{"executor_id": f"N{i}", "status": "NOT_STARTED",
                    "created_at": "2026-01-02T00:00:00"} for i in range(5)]
                + [{"executor_id": f"S{i}", "status": "SHUTTING_DOWN",
                    "created_at": "2026-01-02T00:00:00"} for i in range(3)]
                + [{"executor_id": f"T{i}", "status": "TERMINATED",
                    "created_at": "2026-01-01T00:00:00"} for i in range(40)])
        calls = []
        orig = auto_trader._session.post
        auto_trader._session.post = self._faithful_server(rows, calls, dup_page2=True)
        self.addCleanup(setattr, auto_trader._session, "post", orig)
        got = auto_trader.HBAPI("http://hb", "u", "p").active_executors()
        expect = ({"E%03d" % i for i in range(150)}
                  | {f"N{i}" for i in range(5)} | {f"S{i}" for i in range(3)})
        got_ids = [e["executor_id"] for e in got]
        self.assertEqual(sorted(got_ids), sorted(expect),
                         "3 aktif status'un TAMAMI, eksiksiz ve dupesiz toplanmalı")
        self.assertEqual(len(got_ids), len(set(got_ids)), "executor_id dupesiz olmalı")
        # Terminated geçmişi taranmaz ve sonuçta asla görünmez:
        self.assertNotIn("TERMINATED", {c.get("status") for c in calls})
        self.assertTrue(all(c.get("status") in auto_trader._ACTIVE_EXECUTOR_STATUSES
                            for c in calls))
        self.assertEqual({c.get("status") for c in calls},
                         set(auto_trader._ACTIVE_EXECUTOR_STATUSES),
                         "her status için BAĞIMSIZ cursor zinciri kurulmalı")
        # Her istekte sınır ve imzalı-filtre yokluğu sözleşmeye uygun:
        self.assertTrue(all(c.get("limit") == 100 for c in calls))
        for c in calls:
            self.assertNotIn("is_active", c, "sözleşmede is_active filtresi YOK")
            self.assertNotIn("offset", c, "sözleşmede offset yok")
        # Zincir başlangıçları: status başına tam bir cursorsuz istek (3);
        # tek devam isteği beklenen imleç değeriyle (sayfa sonu satırı):
        no_cur = [c for c in calls if not c.get("cursor")]
        with_cur = [c for c in calls if c.get("cursor")]
        self.assertEqual(len(no_cur), 3)
        self.assertEqual(len(with_cur), 1)
        self.assertEqual(with_cur[0]["cursor"], "2026-01-01T00:00:50:E050",
                         "next_cursor = ilk sayfanın son satırının imleci")

        # FAZ15 Stage2b (aynı testin 2. fazı): herhangi bir zincir adımı
        # sözleşmeyi ihlal ederse active_executors HİÇBİR liste döndürmez
        # (kısmi liste yok): HBContractError fırlatılır ve çağıranlar zaten
        # istisnada fail-closed davranır.
        good_row = {"executor_id": "E1", "status": "RUNNING",
                    "created_at": "2026-01-01T00:00:00"}
        page = lambda more, nc: {"data": [dict(good_row)],
                                 "pagination": {"limit": 100, "has_more": more,
                                                "next_cursor": nc, "total_count": 1}}

        class _Boom(_Resp):
            def json(self):
                raise ValueError("corrupt json")

        def _counter_pages():
            n = [0]

            def _post(url, json=None, auth=None, timeout=None):
                n[0] += 1
                return _Resp({"data": [dict(good_row, executor_id=f"E{n[0]}")],
                              "pagination": {"limit": 100, "has_more": True,
                                             "next_cursor": f"c{n[0]}-x",
                                             "total_count": 9999}})
            return _post

        cases = {
            "http_500": lambda url, json=None, auth=None, timeout=None:
                _Resp({"detail": "boom"}, status=500),
            "has_more_without_next_cursor": lambda url, json=None, auth=None, timeout=None:
                _Resp(page(True, None)),
            "repeated_cursor": lambda url, json=None, auth=None, timeout=None:
                _Resp(page(True, "SAME")),
            "has_more_false_but_cursor": lambda url, json=None, auth=None, timeout=None:
                _Resp(page(False, "X")),
            "missing_pagination": lambda url, json=None, auth=None, timeout=None:
                _Resp({"data": [dict(good_row)]}),
            "non_object_body": lambda url, json=None, auth=None, timeout=None:
                _Resp([good_row]),
            "row_without_executor_id": lambda url, json=None, auth=None, timeout=None:
                _Resp({"data": [{"status": "RUNNING"}],
                       "pagination": {"limit": 100, "has_more": False,
                                      "next_cursor": None, "total_count": 1}}),
            "json_parse_error": lambda url, json=None, auth=None, timeout=None:
                _Boom({}),
            "page_guard": _counter_pages(),
        }
        orig = auto_trader._session.post
        self.addCleanup(setattr, auto_trader._session, "post", orig)
        for name, fake in cases.items():
            with self.subTest(case=name):
                auto_trader._session.post = fake
                hb = auto_trader.HBAPI("http://hb", "u", "p")
                with self.assertRaises(auto_trader.HBContractError):
                    hb.active_executors()


# --------------------- 1: pending entry lifecycle (real open_coin)
class TestPendingEntryLifecycle(Base):
    """Gerçek open_coin akışı: entry CLIENT kimlikleri (custom_info.order_ids,
    connector-üretimi — exchange orderId DEĞİL) pending history'ye taşınır;
    retry kimlikleri eklenir, eski kimlikler ASLA silinmez; close/TP kimliği
    entry history'sine giremez; rezervasyon yalnız DOĞRULANMIŞ terminal
    (tüm kimlikler terminal + executedQty=0 + açık emir yok + flat + executor
    TERMINATED) durumunda bırakılır; UNKNOWN'da KORUNUR."""

    def setUp(self):
        super().setUp()
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2
        names = ("check_liquidity", "check_correlation_limit", "decide_side",
                 "calculate_atr", "calculate_position_size", "_live_cfg")
        self._orig = {n: getattr(auto_trader, n) for n in names}
        auto_trader.check_liquidity = lambda b, s: (True, "ok")
        auto_trader.check_correlation_limit = lambda s, b: True
        auto_trader.decide_side = lambda coin: (1, "LONG")
        auto_trader.calculate_atr = lambda b, s, interval="15m", period=14: 0.02
        auto_trader.calculate_position_size = lambda bal, coin: (200.0, 1.0)
        auto_trader._live_cfg = lambda: SimpleNamespace(
            is_live=False, max_position_pct=None, max_notional=0, max_leverage=20)

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(auto_trader, name, val)
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max
        super().tearDown()

    @staticmethod
    def _mut_set(pending, now, eid, key, value):
        """SAF test mutatoru: deadline/legacy-tohumlama gibi ZAMAN YOLCULUĞU
        değişiklikları transaction üzerinden yapılır (transaction dışı bellek
        yazımı disk hash uzlaşmasını bozar -> MALFORMED_STATE)."""
        new = copy.deepcopy(pending)
        ctx = new.get(str(eid))
        if isinstance(ctx, dict):
            ctx[key] = value
        return new, True

    def test_entry_identity_survives_retry_release_only_verified(self):
        r = self.reserver()
        hb = FakeEntryHB(order_ids=[], status="NOT_STARTED")
        b = FakeEntryBinance()
        coin = {"symbol": "NEARUSDT", "lastPrice": "3.196",
                "quoteVolume": "1000000", "_score": 0.5, "CQ": 0.9,
                "priceChangePercent": "2"}
        # 0) create: create_executor yalnız executor_id döndürür; executor daha
        #    emir koymamış olabilir → kimlik boş (pending+rezervasyon kurulur).
        #    Exchange orderId alanı (entry_order_id) HİÇ yazılmaz.
        self.assertTrue(auto_trader.open_coin(b, hb, coin, dry_run=False,
                                              risk=FakeRisk(), reserver=r))
        ctx = auto_trader._PENDING_FILLS["E1"]
        self.assertEqual(ctx.get("entry_client_order_ids"), [])
        self.assertIsNone(ctx.get("entry_client_order_id"))
        self.assertNotIn("entry_order_id", ctx,
                         "exchange orderId alanına client kimlik asla yazılmaz")
        self.assertIsNotNone(ctx.get("res_id"))
        self.assertEqual(len(r.active_reservations()), 1)
        tx = auto_trader._pending_transaction
        self.assertTrue(tx(self._mut_set, "E1", "deadline", time.time() + 300)[0])

        # 1) Kimlik henüz yok: pending+rezervasyon korunur, otomatik release yok.
        out = auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(out["waiting"], 1)
        self.assertIn("E1", auto_trader._PENDING_FILLS)

        # 2) RUNNING + ilk entry kimliği → history'ye hasat + atomik persist.
        hb.status, hb.order_ids = "RUNNING", ["CID-1"]
        auto_trader._process_pending_fills(b, hb, None, None, r)
        ctx = auto_trader._PENDING_FILLS["E1"]
        self.assertEqual(ctx["entry_client_order_ids"], ["CID-1"])
        self.assertEqual(ctx["entry_client_order_id"], "CID-1")
        disk = json.loads(auto_trader.PENDING_FILLS_JSON.read_text())
        self.assertEqual(disk["pending"]["E1"]["entry_client_order_ids"], ["CID-1"],
                         "kimlik atomik transaction ile diske inmiş olmalı")

        # 3) iptal → retry: eski kimlik order_ids'ten düşer, yenisi gelir;
        #    history ASLA ezilmez — her iki kimlik de korunur.
        hb.order_ids = ["CID-2"]
        auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(
            auto_trader._PENDING_FILLS["E1"]["entry_client_order_ids"],
            ["CID-1", "CID-2"], "retry kimliği history'ye eklenir, ezilmez")

        # 4) close/TP kimliği entry history'sine ASLA giremez: hasat yalnız
        #    NOT_STARTED/RUNNING'de (K1=B kurulumunda bu iki durumda close/TP
        #    zaten oluşamaz — bu adım sağlamalık yoklamasıdır).
        hb.status, hb.order_ids = "SHUTTING_DOWN", ["TP-1"]
        auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(
            auto_trader._PENDING_FILLS["E1"]["entry_client_order_ids"],
            ["CID-1", "CID-2"], "SHUTTING_DOWN'da hasat durur; TP-1 adoption yok")

        # 5) legacy exchange orderId (yalnız Binance yanıtından gelmiş olabilir):
        #    kendi parametresiyle sorgulanır — origClientOrderId ile takas edilmez.
        self.assertTrue(tx(self._mut_set, "E1", "entry_order_id", "12345")[0])
        self.assertTrue(tx(self._mut_set, "E1", "deadline", time.time() - 1)[0])

        # 6) UNKNOWN sorgu yanıtı → pending+rezervasyon KORUNUR (stop denenir,
        #    release yoktur) ve UNKNOWN kimlikte zincir durur.
        b.order = None
        out = auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(out["waiting"], 1)
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(len(r.active_reservations()), 1)
        self.assertIn(("E1", True), hb.stopped, "stop her zaman keep_position=True")
        self.assertEqual(
            b.order_queries, [{"order_id": None, "orig_client_order_id": "CID-1"}],
            "client kimlik yalnız orig_client_order_id parametresine gider; "
            "UNKNOWN'da sonraki kimlikler sorgulanmaz")

        # 7) executedQty>0 terminal no-fill DEĞİLDİR: dolum uzlaştırma yolu
        #    yönetir → pending+rezervasyon korunur.
        b.order = {"status": "CANCELED", "executedQty": "0.4"}
        out = auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(out["waiting"], 1)
        self.assertIn("E1", auto_trader._PENDING_FILLS)

        # 8) Sembolde açık emir kaldıysa (yeni retry ihtimali) release engellenir.
        nq = len(b.order_queries)
        b.open = [{"symbol": "NEARUSDT", "orderId": 999}]
        out = auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(out["waiting"], 1)
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(len(b.order_queries), nq,
                         "openOrders dolu → tekil sorguya gerek yok")
        b.open = []

        # 9) TÜM kimlikler doğrulanmış terminal + sıfır dolum + açık emir yok +
        #    flat + executor TERMINATED → rezervasyon bırakılır.
        b.order = {"status": "CANCELED", "executedQty": "0"}
        out = auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(out["released"], 1)
        self.assertNotIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(r.active_reservations(), [])
        self.assertTrue(all(s == ("E1", True) for s in hb.stopped))
        # Kimlik alanları asla takas edilmemiş: client kimlik orderId paramına,
        # exchange orderId origClientOrderId paramına GİRMEMİŞ olmalı.
        for q in b.order_queries:
            if q["orig_client_order_id"]:
                self.assertIsNone(q["order_id"])
                self.assertIn(q["orig_client_order_id"], ("CID-1", "CID-2"))
            if q["order_id"]:
                self.assertEqual(q["order_id"], "12345")
        self.assertTrue(any(q["order_id"] == "12345"
                            and q["orig_client_order_id"] is None
                            for q in b.order_queries),
                        "legacy exchange orderId kendi parametresiyle sorgulandı")


if __name__ == "__main__":
    unittest.main()
