"""GÖREV 2026-11-27 (hedefli güvenlik düzeltmeleri) — tools/audit_faz9.py
hedef doğrulaması + live_guard yapısal denetim regresyonları.

Kusur A (Binance imzalı istek):
  * mainnet/testnet allowlist PASS; http, bilinmeyen host, kullanıcı
    bilgisi, port, path, query, fragment RED;
  * doğrulama başarısız → AĞ İSTEĞİ YOK (opener/urlopen ÇAĞRILMAZ);
  * imzalı istek yalnız allowlist hosta gider;
  * HTTP 3xx ASLA takip edilmez;
  * hata çıktısında sır/API key/imzalı query YOK.

Kusur B (HB Basic Auth):
  * yalnız loopback host + varsayılan 8000 portu PASS (http/https);
  * uzak host, farklı port, userinfo, path, query, fragment RED;
  * Authorization başlığı YALNIZ doğrulanmış hedef için üretilir;
  * 3xx takip edilmez; Basic Auth içeriği çıktıya YAZILMAZ.

live_guard (§2): ensure_futures_base_url yapısal denetimi (port/userinfo/
path/query/fragment) — allowlist + opt-in + testnet davranışı KORUNUR.

auto_trader (§2 devam): __init__ sonrası self.base değiştirilse bile
istek anında doğrulama imzalı isteği doğrulanmamış adrese GÖNDERMEZ.

AĞ ÇAĞRISI YOK: opener kayıtlı sahte ile değiştirilir; urlopen bombalanır.
"""
import io
import os
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import audit_faz9
from live_guard import LiveConfigError, ensure_futures_base_url

API_KEY = "TESTKEY123"
SECRET = "TESTSECRET456"


