"""Increment 5 final acceptance — multi-executor handoff.

A symbol can have MORE THAN ONE Hummingbot executor (e.g. grid / multi-level
strategies: several position executors on the same trading_pair). _hb_order_ids
must aggregate the tracked order ids of ALL of them; the handoff must not complete
until EVERY executor's orders are terminal. Also: a live ENTRY is cancelled and
confirmed terminal (a partial fill leaves the remainder to be closed after a fresh
positionRisk read), and a lost executor / order mapping after restart -> UNKNOWN.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator, CloseState
from test_inc5_review import FakeBinance, SYM, PAIR, _oo

_MISSING = object()


class MultiHB:
    """active_executors with several executors; each tuple is (pair, order_ids),
    order_ids == _MISSING means the executor has no order_ids mapping."""
    def __init__(self, executors):
        self._execs = executors
        self.stopped = []

    def active_executors(self):
        out = []
        for i, (pair, ids) in enumerate(self._execs):
            e = {"trading_pair": pair, "status": "RUNNING", "executor_id": f"E{i}"}
            if ids is not _MISSING:
                e["custom_info"] = {"order_ids": ids}
            out.append(e)
        return out

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


def _cc(fb, hb):
    return CloseCoordinator(BinanceHBCloseAdapter(fb, hb))


class TestMultiExecutorHandoff(unittest.TestCase):
    # Two executors on the same symbol; the SECOND executor's close is still live.
    def test_second_executor_live_order_blocks(self):
        fb = FakeBinance()
        fb.add(1, "C1", "CANCELED"); fb.add(2, "T1", "CANCELED")   # executor A terminal
        fb.open[SYM] = [_oo(3, "C2", "NEW")]                       # executor B close still live
        fb.add(3, "C2", "NEW"); fb.cancel_to[3] = "NEW"           # cancel cannot terminalise it
        hb = MultiHB([(PAIR, ["E1a", "C1", "T1"]), (PAIR, ["E2b", "C2", "T2"])])
        fb.add(10, "E1a", "FILLED"); fb.add(11, "E2b", "FILLED"); fb.add(12, "T2", "CANCELED")
        r = _cc(fb, hb).request_close(SYM)
        self.assertEqual(r.action, "UNRESOLVED_ORDER",
                         "second executor's live order was skipped")
        self.assertEqual(fb.posts, [], "closed while a second executor order was live")

    def test_both_executors_all_terminal_allows(self):
        fb = FakeBinance()
        for oid, coid in [(1, "E1a"), (2, "C1"), (3, "T1"), (4, "E2b"), (5, "C2"), (6, "T2")]:
            fb.add(oid, coid, "FILLED" if coid.startswith("E") else "CANCELED")
        hb = MultiHB([(PAIR, ["E1a", "C1", "T1"]), (PAIR, ["E2b", "C2", "T2"])])
        r = _cc(fb, hb).request_close(SYM)
        self.assertTrue(r.ok); self.assertEqual(r.state, CloseState.CLOSED)

    # live ENTRY is cancelled + confirmed; partial fill -> remainder closed after
    # a fresh positionRisk read.
    def test_entry_partial_cancel_then_close_remainder(self):
        fb = FakeBinance(positions=[{"symbol": SYM, "positionAmt": "-0.2"}])  # partial pos
        fb.open[SYM] = [_oo(7, "ENTRY", "PARTIALLY_FILLED")]
        fb.add(7, "ENTRY", "PARTIALLY_FILLED"); fb.cancel_to[7] = "CANCELED"  # cancel succeeds
        hb = MultiHB([(PAIR, ["ENTRY"])])
        r = _cc(fb, hb).request_close(SYM)
        self.assertIn(7, fb.cancelled, "entry not cancelled")
        self.assertTrue(r.ok)
        self.assertEqual(len(fb.posts), 1, "remainder not closed after positionRisk re-read")

    # restart: no executor for the pair but open orders exist -> UNKNOWN
    def test_restart_executor_gone_unknown(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(8, "X", "NEW")]
        r = _cc(fb, MultiHB([("BTC-USDT", ["Z"])])).request_close(SYM)  # no SOL executor
        self.assertFalse(r.ok); self.assertTrue(r.critical); self.assertEqual(fb.posts, [])

    # restart: executor present but order_ids mapping lost -> UNKNOWN
    def test_order_mapping_lost_unknown(self):
        fb = FakeBinance(); fb.open[SYM] = [_oo(9, "X", "NEW")]
        r = _cc(fb, MultiHB([(PAIR, _MISSING)])).request_close(SYM)
        self.assertFalse(r.ok); self.assertTrue(r.critical); self.assertEqual(fb.posts, [])


if __name__ == "__main__":
    unittest.main()
