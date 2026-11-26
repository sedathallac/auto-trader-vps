"""K-5 Görev A RED/GREEN — risk_ledger.db şema doğrulayıcı.

GREEN: kodun beklediği tüm tablolar/sütunlar varsa validate_schema boş döndürür
       (fazladan sütun da sorun değildir — süper-küme uyumlu).
RED:   eksik tablo veya eksik sütun tespit edilir ve assert_schema SchemaError
       yükseltir (startup fail-closed).
"""
import sqlite3

import pytest

from schema_validator import (
    EXPECTED_RISK_LEDGER,
    SchemaError,
    assert_schema,
    validate_schema,
)

# Üretimdeki gerçek DDL (test_c3.py fikstüründen; risk_engine/auto_trader'ın beklediği).
GOOD_DDL = """
CREATE TABLE risk_state (
    id INTEGER PRIMARY KEY CHECK (id=1), peak_equity REAL NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0, daily_trades INTEGER NOT NULL DEFAULT 0,
    daily_pnl REAL NOT NULL DEFAULT 0, last_reset_date TEXT NOT NULL,
    is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE trade_ledger (
    executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL NOT NULL DEFAULT 0,
    closed_at TEXT, recorded_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'sync');
CREATE TABLE sync_meta (
    id INTEGER PRIMARY KEY CHECK (id=1), last_close_ts REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL);
CREATE TABLE sl_placements (
    symbol TEXT PRIMARY KEY, token TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE position_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE close_intents (
    symbol TEXT PRIMARY KEY, token TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def _build(db_path, ddl):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


def test_green_full_schema_ok(tmp_path):
    """GREEN: tam ve doğru şema → sorun yok."""
    db = _build(tmp_path / "risk_ledger.db", GOOD_DDL)
    assert validate_schema(db) == []
    assert_schema(db)  # yükseltmemeli


def test_green_superset_columns_ok(tmp_path):
    """GREEN: fazladan sütun eklemek şemayı bozmaz (süper-küme uyumlu)."""
    ddl = GOOD_DDL.replace(
        "source TEXT NOT NULL DEFAULT 'sync');",
        "source TEXT NOT NULL DEFAULT 'sync', extra_future_col TEXT);",
    )
    db = _build(tmp_path / "risk_ledger.db", ddl)
    assert validate_schema(db) == []


def test_red_missing_table_detected(tmp_path):
    """RED: trade_ledger tablosu hiç yoksa tespit edilir."""
    ddl = "\n".join(
        s + ";" for s in GOOD_DDL.split(";")
        if "CREATE TABLE trade_ledger" not in s and s.strip()
    )
    db = _build(tmp_path / "risk_ledger.db", ddl)
    problems = validate_schema(db)
    assert any("trade_ledger" in p and "YOK" in p for p in problems), problems
    with pytest.raises(SchemaError):
        assert_schema(db)


def test_red_missing_column_detected(tmp_path):
    """RED: trade_ledger.source sütunu eksikse tespit edilir (kod bu sütuna yazar)."""
    ddl = GOOD_DDL.replace(
        "recorded_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'sync');",
        "recorded_at TEXT NOT NULL);",
    )
    db = _build(tmp_path / "risk_ledger.db", ddl)
    problems = validate_schema(db)
    assert any("trade_ledger" in p and "source" in p for p in problems), problems
    with pytest.raises(SchemaError):
        assert_schema(db)


def test_red_missing_risk_state_column_detected(tmp_path):
    """RED: risk_state.halt_reason eksikse tespit edilir (risk_engine okur/yazar)."""
    ddl = GOOD_DDL.replace(
        "is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT, updated_at TEXT NOT NULL);",
        "is_halted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);",
    )
    db = _build(tmp_path / "risk_ledger.db", ddl)
    problems = validate_schema(db)
    assert any("risk_state" in p and "halt_reason" in p for p in problems), problems


def test_red_empty_db_reports_all_tables(tmp_path):
    """RED: boş DB → beklenen her tablo eksik olarak raporlanır."""
    db = _build(tmp_path / "risk_ledger.db", "")
    problems = validate_schema(db)
    assert len(problems) == len(EXPECTED_RISK_LEDGER)
    assert all("YOK" in p for p in problems), problems
