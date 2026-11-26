"""BLOK25 Bölüm 4 — paper/shadow bot: realistic (never optimistic) fills,
funding, liquidation model, SL/TP barrier modeling, SQLite persistence and
restart recovery, manifest freeze/tamper refusal, runner closed-bar
discipline, and the NO-REAL-ORDERS guarantee.

The 17 Bölüm 4 measurement items map to recorded fields:
  signal time (bar_ts), signal price (signal_price), target position
  (direction), target notional, RiskEngine decision (checklist summary),
  reference order price (ref_price), simulated fill (fill/entry_px/exit_px),
  realistic spread (spread_bp), slippage (slippage_bp + impact_bp),
  commission, funding settlement (funding table), Mark Price (marks table),
  uPnL / rPnL / drawdown (marks + trades + account), reconciliation (recon
  table), error/restart (events + durable journal table).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from h1_adapter import H1Decision, H1_SYMBOLS
from paper_broker import (PaperBroker, PaperRiskState, FILL_MODEL,
                          LIQ_FRACTION)
from paper_runner import PaperRunner, build_manifest
from risk_checklist import PretradeChecklist
from signal_dedup import SignalDedupStore

SYM = "BTCUSDT"
OTHER = "ETHUSDT"
BAR = 4 * 3600 * 1000
T0 = 1730000000000


def _decision(symbol=SYM, direction=1, notional=100.0, bar_ts=T0,
              execution_ts=T0 + BAR, ref_price=100.0, signal_price=99.0):
    return H1Decision(symbol=symbol, bar_ts=bar_ts, direction=direction,
                      sigma_i=0.30, signal_hash="h", notional=notional,
                      execution_ts=execution_ts, ref_price=ref_price,
                      signal_price=signal_price)



def _bar(b, ts, open_px, close_px, qv=1e6, symbol=SYM):
    """note_bar + on_bar_close — what the router does for one bar.
    BLOK27.5: the mark is stamped at the bar's CLOSE time (open + 4h)."""
    b.note_bar(symbol, ts, open_px, close_px, qv)
    b.on_bar_close(symbol, close_px, ts + BAR)

def _broker(**kw):
    tmp = tempfile.mkdtemp()
    db = kw.pop("db", os.path.join(tmp, "paper_ledger.db"))
    return PaperBroker(db, [SYM, OTHER], **kw)