class _FakeResp:
    """opener.open() sahte yanıtı — bağlam yöneticisi olarak kullanılabilir."""

    def __init__(self, status=200, body=b"[]"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecorderOpener:
    """_OPENER_NO_REDIRECT yerine geçer: çağrıları kaydeder, ağ YOKTUR."""

    def __init__(self, resp=None, exc=None):
        self.calls = []
        self.resp = resp
        self.exc = exc

    def open(self, req, timeout=None):
        self.calls.append(req)
        if self.exc is not None:
            raise self.exc
        return self.resp


def _header(req, name):
    for k, v in req.header_items():
        if k.lower() == name.lower():
            return v
    return None


class BinanceBaseValidatorTest(unittest.TestCase):
    """Kusur A — validate_binance_base: allowlist + yapısal denetim."""

    def test_mainnet_host_pass(self):
        self.assertIsNone(
            audit_faz9.validate_binance_base("https://fapi.binance.com"))

    def test_testnet_host_pass(self):
        self.assertIsNone(
            audit_faz9.validate_binance_base(
                "https://testnet.binancefuture.com"))

    def test_trailing_slash_pass(self):
        self.assertIsNone(
            audit_faz9.validate_binance_base("https://fapi.binance.com/"))

    def test_http_scheme_refused(self):
        err = audit_faz9.validate_binance_base("http://fapi.binance.com")
        self.assertIsInstance(err, str)

    def test_unknown_host_refused(self):
        for bad in ("https://evil.example",
                    "https://fapi.binance.com.evil.example",
                    "https://binance.com"):
            self.assertIsInstance(
                audit_faz9.validate_binance_base(bad), str, bad)

    def test_username_refused(self):
        self.assertIsInstance(audit_faz9.validate_binance_base(
            "https://user@fapi.binance.com"), str)

    def test_password_refused(self):
        self.assertIsInstance(audit_faz9.validate_binance_base(
            "https://user:pass@fapi.binance.com"), str)

    def test_port_refused(self):
        self.assertIsInstance(audit_faz9.validate_binance_base(
            "https://fapi.binance.com:8443"), str)

    def test_path_refused(self):
        self.assertIsInstance(audit_faz9.validate_binance_base(
            "https://fapi.binance.com/evil"), str)

    def test_query_refused(self):
        self.assertIsInstance(audit_faz9.validate_binance_base(
            "https://fapi.binance.com?x=1"), str)

    def test_fragment_refused(self):
        self.assertIsInstance(audit_faz9.validate_binance_base(
            "https://fapi.binance.com#frag"), str)


class SignedGetTargetTest(unittest.TestCase):
    """Kusur A — mbx_signed_get: doğrulama YOKSA istek YOK; imzalı istek
    yalnız allowlist hosta gider; 3xx takip edilmez; hata çıktısında
    sır/imzalı query yok."""

    def setUp(self):
        self._old = audit_faz9._OPENER_NO_REDIRECT
        self.recorder = _RecorderOpener(resp=_FakeResp(200, b"[]"))
        audit_faz9._OPENER_NO_REDIRECT = self.recorder
        # çift güvence: gerçek urlopen asla çağrılmamalı
        self._urlopen_bomb = mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("urlopen çağrıldı!"))
        self._urlopen_bomb.start()
        self.addCleanup(self._restore)

    def _restore(self):
        audit_faz9._OPENER_NO_REDIRECT = self._old
        self._urlopen_bomb.stop()

    def test_invalid_target_no_network_request(self):
        """Doğrulama başarısız → opener VE urlopen ÇAĞRILMAZ (fail-closed);
        hata mesajında API key/secret/imzalı query YOK."""
        for bad_base in ("http://fapi.binance.com",
                         "https://evil.example",
                         "https://user:pass@fapi.binance.com",
                         "https://fapi.binance.com:8443",
                         "https://fapi.binance.com/evil?x=1#f"):
            data, err = audit_faz9.mbx_signed_get(
                bad_base, "/fapi/v2/positionRisk", API_KEY, SECRET)
            self.assertIsNone(data, bad_base)
            self.assertIn("HEDEF REDDEDİLDİ", err)
            self.assertIn("istek GÖNDERİLMEDİ", err)
            self.assertEqual(self.recorder.calls, [], bad_base)
            # sır sızması yok
            self.assertNotIn(API_KEY, err)
            self.assertNotIn(SECRET, err)
            self.assertNotIn("signature=", err)

    def test_signed_request_only_to_allowlisted_host(self):
        """Geçerli hedef: imzalı URL allowlist hostuna gider (API key +
        imzalı query başka hosta ASLA yazılmaz)."""
        data, err = audit_faz9.mbx_signed_get(
            "https://fapi.binance.com", "/fapi/v2/positionRisk",
            API_KEY, SECRET)
        self.assertIsNone(err)
        self.assertEqual(data, [])
        self.assertEqual(len(self.recorder.calls), 1)
        url = self.recorder.calls[0].full_url
        self.assertTrue(url.startswith("https://fapi.binance.com/"), url)
        self.assertIn("signature=", url)  # imza yalnız bu hosta gitti
        self.assertEqual(_header(self.recorder.calls[0], "X-MBX-APIKEY"),
                         API_KEY)

    def test_redirect_302_not_followed(self):
        """Location'sız 302 yanıtı: takip YOK — hata döner, ikinci istek
        (yönlendirme hedefi) ASLA yapılmaz."""
        self.recorder.resp = _FakeResp(302)
        data, err = audit_faz9.mbx_signed_get(
            "https://fapi.binance.com", "/fapi/v2/positionRisk",
            API_KEY, SECRET)
        self.assertIsNone(data)
        self.assertIn("redirect REDDEDİLDİ", err)
        self.assertEqual(len(self.recorder.calls), 1)  # yalnız ilk istek

    def test_redirect_with_location_refused_by_handler(self):
        """Location'lu 3xx: _RedirectRefused HTTPError yükseltir —
        urlopen'un varsayılan takip davranışı DEVRE DIŞI."""
        req = urllib.request.Request("https://fapi.binance.com/x")
        handler = audit_faz9._RedirectRefused()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            handler.redirect_request(req, None, 301, "Moved", {},
                                     "https://evil.example/steal")
        self.assertEqual(cm.exception.code, 301)

    def test_redirect_http_error_sanitized(self):
        """Handler'dan yükselen HTTPError → kod döner; mesajda URL/imzalı
        query YOK."""
        self.recorder.exc = urllib.error.HTTPError(
            "https://fapi.binance.com/x?signature=abc", 302,
            "redirect takip edilmez", {}, None)
        data, err = audit_faz9.mbx_signed_get(
            "https://fapi.binance.com", "/fapi/v2/positionRisk",
            API_KEY, SECRET)
        self.assertIsNone(data)
        self.assertIn("http=302", err)
        self.assertNotIn("signature=", err)


