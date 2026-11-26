"""B-1 fix — no direct MARKET close bypass when the coordinator is unavailable.

Before: coordinate_close(coordinator=None, ...) sent a raw reduce-only MARKET POST,
and run_once initialised coordinator=None in a try/except but still entered the
candidate loop, so a coordinator-construction failure silently fell back to the
direct-close bypass (no intent, no handoff) AND still opened new positions.

After: coordinate_close(None) sends NO order (CRITICAL, fail-closed); run_once blocks
NEW entries when the coordinator/store could not be built, while the exchange-SL
protection check (ensure_sl_coverage, inside protect_then_gate) still runs.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import coordinate_close

SYM = "SOLUSDT"


class FakeBinance:
    def __init__(self):
        self.posts = []

    def _post(self, path, params, method="POST"):
        self.posts.append((path, dict(params), method))
        return {"orderId": 1, "status": "FILLED"}


class TestCoordinateCloseNoBypass(unittest.TestCase):
    def test_none_coordinator_sends_no_market_post(self):
        fb = FakeBinance()
        r = coordinate_close(None, fb, SYM, "SL_UNVERIFIED", side=1, amount="0.3")
        self.assertEqual(fb.posts, [], "direct MARKET close bypass still present")
        self.assertFalse(getattr(r, "ok", False))   # fail-closed result (not a fill)


class TestRunOnceBlocksWhenNoCoordinator(unittest.TestCase):
    def setUp(self):
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2

    def tearDown(self):
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max

    def test_run_once_no_coordinator_blocks_new_entries(self):
        from unittest.mock import MagicMock
        captured = {"open_coin_called": False}
        real_open_coin = auto_trader.open_coin

        def spy(*a, **k):
            captured["open_coin_called"] = True
            return False
        auto_trader.open_coin = spy

        # force CloseCoordinator construction to FAIL -> coordinator stays None
        fake_risk = MagicMock()
        fake_risk.db_file = "/nonexistent_dir_b1/x.db"  # init_schema will raise
        fake_risk.is_halted = False
        fake_risk.state = {}

        class _HB:
            def active_executors(self):
                return []

        class _B:
            pass
        stubs = {
            "heartbeat_tick": lambda: None,
            "sync_risk_engine": lambda *a, **k: None,
            "reconcile_positions": lambda *a, **k: True,
            "check_stale_data": lambda *a, **k: True,
            "protect_then_gate": lambda *a, **k: (True, {"drift": False, "stale": False,
                                                         "mode": "ONEWAY", "mode_ok": True,
                                                         "unprotected": False}),
            "scan_top": lambda *a, **k: [{"symbol": SYM, "lastPrice": "80",
                                          "quoteVolume": "1", "_score": 1.0,
                                          "priceChangePercent": "2"}],
            "RiskEngine": lambda *a, **k: fake_risk,
            "HBAPI": lambda *a, **k: _HB(),
            "BinanceAPI": lambda *a, **k: _B(),
        }
        saved = {n: getattr(auto_trader, n) for n in stubs}
        for n, v in stubs.items():
            setattr(auto_trader, n, v)
        orig_env = {k: os.environ.get(k) for k in ("BINANCE_KEY", "BINANCE_SECRET")}
        os.environ["BINANCE_KEY"] = "k"
        os.environ["BINANCE_SECRET"] = "s"
        try:
            auto_trader.run_once(dry_run=False)
            self.assertFalse(captured["open_coin_called"],
                             "new entry opened without a coordinator")
        finally:
            auto_trader.open_coin = real_open_coin
            for n, v in saved.items():
                setattr(auto_trader, n, v)
            for k, v in orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
