"""BLOK25 Bölüm 3 — H1 strategy adapter: signal / risk / executor /
reconciliation layers, tested separately and wired together.

The executable proof that the FORMULA did not change is
tests/test_blok25_h1_equivalence.py (identical flips vs the frozen
h1_engine.py on the 2022-2025 data). This file covers:
- frozen parameters and the fail-closed universe guard,
- the signal state machine (warmup, EWMA, flip-only, next-bar-open, gap
  carry, m==0 edge),
- §9 sizing incl. the fail-closed None paths,
- the router: 18-check risk decision, dedup replay, halt, reconciliation
  gate (unknown + unexplained drift block; explained drift reports but does
  not gate), warmup replay, journal measurement fields,
- the flip semantics added to PretradeChecklist (a flip REPLACES the symbol
  slot: not a duplicate; the closed leg releases margin),
- NullExecutor (the no-orders phase) records intents and cannot execute.
"""
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from h1_adapter import (H1SignalEngine, H1Router, H1Reconciler, NullExecutor,
                        H1Decision, h1_target_notional, h1_signal_hash,
                        param_manifest, STRATEGY_ID, STRATEGY_LABEL,
                        H1_SYMBOLS, LOOKBACK, HALFLIFE, ANN, LAMBDA)
from risk_checklist import (PretradeChecklist, Snapshot, Decision,
                            SIGMA_TARGET, W_SYMBOL, PASS, FAIL, UNKNOWN,
                            PARTIAL)
from signal_dedup import SignalDedupStore

SYM = "BTCUSDT"
OTHER = "ETHUSDT"
STEP = 4 * 3600 * 1000
T0 = 1730000000000


class FakeRisk:
    """The RiskEngine surface the checklist composes (Bölüm 2 test double)."""

    def __init__(self, **state):
        self.cfg = {"max_drawdown_pct": 0.20,
                    "max_consecutive_losses": 3,
                    "max_trades_per_day": 20,
                    "max_total_margin_pct": 0.40,
                    "max_open_positions": 8}
        self.peak_equity = state.get("peak_equity", 1000.0)
        self.current_equity = state.get("current_equity", 1000.0)
        self.current_drawdown = state.get("current_drawdown", 0.0)
        self.consecutive_losses = state.get("consecutive_losses", 0)
        self.daily_trades = state.get("daily_trades", 0)
        self.daily_pnl = state.get("daily_pnl", 0.0)
        self.is_halted = state.get("is_halted", False)
        self.halt_reason = state.get("halt_reason", None)
        self.state = state.get("state", {"peak_equity": 1000.0})


class FakeWorld:
    """World + Executor test double: apply_target actually moves the fake
    book (like the paper broker will), and records the call order."""

    def __init__(self, equity=1000.0, balance=1000.0, positions=None):
        self._equity = equity
        self._balance = balance
        self.positions = positions if positions is not None else []
        self.calls = []
        self.applied = []

    # World surface
    def equity(self):
        return self._equity

    def available_balance(self):
        return self._balance

    def active_executor_pairs(self):
        return set()

    def reserved_symbols(self):
        return set()

    def leverage_brackets(self):
        return None

    # broker hooks
    def settle_funding(self, symbol, upto_ts):
        self.calls.append(("settle_funding", symbol, upto_ts))

    def on_bar_close(self, symbol, close_px):
        self.calls.append(("on_bar_close", symbol))

    # Executor surface
    def apply_target(self, decision, ref_price):
        self.calls.append(("apply_target", decision.symbol, ref_price))
        self.applied.append(decision)
        amt = (decision.notional / ref_price) * decision.direction
        self.positions = ([p for p in self.positions
                           if p.get("symbol") != decision.symbol]
                          + [{"symbol": decision.symbol,
                              "positionAmt": str(amt),
                              "markPrice": str(ref_price)}])
        return {"executed": True, "mode": "FAKE"}


def _router(world=None, risk=None, execute=True, db=None,
            require_reconciliation=True):
    world = world if world is not None else FakeWorld()
    store = SignalDedupStore(db or tempfile.mkdtemp() + "/db.sqlite3")
    store.init_schema()
    cl = PretradeChecklist(risk or FakeRisk(), signal_dedup=store)
    engine = H1SignalEngine()
    router = H1Router(engine, cl, world, world,
                      require_reconciliation=require_reconciliation,
                      execute=execute)
    return router


