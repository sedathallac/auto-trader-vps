"""FAZ14x — risk_engine günlük-sayaç bütünlüğü regresyonları (yalnız hedefli).

Kanıtlanan kusurlar (kaynak: risk_engine.py @1c9f904 + canlı risk_state kanıtı):
  1. `_save_state` UPSERT'i `last_reset_date` alanını DO UPDATE SET'e dahil
     ETMİYOR: bellekteki tarih DB'ye hiçbir zaman yazılmıyor. Kanıt: canlı DB
     last_reset_date=2026-11-15 donmuş, makine 2026-11-17'de; her servis
     restart'ında `last_reset_date != today` -> reset_daily() -> aynı gün
     birikmiş daily_trades/daily_pnl SİLİNİYOR (canlı kanıt: 4 aynı-gün açılış
     varken daily_trades=0).
  2. `reset_daily()` tarihi hiç kalıcılaştırmıyor; çağrı yeri bellekte
     düzeltiyor ama 1 yüzünden DB'de ölüyor.
  3. `record_trade` `source` parametresi yok; provenance ('sync') sabit.
  4. Gün tarihi yerel saatle üretiliyor (risk_engine + çağrı yeri), DB UTC —
     karışık saat dili.

  Gate gerçeği (G6/G7 sorusuna kanıt): is_halted=0 iken bile
  `consecutive_losses >= max_consecutive_losses` entries ENGELLER — bu
  davranışı KORUYAN test de burada (regresyona karşı sabitler).
"""
import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from risk_engine import RiskEngine

CFG = {
    "db_file": None,  # setUp doldurur
    "max_consecutive_losses": 3,
    "max_trades_per_day": 100,
    "max_drawdown_pct": 0.5,
    "max_daily_loss_pct": 0.5,
    "risk_per_trade_pct": 0.01,
    "min_notional": 10,
    "max_notional": 1000,
}


class RiskDailyHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        # production trade_ledger şeması (source kolonu dahil) — RiskEngine
        # bu tabloyu oluşturmaz, canlıda auto_trader kurar; test aynılar.
        conn = sqlite3.connect(self.db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_ledger (
                executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL,
                closed_at TEXT, recorded_at TEXT, source TEXT DEFAULT 'sync')
        """)
        conn.commit()
        conn.close()
        cfg = dict(CFG)
        cfg["db_file"] = self.db
        self.cfg = cfg
        self.engine = RiskEngine(cfg)

    def _raw(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchone()
        finally:
            conn.close()

    def _reload(self):
        return RiskEngine(self.cfg)


class TestLastResetDatePersistence(RiskDailyHarness):
    def test_save_state_persists_last_reset_date(self):
        """UPSERT last_reset_date'i DO UPDATE SET'e yazmalı.

        Kritik: init anında tablo `or now[:10]` ile BUGÜNÜ yazar (self-heal).
        Kanıt için BUGÜN OLMAYAN bir sentinel gerekir; SET'de alan yoksa DB
        eski (init) tarihte kalır — canlıdaki 2026-11-15 donmasının mekanizması.
        """
        self.engine._save_state()  # satırı init-değeriyle (bugün) INSERT et
        sentinel = "2020-01-02"
        self.engine.state["last_reset_date"] = sentinel
        self.engine._save_state()  # artık UPDATE yolu çalışır
        db_date = self._raw("SELECT last_reset_date FROM risk_state WHERE id=1")[0]
        self.assertEqual(db_date, sentinel,
                         "P2: last_reset_date UPDATE'de yok — DB eski tarihte donuyor "
                         "(restart'ta reset_daily -> günlük sayaç silinme döngüsü)")
        reloaded = self._reload()
        self.assertEqual(reloaded.last_reset_date, sentinel,
                         "reload sonrası tarih geri yüklenmedi")

    def test_reset_daily_persists_date_and_zeroes_counters(self):
        self.engine.daily_trades = 4
        self.engine.daily_pnl = -3.2
        self.engine.consecutive_losses = 2
        self.engine._save_state()
        today = datetime.now(timezone.utc).date().isoformat()
        self.engine.reset_daily(reset_date=today)
        reloaded = self._reload()
        self.assertEqual(reloaded.last_reset_date, today,
                         "reset_daily tarihi kalıcılaştırmıyor")
        self.assertEqual(reloaded.daily_trades, 0)


class TestRecordTradeProvenance(RiskDailyHarness):
    def test_record_trade_accepts_and_persists_source(self):
        ok, _ = self.engine.record_trade("E1", "BTC-USDT", -0.5,
                                         "2026-11-17T00:00:00+00:00",
                                         affect_daily=False, source="binance_fill_verified")
        self.assertTrue(ok)
        row = self._raw("SELECT source FROM trade_ledger WHERE executor_id='E1'")
        self.assertIsNotNone(row, "kayıt yazılmadı")
        self.assertEqual(row[0], "binance_fill_verified",
                         "P1 provenance: source parametresi yok/yazılmıyor")

    def test_default_source_stays_sync(self):
        self.engine.record_trade("E2", "ETH-USDT", 0.1, "2026-11-17T00:00:00+00:00",
                                 affect_daily=False)
        row = self._raw("SELECT source FROM trade_ledger WHERE executor_id='E2'")
        self.assertEqual(row[0], "sync", "geriye uyumlu varsayılan bozuldu")


class TestConsecutiveGateIndependentOfHalt(RiskDailyHarness):
    """G6/G7 — is_halted=0 olsa bile loss_limit girişleri engeller (KORUMA testi)."""

    def test_loss_limit_blocks_even_when_not_halted(self):
        self.engine.is_halted = False
        self.engine.consecutive_losses = self.cfg["max_consecutive_losses"]
        notional, reason = self.engine.approve_trade(1000.0, 0.02)
        self.assertIsNone(notional, "ardışık zarar limiti girişleri engellemedi")
        self.assertIn("loss_limit", reason)
        self.assertFalse(self.engine.is_halted,
                         "loss_limit is_halted'i karıştırmamalı (ayrı kapı)")


if __name__ == "__main__":
    unittest.main()
