"""BLOK12 devam-F §3 (E2): 418/429 cooldown'unun SÜREÇ-DESTİ kalıcılığı.

Kusur (salt-okunur incelemede bulundu): api_guard durumu bellek-içiydi —
(a) restart'ta sıfırlanıyordu (418'den 2 dk sonra yeniden başlayan ana süreç
ban'ı yenilerdi), (b) ayrı süreçlerde koşan orphan_watchdog/orphan_killer
durumu HİÇ bilmezdi: IP ban sürerken turlama Binance isteği GÖNDERİR, ban
süresini uzatırdı. Görev §3 sözleşmesi: "418 sürerken Binance isteği YOK".

Onarım sözleşmesi (dosya-temelli, sırlar taşımaz — yalnız bitiş zamanı):
  * note_http_status(429/418) → STATE_DIR/api_guard.json (atomik tmp+rename).
  * blocked() bellek + DOSYAYA da bakar (yeniden başlayan süreç ban'ı görür).
  * orphan_watchdog: turdan ÖNCE dosya gardı — aktifse exit 2, İSTEK YOK.
  * Bozuk/eksik dosya → bloklama YOK (akış gardı; muhasebe değil) + not.
Ağ YOK. Kırmızı→yeşil: HEAD'de dosya hiç yazılmaz, blocked() dosyayı bilmez.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import api_guard                                    # noqa: E402
import auto_trader                                  # noqa: E402
import orphan_watchdog as OW                        # noqa: E402


class TestPersistence(unittest.TestCase):
    def setUp(self):
        api_guard.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="b12gf-"))
        self.fp = self.tmp / "api_guard.json"
        api_guard.set_state_file(str(self.fp))

    def tearDown(self):
        api_guard.reset()
        # modül bağlantısını geri kur (session-geneli izolasyon):
        api_guard.set_state_file(str(auto_trader.API_GUARD_STATE_FILE))

    def test_01_418_dosyaya_yazilir_ve_yeni_surec_gorur(self):
        api_guard.note_http_status(418, None)
        self.assertTrue(self.fp.exists(), "cooldown dosyası yazılmadı")
        data = json.loads(self.fp.read_text())
        self.assertGreater(data["cooldown_until"], 0)
        self.assertEqual(data["reason"], "418-ip-ban")
        # Yeni süreç: bellek SIFIR ve dosya henüz BAĞLI DEĞİL — ban görünmez
        # (reset() bilinçli olarak dosya bağını KESMEZ; üretimde yeni süreç
        # modül yüklemesinde set_state_file çağırır).
        api_guard.reset()
        api_guard.set_state_file(None)
        self.assertIsNone(api_guard.blocked())        # bellek temiz, dosya yok
        api_guard.set_state_file(str(self.fp))
        why = api_guard.blocked()
        self.assertIsNotNone(why, "restart sonrası ban görünmedi")
        self.assertIn("418", why)

    def test_02_suresi_dolen_dosya_artik_bloklamaz(self):
        self.fp.write_text(json.dumps({"cooldown_until": 1.0,
                                       "reason": "418-ip-ban"}))
        api_guard.set_state_file(str(self.fp))
        self.assertIsNone(api_guard.blocked())

    def test_03_bozuk_dosya_bloklamaz_akaci_durdurmaz(self):
        self.fp.write_text("{bozuk-JSON!!")
        api_guard.set_state_file(str(self.fp))
        self.assertIsNone(api_guard.blocked())        # akış gardı: fail-open
        # ...ama bellek-içi ban varsa dosya-bozuk bahanesiyle kalkmaz:
        api_guard.note_http_status(429, {"Retry-After": "600"})
        self.assertIsNotNone(api_guard.blocked())

    def test_04_dosya_sir_icermez(self):
        api_guard.note_http_status(429, {"Retry-After": "120"})
        raw = self.fp.read_text()
        for needle in ("BINANCE", "api_key", "secret", "sig"):
            self.assertNotIn(needle.lower(), raw.lower())

    def test_05_auto_trader_dosya_yolunu_baglar(self):
        # Ana süreç modül-yüklemesinde gard dosyasını STATE_DIR'e bağlar;
        # dosyada aktif cooldown varsa blocked() onu görür (merge YOK —
        # tek doğruluk kaynağı bellek ∨ dosya).
        import time
        self.assertEqual(str(auto_trader.API_GUARD_STATE_FILE),
                         str(auto_trader.STATE_DIR / "api_guard.json"))
        self.fp.write_text(json.dumps({"cooldown_until": time.time() + 300,
                                       "reason": "429-retry-after"}))
        api_guard.set_state_file(str(self.fp))
        why = api_guard.blocked()
        self.assertIsNotNone(why)
        self.assertIn("429", why)


class TestOrphanWatchdogRespectsBan(unittest.TestCase):
    def setUp(self):
        api_guard.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="b12gw-"))
        self.fp = self.tmp / "api_guard.json"

    def tearDown(self):
        api_guard.reset()
        api_guard.set_state_file(str(auto_trader.API_GUARD_STATE_FILE))

    def test_06_ban_surerken_tur_istek_YOK_exit2(self):
        import time
        self.fp.write_text(json.dumps({"cooldown_until": time.time() + 600,
                                       "reason": "418-ip-ban"}))
        calls = []
        rc = OW.run_once(lambda: calls.append(1),
                         str(self.tmp / "w.lock"), True,
                         guard_file=str(self.fp))
        self.assertEqual(rc, OW.EXIT_CONFIG)
        self.assertEqual(calls, [], "ban sürerken scanner koştu (istek giderdi)")

    def test_07_gard_temizken_tur_kosar(self):
        calls = []
        rc = OW.run_once(lambda: calls.append(1), str(self.tmp / "w2.lock"),
                         True, guard_file=str(self.tmp / "yok.json"))
        self.assertEqual(rc, OW.EXIT_OK)
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
