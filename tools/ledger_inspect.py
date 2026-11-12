#!/usr/bin/env python3
"""Read-only inspector for risk_ledger.db — list current SL intents.

Opens the DB in read-only mode (SQLite URI immutable=1) so it can never write,
lock-contend, or interfere with a running bot.

Usage:
    python3 tools/ledger_inspect.py [path/to/risk_ledger.db]
"""

import sqlite3
import sys
import time


def main(db_path: str = "risk_ledger.db") -> int:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as e:
        print(f"cannot open {db_path} read-only: {e}", file=sys.stderr)
        return 1
    try:
        rows = conn.execute(
            "SELECT symbol, epoch, algo_id, owner, created_at "
            "FROM sl_intent ORDER BY created_at DESC"
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"no sl_intent table in {db_path}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if not rows:
        print("(no SL intents recorded)")
        return 0

    now = time.time()
    print(f"{'symbol':<14} {'epoch':>6} {'algo_id':>9} {'owner':<20} {'age(s)':>8}")
    print("-" * 62)
    for symbol, epoch, algo_id, owner, created_at in rows:
        print(f"{symbol:<14} {epoch:>6} {algo_id:>9} {owner:<20} {now - created_at:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "risk_ledger.db"))