def _feed(router, symbol, closes, ts0=T0):
    """Feed 4h bars (open == close simplification) through the router."""
    ts = ts0
    decisions = []
    for c in closes:
        d = router.run_bar(symbol, ts, c, c)
        if d is not None:
            decisions.append(d)
        ts += STEP
    return decisions


def _rising(n, start=100.0):
    return [start + i for i in range(n)]


def _falling(n, start):
    return [start - i for i in range(n)]


# ---------------------------------------------------------------------------
class TestFrozenParams(unittest.TestCase):
    def test_label_and_id(self):
        self.assertEqual(STRATEGY_LABEL, "RESEARCH_VALIDATION_POSITIVE_OOS_UNPROVEN")
        self.assertEqual(LOOKBACK, 180)
        self.assertEqual(HALFLIFE, 42.0)
        self.assertEqual(len(H1_SYMBOLS), 8)
        self.assertAlmostEqual(W_SYMBOL, 1.0 / 8.0)
        self.assertAlmostEqual(SIGMA_TARGET, 0.15)

    def test_param_manifest_hash_stable(self):
        m = param_manifest()
        import json as _json
        import hashlib as _h
        payload = {k: v for k, v in m.items() if k != "param_hash"}
        self.assertEqual(m["param_hash"],
                         _h.sha256(_json.dumps(payload, sort_keys=True)
                                   .encode()).hexdigest())
        self.assertEqual(m["lookback_bars"], 180)
        self.assertEqual(m["label"], STRATEGY_LABEL)
        # a change to ANY frozen number changes the hash
        payload["lookback_bars"] = 181
        self.assertNotEqual(m["param_hash"],
                            _h.sha256(_json.dumps(payload, sort_keys=True)
                                      .encode()).hexdigest())

    def test_unknown_symbol_fail_closed(self):
        eng = H1SignalEngine()
        with self.assertRaises(ValueError):
            eng.end_bar("FOOUSDT", T0, 100.0)
        r = _router()
        with self.assertRaises(ValueError):
            r.run_bar("FOOUSDT", T0, 100.0, 100.0)


