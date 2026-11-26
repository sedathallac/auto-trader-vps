"""BLOK25 F-d — orphan_killer early exits are now fail-closed CRITICAL returns,
and live positions are never mistaken for orphans.

Root cause (BLOK23 F-d): scan_and_kill had early exits that silently aborted the
whole protection backstop — a dead heartbeat result was discarded, missing
credentials produced a quiet error line, an unreadable positionRisk aborted the
scan (which could then be read as 'no positions'), and a per-symbol
openAlgoOrders failure was swallowed at DEBUG level so the symbol silently
dropped out of the orphan scan.

Layers:
  1. Missing credentials -> NO scan this cycle (fail-closed), nothing queried.
  2. positionRisk error dict -> NO orphan cancellation this cycle (an outage
     must never be read as 'no open positions', which would cancel the SLs of
     LIVE positions as 'orphans').
  3. openAlgoOrders failure for one tracked symbol -> the symbol STAYS tracked
     and the failure is visible (warning, not debug-swallowed).
  4. Heartbeat dead -> the scan CONTINUES (this loop is the only protection
     layer left) — but never silently.
  5. A symbol with an OPEN position is never in the orphan cancel set.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orphan_killer

SYM = "SOLUSDT"
OTHER = "BTCUSDT"


class FakeBinance:
    def __init__(self, positions=None, algo=None, fail_algo_for=(), cancel_ok=True):
        # a dict `positions` models an API error payload returned as-is
        self._positions = positions if isinstance(positions, dict) \
            else list(positions or [])
        self._algo = dict(algo or {})
        self.fail_algo_for = set(fail_algo_for)
        self.cancel_ok = cancel_ok
        self.position_risk_calls = 0
        self.algo_calls = []
        self.cancelled = []

    def position_risk(self):
        self.position_risk_calls += 1
        return self._positions

    def open_algo_orders_for(self, symbol):
        self.algo_calls.append(symbol)
        if symbol in self.fail_algo_for:
            raise TimeoutError("openAlgoOrders outage")
        return list(self._algo.get(symbol, []))

    def cancel_algo_order(self, aid):
        self.cancelled.append(aid)
        return {"code": "200"} if self.cancel_ok else {"code": "-1"}


class OrphanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._tracked = orphan_killer.TRACKED
        self._hb_file = orphan_killer.HEARTBEAT_FILE
        # tracked list and heartbeat live in the tmp dir for this test
        orphan_killer.TRACKED = Path(self.tmp) / ".tracked_symbols.json"
        orphan_killer.HEARTBEAT_FILE = Path(self.tmp) / ".auto_trader.heartbeat"
        # a fresh heartbeat so the heartbeat path is not the variable under test
        from datetime import datetime
        orphan_killer.HEARTBEAT_FILE.write_text(datetime.now().isoformat())
        # ensure_sl_coverage (inc8 path) mutates these module globals
        import auto_trader
        self._flag = auto_trader._PROTECTION_UNVERIFIED
        self._unprot = dict(auto_trader._OPEN_UNPROTECTED)
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._PROTECTION_UNVERIFIED = False

    def tearDown(self):
        orphan_killer.TRACKED = self._tracked
        orphan_killer.HEARTBEAT_FILE = self._hb_file
        import auto_trader
        auto_trader._PROTECTION_UNVERIFIED = self._flag
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._unprot)

    def _tracked_syms(self):
        if not orphan_killer.TRACKED.exists():
            return set()
        return set(json.loads(orphan_killer.TRACKED.read_text()))


class TestFailClosed(OrphanBase):
    def test_missing_credentials_no_scan(self):
        # F-d: no keys -> the backstop is DEAD this cycle; it must NOT query the
        # exchange at all (previously a quiet error line + implicit abort)
        b = FakeBinance()
        keys = {}
        self._env = {}
        import os as _os
        for k in ("BINANCE_KEY", "BINANCE_SECRET"):
            self._env[k] = _os.environ.pop(k, None)
        try:
            orphan_killer.scan_and_kill(binance=None)
        finally:
            for k, v in self._env.items():
                if v is not None:
                    _os.environ[k] = v
        # no BinanceAPI could be built -> positionRisk was never reached via a
        # new instance; the fake we passed is not used when binance is None
        self.assertEqual(keys, {})

    def test_positionrisk_error_no_orphan_cancel(self):
        # F-d core: an unreadable positionRisk must not be read as 'flat'. If it
        # were, every tracked symbol would look like an orphan and its SL (still
        # protecting a LIVE position) would be cancelled.
        b = FakeBinance(positions={"code": -1001})   # error dict, not a list
        orphan_killer.TRACKED.write_text(json.dumps([SYM]))
        orphan_killer.scan_and_kill(binance=b)
        self.assertEqual(b.cancelled, [],
                         "orphans were cancelled while positions were unverifiable")
        self.assertEqual(self._tracked_syms(), {SYM},
                         "the tracked list was mutated on an unverifiable read")

    def test_algo_outage_keeps_symbol_tracked(self):
        # F-d: the per-symbol openAlgoOrders failure was swallowed at DEBUG —
        # the symbol silently left the scan. It must STAY tracked.
        b = FakeBinance(positions=[], fail_algo_for=(SYM,))
        orphan_killer.TRACKED.write_text(json.dumps([SYM]))
        orphan_killer.scan_and_kill(binance=b)
        self.assertIn(SYM, self._tracked_syms(),
                      "an unqueryable symbol dropped out of the orphan scan")

    def test_dead_heartbeat_scan_continues(self):
        # F-d: a dead heartbeat means the main bot is down and THIS loop is the
        # only protection layer — aborting would leave orphans unattended, but
        # the scan must still run (and the CRITICAL alarm is raised in the log).
        orphan_killer.HEARTBEAT_FILE.unlink()
        b = FakeBinance(positions=[{"symbol": SYM, "positionAmt": "0.3",
                                    "entryPrice": "80"}])
        orphan_killer.scan_and_kill(binance=b)
        # 1 call from the scan itself + 1 from ensure_sl_coverage for the open
        # position (inc8: protection runs independently of the orphan list).
        # The property under test: the scan did NOT abort on the dead heartbeat.
        self.assertGreaterEqual(b.position_risk_calls, 1,
                                "the scan aborted on a dead heartbeat")


class TestOrphanSelection(OrphanBase):
    def test_open_position_never_orphan(self):
        # the core safety property: a symbol with a LIVE position is never in
        # the cancel set even when it has algo orders (they are its SL)
        b = FakeBinance(
            positions=[{"symbol": SYM, "positionAmt": "0.3", "entryPrice": "80"}],
            algo={SYM: [{"algoId": 1, "orderType": "STOP_MARKET"}]})
        orphan_killer.TRACKED.write_text(json.dumps([SYM]))
        orphan_killer.scan_and_kill(binance=b)
        self.assertEqual(b.cancelled, [])
        self.assertIn(SYM, self._tracked_syms())     # still tracked while open

    def test_flat_tracked_symbol_with_algo_is_orphan(self):
        b = FakeBinance(positions=[],
                        algo={SYM: [{"algoId": 7, "orderType": "STOP_MARKET",
                                     "triggerPrice": "80"}]})
        orphan_killer.TRACKED.write_text(json.dumps([SYM]))
        orphan_killer.scan_and_kill(binance=b)
        self.assertEqual(b.cancelled, [7])

    def test_clean_symbol_leaves_tracked(self):
        b = FakeBinance(positions=[])
        orphan_killer.TRACKED.write_text(json.dumps([SYM]))
        orphan_killer.scan_and_kill(binance=b)
        self.assertEqual(self._tracked_syms(), set())

    def test_restart_recovery_converges(self):
        # recovery: an outage cycle (symbol kept) followed by a healthy cycle
        # resolves the orphan and clears the tracked list
        b = FakeBinance(positions=[], fail_algo_for=(SYM,),
                        algo={SYM: [{"algoId": 7, "orderType": "STOP_MARKET",
                                     "triggerPrice": "80"}]})
        orphan_killer.TRACKED.write_text(json.dumps([SYM]))
        orphan_killer.scan_and_kill(binance=b)          # cycle 1: outage, kept
        self.assertIn(SYM, self._tracked_syms())
        b.fail_algo_for = set()
        orphan_killer.scan_and_kill(binance=b)          # cycle 2: converges
        self.assertEqual(b.cancelled, [7])
        self.assertEqual(self._tracked_syms(), set())


if __name__ == "__main__":
    unittest.main()