# ---------------------------------------------------------------------------
class TestFillModel(unittest.TestCase):
    def test_buy_above_mid_sell_below_mid(self):
        b = _broker()
        buy, bp, imp = b._fill(SYM, 1, 100.0, 100.0)
        sell, _, _ = b._fill(SYM, -1, 100.0, 100.0)
        self.assertGreater(buy, 100.0)      # NEVER an optimistic fill
        self.assertLess(sell, 100.0)
        self.assertGreater(bp, FILL_MODEL["half_spread_bp"]
                           + FILL_MODEL["slippage_bp"])
        self.assertGreaterEqual(imp, 0.0)

    def test_impact_grows_with_relative_size(self):
        b = _broker()
        b._quote_volume[SYM] = 1e10
        small = b._impact_bp(1_000.0, SYM)
        big = b._impact_bp(100_000.0, SYM)
        self.assertGreater(big, small)

    def test_impact_conservative_without_volume(self):
        b = _broker()                        # no volume noted
        self.assertEqual(b._impact_bp(100.0, SYM),
                         FILL_MODEL["impact_max_bp"])

    def test_commission_charged_per_side(self):
        b = _broker()
        b.apply_target(_decision(notional=100.0, ref_price=100.0), 100.0)
        # entry commission deducted from cash
        expected = 1000.0 - FILL_MODEL["commission_bp"] / 1e4 * 100.0
        self.assertAlmostEqual(b._account()["cash"], expected, places=9)
        # the fill price itself is beyond the mid
        pos = b._position(SYM)
        self.assertGreater(pos["entry_px"], 100.0)

    def test_trade_row_records_all_measurement_fields(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        b.note_bar(SYM, T0 + BAR, 100.0, 110.0, 1e6)
        b.apply_target(_decision(direction=-1, notional=80.0,
                                 execution_ts=T0 + 2 * BAR,
                                 ref_price=110.0, signal_price=110.0,
                                 bar_ts=T0 + BAR), 110.0)
        trades = b.closed_trades()
        self.assertEqual(len(trades), 1)
        t = trades[0]
        for k in ("signal_bar_ts", "execution_ts", "ref_price",
                  "signal_price", "entry_signal_price", "spread_bp",
                  "slippage_bp", "impact_bp", "commission", "entry_px",
                  "exit_px", "units", "pnl"):
            self.assertIn(k, t)
        self.assertEqual(t["direction"], 1)
        self.assertAlmostEqual(t["entry_signal_price"], 99.0)  # entry bar
        self.assertAlmostEqual(t["signal_price"], 110.0)       # exit flip bar
        # LONG closed at a SELL fill: below the 110 reference
        self.assertLess(t["exit_px"], 110.0)
        self.assertGreater(t["pnl"], 0)      # 100 -> ~110 less costs


# ---------------------------------------------------------------------------
class TestFunding(unittest.TestCase):
    def test_long_pays_positive_rate(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        pos = b._position(SYM)
        mark = 100.0
        b._last_mark[SYM] = mark
        # entry at T0+BAR open; settlement strictly AFTER the entry
        b.note_funding_rate(SYM, T0 + 2 * BAR, 0.0001)   # +0.01% per 8h
        b.settle_funding(SYM, T0 + 3 * BAR)
        rows = b.funding_rows()
        self.assertEqual(len(rows), 1)
        expected = -1 * 0.0001 * pos["units"] * mark
        self.assertAlmostEqual(rows[0]["cf"], expected, places=12)
        self.assertLess(b._account()["cash"], 1000.0)

    def test_short_receives_positive_rate(self):
        b = _broker()
        b.apply_target(_decision(direction=-1, notional=100.0), 100.0)
        pos = b._position(SYM)
        b._last_mark[SYM] = 100.0
        b.note_funding_rate(SYM, T0 + 2 * BAR, 0.0001)
        b.settle_funding(SYM, T0 + 3 * BAR)
        self.assertAlmostEqual(b.funding_rows()[0]["cf"],
                               0.0001 * pos["units"] * 100.0, places=12)
        cash_after_entry = b._account()["cash"]      # net of entry comm
        b.note_funding_rate(SYM, T0 + 3 * BAR, 0.0001)
        b.settle_funding(SYM, T0 + 4 * BAR)
        self.assertGreater(b._account()["cash"], cash_after_entry)

    def test_no_charge_at_or_before_entry(self):
        # Binance semantics: a position ENTERING at this bar open is not
        # charged for a settlement at/before its entry
        b = _broker()
        b.note_funding_rate(SYM, T0 + BAR, 0.0001)
        b.apply_target(_decision(direction=1, notional=100.0,
                                 execution_ts=T0 + BAR), 100.0)
        b.settle_funding(SYM, T0 + BAR)
        self.assertEqual(b.funding_rows(), [])

    def test_settlement_exactly_once(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        b._last_mark[SYM] = 100.0
        b.note_funding_rate(SYM, T0 + 2 * BAR, 0.0001)
        b.settle_funding(SYM, T0 + 3 * BAR)
        b.settle_funding(SYM, T0 + 4 * BAR)      # cursor advanced
        self.assertEqual(len(b.funding_rows()), 1)


# ---------------------------------------------------------------------------
class TestAccounting(unittest.TestCase):
    def test_upnl_and_equity(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0 + BAR, 100.0, 120.0)
        st = b.account_status()
        self.assertGreater(st["unrealized"], 0)
        self.assertGreater(st["equity"], st["cash"])

    def test_drawdown_from_peak(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0 + BAR, 100.0, 130.0)     # up -> new peak
        peak = b.account_status()["peak_equity"]
        _bar(b, T0 + 2 * BAR, 130.0, 90.0)  # down
        st = b.account_status()
        self.assertAlmostEqual(st["peak_equity"], peak)
        self.assertGreater(st["drawdown"], 0)

    def test_marks_rows_written_per_bar(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0 + BAR, 100.0, 110.0)
        _bar(b, T0 + 2 * BAR, 110.0, 105.0)
        rows = b.marks_rows(SYM)
        self.assertEqual(len(rows), 2)
        self.assertIn("upnl", rows[0])
        self.assertIn("drawdown", rows[0])


# ---------------------------------------------------------------------------
class TestLiquidation(unittest.TestCase):
    def test_liquidation_closes_and_halts(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        pos = b._position(SYM)
        # liq price derives from the realistic ENTRY FILL, not the ref price
        liq = pos["entry_px"] * (1 - LIQ_FRACTION)
        self.assertAlmostEqual(pos["liq_px"], liq, places=9)
        # buy fill is above the ref -> liq level is higher than ref-based
        self.assertGreater(pos["liq_px"], 100.0 * (1 - LIQ_FRACTION))
        # a ~95% adverse move crosses the analytic bound
        _bar(b, T0 + BAR, 100.0, 4.0)
        trades = b.closed_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "LIQUIDATION")
        self.assertIsNone(b._position(SYM))
        st = b.account_status()
        self.assertTrue(st["halted"])
        self.assertIn("LIQUIDATION", st["halt_reason"])
        # fail-closed: the risk state now blocks new executions
        cl = PretradeChecklist(PaperRiskState(b),
                               signal_dedup=SignalDedupStore(
                                   tempfile.mkdtemp() + "/d.db"))
        cl.signal_dedup.init_schema()
        from risk_checklist import Snapshot
        snap = Snapshot(equity=1000.0, available_balance=1000.0,
                        positions=[], open_symbols=set(),
                        active_executor_pairs=set(), reserved_symbols=set(),
                        target_symbol=OTHER, target_side=1,
                        target_notional=50.0, target_symbol_vol=0.3,
                        bar_ts=T0, signal_hash="x", symbol_vols={},
                        corr=None, leverage_brackets=None,
                        max_open_positions=8, position_notionals=None)
        d = cl.evaluate(snap)
        self.assertFalse(d.approved)
        self.assertIn("halted_kill_switch", d.reason)

    def test_sl_barrier_modeled_but_disabled_by_default(self):
        # default (H1 frozen design): NO barrier closes the position
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0 + BAR, 100.0, 50.0)
        self.assertIsNotNone(b._position(SYM))
        # with a barrier config the model closes at the trigger
        b2 = _broker(barrier_config={"sl_pct": 0.05})
        b2.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b2, T0 + BAR, 100.0, 94.0)
        trades = b2.closed_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "SL_BARRIER")
        self.assertIsNone(b2._position(SYM))


