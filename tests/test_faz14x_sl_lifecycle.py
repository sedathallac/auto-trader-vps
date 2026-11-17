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

    def test_closed_is_terminal_not_active(self):
        # CLOSED, acquire açısından BUSY sayılmamalı (aynı token bile olsa yeni
        # yerleşime izin verilmeli mi? Hayır — aynı token CLOSED ise pozisyon
        # hâlâ flat demek; BUSY/terminal davranışı supersede'e bırakılır.)
        self.store.acquire("ETHUSDT", "LONG:10")
        self.store.mark_placed("ETHUSDT", "LONG:10")
        self.store.mark_closed_flat("ETHUSDT", "LONG:10")
        self.assertEqual(self.store.acquire("ETHUSDT", "LONG:10"), sp.ACQUIRED,
                         "CLOSED aynı token'da yeniden yerleşimi kilitliyor")


if __name__ == "__main__":
    unittest.main()
