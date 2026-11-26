"""sl_placement.py — cross-process SL-placement idempotency (K-3).

Both run_once (900s) and orphan_killer (60s) call ensure_sl_coverage, and open_coin
also places an SL. Without coordination, two PROCESSES can each see a just-opened
position as unprotected (the freshly placed SL not yet visible / a transient ABSENT)
and BOTH call place_algo_sl -> a DUPLICATE SL on one position (root cause of the
observed BTC/SOL double-SL race, orphan_killer.py:84-85 + run_once).

This makes SL placement idempotent across processes using the SAME shared SQLite
ledger as PositionReserver / CloseIntentStore (risk_ledger.db, WAL + BEGIN IMMEDIATE):
only the intent OWNER places an SL for a given (symbol, position-epoch); any other
process/path sees it owned and SKIPS. The epoch `token` ('<side>:<entryPrice>') comes
from exchange truth so a NEW position period supersedes a prior PLACED intent.

States: PENDING (owner acquired, about to place) -> PLACED (SL confirmed placed). On a
placement FAILURE the owner release()s so reality is reflected (the existing -2021 /
OPEN_UNPROTECTED alarm then governs; no silent block).
"""
import sqlite3
from datetime import datetime, timezone
from typing import Optional

ACQUIRED = "ACQUIRED"   # this caller owns the placement; it may place exactly one SL
BUSY = "BUSY"           # another process owns an in-flight / completed placement -> skip

_ACTIVE = ("PENDING", "PLACED")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlPlacementStore:
    """Atomic, cross-process SL-placement intent backed by the shared SQLite ledger."""

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
                CREATE TABLE IF NOT EXISTS sl_placements (
                    symbol TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
        finally:
            conn.close()

    def acquire(self, symbol: str, token: str) -> str:
        """Atomically become the SL placer for (symbol, token), or report BUSY.
        One BEGIN IMMEDIATE tx so concurrent processes serialise: at most one ACQUIRED
        per (symbol, token) epoch."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT token, state FROM sl_placements WHERE symbol=?", (symbol,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sl_placements (symbol, token, state, created_at, updated_at) "
                    "VALUES (?, ?, 'PENDING', ?, ?)", (symbol, token, _now(), _now()))
                conn.execute("COMMIT")
                return ACQUIRED
            if row["token"] == token and row["state"] in _ACTIVE:
                conn.execute("ROLLBACK")
                return BUSY  # another process is placing, or already placed, this epoch
            # different token (a new position period) -> supersede the old intent
            conn.execute(
                "UPDATE sl_placements SET token=?, state='PENDING', created_at=?, "
                "updated_at=? WHERE symbol=?", (token, _now(), _now(), symbol))
            conn.execute("COMMIT")
            return ACQUIRED
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def mark_placed(self, symbol: str, token: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE sl_placements SET state='PLACED', updated_at=? WHERE symbol=? AND token=?",
                (_now(), symbol, token))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def release(self, symbol: str, token: str) -> None:
        """Free the intent after a placement FAILURE so a later attempt can re-acquire
        (the -2021 / OPEN_UNPROTECTED alarm governs the real exchange state)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sl_placements WHERE symbol=? AND token=?", (symbol, token))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def get(self, symbol: str) -> Optional[dict]:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT symbol, token, state FROM sl_placements WHERE symbol=?", (symbol,)
            ).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()
