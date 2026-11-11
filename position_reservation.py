"""position_reservation.py — atomic position-slot reservation (TOCTOU fix).

Root cause this fixes: auto_trader admitted a new position by reading an
eventually-consistent counter (Hummingbot active_executors() RUNNING) and only
then calling create_executor. A just-created executor is not RUNNING yet, so two
candidates (next loop iteration, or a second process) could both read
used < MAX_OPEN_POSITIONS and both open, breaching the cap. Classic
check-then-act TOCTOU with no atomic reserve.

This module reserves a slot ATOMICALLY in the shared SQLite ledger
(risk_ledger.db, WAL + transactional, cross-process) BEFORE create_executor:

  used = | set(exchange_open_symbols)  UNION  {active reservation symbols} |

is counted AND the reservation is inserted inside a single `BEGIN IMMEDIATE`
transaction, so SQLite serialises concurrent reservers and at most
MAX_OPEN_POSITIONS distinct slots can ever exist across all processes.

Lifecycle:
  reserve()  -> hold a slot before the create_executor POST.
  release()  -> confirmed failure (executor NOT created): free the slot.
  (UNKNOWN)  -> caller does NOT release; the slot stays held.
  reconcile()-> resolve reservations against real exchange positions:
                symbol now open on exchange   -> delete (promoted; the real
                                                 position now represents the slot)
                past TTL and still no position -> delete (release; create failed
                                                 or UNKNOWN resolved to no-position)
                fresh and no position yet      -> keep (still in-flight)
  Reservations persist in the db, so a process restart recovers them and a
  startup reconcile() against positionRisk cleans up / promotes them.

No live orders, no exchange calls here: the caller passes the current exchange
open symbols (from positionRisk) in. Deterministic and unit-testable.
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass
class ReserveResult:
    ok: bool
    reservation_id: Optional[int]
    reason: str
    used: int  # distinct slots used at decision time (after this reservation if ok)


@dataclass
class ReconcileReport:
    promoted: List[dict] = field(default_factory=list)  # became real positions
    released: List[dict] = field(default_factory=list)  # stale -> freed
    kept: List[dict] = field(default_factory=list)       # still in-flight


class PositionReserver:
    """Atomic, cross-process slot reservation backed by SQLite."""

    def __init__(self, db_file, max_positions: int):
        if max_positions < 0:
            raise ValueError("max_positions must be >= 0")
        self.db_file = str(db_file)
        self.max_positions = int(max_positions)

    def _connect(self) -> sqlite3.Connection:
        # One connection per operation (thread/process safe). isolation_level=None
        # so we drive transactions explicitly with BEGIN IMMEDIATE.
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
                CREATE TABLE IF NOT EXISTS position_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # One active reservation per symbol (backstop against races + prevents
            # duplicate opens of the same symbol).
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_reservation_symbol
                ON position_reservations(symbol)
            """)
        finally:
            conn.close()

    # ---- counting -------------------------------------------------------
    def _reserved_symbols(self, conn) -> set:
        rows = conn.execute("SELECT symbol FROM position_reservations").fetchall()
        return {r["symbol"] for r in rows}

    def used_slots(self, exchange_open_symbols) -> int:
        conn = self._connect()
        try:
            union = set(exchange_open_symbols) | self._reserved_symbols(conn)
            return len(union)
        finally:
            conn.close()

    def active_reservations(self) -> List[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, symbol, state, created_at FROM position_reservations "
                "ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- the atomic admission -------------------------------------------
    def reserve(self, symbol: str, exchange_open_symbols) -> ReserveResult:
        """Atomically reserve a slot for `symbol`, or reject.

        Rejects if the symbol is already open/reserved (no duplicate) or if the
        cap would be exceeded. Count + insert happen in one BEGIN IMMEDIATE tx so
        concurrent callers (threads or processes) can never both succeed past the
        cap.
        """
        exch = set(exchange_open_symbols)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")  # serialise writers across processes
            reserved = self._reserved_symbols(conn)
            union = exch | reserved

            if symbol in union:
                conn.execute("ROLLBACK")
                return ReserveResult(False, None, "duplicate_symbol", len(union))

            used = len(union)
            if used >= self.max_positions:
                conn.execute("ROLLBACK")
                return ReserveResult(False, None, "cap_reached", used)

            now = _iso(_utcnow())
            try:
                cur = conn.execute(
                    "INSERT INTO position_reservations (symbol, state, created_at, updated_at) "
                    "VALUES (?, 'PENDING', ?, ?)",
                    (symbol, now, now),
                )
            except sqlite3.IntegrityError:
                # unique-index backstop: another writer took this symbol.
                conn.execute("ROLLBACK")
                return ReserveResult(False, None, "duplicate_symbol", len(union))
            conn.execute("COMMIT")
            return ReserveResult(True, int(cur.lastrowid), "reserved", used + 1)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def release(self, reservation_id: int, reason: str = "released") -> bool:
        """Free a slot — call ONLY on confirmed failure (executor not created).
        On UNKNOWN outcomes do NOT call this; let reconcile() resolve it."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM position_reservations WHERE id=?", (reservation_id,)
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        finally:
            conn.close()

    def confirm(self, reservation_id: int) -> None:
        """Mark a reservation CONFIRMED (position known created). Optional — the
        reservation is cleared by reconcile() once the exchange shows the
        position; this just records intent."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE position_reservations SET state='CONFIRMED', updated_at=? WHERE id=?",
                (_iso(_utcnow()), reservation_id),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def reconcile(self, exchange_open_symbols, ttl_seconds: float = 120.0) -> ReconcileReport:
        """Resolve reservations against real exchange positions.

        - symbol now on exchange -> delete (promoted to a real position).
        - past TTL and not on exchange -> delete (release stale / UNKNOWN->none).
        - fresh and not on exchange -> keep (still in-flight).
        Idempotent; safe to call every loop and on startup (restart recovery).
        """
        exch = set(exchange_open_symbols)
        now = _utcnow()
        rep = ReconcileReport()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, symbol, created_at FROM position_reservations"
            ).fetchall()
            for r in rows:
                rid, symbol, created_at = r["id"], r["symbol"], r["created_at"]
                if symbol in exch:
                    conn.execute("DELETE FROM position_reservations WHERE id=?", (rid,))
                    rep.promoted.append({"id": rid, "symbol": symbol})
                    continue
                try:
                    age = (now - datetime.fromisoformat(created_at)).total_seconds()
                except Exception:
                    age = ttl_seconds + 1  # unparsable -> treat as stale
                if age >= ttl_seconds:
                    conn.execute("DELETE FROM position_reservations WHERE id=?", (rid,))
                    rep.released.append({"id": rid, "symbol": symbol, "age": age})
                else:
                    rep.kept.append({"id": rid, "symbol": symbol, "age": age})
            conn.execute("COMMIT")
            return rep
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
