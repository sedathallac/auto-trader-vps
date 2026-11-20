"""BLOK11 §8.3 — watchdog sarmalayıcısı sözleşmesi (N1: planlama yokluğu P1).

Doğrulanmış kök neden: crontab boş, systemd unit yok, üretim kodu orphan_killer'ı
çağırmıyor, süreç yok → SL backstop'u fiilen hiç koşmuyordu. Sarmalayıcı tek
tur + flock + çıkış kodları verir; KURULUM operatör kararıdır.

Testler scanner'ı enjekte eder: tek bir gerçek ağ çağrısı yapılmaz.
"""
import fcntl
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))
from orphan_watchdog import (EXIT_CONFIG, EXIT_OK, EXIT_SCAN_ERROR, run_once)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(os.environ.get("FAZ9_STATE_DIR",
                                               __import__("tempfile").gettempdir()),
                                f"wd-{os.getpid()}-{id(self)}.lock")
        self.calls = []

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_happy_path(self):
        code = run_once(lambda: self.calls.append(1), self.tmp, True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.calls, [1])

    def test_keysiz_tur_yok_istek_yok(self):
        code = run_once(lambda: self.calls.append(1), self.tmp, False)
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(self.calls, [])           # tarayıcı hiç çağrılmadı
        self.assertFalse(os.path.exists(self.tmp))  # kilit dosyası bile açılmadı

    def test_kilit_mesgul_atla(self):
        holder = open(self.tmp, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)          # rakip tur taklidi
        try:
            code = run_once(lambda: self.calls.append(1), self.tmp, True)
            self.assertEqual(code, EXIT_CONFIG)
            self.assertEqual(self.calls, [])        # eşzamanlı çift tur YOK
        finally:
            holder.close()

    def test_kilit_tur_sonunda_serbest(self):
        run_once(lambda: None, self.tmp, True)
        code = run_once(lambda: self.calls.append(1), self.tmp, True)
        self.assertEqual(code, EXIT_OK)             # kilit sızmadı

    def test_scan_istisnasi_exit1_loglanır(self):
        def boom():
            raise RuntimeError("exchange erişilemedi")
        code = run_once(boom, self.tmp, True)
        self.assertEqual(code, EXIT_SCAN_ERROR)     # cron'u uyandırır, tur ölmez

    def test_scan_istisnasi_kilidi_birakmaz(self):
        def boom():
            raise RuntimeError("x")
        run_once(boom, self.tmp, True)
        self.assertEqual(run_once(lambda: None, self.tmp, True), EXIT_OK)

    def test_modul_kaynagi_emir_fonksiyonu_icermaz(self):
        """Watchdog YALNIZ oku+temizle yetkilisidir: yerleştirme/kapanış API'si
        kaynağında geçmemeli (fail-safe mimari sınır)."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "orphan_watchdog.py")).read()
        for banned in ("place_algo", "new_order", "create_executor",
                       "market_close", "close_reduce_only", "cancel_order("):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
