"""FAZ14x — sl_placement yaşam döngüsü regresyonları (yalnız hedefli).

Kanıtlanan kusur (kaynak: sl_placement.py @1c9f904 + canlı DB kanıtı):
  PENDING -> PLACED var; PLACED'den çıkan TEK yol release() (yer-silme, yalnız
  yerleştirme-HATASINDA). Pozisyon kapandığında kayıt terminal duruma geçmiyor;
  canlı DB'de 15 PLACED satırı var ama Binance'te sadece 4 açık algo SL var —
  11'i ölü sembol için sonsuza dek "PLACED" görünüyor.

  Ek hedef: sync, K2-flat kanıtı gördüğünde sembolü CLOSED yabilsin (CLOSED =
  exchange-flat kanıtıyla kapanan dönem). Yeni dönem (farklı token) zaten
  supersede eder; CLOSED satırı supersede'i engellememeli.
"""
import os
import sys
import sqlite3
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sl_placement as sp


class SlLifecycleHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self.store = sp.SlPlacementStore(self.db)
        self.store.init_schema()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state_of(self, symbol):
        conn = sqlite3.connect(self.db)
        try:
            r = conn.execute("SELECT state FROM sl_placements WHERE symbol=?",
                             (symbol,)).fetchone()
            return r[0] if r else None
        finally:
            conn.close()


class TestClosedState(SlLifecycleHarness):
    def test_placed_transitions_to_closed(self):
        self.assertEqual(self.store.acquire("BTCUSDT", "LONG:100"), sp.ACQUIRED)
        self.store.mark_placed("BTCUSDT", "LONG:100")
        self.assertEqual(self.state_of("BTCUSDT"), "PLACED")

        closed = self.store.mark_closed_flat("BTCUSDT", "LONG:100")
        self.assertTrue(closed, "mark_closed_flat geri dönmedi")
        self.assertEqual(self.state_of("BTCUSDT"), "CLOSED",
                         "P2: PLACED kayıt CLOSED terminal durumuna geçmiyor")

    def test_closed_does_not_block_new_epoch(self):
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        self.store.mark_closed_flat("BTCUSDT", "LONG:100")
        # Yeni pozisyon dönemi (farklı token) CLOSED'ı supersede etmeli.
        self.assertEqual(self.store.acquire("BTCUSDT", "SHORT:90"), sp.ACQUIRED,
                         "CLOSED satırı yeni dönemi engelliyor")
        self.assertEqual(self.state_of("BTCUSDT"), "PENDING")

    def test_pending_not_touched_by_wrong_token(self):
        self.store.acquire("SOLUSDT", "SHORT:70")
        # Eski dönemin flat-kanıtı YANLIŞ token ile gelirse yeni PENDING silinmemeli.
        self.assertFalse(self.store.mark_closed_flat("SOLUSDT", "SHORT:69"))
        self.assertEqual(self.state_of("SOLUSDT"), "PENDING")

    def test_missing_row_returns_false(self):
        self.assertFalse(self.store.mark_closed_flat("NOPE-USDT", "LONG:1"))

    def test_stale_token_noop_after_new_epoch(self):
        # Sweep T1 satırını okuduktan SONRA yeni epoch (T2) acquire edilirse
        # (bayat flat-snapshot yarışı), T1 ile güncelleme 0 satır etkiler.
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        self.assertEqual(self.store.acquire("BTCUSDT", "LONG:101"), sp.ACQUIRED)
        self.assertFalse(self.store.mark_closed_flat("BTCUSDT", "LONG:100"),
                         "bayat token ile yeni dönem kapatıldı")
        row = self.store.get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PENDING", "LONG:101"),
                         "yeni epoch'un PENDING'i bozuldu")

    def test_mark_placed_self_heals_after_closed(self):
        # SIDE:entryPrice epoch'a özgü DEĞİL: aynı token'lı yeni dönem
        # yanlışlıkla CLOSED edilse bile sahibinin mark_placed'i PLACED'a
        # döndürür (self-heal); yerleştirme akışı kilitlenmez.
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        self.store.mark_closed_flat("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        self.assertEqual(self.state_of("BTCUSDT"), "PLACED",
                         "mark_placed CLOSED üzerinden iyileşmedi")

    def test_closed_is_terminal_not_active(self):
        # CLOSED, acquire açısından BUSY sayılmamalı (aynı token bile olsa yeni
        # yerleşime izin verilmeli mi? Hayır — aynı token CLOSED ise pozisyon
        # hâlâ flat demek; BUSY/terminal davranışı supersede'e bırakılır.)
        self.store.acquire("ETHUSDT", "LONG:10")
        self.store.mark_placed("ETHUSDT", "LONG:10")
        self.store.mark_closed_flat("ETHUSDT", "LONG:10")
        self.assertEqual(self.store.acquire("ETHUSDT", "LONG:10"), sp.ACQUIRED,
                         "CLOSED aynı token'da yeniden yerleşimi kilitliyor")


