"""GÖREV 2026-11-27 §9.3 — Binance taban adresi URL güvenliği regresyonları.

Fail-closed kurallar (live_guard.LiveConfig.from_env):
  * scheme her modda https olmalı (http'ye kimlik/IMZA düşürme YOK);
  * canlı modda host ALLOWED_FUTURES_HOSTS içinde olmalı;
  * özel host yalnız operatör açık opt-in'iyle (LIVE_ALLOW_CUSTOM_BASE_URL=1);
  * scheme'siz değer reddedilir.

Eski davranış (güvenlik açığı): canlıda BINANCE_FUTURES_BASE_URL her değeri
kabul ediliyordu — imzalı istekler (API key başlıkta) istenilen hosta
gönderilebiliyordu. Bu dosya o davranışın DÖNMEDİĞİNİ kanıtlar.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_guard import (LiveConfig, LiveConfigError, ALLOWED_FUTURES_HOSTS,
                        FUTURES_MAINNET, FUTURES_TESTNET)


def env(**kw):
    base = {"HB_OFFLINE": "0", "LIVE_MODE": "1", "BINANCE_TESTNET": "1",
            "LIVE_STAGE": "1", "LIVE_DRY_RUN": "0", "COMPARE_MODE": "0",
            "LIVE_STRATEGY_FAMILY": "exhaustion",
            "LIVE_WINNING_REGIMES": "range,mid"}
    base.update({k: str(v) for k, v in kw.items()})
    return base


def live_cfg(**kw):
    return LiveConfig.from_env(env(**kw)).validate()


class UrlGuardTest(unittest.TestCase):

    def test_allowlist_both_hosts_pass_in_live_mode(self):
        for host_url in (FUTURES_MAINNET, FUTURES_TESTNET):
            c = live_cfg(BINANCE_FUTURES_BASE_URL=host_url)
            self.assertEqual(c.base_url, host_url)

    def test_live_custom_host_refused(self):
        with self.assertRaises(LiveConfigError) as cm:
            live_cfg(BINANCE_FUTURES_BASE_URL="https://attacker.example/")
        self.assertIn("allowlist", str(cm.exception))

    def test_live_custom_host_refused_even_with_operator_optin(self):
        """GÖREV devamı: LIVE_MODE=1 + LIVE_ALLOW_CUSTOM_BASE_URL=1 birlikteyken
        resmi Binance hostları dışındaki adres KESİNLİKLE reddedilir — gerçek
        canlı imzalı isteklerde opt-in allowlist'i AŞAMAZ."""
        with self.assertRaises(LiveConfigError) as cm:
            live_cfg(BINANCE_FUTURES_BASE_URL="https://example.invalid/",
                     LIVE_ALLOW_CUSTOM_BASE_URL=1)
        self.assertIn("allowlist dışı", str(cm.exception))
        self.assertIn("LIVE_MODE=1", str(cm.exception))
        self.assertIn("AŞAMAZ", str(cm.exception))

    def test_live_optin_cannot_bypass_even_mainnet_lookalike(self):
        """Allowlist benzeri/alt alan adlı host da canlı modda opt-in ile
        GEÇEMEZ (host exact-match)."""
        with self.assertRaises(LiveConfigError):
            live_cfg(BINANCE_FUTURES_BASE_URL=
                     "https://fapi.binance.com.evil.example/",
                     LIVE_ALLOW_CUSTOM_BASE_URL=1)

    def test_nonlive_rig_optin_still_allows_custom_host(self):
        """Opt-in sözleşmesi KORUNUR: canlı OLMAYAN kurulumda (LIVE_MODE=0)
        operatör opt-in'iyle özel adres hâlâ geçerli (yerel test rigı)."""
        c = live_cfg(LIVE_MODE=0,
                     BINANCE_FUTURES_BASE_URL="https://example.invalid/",
                     LIVE_ALLOW_CUSTOM_BASE_URL=1)
        self.assertEqual(c.base_url, "https://example.invalid")

    def test_http_scheme_refused_even_on_allowlisted_host(self):
        with self.assertRaises(LiveConfigError):
            live_cfg(BINANCE_FUTURES_BASE_URL="http://fapi.binance.com")

    def test_schemeless_value_refused(self):
        with self.assertRaises(LiveConfigError):
            live_cfg(BINANCE_FUTURES_BASE_URL="fapi.binance.com")

    def test_compare_mode_https_custom_host_still_allowed(self):
        c = live_cfg(LIVE_MODE=0, COMPARE_MODE=1,
                     BINANCE_FUTURES_BASE_URL="https://example.invalid/")
        self.assertEqual(c.base_url, "https://example.invalid")

    def test_compare_mode_http_refused(self):
        # compare'da bile http'ye düşüş yok: imzalı okuma istekleri aynı
        # istemciden gider.
        with self.assertRaises(LiveConfigError):
            live_cfg(LIVE_MODE=0, COMPARE_MODE=1,
                     BINANCE_FUTURES_BASE_URL="http://127.0.0.1:8000")

    def test_allowlist_constant_is_the_binance_pair(self):
        self.assertEqual(ALLOWED_FUTURES_HOSTS,
                         ("fapi.binance.com", "testnet.binancefuture.com"))


