"""FAZ14s — bekleyen dolum kaydı: süreç başına bir kez yükleme + bayat/ölü kayıt ayıklama.

Kök neden (canlı, 2026-11-16 00:18-00:20): `/root/.pending_fills.json` içinde test
artığı bir kayıt (E1/SOLUSDT, deadline saatler sonrası) vardı. `_load_pending_fills`
HER tick'te yeniden yüklediği için log her tick "♻️ 1 bekleyen dolum diskten geri
yüklendi" + "bekliyor=1" üretiyor, HB'ye boşa sorgu atıyordu. Ayrıca executor HB'de
yoksa err=999 ile "dolmadı" dalına girip sembolü karalisteye yazıyordu.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


class _HB:
    def __init__(self, ex=None):
        self._ex = ex
        self.stopped = []

    def get_executor(self, eid):
        return self._ex

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


class _Binance:
    def position_risk(self):
        return []

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.1"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}


class PendingFillsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "pending.json")
        self._orig_path = auto_trader.PENDING_FILLS_JSON
        self._orig_loaded = list(auto_trader._PENDING_LOADED)
        self._orig_fills = dict(auto_trader._PENDING_FILLS)
        auto_trader.PENDING_FILLS_JSON = auto_trader.Path(self.path)
        auto_trader._PENDING_LOADED[0] = False
        auto_trader._PENDING_FILLS = {}

    def tearDown(self):
        auto_trader.PENDING_FILLS_JSON = self._orig_path
        auto_trader._PENDING_LOADED[0] = self._orig_loaded[0]
        auto_trader._PENDING_FILLS = self._orig_fills

    def _write(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)


class TestLoadOnce(PendingFillsBase):
    def test_second_load_does_not_reload_from_disk(self):
        self._write({"E1": {"symbol": "SOLUSDT", "deadline": time.time() + 300}})
        auto_trader._load_pending_fills()
        self.assertIn("E1", auto_trader._PENDING_FILLS)
        auto_trader._PENDING_FILLS.pop("E1")          # bellekte çözüldü
        auto_trader._load_pending_fills()             # ikinci çağrı diskten YÜKLEMEZ
        self.assertNotIn("E1", auto_trader._PENDING_FILLS)

    def test_stale_entry_pruned_on_load(self):
        self._write({"OLD": {"symbol": "SOLUSDT",
                             "deadline": time.time() - auto_trader.STALE_PENDING_GRACE_S - 60}})
        auto_trader._load_pending_fills()
        self.assertNotIn("OLD", auto_trader._PENDING_FILLS)

    def test_fresh_entry_kept(self):
        self._write({"NEW": {"symbol": "SOLUSDT", "deadline": time.time() + 60}})
        auto_trader._load_pending_fills()
        self.assertIn("NEW", auto_trader._PENDING_FILLS)


class TestDeadExecutorDropped(PendingFillsBase):
    def test_missing_executor_dropped_without_blacklist(self):
        # FAZ15: executor yok + flat + terminal emir kanıtı yok -> pending KORUNUR
        # (eskiden sessizce siliniyordu). Karalisteye yine YAZILMAZ.
        auto_trader._PENDING_FILLS = {"E1": {"symbol": "SOLUSDT", "side": 2,
                                             "amount": 1.0, "sl_pct": 0.01, "tp_pct": 0.02,
                                             "price": 80.0, "deadline": time.time() + 300}}
        calls = []
        orig = auto_trader.add_blacklist
        auto_trader.add_blacklist = lambda *a, **k: calls.append(a)
        try:
            out = auto_trader._process_pending_fills(_Binance(), _HB(None), None, None, None)
        finally:
            auto_trader.add_blacklist = orig
        self.assertEqual(out["waiting"], 1)
        self.assertEqual(calls, [], "ölü executor sembolü karalisteye yazmamalı")
        self.assertIn("E1", auto_trader._PENDING_FILLS)


if __name__ == "__main__":
    unittest.main()