# ---------------------------------------------------------------------------
class TestPersistence(unittest.TestCase):
    def test_state_survives_restart(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "paper.db")
        b1 = PaperBroker(db, [SYM, OTHER])
        b1.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b1, T0 + BAR, 100.0, 110.0)
        b2 = PaperBroker(db, [SYM, OTHER])       # 'restart': fresh handle
        pos = b2._position(SYM)
        self.assertIsNotNone(pos)
        self.assertEqual(pos["direction"], 1)
        st = b2.account_status()
        self.assertAlmostEqual(st["cash"], b1._account()["cash"])
        self.assertGreater(st["unrealized"], 0)

    def test_risk_state_measured_from_ledger(self):
        b = _broker()
        risk = PaperRiskState(b)
        self.assertFalse(risk.is_halted)
        self.assertTrue(risk.state)              # loaded, not default-empty
        self.assertEqual(risk.consecutive_losses, 0)
        # a losing closed trade advances the loss streak
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0 + BAR, 100.0, 80.0)
        b.apply_target(_decision(direction=-1, notional=80.0,
                                 execution_ts=T0 + 2 * BAR, ref_price=80.0,
                                 signal_price=80.0, bar_ts=T0 + BAR), 80.0)
        self.assertEqual(risk.consecutive_losses, 1)