class TestEpochGeneration(unittest.TestCase):
    """FAZ14x-2 nesil-2 — SQLite monoton INTEGER epoch generation.

    updated_at duvar saatidir; NTP step-back'te geri-gider ve saat-koşullu
    guard'ı atlatır. Gerçek monotonluk yalnız DB-içi sayaçla olur: her yeni
    epoch acquire'ında ve güvenlik açısından önemli durumda (mark_placed,
    CLOSED) atomik artar; snapshot'la yakalanan generation, koşullu UPDATE'te
    doğrulanır. Saat geri alınırsa da generation ETKİLENMEZ.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self.store = sp.SlPlacementStore(self.db)
        self.store.init_schema()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state_of(self, symbol):
        conn = sqlite3.connect(self.db)
        try:
            r = conn.execute("SELECT state FROM sl_placements WHERE symbol=?",
                             (symbol,)).fetchone()
            return r[0] if r else None
        finally:
            conn.close()

    # G-a — acquire ve mark_placed generation'ı atomik artırır; sayaç monoton.
    def test_generation_bumps_on_acquire_and_mark_placed(self):
        g0 = self.store.current_generation()
        self.store.acquire("BTCUSDT", "LONG:100")
        g1 = self.store.current_generation()
        self.assertGreater(g1, g0, "acquire generation artırmadı")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        g2 = self.store.current_generation()
        self.assertGreater(g2, g1, "mark_placed generation artırmadı")

    # G-b — Eski generation ile CLOSED UPDATE sıfır satır etkilemeli;
    # aynı token yeniden kullanılsa bile eski kanıt yeni dönemi kapatamaz.
    def test_stale_generation_close_affects_zero_rows(self):
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        g_snap = self.store.current_generation()
        # Dönem kapanır, AYNI token yeniden doğar (release→acquire):
        self.store.release("BTCUSDT", "LONG:100")
        self.assertEqual(self.store.acquire("BTCUSDT", "LONG:100"), sp.ACQUIRED)
        self.store.mark_placed("BTCUSDT", "LONG:100")
        self.assertFalse(
            self.store.mark_closed_flat("BTCUSDT", "LONG:100", max_gen=g_snap),
            "eski generation, aynı token'lı yeni dönemi kapattı")
        self.assertEqual(self.state_of("BTCUSDT"), "PLACED")

    # G-c — Doğru (snapshot ile gelen) generation doğru epoch'u kapatır.
    def test_matching_generation_closes_correct_epoch(self):
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        g_snap = self.store.current_generation()
        self.assertTrue(
            self.store.mark_closed_flat("BTCUSDT", "LONG:100", max_gen=g_snap))
        self.assertEqual(self.state_of("BTCUSDT"), "CLOSED")

    # G-d — PENDING, yüksek max_gen ile bile CLOSED EDİLEMEZ (madde 8).
    def test_pending_never_closed_by_generation_predicate(self):
        self.store.acquire("BTCUSDT", "LONG:100")  # PENDING
        g = self.store.current_generation()
        self.assertFalse(
            self.store.mark_closed_flat("BTCUSDT", "LONG:100", max_gen=g + 100),
            "PENDING kayıt CLOSED edildi")
        self.assertEqual(self.state_of("BTCUSDT"), "PENDING")

    # G-e — Saat geriye alınsa da generation monoton kalır ve bayat kanıt
    # yeni dönemi kapatamaz (SAAT-BAĞIMSIZLIĞININ KANITI).
    def test_clock_rewind_does_not_disturb_generation(self):
        g_before = self.store.current_generation()
        real = sp._now
        try:
            sp._now = lambda: "2000-01-01T00:00:00+00:00"  # ağır NTP step-back
            self.store.acquire("BTCUSDT", "SHORT:1")
            self.store.mark_placed("BTCUSDT", "SHORT:1")
        finally:
            sp._now = real
        g_after = self.store.current_generation()
        self.assertGreater(g_after, g_before,
                           "geri-saat generation'ı geri aldı (monotonluk bozuk)")
        self.assertFalse(
            self.store.mark_closed_flat("BTCUSDT", "SHORT:1", max_gen=g_before),
            "geri-saatli yeni dönem eski generation kanıtıyla kapandı")
        self.assertEqual(self.state_of("BTCUSDT"), "PLACED")

    # --- Şema geçidi (yalnız YEREL test DB'leri; canlıya uygulanmaz) -------

    def _legacy_db(self, name="legacy.db"):
        """FAZ14x (01229ce) şeması: sl_placements VAR, gen/meta YOK."""
        path = os.path.join(self.tmp, name)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE sl_placements (
                symbol TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        conn.execute(
            "INSERT INTO sl_placements VALUES "
            "('BTCUSDT','LONG:1','PLACED','2026-11-17T00:00:00+00:00','2026-11-17T00:00:01+00:00')")
        conn.commit()
        conn.close()
        return path

    def _columns(self, path, table):
        conn = sqlite3.connect(path)
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    # M-a — Eski DB şeması güvenli yükseltilir: gen kolonu eklenir, meta
    # tohumlanır, mevcut satırlar/veri bozulmaz, sayaç eski max'tan devam eder.
    def test_legacy_schema_upgrades_safely(self):
        path = self._legacy_db()
        store = sp.SlPlacementStore(path)
        store.init_schema()
        self.assertIn("gen", self._columns(path, "sl_placements"),
                      "migration gen kolonunu eklemedi")
        self.assertIn("gen", self._columns(path, "sl_placement_meta"),
                      "meta tablosu oluşmadı")
        row = store.get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PLACED", "LONG:1"),
                         "migration mevcut satırı bozdu")
        g = store.current_generation()
        self.assertIsInstance(g, int)
        self.assertGreaterEqual(g, 0)
        # Yükseltilmiş DB'de generation akışı çalışır:
        g0 = store.current_generation()
        store.acquire("BTCUSDT", "SHORT:9")  # supersede
        self.assertGreater(store.current_generation(), g0)

    # M-b — Migration idempotent: tekrar çalıştırma şemayı/veriyi/sayacı
    # değiştirmez.
    def test_migration_is_idempotent(self):
        path = self._legacy_db()
        store = sp.SlPlacementStore(path)
        store.init_schema()
        g1 = store.current_generation()
        store.acquire("BTCUSDT", "SHORT:9")   # supersede → gen artar
        g2 = store.current_generation()
        store.init_schema()                    # ikinci + üçüncü tur
        store.init_schema()
        self.assertEqual(store.current_generation(), g2,
                         "tekrar migration sayacı sıfırladı")
        self.assertGreater(g2, g1)
        row = store.get("BTCUSDT")
        self.assertEqual((row["token"], row["state"]), ("SHORT:9", "PENDING"),
                         "tekrar migration satırı bozdu")


if __name__ == "__main__":
    unittest.main()