class TestSignalEngine(unittest.TestCase):
    def _engine(self):
        return H1SignalEngine()

    def _feed_engine(self, eng, symbol, closes, ts0=T0):
        """Engine-level bar feed mirroring the router order: begin_bar(open)
        then end_bar(close) — a pending created at bar t is consumed at the
        open of bar t+1, exactly like the live flow."""
        ts = ts0
        pends, execs = [], []
        for c in closes:
            d = eng.begin_bar(symbol, c)
            if d is not None:
                execs.append(d)
            p = eng.end_bar(symbol, ts, c)
            if p is not None:
                pends.append(p)
            ts += STEP
        return pends, execs

    def test_no_signal_before_warmup(self):
        eng = self._engine()
        # 179 bars: no momentum window possible yet
        pends, execs = self._feed_engine(eng, SYM, _rising(179))
        self.assertEqual(pends, [])
        self.assertEqual(execs, [])
        self.assertFalse(eng.warm(SYM))
        # bar 180 (the 180th) makes the window available for the NEXT close,
        # but no signal has fired yet (i max = 179)
        pends, execs = self._feed_engine(eng, SYM, [279.0],
                                         ts0=T0 + 179 * STEP)
        self.assertEqual(pends, [])
        self.assertTrue(eng.warm(SYM))
        # bar i=180, var>0 -> first pending flip at the close, executed at
        # the NEXT bar open
        pends, execs = self._feed_engine(eng, SYM, _rising(2, start=280.0),
                                         ts0=T0 + 180 * STEP)
        self.assertEqual([p.direction for p in pends], [1])
        self.assertEqual([e.direction for e in execs], [1])
        self.assertTrue(eng.warm(SYM))

    def test_ewma_matches_reference(self):
        eng = self._engine()
        closes = [100.0 * (1.0 + 0.01 * ((i % 7) - 3)) for i in range(60)]
        self._feed_engine(eng, SYM, closes)
        var, prev = None, None
        for c in closes:
            if prev is not None:
                r = math.log(c / prev)
                var = r * r if var is None else LAMBDA * var + (1 - LAMBDA) * r * r
            prev = c
        self.assertAlmostEqual(eng.sigma(SYM), math.sqrt(var) * ANN, places=12)

    def test_flip_only_no_retrade_while_trend_holds(self):
        eng = self._engine()
        # 400 rising bars: ONE long entry, then no further decisions
        pends, execs = self._feed_engine(eng, SYM, _rising(400))
        self.assertEqual([p.direction for p in pends], [1])
        self.assertEqual([e.direction for e in execs], [1])
        self.assertEqual(eng.direction(SYM), 1)
        # further rising bars stay quiet — flip-only, no re-sizing/re-entry
        pends, execs = self._feed_engine(eng, SYM, _rising(50, start=500.0),
                                         ts0=T0 + 400 * STEP)
        self.assertEqual(pends, [])
        self.assertEqual(execs, [])

    def test_direction_flip_short(self):
        eng = self._engine()
        pends, execs = self._feed_engine(eng, SYM, _rising(182))
        self.assertEqual([e.direction for e in execs], [1])
        # falling bars: m < 0 -> exactly one SHORT flip decision + execution
        pends, execs = self._feed_engine(eng, SYM, _falling(181, start=282.0),
                                         ts0=T0 + 182 * STEP)
        self.assertEqual([p.direction for p in pends], [-1])
        self.assertEqual([e.direction for e in execs], [-1])
        self.assertEqual(eng.direction(SYM), -1)

    def test_next_bar_open_semantics(self):
        eng = self._engine()
        # feed exactly through the decision bar: the pending exists but is
        # NOT executable at the decision bar itself — only at the NEXT open
        pends, execs = self._feed_engine(eng, SYM, _rising(181))
        self.assertEqual(pends[0].bar_ts, T0 + 180 * STEP)
        self.assertEqual(execs, [])
        d = eng.begin_bar(SYM, 281.0)
        self.assertEqual(d.bar_ts, T0 + 180 * STEP)
        self.assertEqual(d.signal_hash,
                         h1_signal_hash(SYM, d.bar_ts, 1, d.sigma_i))
        # consumed exactly once
        self.assertIsNone(eng.begin_bar(SYM, 282.0))

    def test_gap_carries_pending_to_next_available_open(self):
        # prereg §7: if the next bar is missing, the flip fills at the next
        # AVAILABLE open — the pending survives until executed
        eng = self._engine()
        # feed exactly through the decision bar (pending created at the close
        # of bar 180, NOT yet consumed by any begin_bar)
        pends, execs = self._feed_engine(eng, SYM, _rising(181))
        self.assertEqual(pends[0].bar_ts, T0 + 180 * STEP)
        self.assertEqual(execs, [])
        # bar 181 entirely MISSING (no open, no close); bar 182's open is the
        # next available open -> the pending executes there, unchanged
        d = eng.begin_bar(SYM, 282.0)
        self.assertIsNotNone(d)
        self.assertEqual(d.direction, 1)
        self.assertEqual(d.bar_ts, T0 + 180 * STEP)

    def test_m_zero_keeps_current_direction(self):
        eng = self._engine()
        closes = _rising(182)
        self._feed_engine(eng, SYM, closes)
        eng.begin_bar(SYM, 282.0)
        self.assertEqual(eng.direction(SYM), 1)
        # a close EXACTLY equal to the close 180 bars back: m == 0 -> the
        # signal keeps the current direction -> no pending
        same = closes[2]
        self.assertIsNone(eng.end_bar(SYM, T0 + 182 * STEP, same))
        self.assertIsNone(eng.begin_bar(SYM, same))

    def test_sigma_annualized_and_warmup_none(self):
        eng = self._engine()
        self.assertIsNone(eng.sigma(SYM))       # not warmed
        self._feed_engine(eng, SYM, _rising(3))
        self.assertIsNotNone(eng.sigma(SYM))
        self.assertGreater(eng.sigma(SYM), 0)


class TestSizing(unittest.TestCase):
    def test_formula_uncapped(self):
        # E=1000, sigma=0.30: w*E*(0.15/0.30) = 62.5 (< cap 125)
        self.assertAlmostEqual(h1_target_notional(1000.0, 0.30), 62.5)

    def test_formula_capped_at_w_times_equity(self):
        # sigma=0.10 -> 187.5 raw, capped at w*E = 125
        self.assertAlmostEqual(h1_target_notional(1000.0, 0.10), 125.0)

    def test_fail_closed_none(self):
        self.assertIsNone(h1_target_notional(1000.0, None))
        self.assertIsNone(h1_target_notional(1000.0, 0.0))
        self.assertIsNone(h1_target_notional(None, 0.30))
        self.assertIsNone(h1_target_notional(0.0, 0.30))
        self.assertIsNone(h1_target_notional(-5.0, 0.30))