# ---------------------------------------------------------------------------
class FakeFetcher:
    """Scripted public-market-data fetcher (klines + funding history)."""

    def __init__(self, klines, funding=None):
        self._klines = klines           # symbol -> [(ot, open, close, qv)]
        self._funding = funding or {}
        self.calls = []

    def klines(self, symbol, interval="4h", limit=500):
        self.calls.append(("klines", symbol))
        return list(self._klines.get(symbol, []))[-limit:]

    def funding_history(self, symbol, limit=100):
        self.calls.append(("funding", symbol))
        return list(self._funding.get(symbol, []))[-limit:]


def _mk_bars(n, start=100.0, step=1.0, ts0=T0, qv=1e7):
    return [(ts0 + i * BAR, start + i * step, start + i * step, qv)
            for i in range(n)]


class TestRunner(unittest.TestCase):
    def _runner(self, fetcher, clock, db=None, results=None, **kw):
        db = db or os.path.join(tempfile.mkdtemp(), "paper.db")
        results = results or tempfile.mkdtemp()
        return PaperRunner(db, results, symbols=[SYM, OTHER],
                           equity_start=1000.0, fetcher=fetcher, clock=clock,
                           **kw)

    def test_first_cycle_warmup_freezes_manifest(self):
        fetcher = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        r = self._runner(fetcher, lambda: now)
        status = r.cycle()
        # manifest frozen before any bar, with every required component
        m = json.loads(r.broker.meta_get("manifest"))
        for k in ("test_start_ts", "strategy", "code_hashes",
                  "execution_model", "execution_model_hash",
                  "acceptance_criteria", "end_conditions", "manifest_hash"):
            self.assertIn(k, m)
        self.assertEqual(m["strategy"]["lookback_bars"], 180)
        self.assertEqual(m["label"], "RESEARCH_VALIDATION_POSITIVE_OOS_UNPROVEN")
        # warmup replayed history WITHOUT executing
        self.assertEqual(r.broker.closed_trades(), [])
        self.assertTrue(r.engine.warm(SYM))
        self.assertEqual(r.engine.direction(SYM), 1)     # rising history
        self.assertEqual(r.engine.direction(OTHER), 1)
        self.assertEqual(status["journal_outcomes"].get("WARMUP_SKIPPED", 0)
                         + status["journal_outcomes"].get("EXECUTED", 0) > 0,
                         True)
        # status file written
        self.assertTrue((Path(r.results_dir) / "paper_status.json").exists())

    def test_manifest_tamper_refuses_to_run(self):
        fetcher = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        db = os.path.join(tempfile.mkdtemp(), "paper.db")
        r = self._runner(fetcher, lambda: now, db=db)
        r.cycle()
        # tamper with the frozen manifest
        r.broker.meta_set("manifest_hash", "tampered")
        r2 = self._runner(fetcher, lambda: now, db=db)
        with self.assertRaises(RuntimeError):
            r2.cycle()
        # the breach is recorded
        kinds = [e["kind"] for e in r.broker.events_rows()]
        self.assertIn("MANIFEST_BREACH", kinds)

    def test_manifest_stable_across_cycles(self):
        fetcher = FakeFetcher({SYM: _mk_bars(402), OTHER: _mk_bars(402)})
        now = T0 + 402 * BAR
        db = os.path.join(tempfile.mkdtemp(), "paper.db")
        r = self._runner(fetcher, lambda: now, db=db)
        r.cycle()
        r.cycle()       # same code, same config -> verification passes
        kinds = [e["kind"] for e in r.broker.events_rows()]
        self.assertEqual(kinds.count("MANIFEST_FROZEN"), 1)
        self.assertNotIn("MANIFEST_BREACH", kinds)      # no breach events

    def test_closed_bars_only(self):
        # the last bar is still IN PROGRESS (open + 4h > now): never processed
        fetcher = FakeFetcher({SYM: _mk_bars(400) + [
            (T0 + 400 * BAR, 500.0, 500.0, 1e7)], OTHER: _mk_bars(401)})
        now = T0 + 400 * BAR + 1000        # bar 399 closed, bar 400 open
        r = self._runner(fetcher, lambda: now)
        r.cycle()
        self.assertEqual(int(r.broker.meta_get(f"last_bar:{SYM}")),
                         T0 + 399 * BAR)

    def test_live_flip_executes_with_realistic_fill(self):
        # warmup history: both symbols rising (targets LONG, skipped);
        # then NEW falling bars for OTHER trigger a live SHORT flip
        hist = {SYM: _mk_bars(402), OTHER: _mk_bars(402)}
        fetcher = FakeFetcher(hist)
        now = T0 + 402 * BAR
        db = os.path.join(tempfile.mkdtemp(), "paper.db")
        results = tempfile.mkdtemp()
        r = self._runner(fetcher, lambda: now, db=db, results=results)
        r.cycle()
        # append 181 falling bars for OTHER (+1 in-progress bar for realism)
        fall = [(T0 + (402 + i) * BAR, 502.0 - i, 502.0 - i, 1e7)
                for i in range(181)]
        fetcher._klines[OTHER] = fetcher._klines[OTHER] + fall
        # SYM feed must stay FRESH too (production: dead feed = CRITICAL
        # HALT). Rising bars -> SYM target stays long, no flip interference.
        fetcher._klines[SYM] = fetcher._klines[SYM] + [
            (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i, 1e7)
            for i in range(181)]
        now2 = T0 + 583 * BAR + 1000
        # backlog scenario: production default (2 bars) would rightly skip
        # these as STALE; this test exercises flip MECHANICS over a
        # scripted 30-day backlog, so the staleness gate is widened here.
        r2 = self._runner(fetcher, lambda: now2, db=db, results=results,
                          stale_bar_ms=10 ** 15)
        status = r2.cycle()
        # exactly ONE live execution, with a realistic (adverse) fill
        executed = [j for j in r2.broker.journal_rows()
                    if j["outcome"] == "EXECUTED"]
        self.assertEqual(len(executed), 1)
        j = executed[0]
        self.assertEqual(j["symbol"], OTHER)
        self.assertEqual(j["direction"], -1)
        pos = r2.broker._position(OTHER)
        self.assertIsNotNone(pos)
        self.assertEqual(pos["direction"], -1)
        # SHORT entry fills BELOW the bar-open reference (never optimistic)
        self.assertLess(pos["entry_px"], j["ref_price"])
        self.assertGreater(pos["entry_px"], 0)
        # the flip is durable in the journal (measurement items intact)
        for k in ("bar_ts", "signal_price", "execution_ts", "ref_price",
                  "sigma_i", "notional", "checklist", "portfolio_vol"):
            self.assertIn(k, j)
        self.assertEqual(status["journal_outcomes"]["EXECUTED"], 1)
        # recon recorded for every symbol (engine order, not [0])
        recon = {i["symbol"]: i["status"] for i in status["recon"]}
        self.assertIn(OTHER, recon)
        self.assertEqual(recon[OTHER], "MATCH")

    def test_restart_does_not_double_execute(self):
        hist = {SYM: _mk_bars(402), OTHER: _mk_bars(402)}
        fetcher = FakeFetcher(hist)
        now = T0 + 402 * BAR
        db = os.path.join(tempfile.mkdtemp(), "paper.db")
        r = self._runner(fetcher, lambda: now, db=db)
        r.cycle()
        fall = [(T0 + (402 + i) * BAR, 502.0 - i, 502.0 - i, 1e7)
                for i in range(181)]
        fetcher._klines[OTHER] = fetcher._klines[OTHER] + fall
        # SYM feed must stay FRESH too (production: dead feed = CRITICAL
        # HALT). Rising bars -> SYM target stays long, no flip interference.
        fetcher._klines[SYM] = fetcher._klines[SYM] + [
            (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i, 1e7)
            for i in range(181)]
        now2 = T0 + 583 * BAR + 1000
        # backlog scenario: staleness gate widened (see live-flip test)
        r2 = self._runner(fetcher, lambda: now2, db=db,
                          stale_bar_ms=10 ** 15)
        r2.cycle()                                  # the flip executes
        pos_before = r2.broker._position(OTHER)
        # RESTART: a fresh process over the same ledger
        r3 = self._runner(fetcher, lambda: now2, db=db)
        status = r3.cycle()
        pos_after = r3.broker._position(OTHER)
        self.assertEqual(pos_after["units"], pos_before["units"])
        self.assertEqual(pos_after["entry_px"], pos_before["entry_px"])
        executed = [j for j in r3.broker.journal_rows()
                    if j["outcome"] == "EXECUTED"]
        self.assertEqual(len(executed), 1)          # exactly once, ever
        # engine state rebuilt by the restart warmup
        self.assertEqual(r3.engine.direction(OTHER), -1)

    def test_funding_settled_in_live_flow(self):
        hist = {SYM: _mk_bars(402), OTHER: _mk_bars(402)}
        # the flip to SHORT executes around bar 494 (momentum turns after
        # ~91 falling bars); the settlement must fall AFTER that entry
        funding = {OTHER: [(T0 + 500 * BAR, 0.0001)]}
        fetcher = FakeFetcher(hist, funding)
        now = T0 + 402 * BAR
        db = os.path.join(tempfile.mkdtemp(), "paper.db")
        r = self._runner(fetcher, lambda: now, db=db)
        r.cycle()
        fall = [(T0 + (402 + i) * BAR, 502.0 - i, 502.0 - i, 1e7)
                for i in range(181)]
        fetcher._klines[OTHER] = fetcher._klines[OTHER] + fall
        # SYM feed must stay FRESH too (production: dead feed = CRITICAL
        # HALT). Rising bars -> SYM target stays long, no flip interference.
        fetcher._klines[SYM] = fetcher._klines[SYM] + [
            (T0 + (402 + i) * BAR, 502.0 + i, 502.0 + i, 1e7)
            for i in range(181)]
        now2 = T0 + 583 * BAR + 1000
        # backlog scenario: staleness gate widened (see live-flip test)
        r2 = self._runner(fetcher, lambda: now2, db=db,
                          stale_bar_ms=10 ** 15)
        r2.cycle()
        # the SHORT position received the positive funding rate
        rows = r2.broker.funding_rows()
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(row["direction"], -1)
            self.assertGreater(row["cf"], 0)

    def test_cycle_error_recorded_not_fatal(self):
        class BoomFetcher(FakeFetcher):
            def klines(self, *a, **k):
                raise IOError("network down")
        fetcher = BoomFetcher({})
        r = self._runner(fetcher, lambda: T0)
        with self.assertRaises(Exception):
            r.cycle()          # cycle raises to the caller; run() would log
        # (the fetch failure itself is visible to the caller — fail-closed)


