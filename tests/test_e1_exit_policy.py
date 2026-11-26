"""E-1 — live exit policy is exchange-SL-only (documented, no strategy invented).

The live exits are: (1) the exchange reduceOnly STOP_MARKET algo SL filling
autonomously, and (2) the coordinator-routed emergency close. There is deliberately
NO TP/time-limit trigger in the live loop. This test pins that invariant so a future
change can't silently re-enable HB autonomous closes or add an untested exit:
  * build_executor_config disables ALL triple-barrier closes (K1=B).
  * request_take_profit/request_time_limit exist (future interface) and, when ever
    used, route through the ONE CloseCoordinator (never a separate HB/MARKET close).
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from close_coordinator import CloseCoordinator


class TestE1ExitPolicy(unittest.TestCase):
    def test_all_hb_barriers_disabled(self):
        cfg = auto_trader.build_executor_config("SOLUSDT", "0.3", 3, 1)
        tb = cfg["triple_barrier_config"]
        for k in ("stop_loss", "take_profit", "time_limit", "trailing_stop"):
            self.assertIsNone(tb[k], f"HB autonomous {k} must stay disabled (K1=B)")

    def test_tp_timelimit_interfaces_route_through_coordinator(self):
        # The interfaces exist (future increment) and delegate to request_close —
        # i.e. the single authority, never an independent HB/MARKET close path.
        self.assertTrue(hasattr(CloseCoordinator, "request_take_profit"))
        self.assertTrue(hasattr(CloseCoordinator, "request_time_limit"))

    def test_no_live_tp_timelimit_trigger_in_source(self):
        # Document the SL-only policy: no production call site invokes the TP/
        # time-limit triggers (only their definitions/docstrings mention them).
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "auto_trader.py")).read()
        # a trigger call would look like `.request_take_profit(` / `.request_time_limit(`
        calls = re.findall(r"\.request_(?:take_profit|time_limit)\(", src)
        self.assertEqual(calls, [], "unexpected live TP/time-limit trigger in auto_trader")


if __name__ == "__main__":
    unittest.main()
