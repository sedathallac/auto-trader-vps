"""schema_validator.py — K-5 Görev A: SQLite şema doğrulama (risk_ledger.db).

risk_state / sync_meta / trade_ledger tabloları kod tarafından OKUNUP YAZILIR ama
kodun kendisi tarafından OLUŞTURULMAZ (harici kurulum / elle migrate). Şema koddan
sapmışsa (eksik tablo veya sütun), hata ancak çalışma anında ortaya çıkar ve
risk_engine fail-closed'a düşer (bkz. risk_engine.py:71) — yani bot sessizce halt
olur. Bu modül, kodun beklediği şemayı programatik olarak doğrular; bir başlangıç
(startup) kontrolü veya test olarak kullanılabilir.

Kuralı: beklenen her tablo var olmalı ve beklenen sütunların TAMAMINI içermeli.
Fazladan sütun / index / farklı tip SERBEST (süper-küme uyumlu) — amaç, kodun
dayandığı sütunların varlığını garanti etmek, DDL'i bit-bit sabitlemek değil.

sl_placements / position_reservations / close_intents tabloları ilgili store'ların
init_schema()'sı tarafından KENDİ-oluşturulur, bu yüzden DDL'leri her zaman kendileriyle
tutarlıdır; yine de bütünlük kontrolü için beklenen şemaya dahil edilmiştir.
"""
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set

# Kodun risk_ledger.db üzerinde fiilen dayandığı sütunlar (okuma/yazma yollarından
# türetilmiştir — referanslar aşağıda):
EXPECTED_RISK_LEDGER: Dict[str, Set[str]] = {
    # risk_engine.py:49 (SELECT) + :83-86 (INSERT/UPSERT)
    "risk_state": {
        "id", "peak_equity", "consecutive_losses", "daily_trades", "daily_pnl",
        "last_reset_date", "is_halted", "halt_reason", "updated_at",
    },
    # risk_engine.py:113-115 (INSERT) + auto_trader.py:422 (SELECT executor_id)
    "trade_ledger": {
        "executor_id", "trading_pair", "net_pnl_quote", "closed_at",
        "recorded_at", "source",
    },
    # auto_trader.py:420 (SELECT last_close_ts) + :480 (UPDATE last_close_ts, updated_at)
    "sync_meta": {"id", "last_close_ts", "updated_at"},
    # sl_placement.py:51 (K-3, kendi-oluşturur)
    "sl_placements": {"symbol", "token", "state", "created_at", "updated_at"},
    # position_reservation.py:87 (kendi-oluşturur)
    "position_reservations": {"id", "symbol", "state", "created_at", "updated_at"},
    # close_intent.py:78 (kendi-oluşturur)
    "close_intents": {"symbol", "token", "state", "created_at", "updated_at"},
}

# risk_ledger.db varsayılan konumu (risk_engine.DEFAULT_DB_FILE / SL_LEDGER_DB ile aynı).
DEFAULT_DB_FILE = Path.home() / "risk_ledger.db"


class SchemaError(RuntimeError):
    """Beklenen şema ile gerçek DB şeması uyuşmadığında yükseltilir."""


def table_columns(conn: sqlite3.Connection, table: str) -> Optional[Set[str]]:
    """`table` mevcutsa sütun adları kümesini, yoksa None döndürür."""
    # table_info ismi parametre bağlanamadığı için önce tablonun varlığını güvenle sorgula.
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return None
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {r[1] for r in rows}  # r[1] = column name


def validate_schema(
    db_file, expected: Dict[str, Set[str]] = EXPECTED_RISK_LEDGER
) -> List[str]:
    """`db_file` şemasını `expected` ile karşılaştırır.

    Sorun bulunmazsa boş liste döndürür. Her sorun, insan-okunur tek satırdır:
    eksik tablo veya eksik sütun(lar). Fazladan sütun bir sorun DEĞİLDİR.
    DB dosyası açılamıyorsa tek öğeli bir sorun listesi döndürür (fail-closed)."""
    problems: List[str] = []
    try:
        conn = sqlite3.connect(str(db_file), timeout=10)
    except Exception as e:  # pragma: no cover - açılış hatası nadir
        return [f"DB açılamadı ({db_file}): {e}"]
    try:
        for table, required in expected.items():
            cols = table_columns(conn, table)
            if cols is None:
                problems.append(f"tablo YOK: {table}")
                continue
            missing = required - cols
            if missing:
                problems.append(
                    f"tablo {table}: eksik sütun(lar): {', '.join(sorted(missing))}"
                )
    finally:
        conn.close()
    return problems


def assert_schema(
    db_file=DEFAULT_DB_FILE, expected: Dict[str, Set[str]] = EXPECTED_RISK_LEDGER
) -> None:
    """Startup fail-closed kontrolü: şema uyuşmazlığında SchemaError yükseltir."""
    problems = validate_schema(db_file, expected)
    if problems:
        raise SchemaError(
            "risk_ledger.db şeması kodun beklediğiyle uyumsuz:\n  - "
            + "\n  - ".join(problems)
        )


def main() -> int:
    """CLI: `python3 schema_validator.py [db_path]` — OK ise 0, uyumsuzsa 1 döndürür."""
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_FILE
    problems = validate_schema(db)
    if problems:
        print(f"❌ ŞEMA UYUMSUZ ({db}):")
        for p in problems:
            print(f"   - {p}")
        return 1
    print(f"✅ Şema uyumlu ({db}): {len(EXPECTED_RISK_LEDGER)} tablo doğrulandı")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