class BinanceApiChokePointTest(unittest.TestCase):
    """BÖLÜM 18 inceleme düzeltmesi: BinanceAPI.__init__ env'den base
    okuduğunda LiveConfig doğrulamasını atlıyordu (orphan_killer/
    archiver yolu) — imzalı istekler doğrulamasız hosta gidebiliyordu.
    Artık doğrulama TEK boğum noktasında (ensure_futures_base_url)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("BINANCE_FUTURES_BASE_URL",
                                 "LIVE_ALLOW_CUSTOM_BASE_URL")}
        for k in self._saved:
            os.environ.pop(k, None)
        import auto_trader
        self.auto_trader = auto_trader

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_env_clean_uses_mainnet(self):
        api = self.auto_trader.BinanceAPI("k", "s")
        self.assertEqual(api.base, self.auto_trader.BINANCE_FAPI)

    def test_env_http_base_refused(self):
        os.environ["BINANCE_FUTURES_BASE_URL"] = "http://fapi.binance.com"
        from live_guard import LiveConfigError
        with self.assertRaises(LiveConfigError):
            self.auto_trader.BinanceAPI("k", "s")

    def test_env_custom_host_refused(self):
        os.environ["BINANCE_FUTURES_BASE_URL"] = "https://evil.example/"
        from live_guard import LiveConfigError
        with self.assertRaises(LiveConfigError):
            self.auto_trader.BinanceAPI("k", "s")

    def test_env_custom_host_allowed_with_operator_optin(self):
        """Opt-in sözleşmesi KORUNUR: LIVE_MODE kapalıyken (canlı olmayan
        test rigı) opt-in özel hosta izin verir."""
        os.environ["BINANCE_FUTURES_BASE_URL"] = "https://example.invalid/"
        os.environ["LIVE_ALLOW_CUSTOM_BASE_URL"] = "1"
        api = self.auto_trader.BinanceAPI("k", "s")
        self.assertEqual(api.base, "https://example.invalid")

    def test_live_mode_optin_cannot_bypass_at_api_init(self):
        """GÖREV devamı: LIVE_MODE=1 + opt-in birlikteyken özel host
        BinanceAPI KURULUMUNDA reddedilir (imzalı istek asla özel hosta
        gitmez)."""
        from live_guard import LiveConfigError
        os.environ["BINANCE_FUTURES_BASE_URL"] = "https://evil.example/"
        os.environ["LIVE_ALLOW_CUSTOM_BASE_URL"] = "1"
        os.environ["LIVE_MODE"] = "1"
        try:
            with self.assertRaises(LiveConfigError) as cm:
                self.auto_trader.BinanceAPI("k", "s")
            self.assertIn("AŞAMAZ", str(cm.exception))
        finally:
            os.environ.pop("LIVE_MODE", None)

    def test_live_mode_optin_cannot_bypass_at_request_time(self):
        """Aynı kombinasyon istek ANINDA da reddedilir: init sonrası base
        değiştirilse bile canlı modda opt-in allowlist'i aşamaz."""
        from live_guard import LiveConfigError
        api = self.auto_trader.BinanceAPI("k", "s")  # mainnet — geçerli
        os.environ["LIVE_ALLOW_CUSTOM_BASE_URL"] = "1"
        os.environ["LIVE_MODE"] = "1"
        try:
            api.base = "https://evil.example"
            with self.assertRaises(LiveConfigError):
                api._get("/fapi/v2/positionRisk", signed=True)
        finally:
            os.environ.pop("LIVE_MODE", None)

    def test_explicit_base_arg_also_validated(self):
        from live_guard import LiveConfigError
        with self.assertRaises(LiveConfigError):
            self.auto_trader.BinanceAPI("k", "s",
                                        base="http://127.0.0.1:9999")

    def test_explicit_allowlisted_base_arg_ok(self):
        api = self.auto_trader.BinanceAPI(
            "k", "s", base="https://testnet.binancefuture.com")
        self.assertEqual(api.base, "https://testnet.binancefuture.com")


if __name__ == "__main__":
    unittest.main()
