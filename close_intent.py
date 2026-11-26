"""close_intent.py — cross-PROCESS close idempotency for the CloseCoordinator.

inc12 made request_close atomic WITHIN a process (threading.RLock), but two
separate PROCESSES have no shared memory, so each could run a handoff and send a
close POST. inc12 proved the EXCHANGE (reduceOnly -2022) is the last-resort
backstop against a reverse SHORT, but that still allows a redundant second POST
(rejected, CRITICAL, noise). This moves the idempotency guarantee to the PROCESS
boundary with an ATOMIC close-intent, reusing the SAME shared SQLite ledger as
PositionReserver (risk_ledger.db, WAL + BEGIN IMMEDIATE) — no second database.

A close-intent is per (symbol, token). The token is a POSITION-PERIOD id derived
from exchange truth (side + entryPrice), because identifying a position by symbol
ALONE is unsafe:
  * ONEWAY: a new position opened on the same symbol after the old one closed must
    NOT inherit the old (terminal) intent.
  * HEDGE: a symbol can hold a LONG and a SHORT position at once — they are
    DIFFERENT positions and need different intents (the token carries the side).

States: PENDING (owner acquired, close not yet confirmed sent) -> SENT (close POST
issued) -> CLOSED (flat-confirmed, TERMINAL). acquire() is the atomic gate:
  ACQUIRED - this caller owns the intent; it may send exactly one close.
  BUSY     - an active (PENDING/SENT) intent with the SAME token exists: another
             process owns an in-flight close -> caller must NOT send a new POST;
             it queries the existing intent + exchange order state instead.
  CLOSED   - a terminal intent for the SAME token exists -> already closed; IGNORE.
  STALE    - an active intent exists for a DIFFERENT token (previous period, owner
             likely crashed) -> fail-closed; reconcile against exchange truth first.

UNKNOWN handling: on an ambiguous close outcome the caller does NOT release or
delete the intent; it stays PENDING/SENT so no other process blindly re-POSTs, and
reconcile() resolves it against positionRisk later (flat -> CLOSED terminal).
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


ACQUIRED = "ACQUIRED"
BUSY = "BUSY"
CLOSED = "CLOSED"
STALE = "STALE"

_ACTIVE = ("PENDING", "SENT")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IntentResult:
    status: str                 # ACQUIRED | BUSY | CLOSED | STALE
    symbol: str
    token: str
    state: Optional[str] = None  # the stored row's state, when one exists
    reason: str = ""


class CloseIntentStore:
    """Atomic, cross-process close-intent backed by the shared SQLite ledger."""

    def __init__(self, db_file):
        self.db_file = str(db_file)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS close_intents (
                    symbol TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
        finally:
            conn.close()

    def get(self, symbol: str) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT symbol, token, state, created_at, updated_at "
                "FROM close_intents WHERE symbol=?", (symbol,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ---- the atomic gate ------------------------------------------------
    def acquire(self, symbol: str, token: str) -> IntentResult:
        """Atomically become the close owner for (symbol, token), or report why not.

        One BEGIN IMMEDIATE transaction so concurrent processes serialise: at most
        one ACQUIRED per (symbol, token) period.
        """
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT token, state FROM close_intents WHERE symbol=?", (symbol,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO close_intents (symbol, token, state, created_at, updated_at) "
                    "VALUES (?, ?, 'PENDING', ?, ?)", (symbol, token, now, now))
                conn.execute("COMMIT")
                return IntentResult(ACQUIRED, symbol, token, "PENDING")

            r_token, r_state = row["token"], row["state"]
            if r_token == token:
                if r_state in _ACTIVE:
                    conn.execute("ROLLBACK")
                    return IntentResult(BUSY, symbol, token, r_state,
                                        "active intent for this position")
                # r_state == CLOSED (terminal) for the SAME token -> already closed.
                conn.execute("ROLLBACK")
                return IntentResult(CLOSED, symbol, token, r_state,
                                    "position already closed")

            # DIFFERENT token (a new/other position period).
            if r_state in _ACTIVE:
                # a previous period's intent is still active (owner likely crashed):
                # fail-closed; the caller must reconcile against exchange truth.
                conn.execute("ROLLBACK")
                return IntentResult(STALE, symbol, token, r_state,
                                    f"active intent for a different period ({r_token})")
            # previous period is terminal (CLOSED) -> start a fresh intent (new epoch).
            conn.execute(
                "UPDATE close_intents SET token=?, state='PENDING', created_at=?, "
                "updated_at=? WHERE symbol=?", (token, now, now, symbol))
            conn.execute("COMMIT")
            return IntentResult(ACQUIRED, symbol, token, "PENDING",
                                "superseded a terminal intent from a prior period")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _set_state(self, symbol: str, token: str, state: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE close_intents SET state=?, updated_at=? WHERE symbol=? AND token=?",
                (state, _utcnow_iso(), symbol, token))
            conn.execute("COMMIT")
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_sent(self, symbol: str, token: str) -> bool:
        """Record that the single close POST has been issued (still not confirmed)."""
        return self._set_state(symbol, token, "SENT")

    def mark_closed(self, symbol: str, token: str) -> bool:
        """TERMINAL: the position is flat-confirmed. Only call with real flat proof."""
        return self._set_state(symbol, token, "CLOSED")

    def release(self, symbol: str, token: str) -> bool:
        """Delete the intent — ONLY on a confirmed no-op (e.g. the exchange was
        already flat and nothing was sent). NEVER call on an UNKNOWN outcome."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM close_intents WHERE symbol=? AND token=?", (symbol, token))
            conn.execute("COMMIT")
            return cur.rowcount > 0
        finally:
            conn.close()

    def reconcile(self, symbol: str, exchange_flat: bool) -> Optional[str]:
        """Restart/UNKNOWN recovery against exchange truth. If the exchange is
        RELIABLY flat, move an active intent to CLOSED (terminal). If not flat, keep
        it (the position still needs handling). Returns the resulting state or None
        if there is no intent. Never deletes on UNKNOWN; caller passes exchange_flat
        only when positionRisk reliably confirmed amt == 0."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT token, state FROM close_intents WHERE symbol=?", (symbol,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            if exchange_flat and row["state"] in _ACTIVE:
                conn.execute(
                    "UPDATE close_intents SET state='CLOSED', updated_at=? WHERE symbol=?",
                    (_utcnow_iso(), symbol))
                conn.execute("COMMIT")
                return "CLOSED"
            conn.execute("COMMIT")
            return row["state"]
        finally:
            conn.close()
