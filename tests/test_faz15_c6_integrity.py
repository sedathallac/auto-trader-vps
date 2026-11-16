"""FAZ15 — C6 slot/rezervasyon/pozisyon bütünlüğü ve pending lifecycle testleri.

42 test metodu, 0 SKIP. Tüm DB'ler geçici dizinde; canlı state'e yazılmaz.
"""
import json
import os
import sys
import tempfile
import time
import unittest

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

    def test_no_running_executor_count_gate(self):
        src = self._src("process_candidates")
        self.assertNotIn('e["trading_pair"] for e in hb.active_executors()', src)


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

    def test_terminal_zero_exec_flat_releases(self):
        r = self._reserver_recording()
        rid = r.reserve("NEARUSDT", set()).reservation_id
        auto_trader._PENDING_FILLS = {"E1": self._ctx(
            res_id=rid, deadline=time.time() - 1, entry_order_id=12345)}
        hb = FakeHB(executor={"filled_amount_quote": 0.0, "error_count": 0})
        b = FakeBinance([], order={"status": "CANCELED", "executedQty": "0"})
        out = auto_trader._process_pending_fills(b, hb, None, None, r)
        self.assertEqual(out["released"], 1)
        self.assertNotIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(r.active_reservations(), [])
        self.assertEqual(hb.stopped, [("E1", True)])

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
        out1 = auto_trader._process_pending_fills(FakeBinance([]), hb, None, None, r)
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        self.assertTrue(auto_trader._PENDING_FILLS["E1"]["finalization_complete"])
        self.assertEqual(out1["waiting"], 1)
        self.assertEqual(self.order.count("finalize"), 1)
        r.fail = False
        out2 = auto_trader._process_pending_fills(FakeBinance([]), hb, None, None, r)
        self.assertEqual(self.order.count("finalize"), 1, "finalizer tekrar çalışmamalı")
        self.assertNotIn("E1", auto_trader._PENDING_FILLS)
        self.assertEqual(out2["waiting"], 0)

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


if __name__ == "__main__":
    unittest.main()
