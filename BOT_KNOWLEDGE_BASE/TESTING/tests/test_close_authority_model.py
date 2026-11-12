"""Model-based regression test for the SOLUSDT double-close -> reverse position bug.

STATUS: WRITTEN on VPS, NOT RUN here (bash_guard blocks python/pytest on the VPS).
RUN IT ON ZORIN:  ~/.local/share/bot-tooling/venv/bin/pytest -q test_close_authority_model.py

This test is SELF-CONTAINED. It imports no live bot code and talks to no exchange.
It encodes a minimal model of Binance USDⓈ-M ONEWAY netting and two independent
close mechanisms, then asserts the safety invariant:

    F6/F15: a position-close must never open an opposite-side position.

Grounding: findings/2026-11-11_sol_double_close.md — exchange records show a 0.3 LONG
closed by order 788691 (reduceOnly, realized_pnl -0.2526) and then order 806439
(MARKET, not reduceOnly, realized_pnl 0) opened a -0.3 SHORT. HB net_pnl -0.270801
reconciles to open + 806439, proving 806439 was HB's (non-reduceOnly) close.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Tuple

D = Decimal


# --- Minimal exchange model: USDⓈ-M ONEWAY netting -------------------------------
@dataclass
class OneWayAccount:
    """Single netted position. SELL reduces/goes short, BUY increases/covers.

    reduce_only clamps the fill so it can never cross zero (Binance -2022 /
    'ReduceOnly Order is rejected' semantics): a reduce-only order only reduces
    the open position toward flat, never opens or flips.
    """
    pos: Decimal = D("0")          # signed base qty: >0 long, <0 short
    fills: List[Tuple[str, Decimal, bool]] = field(default_factory=list)

    def market(self, side: str, qty: Decimal, reduce_only: bool) -> Decimal:
        assert side in ("BUY", "SELL") and qty > 0
        signed = qty if side == "BUY" else -qty
        if reduce_only:
            # clamp so the result cannot cross zero
            if self.pos == 0 or (self.pos > 0) == (signed > 0):
                filled = D("0")                      # nothing to reduce / same side
            else:
                filled = signed if abs(signed) <= abs(self.pos) else -self.pos
        else:
            filled = signed
        self.pos += filled
        self.fills.append((side, filled, reduce_only))
        return filled


# --- The bug: two independent closers, second one not reduce-only ----------------
def _naive_double_close() -> OneWayAccount:
    acc = OneWayAccount(pos=D("0.3"))                # open LONG 0.3
    # closer A: exchange algo stop-loss, reduceOnly -> closes long to flat
    acc.market("SELL", D("0.3"), reduce_only=True)
    # closer B: HB ONEWAY close, NON reduce-only -> opens SHORT on flat book
    acc.market("SELL", D("0.3"), reduce_only=False)
    return acc


def test_naive_double_close_reproduces_reverse_position():
    """Documents the SOL bug: the naive design ends up SHORT -0.3."""
    acc = _naive_double_close()
    assert acc.pos == D("-0.3")                      # reverse position created (the bug)


def test_reduceonly_close_can_never_reverse():
    """Invariant primitive: a reduce-only close never crosses zero."""
    acc = OneWayAccount(pos=D("0.3"))
    acc.market("SELL", D("0.3"), reduce_only=True)
    assert acc.pos == D("0")
    acc.market("SELL", D("0.3"), reduce_only=True)   # second reduce-only on flat
    assert acc.pos == D("0")                          # still flat, no short opened


# --- The fix model: single close authority + reduce_only + flat confirmation -----
class SafeCloseCoordinator:
    """Single authority. Idempotent: at most one effective close per position.
    Forces reduce_only=True and confirms flat via the exchange before CLOSED."""
    def __init__(self, acc: OneWayAccount):
        self.acc = acc
        self._closed = False

    def request_close(self, qty: Decimal) -> str:
        if self._closed:
            return "IGNORED_ALREADY_CLOSED"           # idempotency (F15)
        side = "SELL" if self.acc.pos > 0 else "BUY"
        self.acc.market(side, abs(qty), reduce_only=True)  # F6: never reverse
        if self.acc.pos == 0:                          # F1: exchange flat confirm
            self._closed = True
            return "CLOSED_CONFIRMED"
        return "PARTIAL"


def test_safe_coordinator_prevents_reverse_on_double_trigger():
    acc = OneWayAccount(pos=D("0.3"))
    coord = SafeCloseCoordinator(acc)
    r1 = coord.request_close(D("0.3"))                # authoritative close
    r2 = coord.request_close(D("0.3"))                # duplicate trigger (e.g. 2nd SL)
    assert r1 == "CLOSED_CONFIRMED"
    assert r2 == "IGNORED_ALREADY_CLOSED"
    assert acc.pos == D("0")                          # invariant holds: no reverse


# --- Optional property-based check (skipped automatically if hypothesis absent) ---
try:
    from hypothesis import given, strategies as st

    @given(
        start=st.sampled_from([D("0.3"), D("-0.3"), D("1.5")]),
        triggers=st.integers(min_value=1, max_value=5),
    )
    def test_property_coordinator_never_reverses(start, triggers):
        acc = OneWayAccount(pos=start)
        coord = SafeCloseCoordinator(acc)
        for _ in range(triggers):
            coord.request_close(abs(start))
        # never flips sign relative to start, and ends flat
        assert acc.pos == D("0")
        assert not ((start > 0) and (acc.pos < 0))
        assert not ((start < 0) and (acc.pos > 0))
except Exception:  # pragma: no cover - hypothesis optional
    pass