class HbUrlValidatorTest(unittest.TestCase):
    """Kusur B — validate_hb_url: yalnız loopback + varsayılan port 8000."""

    def test_loopback_http_pass(self):
        self.assertIsNone(audit_faz9.validate_hb_url("http://127.0.0.1:8000"))
        self.assertIsNone(audit_faz9.validate_hb_url("http://localhost:8000"))

    def test_loopback_https_pass(self):
        self.assertIsNone(audit_faz9.validate_hb_url("https://127.0.0.1:8000"))

    def test_ipv6_loopback_pass(self):
        self.assertIsNone(audit_faz9.validate_hb_url("http://[::1]:8000"))

    def test_remote_host_refused(self):
        for bad in ("http://example.com:8000", "http://10.0.0.5:8000",
                    "https://evil.example:8000"):
            self.assertIsInstance(audit_faz9.validate_hb_url(bad), str, bad)

    def test_nonstandard_port_refused(self):
        """Farklı port RAPORLANIR — izin otomatik genişletilmez."""
        self.assertIsInstance(
            audit_faz9.validate_hb_url("http://127.0.0.1:9999"), str)

    def test_default_scheme_port_refused(self):
        """Port yazılmadıysa http→80 / https→443 — 8000 DEĞİL → RED."""
        self.assertIsInstance(
            audit_faz9.validate_hb_url("http://127.0.0.1"), str)
        self.assertIsInstance(
            audit_faz9.validate_hb_url("https://127.0.0.1"), str)

    def test_userinfo_refused(self):
        self.assertIsInstance(audit_faz9.validate_hb_url(
            "http://user:pass@127.0.0.1:8000"), str)

    def test_path_query_fragment_refused(self):
        self.assertIsInstance(audit_faz9.validate_hb_url(
            "http://127.0.0.1:8000/evil"), str)
        self.assertIsInstance(audit_faz9.validate_hb_url(
            "http://127.0.0.1:8000?q=1"), str)
        self.assertIsInstance(audit_faz9.validate_hb_url(
            "http://127.0.0.1:8000#f"), str)


class HbProbeTest(unittest.TestCase):
    """Kusur B — hb_executors_probe: Authorization YALNIZ doğrulanmış
    loopback:8000 hedefi için üretilir; 3xx takip edilmez; Basic Auth
    içeriği çıktıya yazılmaz."""

    def setUp(self):
        self._old = audit_faz9._OPENER_NO_REDIRECT
        self.recorder = _RecorderOpener(
            resp=_FakeResp(200, b'{"data": [{"status": "TERMINATED"}]}'))
        audit_faz9._OPENER_NO_REDIRECT = self.recorder
        self.emitted = []
        self._emit_patch = mock.patch.object(
            audit_faz9, "emit",
            side_effect=lambda s, l, t: self.emitted.append((s, l, t)))
        self._emit_patch.start()
        self.addCleanup(self._restore)

    def _restore(self):
        audit_faz9._OPENER_NO_REDIRECT = self._old
        self._emit_patch.stop()

    def _text(self):
        return " | ".join(t for _, _, t in self.emitted)

    def test_remote_target_rejected_before_auth_header(self):
        """Uzak hedef → Authorization başlığı OLUŞTURULMAZ, istek YOK."""
        audit_faz9.hb_executors_probe("http://example.com:8000", "hbuser",
                                      "hbpass")
        self.assertEqual(self.recorder.calls, [])
        self.assertIn("HEDEF REDDEDİLDİ", self._text())
        self.assertIn("istek GÖNDERİLMEDİ", self._text())
        # Basic Auth içeriği hiçbir çıktıda yok
        self.assertNotIn("hbpass", self._text())
        self.assertNotIn("Basic ", self._text())

    def test_wrong_port_rejected_not_widened(self):
        audit_faz9.hb_executors_probe("http://127.0.0.1:9999", "u", "p")
        self.assertEqual(self.recorder.calls, [])
        self.assertIn("port 9999 izinli değil", self._text())

    def test_loopback_target_sends_basic_auth(self):
        """Doğrulanmış loopback hedef → Authorization: Basic ... üretilir
        VE istek yalnız o hedefe gider."""
        audit_faz9.hb_executors_probe("http://127.0.0.1:8000", "hbuser",
                                      "hbpass")
        self.assertEqual(len(self.recorder.calls), 1)
        req = self.recorder.calls[0]
        self.assertTrue(req.full_url.startswith(
            "http://127.0.0.1:8000/executors/search"))
        auth = _header(req, "Authorization")
        self.assertIsNotNone(auth)
        self.assertTrue(auth.startswith("Basic "))
        self.assertEqual(
            [l for _, l, _ in self.emitted], ["VERIFIED"])

    def test_loopback_redirect_refused(self):
        self.recorder.resp = _FakeResp(302)
        audit_faz9.hb_executors_probe("http://127.0.0.1:8000", "u", "p")
        self.assertEqual(len(self.recorder.calls), 1)  # takip yok
        self.assertIn("redirect REDDEDİLDİ", self._text())
        self.assertEqual([l for _, l, _ in self.emitted], ["UNKNOWN"])


