"""GÖREV 2026-11-27 (devam) — BinanceAPI HTTP yönlendirme güvenliği.

Kanıtlanması gereken: 3xx yanıtının API key başlığını (X-MBX-APIKEY) veya
imzalı sorguyu (signature=...) başka bir hosta TAŞIMASINA izin verilmez.

Düzeltme: _get/_post tüm istekleri allow_redirects=False ile gönderir;
_api_response 3xx'i HTTP_REDIRECT_REFUSED fail-closed hata sözlüğüne
çevirir (requests varsayılanı redirect TAKİP EDİYORDU — POST 301/302
yeniden GET olarak takip edilebiliyordu).

AĞ ÇAĞRISI YOK: _session kayıtlı sahte ile değiştirilir; 302 sonrası
İKİNCİ istek (yönlendirme hedefi) yapılmadığı kayıt sayısıyla kanıtlanır.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_trader


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.headers = {}
        self._body = body if body is not None else {"ok": True}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeSession:
    """auto_trader._session yerine — çağrıları kaydeder, ağ YOKTUR."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.resp

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.resp

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, kw))
        return self.resp


class BinanceApiRedirectTest(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("BINANCE_FUTURES_BASE_URL",
                                 "LIVE_ALLOW_CUSTOM_BASE_URL", "LIVE_MODE")}
        for k in self._saved:
            os.environ.pop(k, None)
        self.api = auto_trader.BinanceAPI("k", "s")  # mainnet — geçerli

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _patch(self, resp):
        sess = _FakeSession(resp)
        p = mock.patch.object(auto_trader, "_session", sess)
        p.start()
        self.addCleanup(p.stop)
        return sess

    def test_signed_get_redirect_fail_closed_and_not_followed(self):
        """302: imzalı GET takip EDİLMEZ — tek istek (yönlendirme hedefine
        ikinci istek YOK), sonuç fail-closed hata sözlüğü."""
        sess = self._patch(_FakeResp(302))
        out = self.api._get("/fapi/v2/positionRisk", signed=True)
        self.assertEqual(out.get("code"), -1)
        self.assertIn("HTTP_REDIRECT_REFUSED", out["msg"])
        self.assertTrue(out.get("redirect_refused"))
        # KANIT: yalnız İLK istek yapıldı — redirect hedefine istek YOK
        self.assertEqual(len(sess.calls), 1)
        method, url, kw = sess.calls[0]
        self.assertEqual(method, "GET")
        # imzalı istek allowlist hosta gitti (API key + imza başka hosta değil)
        self.assertTrue(url.startswith("https://fapi.binance.com/"), url)
        self.assertIn("signature=", url)
        # requests'e takip YASAĞI açıkça bildirildi
        self.assertIs(kw.get("allow_redirects"), False)
        self.assertEqual(kw["headers"]["X-MBX-APIKEY"], "k")

    def test_unsigned_get_redirect_fail_closed(self):
        sess = self._patch(_FakeResp(301))
        out = self.api._get("/fapi/v1/klines", params={"limit": 5})
        self.assertEqual(out.get("code"), -1)
        self.assertIn("HTTP_REDIRECT_REFUSED http=301", out["msg"])
        self.assertEqual(len(sess.calls), 1)
        self.assertIs(sess.calls[0][2].get("allow_redirects"), False)

    def test_post_redirect_fail_closed_and_not_followed(self):
        """302: imzalı POST da takip edilmez (requests varsayılanı POST
        301/302'yi GET olarak yeniden gönderebiliyordu — imzalı sorgu
        yönlendirme hedefine düşerdi)."""
        sess = self._patch(_FakeResp(302))
        out = self.api._post("/fapi/v1/order", {"symbol": "BTCUSDT"})
        self.assertEqual(out.get("code"), -1)
        self.assertIn("HTTP_REDIRECT_REFUSED", out["msg"])
        self.assertEqual(len(sess.calls), 1)
        method, url, kw = sess.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.startswith("https://fapi.binance.com/"), url)
        self.assertIn("signature=", url)
        self.assertIs(kw.get("allow_redirects"), False)

    def test_delete_redirect_fail_closed(self):
        sess = self._patch(_FakeResp(307))
        out = self.api._post("/fapi/v1/order", {"orderId": 1},
                             method="DELETE")
        self.assertEqual(out.get("code"), -1)
        self.assertIn("HTTP_REDIRECT_REFUSED http=307", out["msg"])
        self.assertEqual(sess.calls[0][0], "DELETE")
        self.assertIs(sess.calls[0][2].get("allow_redirects"), False)

    def test_2xx_success_path_unchanged(self):
        """Normal 2xx davranışı DEĞİŞMEZ — gövde json olarak döner."""
        sess = self._patch(_FakeResp(200, {"serverTime": 123}))
        out = self.api._get("/fapi/v1/time")
        self.assertEqual(out, {"serverTime": 123})
        self.assertEqual(len(sess.calls), 1)

    def test_non_json_body_still_fail_closed(self):
        sess = self._patch(_FakeResp(200, ValueError("no json")))
        out = self.api._get("/fapi/v1/time")
        self.assertEqual(out.get("code"), -1)
        self.assertIn("non-json http 200", out["msg"])


if __name__ == "__main__":
    unittest.main()
