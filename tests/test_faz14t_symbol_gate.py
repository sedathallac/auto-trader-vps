"""FAZ14t — canlı sembol kapısı: yasaklı sembollere emir açılmaz.

Kök neden (canlı, 2026-11-16 00:16:57): FAZ9 live yolunda sembol kapısı yoktu ve bot
ENAUSDT SHORT açtı (qty=1664, lev=11, notional=200). Kullanıcı kısıtı: BARD, SUI, ENA,
PEPE, XRP açılmaz. Kapı open_coin'in en başında, hiçbir API çağrısından önce çalışır.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


class _Binance:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _rec(*a, **k):
            self.calls.append(name)
            raise AssertionError(f"kapı sonrası API çağrısı olmamalı: {name}")
        return _rec


class _HB:
    def active_executors(self):
        return []


class TestSymbolGate(unittest.TestCase):
    def test_default_blocklist_covers_user_constraint(self):
        for sym in ("BARDUSDT", "SUIUSDT", "ENAUSDT", "PEPEUSDT", "XRPUSDT"):
            self.assertIn(sym, auto_trader.LIVE_BLOCKED_SYMBOLS)

    def test_blocked_symbol_rejected_before_any_api_call(self):
        b = _Binance()
        coin = {"symbol": "ENAUSDT", "lastPrice": "0.12", "quoteVolume": "9e8",
                "_score": 0.9}
        ok = auto_trader.open_coin(b, _HB(), coin)
        self.assertFalse(ok)
        self.assertEqual(b.calls, [], "kapı API çağrısından ÖNCE dönmeli")

    def test_env_only_extends_never_clears(self):
        old = os.environ.get("LIVE_BLOCKED_SYMBOLS")
        os.environ["LIVE_BLOCKED_SYMBOLS"] = "FOOUSDT"
        try:
            got = auto_trader._blocked_symbols()
        finally:
            if old is None:
                os.environ.pop("LIVE_BLOCKED_SYMBOLS", None)
            else:
                os.environ["LIVE_BLOCKED_SYMBOLS"] = old
        self.assertIn("FOOUSDT", got)
        self.assertIn("ENAUSDT", got, "env varsayılanı silememeli (fail-closed)")

    def test_empty_env_keeps_defaults(self):
        old = os.environ.get("LIVE_BLOCKED_SYMBOLS")
        os.environ["LIVE_BLOCKED_SYMBOLS"] = ""
        try:
            got = auto_trader._blocked_symbols()
        finally:
            if old is None:
                os.environ.pop("LIVE_BLOCKED_SYMBOLS", None)
            else:
                os.environ["LIVE_BLOCKED_SYMBOLS"] = old
        self.assertIn("XRPUSDT", got)


if __name__ == "__main__":
    unittest.main()
