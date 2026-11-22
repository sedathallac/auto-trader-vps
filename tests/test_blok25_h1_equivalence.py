"""BLOK25 Bölüm 3 — EXECUTABLE PROOF that the H1 adapter did NOT change the
signal formula: the frozen reference engine and this adapter replay the SAME
2022-2025 4h klines + funding data and must produce IDENTICAL flip sequences,
fills, units, commissions, trade PnL and funding cashflows.

Frozen reference: ~/futures-research/blok22/code/h1_engine.py simulate(180)
(BLOK22 prereg literal implementation; 2026 holdout sealed — both sides
hard-stop at the 2025-12-31 20:00 bar).

The replica broker below is an independent transcription of h1_engine's
accounting (steps a/b + the open-position close-out): taker commission 5
bp/side, slippage 3 bp/side against the trade direction, funding accrues iff
entry_ts < settlement ts, notional sized BEFORE the exit leg realizes. The
risk layer is stubbed with ApproveAll because the frozen simulation has no
pretrade gate — the 18-check risk integration is covered by
test_blok25_h1_adapter.py and test_blok25_riskchecklist.py.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from h1_adapter import H1SignalEngine, H1Router, H1_SYMBOLS
from risk_checklist import Decision

H1_ENGINE_PATH = Path.home() / "futures-research" / "blok22" / "code" / "h1_engine.py"


def _load_frozen():
    spec = importlib.util.spec_from_file_location("h1_engine_frozen",
                                                  H1_ENGINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ApproveAll:
    """Signal-layer equivalence stub: the frozen h1_engine simulation has no
    pretrade gate, so approving everything isolates the SIGNAL comparison."""

    def evaluate(self, snap):
        return Decision(approved=True, checks=[], reason="EQUIVALENCE_STUB")


class ReplicaBroker:
    """Independent transcription of h1_engine.py's accounting — the paper
    model core (Bölüm 4 reuses this shape). Implements the World surface
    (equity/positions/...) and the Executor surface (apply_target)."""

    SLIP = 0.0003   # per side, against the trade direction (prereg §17/18)
    COMM = 0.0005   # taker commission per side

    def __init__(self, syms, funding):
        self.st = {s: {"dir": 0, "units": 0.0, "entry_px": 0.0,
                       "entry_ts": None, "prev_settle_close": None}
                   for s in syms}
        self.funding = funding                    # sym -> [(ts, rate)]
        self.fidx = {s: 0 for s in syms}
        self.equity_real = 1.0
        self.trades = {s: [] for s in syms}
        self.fund_flows = {s: [] for s in syms}
        self.e_comm_prev = {}

    # World surface -------------------------------------------------------
    def equity(self):
        return self.equity_real

    def available_balance(self):
        return self.equity_real

    def positions(self):
        return [{"symbol": s,
                 "positionAmt": str(st["dir"] * st["units"]),
                 "markPrice": str(st["entry_px"])}
                for s, st in self.st.items()]

    def active_executor_pairs(self):
        return set()

    def reserved_symbols(self):
        return set()

    def leverage_brackets(self):
        return None

    # broker hooks (router calls them in the h1_engine order) ---------------
    def settle_funding(self, symbol, upto_ts):
        # step (a): settlements in (previous open, this open], BEFORE the
        # flip; a position open at the settlement snapshot pays even if it
        # exits at this same open
        st = self.st[symbol]
        fund = self.funding[symbol]
        while self.fidx[symbol] < len(fund) and \
                fund[self.fidx[symbol]][0] <= upto_ts:
            stime, rate = fund[self.fidx[symbol]]
            if st["dir"] != 0 and st["entry_ts"] is not None \
                    and st["entry_ts"] < stime:
                base_px = (st["prev_settle_close"]
                           if st["prev_settle_close"] else st["entry_px"])
                cf = -st["dir"] * rate * st["units"] * base_px
                self.equity_real += cf
                self.fund_flows[symbol].append(
                    {"ts": stime, "cf": cf, "dir": st["dir"], "rate": rate})
            self.fidx[symbol] += 1

    def on_bar_close(self, symbol, close_px):
        self.st[symbol]["prev_settle_close"] = close_px

    # Executor --------------------------------------------------------------
    def apply_target(self, decision, ref_price):
        # step (b) transcription. decision.notional was sized by the router
        # from the PRE-exit equity — h1_engine computes the notional before
        # realizing the exit leg; the order matters and is preserved.
        st = self.st[decision.symbol]
        new_dir = decision.direction
        o = ref_price
        notional = decision.notional
        fill = o * (1.0 + new_dir * self.SLIP)
        e_comm = self.COMM * notional
        if st["dir"] != 0:
            xfill = o * (1.0 - st["dir"] * self.SLIP)
            gross = st["dir"] * st["units"] * (xfill - st["entry_px"])
            x_comm = self.COMM * st["units"] * xfill
            self.equity_real += gross - x_comm
            self.trades[decision.symbol].append({
                "dir": st["dir"], "entry_ts": st["entry_ts"],
                "exit_ts": decision.execution_ts,
                "entry_px": st["entry_px"], "exit_px": xfill,
                "units": st["units"],
                "e_comm": self.e_comm_prev.get(decision.symbol, 0.0),
                "x_comm": x_comm, "pnl": gross - x_comm})
        units = notional / fill
        self.equity_real -= e_comm
        self.e_comm_prev[decision.symbol] = e_comm
        st.update(dir=new_dir, units=units, entry_px=fill,
                  entry_ts=decision.execution_ts)
        return {"executed": True, "mode": "REPLICA"}

    def close_open_positions(self, last_bars):
        # h1_engine close-out accounting: open positions marked, NOT closed
        for s, st in self.st.items():
            if st["dir"] != 0 and s in last_bars:
                lt, lc = last_bars[s]
                self.trades[s].append({
                    "dir": st["dir"], "entry_ts": st["entry_ts"],
                    "exit_ts": lt, "entry_px": st["entry_px"],
                    "exit_px": lc, "units": st["units"],
                    "e_comm": self.e_comm_prev.get(s, 0.0), "x_comm": 0.0,
                    "pnl": st["dir"] * st["units"] * (lc - st["entry_px"]),
                    "open_at_end": True})


class TestEquivalenceToFrozenEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not H1_ENGINE_PATH.exists():
            raise unittest.SkipTest(
                f"frozen engine not found: {H1_ENGINE_PATH}")
        cls.h1 = _load_frozen()
        # reference side: the frozen engine, untouched
        cls.ref_trades, cls.ref_fund, _, _, _ = cls.h1.simulate(180)
        # adapter side: same data, same order (merged timeline sorted by
        # (open_ts, symbol) exactly like h1_engine's timeline.sort())
        engine = H1SignalEngine()
        broker = ReplicaBroker(
            H1_SYMBOLS, {s: cls.h1.load_funding(s) for s in cls.h1.SYMS})
        router = H1Router(engine, ApproveAll(), broker, broker)
        timeline = []
        for s in cls.h1.SYMS:
            for (ot, o, c) in cls.h1.load_4h(s):
                timeline.append((ot, s, o, c))
        timeline.sort()
        last_bar = {}
        for (ot, s, o, c) in timeline:
            router.run_bar(s, ot, o, c)
            last_bar[s] = (ot, c)
        broker.close_open_positions(last_bar)
        cls.broker = broker
        cls.engine = engine
        cls.timeline_len = len(timeline)

    def test_both_sides_saw_the_same_bars(self):
        # sanity: 2022-2025 4h universe, hard stop before 2026 (holdout sealed)
        self.assertGreater(self.timeline_len, 8 * 8000)
        self.assertLess(self.timeline_len, 8 * 9000)

    def test_flip_sequences_identical(self):
        for s in self.h1.SYMS:
            ref = self.ref_trades[s]
            mine = self.broker.trades[s]
            self.assertEqual(len(mine), len(ref),
                             f"{s}: {len(mine)} vs {len(ref)} flips")
            for a, b in zip(mine, ref):
                self.assertEqual(a["dir"], b["dir"], s)
                self.assertEqual(a["entry_ts"], b["entry_ts"], s)
                self.assertEqual(a["exit_ts"], b["exit_ts"], s)
                self.assertEqual(a.get("open_at_end", False),
                                 b.get("open_at_end", False), s)

    def test_fills_units_costs_pnl_identical(self):
        for s in self.h1.SYMS:
            for a, b in zip(self.broker.trades[s], self.ref_trades[s]):
                self.assertAlmostEqual(a["entry_px"], b["entry_px"],
                                       places=12, msg=s)
                self.assertAlmostEqual(a["exit_px"], b["exit_px"],
                                       places=12, msg=s)
                self.assertAlmostEqual(a["units"], b["units"],
                                       places=12, msg=s)
                self.assertAlmostEqual(a["e_comm"], b["e_comm"],
                                       places=12, msg=s)
                self.assertAlmostEqual(a["x_comm"], b["x_comm"],
                                       places=12, msg=s)
                self.assertAlmostEqual(a["pnl"], b["pnl"],
                                       places=10, msg=s)

    def test_funding_cashflows_identical(self):
        for s in self.h1.SYMS:
            ref = self.ref_fund[s]
            mine = self.broker.fund_flows[s]
            self.assertEqual(len(mine), len(ref), s)
            for a, b in zip(mine, ref):
                self.assertEqual(a["ts"], b["ts"], s)
                self.assertEqual(a["dir"], b["dir"], s)
                self.assertAlmostEqual(a["cf"], b["cf"], places=12, msg=s)

    def test_realized_equity_path_identical(self):
        # trades + funding determine realized equity uniquely; compare the
        # total cashflow: sum(trade pnl - entry comm) + sum(funding cf)
        for side, trades, fund in (
                ("adapter", self.broker.trades, self.broker.fund_flows),
                ("frozen", self.ref_trades, self.ref_fund)):
            tot = sum(t["pnl"] - t["e_comm"]
                      for s in self.h1.SYMS for t in trades[s])
            tot += sum(f["cf"] for s in self.h1.SYMS for f in fund[s])
            if side == "adapter":
                mine = tot
            else:
                ref = tot
        self.assertAlmostEqual(mine, ref, places=10)


if __name__ == "__main__":
    unittest.main()
