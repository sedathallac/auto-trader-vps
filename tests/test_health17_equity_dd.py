"""FAZ17 sağlık — geçersiz equity → sahte Drawdown%100 halt regresyonu (§12.1/§12.2).

Kök neden (canlı kanıt, journal 2026-11-19 04:39:07 +03, eski kod @01229ce):
    "💰 Equity=0.0 | DD=%1e+02 | Peak=3.5e+03" → "🚨 DEVRE KESİCİ: Drawdown %100.0"
Eski update_equity'nin hiçbir guard'ı yoktu: ban/kesinti anında equity okuması 0.0
dönerse DD=(peak−0)/peak=1.0 → halt KALICI olarak yazılıyordu (bugünkü halt_reason
artığı budur; gerçek DD %1.0). FAZ16 guard'ı kalıcılaştırdı; bu test o sözleşmeyi
kilitler ve tekrarını engeller:

  * equity geçersiz (None/metin/NaN/Inf/≤0) → update_equity False, halt YAZILMAZ,
    drawdown/baseline/DB BOZULMAZ (fail-closed: giriş zaten kapalı, ama sahte
    finansal zarar ÜRETİLMEZ).
  * gerçek drawdown → halt ÇALIŞIR ve kalıcıdır; reload'da korunur, yeni tepe
    halt'ı TEMİZLEMEZ.
  * günlük baseline: gün içinde TEK KEZ ve idempotent; "reset bugün ama baseline
    yok" durumunda geçmiş baseline UYDURULMAZ (daily_baseline_missing bloğu).

Yok-hücumu (old-code) kanıtı ayrıdır: evidence diziminde eski modülle üretilen
RED çıktısı durur; bu dosya mevcut üretime yönelik kalıcı kilit-testidir.
"""
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import risk_engine

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

CFG = {
    "max_trades_per_day": 20, "max_consecutive_losses": 3,
    "risk_per_trade_pct": 0.015, "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008, "slippage_pct_round_trip": 0.0004,
    "min_net_reward_risk": 1.20, "min_notional_policy": "refuse",
    "min_notional": 5.5, "max_notional": 25.0, "max_drawdown_pct": 0.2,
    "max_total_margin_pct": 0.4, "max_daily_loss_pct": 0.05,
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "risk_ledger.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(FULL_DDL)
        conn.commit()
        conn.close()

    def engine(self):
        return risk_engine.RiskEngine(dict(CFG, db_file=self.db))

    def raw(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchone()
        finally:
            conn.close()


class TestInvalidEquityNoFalseHalt(Base):
    def test_zero_equity_does_not_fake_hundred_pct_halt(self):
        r = self.engine()
        self.assertTrue(r.update_equity(3500.0))
        self.assertEqual(r.current_drawdown, 0.0)
        # Ban/kesinti anındaki 0.0 okuması: block et, HİÇBİR ŞEYİ bozma.
        self.assertFalse(r.update_equity(0.0))
        self.assertFalse(r.is_halted, "equity=0.0 sahte halt üretti (§12.1)")
        self.assertEqual(r.current_drawdown, 0.0, "drawdown kirlendi")
        self.assertIsNone(r.halt_reason)
        self.assertFalse(r.health_ok, "geçersiz equity blok işareti bırakmalı")
        self.assertIn("equity geçersiz", r.health_reason)
        row = self.raw("SELECT peak_equity, is_halted, day_start_equity FROM risk_state")
        self.assertEqual(row, (3500.0, 0, 3500.0), "DB sahte değerle ezildi")

    def test_invalid_variants_all_blocked_no_halt(self):
        # F25 sözleşme güncellemesi: TEK geçersiz okuma asla halt değildir ve
        # SAHTE halt (Drawdown hükmü) üretemez — bu değişmez. F25 fail-safe
        # (BLOK2 İŞ0.6) ARDIŞIK 3 okunamayan equity'de EQUITY_UNAVAILABLE ile
        # halt verir; bu ayrı bir sözleşmedir (test_faz18_f25_dd_gate.py).
        # Varant-başına taze motor: "tek tetik halt üretmez" anlamı korunur.
        for bad in (None, " ", "abc", float("nan"), float("inf"), float("-inf"), -10.0):
            with self.subTest(bad=bad):
                r = self.engine()
                r.update_equity(3500.0)
                self.assertFalse(r.update_equity(bad))
                self.assertFalse(r.is_halted)
                self.assertEqual(r.current_drawdown, 0.0)

    def test_zero_equity_does_not_pollute_daily_baseline(self):
        r = self.engine()
        self.assertFalse(r.update_equity(0.0))
        self.assertEqual(r.day_start_equity, 0.0, "0.0 baseline olarak uyduruldu")
        self.assertIsNone(r.day_start_equity_date)


class TestRealDrawdownStillWorks(Base):
    def test_true_drawdown_halts_and_persists(self):
        r = self.engine()
        r.update_equity(5000.0)  # peak=5000, baseline=5000
        # Günü ayır: baseline 4000 (günlük kayıp %0), peak 5000 → yalnız DD-kapısı
        # ölçülebilir (günlük kesici %5 ile önce tetiklenmesin — ayrı testtir).
        from risk_engine import utc_day
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE risk_state SET day_start_equity=4000, "
                     "day_start_equity_date=?", (utc_day(),))
        conn.commit()
        conn.close()
        r = self.engine()
        self.assertTrue(r.update_equity(4000.0))  # DD=(5000-4000)/5000=%20
        self.assertTrue(r.is_halted, "gerçek drawdown halt üretmedi — guard aşırı geniş")
        self.assertIn("Drawdown", r.halt_reason)
        re_loaded = self.engine()
        self.assertTrue(re_loaded.is_halted, "halt kalıcı değil")

    def test_daily_breaker_is_separate_and_fires(self):
        r = self.engine()
        r.update_equity(3500.0)
        self.assertTrue(r.update_equity(2700.0))  # günlük %22.9 ≥ %5
        self.assertTrue(r.is_halted)
        self.assertIn("daily_loss_pct", r.halt_reason)

    def test_new_high_does_not_clear_halt(self):
        r = self.engine()
        r.update_equity(3500.0)
        r.update_equity(2700.0)
        self.assertTrue(r.is_halted)
        r.update_equity(3600.0)
        self.assertTrue(r.is_halted, "halt otomatik temizlendi — operatör kararı gerekir")
        self.assertEqual(r.peak_equity, 3600.0)


class TestDailyBaselineDiscipline(Base):
    def test_baseline_recorded_once_and_idempotent(self):
        r = self.engine()
        r.update_equity(3500.0)
        base_at = r.day_start_equity_at
        r.update_equity(3400.0)  # aynı gün: baseline KORUNUR
        self.assertEqual(r.day_start_equity, 3500.0)
        self.assertEqual(r.day_start_equity_at, base_at)

    def test_missing_baseline_blocks_instead_of_inventing(self):
        # Migration-sonrası canlı durum: reset bugün ama baseline yok.
        from risk_engine import utc_day
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE risk_state SET last_reset_date=?, day_start_equity=0, "
                     "day_start_equity_date=NULL, day_start_equity_at=NULL",
                     (utc_day(),))
        conn.commit()
        conn.close()
        r = self.engine()
        self.assertFalse(r.update_equity(3400.0))
        self.assertIn("daily_baseline_missing", r.health_reason)
        self.assertEqual(r.day_start_equity, 0.0, "geçmiş baseline uyduruldu")
        row = self.raw("SELECT day_start_equity_date FROM risk_state")
        self.assertIsNone(row[0])


