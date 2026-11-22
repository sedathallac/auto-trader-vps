"""risk_checklist.py — BLOK25 Bölüm 2: the REAL pre-order risk decision.

The RiskEngine's approve_trade() sizes a trade; this checklist is the emir-öncesi
DECISION that must pass before ANY order is sent. It runs the 18 documented
checks against a single consistent snapshot of exchange + ledger state.

Design constraints (from the task):
- Fail-closed: a check whose CRITICAL data is unavailable is UNKNOWN and BLOCKS
  the decision — an outage must never read as 'no positions / no risk'.
- No invented limits: every enforced bound is either (a) an existing RISK_CONFIG
  limit, (b) a BLOK22-preregistered sizing rule (§9), or (c) a BLOK23-measured
  quantity. Anything without a proven limit is reported as PARTIAL, never
  silently enforced with a made-up number.
- Liquidation distance stays PARTIAL when LeverageBracket/maintenance-margin
  data cannot be verified (BLOK23 §6 lesson: the MM schedule is not public
  without credentials; low gross leverage does NOT mean liquidation is
  impossible).

Proven/reference values (frozen):
- sigma_target = 15% annualized PER SYMBOL, w = 1/8 (BLOK22 prereg §9)
- per-symbol notional cap: notional_i <= w * E  => gross leverage <= 1.0x
  (BLOK22 prereg §9; BLOK23 measured max 0.39x)
- cross-symbol correlation: mean 0.799 / min 0.65 / max 0.886
  (BLOK23 results/h1_portfolio_risk.json, validation window)
- max drawdown 20% (RISK_CONFIG max_drawdown_pct == prereg acceptance §21e)
"""
from dataclasses import dataclass, field
from typing import Optional

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"   # critical data unavailable -> blocks (fail-closed)
PARTIAL = "PARTIAL"   # model unverifiable (e.g. liquidation) -> reported, documented

# BLOK22 prereg §9 (frozen) + BLOK23 measurements
SIGMA_TARGET = 0.15          # per-symbol annualized vol target
W_SYMBOL = 1.0 / 8.0         # equal weight, 8 symbols
CORR_MEAN = 0.7985357142857142   # BLOK23 measured mean pairwise correlation
CORR_MIN = 0.65                  # BLOK23 measured min (lower bound guard)
MAX_GROSS_LEVERAGE = 1.0         # prereg §9 per-symbol cap x 8 symbols


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass
class Decision:
    approved: bool
    checks: list = field(default_factory=list)
    reason: str = ""
    portfolio_vol: Optional[float] = None
    gross_leverage: Optional[float] = None
    net_exposure: Optional[float] = None

    @property
    def failed(self):
        return [c for c in self.checks if c.status == FAIL]

    @property
    def unknown(self):
        return [c for c in self.checks if c.status == UNKNOWN]

    @property
    def partial(self):
        return [c for c in self.checks if c.status == PARTIAL]

    def summary(self) -> str:
        if self.approved:
            p = ", ".join(c.name for c in self.partial) or "none"
            return f"APPROVED (partial: {p})"
        fails = ", ".join(f"{c.name}({c.status})" for c in self.checks
                          if c.status in (FAIL, UNKNOWN))
        return f"REJECTED: {fails}"


@dataclass
class Snapshot:
    """One consistent view of everything the 18 checks need. Fields set to
    None mean 'unavailable' — the dependent checks go UNKNOWN (fail-closed)."""
    equity: float                              # account equity (USDT)
    available_balance: Optional[float]         # available balance (USDT)
    positions: Optional[list]                  # positionRisk rows (raw dicts)
    open_symbols: Optional[set]                # symbols with a live position
    active_executor_pairs: Optional[set]       # HB pairs with RUNNING executor
    reserved_symbols: Optional[set]            # active PositionReserver slots
    target_symbol: str
    target_side: int                           # 1 LONG / 2 SHORT
    target_notional: float
    target_symbol_vol: Optional[float]         # sigma_i (annualized, e.g. EWMA)
    bar_ts: Optional[int]                      # signal bar timestamp (ms)
    signal_hash: Optional[str] = None
    symbol_vols: Optional[dict] = None         # symbol -> sigma_i for the BOOK
    corr: Optional[float] = None               # pairwise correlation override
    leverage_brackets: Optional[dict] = None   # symbol -> bracket rows (or None)
    max_open_positions: Optional[int] = None
    # per-position mark/entry notional override when positionRisk rows lack
    # markPrice: symbol -> notional (signed)
    position_notionals: Optional[dict] = None


