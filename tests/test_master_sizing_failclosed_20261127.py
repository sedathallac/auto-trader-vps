"""GÖREV 2026-11-27 §10.5 — calculate_position_size_v2 canlı-override fail-closed.

Eski davranış (kusur): canlı konfig VARSA ama risk_overrides() çalışamazsa
`except Exception: pass` override'ı yutup paylaşılan RISK_CONFIG'e
(max_position_pct=20, max_leverage=20 — operatör canlı limitlerinden GEVŞEK)
sessizce düşüyordu → fail-open, fazla boyutlu emir riski.

Yeni davranış: canlı modda override alınamazsa (None, 0.0, None) döner —
çağıran sözleşme gereği emir YOK. Compare modunda (canlı konfig yok)
paylaşılan cfg geçerliliğini KORUR (regresyon yok).
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_trader


class _FakeLiveCfg:
    """is_live=True, risk_overrides() davranışı enjekte edilir."""

    is_live = True

    def __init__(self, overrides=None, raise_on_overrides=False):
        self._overrides = overrides or {}
        self._raise = raise_on_overrides

    def risk_overrides(self):
        if self._raise:
            raise RuntimeError("override patladı")
        return dict(self._overrides)


class SizingFailClosedTest(unittest.TestCase):

    def setUp(self):
        self._saved = auto_trader._LIVE_CFG
        auto_trader._LIVE_CFG = None      # her test kendi değerini kurar

    def tearDown(self):
        auto_trader._LIVE_CFG = self._saved

    def test_live_override_failure_returns_none_no_order(self):
        """KANIT (eski kodda fail-open): override hatası → None → emir yok."""
        auto_trader._LIVE_CFG = _FakeLiveCfg(raise_on_overrides=True)
        out = auto_trader.calculate_position_size_v2(1000.0, 0.02, 0.001)
        self.assertIsNone(out[0], "canlı override hatası None dönmelidir")
        self.assertEqual(out[1], 0.0)

    def test_live_overrides_are_applied(self):
        """Geçerli override uygulanır: notional tavana kırpılır."""
        auto_trader._LIVE_CFG = _FakeLiveCfg(
            overrides={"max_notional": 10.0, "min_notional": 5.5,
                       "max_position_pct": 1.0})
        notional, _budget, _sl = auto_trader.calculate_position_size_v2(
            1000.0, 0.02, 0.001)
        self.assertIsNotNone(notional)
        self.assertLessEqual(notional, 10.0 + 1e-9)

    def test_no_live_config_uses_shared_config(self):
        """Compare/legacy: canlı konfig yok → paylaşılan cfg (davranış korunur)."""
        # _live_cfg() bu test ortamında ya None döner ya da is_live=False
        # env'i; her iki yolda da override UYGULANMAZ ve sonuç None DEĞİLDİR.
        auto_trader._LIVE_CFG = None
        os.environ.pop("LIVE_MODE", None)
        notional, _budget, _sl = auto_trader.calculate_position_size_v2(
            1000.0, 0.02, 0.001)
        self.assertIsNotNone(notional,
                             "compare modunda paylaşımlı cfg boyut üretmeli")

    def test_not_live_cfg_no_override(self):
        class _Compare:
            is_live = False

        auto_trader._LIVE_CFG = _Compare()
        notional, _budget, _sl = auto_trader.calculate_position_size_v2(
            1000.0, 0.02, 0.001)
        self.assertIsNotNone(notional)


if __name__ == "__main__":
    unittest.main()
