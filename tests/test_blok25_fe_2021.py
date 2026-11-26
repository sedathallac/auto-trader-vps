"""BLOK25 F-e — place_algo_sl -2021 is CLASSIFIED on the open_coin path, flagged
OPEN_UNPROTECTED, blocks new entries, and is never blindly retried.

Root cause (BLOK23 F-e): the open_coin path never classified place_algo_sl's
result, so a -2021 rejection ("order would immediately trigger") could be
silently treated as protected while the position sat unarmed.

Layers:
  1. note_sl_outcome / sl_placement_outcome classification purity.
  2. open_coin: -2021 -> _OPEN_UNPROTECTED[symbol] == -2021, no fake protection.
  3. ensure_sl_coverage: a -2021-flagged symbol is NOT resent (no uncontrolled
     retry); the alarm stays up.
  4. Recovery: RC-B — a reliable flat read clears the stale -2021 alarm, so it
     can never block new entries forever.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import (ensure_sl_coverage, note_sl_outcome,
                         sl_placement_outcome, should_block_new_entries)

SYM = "SOLUSDT"


class TestClassificationPurity(unittest.TestCase):
    def test_accepted_is_protected(self):
        self.assertEqual(sl_placement_outcome({"algoId": 878098}), ("PROTECTED", None))

    def test_2021_classified(self):
        self.assertEqual(sl_placement_outcome({"code": -2021, "msg": "Order would immediately trigger."}),
                         ("UNPROTECTED", -2021))

    def test_other_error_classified(self):
        self.assertEqual(sl_placement_outcome({"code": -2019, "msg": "margin"}),
                         ("UNPROTECTED", -2019))

    def test_garbage_is_unprotected_no_code(self):
        self.assertEqual(sl_placement_outcome("boom"), ("UNPROTECTED", None))
        self.assertEqual(sl_placement_outcome(None), ("UNPROTECTED", None))

    def test_note_2021_action(self):
        m, action = note_sl_outcome({}, SYM, {"code": -2021})
        self.assertEqual(action, "UNPROTECTED_2021")
        self.assertEqual(m, {SYM: -2021})

    def test_note_protected_clears(self):
        m, action = note_sl_outcome({SYM: -2021}, SYM, {"algoId": 5})
        self.assertEqual(action, "PROTECTED")
        self.assertEqual(m, {})

    def test_note_other_error(self):
        m, action = note_sl_outcome({}, SYM, {"code": -1111})
        self.assertEqual(action, "UNPROTECTED_OTHER")
        self.assertEqual(m, {SYM: -1111})


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
    """Fills + a live LONG; place_algo_sl is scripted (default: -2021)."""

    def __init__(self, sl_result=None):
        self.amt = Decimal("0.3")
        self.sl_result = sl_result if sl_result is not None else {"code": -2021}
        self.placed = []

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
        return []

    def place_algo_sl(self, s, trig, qty, side):
        self.placed.append((trig, qty, side))
        return self.sl_result


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


class TestOpenCoin2021(_OpenCoinBase):
    def test_2021_flagged_unprotected(self):
        b = B()
        auto_trader.open_coin(b, HB(), self._coin(), risk=None, reserver=None,
                              coordinator=None)
        # F-e core: the -2021 rejection is now CLASSIFIED, not assumed protected
        self.assertEqual(auto_trader._OPEN_UNPROTECTED.get(SYM), -2021)
        self.assertTrue(should_block_new_entries())

    def test_success_not_flagged(self):
        b = B(sl_result={"algoId": 878098})
        auto_trader.open_coin(b, HB(), self._coin(), risk=None, reserver=None,
                              coordinator=None)
        self.assertEqual(auto_trader._OPEN_UNPROTECTED, {})


class TestNoUncontrolledRetry(unittest.TestCase):
    """ensure_sl_coverage must NOT resend a -2021-flagged symbol (the same
    trigger would just fail again); the alarm stays until exchange truth moves."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_db = auto_trader.SL_LEDGER_DB
        auto_trader.SL_LEDGER_DB = os.path.join(self.tmp, "risk_ledger.db")
        self._unprot = dict(auto_trader._OPEN_UNPROTECTED)
        self._flag = auto_trader._PROTECTION_UNVERIFIED
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._PROTECTION_UNVERIFIED = False
        self._vo = auto_trader._verify_oneway
        auto_trader._verify_oneway = lambda b: (True, "ONEWAY")
        self._atr = auto_trader.calculate_atr
        auto_trader.calculate_atr = lambda b, s, **k: 0.02

    def tearDown(self):
        auto_trader.SL_LEDGER_DB = self._orig_db
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._unprot)
        auto_trader._PROTECTION_UNVERIFIED = self._flag
        auto_trader._verify_oneway = self._vo
        auto_trader.calculate_atr = self._atr

    class CoverB(B):
        """Open LONG with NO live SL (SL_ABSENT) and a scripted place result."""

        def __init__(self, sl_result):
            super().__init__(sl_result=sl_result)
            self.position_reads = 0

        def position_risk(self):
            self.position_reads += 1
            return [{"symbol": SYM, "positionAmt": "0.3",
                     "entryPrice": "81.27"}]

    def test_2021_not_resent(self):
        auto_trader._OPEN_UNPROTECTED[SYM] = -2021
        b = self.CoverB({"code": -2021})
        ensure_sl_coverage(b)
        self.assertEqual(b.placed, [], "a -2021 symbol was resent (uncontrolled retry)")
        self.assertEqual(auto_trader._OPEN_UNPROTECTED.get(SYM), -2021)

    def test_flat_read_clears_stale_2021(self):
        # RC-B recovery: the position is reliably flat -> the stale -2021 alarm
        # clears, so it can never block new entries forever
        auto_trader._OPEN_UNPROTECTED[SYM] = -2021
        b = self.CoverB({"algoId": 1})
        b.position_risk = lambda: []
        ensure_sl_coverage(b)
        self.assertEqual(auto_trader._OPEN_UNPROTECTED, {})
        self.assertFalse(should_block_new_entries())


if __name__ == "__main__":
    unittest.main()
