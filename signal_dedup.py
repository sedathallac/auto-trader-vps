"""signal_dedup.py — cross-process signal-replay prevention (BLOK25 Bölüm 2).

A strategy signal is identified by (symbol, bar_ts): for a 4h strategy the bar
timestamp uniquely identifies the decision point. The SAME signal must never be
executed twice — a restart, a duplicate worker or a replayed message would
otherwise open a second position for the same decision.

Same shared SQLite ledger + pattern as SlPlacementStore / CloseIntentStore /
PositionReserver (risk_ledger.db, WAL + BEGIN IMMEDIATE): only the FIRST caller
for a (symbol, bar_ts) owns the signal; every later caller sees REPLAY and must
skip. A signal is keyed FOREVER (a bar's decision does not expire) — a new bar
is a new key.
"""
import sqlite3
from datetime import datetime, timezone

FIRST = "FIRST"    # this caller owns the signal for (symbol, bar_ts)
REPLAY = "REPLAY"  # the signal was already consumed -> skip (fail-closed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SignalDedupStore:
    """Atomic, cross-process signal-replay guard backed by the shared ledger."""

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
                CREATE TABLE IF NOT EXISTS signal_dedup (
                    symbol TEXT NOT NULL,
                    bar_ts INTEGER NOT NULL,
                    signal_hash TEXT,
                    state TEXT NOT NULL DEFAULT 'CONSUMED',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, bar_ts)
                )
            """)
        finally:
            conn.close()

    def acquire(self, symbol: str, bar_ts: int, signal_hash: str = None) -> str:
        """Atomically consume (symbol, bar_ts). FIRST exactly once, ever;
        every later call (any process, any restart) gets REPLAY."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM signal_dedup WHERE symbol=? AND bar_ts=?",
                (symbol, bar_ts)).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                return REPLAY
            conn.execute(
                "INSERT INTO signal_dedup (symbol, bar_ts, signal_hash, state, "
                "created_at) VALUES (?, ?, ?, 'CONSUMED', ?)",
                (symbol, bar_ts, signal_hash, _now()))
            conn.execute("COMMIT")
            return FIRST
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get(self, symbol: str, bar_ts: int):
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT symbol, bar_ts, signal_hash, state, created_at "
                "FROM signal_dedup WHERE symbol=? AND bar_ts=?",
                (symbol, bar_ts)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()