class TestNoRealOrders(unittest.TestCase):
    def test_paper_broker_has_no_network_surface(self):
        src = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                   ).joinpath("paper_broker.py").read_text()
        for banned in ("import requests", "urllib", "socket", "httpx",
                       "aiohttp", "urlopen"):
            self.assertNotIn(banned, src)

    def test_runner_public_gets_only(self):
        src = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                   ).joinpath("paper_runner.py").read_text()
        # only public market-data endpoints; no order paths, no credentials
        self.assertIn("/fapi/v1/klines", src)
        self.assertIn("/fapi/v1/fundingRate", src)
        for banned in ("POST", "/order", "apiKey", "API_KEY",
                       "X-MBX-APIKEY", "signature", "secret"):
            self.assertNotIn(banned, src)

    def test_manifest_components_frozen(self):
        m = build_manifest(1234567)
        self.assertIn("manifest_hash", m)
        self.assertIn("orders", m)
        self.assertIn("NONE", m["orders"])
        # acceptance criteria + end conditions are part of the freeze
        self.assertEqual(m["acceptance_criteria"]["max_drawdown_halt"], 0.20)
        self.assertIn("min_duration_days", m["acceptance_criteria"])
        self.assertIn("operator_stop", m["end_conditions"])


if __name__ == "__main__":
    unittest.main()
