#!/usr/bin/env python3
"""kb_pg_ro.py — read-only PostgreSQL inspector for evidence gathering.

Safety:
  * Connection forced READ ONLY (set_session(readonly=True)) + autocommit.
  * Only SELECT / WITH / explain-free introspection permitted; single statement.
  * Never prints the password or the raw DSN. Credentials are read from the
    non-secret docker-compose.yml DATABASE_URL and kept in memory only.
  * No writes, no COPY, no exports.

Usage:
  kb_pg_ro.py --probe
  kb_pg_ro.py --tables
  kb_pg_ro.py --schema <table>
  kb_pg_ro.py --sql "SELECT ..."
"""
import re
import sys
import argparse

COMPOSE = "/root/hummingbot-api/docker-compose.yml"
ALLOWED_FIRST = ("SELECT", "WITH")
BLOCKED = ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
           "REPLACE", "GRANT", "REVOKE", "COPY", "CALL", "DO", "COMMIT",
           "BEGIN", "VACUUM", "MERGE", "REFRESH", "SET", "RESET")


def _dsn():
    """Parse DATABASE_URL from docker-compose.yml. Returns a psycopg2 DSN dict.
    Never returned/printed as a string."""
    txt = open(COMPOSE).read()
    m = re.search(r"DATABASE_URL=postgresql\+asyncpg://([^:]+):([^@]+)@([^:/]+):(\d+)/(\S+)", txt)
    if not m:
        raise SystemExit("ERR: DATABASE_URL compose icinde bulunamadi")
    user, pw, host, port, db = m.groups()
    # compose host is the docker service name 'postgres'; from host use loopback
    host = "127.0.0.1"
    return dict(user=user, password=pw, host=host, port=int(port), dbname=db)


def _guard(sql):
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise SystemExit("ERR: tek sorgu, noktali virgul yasak")
    first = s.split(None, 1)[0].upper() if s else ""
    if first not in ALLOWED_FIRST:
        raise SystemExit(f"ERR: yalnizca SELECT/WITH izinli (gelen: {first})")
    up = s.upper()
    for bad in BLOCKED:
        if re.search(r"\b" + bad + r"\b", up):
            raise SystemExit(f"ERR: yasak token: {bad}")
    return s


def _drivers():
    avail = []
    for mod in ("psycopg2", "psycopg", "pg8000", "asyncpg"):
        try:
            __import__(mod)
            avail.append(mod)
        except Exception:
            pass
    return avail


def _connect():
    c = _dsn()
    avail = _drivers()
    sys.stderr.write(f"[info] driverlar: {avail or 'YOK'}\n")
    if "psycopg2" in avail:
        import psycopg2
        conn = psycopg2.connect(connect_timeout=8, **c)
        conn.set_session(readonly=True, autocommit=True)
        return conn
    if "psycopg" in avail:
        import psycopg
        conn = psycopg.connect(
            host=c["host"], port=c["port"], user=c["user"],
            password=c["password"], dbname=c["dbname"],
            connect_timeout=8, autocommit=True)
        conn.read_only = True
        return conn
    if "pg8000" in avail:
        import pg8000.dbapi
        conn = pg8000.dbapi.connect(
            host=c["host"], port=c["port"], user=c["user"],
            password=c["password"], database=c["dbname"])
        cur = conn.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.autocommit = True
        return conn
    raise SystemExit(f"ERR: PostgreSQL driver yok (bakilanlar: psycopg2/psycopg/pg8000/asyncpg)")


def _run(conn, sql):
    cur = conn.execute(sql) if hasattr(conn, "execute") else conn.cursor()
    if not hasattr(conn, "execute"):
        cur.execute(sql)
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
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--tables", action="store_true")
    ap.add_argument("--schema")
    ap.add_argument("--sql")
    a = ap.parse_args()

    conn = _connect()
    ro = conn.cursor()
    ro.execute("SHOW transaction_read_only")
    sys.stderr.write(f"[info] transaction_read_only={ro.fetchone()[0]}\n")

    if a.probe:
        _run(conn, "SELECT current_database() AS db, current_user AS usr, version() AS ver")
    elif a.tables:
        _run(conn, "SELECT table_schema, table_name FROM information_schema.tables "
                   "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                   "ORDER BY table_schema, table_name")
    elif a.schema:
        name = a.schema.replace("'", "")
        _run(conn, "SELECT column_name, data_type FROM information_schema.columns "
                   f"WHERE table_name='{name}' ORDER BY ordinal_position")
    elif a.sql:
        _run(conn, _guard(a.sql))
    else:
        ap.error("--probe | --tables | --schema T | --sql '...'")
    conn.close()


if __name__ == "__main__":
    main()