class TestConcurrentBaseline(Base):
    """§12.2 — çok-yazarlı aynı DB: baseline TEK ve tutarlı yazılmalı,
    hiçbir yazar hata almamalı/torn state görmemeli (BEGIN IMMEDIATE seri)."""

    def test_concurrent_update_equity_single_consistent_baseline(self):
        import threading
        barrier = threading.Barrier(8)
        results, errors = [], []

        def worker():
            try:
                r = self.engine()  # her thread KENDİ bağlantısı/memarisi
                barrier.wait(timeout=15)
                ok = r.update_equity(3400.0)
                results.append(ok)
            except Exception as e:  # noqa: BLE001 — test teşhisi
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [], f"eşzamanlı yazarda hata: {errors}")
        self.assertEqual(len(results), 8)
        self.assertTrue(all(results), "eşzamanlı update_equity False döndü")
        row = self.raw("SELECT day_start_equity, day_start_equity_date, "
                       "peak_equity, is_halted FROM risk_state")
        from risk_engine import utc_day
        self.assertEqual(row[0], 3400.0, "baseline torn/kayıp")
        self.assertEqual(row[1], utc_day())
        self.assertEqual(row[2], 3400.0)
        self.assertFalse(row[3])
        # Reload: bütün yazarlar AYNI kalıcı durumu görür (idempotent konsensüs).
        r2 = self.engine()
        self.assertEqual(r2.day_start_equity, 3400.0)
        self.assertIsNotNone(r2.day_start_equity_at)


def _mp_worker(db, q):
    """Gerçek AYRI SÜREÇ: kendi SQLite bağlantısı + RiskEngine'i."""
    try:
        import risk_engine as _re
        cfg = dict(CFG, db_file=db)
        r = _re.RiskEngine(cfg)
        ok = r.update_equity(3400.0)
        q.put(("ok" if ok else "false", None))
    except Exception as e:  # noqa: BLE001 — test teşhisi
        q.put(("error", f"{type(e).__name__}: {e}"))


class TestMultiprocessBaseline(Base):
    """§12.2 (gerçek çoklu-süreç): 4 AYRI process aynı DB'ye eşzamanlı
    update_equity — tek tutarlı UTC baseline, hiç hata/yarış yok."""

    def test_four_processes_one_baseline(self):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_mp_worker, args=(self.db, q))
                 for _ in range(4)]
        # Ayrı proses'lerin SQLite write-lock kuyruğu timeout=30s içinde çözülür.
        for p in procs:
            p.start()
        results = []
        import time as _t
        t0 = _t.time()
        while len(results) < 4 and _t.time() - t0 < 60:
            try:
                results.append(q.get(timeout=2))
            except Exception:
                pass
        for p in procs:
            p.join(timeout=15)
        self.assertEqual(len(results), 4, f"proses sonuçları eksik: {results}")
        errs = [r for r in results if r[0] == "error"]
        self.assertEqual(errs, [], f"proses hataları: {errs}")
        self.assertTrue(all(r[0] == "ok" for r in results),
                        f"update_equity False üreten proses: {results}")
        row = self.raw("SELECT day_start_equity, peak_equity, is_halted "
                       "FROM risk_state")
        self.assertEqual(row[0], 3400.0, "baseline kayboldu/ezildi")
        self.assertEqual(row[1], 3400.0)
        self.assertFalse(row[2])
        r2 = self.engine()
        self.assertEqual(r2.day_start_equity, 3400.0)
        self.assertIsNotNone(r2.day_start_equity_at)


if __name__ == "__main__":
    unittest.main()
