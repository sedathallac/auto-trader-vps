"""BLOK11 §9 — testnet smoke gardları. AĞ YOK (fetch enjekte). Üretim
base'i kaynağın kendisinde geçemez; üretim anahtarı ortamda varsa RED;
testnet anahtarı yoksa RED (blokaj)."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import testnet_smoke as TS  # noqa: E402

ENV_OK = {"TESTNET_API_KEY": "tk", "TESTNET_API_SECRET": "ts"}


class _Fetch:
    def __init__(self, account=None):
        self.urls = []
        self.account = account if account is not None \
            else {"totalWalletBalance": "1000"}

    def __call__(self, req):
        if isinstance(req, str):
            url = req
        elif isinstance(req, dict):
            url = req.get("url")
        else:
            url = req.full_url
            if getattr(req, "data", None):      # POST/DELETE gövdesi de görünsün
                url += " BODY:" + req.data.decode()
        self.urls.append(url)
        if "/fapi/v1/ping" in url:
            return {}
        if "/fapi/v1/account" in url:
            return self.account
        if "/ticker/price" in url:
            return {"price": "100000.0"}
        if "order" in url:
            return {"orderId": 42, "status": "NEW"}
        return {}

    calls = None


class TestSmoke(unittest.TestCase):
    def test_01_uretim_anahtari_varsa_red(self):
        for extra in ({"BINANCE" + "_" + "KEY": "pk"},
                      {"BINANCE" + "_" + "SECRET": "ps"}):
            env = dict(ENV_OK, **extra)
            with self.assertRaises(TS.SmokeRefused):
                TS.precheck(env)

    def test_02_testnet_anahtar_yoksa_red_blokaj(self):
        for env in ({}, {"TESTNET_API_KEY": "tk"}):
            with self.assertRaises(TS.SmokeRefused) as cm:
                TS.precheck(env)
            self.assertIn("TESTNET", str(cm.exception))

    def test_03_base_kodda_testnet_sabit(self):
        self.assertTrue(TS.TESTNET_BASE.startswith(
            "https://testnet.binancefuture.com"))
        src = open(os.path.join(_ROOT, "tools", "testnet_smoke.py"),
                   encoding="utf-8").read()
        for prod in ("fapi.binance.com", "api.binance.com",
                     "fstream.binance.com"):
            self.assertNotIn(prod, src)   # üretim hostu kaynakta GEÇEMEZ

    def test_04_readonly_akis_yalniz_testnet(self):
        f = _Fetch()
        steps = TS.run_smoke(ENV_OK, fetch=f)
        self.assertEqual(len(steps), 2)         # ping + account
        for u in f.urls:
            self.assertTrue(u.startswith(TS.TESTNET_BASE), u)
            if "/fapi/v1/ping" not in u:        # ping imzasız (public)
                self.assertIn("signature=", u)  # gerisi imzalı
            self.assertNotIn("BINANCE", u.split("?")[0])

    def test_05_bozuk_hesap_yaniti_red(self):
        with self.assertRaises(TS.SmokeRefused):
            TS.run_smoke(ENV_OK, fetch=_Fetch(account={"code": -2015}))

    def test_06_place_order_akisi(self):
        f = _Fetch()
        env = dict(ENV_OK)
        # BLOK12 §7: emir adımı AYRI kalıcı operatör onayı ister — test 0600
        # işaret dosyası kurar (onay akışının kendisi
        # tests/test_blok12_testnet_gate.py'de).
        import tempfile
        fd, fp = tempfile.mkstemp()
        os.write(fd, b"operator-approved")
        os.close(fd)
        os.chmod(fp, 0o600)
        self.addCleanup(os.remove, fp)
        env["TESTNET_ORDER_APPROVAL_FILE"] = fp
        steps = TS.run_smoke(env, symbol="BTCUSDT", place_order=True,
                             fetch=f, sleep=lambda s: None)
        # ping/account/limit/query/cancel = 5 adım
        self.assertEqual(len(steps), 5)
        joined = " ".join(f.urls)
        self.assertIn("newClientOrderId=fz9sBTCUSDT", joined)
        self.assertIn("price=95000", joined)     # %5 uzak limit
        self.assertTrue(all(u.startswith(TS.TESTNET_BASE) for u in f.urls))

    def test_07_main_blokaj_exit2(self):
        rc = TS.main([], env={})                 # ağzına kadar sahte ortam
        self.assertEqual(rc, 2)                  # RED — gerçek istek yok


if __name__ == "__main__":
    unittest.main()
