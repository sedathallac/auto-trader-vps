"""BLOK11 §8.1 — heartbeat YAZMA + freshness sözleşmesi (doğrulanmış gerçek).

Review iddiası "heartbeat_writer.py hiçbir şey yazmıyor" GERÇEK KODA KARŞI
REFUTED: dosya yok; gerçek yazar auto_trader.heartbeat_tick() (:904) ve main()
döngüsü her ~60 sn'de çağırıyor (:5269/:5585). Bu dosya o yazma sözleşmesini ve
orphan_killer.check_heartbeat okuyucusunun THREE durumunu (taze/bayat/bozuk)
doğrudan çapalar — BLOK10 F-D kararının (bayat HB temizliği DURDURMAZ) üstüne
yazma tarafının kanıtını ekler.

Gerçek ~/.auto_trader.heartbeat dosyasına DOKUNULMAZ (path monkeypatch edilir).
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import auto_trader
import orphan_killer


class TestHeartbeatWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blok11-hb-"))
        self._orig = auto_trader.HEARTBEAT_FILE
        auto_trader.HEARTBEAT_FILE = self.tmp / ".auto_trader.heartbeat"

    def tearDown(self):
        auto_trader.HEARTBEAT_FILE = self._orig

    def test_heartbeat_tick_dosyaya_yazar(self):
        auto_trader.heartbeat_tick()
        self.assertTrue(self.tmp.joinpath(".auto_trader.heartbeat").exists())
        # içerik parse-edilebilir ISO zamanı olmalı (okuyucu bunu bekler)
        ts = datetime.fromisoformat(
            auto_trader.HEARTBEAT_FILE.read_text().strip())
        self.assertLess(abs(datetime.now() - ts), timedelta(seconds=10))

    def test_yazma_hatasi_durumu_dusurmaz(self):
        # yazılamayan path → log.error, istisna YAYILMAZ (döngü ölmez)
        auto_trader.HEARTBEAT_FILE = self.tmp / "yok-dizin" / "hb"
        auto_trader.heartbeat_tick()  # patlamamalı


class TestHeartbeatReader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blok11-hb-r-"))
        self._orig = orphan_killer.HEARTBEAT_FILE
        orphan_killer.HEARTBEAT_FILE = self.tmp / "hb"

    def tearDown(self):
        orphan_killer.HEARTBEAT_FILE = self._orig

    def test_taze_true(self):
        orphan_killer.HEARTBEAT_FILE.write_text(datetime.now().isoformat())
        self.assertTrue(orphan_killer.check_heartbeat())

    def test_bayat_false(self):
        old = datetime.now() - timedelta(
            seconds=orphan_killer.MAX_HEARTBEAT_AGE + 60)
        orphan_killer.HEARTBEAT_FILE.write_text(old.isoformat())
        self.assertFalse(orphan_killer.check_heartbeat())

    def test_eksik_false(self):
        self.assertFalse(orphan_killer.check_heartbeat())

    def test_bozuk_icsek_false(self):
        orphan_killer.HEARTBEAT_FILE.write_text("bu-zaman-damgasi-degil")
        self.assertFalse(orphan_killer.check_heartbeat())

    def test_writer_reader_sozlesmesi(self):
        """Yazan formatı okuyucu kabul eder (entegrasyon dikişi)."""
        auto_trader.HEARTBEAT_FILE = orphan_killer.HEARTBEAT_FILE
        try:
            auto_trader.heartbeat_tick()
            self.assertTrue(orphan_killer.check_heartbeat())
        finally:
            auto_trader.HEARTBEAT_FILE = Path(
                tempfile.mkdtemp(prefix="blok11-hb-x-")) / "unused"


if __name__ == "__main__":
    unittest.main()
