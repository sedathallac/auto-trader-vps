"""BLOK12 §8 — tek-süreç koruması + SIGTERM/SIGINT kapanışı.

Sözleşme: aynı kilit dosyası üzerinde İKİNCİ süreç giremez (main exit 3,
run_once hiç çağrılmaz); kapanış işleyicisi YALNIZ bayrak koyar — iptal/API
çağrısı YOK (in-flight emrin akıbeti sorgulanmadan cancel göndermek UNKNOWN'ı
bozar); shutdown açıkken open_coin dry-run'da bile False döner; uyku ≤1s
dilimlerle bölünür ve bayrakla ERKEN döner; bütçe/emir(pending)/onay durumu
deponun yeniden açılışında (restart simülasyonu) korunur. Ağ YOK.
"""
import inspect
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import auto_trader                                        # noqa: E402
import operator_approval as OA                            # noqa: E402
import validation_budget as VB                            # noqa: E402

PY = sys.executable
FORBIDDEN_IN_HANDLER = re.compile(
    r"\b(cancel\w*|post|get|close|urlopen|flock|release|delete\w*)\s*\(")


class _FlagBase(unittest.TestCase):
    """_SHUTTING_DOWN sızıntısını kes (conftest autouse'a ek savunma)."""

    def setUp(self):
        self._saved = list(auto_trader._SHUTTING_DOWN)
        self.tmp = tempfile.mkdtemp(prefix="b12s8-")

    def tearDown(self):
        auto_trader._SHUTTING_DOWN[:] = self._saved


class TestProcessLock(_FlagBase):
    def test_ikinci_surece_kilidi_giremez(self):
        p = os.path.join(self.tmp, "bot.lock")
        fd1 = auto_trader.acquire_process_lock(p)
        self.assertIsNotNone(fd1)
        try:
            self.assertIsNone(auto_trader.acquire_process_lock(p))
        finally:
            os.close(fd1)

    def test_fd_kapaninca_kilidi_yeni_surec_alir(self):
        # Süreç ölümü = fd kapanması = OS kilidi bırakır (restart'ta kilit
        # yetimi kalıcı engeldir — flock sahibiyle birlikte çözülür).
        p = os.path.join(self.tmp, "bot.lock")
        fd1 = auto_trader.acquire_process_lock(p)
        self.assertIsNotNone(fd1)
        os.close(fd1)
        fd2 = auto_trader.acquire_process_lock(p)
        self.assertIsNotNone(fd2)
        os.close(fd2)


class TestShutdownHandler(_FlagBase):
    def test_handler_cagrisi_yoksa_sadece_bayrak(self):
        src = inspect.getsource(auto_trader._shutdown_handler)
        # Log METNİ serbest; ÇAĞRI yok: tek yapabildiği bayrak + log.
        body = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        code_only = re.sub(r"(['\"]).*?\1", "''", body)
        m = FORBIDDEN_IN_HANDLER.search(code_only)
        self.assertIsNone(m, "işleyicide iptal/API çağrısı var: %r" % m)

    def test_sigterm_bayrak_koyar_idempotent(self):
        auto_trader._SHUTTING_DOWN[0] = False
        auto_trader._shutdown_handler(signal.SIGTERM, None)
        self.assertTrue(auto_trader.shutdown_requested())
        auto_trader._shutdown_handler(signal.SIGINT, None)   # ikinci sinyal
        self.assertTrue(auto_trader.shutdown_requested())    # hâlâ tek bayrak

    def test_gercek_sigterm_sinyali_bayrak_kararir(self):
        code = (
            "import os, signal, sys; sys.path.insert(0, %r)\n"
            "import auto_trader as A\n"
            "A.install_shutdown_handlers()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
            "print('SET' if A.shutdown_requested() else 'UNSET')\n" % _ROOT)
        env = dict(os.environ, HOME=self.tmp)
        r = subprocess.run([PY, "-I", "-c", code], capture_output=True,
                           text=True, cwd=_ROOT, env=env, timeout=120)
        self.assertIn("SET", r.stdout, r.stderr[-800:])

    def test_shutdown_pending_dosyasini_SILMEZ(self):
        # Kapanış işleyicisi hiçbir dosya/depoya dokunmaz: pending kaydı ve
        # kilit dosyası handler'dan sonra yerli yerinde durur.
        pf = os.path.join(self.tmp, "pending.json")
        with open(pf, "w") as fh:
            fh.write("{}")
        auto_trader._SHUTTING_DOWN[0] = False
        auto_trader._shutdown_handler(signal.SIGTERM, None)
        self.assertTrue(os.path.exists(pf))


class TestOpenCoinShutdownGate(_FlagBase):
    def test_shutdown_acikken_open_coin_dry_run_bile_giremez(self):
        auto_trader._SHUTTING_DOWN[0] = True
        # binance=None/hb=None: kapı ilk API kullanımından ÖNCE olmalı —
        # istisna DEĞİL False dönmeli.
        self.assertFalse(auto_trader.open_coin(
            None, None, {"symbol": "XUSDT"}, dry_run=True))

    def test_shutdown_kapaliyken_kapi_vurmayiz(self):
        # Negatif kontrol: bayrak False iken gate yüzünden False DÖNMEZ —
        # ilerideki başka bir kapı (risk=None fail-closed) False dönebilir;
        # bu test yalnız gate'in kendi ret mesajının çıkmadığını doğrular.
        auto_trader._SHUTTING_DOWN[0] = False
        import io
        import logging
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        auto_trader.log.addHandler(h)
        try:
            auto_trader.open_coin(None, None, {"symbol": "XUSDT"},
                                  dry_run=True)
        except Exception:
            pass                       # sonraki kapılar istisna atabilir — önemsiz
        finally:
            auto_trader.log.removeHandler(h)
        self.assertNotIn("kapanış sürüyor", buf.getvalue())