# ---------------------------------------------------------------------------
class TestRouter(unittest.TestCase):
    def test_healthy_entry_executed(self):
        r = _router()
        ds = _feed(r, SYM, _rising(183))
        self.assertEqual(len(ds), 1)
        self.assertEqual([e["outcome"] for e in r.journal], ["EXECUTED"])
        j = r.journal[0]
        self.assertEqual(j["symbol"], SYM)
        self.assertEqual(j["direction"], 1)
        self.assertEqual(j["label"], STRATEGY_LABEL)
        self.assertIsNotNone(j["signal_hash"])
        self.assertIsNotNone(j["notional"])
        self.assertLessEqual(j["notional"], W_SYMBOL * 1000.0 + 1e-9)
        # measurement fields for the Bölüm 4 paper run: signal at the close
        # of bar 180, fill at the open of bar 181 (next-bar-open)
        self.assertEqual(j["bar_ts"], T0 + 180 * STEP)      # signal time
        self.assertEqual(j["execution_ts"], T0 + 181 * STEP)  # fill time
        self.assertEqual(j["ref_price"], 281.0)              # the open
        self.assertTrue(j["checklist"].startswith("APPROVED"))
        self.assertIn("daily_loss_limit", j["checklist"])    # documented gaps
        self.assertIn("liquidation_distance", j["checklist"])
        self.assertEqual(len(r.world.applied), 1)
        self.assertEqual(r.engine.direction(SYM), 1)

    def test_funding_settles_before_flip(self):
        r = _router()
        _feed(r, SYM, _rising(183))
        calls = r.world.calls
        idx = next(i for i, c in enumerate(calls) if c[0] == "apply_target")
        # the flip executed at the open of bar 181: the funding settlement
        # for that bar ran FIRST (Binance semantics, h1_engine step order)
        self.assertEqual(calls[idx - 1],
                         ("settle_funding", SYM, T0 + 181 * STEP))

    def test_replay_of_same_decision_blocked(self):
        r = _router()
        ds = _feed(r, SYM, _rising(183))
        d = ds[0]
        d.execution_ts = T0 + 190 * STEP
        d.ref_price = 290.0
        r._execute(d, 290.0)                       # the SAME bar signal again
        self.assertEqual([e["outcome"] for e in r.journal],
                         ["EXECUTED", "BLOCKED"])
        self.assertIn("signal_replay", r.journal[1]["reason"])
        self.assertEqual(len(r.world.applied), 1)  # executed exactly once

    def test_halted_blocks_no_execution(self):
        r = _router(risk=FakeRisk(is_halted=True, halt_reason="kill switch"))
        _feed(r, SYM, _rising(183))
        self.assertEqual([e["outcome"] for e in r.journal], ["BLOCKED"])
        self.assertIn("halted_kill_switch", r.journal[0]["reason"])
        self.assertEqual(r.world.applied, [])
        # target advanced (signal state) but the book did not -> explained
        self.assertEqual(r.engine.direction(SYM), 1)

    def test_unusable_equity_misses_fail_closed(self):
        r = _router(world=FakeWorld(equity=0.0))
        _feed(r, SYM, _rising(183))
        self.assertEqual([e["outcome"] for e in r.journal],
                         ["MISSED_FAIL_CLOSED"])
        self.assertEqual(r.world.applied, [])

    def test_flip_replaces_slot_and_passes_duplicate(self):
        # LONG entry, then a flip to SHORT with the position still open:
        # duplicate_executor must PASS (replacement), not FAIL
        r = _router()
        _feed(r, SYM, _rising(183))
        self.assertEqual(r.journal[-1]["outcome"], "EXECUTED")
        ds = _feed(r, SYM, _falling(182, start=283.0),
                   ts0=T0 + 183 * STEP)
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].direction, -1)
        self.assertEqual(r.journal[-1]["outcome"], "EXECUTED",
                         r.journal[-1]["reason"])
        self.assertEqual(r.engine.direction(SYM), -1)
        # the fake book followed the flip
        amt = float(r.world.positions[0]["positionAmt"])
        self.assertLess(amt, 0)

    def test_balance_release_on_flip(self):
        # the flip CLOSES the existing ~78 USDT slot first: a 40 USDT balance
        # (below the ~81 target alone) still suffices with the release, and
        # would FAIL without it
        r = _router(world=FakeWorld())
        _feed(r, SYM, _rising(183))
        entry_notional = r.journal[0]["notional"]
        self.assertGreater(entry_notional, 60.0)     # a meaningful slot
        r.world._balance = 40.0                      # < target notional alone
        ds = _feed(r, SYM, _falling(182, start=283.0),
                   ts0=T0 + 183 * STEP)
        self.assertEqual(len(ds), 1)
        self.assertEqual(r.journal[-1]["outcome"], "EXECUTED",
                         r.journal[-1]["reason"])

    def test_unexplained_drift_blocks_other_symbols(self):
        # an ETH position nobody journaled (manual/unexplained) -> EVERY new
        # execution is blocked until reconciliation is verified again
        world = FakeWorld(positions=[{"symbol": OTHER,
                                      "positionAmt": "1",
                                      "markPrice": "100"}])
        r = _router(world=world)
        _feed(r, SYM, _rising(183))
        ds = _feed(r, SYM, _falling(182, start=283.0),
                   ts0=T0 + 183 * STEP)
        self.assertEqual([e["outcome"] for e in r.journal],
                         ["BLOCKED_RECONCILIATION",
                          "BLOCKED_RECONCILIATION"])
        self.assertIn("DRIFT", r.journal[-1]["reason"])
        self.assertIn("ETHUSDT", r.journal[-1]["reason"])
        self.assertEqual(r.world.applied, [])       # nothing traded

    def test_explained_drift_does_not_gate(self):
        # a BLOCKED flip is journaled -> the drift it leaves is EXPLAINED:
        # other symbols may still trade, and the symbol itself can flip again
        risk = FakeRisk(is_halted=True, halt_reason="kill switch")
        r = _router(risk=risk)
        _feed(r, SYM, _rising(183))                     # BLOCKED (halted)
        risk.is_halted = False
        # ETH first entry must be allowed despite BTC's explained drift
        ds = _feed(r, OTHER, _rising(183))
        self.assertEqual(len(ds), 1)
        self.assertEqual(r.journal[-1]["outcome"], "EXECUTED",
                         r.journal[-1]["reason"])

    def test_unreadable_positions_block(self):
        world = FakeWorld()
        world.positions = None                          # outage
        r = _router(world=world)
        _feed(r, SYM, _rising(183))
        self.assertEqual(r.journal[-1]["outcome"], "BLOCKED_RECONCILIATION")
        self.assertEqual(r.world.applied, [])

    def test_warmup_replay_skips_execution(self):
        r = _router(execute=False)
        ds = _feed(r, SYM, _rising(183))
        self.assertEqual(len(ds), 1)
        self.assertEqual([e["outcome"] for e in r.journal],
                         ["WARMUP_SKIPPED"])
        self.assertEqual(r.world.applied, [])
        # engine target state still rebuilt (restart recovery input)
        self.assertEqual(r.engine.direction(SYM), 1)
        # the skipped decision consumed NO dedup entry: a later live pass
        # over the same bar would still be first (see equivalence with
        # execute=True in the other tests)

    def test_journal_measurement_fields(self):
        r = _router()
        _feed(r, SYM, _rising(183))
        j = r.journal[0]
        for k in ["strategy", "label", "symbol", "bar_ts", "execution_ts",
                  "ref_price", "direction", "sigma_i", "notional",
                  "signal_hash", "outcome", "reason", "checklist",
                  "portfolio_vol", "gross_leverage", "net_exposure"]:
            self.assertIn(k, j)
        self.assertIsNotNone(j["portfolio_vol"])
        self.assertIsNotNone(j["gross_leverage"])


