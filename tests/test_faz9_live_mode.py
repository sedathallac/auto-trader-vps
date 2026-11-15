"""FAZ9 live-mode regression tests: staged activation, family gate, fail-closed startup.

Every test runs offline against temp directories. Nothing here touches /var/lib/auto_trader_faz9, the compare
instance's DB, or the exchange.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import live_guard
from live_guard import LiveConfig, LiveConfigError
from risk_engine import RiskEngine

ROOT = Path(__file__).resolve().parents[1]
UNIT_LIVE = ROOT / "deploy" / "auto_trader_faz9_live.service"


def env(**kw):
    base = {"HB_OFFLINE": "0", "LIVE_MODE": "1", "BINANCE_TESTNET": "1", "LIVE_STAGE": "1", "LIVE_DRY_RUN": "0",
            "COMPARE_MODE": "0", "LIVE_STRATEGY_FAMILY": "exhaustion", "LIVE_WINNING_REGIMES": "range,mid"}
    base.update({k: str(v) for k, v in kw.items()})
    return base


def cfg(**kw):
    return LiveConfig.from_env(env(**kw)).validate()


RISK_DDL = ("CREATE TABLE risk_state (id INTEGER PRIMARY KEY CHECK (id=1), peak_equity REAL NOT NULL DEFAULT 0,"
            " consecutive_losses INTEGER NOT NULL DEFAULT 0, daily_trades INTEGER NOT NULL DEFAULT 0,"
            " daily_pnl REAL NOT NULL DEFAULT 0, last_reset_date TEXT NOT NULL,"
            " is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT, updated_at TEXT NOT NULL)")


def risk_engine(tmp, **over):
    """A RiskEngine on a temp ledger with the schema in place, so no test touches the production DB."""
    db = os.path.join(tmp, "r.db")
    conn = sqlite3.connect(db)
    conn.execute(RISK_DDL)
    conn.commit()
    conn.close()
    base = {"db_file": db, "max_trades_per_day": 20, "max_consecutive_losses": 3,
            "risk_per_trade_pct": 0.015, "kelly_fraction": 0.25, "fee_pct_round_trip": 0.0008,
            "min_notional": 5.5, "max_notional": 25.0, "max_drawdown_pct": 0.2}
    base.update(over)
    return RiskEngine(base)


class FakeClient:
    def __init__(self, balance=100.0, can_trade=True, symbols=3, fail=None):
        self.balance = balance
        self.can_trade = can_trade
        self.symbols = symbols
        self.fail = fail

    def account(self):
        if self.fail == "account":
            raise RuntimeError("HTTP 401")
        return {"canTrade": self.can_trade,
                "assets": [{"asset": "USDT", "availableBalance": str(self.balance)}]}

    def exchange_info(self):
        if self.fail == "info":
            raise RuntimeError("HTTP 503")
        return {"symbols": [{"symbol": f"S{i}USDT"} for i in range(self.symbols)]}


class HbOfflineTest(unittest.TestCase):
    """test_live_refuses_when_hb_offline_ne_0"""

    def test_hb_offline_1_is_never_live(self):
        c = LiveConfig.from_env(env(HB_OFFLINE=1, LIVE_MODE=0)).validate()
        self.assertFalse(c.is_live)
        self.assertIn("not a live instance", " ".join(c.notes))

    def test_live_mode_and_hb_offline_together_are_refused(self):
        with self.assertRaises(LiveConfigError):
            LiveConfig.from_env(env(HB_OFFLINE=1, LIVE_MODE=1)).validate()

    def test_without_live_mode_nothing_is_gated(self):
        c = LiveConfig.from_env(env(LIVE_MODE=0)).validate()
        self.assertFalse(c.is_live)
        self.assertTrue(live_guard.family_gate("volume", c)[0])

    def test_hb_offline_0_is_live(self):
        self.assertTrue(cfg().is_live)

    def test_the_unit_refuses_to_start_unless_hb_offline_is_0(self):
        text = UNIT_LIVE.read_text()
        self.assertIn('test \\"$HB_OFFLINE\\" = 0', text)
        self.assertNotIn('test \\"$HB_OFFLINE\\" = 1', text)

    def test_live_service_execstartpre_requires_hb_offline_0(self):
        """The live unit must refuse to start when HB_OFFLINE is anything but 0."""
        text = UNIT_LIVE.read_text()
        pre = [ln for ln in text.splitlines() if ln.startswith("ExecStartPre=")]
        self.assertEqual(len(pre), 1, f"expected exactly one ExecStartPre, got {pre}")
        self.assertIn('test \\"$HB_OFFLINE\\" = 0', pre[0],
                      f"live ExecStartPre does not require HB_OFFLINE=0: {pre[0]}")


class TestnetUrlTest(unittest.TestCase):
    """test_live_uses_testnet_url_when_testnet_flag"""

    def test_testnet_flag_selects_the_testnet_host(self):
        self.assertEqual(cfg(BINANCE_TESTNET=1).base_url, live_guard.FUTURES_TESTNET)

    def test_mainnet_stage_selects_the_mainnet_host(self):
        c = cfg(BINANCE_TESTNET=0, LIVE_STAGE=2, LIVE_STAGE_CONFIRMED=1)
        self.assertEqual(c.base_url, live_guard.FUTURES_MAINNET)

    def test_an_explicit_base_url_wins(self):
        c = cfg(BINANCE_FUTURES_BASE_URL="https://example.invalid/")
        self.assertEqual(c.base_url, "https://example.invalid")

    def test_stage_1_refuses_mainnet(self):
        with self.assertRaises(LiveConfigError):
            cfg(BINANCE_TESTNET=0)


class FamilyGateTest(unittest.TestCase):
    """test_only_winning_family_places_orders"""

    def test_the_configured_family_passes(self):
        ok, _ = live_guard.family_gate("exhaustion", cfg())
        self.assertTrue(ok)

    def test_another_family_is_refused(self):
        ok, reason = live_guard.family_gate("volume", cfg())
        self.assertFalse(ok)
        self.assertIn("not the live family", reason)

    def test_an_empty_family_refuses_every_order(self):
        """Empty family AND empty regimes: nothing would gate an order, so the config is refused."""
        c = LiveConfig.from_env(env(LIVE_STRATEGY_FAMILY="", LIVE_WINNING_REGIMES=""))
        with self.assertRaises(LiveConfigError):
            c.validate()

    def test_an_empty_family_is_allowed_when_a_regime_set_exists(self):
        c = cfg(LIVE_STRATEGY_FAMILY="")
        self.assertTrue(c.is_live)
        self.assertIn("regime gate only", " ".join(c.notes))
        self.assertTrue(live_guard.family_gate("volume", c)[0])

    def test_compare_mode_keeps_the_gate_open(self):
        c = LiveConfig.from_env(env(LIVE_MODE=0)).validate()
        ok, _ = live_guard.family_gate("volume", c)
        self.assertTrue(ok)

    def test_the_winner_rule_is_deterministic(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "s.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE signal_outcomes (family TEXT, resolved INT, fwd_ret REAL)")
        conn.executemany("INSERT INTO signal_outcomes VALUES (?,?,?)",
                         [("good", 1, 0.01)] * 120 + [("bad", 1, -0.01)] * 120 + [("thin", 1, 0.05)] * 10)
        conn.commit()
        conn.close()
        self.assertEqual(live_guard.select_winning_family(db), "good")
        self.assertEqual(live_guard.select_winning_family(db), "good")

    def test_no_family_qualifies_on_a_losing_book(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "s.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE signal_outcomes (family TEXT, resolved INT, fwd_ret REAL)")
        conn.executemany("INSERT INTO signal_outcomes VALUES (?,?,?)",
                         [("a", 1, -0.002)] * 200 + [("b", 1, -0.004)] * 200)
        conn.commit()
        conn.close()
        self.assertIsNone(live_guard.select_winning_family(db))


class NotionalCapTest(unittest.TestCase):
    """test_notional_cap_enforced"""

    def test_a_notional_above_the_stage_cap_is_refused(self):
        with self.assertRaises(LiveConfigError):
            cfg(LIVE_MAX_NOTIONAL_USDT=500)

    def test_the_risk_engine_clamps_to_the_cap(self):
        tmp = tempfile.mkdtemp()
        c = cfg(LIVE_MAX_NOTIONAL_USDT=25)
        risk = risk_engine(tmp, **c.risk_overrides())
        notional, status = risk.approve_trade(10000.0, 0.01)
        self.assertEqual(status, "approved")
        self.assertLessEqual(notional, 25.0)


class DailyLossTest(unittest.TestCase):
    """test_daily_loss_limit_halts"""

    def test_the_daily_loss_limit_halts_the_engine(self):
        tmp = tempfile.mkdtemp()
        c = cfg(LIVE_DAILY_LOSS_LIMIT_USDT=20)
        risk = risk_engine(tmp, **c.risk_overrides())
        risk.daily_pnl = -20.0
        notional, status = risk.approve_trade(1000.0, 0.01)
        self.assertIsNone(notional)
        self.assertIn("daily_loss_limit", status)
        self.assertTrue(risk.is_halted)

    def test_below_the_limit_it_still_approves(self):
        tmp = tempfile.mkdtemp()
        c = cfg(LIVE_DAILY_LOSS_LIMIT_USDT=20)
        risk = risk_engine(tmp, **c.risk_overrides())
        risk.daily_pnl = -19.99
        notional, status = risk.approve_trade(1000.0, 0.01)
        self.assertEqual(status, "approved")
        self.assertIsNotNone(notional)


class LeverageCapTest(unittest.TestCase):
    """test_leverage_cap_enforced"""

    def test_leverage_above_the_stage_cap_is_refused(self):
        with self.assertRaises(LiveConfigError):
            cfg(LIVE_LEVERAGE=10)

    def test_the_stage_cap_reaches_the_risk_engine(self):
        # FAZ14: kademe tavani artik LIVE_MAX_LEVERAGE'dir; LIVE_LEVERAGE sabit kirpma yapmaz.
        self.assertEqual(cfg(LIVE_MAX_LEVERAGE=5).risk_overrides()["max_leverage"], 5)
        self.assertEqual(cfg(LIVE_MAX_LEVERAGE=3).risk_overrides()["max_leverage"], 3)

    def test_stage_4_allows_five(self):
        c = cfg(BINANCE_TESTNET=0, LIVE_STAGE=4, LIVE_STAGE_CONFIRMED=1, LIVE_LEVERAGE=5,
                LIVE_MAX_NOTIONAL_USDT=150, LIVE_DAILY_LOSS_LIMIT_USDT=150, LIVE_MAX_OPEN_POSITIONS=4)
        self.assertEqual(c.risk_overrides()["max_leverage"], 5)


class MaxPositionsTest(unittest.TestCase):
    """test_max_open_positions_enforced"""

    def test_positions_above_the_stage_cap_are_refused(self):
        with self.assertRaises(LiveConfigError):
            cfg(LIVE_MAX_OPEN_POSITIONS=9)

    def test_the_stage_cap_is_reported(self):
        self.assertEqual(cfg(LIVE_MAX_OPEN_POSITIONS=2).max_open_positions, 2)


class StateIsolationTest(unittest.TestCase):
    """test_state_dir_isolated_between_compare_and_live"""

    def test_the_two_units_use_different_state_dirs(self):
        compare = (ROOT / "deploy" / "auto_trader_faz9.service").read_text()
        live = UNIT_LIVE.read_text()
        self.assertIn("HOME=/var/lib/auto_trader_faz9", compare)
        self.assertIn("HOME=/var/lib/auto_trader_faz9_live", live)
        self.assertNotIn("HOME=/var/lib/auto_trader_faz9\n", live)

    def test_live_and_compare_use_different_home_dirs(self):
        """State isolation: the live unit must never resolve HOME to the compare directory."""
        compare_home = [ln for ln in (ROOT / "deploy" / "auto_trader_faz9.service").read_text().splitlines()
                        if ln.startswith("Environment=HOME=")]
        live_home = [ln for ln in UNIT_LIVE.read_text().splitlines() if ln.startswith("Environment=HOME=")]
        self.assertEqual(compare_home, ["Environment=HOME=/var/lib/auto_trader_faz9"])
        self.assertEqual(live_home, ["Environment=HOME=/var/lib/auto_trader_faz9_live"])
        self.assertNotEqual(compare_home[0], live_home[0], "compare and live share a HOME directory")

    def test_the_two_units_use_different_env_files(self):
        compare = (ROOT / "deploy" / "auto_trader_faz9.service").read_text()
        live = UNIT_LIVE.read_text()
        self.assertIn("/etc/auto_trader/auto_trader.env", compare)
        self.assertIn("/etc/auto_trader/live.env", live)
        self.assertNotIn("auto_trader.env", live.replace("live.env", ""))

    def test_the_live_unit_keeps_the_hardening(self):
        live = UNIT_LIVE.read_text()
        for line in ("ProtectHome=true", "ProtectSystem=full", "PrivateTmp=true", "NoNewPrivileges=true",
                     "Restart=always", "RestartSec=10"):
            self.assertIn(line, live)


class RegimeGateTest(unittest.TestCase):
    """test_live_only_trades_winning_regimes / test_trend_signals_are_logged_not_traded /
    test_empty_winning_regimes_refuses_all_orders / test_compare_mode_ignores_regime_filter"""

    def test_live_only_trades_winning_regimes(self):
        c = cfg()
        self.assertTrue(live_guard.regime_gate("range", c)[0])
        self.assertTrue(live_guard.regime_gate("mid", c)[0])

    def test_trend_signals_are_logged_not_traded(self):
        ok, reason = live_guard.regime_gate("trend", cfg())
        self.assertFalse(ok)
        self.assertIn("not in winning set", reason)

    def test_empty_winning_regimes_refuses_all_orders(self):
        c = cfg(LIVE_WINNING_REGIMES="")
        self.assertEqual(c.winning_regimes, frozenset())
        for regime in ("range", "mid", "trend"):
            self.assertFalse(live_guard.regime_gate(regime, c)[0], regime)

    def test_compare_mode_ignores_regime_filter(self):
        c = LiveConfig.from_env(env(LIVE_MODE=0, LIVE_WINNING_REGIMES="")).validate()
        self.assertTrue(live_guard.regime_gate("trend", c)[0])

    def test_the_default_set_is_range_and_mid(self):
        e = env()
        e.pop("LIVE_WINNING_REGIMES")
        self.assertEqual(LiveConfig.from_env(e).winning_regimes, frozenset({"range", "mid"}))

    def test_live_winning_regimes_default(self):
        """Unset LIVE_WINNING_REGIMES must default to exactly {range, mid}, not to everything."""
        e = env()
        e.pop("LIVE_WINNING_REGIMES")
        c = LiveConfig.from_env(e).validate()
        self.assertEqual(c.winning_regimes, frozenset({"range", "mid"}),
                         f"default winning regimes are {sorted(c.winning_regimes)}, expected ['mid', 'range']")

    def test_live_refuses_trend_orders(self):
        """A trend signal must be refused in live mode even when its family is the live family."""
        c = cfg(LIVE_STRATEGY_FAMILY="")
        self.assertTrue(live_guard.family_gate("exhaustion", c)[0])
        ok, reason = live_guard.regime_gate("trend", c)
        self.assertFalse(ok, "trend regime was allowed to place an order in live mode")
        self.assertIn("not in winning set", reason)

    def test_the_set_is_parsed_case_and_space_insensitively(self):
        self.assertEqual(cfg(LIVE_WINNING_REGIMES=" Range , MID ").winning_regimes,
                         frozenset({"range", "mid"}))

    def test_the_template_carries_the_key(self):
        text = (ROOT / "deploy" / "live.env.template").read_text()
        self.assertIn("LIVE_WINNING_REGIMES=range,mid", text)


class StartupCheckTest(unittest.TestCase):
    """test_startup_balance_check_fail_closed"""

    def test_a_zero_balance_refuses_the_start(self):
        with self.assertRaises(LiveConfigError):
            live_guard.startup_check(FakeClient(balance=0.0), cfg())

    def test_startup_check_fail_closed(self):
        """Any startup failure must raise, never return a usable result."""
        for client in (FakeClient(balance=0.0), FakeClient(fail="account"), FakeClient(fail="info"),
                       FakeClient(can_trade=False)):
            with self.assertRaises(LiveConfigError):
                live_guard.startup_check(client, cfg())

    def test_account_error_refuses_start(self):
        with self.assertRaises(LiveConfigError) as ctx:
            live_guard.startup_check(FakeClient(fail="account"), cfg())
        self.assertIn("account() failed", str(ctx.exception))

    def test_exchange_info_error_refuses_start(self):
        with self.assertRaises(LiveConfigError) as ctx:
            live_guard.startup_check(FakeClient(fail="info"), cfg())
        self.assertIn("exchangeInfo() failed", str(ctx.exception))

    def test_an_account_error_refuses_the_start(self):
        with self.assertRaises(LiveConfigError):
            live_guard.startup_check(FakeClient(fail="account"), cfg())

    def test_an_exchange_info_error_refuses_the_start(self):
        with self.assertRaises(LiveConfigError):
            live_guard.startup_check(FakeClient(fail="info"), cfg())

    def test_can_trade_false_refuses_the_start(self):
        with self.assertRaises(LiveConfigError):
            live_guard.startup_check(FakeClient(can_trade=False), cfg())

    def test_a_healthy_account_passes(self):
        out = live_guard.startup_check(FakeClient(balance=250.0), cfg())
        self.assertEqual(out["balance_usdt"], 250.0)
        self.assertEqual(out["symbols"], 3)
        self.assertTrue(out["testnet"])


if __name__ == "__main__":
    unittest.main()