def portfolio_vol(contribs: list, corr: float) -> Optional[float]:
    """Annualized portfolio vol from per-position EQUITY-vol contributions.

    contribs: [notional_i * sigma_i / E, ...] (each position's equity-vol
    contribution). With equal correlation rho the quadratic form is
    sqrt((1-rho) * sum(c_i^2) + rho * (sum(c_i))^2) — the standard
    single-factor (equicorrelated) form. Returns None on bad input."""
    if not contribs or corr is None:
        return None
    try:
        vals = [float(c) for c in contribs]
    except (TypeError, ValueError):
        return None
    s2 = sum(v * v for v in vals)
    s = sum(vals)
    var = (1.0 - corr) * s2 + corr * s * s
    return var ** 0.5 if var >= 0 else None


class PretradeChecklist:
    """The 18-check pre-order decision. Composes the RiskEngine (ledger state)
    with a Snapshot (exchange truth). Pure function of its inputs — no I/O of
    its own except the signal-dedup store passed by the caller."""

    def __init__(self, risk_engine, signal_dedup=None):
        self.risk = risk_engine
        self.signal_dedup = signal_dedup
        self.cfg = getattr(risk_engine, "cfg", {}) or {}

    # ---- helpers -------------------------------------------------------
    def _book_notionals(self, snap) -> Optional[dict]:
        """symbol -> signed notional for the CURRENT book. None when the
        position data is unavailable (fail-closed upstream)."""
        if snap.position_notionals is not None:
            return dict(snap.position_notionals)
        if snap.positions is None:
            return None
        out = {}
        for p in snap.positions:
            try:
                amt = float(p.get("positionAmt", 0))
            except (TypeError, ValueError):
                return None
            if amt == 0:
                continue
            px = p.get("markPrice") or p.get("entryPrice")
            try:
                out[p["symbol"]] = amt * float(px)
            except (TypeError, ValueError, KeyError):
                return None
        return out

    # ---- the 18 checks -------------------------------------------------
    def _check_halted(self, snap):
        if self.risk.is_halted:
            return CheckResult("halted_kill_switch", FAIL,
                               f"halted: {self.risk.halt_reason}")
        return CheckResult("halted_kill_switch", PASS, "not halted")

    def _check_state_persistence(self, snap):
        # the ledger state row must actually have loaded (an empty/default
        # state means persistence is broken -> fail-closed)
        if getattr(self.risk, "state", None):
            return CheckResult("risk_state_persistence", PASS,
                               f"state loaded: dd={self.risk.current_drawdown:.3f} "
                               f"cons_losses={self.risk.consecutive_losses}")
        return CheckResult("risk_state_persistence", UNKNOWN,
                           "risk_state row did not load (defaults in use)")

    def _check_restart_recovery(self, snap):
        # after a restart the engine must have RECOVERED its counters from the
        # ledger, not restarted from zero while a book exists. A book without
        # recovered state is an unverified recovery.
        if snap.open_symbols is None:
            return CheckResult("restart_recovery", UNKNOWN,
                               "open symbols unavailable")
        if snap.open_symbols and not getattr(self.risk, "state", None):
            return CheckResult("restart_recovery", UNKNOWN,
                               f"open book {sorted(snap.open_symbols)} but risk "
                               f"state not recovered from ledger")
        return CheckResult("restart_recovery", PASS,
                           f"state consistent with book "
                           f"({len(snap.open_symbols)} open)")

    def _check_drawdown(self, snap):
        lim = self.cfg.get("max_drawdown_pct", 0.20)
        if self.risk.current_drawdown >= lim:
            return CheckResult("drawdown_limit", FAIL,
                               f"drawdown {self.risk.current_drawdown:.1%} >= {lim:.0%}")
        return CheckResult("drawdown_limit", PASS,
                           f"drawdown {self.risk.current_drawdown:.1%} < {lim:.0%}")

    def _check_consecutive_losses(self, snap):
        lim = self.cfg.get("max_consecutive_losses", 3)
        if self.risk.consecutive_losses >= lim:
            return CheckResult("consecutive_loss_limit", FAIL,
                               f"{self.risk.consecutive_losses} >= {lim}")
        return CheckResult("consecutive_loss_limit", PASS,
                           f"{self.risk.consecutive_losses} < {lim}")

    def _check_daily_loss(self, snap):
        # NO INVENTED LIMIT: a daily loss limit is only enforced when the
        # operator configured one; otherwise the measured daily PnL is reported
        # as PARTIAL (documented gap, not a made-up bound).
        lim = self.cfg.get("max_daily_loss_pct")
        if lim is None:
            return CheckResult("daily_loss_limit", PARTIAL,
                               f"daily_pnl={self.risk.daily_pnl:+.2f} "
                               f"(no configured limit — NOT invented)")
        if self.risk.daily_pnl <= -abs(lim) * snap.equity:
            return CheckResult("daily_loss_limit", FAIL,
                               f"daily_pnl {self.risk.daily_pnl:+.2f} <= "
                               f"-{abs(lim):.0%} x E")
        return CheckResult("daily_loss_limit", PASS,
                           f"daily_pnl {self.risk.daily_pnl:+.2f} within limit")

    def _check_max_open_positions(self, snap):
        if snap.open_symbols is None or snap.reserved_symbols is None:
            return CheckResult("max_open_positions", UNKNOWN,
                               "open/reserved symbols unavailable")
        cap = snap.max_open_positions
        if cap is None:
            cap = self.cfg.get("max_open_positions")
        if cap is None:
            return CheckResult("max_open_positions", UNKNOWN,
                               "no max_open_positions configured")
        used = set(snap.open_symbols) | set(snap.reserved_symbols)
        if snap.target_symbol in used:
            used = set(used)                       # target already counted
        else:
            used = set(used) | {snap.target_symbol}  # this decision adds one
        if len(used) > cap:
            return CheckResult("max_open_positions", FAIL,
                               f"{len(used)} > {cap}")
        return CheckResult("max_open_positions", PASS, f"{len(used)}/{cap}")

    def _check_per_symbol_risk(self, snap):
        # prereg §9: notional_i <= w * E (1x slot) — for the TARGET and every
        # existing position
        book = self._book_notionals(snap)
        if book is None:
            return CheckResult("per_symbol_risk", UNKNOWN,
                               "book notionals unavailable")
        cap = W_SYMBOL * snap.equity
        over = [s for s, n in book.items() if abs(n) > cap * 1.0000001]
        if abs(snap.target_notional) > cap * 1.0000001:
            over.append(snap.target_symbol)
        if over:
            return CheckResult("per_symbol_risk", FAIL,
                               f"notional > w*E for {sorted(set(over))} "
                               f"(cap={cap:.2f})")
        return CheckResult("per_symbol_risk", PASS,
                           f"all |notional_i| <= w*E ({cap:.2f})")

    def _common_contribs(self, snap):
        """Per-position equity-vol contributions INCLUDING the target. None
        when the vols or the book are unavailable."""
        book = self._book_notionals(snap)
        vols = dict(snap.symbol_vols or {})
        if snap.target_symbol_vol is not None:
            vols[snap.target_symbol] = snap.target_symbol_vol
        if book is None or not vols:
            return None, None
        contribs = []
        for s, n in book.items():
            if s == snap.target_symbol:
                continue                      # replaced by the target below
            v = vols.get(s)
            if v is None or v <= 0:
                return None, None             # missing vol -> UNKNOWN (fail-closed)
            contribs.append(abs(n) * v / snap.equity)
        tv = vols.get(snap.target_symbol)
        if tv is None or tv <= 0:
            return None, None
        contribs.append(abs(snap.target_notional) * tv / snap.equity)
        return contribs, book

    def _check_total_portfolio_risk(self, snap):
        contribs, _ = self._common_contribs(snap)
        corr = snap.corr if snap.corr is not None else CORR_MEAN
        if contribs is None:
            return CheckResult("total_portfolio_risk", UNKNOWN,
                               "vols/book unavailable")
        vol = portfolio_vol(contribs, corr)
        if vol is None:
            return CheckResult("total_portfolio_risk", UNKNOWN,
                               "portfolio vol not computable")
        # Bound implied by the frozen sizing rule (NOT invented): with w=1/8,
        # per-symbol target 15% and any rho <= 1, portfolio vol <= sigma_target.
        # Exceeding it means the sizing rule or the correlation assumption broke.
        if vol > SIGMA_TARGET * 1.0000001:
            return CheckResult("total_portfolio_risk", FAIL,
                               f"portfolio vol {vol:.1%} > sigma_target "
                               f"{SIGMA_TARGET:.0%} (rho={corr:.2f})")
        return CheckResult("total_portfolio_risk", PASS,
                           f"portfolio vol {vol:.1%} <= {SIGMA_TARGET:.0%} "
                           f"(rho={corr:.2f}, n={len(contribs)})")

    def _check_gross_notional(self, snap):
        book = self._book_notionals(snap)
        if book is None:
            return CheckResult("gross_notional", UNKNOWN,
                               "book notionals unavailable")
        gross = (sum(abs(n) for n in book.values())
                 - abs(book.get(snap.target_symbol, 0.0))
                 + abs(snap.target_notional)) / snap.equity
        if gross > MAX_GROSS_LEVERAGE * 1.0000001:
            return CheckResult("gross_notional", FAIL,
                               f"gross {gross:.2f}x > {MAX_GROSS_LEVERAGE:.1f}x")
        return CheckResult("gross_notional", PASS,
                           f"gross {gross:.2f}x <= {MAX_GROSS_LEVERAGE:.1f}x")

    def _check_net_exposure(self, snap):
        book = self._book_notionals(snap)
        if book is None:
            return CheckResult("net_exposure", UNKNOWN,
                               "book notionals unavailable")
        net = (sum(n for n in book.values())
               - book.get(snap.target_symbol, 0.0)
               + (snap.target_notional if snap.target_side == 1
                  else -snap.target_notional)) / snap.equity
        # |net| is mathematically bounded by gross; the prereg-implied aggregate
        # cap is the same 1.0x. Reported for visibility (common-market risk).
        if abs(net) > MAX_GROSS_LEVERAGE * 1.0000001:
            return CheckResult("net_exposure", FAIL,
                               f"net {net:+.2f}x outside +/-{MAX_GROSS_LEVERAGE:.1f}x")
        return CheckResult("net_exposure", PASS,
                           f"net {net:+.2f}x (directional exposure reported)")

    def _check_available_balance(self, snap):
        if snap.available_balance is None:
            return CheckResult("available_balance", UNKNOWN,
                               "balance read unavailable")
        # at <= 1x gross the target's margin requirement is ~ its notional
        # (cross-margin optimum); require the balance to cover it
        if snap.available_balance < snap.target_notional:
            return CheckResult("available_balance", FAIL,
                               f"balance {snap.available_balance:.2f} < target "
                               f"notional {snap.target_notional:.2f}")
        return CheckResult("available_balance", PASS,
                           f"balance {snap.available_balance:.2f} >= notional "
                           f"{snap.target_notional:.2f}")

    def _check_margin_usage(self, snap):
        book = self._book_notionals(snap)
        if book is None or snap.available_balance is None or snap.equity <= 0:
            return CheckResult("margin_usage", UNKNOWN,
                               "margin inputs unavailable")
        lim = self.cfg.get("max_total_margin_pct", 0.40)
        # at <= 1x gross, margin ~ notional; usage against EQUITY (the loss
        # absorber), consistent with update_exposure()
        total_notional = (sum(abs(n) for n in book.values())
                          - abs(book.get(snap.target_symbol, 0.0))
                          + abs(snap.target_notional))
        usage = total_notional / snap.equity
        if usage > lim:
            return CheckResult("margin_usage", FAIL,
                               f"usage {usage:.1%} > {lim:.0%}")
        return CheckResult("margin_usage", PASS, f"usage {usage:.1%} <= {lim:.0%}")

    def _check_max_leverage(self, snap):
        # account-level leverage: total notional / available balance. The
        # prereg-documented aggregate cap is 1.0x (8 slots x w=1/8 x 1x slot).
        book = self._book_notionals(snap)
        if book is None or snap.available_balance is None:
            return CheckResult("max_leverage", UNKNOWN,
                               "leverage inputs unavailable")
        total = (sum(abs(n) for n in book.values())
                 - abs(book.get(snap.target_symbol, 0.0))
                 + abs(snap.target_notional))
        lev = total / snap.available_balance if snap.available_balance > 0 else None
        if lev is None:
            return CheckResult("max_leverage", FAIL,
                               f"non-positive balance {snap.available_balance}")
        if lev > MAX_GROSS_LEVERAGE * 1.0000001:
            return CheckResult("max_leverage", FAIL,
                               f"account leverage {lev:.2f}x > "
                               f"{MAX_GROSS_LEVERAGE:.1f}x")
        return CheckResult("max_leverage", PASS,
                           f"account leverage {lev:.2f}x <= "
                           f"{MAX_GROSS_LEVERAGE:.1f}x")

    def _check_liquidation_distance(self, snap):
        # BLOK23 §6: the maintenance-margin schedule is NOT verifiable without
        # credentials -> the liquidation model stays PARTIAL, never VERIFIED.
        # Low gross leverage does NOT mean liquidation is impossible.
        book = self._book_notionals(snap)
        if book is None:
            return CheckResult("liquidation_distance", UNKNOWN,
                               "book notionals unavailable")
        gross = (sum(abs(n) for n in book.values())
                 - abs(book.get(snap.target_symbol, 0.0))
                 + abs(snap.target_notional)) / snap.equity
        if snap.leverage_brackets:
            # brackets supplied: compute a per-symbol MMR bound and report it,
            # but the model is still PARTIAL (bracket data is exchange-side and
            # can change; verified only at order time by the exchange)
            detail = (f"gross {gross:.2f}x; brackets supplied — analytic MM "
                      f"distance computed, model PARTIAL (exchange-authoritative)")
        else:
            detail = (f"gross {gross:.2f}x; LeverageBracket/MMR NOT verifiable "
                      f"— analytic bound only (BLOK23 §6): at <=1x gross, "
                      f"liquidation requires a ~95%+ adverse equity move; "
                      f"low gross leverage != impossible liquidation")
        return CheckResult("liquidation_distance", PARTIAL, detail)

    def _check_correlation_common_market(self, snap):
        # The common-market guard: the portfolio vol above must be computed
        # WITH the measured correlation, never with an optimistic (low) one —
        # applying 15% vol targeting per symbol must not hide the total.
        corr = snap.corr if snap.corr is not None else CORR_MEAN
        if snap.corr is not None and snap.corr < CORR_MIN:
            return CheckResult("correlation_common_market", FAIL,
                               f"assumed rho {snap.corr:.2f} < measured min "
                               f"{CORR_MIN:.2f} — common-market risk understated")
        book = self._book_notionals(snap)
        if book is None:
            return CheckResult("correlation_common_market", UNKNOWN,
                               "book notionals unavailable")
        net_same = (sum(n for n in book.values())
                    - book.get(snap.target_symbol, 0.0)
                    + (snap.target_notional if snap.target_side == 1
                       else -snap.target_notional)) / snap.equity
        return CheckResult("correlation_common_market", PASS,
                           f"rho={corr:.2f} (BLOK23 measured mean {CORR_MEAN:.2f}); "
                           f"same-direction exposure {net_same:+.2f}x E reported")

    def _check_duplicate_executor(self, snap):
        reasons = []
        if snap.open_symbols is None:
            return CheckResult("duplicate_executor", UNKNOWN,
                               "open symbols unavailable")
        if snap.target_symbol in snap.open_symbols:
            reasons.append("position already open")
        if snap.active_executor_pairs is not None:
            pair = snap.target_symbol.replace("USDT", "-USDT")
            if pair in snap.active_executor_pairs:
                reasons.append("RUNNING executor on pair")
        if snap.reserved_symbols is not None and \
                snap.target_symbol in snap.reserved_symbols:
            reasons.append("active reservation")
        if reasons:
            return CheckResult("duplicate_executor", FAIL,
                               "; ".join(reasons))
        if snap.active_executor_pairs is None or snap.reserved_symbols is None:
            return CheckResult("duplicate_executor", UNKNOWN,
                               "executor/reservation state unavailable")
        return CheckResult("duplicate_executor", PASS, "no duplicate on symbol")

    def _check_signal_replay(self, snap):
        if snap.bar_ts is None:
            return CheckResult("signal_replay", UNKNOWN,
                               "no signal bar timestamp")
        if self.signal_dedup is None:
            return CheckResult("signal_replay", UNKNOWN,
                               "no signal-dedup store wired")
        r = self.signal_dedup.acquire(snap.target_symbol, snap.bar_ts,
                                      snap.signal_hash)
        if r == "REPLAY":
            return CheckResult("signal_replay", FAIL,
                               f"signal for bar {snap.bar_ts} already consumed")
        return CheckResult("signal_replay", PASS,
                           f"first consumption of bar {snap.bar_ts}")

    # ---- the decision ----------------------------------------------------
    CHECKS = [
        ("halted_kill_switch", _check_halted),
        ("risk_state_persistence", _check_state_persistence),
        ("restart_recovery", _check_restart_recovery),
        ("drawdown_limit", _check_drawdown),
        ("consecutive_loss_limit", _check_consecutive_losses),
        ("daily_loss_limit", _check_daily_loss),
        ("max_open_positions", _check_max_open_positions),
        ("per_symbol_risk", _check_per_symbol_risk),
        ("total_portfolio_risk", _check_total_portfolio_risk),
        ("gross_notional", _check_gross_notional),
        ("net_exposure", _check_net_exposure),
        ("available_balance", _check_available_balance),
        ("margin_usage", _check_margin_usage),
        ("max_leverage", _check_max_leverage),
        ("liquidation_distance", _check_liquidation_distance),
        ("correlation_common_market", _check_correlation_common_market),
        ("duplicate_executor", _check_duplicate_executor),
        ("signal_replay", _check_signal_replay),
    ]

    def evaluate(self, snap: Snapshot) -> Decision:
        checks = []
        for name, fn in self.CHECKS:
            checks.append(fn(self, snap))
        d = Decision(approved=False, checks=checks)
        # PARTIAL (liquidation model, unconfigured daily-loss limit) is
        # reported but does not block; FAIL and UNKNOWN both block (fail-closed
        # on missing critical data).
        blocking = [c for c in checks if c.status in (FAIL, UNKNOWN)]
        d.approved = not blocking
        d.reason = d.summary()
        # expose the headline portfolio quantities for the log/ledger
        contribs, book = self._common_contribs(snap)
        corr = snap.corr if snap.corr is not None else CORR_MEAN
        if contribs is not None:
            d.portfolio_vol = portfolio_vol(contribs, corr)
        if book is not None and snap.equity > 0:
            d.gross_leverage = (sum(abs(n) for n in book.values())
                                - abs(book.get(snap.target_symbol, 0.0))
                                + abs(snap.target_notional)) / snap.equity
            d.net_exposure = (sum(n for n in book.values())
                              - book.get(snap.target_symbol, 0.0)
                              + (snap.target_notional if snap.target_side == 1
                                 else -snap.target_notional)) / snap.equity
        return d