class TestFlipChecklistSemantics(unittest.TestCase):
    """The Bölüm 3 addition to PretradeChecklist: a flip REPLACES the symbol
    slot (default-False behavior from Bölüm 2 is unchanged)."""

    def _snap(self, **kw):
        d = dict(equity=1000.0, available_balance=1000.0,
                 positions=[], open_symbols=set(),
                 active_executor_pairs=set(), reserved_symbols=set(),
                 target_symbol=SYM, target_side=2, target_notional=80.0,
                 target_symbol_vol=0.30, bar_ts=1730000000000,
                 signal_hash="abc", symbol_vols={}, corr=None,
                 leverage_brackets=None, max_open_positions=8,
                 position_notionals=None, target_replaces_existing=False)
        d.update(kw)
        return Snapshot(**d)

    def _cl(self):
        st = SignalDedupStore(tempfile.mkdtemp() + "/db.sqlite3")
        st.init_schema()
        return PretradeChecklist(FakeRisk(), signal_dedup=st)

    def _status(self, d, name):
        return [c for c in d.checks if c.name == name][0]

    def test_flip_with_open_position_not_duplicate(self):
        d = self._cl().evaluate(self._snap(
            positions=[{"symbol": SYM, "positionAmt": "1",
                        "markPrice": "100"}],
            open_symbols={SYM}, position_notionals={SYM: 100.0},
            symbol_vols={SYM: 0.30},
            target_replaces_existing=True))
        self.assertEqual(self._status(d, "duplicate_executor").status, PASS)
        self.assertIn("flip replaces",
                      self._status(d, "duplicate_executor").detail)
        self.assertTrue(d.approved, d.reason)

    def test_non_flip_with_open_position_still_duplicate(self):
        # Bölüm 2 behavior unchanged: a NEW entry on an occupied symbol fails
        d = self._cl().evaluate(self._snap(
            positions=[{"symbol": SYM, "positionAmt": "1",
                        "markPrice": "100"}],
            open_symbols={SYM}, position_notionals={SYM: 100.0},
            symbol_vols={SYM: 0.30}))
        self.assertEqual(self._status(d, "duplicate_executor").status, FAIL)

    def test_flip_balance_release(self):
        d = self._cl().evaluate(self._snap(
            available_balance=0.0,
            positions=[{"symbol": SYM, "positionAmt": "1",
                        "markPrice": "100"}],
            open_symbols={SYM}, position_notionals={SYM: 100.0},
            symbol_vols={SYM: 0.30},
            target_replaces_existing=True))
        self.assertEqual(self._status(d, "available_balance").status, PASS)

    def test_non_flip_no_balance_still_fails(self):
        d = self._cl().evaluate(self._snap(
            available_balance=0.0,
            positions=[{"symbol": SYM, "positionAmt": "1",
                        "markPrice": "100"}],
            open_symbols={SYM}, position_notionals={SYM: 100.0},
            symbol_vols={SYM: 0.30}))
        self.assertEqual(self._status(d, "available_balance").status, FAIL)


