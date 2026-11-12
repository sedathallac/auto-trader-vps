"""Increment 3 (disable all HB autonomous close paths) + Increment 9 (-2021 ->
OPEN_UNPROTECTED + CRITICAL + block new entries). Tests bind to the real
production functions in auto_trader.py and close_coordinator.py.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at
from close_coordinator import CloseCoordinator, CloseState, ONEWAY, HEDGE


# ---- Increment 3: HB autonomous close paths disabled ----------------------
class TestExecutorBarriersDisabled(unittest.TestCase):
    def _cfg(self):
        return at.build_executor_config("SOLUSDT", 0.3, 3, 1)

    def test_all_four_barriers_none(self):
        tb = self._cfg()["triple_barrier_config"]
        # stop_loss / market+limit take_profit / trailing / time_limit cannot fire
        self.assertIsNone(tb["stop_loss"])
        self.assertIsNone(tb["take_profit"])
        self.assertIsNone(tb["time_limit"])
        self.assertIsNone(tb["trailing_stop"])

    def test_no_barrier_order_types(self):
        tb = self._cfg()["triple_barrier_config"]
        # no take_profit/stop_loss/time_limit order-type keys -> HB builds none
        self.assertNotIn("take_profit_order_type", tb)
        self.assertNotIn("stop_loss_order_type", tb)
        self.assertNotIn("time_limit_order_type", tb)
        self.assertEqual(tb.get("open_order_type"), 1)

    def test_config_is_position_executor_for_pair(self):
        cfg = self._cfg()
        self.assertEqual(cfg["type"], "position_executor")
        self.assertEqual(cfg["trading_pair"], "SOL-USDT")
        self.assertEqual(cfg["side"], 1)


class _MockEx:
    def __init__(self, pos, mode=ONEWAY, protective=None):
        self._pos = Decimal(str(pos)); self._mode = mode
        self._protective = list(protective or []); self.close_calls = []; self.stopped = 0
    def position_amt(self, s): return self._pos
    def position_mode(self): return self._mode
    def protective_orders(self, s): return list(self._protective)
    def order_terminal(self, o): return bool(o.get("terminal", False))
    def cancel_order(self, o): return bool(o.get("cancel_terminal", True))
    def stop_executor(self, s): self.stopped += 1
    def close_reduce_only(self, s, q, position_side=None):
        self.close_calls.append((Decimal(str(q)), position_side))
        if self._pos > 0: self._pos = max(Decimal("0"), self._pos - Decimal(str(q)))
        elif self._pos < 0: self._pos = min(Decimal("0"), self._pos + Decimal(str(q)))
        return Decimal(str(q))


class TestTPTimeLimitRoutedThroughSingleAuthority(unittest.TestCase):
    def test_take_profit_routes_through_reduce_only_close(self):
        ex = _MockEx("0.3")
        cc = CloseCoordinator(ex)
        r = cc.request_take_profit("SOLUSDT")
        self.assertEqual(r.state, CloseState.CLOSED)
        self.assertEqual(r.action, "SENT_REDUCE_ONLY")
        self.assertEqual(len(ex.close_calls), 1)

    def test_time_limit_after_close_is_ignored(self):
        ex = _MockEx("0.3")
        cc = CloseCoordinator(ex)
        cc.request_take_profit("SOLUSDT")           # TP closes it
        r = cc.request_time_limit("SOLUSDT")         # later time-limit trigger
        self.assertEqual(r.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(ex.close_calls), 1)     # no second (uncoordinated) close

    def test_pending_limit_tp_unresolved_blocks_handoff(self):
        # a non-terminal limit TP whose cancel is not confirmed -> no close sent
        ex = _MockEx("0.3", protective=[{"id": "tp", "terminal": False, "cancel_terminal": False}])
        cc = CloseCoordinator(ex)
        r = cc.request_close("SOLUSDT")
        self.assertEqual(r.action, "UNRESOLVED_ORDER")
        self.assertTrue(r.critical)
        self.assertEqual(len(ex.close_calls), 0)

    def test_stop_executor_called_keep_position(self):
        ex = _MockEx("0.3")
        CloseCoordinator(ex).request_close("SOLUSDT")
        self.assertEqual(ex.stopped, 1)  # HB stopped (keep_position=True in adapter)


# ---- Increment 9: -2021 -> OPEN_UNPROTECTED ------------------------------
class TestSlOutcomeClassifier(unittest.TestCase):
    def test_protected(self):
        self.assertEqual(at.sl_placement_outcome({"algoId": 123}), ("PROTECTED", None))

    def test_2021(self):
        self.assertEqual(at.sl_placement_outcome({"code": -2021, "msg": "x"}), ("UNPROTECTED", -2021))

    def test_other_error(self):
        self.assertEqual(at.sl_placement_outcome({"code": -1130}), ("UNPROTECTED", -1130))

    def test_empty_or_bad(self):
        self.assertEqual(at.sl_placement_outcome({}), ("UNPROTECTED", None))
        self.assertEqual(at.sl_placement_outcome("boom"), ("UNPROTECTED", None))


class TestUnprotectedTransitions(unittest.TestCase):
    def test_note_transitions(self):
        m, a = at.note_sl_outcome({}, "SOLUSDT", {"code": -2021})
        self.assertEqual(a, "UNPROTECTED_2021")
        self.assertEqual(m["SOLUSDT"], -2021)
        m2, a2 = at.note_sl_outcome(m, "SOLUSDT", {"algoId": 9})  # resolved
        self.assertEqual(a2, "PROTECTED")
        self.assertNotIn("SOLUSDT", m2)
        m3, a3 = at.note_sl_outcome({}, "ETHUSDT", {"code": -1130})
        self.assertEqual(a3, "UNPROTECTED_OTHER")

    def test_should_block_new_entries(self):
        self.assertFalse(at.should_block_new_entries({}))
        self.assertTrue(at.should_block_new_entries({"SOLUSDT": -2021}))

    def test_module_state_gate(self):
        at._OPEN_UNPROTECTED.clear()
        self.assertFalse(at.should_block_new_entries())
        at._OPEN_UNPROTECTED["SOLUSDT"] = -2021
        try:
            self.assertTrue(at.should_block_new_entries())
        finally:
            at._OPEN_UNPROTECTED.clear()


if __name__ == "__main__":
    unittest.main()
