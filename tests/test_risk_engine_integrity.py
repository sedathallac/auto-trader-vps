"""FAZ16 — RiskEngine bütünleşik bütünlük testleri (Commit 1 + Commit 2).

Her test gerçek SQLite dosyası kullanır; hiçbiri üretim DB'sine veya Binance'e
dokunmaz. Kapsam: DB yolu/oluşturma, şema+singleton, atomik/idempotent ledger,
rowcount, daily_pnl türetimi, UTC gün sınırı, baseline kalıcılığı, reset/HALTED,
watermark, migration, sizing cap ve nihai notional.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import risk_engine
import risk_migration
import schema_validator

FULL_DDL = """
CREATE TABLE risk_state (
    id INTEGER PRIMARY KEY CHECK (id=1), peak_equity REAL NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0, daily_trades INTEGER NOT NULL DEFAULT 0,
    daily_pnl REAL NOT NULL DEFAULT 0, last_reset_date TEXT NOT NULL,
    is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT, updated_at TEXT NOT NULL,
    day_start_equity REAL DEFAULT 0, day_start_equity_date TEXT,
    day_start_equity_at TEXT, open_keys TEXT);
CREATE TABLE trade_ledger (
    executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL NOT NULL DEFAULT 0,
    closed_at TEXT, recorded_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'sync',
    closed_at_epoch REAL);
CREATE TABLE sync_meta (
    id INTEGER PRIMARY KEY CHECK (id=1), last_close_ts REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL);
