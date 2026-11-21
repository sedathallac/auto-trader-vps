"""BLOK12 devam-A: 429/418 gardi GÖNDERİM ANINDA + cooldown'da istek durdurma.

Giriş kapısı (open_coin başı) tek başına yetmez: kapı ile POST arasındaki her
API turu yeni bir 429/418 getirebilir. Sözleşme:
(1) BinanceAPI._get/_post yerel cooldown sürüyorsa İSTEK GÖNDERMEZ — transport
    hiç yoklanır (sentry session patlar), sentetik local_block yanıtı döner;
(2) create_executor çağrısından HEMEN ÖNCE ikinci gard: cooldown bu pencerede
    girdiyse POST YOK, hak iade (teyitli başarısızlık — istek gitmedi), slot
    serbest;
(3) local_block = teyitli başarısızlık: _reservation_should_keep False
    (UNKNOWN DEĞİL — hiçbir şey gönderilmedi).
Ağ YOK (sentry transport + LiveWiringBase).
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_guard                                        # noqa: E402
import auto_trader                                      # noqa: E402
from test_inc13_live_wiring import (FakeBinance, FakeHB,  # noqa: E402
                                    LiveWiringBase, _RiskOK, _coordinator)

_ENV_KEYS = ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
             "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
             "MAX_VALIDATION_TOTAL_NOTIONAL_USDT",
             "VALIDATION_APPROVAL_DB", "OPERATOR_APPROVAL_TOKEN_FILE")


class _Sentry:
    """Buradan TEK bir ağ çağrısı bile geçerse patlar."""

    def __init__(self):
        self.get_calls = 0

    def _boom(self, *a, **k):
        raise AssertionError("AĞA ÇIKILDI — cooldown gardı delindi")

    get = post = delete = _boom


class _Resp:
    status_code = 200
    headers = {}

    def json(self):
        return {"ok": 1}


class _OkSession:
    def get(self, *a, **k):
        return _Resp()

    post = get


class TestTransportLocalCooldown(unittest.TestCase):
    def setUp(self):
        api_guard.reset()
        self.api = auto_trader.BinanceAPI("k", "s")

    def tearDown(self):
        api_guard.reset()

    def test_429_sonuasi_get_gonderilmez(self):
        api_guard.note_http_status(429, {"Retry-After": "60"})
        with mock.patch.object(auto_trader, "_session", _Sentry()):
            r = self.api._get("/fapi/v1/klines", {"symbol": "BTCUSDT"})
        self.assertIsInstance(r, dict)
        self.assertTrue(r.get("local_block"), r)
        self.assertEqual(r.get("code"), -1003)
        self.assertIn("LOCAL-COOLDOWN", r.get("msg", ""))

    def test_429_sonuasi_post_gonderilmez(self):
        api_guard.note_http_status(429, {"Retry-After": "60"})
        with mock.patch.object(auto_trader, "_session", _Sentry()):
            r = self.api._post("/fapi/v1/order", {"symbol": "BTCUSDT"})
        self.assertTrue(r.get("local_block"), r)

    def test_418_sonuasi_post_gonderilmez(self):
        api_guard.note_http_status(418, {})
        with mock.patch.object(auto_trader, "_session", _Sentry()):
            r = self.api._post("/fapi/v1/order", {"symbol": "BTCUSDT"})
        self.assertTrue(r.get("local_block"), r)

    def test_cooldown_sonrasina_kadar_tumu_engelli(self):
        # imzalı/imszasız ayrımı YOK — salt-okunur GET de durur (418 uzatma
        # riski; pozisyonlar borsa-üstü SL ile korunur).
        api_guard.note_http_status(429, {"Retry-After": "60"})
        with mock.patch.object(auto_trader, "_session", _Sentry()):
            for call in (lambda: self.api._get("/fapi/v1/time"),
                         lambda: self.api._get("/fapi/v2/account",
                                               signed=True),
                         lambda: self.api.account_snapshot()):
                r = call()
                if hasattr(r, "raw"):          # AccountSnapshot sarmalı
                    r = r.raw
                self.assertTrue(isinstance(r, dict) and r.get("local_block"),
                                r)

    def test_retry_after_dolunca_istek_gecer(self):
        api_guard.note_http_status(429, {"Retry-After": "0"})
        sess = _OkSession()
        with mock.patch.object(auto_trader, "_session", sess):
            r = self.api._get("/fapi/v1/time")
        self.assertEqual(r, {"ok": 1})         # transport kullanıldı

    def test_local_block_teystli_basarisizlik_release(self):
        # istek HİÇ gitmedi → UNKNOWN değil → slot/bütçe İADE.
        self.assertFalse(auto_trader._reservation_should_keep(
            {"code": -1003, "msg": "LOCAL-COOLDOWN:x", "local_block": True}))


class TestSendTimeGateInOpenCoin(LiveWiringBase):
    """Gardi AÇIKKEN bile cooldown tura SONRADAN girerse create_executor'a
    ulaşmadan durur; hak iade edilir."""

    @contextlib.contextmanager
    def _vb(self):
        old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        old_dir, old_bl = auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE
        saved = (auto_trader._VB_CFG, auto_trader._VB_BUDGET)
        tmp = Path(self.tmp)
        auto_trader.STATE_DIR = tmp
        auto_trader.BLACKLIST_FILE = tmp / ".bl.json"
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["MAX_VALIDATION_TRADES"] = "2"
        os.environ["MAX_VALIDATION_POSITION_NOTIONAL_USDT"] = "50"
        os.environ["MAX_VALIDATION_TOTAL_NOTIONAL_USDT"] = "100"
        os.environ["VALIDATION_APPROVAL_DB"] = str(tmp / "gate.db")
        os.environ.pop("OPERATOR_APPROVAL_TOKEN_FILE", None)
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None
        try:
            yield
        finally:
            auto_trader._VB_CFG, auto_trader._VB_BUDGET = saved
            auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE = old_dir, old_bl
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def setUp(self):
        super().setUp()
        api_guard.reset()

    def tearDown(self):
        api_guard.reset()
        super().tearDown()

    def _open(self, binance, hb, cc):
        return auto_trader.open_coin(binance, hb, dict(self._coin()),
                                     dry_run=False, risk=_RiskOK(),
                                     reserver=None, coordinator=cc)

    def _seed_cooldown_midflight(self):
        """Giriş kapısı AÇIK; onay kapısı anında (POST'un bir önceki son
        adımı) 429 belirir — send-time gardı bunu yakalamalı."""
        orig = auto_trader._operator_gate_check

        def seam():
            api_guard.note_http_status(429, {"Retry-After": "600"})
            return orig()
        return mock.patch.object(auto_trader, "_operator_gate_check", seam)

    def test_tur_icinde_beliren_429_postu_dururur_hak_iade(self):
        with self._vb():
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            with self._seed_cooldown_midflight():
                res = self._open(binance, hb, cc)
            self.assertIs(res, False)
            self.assertEqual(hb.created, [])      # POST YOK
            # reserve OLMUŞTU (trades_used=1); iade geri saymalı:
            self.assertEqual(auto_trader._VB_BUDGET.status()["trades_used"], 0)

    def test_gard_acikken_giris_kosar_negatif_kontrol(self):
        with self._vb():
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            res = self._open(binance, hb, cc)
            self.assertTrue(res)
            self.assertEqual(len(hb.created), 1)  # gard kör red değildir


if __name__ == "__main__":
    unittest.main()
