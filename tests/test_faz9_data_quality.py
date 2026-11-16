"""FAZ9 veri kalitesi: resolved_at damgası, `real` filtresinin kaldırılması, persist_bars karşılaştırması.

`real = fwd_ret > cost AND mae > -sl_pct` tanım gereği kazananları seçer; onunla hesaplanan win oranı tautolojiktir.
Bu dosya, raporların artık `fwd_ret > cost` kullandığını ve `resolved_at` ile resolver sürümünün ayırt edilebildiğini
kilitler. Hepsi geçici DB üzerinde; canlı DB'ye dokunulmaz.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import analyze_live_data as ald
import strategy_engine as se

OLD_SCHEMA = """CREATE TABLE signal_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at REAL NOT NULL, symbol TEXT NOT NULL, signal TEXT NOT NULL, family TEXT NOT NULL,
    regime TEXT NOT NULL, direction INTEGER NOT NULL, strength REAL,
    entry_ts INTEGER NOT NULL, entry_price REAL NOT NULL, horizon INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'live', resolved INTEGER NOT NULL DEFAULT 0,
    real INTEGER, fwd_ret REAL, persist_bars INTEGER, sl_pct REAL)"""


def make_old_db(path):
    """14 Kasım öncesi şema: resolved_at YOK, bir çözülmüş satır var."""
    c = sqlite3.connect(path)
    c.execute(OLD_SCHEMA)
    c.execute("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,entry_ts,"
              "entry_price,horizon,source,resolved,real,fwd_ret,persist_bars,sl_pct) "
              "VALUES (1,'BTCUSDT','rsi_div','exhaustion','range',1,1000,100,8,'live',1,1,0.01,5,0.02)")
    c.commit()
    c.close()


class ResolvedAtTest(unittest.TestCase):
    def test_the_migration_adds_the_column_and_keeps_old_rows_null(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "s.db")
        make_old_db(db)
        store = se.SignalStore(db)
        c = store._conn()
        cols = {r[1] for r in c.execute("PRAGMA table_info(signal_outcomes)")}
        self.assertIn("resolved_at", cols)
        self.assertIsNone(c.execute("SELECT resolved_at FROM signal_outcomes WHERE id=1").fetchone()[0])
        c.close()

    def test_the_migration_is_idempotent(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "s.db")
        make_old_db(db)
        se.SignalStore(db)._conn().close()
        se.SignalStore(db)._conn().close()
        c = sqlite3.connect(db)
        n = len([r for r in c.execute("PRAGMA table_info(signal_outcomes)") if r[1] == "resolved_at"])
        c.close()
        self.assertEqual(n, 1)

    def test_a_fresh_schema_has_the_column(self):
        tmp = tempfile.mkdtemp()
        store = se.SignalStore(os.path.join(tmp, "s.db"))
        c = store._conn()
        self.assertIn("resolved_at", {r[1] for r in c.execute("PRAGMA table_info(signal_outcomes)")})
        c.close()


class ResolverStampTest(unittest.TestCase):
    def _store_with_pending(self, tmp):
        db = os.path.join(tmp, "s.db")
        store = se.SignalStore(db)
        c = store._conn()
        c.execute("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,entry_ts,"
                  "entry_price,horizon,source,resolved,sl_pct) "
                  "VALUES (1,'BTCUSDT','rsi_div','exhaustion','range',1,1000,100,2,'live',0,0.02)")
        c.commit()
        c.close()
        return store, db

    def test_the_resolver_stamps_resolved_at(self):
        tmp = tempfile.mkdtemp()
        store, db = self._store_with_pending(tmp)
        cd = se.Candles(t=[1000, 1060, 1120], o=[100, 100, 100], h=[101, 102, 103],
                        l=[99, 98, 97], c=[100, 101, 102], v=[1, 1, 1])
        stats = store.resolve_pending_detailed("BTCUSDT", cd, 0.0008)
        self.assertGreaterEqual(stats["resolved"], 1)
        c = sqlite3.connect(db)
        row = c.execute("SELECT resolved, resolved_at FROM signal_outcomes WHERE id=1").fetchone()
        c.close()
        self.assertEqual(row[0], 1)
        self.assertIsNotNone(row[1])
        self.assertTrue(row[1].endswith("Z"), row[1])

    def test_an_unresolved_row_keeps_resolved_at_null(self):
        tmp = tempfile.mkdtemp()
        store, db = self._store_with_pending(tmp)
        cd = se.Candles(t=[1000], o=[100], h=[101], l=[99], c=[100], v=[1])
        store.resolve_pending_detailed("BTCUSDT", cd, 0.0008)
        c = sqlite3.connect(db)
        self.assertIsNone(c.execute("SELECT resolved_at FROM signal_outcomes WHERE id=1").fetchone()[0])
        c.close()


class RealFilterTest(unittest.TestCase):
    def _db(self, tmp):
        db = os.path.join(tmp, "s.db")
        c = sqlite3.connect(db)
        c.execute(se._SCHEMA)
        rows = [(1, "BTCUSDT", "a", "exhaustion", "range", 1, 1000, 100, 8, "live", 1, 1, 0.01, 5, 0.02),
                (2, "BTCUSDT", "a", "exhaustion", "range", 1, 1060, 100, 8, "live", 1, 0, -0.01, 1, 0.02),
                (3, "BTCUSDT", "a", "exhaustion", "range", 1, 1120, 100, 8, "live", 1, None, 0.02, 4, 0.02)]
        c.executemany("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,entry_ts,"
                      "entry_price,horizon,source,resolved,real,fwd_ret,persist_bars,sl_pct) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.commit()
        c.close()
        return db

    def test_by_default_the_real_filter_is_not_applied(self):
        db = self._db(tempfile.mkdtemp())
        self.assertEqual(len(ald.load_signal_rows(db)), 3)

    def test_real_only_keeps_the_tautological_subset(self):
        db = self._db(tempfile.mkdtemp())
        self.assertEqual(len(ald.load_signal_rows(db, real_only=True)), 2)

    def test_win_is_computed_from_fwd_not_from_real(self):
        db = self._db(tempfile.mkdtemp())
        rows = ald.load_signal_rows(db)
        self.assertEqual([ald.win_of(r) for r in rows], [1, 0, 1])

    def test_persist_bars_compares_the_persistent_subset(self):
        db = self._db(tempfile.mkdtemp())
        out = ald.persist_bars_analysis(ald.load_signal_rows(db))
        self.assertEqual(out["all"]["n"], 3)
        self.assertEqual(out["persistent"]["n"], 2)
        self.assertGreater(out["persistent"]["ev_pct"], out["all"]["ev_pct"])

    def test_the_report_states_that_real_was_not_used(self):
        db = self._db(tempfile.mkdtemp())
        rep, _ = ald.run("/nonexistent/whale.db", db, None, n_perm=10)
        self.assertIn("real filtresi KULLANILMADI", rep["strategy"]["real_filter"])
        self.assertIn("fwd_ret > cost", rep["strategy"]["win_definition"])
        self.assertIn("persist_bars", rep["strategy"])


if __name__ == "__main__":
    unittest.main()
