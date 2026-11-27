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

    def test_live_custom_host_allowed_only_with_operator_optin(self):
        c = live_cfg(BINANCE_FUTURES_BASE_URL="https://example.invalid/",
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


if __name__ == "__main__":
    unittest.main()
