"""FAZ14v — slot sayımı borsa gerçeğine dayanır; hayalet executor slot işgal etmez.

Kök neden (canlı, 2026-11-16 00:35:24): borsada 3 pozisyon varken kapı "Maks pozisyon
limiti (5) dolu" deyip yeni girişi kesti, çünkü sayım yalnız HB executor'larına bakıyordu
ve HB'de 5 RUNNING (ama borsada flat) executor vardı.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


class _HB:
    def __init__(self, pairs):
        self._pairs = pairs

    def active_executors(self):
        return [{"trading_pair": p, "status": "RUNNING"} for p in self._pairs]


class _Binance:
    def __init__(self, amts):
        self._amts = amts

    def position_risk(self):
        return [{"symbol": s, "positionAmt": str(a)} for s, a in self._amts.items()]


class _BinanceBroken:
    def position_risk(self):
        raise ConnectionError("RemoteDisconnected")


class TestOccupiedSlots(unittest.TestCase):
    def setUp(self):
        self._orig = dict(auto_trader._PENDING_FILLS)
        auto_trader._PENDING_FILLS = {}

    def tearDown(self):
        auto_trader._PENDING_FILLS = self._orig

    def test_ghost_executors_do_not_occupy_slots(self):
        slots, pairs = auto_trader._occupied_slots(
            _Binance({"NEARUSDT": -62, "WLDUSDT": -751, "BNBUSDT": -0.33}),
            _HB(["NEAR-USDT", "WLD-USDT", "BNB-USDT", "XRP-USDT", "ENA-USDT"]))
        self.assertEqual(slots, {"NEARUSDT", "WLDUSDT", "BNBUSDT"})
        self.assertEqual(len(pairs), 5)

    def test_pending_fill_occupies_slot(self):
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "SOLUSDT"}}
        slots, _ = auto_trader._occupied_slots(_Binance({}), _HB([]))
        self.assertIn("SOLUSDT", slots)

    def test_unreadable_position_risk_is_fail_closed(self):
        slots, pairs = auto_trader._occupied_slots(_BinanceBroken(), _HB([]))
        self.assertIsNone(slots)
        self.assertIsNone(pairs)

    def test_unreadable_hb_is_fail_closed(self):
        class _BadHB:
            def active_executors(self):
                raise ConnectionError("RemoteDisconnected")
        slots, _ = auto_trader._occupied_slots(_Binance({}), _BadHB())
        self.assertIsNone(slots)


if __name__ == "__main__":
    unittest.main()
