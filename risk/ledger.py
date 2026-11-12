"""Cross-process idempotency ledger for stop-loss (SL) placement intents.

The problem K-4 solves
----------------------
Two independent actors can try to place an SL for the same position at the
same moment:

* ``open_coin()``          -- places an SL immediately after opening a position.
* ``ensure_sl_coverage()`` -- the "orphan killer" that periodically scans for
                              positions missing an SL and places one.

If both run concurrently they can each observe "no SL yet" and each place one,
producing a DUPLICATE stop-loss.

The deterministic dedupe (keep the largest ``algo_id``, cancel the rest) is a
*recovery* mechanism and runs with up to ~60s latency. K-4 PREVENTS the
duplicate at the source with an intent lock:

    ``claim(symbol, epoch)`` is atomic (``BEGIN IMMEDIATE``). Exactly one caller
    wins the claim and is allowed to place the SL; every other caller busy-skips.

Fail-closed: if the DB is locked beyond ``busy_timeout`` the claim returns
``False`` (skip, do not place) instead of racing. Not placing is always safer
than placing twice, because the orphan killer will cover a genuinely missing SL
on its next pass.
"""

from __future__ import annotations

import sqlite3
import time

CLAIM_WON = True
CLAIM_SKIP = False


class RiskLedger:
    """SQLite-backed intent ledger. One row per (symbol, epoch) coverage window."""

    def __init__(self, db_path: str = "risk_ledger.db", busy_timeout_ms: int = 5000):
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self._init_db()

    # -- connection -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None -> autocommit; we drive transactions explicitly
        # with BEGIN IMMEDIATE so the write lock is taken up front.
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sl_intent (
                    symbol     TEXT    NOT NULL,
                    epoch      INTEGER NOT NULL,
                    algo_id    INTEGER NOT NULL,
                    owner      TEXT    NOT NULL,
                    created_at REAL    NOT NULL,
                    PRIMARY KEY (symbol, epoch)
                )
                """
            )
        finally:
            conn.close()

    # -- core API ---------------------------------------------------------
    def claim(self, symbol: str, epoch: int, algo_id: int, owner: str) -> bool:
        """Try to claim the exclusive right to place the SL for (symbol, epoch).

        Returns ``True``  -> caller WON the intent and MUST place the SL.
        Returns ``False`` -> another actor already holds the intent (busy-skip)
                             or the DB was locked (fail-closed). Caller MUST NOT
                             place the SL.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the write lock up front
            row = conn.execute(
                "SELECT algo_id, owner FROM sl_intent WHERE symbol=? AND epoch=?",
                (symbol, epoch),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sl_intent (symbol, epoch, algo_id, owner, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (symbol, epoch, algo_id, owner, time.time()),
                )
                conn.execute("COMMIT")
                return CLAIM_WON
            # Someone already owns this coverage window -> busy-skip.
            conn.execute("COMMIT")
            return CLAIM_SKIP
        except sqlite3.OperationalError:
            # "database is locked" past busy_timeout -> fail-closed, do not place.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return CLAIM_SKIP
        finally:
            conn.close()

    def current_owner(self, symbol: str, epoch: int):
        """Return ``(algo_id, owner)`` for the current intent holder, or ``None``."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT algo_id, owner FROM sl_intent WHERE symbol=? AND epoch=?",
                (symbol, epoch),
            ).fetchone()
        finally:
            conn.close()

    def release(self, symbol: str, epoch: int) -> None:
        """Drop the intent for a coverage window (e.g. after the position closes)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM sl_intent WHERE symbol=? AND epoch=?", (symbol, epoch)
            )
            conn.execute("COMMIT")
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