class TestReconciler(unittest.TestCase):
    def test_all_flat_match(self):
        eng = H1SignalEngine()
        items = H1Reconciler().reconcile(eng, [])
        self.assertEqual([i.status for i in items], ["MATCH"] * 8)

    def test_positions_none_unknown(self):
        eng = H1SignalEngine()
        items = H1Reconciler().reconcile(eng, None)
        self.assertEqual([i.status for i in items], ["UNKNOWN"] * 8)

    def test_drift_on_untargeted_position(self):
        eng = H1SignalEngine()
        items = H1Reconciler().reconcile(
            eng, [{"symbol": OTHER, "positionAmt": "2",
                   "markPrice": "50"}])
        by = {i.symbol: i for i in items}
        self.assertEqual(by[OTHER].status, "DRIFT")
        self.assertEqual(by[SYM].status, "MATCH")

    def test_malformed_row_unknown(self):
        eng = H1SignalEngine()
        items = H1Reconciler().reconcile(
            eng, [{"symbol": OTHER, "positionAmt": "garbage"}])
        by = {i.symbol: i for i in items}
        self.assertEqual(by[OTHER].status, "UNKNOWN")

    def test_match_with_target_and_notional(self):
        eng = H1SignalEngine()
        eng._st[SYM]["dir"] = 1
        eng.note_intended_notional(SYM, 104.0)
        items = H1Reconciler().reconcile(
            eng, [{"symbol": SYM, "positionAmt": "0.4",
                   "markPrice": "260"}])
        by = {i.symbol: i for i in items}
        self.assertEqual(by[SYM].status, "MATCH")
        self.assertAlmostEqual(by[SYM].actual_notional, 104.0)
        self.assertEqual(by[SYM].target_direction, 1)
        self.assertEqual(by[SYM].actual_direction, 1)


class TestNullExecutor(unittest.TestCase):
    def test_records_intent_never_executes(self):
        ex = NullExecutor()
        d = H1Decision(symbol=SYM, bar_ts=1, direction=-1, sigma_i=0.4,
                       signal_hash="h", notional=50.0)
        res = ex.apply_target(d, 100.0)
        self.assertFalse(res["executed"])
        self.assertEqual(res["mode"], "NULL_NO_ORDERS")
        self.assertEqual(len(ex.intents), 1)
        self.assertEqual(ex.intents[0]["label"], STRATEGY_LABEL)
        # repeated application also never executes (idempotent no-orders)
        ex.apply_target(d, 101.0)
        self.assertEqual(len(ex.intents), 2)
        # the class has NO network/order surface at all — the no-orders proof


if __name__ == "__main__":
    unittest.main()