INSERT INTO risk_state (id, last_reset_date, updated_at) VALUES (1, '2020-01-01', 'x');
INSERT INTO sync_meta (id, last_close_ts, updated_at) VALUES (1, 0, 'x');
"""

OLD_DDL = """
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
INSERT INTO risk_state (id, last_reset_date, updated_at) VALUES (1, '2020-01-01', 'x');
INSERT INTO sync_meta (id, last_close_ts, updated_at) VALUES (1, 0, 'x');
"""

CFG = {
    "max_trades_per_day": 20, "max_consecutive_losses": 3,
    "risk_per_trade_pct": 0.015, "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008, "slippage_pct_round_trip": 0.0004,
    "min_net_reward_risk": 1.20, "min_notional_policy": "refuse",
    "min_notional": 5.5, "max_notional": 25.0, "max_drawdown_pct": 0.2,
    "max_total_margin_pct": 0.4, "max_daily_loss_pct": 0.05,
}


def build(tmp, ddl=FULL_DDL, name="risk_ledger.db"):
    db = Path(tmp) / name
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    return str(db)


def engine(db, **over):
    cfg = dict(CFG, db_file=db)
    cfg.update(over)
    return risk_engine.RiskEngine(cfg)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = build(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNoSilentCreate(unittest.TestCase):
    def test_missing_db_is_not_created(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "nope.db")
        r = engine(db)
        self.assertFalse(r.health_ok)
        self.assertIn("yok", r.health_reason)
        self.assertFalse(os.path.exists(db), "sessiz DB oluşturma yasak")
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_db_blocks_entry(self):
        tmp = tempfile.mkdtemp()
        r = engine(os.path.join(tmp, "nope.db"))
        ok, why = auto_trader._risk_entry_gate(r)
        self.assertFalse(ok)
        self.assertIn("risk_store", why)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


class TestSchemaValidation(Base):
    def test_full_schema_is_healthy(self):
        r = engine(self.db)
        self.assertTrue(r.health_ok, r.health_reason)
        self.assertFalse(r.migration_required)

    def test_missing_risk_state_table_blocks(self):
        db = build(self.tmp, "CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY);",
                   name="b.db")
        r = engine(db)
        self.assertFalse(r.health_ok)
        self.assertIn("risk_state", r.health_reason)

    def test_missing_required_column_blocks(self):
        ddl = FULL_DDL.replace("is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT,", "")
        db = build(self.tmp, ddl, name="c.db")
        r = engine(db)
        self.assertFalse(r.health_ok)
        self.assertIn("halt_reason", r.health_reason)

    def test_old_schema_requires_migration_and_blocks_entry(self):
        db = build(self.tmp, OLD_DDL, name="old.db")
        r = engine(db)
        self.assertTrue(r.health_ok, r.health_reason)
        self.assertTrue(r.migration_required)
        ok, why = auto_trader._risk_entry_gate(r)
        self.assertFalse(ok)
        self.assertIn("migration_required", why)


class TestSingletons(Base):
    def test_missing_risk_state_row_is_reported(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM risk_state")
        conn.commit()
        conn.close()
        self.assertTrue(any("risk_state id=1" in p
                            for p in schema_validator.validate_singletons(self.db)))

    def test_missing_sync_meta_row_is_bootstrapped(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM sync_meta")
        conn.commit()
        conn.close()
        r = engine(self.db)
        self.assertTrue(r.health_ok, r.health_reason)
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT last_close_ts FROM sync_meta WHERE id=1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(float(row[0]), 0.0)

    def test_duplicate_ledger_key_is_reported(self):
        ddl = FULL_DDL.replace("executor_id TEXT PRIMARY KEY", "executor_id TEXT")
        db = build(self.tmp, ddl, name="dup.db")
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trade_ledger (executor_id, recorded_at) VALUES ('E1','x')")
        conn.execute("INSERT INTO trade_ledger (executor_id, recorded_at) VALUES ('E1','x')")
        conn.commit()
        conn.close()
        self.assertTrue(any("duplicate" in p
                            for p in schema_validator.validate_singletons(db)))


class TestRecordTrade(Base):
    def test_insert_then_duplicate_is_rejected(self):
        r = engine(self.db)
        ok, _ = r.record_trade("E1", "SOL-USDT", -1.5, closed_at_epoch=1000.0)
        self.assertTrue(ok)
        ok2, msg = r.record_trade("E1", "SOL-USDT", -1.5, closed_at_epoch=1000.0)
        self.assertFalse(ok2)
        self.assertEqual(msg, "already-recorded")
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_daily_pnl_derived_from_ledger_not_incremented(self):
        r = engine(self.db)
        r.record_trade("E1", "SOL-USDT", -2.0, closed_at_epoch=risk_engine.utc_now().timestamp())
        r.record_trade("E2", "SOL-USDT", -2.0, closed_at_epoch=risk_engine.utc_now().timestamp())
        self.assertAlmostEqual(r.daily_pnl, -4.0, places=6)
        r2 = engine(self.db)
        self.assertAlmostEqual(r2.daily_pnl, -4.0, places=6,
                               msg="restart PnL'yi ikiye katlamamalı")

    def test_consecutive_losses_chronological(self):
        r = engine(self.db)
        now = risk_engine.utc_now().timestamp()
        r.record_trade("E1", "SOL-USDT", -1.0, closed_at_epoch=now - 300)
        r.record_trade("E2", "SOL-USDT", -1.0, closed_at_epoch=now - 200)
        r.record_trade("E3", "SOL-USDT", 2.0, closed_at_epoch=now - 100)
        self.assertEqual(r.consecutive_losses, 0)
        r.record_trade("E4", "SOL-USDT", -1.0, closed_at_epoch=now)
        self.assertEqual(r.consecutive_losses, 1)

    def test_utc_day_boundary_excludes_previous_day(self):
        r = engine(self.db)
        now = risk_engine.utc_now().timestamp()
        r.record_trade("E1", "SOL-USDT", -9.0, closed_at_epoch=now - 86400 * 2)
        r.record_trade("E2", "SOL-USDT", -1.0, closed_at_epoch=now)
        self.assertAlmostEqual(r.daily_pnl, -1.0, places=6)

    def test_unresolved_timestamp_blocks_entry(self):
        r = engine(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO trade_ledger (executor_id, trading_pair, net_pnl_quote,"
                     " closed_at, recorded_at, source) VALUES ('E9','SOL-USDT',-1.0,"
                     " '2026-01-01T00:00:00','x','sync')")
        conn.commit()
        conn.close()
        r.record_trade("E1", "SOL-USDT", -1.0, closed_at_epoch=risk_engine.utc_now().timestamp())
        self.assertFalse(r.health_ok)
        self.assertIn("çözümlenemedi", r.health_reason)

    def test_rowcount_mismatch_rolls_back(self):
        r = engine(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM risk_state")
        conn.commit()
        conn.close()
        ok, msg = r.record_trade("E1", "SOL-USDT", -1.0,
                                 closed_at_epoch=risk_engine.utc_now().timestamp())
        self.assertFalse(ok)
        self.assertIn("changes!=1", msg)
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0, "rollback: ledger satırı kalmamalı")


class TestBaseline(Base):
    def test_baseline_persists_across_restart(self):
        r = engine(self.db)
        r.update_equity(1000.0)
        self.assertEqual(r.day_start_equity, 1000.0)
        r2 = engine(self.db)
        self.assertEqual(r2.day_start_equity, 1000.0)
        self.assertEqual(r2.day_start_equity_date, risk_engine.utc_day())
        r2.update_equity(900.0)
        self.assertEqual(r2.day_start_equity, 1000.0, "gün içi restart baseline'ı değiştirmemeli")

    def test_reset_keeps_halted_and_streak(self):
        r = engine(self.db)
        r.update_equity(1000.0)
        r.is_halted = True
        r.halt_reason = "manual"
        r.consecutive_losses = 2
        r._save_state()
        r.reset_daily()
        self.assertTrue(r.is_halted, "reset HALTED'ı kaldırmamalı")
        self.assertEqual(r.halt_reason, "manual")
        self.assertEqual(r.consecutive_losses, 2)
        self.assertEqual(r.daily_trades, 0)
        self.assertEqual(r.day_start_equity, 0.0)

    def test_baseline_missing_after_reset_blocks(self):
        r = engine(self.db)
        r.last_reset_date = risk_engine.utc_day()
        r.day_start_equity_date = "1999-01-01"
        r.day_start_equity = 0.0
        r._save_state()
        ok = r.update_equity(1234.0)
        self.assertFalse(ok)
        self.assertFalse(r.health_ok)
        self.assertIn("daily_baseline_missing", r.health_reason)


class TestRegisterOpen(Base):
    def test_register_open_is_idempotent_by_key(self):
        r = engine(self.db)
        self.assertTrue(r.register_open(key="E1"))
        self.assertTrue(r.register_open(key="E1"))
        self.assertEqual(r.daily_trades, 1)

    def test_register_open_without_key_counts_each_call(self):
        r = engine(self.db)
        r.register_open()
        r.register_open()
        self.assertEqual(r.daily_trades, 2)


class TestMigration(Base):
    def test_plan_lists_missing_columns(self):
        db = build(self.tmp, OLD_DDL, name="old.db")
        steps = risk_migration.plan(db)
        self.assertTrue(any("day_start_equity" in s for s in steps))
        self.assertTrue(any("closed_at_epoch" in s for s in steps))

    def test_apply_is_idempotent_and_preserves_state(self):
        db = build(self.tmp, OLD_DDL, name="old.db")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE risk_state SET is_halted=1, halt_reason='keep me', "
                     "daily_pnl=-3.5 WHERE id=1")
        conn.execute("INSERT INTO trade_ledger (executor_id, recorded_at) VALUES ('E1','x')")
        conn.commit()
        conn.close()
        bak, steps = risk_migration.apply(db, do_backup=True)
        self.assertTrue(steps)
        self.assertTrue(bak and os.path.exists(bak), "yedek alınmalı")
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT is_halted, halt_reason, daily_pnl FROM risk_state "
                           "WHERE id=1").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0]
        conn.close()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "keep me")
        self.assertAlmostEqual(row[2], -3.5)
        self.assertEqual(n, 1)
        self.assertEqual(risk_migration.plan(db), [], "ikinci plan boş olmalı")
        bak2, steps2 = risk_migration.apply(db, do_backup=False)
        self.assertEqual(steps2, [])
        self.assertIsNone(bak2)

    def test_backup_quick_check_ok(self):
        db = build(self.tmp, OLD_DDL, name="old.db")
        bak = risk_migration.backup(db)
        conn = sqlite3.connect(bak)
        self.assertEqual(str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower(), "ok")
        conn.close()


class TestSizingCaps(Base):
    def test_approve_trade_applies_pct_cap(self):
        r = engine(self.db)
        n, status = r.approve_trade(10000.0, 0.01, equity=100.0, max_position_pct=20.0)
        self.assertEqual(status, "approved")
        self.assertLessEqual(n, 20.0 + 1e-9)

    def test_approve_trade_refuses_below_min_after_cap(self):
        r = engine(self.db)
        n, status = r.approve_trade(10000.0, 0.01, equity=10.0, max_position_pct=20.0)
        self.assertIsNone(n)
        self.assertIn("below_min_notional_after_pct_cap", status)

    def test_legacy_sizing_respects_pct_cap(self):
        old = dict(auto_trader.RISK_CONFIG)
        try:
            auto_trader.RISK_CONFIG["max_position_pct"] = 20.0
            notional, _ = auto_trader.calculate_position_size(
                100.0, {"priceChangePercent": "1.0"})
            self.assertLessEqual(notional, 20.0 + 1e-9)
        finally:
            auto_trader.RISK_CONFIG.clear()
            auto_trader.RISK_CONFIG.update(old)


class TestAccountSnapshot(unittest.TestCase):
    def test_snapshot_separates_equity_and_available(self):
        s = auto_trader.AccountSnapshot(equity=1000.0, available_balance=100.0, can_trade=True)
        ok, why = s.valid()
        self.assertTrue(ok, why)
        self.assertNotEqual(s.equity, s.available_balance)

    def test_snapshot_invalid_on_missing_or_nan(self):
        for kw in ({"equity": None, "available_balance": 10.0},
                   {"equity": float("nan"), "available_balance": 10.0},
                   {"equity": 0.0, "available_balance": 10.0},
                   {"equity": 100.0, "available_balance": 10.0, "can_trade": False}):
            s = auto_trader.AccountSnapshot(**kw)
            ok, _ = s.valid()
            self.assertFalse(ok, f"{kw} geçersiz sayılmalı")

    @staticmethod
    def _restore_entry_health(saved):
        with auto_trader._ENTRY_HEALTH_LOCK:
            auto_trader._ENTRY_HEALTH.clear()
            auto_trader._ENTRY_HEALTH.update(saved)

    def test_gate_blocks_on_insufficient_margin(self):
        saved = auto_trader._entry_health_reasons()
        with auto_trader._ENTRY_HEALTH_LOCK:
            auto_trader._ENTRY_HEALTH.clear()
        self.addCleanup(self._restore_entry_health, saved)
        r = engine(build(tempfile.mkdtemp()))
        s = auto_trader.AccountSnapshot(equity=1000.0, available_balance=1.0, can_trade=True)
        ok, why = auto_trader._risk_entry_gate(r, snapshot=s, notional=200.0)
        self.assertFalse(ok)
        self.assertIn("insufficient_margin", why)


if __name__ == "__main__":
    unittest.main()
