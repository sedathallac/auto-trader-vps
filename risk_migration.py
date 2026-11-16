"""risk_migration.py — FAZ16 additive migration (YALNIZ operatör komutu).

Bu modül UYGULAMA BAŞLANGICINDA OTOMATİK ÇALIŞMAZ. `auto_trader`/`risk_engine`
import edildiğinde hiçbir DDL çalışmaz. Gerçek DB üzerinde çalıştırmak ayrı onay
gerektirir; varsayılan `--apply` YOKTUR (dry-run).

Güvenlik sırası (şartname):
  1. SQLite Backup API ile timestamp'li yedek.
  2. Yedekte `PRAGMA quick_check` == "ok".
  3. Yedek başarısızsa migration DURUR.
  4. Migration tek transaction içinde.
  5. Hata → rollback.
  6. risk_state / ledger kayıtları / is_halted / halt_reason KORUNUR (yalnız ADD COLUMN).
  7. Tekrar çalıştırma veriyi bozmaz (idempotent).

Kullanım:
    python3 risk_migration.py <db>            # dry-run: ne yapılacağını yazar
    python3 risk_migration.py <db> --apply    # uygula (operatör onayı)
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

RISK_STATE_COLUMNS = {
    "day_start_equity": "REAL DEFAULT 0",
    "day_start_equity_date": "TEXT",
    "day_start_equity_at": "TEXT",
    "open_keys": "TEXT",
}
TRADE_LEDGER_COLUMNS = {
    "closed_at_epoch": "REAL",
}
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_trade_ledger_closed_epoch "
    "ON trade_ledger(closed_at_epoch)",
    "CREATE INDEX IF NOT EXISTS idx_trade_ledger_pair "
    "ON trade_ledger(trading_pair)",
)


class MigrationError(RuntimeError):
    pass


def _cols(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return None
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def plan(db_file):
    """Eksik kolon/index listesi. DB yoksa veya tablo yoksa MigrationError."""
    p = Path(db_file)
    if not p.exists():
        raise MigrationError(f"DB yok: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    try:
        steps = []
        for table, cols in (("risk_state", RISK_STATE_COLUMNS),
                            ("trade_ledger", TRADE_LEDGER_COLUMNS)):
            have = _cols(conn, table)
            if have is None:
                raise MigrationError(f"tablo YOK: {table} — migration değil bootstrap gerekir")
            for name, ddl in cols.items():
                if name not in have:
                    steps.append(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        steps.extend(INDEXES)
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        steps = [s for s in steps
                 if not (s.startswith("CREATE INDEX") and
                         s.split("EXISTS ")[1].split(" ")[0] in existing)]
        return steps
    finally:
        conn.close()


def backup(db_file):
    """Backup API ile timestamp'li yedek + quick_check. Dönüş: yedek yolu."""
    src_path = Path(db_file)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst_path = src_path.with_name(f"{src_path.name}.bak-faz16-{ts}")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
        dst.commit()
    except Exception as e:
        dst.close()
        src.close()
        raise MigrationError(f"yedek alınamadı: {e}") from e
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
    chk = sqlite3.connect(str(dst_path), timeout=30)
    try:
        res = chk.execute("PRAGMA quick_check").fetchone()
    finally:
        chk.close()
    if not res or str(res[0]).lower() != "ok":
        raise MigrationError(f"yedek quick_check başarısız: {res}")
    return dst_path


def apply(db_file, do_backup=True):
    """Additive migration. Dönüş: (yedek yolu|None, uygulanan adımlar)."""
    steps = plan(db_file)
    if not steps:
        return None, []
    bak = backup(db_file) if do_backup else None
    conn = sqlite3.connect(f"file:{db_file}?mode=rw", uri=True, timeout=30,
                           isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in steps:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise MigrationError(f"migration rollback: {e}") from e
    finally:
        conn.close()
    return bak, steps


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    db = argv[1]
    do_apply = "--apply" in argv[2:]
    try:
        steps = plan(db)
    except MigrationError as e:
        print(f"❌ {e}")
        return 1
    if not steps:
        print(f"✅ Şema güncel, migration gerekmiyor ({db})")
        return 0
    print(f"Plan ({len(steps)} adım) — {db}")
    for s in steps:
        print(f"  - {s}")
    if not do_apply:
        print("DRY-RUN: uygulamak için --apply (operatör onayı gerekir)")
        return 0
    bak, applied = apply(db, do_backup=True)
    print(f"✅ Uygulandı ({len(applied)} adım), yedek: {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