class LiveGuardStructuralTest(unittest.TestCase):
    """§2 — ensure_futures_base_url yapısal denetimi (port/userinfo/path/
    query/fragment). Allowlist + opt-in + testnet sözleşmesi KORUNUR."""

    def test_mainnet_and_testnet_still_pass(self):
        self.assertEqual(ensure_futures_base_url(
            "https://fapi.binance.com"), "https://fapi.binance.com")
        self.assertEqual(ensure_futures_base_url(
            "https://testnet.binancefuture.com"),
            "https://testnet.binancefuture.com")

    def test_port_refused_even_on_allowlisted_host(self):
        with self.assertRaises(LiveConfigError) as cm:
            ensure_futures_base_url("https://fapi.binance.com:8443")
        self.assertIn("port", str(cm.exception))

    def test_userinfo_refused(self):
        with self.assertRaises(LiveConfigError) as cm:
            ensure_futures_base_url("https://key:secret@fapi.binance.com")
        self.assertIn("userinfo", str(cm.exception))
        # kimlik bilgisi mesaja SIZMAZ
        self.assertNotIn("key:secret", str(cm.exception))

    def test_path_query_fragment_refused(self):
        for bad in ("https://fapi.binance.com/evil",
                    "https://fapi.binance.com?q=1",
                    "https://fapi.binance.com#f"):
            with self.assertRaises(LiveConfigError, msg=bad):
                ensure_futures_base_url(bad)

    def test_opt_in_custom_host_still_allowed(self):
        """Operatör opt-in'i (LIVE_ALLOW_CUSTOM_BASE_URL=1) tek istisna —
        yapısal denetim YİNE geçerli (https + portsuz + yalnız host)."""
        env = {"LIVE_ALLOW_CUSTOM_BASE_URL": "1"}
        self.assertEqual(
            ensure_futures_base_url("https://rig.local", env=env),
            "https://rig.local")
        with self.assertRaises(LiveConfigError):
            ensure_futures_base_url("https://rig.local:9443", env=env)
        with self.assertRaises(LiveConfigError):
            ensure_futures_base_url("http://rig.local", env=env)

    def test_compare_mode_relaxed_host_but_strict_structure(self):
        """strict_host=False (compare): host serbest, yapı KATI."""
        self.assertEqual(
            ensure_futures_base_url("https://compare.example",
                                    strict_host=False),
            "https://compare.example")
        with self.assertRaises(LiveConfigError):
            ensure_futures_base_url("https://compare.example:8443",
                                    strict_host=False)


class BinanceApiRequestTimeGuardTest(unittest.TestCase):
    """§2 devam — __init__ sonrası self.base değişirse istek anında
    doğrulama imzalı isteği DURDURUR (farklı base kullanılamaz)."""

    def test_get_validates_base_at_request_time(self):
        import auto_trader
        api = auto_trader.BinanceAPI("k", "s")  # varsayılan mainnet — geçerli
        api.base = "https://evil.example"       # init sonrası değiştirme
        with self.assertRaises(LiveConfigError):
            api._get("/fapi/v2/positionRisk", signed=True)

    def test_post_validates_base_at_request_time(self):
        import auto_trader
        api = auto_trader.BinanceAPI("k", "s")
        api.base = "https://evil.example/path"
        with self.assertRaises(LiveConfigError):
            api._post("/fapi/v1/order", {"symbol": "BTCUSDT"})


if __name__ == "__main__":
    unittest.main()
