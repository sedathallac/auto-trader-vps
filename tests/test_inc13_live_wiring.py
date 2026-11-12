"""Increment 13 — proof the LIVE open_coin/run_once chain reaches CloseCoordinator.

inc3-inc12 proved the components in isolation; this proves the REAL trading
lifecycle uses them. It drives the REAL open_coin() to its emergency-close branch
(SL could not be placed) and asserts the close is served by the REAL
CloseCoordinator.request_close (HB executor stopped keep_position=True, a cross-
process intent created + marked CLOSED, exactly one reduce-only close, flat-
confirmed, idempotent) — NOT the old direct market-close POST. Only the Binance/HB
boundary is faked.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import coordinate_close
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator
from close_intent import CloseIntentStore

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class FakeBinance:
    """Boundary fake: an open SOL LONG that a reduce-only close flattens. Records
    every /fapi/v1/order POST so we can assert exactly one close + its params."""
    def __init__(self, amt="0.6", entry="80", sl_result=None):
        self.amt = Decimal(str(amt))
        self.entry = entry
        self.sl_result = sl_result if sl_result is not None else {"code": -2021}
        self.order_posts = []

    # --- used by open_coin before the close ---
    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.1"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 1000.0

    def set_leverage(self, symbol, lev):
        return {"leverage": lev}

    def place_algo_sl(self, symbol, trig, qty, side):
        return self.sl_result            # -2021 -> algo_id None -> SL unverified

    def open_algo_orders_for(self, symbol):
        return []

    # --- used by the CloseCoordinator / adapter ---
    def position_risk(self):
        return [{"symbol": SYM, "positionAmt": str(self.amt), "entryPrice": self.entry}]

    def position_mode(self):
        return {"dualSidePosition": False}      # ONEWAY

    def open_orders(self, symbol):
        return []

    def _post(self, path, params, method="POST"):
        if path == "/fapi/v1/order" and method == "POST":
            self.order_posts.append(dict(params))
            # reduce-only close: flatten (never cross zero)
            if str(params.get("reduceOnly", "")).lower() == "true":
                self.amt = Decimal("0")
            return {"orderId": 1, "status": "FILLED"}
        return {"orderId": 1, "status": "NEW"}


class FakeHB:
    def __init__(self):
        self.created = []
        self.stopped = []

    def active_executors(self):
        # empty until the executor is created (so open_coin's entry cap check passes),
        # then RUNNING so the CloseCoordinator handoff can stop it.
        if not self.created:
            return []
        return [{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
                 "custom_info": {"order_ids": []}}]

    def create_executor(self, symbol, amount, lev, sl, tp, tl, side=1):
        self.created.append(symbol)
        return {"executor_id": "E1"}

    def get_executor(self, eid):
        return {"filled_amount_quote": 100.0, "error_count": 0}

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


def _coordinator(binance, hb, db):
    store = CloseIntentStore(db)
    store.init_schema()
    return CloseCoordinator(BinanceHBCloseAdapter(binance, hb), intent_store=store), store


class LiveWiringBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2
        # stub the pre-close scoring/market helpers so open_coin reaches the SL step
        self._orig = {}
        for name, val in [
            ("check_liquidity", lambda b, s: (True, "ok")),
            ("check_correlation_limit", lambda s, b: True),
            ("decide_side", lambda coin: (1, "LONG")),
            ("calculate_atr", lambda b, s, interval="15m", period=14: 0.02),
            ("calculate_position_size", lambda bal, coin: (50.0, 1.0)),
        ]:
            self._orig[name] = getattr(auto_trader, name)
            setattr(auto_trader, name, val)
        self._orig_sleep = auto_trader.time.sleep
        auto_trader.time.sleep = lambda *_a, **_k: None

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(auto_trader, name, val)
        auto_trader.time.sleep = self._orig_sleep
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "80", "quoteVolume": "1000000",
                "_score": 0.5, "priceChangePercent": "2"}


class TestOpenCoinReachesCoordinator(LiveWiringBase):
    def test_emergency_close_routes_through_request_close(self):
        binance = FakeBinance()
        hb = FakeHB()
        cc, store = _coordinator(binance, hb, self.db)
        # REAL open_coin, SL placement fails (-2021) -> emergency close branch.
        res = auto_trader.open_coin(binance, hb, self._coin(), dry_run=False,
                                    risk=None, reserver=None, coordinator=cc)
        self.assertFalse(res)                              # open aborted (SL failed)
        # Proof the CloseCoordinator handoff ran (not a raw market POST):
        self.assertEqual(hb.stopped, [("E1", True)])       # HB stopped keep_position
        self.assertEqual(len(binance.order_posts), 1)      # exactly ONE close
        self.assertEqual(binance.order_posts[0].get("reduceOnly"), "true")
        self.assertEqual(binance.amt, Decimal("0"))        # flat-confirmed
        self.assertEqual(store.get(SYM)["state"], "CLOSED")  # cross-process intent terminal

    def test_second_close_is_idempotent(self):
        binance = FakeBinance()
        hb = FakeHB()
        cc, store = _coordinator(binance, hb, self.db)
        auto_trader.open_coin(binance, hb, self._coin(), coordinator=cc)
        posts_after_first = len(binance.order_posts)
        # a duplicate close trigger (e.g. a re-scan) must send NO second POST
        r2 = coordinate_close(cc, binance, SYM, "DUP")
        self.assertEqual(r2.action, "IGNORED_ALREADY_CLOSED")
        self.assertEqual(len(binance.order_posts), posts_after_first)


class TestNoDirectBypass(LiveWiringBase):
    def test_live_path_has_no_direct_market_close(self):
        # With a coordinator wired (as run_once always does), open_coin must NOT call
        # binance._post directly for the close — it goes via request_close, which
        # stops the HB executor. Detect the direct path by its ABSENCE of a stop.
        binance = FakeBinance()
        hb = FakeHB()
        cc, _ = _coordinator(binance, hb, self.db)
        auto_trader.open_coin(binance, hb, self._coin(), coordinator=cc)
        self.assertNotEqual(hb.stopped, [], "close bypassed the coordinator handoff")

    def test_fallback_direct_close_only_without_coordinator(self):
        # coordinate_close(None, ...) is the ONLY direct-close path, used solely when
        # no coordinator is wired (never in the live loop).
        binance = FakeBinance()
        coordinate_close(None, binance, SYM, "LEGACY", side=1, amount="0.6")
        self.assertEqual(len(binance.order_posts), 1)
        self.assertEqual(binance.order_posts[0]["type"], "MARKET")


class TestRunOnceWiresCoordinator(LiveWiringBase):
    def test_run_once_builds_and_passes_coordinator(self):
        # Prove run_once constructs a CloseCoordinator and hands it to open_coin.
        from unittest.mock import MagicMock
        captured = {}
        real_open_coin = auto_trader.open_coin

        def spy(binance, hb, c, dry_run=False, risk=None, reserver=None, coordinator=None):
            captured["coordinator"] = coordinator
            return False
        auto_trader.open_coin = spy
        orig_env = {k: os.environ.get(k) for k in ("BINANCE_KEY", "BINANCE_SECRET")}
        os.environ["BINANCE_KEY"] = "k"
        os.environ["BINANCE_SECRET"] = "s"

        fake_risk = MagicMock()
        fake_risk.db_file = self.db          # temp db, NOT the real home ledger
        fake_risk.is_halted = False
        fake_risk.state = {}

        class _HB:
            def active_executors(self):
                return []
        stubs = {
            "heartbeat_tick": lambda: None,
            "sync_risk_engine": lambda *a, **k: None,
            "reconcile_positions": lambda *a, **k: True,
            "check_stale_data": lambda *a, **k: True,
            "protect_then_gate": lambda *a, **k: (True, {"drift": False, "stale": False,
                                                         "mode": "ONEWAY", "mode_ok": True,
                                                         "unprotected": False}),
            "scan_top": lambda *a, **k: [self._coin()],
            "RiskEngine": lambda *a, **k: fake_risk,
            "HBAPI": lambda *a, **k: _HB(),
            "BinanceAPI": lambda *a, **k: FakeBinance(),
        }
        saved = {n: getattr(auto_trader, n) for n in stubs}
        for n, v in stubs.items():
            setattr(auto_trader, n, v)
        try:
            auto_trader.run_once(dry_run=False)
            self.assertIn("coordinator", captured)
            self.assertIsInstance(captured["coordinator"], CloseCoordinator)
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
