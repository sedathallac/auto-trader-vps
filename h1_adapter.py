"""h1_adapter.py — BLOK25 Bölüm 3: H1 strategy adapter (clean layer binding).

Binds the FROZEN H1 — 4h time-series momentum, BLOK22 preregistration — into
the Futures execution architecture. The strategy itself is NOT touched:

  §6 signal : m(t) = close(t)/close(t-180) - 1 on 4h bars; EWMA variance on
              close-to-close log returns, halflife 42 bars, annualized with
              sqrt(6*365). Gap bar: signal/vol carried (fail-closed).
  §7 trades : ONLY at direction flips; execution at the NEXT bar open (a
              missing open bar carries the pending flip to the next available
              open). Always-in-market after the first valid signal.
  §9 sizing : notional_i = min(w*E*(sigma_target/sigma_i), w*E) with w=1/8 and
              sigma_target=15%; set at entry, FIXED in units until the next
              flip. NO SL/TP in the research design (signal flip is the only
              exit). Funding is ACCOUNTING only — never an entry signal.

Frozen reference implementation:
  ~/futures-research/blok22/code/h1_engine.py  (simulate(180))
tests/test_blok25_h1_equivalence.py replays the identical 2022-2025 4h data
through the frozen engine and through this adapter and requires IDENTICAL
flips, fills, units and funding flows — the executable proof that the signal
formula did not change.

Layer separation (BLOK25 Bölüm 3 requirement):
  SIGNAL          H1SignalEngine  pure bars -> flip decisions, no I/O
  RISK            H1Router._execute: PretradeChecklist (Bölüm 2, 18 checks)
                  + SignalDedupStore replay guard (inside the checklist)
  EXECUTOR        Executor interface; NullExecutor = the NO-ORDERS phase.
                  A real-order executor is operator-gated (Bölüm 8) and is
                  deliberately NOT implemented in this module.
  RECONCILIATION  H1Reconciler: signal target book vs actual book; UNKNOWN or
                  unexplained DRIFT blocks new executions (fail-closed).

Label: RESEARCH_VALIDATION_POSITIVE_OOS_UNPROVEN — never claimed as
OOS-proven; the 2026 holdout stays sealed.

Restart note (full recovery is Bölüm 6 scope): the engine state is rebuildable
by replaying history with execute=False (warmup); already-executed bars stay
consumed in the persistent SignalDedupStore, so a replay can never
double-execute a consumed decision.
"""
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Optional

from risk_checklist import (PretradeChecklist, Snapshot, Decision,
                            SIGMA_TARGET, W_SYMBOL, CORR_MEAN)

STRATEGY_ID = "H1_TSMOM_4H_L180"
STRATEGY_LABEL = "RESEARCH_VALIDATION_POSITIVE_OOS_UNPROVEN"

# ---- frozen parameters (BLOK22 prereg §6/§9; DO NOT CHANGE) ---------------
H1_SYMBOLS = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT",
              "DOGEUSDT", "LINKUSDT", "SOLUSDT", "XRPUSDT"]
LOOKBACK = 180                        # N, in 4h bars
HALFLIFE = 42.0                       # EWMA vol halflife, in bars
ANN = math.sqrt(6.0 * 365.0)          # 4h bars per year -> annualization
LAMBDA = math.exp(math.log(0.5) / HALFLIFE)
BAR_INTERVAL_MS = 4 * 3600 * 1000     # H1 runs on 4h bars (frozen design)


def bar_close_ts(bar_ts):
    """The closed bar's close timestamp (UTC epoch ms): bar open + 4h —
    the instant the close price became known. BLOK27.5 (F-1): the mark row
    must carry THIS value, never the latest live bar's ts. Validates
    bar_ts (integer, ms-scale) — a seconds-scale or non-integral value
    raises (fail-closed)."""
    if bar_ts is None or isinstance(bar_ts, bool):
        raise ValueError(f"bar_ts {bar_ts!r} is REQUIRED (UTC epoch ms, "
                         "the closed bar's open time)")
    v = int(bar_ts)
    if v != bar_ts or v < 10 ** 12:
        raise ValueError(f"bar_ts {bar_ts!r} is not UTC epoch MILLISECONDS")
    return v + BAR_INTERVAL_MS


