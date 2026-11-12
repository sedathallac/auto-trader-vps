"""close_coordinator.py — single close authority for the futures bot (K1=B).

Decision K1=B (permanent): the Binance exchange reduceOnly STOP_MARKET algo order
is the sole STOP-LOSS authority. Every OTHER close (take-profit, time-limit,
manual, reconcile-driven) goes through this ONE CloseCoordinator, which:

- runs the HANDOFF state machine against EXCHANGE TRUTH before handing a position
  to the exchange SL authority:
    HANDOFF_PENDING -> OLD_ORDERS_RESOLVED -> EXCHANGE_STATE_VERIFIED
    -> EXTERNAL_AUTHORITY_ACTIVE  (fail-closed: any failed step stays / FAILED_SAFE)
- forces a reduce-only close in ONEWAY (Binance -2022 guarantees a reduce-only
  order can never cross zero -> it can never open a reverse position), and a
  positionSide-targeted close in HEDGE (reduceOnly is rejected in HEDGE);
- is idempotent per symbol: at most ONE effective close; a delayed / duplicate
  trigger (second SL, late TP, WebSocket/REST replay) after CLOSED is IGNORED,
  so it can never create the SOLUSDT-style reverse SHORT;
- confirms the exchange is FLAT (positionRisk positionAmt == 0) before marking a
  position CLOSED (K2); if not flat it raises CRITICAL and does NOT blindly send a
  second order.

The exchange/HB side is an injected interface so this is unit-testable with a
mock that models ONEWAY netting + reduce-only clamping, and drives the real
BinanceAPI/HBAPI in production. No order is sent except the single coordinated
reduce-only/positionSide close.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict


class CloseState(str, Enum):
    HANDOFF_PENDING = "HANDOFF_PENDING"
    OLD_ORDERS_RESOLVED = "OLD_ORDERS_RESOLVED"
    EXCHANGE_STATE_VERIFIED = "EXCHANGE_STATE_VERIFIED"
    EXTERNAL_AUTHORITY_ACTIVE = "EXTERNAL_AUTHORITY_ACTIVE"
    CLOSED = "CLOSED"
    FAILED_SAFE = "FAILED_SAFE"


ONEWAY = "ONEWAY"
HEDGE = "HEDGE"


@dataclass
class CloseResult:
    ok: bool
    state: CloseState
    action: str                 # SENT_REDUCE_ONLY | IGNORED_ALREADY_CLOSED | ...
    reason: str = ""
    critical: bool = False      # True -> caller must raise a CRITICAL alarm
    detail: dict = field(default_factory=dict)


class CloseCoordinatorError(Exception):
    pass


class CloseCoordinator:
    """Single, idempotent, fail-closed close authority. `exchange` must provide:

        position_amt(symbol) -> Decimal        # signed: >0 long, <0 short, 0 flat
        position_mode() -> "ONEWAY" | "HEDGE"
        protective_orders(symbol) -> list[dict]  # open close/TP/conditional orders
        order_terminal(order) -> bool          # True if FILLED/CANCELED/EXPIRED
        cancel_order(order) -> bool            # request cancel; returns terminal?
        stop_executor(symbol) -> None          # stop HB executor keep_position=True
        close_reduce_only(symbol, qty, position_side=None) -> Decimal  # filled qty
    """

    def __init__(self, exchange):
        self.ex = exchange
        self._state: Dict[str, CloseState] = {}
        self._closed: set = set()

    def state_of(self, symbol: str) -> CloseState:
        if symbol in self._closed:
            return CloseState.CLOSED
        return self._state.get(symbol, CloseState.HANDOFF_PENDING)

    # ---- the single close entry point -----------------------------------
    def request_close(self, symbol: str, close_type: str = "MANUAL") -> CloseResult:
        # Idempotency (F15): one effective close; late/duplicate triggers ignored.
        if symbol in self._closed:
            return CloseResult(True, CloseState.CLOSED, "IGNORED_ALREADY_CLOSED",
                               reason=f"already closed (trigger={close_type})")

        # 1) HANDOFF_PENDING: stop the HB executor WITHOUT letting it place its own
        #    close (keep_position=True == POSITION_HOLD) so no autonomous close/TP
        #    races this coordinator.
        try:
            self.ex.stop_executor(symbol)
            self._state[symbol] = CloseState.HANDOFF_PENDING
        except Exception as e:
            self._state[symbol] = CloseState.FAILED_SAFE
            return CloseResult(False, CloseState.FAILED_SAFE, "STOP_FAILED",
                               reason=f"stop_executor failed: {e}", critical=True)

        # 2) OLD_ORDERS_RESOLVED: cancel pre-existing close/TP/conditional orders
        #    and verify they are terminal. An in-flight / UNKNOWN order means we do
        #    NOT send a new close (it could double-fill) -> stay, fail-closed.
        try:
            pending = self.ex.protective_orders(symbol)
        except Exception as e:
            self._state[symbol] = CloseState.FAILED_SAFE
            return CloseResult(False, CloseState.FAILED_SAFE, "ORDERS_QUERY_FAILED",
                               reason=str(e), critical=True)
        for o in pending:
            if self.ex.order_terminal(o):
                continue
            terminal = self.ex.cancel_order(o)
            if not terminal:
                # cancel ack unknown / order still live -> do not send a new close
                return CloseResult(False, CloseState.HANDOFF_PENDING,
                                   "UNRESOLVED_ORDER",
                                   reason="pre-existing order not terminal; "
                                          "refusing to send a second close",
                                   critical=True, detail={"order": o})
        self._state[symbol] = CloseState.OLD_ORDERS_RESOLVED

        # 3) EXCHANGE_STATE_VERIFIED: read the real position from the exchange.
        try:
            amt = self.ex.position_amt(symbol)
        except Exception as e:
            self._state[symbol] = CloseState.FAILED_SAFE
            return CloseResult(False, CloseState.FAILED_SAFE, "POSITION_QUERY_FAILED",
                               reason=str(e), critical=True)
        amt = Decimal(amt)
        self._state[symbol] = CloseState.EXCHANGE_STATE_VERIFIED
        if amt == 0:
            # Already flat -> nothing to close; hand over cleanly.
            self._closed.add(symbol)
            return CloseResult(True, CloseState.CLOSED, "ALREADY_FLAT",
                               reason="exchange flat before close")

        # 4) EXTERNAL_AUTHORITY_ACTIVE: send exactly ONE reduce-only / positionSide
        #    close, then confirm flat. reduce-only can never cross zero (-2022).
        mode = self.ex.position_mode()
        qty = abs(amt)
        position_side = None
        if mode == HEDGE:
            position_side = "LONG" if amt > 0 else "SHORT"
        try:
            self.ex.close_reduce_only(symbol, qty, position_side=position_side)
        except Exception as e:
            self._state[symbol] = CloseState.FAILED_SAFE
            return CloseResult(False, CloseState.FAILED_SAFE, "CLOSE_SEND_FAILED",
                               reason=str(e), critical=True)

        # Flat confirmation (K2): the executor stopping / the order returning is NOT
        # proof the position closed — only positionRisk is.
        try:
            after = Decimal(self.ex.position_amt(symbol))
        except Exception as e:
            return CloseResult(False, CloseState.EXTERNAL_AUTHORITY_ACTIVE,
                               "FLAT_CONFIRM_FAILED", reason=str(e), critical=True)
        # Invariant F6: a close must never flip the sign.
        if (amt > 0 and after < 0) or (amt < 0 and after > 0):
            self._state[symbol] = CloseState.FAILED_SAFE
            return CloseResult(False, CloseState.FAILED_SAFE, "REVERSED",
                               reason=f"close reversed position {amt} -> {after}",
                               critical=True, detail={"before": str(amt), "after": str(after)})
        if after == 0:
            self._state[symbol] = CloseState.EXTERNAL_AUTHORITY_ACTIVE
            self._closed.add(symbol)
            return CloseResult(True, CloseState.CLOSED, "SENT_REDUCE_ONLY",
                               reason="closed and flat-confirmed")
        # Partial close: remaining exposure is preserved; do NOT blindly resend.
        return CloseResult(False, CloseState.EXCHANGE_STATE_VERIFIED, "PARTIAL",
                           reason=f"partial close, remaining={after}",
                           critical=True, detail={"remaining": str(after)})