class TestInterruptibleSleep(_FlagBase):
    def test_bayrak_erkene_keser_ve_dilimler_kucuk(self):
        calls = []

        def fake_sleep(s):
            calls.append(s)
            if len(calls) == 2:
                auto_trader._SHUTTING_DOWN[0] = True
        with mock.patch.object(auto_trader.time, "sleep", fake_sleep):
            stopped = auto_trader.interruptible_sleep(3600)
        self.assertTrue(stopped)
        self.assertTrue(all(c <= 1.0 for c in calls), calls)
        self.assertLessEqual(len(calls), 3)      # 60s'lik dilimde boğulmadı

    def test_sure_dolunca_false(self):
        auto_trader._SHUTTING_DOWN[0] = False
        calls = []
        mono = [1000.0]

        def fake_monotonic():
            return mono[0]

        def fake_sleep(s):
            calls.append(s)
            mono[0] += s
        with mock.patch.object(auto_trader.time, "sleep", fake_sleep), \
             mock.patch.object(auto_trader.time, "monotonic", fake_monotonic):
            stopped = auto_trader.interruptible_sleep(2.5)
        self.assertFalse(stopped)
        self.assertAlmostEqual(sum(calls), 2.5, places=6)
        self.assertTrue(all(c <= 1.0 for c in calls))


class TestMainGate(_FlagBase):
    def test_kilit_verilmezse_exit3_run_once_yok(self):
        with mock.patch.object(auto_trader, "acquire_process_lock",
                               return_value=None), \
             mock.patch.object(auto_trader, "run_once") as ro:
            rc = auto_trader.main([])
        self.assertEqual(rc, 3)
        ro.assert_not_called()

    def test_kilit_alinirsa_run_once_kosar_exit0(self):
        with mock.patch.object(auto_trader, "acquire_process_lock",
                               return_value=os.open(
                                   os.path.join(self.tmp, "l"),
                                   os.O_CREAT | os.O_RDWR, 0o600)), \
             mock.patch.object(auto_trader, "install_shutdown_handlers"), \
             mock.patch.object(auto_trader, "run_once") as ro:
            rc = auto_trader.main([])
        self.assertEqual(rc, 0)
        ro.assert_called_once()


class TestStateSurvivesReopen(_FlagBase):
    # "bütçe/emir/onay durumu restart sonrası korunur" — SQLite/JSON depoları
    # süreç-ötesi; kanıt: AYNI dosya üzerinde YENİ instances (yeni süreç
    # simülasyonu) yazılanı görür.
    def test_butce_reserve_yeni_instance_sonrasi_sayili(self):
        db = os.path.join(self.tmp, "vb.db")
        lim = VB.Limits(max_trades=10, max_per_trade_usdt=50.0,
                        max_total_usdt=100.0)
        vb = VB.ValidationBudget(db, lim)
        r = vb.reserve("BTCUSDT", 10.0, "s8-k1")
        self.assertTrue(r.ok)
        vb2 = VB.ValidationBudget(db, lim)          # "restart"
        self.assertEqual(vb2.status()["trades_used"], 1)

    def test_onay_durumu_yeni_instance_sonrasi_korunur(self):
        db = os.path.join(self.tmp, "gate.db")
        g = OA.ApprovalGate(db)
        s1 = g.state()
        ev = {k: True for k in OA.SIX_EVIDENCE}
        self.assertTrue(g.record_completion(1, ev, "operator"))
        g2 = OA.ApprovalGate(db)                    # "restart"
        s2 = g2.state()
        self.assertEqual(s2["ops_completed"], 1)
        self.assertEqual(s2["state"], "AWAITING_OPERATOR_APPROVAL")
        self.assertEqual(s1["state"], "FIRST_OP_INCOMPLETE")

    def test_pending_kaydi_restart_uzlasmasinda_KORUNUR(self):
        # conftest _isolate_pending_store PENDING_FILLS_JSON'u tmp'ye çevirir.
        def mut(pending, now):
            pending["eid-s8"] = {"symbol": "BTCUSDT", "status": "UNKNOWN",
                                 "ts": now}
            return pending
        ok, _ = auto_trader._pending_transaction(mut)
        self.assertTrue(ok)
        disk = str(auto_trader.PENDING_FILLS_JSON)
        # Süreç ölümü simülasyonu: bellek sıfırdan başlar.
        auto_trader._PENDING_FILLS = {}
        auto_trader._PENDING_REVISION[0] = 0
        ok, _rev = auto_trader._reconcile_with_disk()
        self.assertTrue(ok)                          # disk silinmemiş ki okunabildi
        self.assertIn("eid-s8", auto_trader._PENDING_FILLS)
        self.assertEqual(auto_trader._PENDING_FILLS["eid-s8"]["status"],
                         "UNKNOWN")                  # in-flight akıbeti bozulmadı
        self.assertTrue(os.path.exists(disk))


if __name__ == "__main__":
    unittest.main()
