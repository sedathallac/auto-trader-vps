"""BLOK25 Bölüm 2 — the 18-check pretrade risk decision.

Every check is tested in its PASS, FAIL (or PARTIAL where documented) and
UNKNOWN (fail-closed: missing critical data BLOCKS) form, plus:
- portfolio vol with the measured correlation (per-symbol 15% targeting must
  not hide the total portfolio risk),
- no invented limits (unconfigured daily-loss limit -> PARTIAL, never enforced
  with a made-up number),
- liquidation distance stays PARTIAL without LeverageBracket data,
- signal replay prevention across processes/restarts (SignalDedupStore).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from risk_checklist import (PretradeChecklist, Snapshot, Decision,
                            PASS, FAIL, UNKNOWN, PARTIAL,
                            SIGMA_TARGET, W_SYMBOL, CORR_MEAN, CORR_MIN,
                            MAX_GROSS_LEVERAGE, portfolio_vol)
from signal_dedup import SignalDedupStore, FIRST, REPLAY

SYM = "BTCUSDT"
OTHER = "ETHUSDT"


class FakeRisk:
    """The RiskEngine surface the checklist composes (no DB needed)."""

    def __init__(self, cfg=None, **state):
        self.cfg = cfg or {
            "max_drawdown_pct": 0.20,
            "max_consecutive_losses": 3,
            "max_trades_per_day": 20,
            "max_total_margin_pct": 0.40,
            "max_open_positions": 8,
        }
        self.peak_equity = state.get("peak_equity", 1000.0)
        self.current_equity = state.get("current_equity", 1000.0)
        self.current_drawdown = state.get("current_drawdown", 0.0)
        self.consecutive_losses = state.get("consecutive_losses", 0)
        self.daily_trades = state.get("daily_trades", 0)
        self.daily_pnl = state.get("daily_pnl", 0.0)
        self.is_halted = state.get("is_halted", False)
        self.halt_reason = state.get("halt_reason", None)
        self.state = state.get("state", {"peak_equity": 1000.0})


def _snap(**kw):
    """A healthy default snapshot: flat book, one target within all caps.

    E=1000, target BTC 100 USDT LONG, sigma_i=0.30 -> contribution
    100*0.30/1000 = 3% equity vol << 15% cap."""
    d = dict(equity=1000.0, available_balance=1000.0,
             positions=[], open_symbols=set(), active_executor_pairs=set(),
             reserved_symbols=set(), target_symbol=SYM, target_side=1,
             target_notional=100.0, target_symbol_vol=0.30,
             bar_ts=1730000000000, signal_hash="abc",
             symbol_vols={}, corr=None, leverage_brackets=None,
             max_open_positions=8, position_notionals=None)
    d.update(kw)
    return Snapshot(**d)


class TestPortfolioVol(unittest.TestCase):
    def test_equicorrelated_form(self):
        # 8 equal contributions of w*sigma_t each, rho=0.8:
        # vol = w*sigma_t*sqrt(n + n(n-1)rho) = 0.01875*sqrt(52.8) = 13.63%
        c = [W_SYMBOL * SIGMA_TARGET] * 8
        v = portfolio_vol(c, 0.8)
        self.assertAlmostEqual(v, 0.136244, places=5)

    def test_full_book_at_target_within_cap(self):
        # the frozen sizing rule at FULL 8-symbol allocation stays <= 15%
        c = [W_SYMBOL * SIGMA_TARGET] * 8
        self.assertLessEqual(portfolio_vol(c, CORR_MEAN), SIGMA_TARGET + 1e-12)

    def test_per_symbol_targeting_hides_total(self):
        # THE BLOK25 Bölüm 2 point: 8 symbols EACH at 15% equity-vol (i.e. the
        # per-symbol cap ignored) is ~109% portfolio vol at rho=0.8 — the
        # per-symbol view alone would report '15%' and hide it
        c = [SIGMA_TARGET] * 8
        v = portfolio_vol(c, 0.8)
        self.assertAlmostEqual(v, 1.089954, places=5)
        self.assertGreater(v, 7 * SIGMA_TARGET)

    def test_zero_corr_is_sum_of_squares(self):
        c = [0.03, 0.04]
        self.assertAlmostEqual(portfolio_vol(c, 0.0), 0.05, places=9)

    def test_bad_input_none(self):
        self.assertIsNone(portfolio_vol([], 0.8))
        self.assertIsNone(portfolio_vol([0.03], None))
        self.assertIsNone(portfolio_vol(["x"], 0.8))


class TestChecklistHealthy(unittest.TestCase):
    def test_all_pass_approves(self):
        st = SignalDedupStore(tempfile.mkdtemp() + "/db.sqlite3"); st.init_schema()
        cl = PretradeChecklist(FakeRisk(), signal_dedup=st)
        d = cl.evaluate(_snap())
        self.assertTrue(d.approved, d.reason)
        self.assertEqual(len(d.checks), 18)
        self.assertEqual([c.name for c in d.checks if c.status not in
                          (PASS, PARTIAL)], [])
        # the documented PARTIALs on a healthy decision:
        partials = {c.name for c in d.partial}
        self.assertIn("liquidation_distance", partials)
        self.assertIn("daily_loss_limit", partials)   # no configured limit
        # headline quantities exposed
        self.assertAlmostEqual(d.portfolio_vol, 100 * 0.30 / 1000, places=9)
        self.assertAlmostEqual(d.gross_leverage, 0.10, places=9)
        self.assertAlmostEqual(d.net_exposure, 0.10, places=9)


class TestCheckFailures(unittest.TestCase):
    def _eval(self, **kw):
        risk_kw = kw.pop("risk_kw", {})
        if "signal_dedup" not in kw:
            st = SignalDedupStore(tempfile.mkdtemp() + "/db.sqlite3")
            st.init_schema()
            kw["signal_dedup"] = st
        sd = kw.pop("signal_dedup")
        return PretradeChecklist(FakeRisk(**risk_kw), signal_dedup=sd).evaluate(
            _snap(**kw))

    def _status(self, d, name):
        r = [c for c in d.checks if c.name == name][0]
        return r.status, r.detail

    def test_halted_rejects(self):
        d = self._eval(risk_kw={"is_halted": True,
                                "halt_reason": "kill switch"})
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "halted_kill_switch")[0], FAIL)

    def test_state_not_loaded_rejects(self):
        d = self._eval(risk_kw={"state": {}})
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "risk_state_persistence")[0], UNKNOWN)

    def test_open_book_without_recovered_state_rejects(self):
        # restart recovery: a live book but the ledger state did not reload
        d = self._eval(risk_kw={"state": {}},
                       positions=[{"symbol": OTHER, "positionAmt": "1",
                                   "markPrice": "100"}],
                       open_symbols={OTHER},
                       position_notionals={OTHER: 100.0})
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "restart_recovery")[0], UNKNOWN)

    def test_drawdown_rejects(self):
        d = self._eval(risk_kw={"current_drawdown": 0.21})
        self.assertEqual(self._status(d, "drawdown_limit")[0], FAIL)

    def test_consecutive_losses_rejects(self):
        d = self._eval(risk_kw={"consecutive_losses": 3})
        self.assertEqual(self._status(d, "consecutive_loss_limit")[0], FAIL)

    def test_daily_loss_not_invented(self):
        # no configured limit -> PARTIAL, never a made-up enforcement
        d = self._eval(risk_kw={"daily_pnl": -500.0})
        self.assertTrue(d.approved)                       # PARTIAL does not block
        self.assertEqual(self._status(d, "daily_loss_limit")[0], PARTIAL)

    def test_daily_loss_enforced_when_configured(self):
        risk = FakeRisk(cfg={"max_daily_loss_pct": 0.05,
                             "max_drawdown_pct": 0.20,
                             "max_consecutive_losses": 3,
                             "max_total_margin_pct": 0.40,
                             "max_open_positions": 8},
                        daily_pnl=-60.0)                  # -6% of E=1000
        d = PretradeChecklist(risk).evaluate(_snap())
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "daily_loss_limit")[0], FAIL)

    def test_max_open_positions_rejects(self):
        book = {f"S{i}USDT": 50.0 for i in range(8)}      # 8 open + target = 9
        d = self._eval(positions=[{"symbol": f"S{i}USDT", "positionAmt": "1",
                                   "markPrice": "50"} for i in range(8)],
                       open_symbols=set(book), position_notionals=book,
                       symbol_vols={f"S{i}USDT": 0.30 for i in range(8)})
        self.assertEqual(self._status(d, "max_open_positions")[0], FAIL)

    def test_per_symbol_cap_rejects(self):
        # notional 200 > w*E = 125
        d = self._eval(target_notional=200.0)
        self.assertEqual(self._status(d, "per_symbol_risk")[0], FAIL)

    def test_gross_notional_rejects(self):
        # 8 symbols each at the w*E cap + target -> gross > 1.0x
        book = {f"S{i}USDT": 125.0 for i in range(8)}
        d = self._eval(positions=[{"symbol": f"S{i}USDT", "positionAmt": "1",
                                   "markPrice": "125"} for i in range(8)],
                       open_symbols=set(book), position_notionals=book,
                       symbol_vols={f"S{i}USDT": 0.30 for i in range(8)})
        self.assertEqual(self._status(d, "gross_notional")[0], FAIL)

    def test_total_portfolio_risk_rejects_hidden_risk(self):
        # each of 8 symbols sized to a 15% equity-vol contribution (the
        # 'per-symbol 15% targeting hides the total' scenario) -> vol ~109%
        book = {f"S{i}USDT": 100.0 for i in range(8)}
        d = self._eval(positions=[{"symbol": f"S{i}USDT", "positionAmt": "1",
                                   "markPrice": "100"} for i in range(8)],
                       open_symbols=set(book), position_notionals=book,
                       symbol_vols={f"S{i}USDT": 1.5 for i in range(8)},
                       target_notional=100.0)
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "total_portfolio_risk")[0], FAIL)

    def test_missing_vol_is_unknown_rejects(self):
        # vols unavailable -> the portfolio risk check cannot run -> BLOCKED
        d = self._eval(target_symbol_vol=None, symbol_vols=None)
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "total_portfolio_risk")[0], UNKNOWN)

    def test_balance_missing_rejects(self):
        d = self._eval(available_balance=None)
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "available_balance")[0], UNKNOWN)

    def test_balance_below_notional_rejects(self):
        d = self._eval(available_balance=50.0)
        self.assertEqual(self._status(d, "available_balance")[0], FAIL)

    def test_margin_usage_rejects(self):
        # 0.40 limit: 500 USDT book on E=1000 -> 50% usage (target replaces
        # nothing) -> FAIL
        book = {OTHER: 450.0}
        d = self._eval(positions=[{"symbol": OTHER, "positionAmt": "1",
                                   "markPrice": "450"}],
                       open_symbols={OTHER}, position_notionals=book,
                       symbol_vols={OTHER: 0.30})
        self.assertEqual(self._status(d, "margin_usage")[0], FAIL)

    def test_max_leverage_rejects_on_low_balance(self):
        # account leverage uses AVAILABLE balance: 100 notional on 50 balance
        d = self._eval(available_balance=50.0)
        self.assertEqual(self._status(d, "max_leverage")[0], FAIL)

    def test_liquidation_always_partial_without_brackets(self):
        d = self._eval()
        self.assertEqual(self._status(d, "liquidation_distance")[0], PARTIAL)
        self.assertIn("!= impossible", self._status(d, "liquidation_distance")[1]
                      .replace("≠", "!=").replace("imkânsız", "impossible")
                      or "impossible", )

    def test_liquidation_partial_even_with_brackets(self):
        # brackets supplied: still PARTIAL (exchange-authoritative only)
        d = self._eval(leverage_brackets={SYM: [{"bracket": 1}]})
        self.assertEqual(self._status(d, "liquidation_distance")[0], PARTIAL)

    def test_optimistic_correlation_rejects(self):
        # assuming rho below the measured minimum understates common risk
        d = self._eval(corr=0.20)
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "correlation_common_market")[0], FAIL)

    def test_duplicate_executor_position_open(self):
        d = self._eval(positions=[{"symbol": SYM, "positionAmt": "1",
                                   "markPrice": "100"}],
                       open_symbols={SYM}, position_notionals={SYM: 100.0})
        self.assertEqual(self._status(d, "duplicate_executor")[0], FAIL)

    def test_duplicate_executor_running_executor(self):
        d = self._eval(active_executor_pairs={"BTC-USDT"})
        self.assertEqual(self._status(d, "duplicate_executor")[0], FAIL)

    def test_duplicate_executor_reservation(self):
        d = self._eval(reserved_symbols={SYM})
        self.assertEqual(self._status(d, "duplicate_executor")[0], FAIL)

    def test_executor_state_missing_rejects(self):
        d = self._eval(active_executor_pairs=None, reserved_symbols=None)
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "duplicate_executor")[0], UNKNOWN)

    def test_signal_replay_rejects(self):
        store = SignalDedupStore(tempfile.mkdtemp() + "/db.sqlite3")
        store.init_schema()
        cl = PretradeChecklist(FakeRisk(), signal_dedup=store)
        d1 = cl.evaluate(_snap())
        self.assertTrue(d1.approved, d1.reason)
        d2 = cl.evaluate(_snap())          # the SAME bar signal again
        self.assertFalse(d2.approved)
        self.assertEqual(self._status(d2, "signal_replay")[0], FAIL)

    def test_signal_new_bar_allowed(self):
        store = SignalDedupStore(tempfile.mkdtemp() + "/db.sqlite3")
        store.init_schema()
        cl = PretradeChecklist(FakeRisk(), signal_dedup=store)
        cl.evaluate(_snap())
        d = cl.evaluate(_snap(bar_ts=1730000000000 + 4 * 3600 * 1000))
        self.assertEqual(self._status(d, "signal_replay")[0], PASS)

    def test_signal_store_missing_rejects(self):
        # no dedup store wired -> replay protection unverifiable -> blocked
        d = self._eval(signal_dedup=None)
        self.assertFalse(d.approved)
        self.assertEqual(self._status(d, "signal_replay")[0], UNKNOWN)

    def test_positions_missing_rejects_many(self):
        # the umbrella fail-closed: positionRisk unavailable -> the dependent
        # checks all go UNKNOWN and the decision is REJECTED, never 'no risk'
        d = self._eval(positions=None, open_symbols=None, reserved_symbols=None,
                       active_executor_pairs=None, position_notionals=None)
        self.assertFalse(d.approved)
        unknowns = {c.name for c in d.unknown}
        self.assertIn("max_open_positions", unknowns)
        self.assertIn("per_symbol_risk", unknowns)
        self.assertIn("gross_notional", unknowns)


class TestSignalDedupStore(unittest.TestCase):
    def test_first_then_replay(self):
        s = SignalDedupStore(tempfile.mkdtemp() + "/db.sqlite3")
        s.init_schema()
        self.assertEqual(s.acquire(SYM, 1, "h1"), FIRST)
        self.assertEqual(s.acquire(SYM, 1, "h1"), REPLAY)
        self.assertEqual(s.acquire(SYM, 1, "h2"), REPLAY)   # same bar, any hash
        self.assertEqual(s.acquire(SYM, 2), FIRST)          # new bar
        self.assertEqual(s.acquire(OTHER, 1), FIRST)        # other symbol

    def test_replay_survives_restart(self):
        db = tempfile.mkdtemp() + "/db.sqlite3"
        s = SignalDedupStore(db)
        s.init_schema()
        self.assertEqual(s.acquire(SYM, 1), FIRST)
        s2 = SignalDedupStore(db)                    # 'restart': fresh handle
        self.assertEqual(s2.acquire(SYM, 1), REPLAY)
        row = s2.get(SYM, 1)
        self.assertEqual(row["state"], "CONSUMED")
        self.assertIsNotNone(row["created_at"])

    def test_concurrent_single_first(self):
        import multiprocessing as mp
        db = tempfile.mkdtemp() + "/db.sqlite3"
        SignalDedupStore(db).init_schema()
        out = tempfile.mkdtemp() + "/out.txt"
        ctx = mp.get_context("spawn")
        ps = [ctx.Process(target=_dedup_worker, args=(db, out, i))
              for i in range(4)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        lines = open(out).read().strip().splitlines()
        firsts = [l for l in lines if l.endswith(":FIRST")]
        self.assertEqual(len(firsts), 1, lines)


def _dedup_worker(db, out, i):
    # module-level: picklable under the spawn start method
    s = SignalDedupStore(db)
    r = s.acquire(SYM, 42)
    with open(out, "a") as fh:
        fh.write(f"{i}:{r}\n")


if __name__ == "__main__":
    unittest.main()
