"""PRODUCTION-BOUND tests for risk_engine.RiskEngine (real code, not a model).

STATUS: WRITTEN on VPS, NOT RUN here. RUN ON ZORIN:
    ~/.local/share/bot-tooling/venv/bin/pytest -q BOT_KNOWLEDGE_BASE/TESTING/tests/test_risk_engine_real.py

Covers:
  * record_trade() idempotency (F: no double-count of the same executor PnL).
  * approve_trade() hard-caps notional to [min_notional, max_notional].
  * update_exposure() trips the circuit breaker above max_total_margin_pct.

The fixture builds a fresh SQLite ledger whose schema matches what risk_engine
reads/writes (risk_state, trade_ledger). If the real production DDL differs, the
first Zorin run will reveal it — that is the point of running against real code.
"""
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import pytest

risk_engine = pytest.importorskip("risk_engine", reason="run from the repo containing risk_engine.py")


CONFIG_KEYS = {
    "max_drawdown_pct": 0.20,
    "max_total_margin_pct": 0.40,
    "max_consecutive_losses": 3,
    "max_trades_per_day": 20,
    "risk_per_trade_pct": 0.015,
    "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008,
    "min_notional": 5.5,
    "max_notional": 25.0,
}


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "risk_ledger.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE risk_state (id INTEGER PRIMARY KEY, peak_equity REAL, "
        "consecutive_losses INTEGER, daily_trades INTEGER, daily_pnl REAL, "
        "last_reset_date TEXT, is_halted INTEGER, halt_reason TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY, trading_pair TEXT, "
        "net_pnl_quote REAL, closed_at TEXT, recorded_at TEXT, source TEXT)"
    )
    conn.execute(
        "INSERT INTO risk_state (id, peak_equity, consecutive_losses, daily_trades, "
        "daily_pnl, last_reset_date, is_halted, halt_reason, updated_at) "
        "VALUES (1, 0, 0, 0, 0, NULL, 0, NULL, NULL)"
    )
    conn.commit()
    conn.close()
    cfg = dict(CONFIG_KEYS, db_file=str(db))
    return risk_engine.RiskEngine(cfg)


def test_record_trade_is_idempotent(engine):
    ok1, _ = engine.record_trade("EXEC_1", "SOLUSDT", -0.2708, "2026-11-11T07:05:08Z")
    ok2, msg2 = engine.record_trade("EXEC_1", "SOLUSDT", -0.2708, "2026-11-11T07:05:08Z")
    assert ok1 is True
    assert ok2 is False
    assert "already" in msg2


def test_approve_trade_caps_notional(engine):
    notional, msg = engine.approve_trade(balance=2500.0, stop_distance_pct=0.01)
    assert msg == "approved"
    assert CONFIG_KEYS["min_notional"] <= notional <= CONFIG_KEYS["max_notional"]


def test_update_exposure_halts_over_limit(engine):
    # notional 10000 on balance 100 -> usage 100x >> 0.40 limit
    engine.update_exposure(balance=100.0, positions=[
        {"positionAmt": "10", "markPrice": "1000", "leverage": "1"}
    ])
    assert engine.is_halted is True


def test_approve_trade_blocked_when_halted(engine):
    engine.is_halted = True
    engine.halt_reason = "test"
    notional, msg = engine.approve_trade(balance=2500.0, stop_distance_pct=0.01)
    assert notional is None
    assert "halted" in msg
