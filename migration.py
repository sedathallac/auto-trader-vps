"""migration.py — safe handoff of ALREADY-OPEN positions to the K1=B authority.

On update/restart the exchange may already hold open positions. This migrates
each one to the new authority (exchange reduceOnly STOP_MARKET algo SL) WITHOUT
closing it, and never leaves it unprotected or double-closed. It only classifies
+ resolves; it never sends a close and never auto-places an SL (those are
separate procedures / the normal ensure_sl_coverage loop):

  NOT_OPEN   - positionRisk confirms flat (amt == 0): nothing to migrate.
  MIGRATED   - position open, HB neutralised (stop keep_position=True), every
               pre-sent close/TP order terminal, and a live exchange SL verified
               by real status: safe under the new authority (no second SL).
  UNPROTECTED- position open, resolved, but NO verified SL: OPEN_UNPROTECTED ->
               block new entries + CRITICAL (SL placement is a separate step).
  UNKNOWN    - position / order / SL / mode state unverifiable, or an order could
               not be resolved: HALT (fail-closed), block new entries.

Exchange truth only: local state is never trusted; the position is re-read after
order resolution (partial fill / a close filling during resolution). The injected
exchange is the real BinanceHBCloseAdapter in production and a mock in tests.
"""
from decimal import Decimal

from close_adapter import SL_ABSENT, SL_PRESENT


NOT_OPEN = "NOT_OPEN"
MIGRATED = "MIGRATED"
UNPROTECTED = "UNPROTECTED"
UNKNOWN = "UNKNOWN"

ONEWAY = "ONEWAY"
HEDGE = "HEDGE"


class MigrationResult:
    def __init__(self, symbol, state, reason="", amt=None, side=None, mode=None,
                 critical=False):
        self.symbol = symbol
        self.state = state
        self.reason = reason
        self.amt = amt
        self.side = side
        self.mode = mode
        self.critical = critical

    def __repr__(self):
        return f"<Migration {self.symbol} {self.state} {self.reason}>"


class PositionMigrator:
    """`exchange` provides: position_amt, position_mode, stop_executor,
    protective_orders, order_terminal, cancel_order, sl_status."""

    def __init__(self, exchange):
        self.ex = exchange

    def migrate(self, symbol) -> MigrationResult:
        # 1) real position (never trust local state)
        try:
            amt = Decimal(str(self.ex.position_amt(symbol)))
        except Exception as e:
            return MigrationResult(symbol, UNKNOWN, f"position query failed: {e}", critical=True)
        if amt == 0:
            return MigrationResult(symbol, NOT_OPEN, "exchange flat", amt=Decimal("0"))
        side = "LONG" if amt > 0 else "SHORT"

        # 2) position mode must be verifiable (ONEWAY/HEDGE)
        try:
            mode = self.ex.position_mode()
        except Exception as e:
            return MigrationResult(symbol, UNKNOWN, f"position mode unverifiable: {e}",
                                   amt=amt, side=side, critical=True)
        if mode not in (ONEWAY, HEDGE):
            return MigrationResult(symbol, UNKNOWN, f"unknown position mode {mode}",
                                   amt=amt, side=side, critical=True)

        # 3) neutralise HB (POSITION_HOLD) so it places no autonomous close
        try:
            self.ex.stop_executor(symbol)
        except Exception as e:
            return MigrationResult(symbol, UNKNOWN, f"stop_executor failed: {e}",
                                   amt=amt, side=side, mode=mode, critical=True)

        # 4) resolve pre-sent close/TP orders; any unresolved -> UNKNOWN (halt)
        try:
            pending = self.ex.protective_orders(symbol)
        except Exception as e:
            return MigrationResult(symbol, UNKNOWN, f"orders unverifiable: {e}",
                                   amt=amt, side=side, mode=mode, critical=True)
        for o in pending:
            if self.ex.order_terminal(o):
                continue
            if not self.ex.cancel_order(o):
                return MigrationResult(symbol, UNKNOWN, "pre-sent order unresolved",
                                       amt=amt, side=side, mode=mode, critical=True)

        # 5) RE-READ the position after resolution (partial fill / a close filling)
        try:
            amt2 = Decimal(str(self.ex.position_amt(symbol)))
        except Exception as e:
            return MigrationResult(symbol, UNKNOWN, f"re-read failed: {e}",
                                   amt=amt, side=side, mode=mode, critical=True)
        if amt2 == 0:
            return MigrationResult(symbol, NOT_OPEN, "flat after order resolution",
                                   amt=Decimal("0"), mode=mode)
        side = "LONG" if amt2 > 0 else "SHORT"

        # 6) verify the exchange SL by real tri-state status (PRESENT/ABSENT/UNKNOWN).
        # side is passed so a HEDGE SL is matched to THIS position side.
        try:
            sl = self.ex.sl_status(symbol, side)
        except Exception as e:
            return MigrationResult(symbol, UNKNOWN, f"sl_status failed: {e}",
                                   amt=amt2, side=side, mode=mode, critical=True)
        if sl == SL_PRESENT:
            # verified SL exists -> do NOT send a second SL
            return MigrationResult(symbol, MIGRATED, "protected by exchange SL",
                                   amt=amt2, side=side, mode=mode)
        if sl == SL_ABSENT:
            return MigrationResult(symbol, UNPROTECTED, "no verified SL",
                                   amt=amt2, side=side, mode=mode, critical=True)
        # SL_UNKNOWN or any unexpected value -> fail-closed HALT
        return MigrationResult(symbol, UNKNOWN, "SL status unverifiable",
                               amt=amt2, side=side, mode=mode, critical=True)


def migrate_all(exchange, open_symbols) -> dict:
    """Migrate every open symbol. Returns {symbol: MigrationResult}. New entries
    must be blocked while any result is UNKNOWN or UNPROTECTED (see
    block_new_entries)."""
    m = PositionMigrator(exchange)
    return {s: m.migrate(s) for s in open_symbols}


def block_new_entries(results) -> bool:
    """True if any migrated position is UNKNOWN or UNPROTECTED (unsafe)."""
    return any(r.state in (UNKNOWN, UNPROTECTED) for r in results.values())
