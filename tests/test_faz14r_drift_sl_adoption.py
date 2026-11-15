"""FAZ14r — drift, SL ile korunan pozisyonu kilitlemez.

Kök neden (canlı, 2026-11-16 00:13): restart sonrası HB executor'ları TERM olmuş,
borsada NEAR/WLD/BNB pozisyonları algo SL ile TAM korunuyordu. `reconcile_positions`
"borsada pozisyon var + HB executor yok" durumunu koşulsuz drift sayıp TÜM yeni
girişleri kilitledi ("DRIFT! Bu döngüde yeni işlem açılmayacak") — pozisyonlar
kapanana kadar bot bir daha emir açamazdı. Artık karar SL durumuna bağlı:
PRESENT -> benimsendi (drift değil), ABSENT/UNKNOWN -> drift (fail-closed).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


def _algo(side="BUY", qty="62.0"):
    return {"symbol": "NEARUSDT", "algoId": 1, "type": "STOP_MARKET",
            "algoStatus": "NEW", "side": side, "positionSide": "BOTH",
            "reduceOnly": "true", "quantity": qty}


class _Binance:
    def __init__(self, algos):
        self._algos = algos

    def position_risk(self):
        return [{"symbol": "NEARUSDT", "positionAmt": "-62.0", "entryPrice": "3.193"}]

    def open_algo_orders_raw(self, symbol):
        return list(self._algos)

    def open_algo_orders_for(self, symbol):
        return list(self._algos)

    def position_mode(self):
        return {"dualSidePosition": False}


class _HB:
    def __init__(self, executors=None):
        self._ex = executors or []

    def active_executors(self):
        return list(self._ex)


class TestDriftAdoptsProtectedPosition(unittest.TestCase):
    def test_protected_position_is_adopted_not_drift(self):
        # SHORT pozisyon -> koruyucu taraf BUY; tam kapsama -> PRESENT
        ok = auto_trader.reconcile_positions(_Binance([_algo()]), _HB())
        self.assertTrue(ok, "SL tam koruyan pozisyon drift sayılmamalı")

    def test_unprotected_position_still_blocks_entries(self):
        ok = auto_trader.reconcile_positions(_Binance([]), _HB())
        self.assertFalse(ok, "SL'siz pozisyon fail-closed kalmalı")

    def test_wrong_side_sl_is_not_protection(self):
        # SHORT pozisyonu SELL SL korumaz -> ABSENT -> drift
        ok = auto_trader.reconcile_positions(_Binance([_algo(side="SELL")]), _HB())
        self.assertFalse(ok)

    def test_partial_quantity_is_not_full_coverage(self):
        ok = auto_trader.reconcile_positions(_Binance([_algo(qty="10.0")]), _HB())
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
