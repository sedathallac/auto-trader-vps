"""BLOK10 §5 (F-C) — ATR yoksa CANLI giriş fail-closed (P1, uydurma geometri).

Kök neden (doğrulanmış): auto_trader.py:3456-3459 — calculate_atr None
dönerse (kline fetch hatası / kısa veri) 0.02 UYDURULUYOR ve bu değer
SL/TP geometrisinin (sizing_policy.suggest_sl_tp) ve dolayısıyla pozisyon
BOYUTUNUN girdisi oluyor. Piyasa verisi akmadığı anda bot %2 varsayımla
gerçek emir açabiliyordu: SL mesafesi ve risk bütçesi hayalî.

Onarım: YALNIZ canlı yol (dry_run=False) — ATR None → giriş RED.
dry_run/compare veri toplamaya fallback ile devam eder (BLOK2-9 doktrini:
karar hattı değişir, veri hattı değişmez); ensure_sl_coverage'daki koruyucu
fallback (2234) bilinçli KORUNUR: açık pozisyona hiç SL'siz bırakmak yerine
muhafazakar mesafeyle SL koymanın alternatifi YOK.

Harness notu: FakeBinance'e gerçekçi klines akışı eklendi (test_inc13'teki
mevcut yeşiller ATR fallback'ine GİZLİ bağımlıydı — bu bulgunun kendisi).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_inc13_live_wiring import FakeBinance, FakeHB, LiveWiringBase, _RiskOK, _coordinator


class TestAtrFailClosed(LiveWiringBase):
    def _atr_none(self):
        # fetch hattını taklit etmeyiz: calculate_atr'ın KONTRAT değeri None.
        _saved = auto_trader.calculate_atr
        auto_trader.calculate_atr = lambda *a, **k: None
        self.addCleanup(lambda: setattr(auto_trader, "calculate_atr", _saved))

    def _open(self, binance, hb, cc, dry_run):
        return auto_trader.open_coin(binance, hb, self._coin(), dry_run=dry_run,
                                     risk=_RiskOK(), reserver=None, coordinator=cc)

    # CANLI: ATR yok → emir YOK (create_executor'a asla gelmez).
    def test_live_entry_rejected_when_atr_missing(self):
        self._atr_none()
        binance, hb = FakeBinance(), FakeHB()
        cc, _ = _coordinator(binance, hb, self.db)
        self.assertIs(self._open(binance, hb, cc, dry_run=False), False)
        self.assertEqual(hb.created, [])

    # dry_run: veri hattı DEĞİŞMEZ — fallback ile akış devam eder, yine emir yok.
    def test_dry_run_flow_unchanged(self):
        self._atr_none()
        binance, hb = FakeBinance(), FakeHB()
        cc, _ = _coordinator(binance, hb, self.db)
        self.assertIs(self._open(binance, hb, cc, dry_run=True), True)
        self.assertEqual(hb.created, [])

    # Gerçek ATR akan canlı yol onarım sonrası da açılır (regresyon çapası).
    def test_live_entry_still_works_with_real_atr(self):
        binance, hb = FakeBinance(), FakeHB()
        k = binance._get("/fapi/v1/klines", {})
        self.assertIsInstance(k, list)
        cc, _ = _coordinator(binance, hb, self.db)
        self.assertIs(self._open(binance, hb, cc, dry_run=False), True)
        self.assertEqual(len(hb.created), 1)


if __name__ == "__main__":
    unittest.main()