def param_manifest() -> dict:
    """The frozen parameter set + its hash (feeds the Bölüm 4 paper manifest:
    signal/model hash and param hash must be frozen BEFORE the run starts)."""
    p = {"strategy": STRATEGY_ID,
         "label": STRATEGY_LABEL,
         "symbols": list(H1_SYMBOLS),
         "interval": "4h",
         "lookback_bars": LOOKBACK,
         "vol": "EWMA on close-to-close log returns, halflife 42 bars",
         "annualization": ANN,
         "w_symbol": W_SYMBOL,
         "sigma_target": SIGMA_TARGET,
         "exit": "flip-only (no SL/TP in the research design)",
         "execution": "next-bar-open (gap: next available open)"}
    p["param_hash"] = hashlib.sha256(
        json.dumps(p, sort_keys=True).encode()).hexdigest()
    return p


def h1_signal_hash(symbol: str, bar_ts: int, direction: int,
                   sigma_i: float) -> str:
    """Stable identity of one decision: strategy + symbol + decision bar +
    direction + vol. The (symbol, bar_ts) pair is the dedup key; the hash is
    recorded for the journal/manifest audit trail."""
    payload = json.dumps([STRATEGY_ID, symbol, int(bar_ts), int(direction),
                          round(float(sigma_i), 12)], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class H1Decision:
    """One flip decision emitted by the signal layer."""
    symbol: str
    bar_ts: int            # the DECISION bar (close) — dedup key with symbol
    direction: int         # +1 long / -1 short (the NEW target direction)
    sigma_i: float         # annualized EWMA vol at the decision bar
    signal_hash: str
    notional: Optional[float] = None   # filled by the risk layer (§9 sizing)
    execution_ts: Optional[int] = None  # the bar whose OPEN executed it
    ref_price: Optional[float] = None   # that open (the reference order price)
    signal_price: Optional[float] = None  # the DECISION bar close


def h1_target_notional(equity: Optional[float],
                       sigma_i: Optional[float]) -> Optional[float]:
    """§9 sizing: notional_i = w*E*(sigma_target/sigma_i) capped at w*E.
    None when the inputs are unusable — fail-closed, never a blind trade."""
    if equity is None or equity <= 0 or sigma_i is None or sigma_i <= 0:
        return None
    return min(W_SYMBOL * equity * (SIGMA_TARGET / sigma_i),
               W_SYMBOL * equity)


# --------------------------------------------------------------------------
# SIGNAL layer
# --------------------------------------------------------------------------
class H1SignalEngine:
    """Pure 4h TSMom state machine, mirroring h1_engine.py steps (b)/(c)/(e)
    exactly. No I/O: the caller feeds bars (router/poller) and reads decisions.

    Per-symbol state:
      var        EWMA variance of close-to-close log returns (None until the
                 2nd close)
      prev_close last close seen
      dir        CURRENT target direction (advances when a pending flip is
                 popped by begin_bar — the target state, independent of
                 whether the executor actually moved the book)
      pend/pend_vol/pend_bar_ts/pend_price  the flip scheduled for the next
                                            bar open (price = decision close)
      closes     close history (indexing mirrors h1_engine's data[s][i-N])
      n          bars seen (h1_engine's bidx)
      target_notional  last §9 notional the router sized for this symbol
    """

    def __init__(self, symbols=None):
        self.symbols = list(symbols if symbols is not None else H1_SYMBOLS)
        self.lookback = LOOKBACK
        self._st = {s: {"var": None, "prev_close": None, "dir": 0,
                        "pend": 0, "pend_vol": 0.0, "pend_bar_ts": None,
                        "pend_price": None,
                        "closes": [], "n": 0, "target_notional": None}
                    for s in self.symbols}

    def _state(self, symbol):
        if symbol not in self._st:
            raise ValueError(f"symbol {symbol} is not part of the frozen "
                             f"H1 universe {self.symbols}")
        return self._st[symbol]

    # ---- reads ----------------------------------------------------------
    def direction(self, symbol) -> int:
        return self._state(symbol)["dir"]

    def sigma(self, symbol) -> Optional[float]:
        """Annualized EWMA vol after the latest close (None before warmup)."""
        v = self._state(symbol)["var"]
        return math.sqrt(v) * ANN if v is not None and v > 0 else None

    def intended_notional(self, symbol) -> Optional[float]:
        return self._state(symbol)["target_notional"]

    def target_notionals(self) -> dict:
        return {s: self._st[s]["target_notional"] for s in self.symbols}

    def note_intended_notional(self, symbol, notional) -> None:
        """The router records the §9 notional it sized for the current target
        (reconciliation compares it against the actual book)."""
        self._state(symbol)["target_notional"] = notional

    def warm(self, symbol) -> bool:
        """True once the momentum window is usable (i >= N and var > 0)."""
        S = self._state(symbol)
        return S["n"] >= self.lookback and S["var"] is not None and S["var"] > 0

    # ---- the two bar steps ------------------------------------------------
    def begin_bar(self, symbol, open_px) -> Optional[H1Decision]:
        """§7 step (b): pop a pending flip at THIS bar open. Returns the
        decision the caller must act on NOW (size it, run the risk layer,
        execute at open_px), or None. The target direction advances here —
        the signal layer owns the TARGET book, the executor owns the ACTUAL
        book; H1Reconciler compares the two."""
        S = self._state(symbol)
        if S["pend"] != 0 and S["pend"] != S["dir"]:
            d = H1Decision(symbol=symbol, bar_ts=S["pend_bar_ts"],
                           direction=S["pend"],
                           sigma_i=S["pend_vol"],
                           signal_price=S["pend_price"],
                           signal_hash=h1_signal_hash(symbol, S["pend_bar_ts"],
                                                      S["pend"], S["pend_vol"]))
            S["dir"] = S["pend"]
            S["pend"] = 0
            return d
        return None

    def end_bar(self, symbol, bar_ts, close_px) -> Optional[H1Decision]:
        """§6 steps (c)+(e): EWMA update on the close, then the signal at this
        close for the NEXT bar. Returns the newly PENDING decision
        (informational — it only becomes executable at the next begin_bar),
        or None."""
        S = self._state(symbol)
        # (c) EWMA variance on the close-to-close log return
        if S["prev_close"] is not None:
            r = math.log(close_px / S["prev_close"])
            S["var"] = (r * r if S["var"] is None
                        else LAMBDA * S["var"] + (1 - LAMBDA) * r * r)
        S["prev_close"] = close_px
        # (e) momentum signal at this close for the next bar
        i = S["n"]
        S["closes"].append(close_px)
        S["n"] = i + 1
        if i >= self.lookback and S["var"] is not None and S["var"] > 0:
            m = close_px / S["closes"][i - self.lookback] - 1.0
            sig = (1 if m > 0 else
                   (-1 if m < 0 else (S["dir"] if S["dir"] != 0 else 0)))
            if sig != 0 and sig != S["dir"]:
                S["pend"] = sig
                S["pend_vol"] = math.sqrt(S["var"]) * ANN
                S["pend_bar_ts"] = bar_ts
                S["pend_price"] = close_px
                return H1Decision(symbol=symbol, bar_ts=bar_ts, direction=sig,
                                  sigma_i=S["pend_vol"], signal_price=close_px,
                                  signal_hash=h1_signal_hash(symbol, bar_ts,
                                                             sig, S["pend_vol"]))
            S["pend"] = 0
        return None


# --------------------------------------------------------------------------
# EXECUTOR layer
# --------------------------------------------------------------------------
class Executor:
    """Executor interface. apply_target() must move the symbol to the target
    direction/notional atomically (close + open on a flip) at the given
    reference (bar open) price, and return a dict with at least 'executed'
    and 'mode'. Nothing in this module sends real orders."""

    def apply_target(self, decision: H1Decision, ref_price: float) -> dict:
        raise NotImplementedError

    def positions(self) -> Optional[list]:
        raise NotImplementedError


class NullExecutor(Executor):
    """The NO-ORDERS phase executor: records intents, sends NOTHING.

    This is the executor bound until the operator approves live trading
    (Bölüm 8 gate). It is also the executable proof that Bölüm 3 introduces
    no order path: there IS no code here that could send one."""

    def __init__(self):
        self.intents = []

    def apply_target(self, decision: H1Decision, ref_price: float) -> dict:
        self.intents.append({"symbol": decision.symbol,
                             "bar_ts": decision.bar_ts,
                             "direction": decision.direction,
                             "notional": decision.notional,
                             "ref_price": ref_price,
                             "label": STRATEGY_LABEL})
        return {"executed": False, "mode": "NULL_NO_ORDERS"}

    def positions(self) -> Optional[list]:
        return {}


# --------------------------------------------------------------------------
# RECONCILIATION layer
# --------------------------------------------------------------------------
@dataclass
class ReconItem:
    symbol: str
    target_direction: Optional[int]
    actual_direction: Optional[int]
    target_notional: Optional[float]
    actual_notional: Optional[float]
    status: str          # MATCH / DRIFT / UNKNOWN
    detail: str = ""


class H1Reconciler:
    """Target book (signal layer) vs actual book (exchange/paper truth).

    Direction-level comparison: the target notional is the §9 intent recorded
    at the last sizing; the actual notional is mark/entry price x positionAmt
    from the positionRisk-shaped rows. positions=None (unreadable) is UNKNOWN
    for every symbol — fail-closed, never 'no positions'."""

    def reconcile(self, engine: H1SignalEngine, positions: Optional[list],
                  equity: Optional[float] = None) -> list:
        items = []
        for s in engine.symbols:
            tdir = engine.direction(s)
            tn = engine.intended_notional(s)
            if positions is None:
                items.append(ReconItem(s, tdir, None, tn, None, "UNKNOWN",
                                       "positions unreadable"))
                continue
            row, amt, ok = None, 0.0, True
            for p in positions:
                if isinstance(p, dict) and p.get("symbol") == s:
                    row = p
                    break
            if row is not None:
                try:
                    amt = float(row.get("positionAmt", 0) or 0)
                except (TypeError, ValueError):
                    ok = False
            if not ok:
                items.append(ReconItem(s, tdir, None, tn, None, "UNKNOWN",
                                       "positionAmt unreadable"))
                continue
            adir = 0 if amt == 0 else (1 if amt > 0 else -1)
            an = None
            if row is not None and amt != 0:
                try:
                    px = float(row.get("markPrice") or row.get("entryPrice")
                               or 0)
                    an = abs(amt) * px
                except (TypeError, ValueError):
                    an = None
            status = "MATCH" if adir == tdir else "DRIFT"
            detail = (f"target {tdir:+d} vs actual {adir:+d}"
                      + (f", notional target {tn} vs actual {an}"
                         if (tn is not None or an is not None) else ""))
            items.append(ReconItem(s, tdir, adir, tn, an, status, detail))
        return items


# --------------------------------------------------------------------------
# ROUTER (signal -> risk -> executor, with the reconciliation gate)
# --------------------------------------------------------------------------
class H1Router:
    """Drives one 4h bar for one symbol through the layers, in the exact
    h1_engine.py order:
      (a) funding settlements <= this bar open, BEFORE the flip (Binance
          semantics — delegated to the world/broker hook when it models
          funding; the paper broker implements it in Bölüm 4)
      (b) pending flip executes at THIS bar open (sizing -> 18-check risk
          decision -> executor)
      (c)+(e) EWMA + next-bar signal; broker close hook
    """

    def __init__(self, engine: H1SignalEngine, checklist, executor, world,
                 reconciler=None, require_reconciliation: bool = True,
                 execute: bool = True, journal_sink=None):
        self.engine = engine
        self.checklist = checklist
        self.executor = executor
        self.world = world
        self.reconciler = reconciler or H1Reconciler()
        self.require_reconciliation = require_reconciliation
        self.execute = execute
        # optional durable sink (e.g. the paper ledger): every journal entry
        # is forwarded so measurement items survive restarts
        self.journal_sink = journal_sink
        # symbols whose target/actual divergence is EXPLAINED by a journaled
        # BLOCKED/MISSED decision (reported by recon, does not gate new trades)
        self._explained_drift = set()
        self.journal = []

    # ---- helpers ---------------------------------------------------------
    def _read(self, name, default=None):
        """Fail-closed world read (attribute or method): an outage reads as
        'unavailable', the checklist then blocks on UNKNOWN — never as 'no
        positions / no risk'."""
        try:
            v = getattr(self.world, name)
            if callable(v):
                v = v()
            return default if v is None else v
        except Exception:
            return default

    def _open_symbols(self, positions):
        if positions is None:
            return None
        out = set()
        for p in positions:
            if not isinstance(p, dict):
                return None
            try:
                if float(p.get("positionAmt", 0) or 0) != 0:
                    out.add(p["symbol"])
            except (TypeError, ValueError, KeyError):
                return None
        return out

    def _snapshot(self, decision: H1Decision) -> Snapshot:
        positions = self._read("positions")
        open_symbols = self._open_symbols(positions)
        # H1's own EWMA IS the vol model: book vols come from the engine so
        # the 18 checks see the same vols the sizing used
        vols = {s: self.engine.sigma(s) for s in self.engine.symbols}
        return Snapshot(
            equity=self._read("equity", 0.0),
            available_balance=self._read("available_balance"),
            positions=positions,
            open_symbols=open_symbols,
            active_executor_pairs=self._read("active_executor_pairs"),
            reserved_symbols=self._read("reserved_symbols"),
            target_symbol=decision.symbol,
            target_side=1 if decision.direction > 0 else 2,
            target_notional=decision.notional,
            target_symbol_vol=decision.sigma_i,
            bar_ts=decision.bar_ts,
            signal_hash=decision.signal_hash,
            symbol_vols=vols,
            corr=None,                    # checklist default = measured mean
            leverage_brackets=self._read("leverage_brackets", None),
            # a flip CLOSES+REOPENS the same symbol slot — it is not a
            # duplicate on the symbol (checklist treats it as replacement)
            target_replaces_existing=(decision.symbol in open_symbols
                                      if open_symbols is not None else False))

    def _recon_gate(self, decision: H1Decision) -> Optional[str]:
        """Fail-closed reconciliation gate, run BEFORE the 18 checks.
        The decision's own symbol is mid-transition by construction (the flip
        IS its reconciliation action) and is excluded; UNKNOWN anywhere, or
        an UNEXPLAINED drift on another symbol, blocks the execution."""
        if not self.require_reconciliation:
            return None
        positions = self._read("positions")
        items = self.reconciler.reconcile(self.engine, positions,
                                          self._read("equity"))
        blockers = [i for i in items if i.status == "UNKNOWN"]
        unexplained = [i for i in items
                       if i.status == "DRIFT"
                       and i.symbol != decision.symbol
                       and i.symbol not in self._explained_drift]
        if blockers or unexplained:
            why = [(i.symbol, i.status) for i in blockers + unexplained]
            return (f"reconciliation not verified: {why} — new executions "
                    f"blocked (fail-closed)")
        return None

    def _record(self, decision: H1Decision, d, outcome: str, reason: str,
                result=None):
        self.journal.append({
            "strategy": STRATEGY_ID,
            "label": STRATEGY_LABEL,
            "symbol": decision.symbol,
            "bar_ts": decision.bar_ts,          # signal (decision) time
            "signal_price": decision.signal_price,   # the decision bar close
            "execution_ts": decision.execution_ts,   # execution bar open time
            "ref_price": decision.ref_price,    # the execution open price
            "direction": decision.direction,
            "sigma_i": decision.sigma_i,
            "notional": decision.notional,      # target notional (§9)
            "signal_hash": decision.signal_hash,
            "outcome": outcome,
            "reason": reason,
            "checklist": (d.summary() if d is not None else None),
            "portfolio_vol": (getattr(d, "portfolio_vol", None)
                              if d is not None else None),
            "gross_leverage": (getattr(d, "gross_leverage", None)
                               if d is not None else None),
            "net_exposure": (getattr(d, "net_exposure", None)
                             if d is not None else None),
            "executor_result": result,
        })
        if self.journal_sink is not None:
            try:
                self.journal_sink(self.journal[-1])
            except Exception:
                pass               # a sink failure must never break trading

    # ---- the bar -----------------------------------------------------------
    def run_bar(self, symbol, bar_ts, open_px, close_px, execute=None):
        """One 4h bar for one symbol, h1_engine order (a)->(b)->(c)/(e).
        Returns the decision that was due at this bar open (if any)."""
        self.engine._state(symbol)              # unknown symbol -> ValueError
        execute = self.execute if execute is None else execute
        # (a) funding settles in (previous open, this open] BEFORE the flip
        settle = getattr(self.world, "settle_funding", None)
        if callable(settle):
            settle(symbol, bar_ts)
        # (b) pending flip at THIS bar open
        pend = self.engine.begin_bar(symbol, open_px)
        if pend is not None:
            pend.execution_ts = bar_ts
            pend.ref_price = open_px
            if execute:
                self._execute(pend, open_px)
            else:
                # history replay: the target advances but the paper book does
                # not — mark the divergence EXPLAINED so the recon gate does
                # not treat the warmup book as unexplained drift
                self._explained_drift.add(pend.symbol)
                self._record(pend, None, "WARMUP_SKIPPED",
                             "history replay: no execution, no dedup "
                             "consumption")
        # (c)+(e) EWMA + next-bar signal; broker close hook
        self.engine.end_bar(symbol, bar_ts, close_px)
        on_close = getattr(self.world, "on_bar_close", None)
        if callable(on_close):
            # BLOK27.5 (F-1): the mark carries the closed bar's OWN close
            # time (open + 4h). Warmup-replayed history therefore lands on
            # its true timestamp — never on the latest live bar's ts, the
            # old silent fallback that mis-stamped ~99% of marks rows.
            on_close(symbol, close_px, bar_close_ts(bar_ts))
        return pend

    def _execute(self, decision: H1Decision, ref_price: float):
        # sizing first (§9, with the CURRENT equity — h1_engine computes the
        # notional BEFORE realizing the exit leg of the flip)
        decision.notional = h1_target_notional(self._read("equity"),
                                               decision.sigma_i)
        if decision.notional is None:
            self._explained_drift.add(decision.symbol)
            self._record(decision, None, "MISSED_FAIL_CLOSED",
                         "equity/sigma unusable — no blind trade")
            return
        self.engine.note_intended_notional(decision.symbol,
                                           decision.notional)
        gate = self._recon_gate(decision)
        if gate is not None:
            self._explained_drift.add(decision.symbol)
            self._record(decision, None, "BLOCKED_RECONCILIATION", gate)
            return
        snap = self._snapshot(decision)
        d = self.checklist.evaluate(snap)
        if d.approved:
            res = self.executor.apply_target(decision, ref_price)
            self._explained_drift.discard(decision.symbol)
            mode = res.get("mode") if isinstance(res, dict) else "ok"
            self._record(decision, d, "EXECUTED", str(mode), result=res)
        else:
            # the target advanced but the book did not: the divergence is
            # EXPLAINED (journaled) — recon reports it without gating the
            # rest of the book forever
            self._explained_drift.add(decision.symbol)
            self._record(decision, d, "BLOCKED", d.reason)
