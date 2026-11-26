#!/usr/bin/env python3
"""kb_sqlite_ro.py — read-only SQLite inspector for evidence gathering.

Hard guarantees:
  * Opens the DB with URI flag mode=ro (immutable=0) so the file is never created/written.
  * Runs `PRAGMA query_only = ON` on the connection.
  * Rejects any statement whose first keyword is not SELECT, WITH or PRAGMA.
  * Never writes, exports or mutates anything. Output goes to stdout only.

Usage:
  kb_sqlite_ro.py <db_path> --tables
  kb_sqlite_ro.py <db_path> --schema <table>
  kb_sqlite_ro.py <db_path> --sql "SELECT ... "

Only SELECT/WITH/PRAGMA(read) statements are permitted.
"""
import re
import sys
import sqlite3
import argparse

ALLOWED_FIRST = ("SELECT", "WITH", "PRAGMA")
# PRAGMAs that could write/attach are blocked; we only allow introspection pragmas.
# Matched as whole words (\b) so column names like `update_time` are not false-positives.
BLOCKED_TOKENS = (
    "ATTACH", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "REPLACE", "VACUUM", "REINDEX", "COMMIT", "BEGIN", "ROLLBACK",
    "TRIGGER",
)


def _guard(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise SystemExit("ERR: bos sorgu")
    first = s.split(None, 1)[0].upper()
    if first not in ALLOWED_FIRST:
        raise SystemExit(f"ERR: yalnizca SELECT/WITH/PRAGMA izinli (gelen: {first})")
    up = s.upper()
    if ";" in s:
        raise SystemExit("ERR: tek sorgu, noktali virgul yasak")
    for bad in BLOCKED_TOKENS:
        if re.search(r"\b" + bad + r"\b", up):
            raise SystemExit(f"ERR: yasak token: {bad}")
    if first == "PRAGMA":
        # only read pragmas: table_info, index_list, database_list(read), etc.
        allowed_pragmas = ("TABLE_INFO", "INDEX_LIST", "TABLE_LIST",
                           "DATABASE_LIST", "QUERY_ONLY", "SCHEMA_VERSION")
        if not any(p in up for p in allowed_pragmas):
            raise SystemExit("ERR: yalnizca introspection PRAGMA izinli")
    return s


def _connect(db_path: str) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _run(conn, sql):
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    if cols:
        print("\t".join(cols))
        print("-" * 60)
    for r in rows:
        print("\t".join("" if v is None else str(v) for v in r))
    print(f"\n[{len(rows)} satir]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--tables", action="store_true")
    ap.add_argument("--schema", metavar="TABLE")
    ap.add_argument("--sql")
    a = ap.parse_args()

    conn = _connect(a.db)
    # sanity: verify read-only enforcement
    qo = conn.execute("PRAGMA query_only").fetchone()[0]
    sys.stderr.write(f"[info] query_only={qo} mode=ro db={a.db}\n")

    if a.tables:
        _run(conn, "SELECT name, type FROM sqlite_master "
                   "WHERE type IN ('table','view') ORDER BY name")
    elif a.schema:
        name = a.schema.replace("'", "")
        _run(conn, f"SELECT sql FROM sqlite_master WHERE name='{name}'")
        print()
        _run(conn, f"PRAGMA table_info('{name}')")
    elif a.sql:
        sql = _guard(a.sql)
        _run(conn, sql)
    else:
        ap.error("bir islem secin: --tables | --schema T | --sql '...'")
    conn.close()


if __name__ == "__main__":
    main()
