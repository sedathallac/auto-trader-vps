"""BLOK12 §7 — testnet emir adiminin AYRI operatör onayı kapısı.

Sözleşme: ping+account salt-okunur ve onay istemez; --place-order 0600
işaret dosyası (TESTNET_ORDER_APPROVAL_FILE) olmadan KOŞMAZ; üretim anahtarı
kontaminasyonu her şeyden önce RED; başarı çıktısı "ÜRETİM KANITI DEĞİLDİR"
notu taşır. Ağ YOK (fetch enjekte / run_smoke ikame).
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testnet_smoke as TS                       # noqa: E402
from test_blok11_testnet_smoke import _Fetch     # noqa: E402

ENV_OK = {"TESTNET_API_KEY": "tk", "TESTNET_API_SECRET": "ts"}


def _approval_file(tmp, body="operator-approved", mode=0o600):
    fp = os.path.join(tmp, "tn_approve")
    with open(fp, "w") as fh:
        fh.write(body)
    os.chmod(fp, mode)
    return fp


class TestTestnetOrderGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12tn-")

    def test_onay_dosyasi_yoksa_emir_adimi_kosmaz_fetch_durur(self):
        f = _Fetch()
        with self.assertRaises(TS.SmokeRefused) as cm:
            TS.run_smoke(dict(ENV_OK), place_order=True, fetch=f,
                         sleep=lambda s: None)
        self.assertIn("OPERATÖR-ONAYI-BEKLENIYOR", str(cm.exception))
        joined = " ".join(f.urls)
        self.assertIn("/fapi/v1/account", joined)        # salt-okunur geçti
        self.assertNotIn("order", joined)                # emir POST'u YOK

    def test_gevsek_izin_kirmizi(self):
        env = dict(ENV_OK, TESTNET_ORDER_APPROVAL_FILE=_approval_file(
            self.tmp, mode=0o644))
        with self.assertRaises(TS.SmokeRefused) as cm:
            TS.run_smoke(env, place_order=True, fetch=_Fetch(),
                         sleep=lambda s: None)
        self.assertIn("600", str(cm.exception))

    def test_bos_dosya_kirmizi(self):
        env = dict(ENV_OK, TESTNET_ORDER_APPROVAL_FILE=_approval_file(
            self.tmp, body=""))
        with self.assertRaises(TS.SmokeRefused) as cm:
            TS.run_smoke(env, place_order=True, fetch=_Fetch(),
                         sleep=lambda s: None)
        self.assertIn("boş", str(cm.exception))

    def test_dosya_yok_yolu_kirmizi(self):
        env = dict(ENV_OK,
                   TESTNET_ORDER_APPROVAL_FILE=os.path.join(self.tmp, "yok"))
        with self.assertRaises(TS.SmokeRefused):
            TS.run_smoke(env, place_order=True, fetch=_Fetch(),
                         sleep=lambda s: None)

    def test_onay_varsa_acik_emir_adimlari_kosar(self):
        f = _Fetch()
        env = dict(ENV_OK, TESTNET_ORDER_APPROVAL_FILE=_approval_file(self.tmp))
        steps = TS.run_smoke(env, place_order=True, fetch=f,
                             sleep=lambda s: None)
        self.assertEqual(len(steps), 5)                  # ping→cancel

    def test_uretim_anahtari_onaydan_once_kirmizi(self):
        # Sıra: kontaminasyon kontrolü ONAY kontrolünden önce — onaylı da olsa
        # üretim anahtarıyla tek istek yok.
        f = _Fetch()
        env = dict(ENV_OK, BINANCE_KEY="prod", BINANCE_SECRET="ps",
                   TESTNET_ORDER_APPROVAL_FILE=_approval_file(self.tmp))
        with self.assertRaises(TS.SmokeRefused) as cm:
            TS.run_smoke(env, place_order=True, fetch=f,
                         sleep=lambda s: None)
        self.assertIn("üretim anahtarı", str(cm.exception))
        self.assertEqual(f.urls, [])                     # TEK İSTEK YOK

    def test_readonly_onay_istemez(self):
        f = _Fetch()
        steps = TS.run_smoke(dict(ENV_OK), fetch=f)      # yerleşik onay dosyasız
        self.assertEqual(len(steps), 2)                  # ping+account

    def test_main_basari_çıktisinda_uretim_kaniti_degildir_notu(self):
        env = dict(ENV_OK, TESTNET_ORDER_APPROVAL_FILE=_approval_file(self.tmp))
        out = io.StringIO()
        old_run = TS.run_smoke
        TS.run_smoke = lambda *a, **k: ["ping OK", "account OK"]
        try:
            with contextlib.redirect_stdout(out):
                rc = TS.main(["--symbol", "BTCUSDT"], env=env)
        finally:
            TS.run_smoke = old_run
        self.assertEqual(rc, 0)
        self.assertIn("ÜRETİM KANITI DEĞİLDİR", out.getvalue())

    def test_main_onaysiz_place_order_exit2(self):
        out = io.StringIO()
        f = _Fetch()
        orig = TS.run_smoke
        # fetch enjeksiyonu için incecik sarmal — gerçek run_smoke koşar,
        # tek fark ağ yerine _Fetch görür.
        TS.run_smoke = lambda env, symbol="BTCUSDT", place_order=False, **k: \
            orig(env, symbol=symbol, place_order=place_order, fetch=f,
                 sleep=lambda s: None)
        try:
            with contextlib.redirect_stdout(out):
                rc = TS.main(["--place-order"], env=dict(ENV_OK))
        finally:
            TS.run_smoke = orig
        self.assertEqual(rc, 2)
        self.assertIn("OPERATÖR-ONAYI-BEKLENIYOR", out.getvalue())
        self.assertNotIn("order", " ".join(f.urls))


if __name__ == "__main__":
    unittest.main()
