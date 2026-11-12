"""Increment 6 — K2: exchange flat confirmation before a close is recorded.

is_flat_confirmed is the K2 decision (unit-tested, all 8 required cases). A
focused sync_risk_engine test binds to the real function and proves the gate:
a TERMINATED executor whose symbol is NOT confirmed flat is NOT recorded.
"""
import os
import sys
import sqlite3
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at


class TestIsFlatConfirmed(unittest.TestCase):
    def test_zero_amount_is_flat(self):
        self.assertTrue(at.is_flat_confirmed([{"symbol": "SOLUSDT", "positionAmt": "0"}], "SOLUSDT"))

    def test_nonzero_amount_open(self):
        self.assertFalse(at.is_flat_confirmed([{"symbol": "SOLUSDT", "positionAmt": "-0.3"}], "SOLUSDT"))

    def test_partial_close_nonzero_not_flat(self):
        self.assertFalse(at.is_flat_confirmed([{"symbol": "SOLUSDT", "positionAmt": "0.1"}], "SOLUSDT"))

    def test_api_error_dict_not_flat(self):
        self.assertFalse(at.is_flat_confirmed({"code": -1130, "msg": "err"}, "SOLUSDT"))

    def test_none_timeout_not_flat(self):
        self.assertFalse(at.is_flat_confirmed(None, "SOLUSDT"))

    def test_missing_symbol_not_flat(self):
        self.assertFalse(at.is_flat_confirmed([{"symbol": "BTCUSDT", "positionAmt": "0"}], "SOLUSDT"))

    def test_mismatched_symbol_not_flat(self):
        self.assertFalse(at.is_flat_confirmed([{"symbol": "SOL", "positionAmt": "0"}], "SOLUSDT"))

    def test_unparseable_amount_not_flat(self):
        self.assertFalse(at.is_flat_confirmed([{"symbol": "SOLUSDT", "positionAmt": "x"}], "SOLUSDT"))


class _FakeBinance:
    def __init__(self, positions):
        self._positions = positions
    def position_risk(self):
        return self._positions


class _FakeHB:
    auth = ("u", "p")


class _FakeRisk:
    def __init__(self):
        self.recorded = []
    def record_trade(self, eid, pair, pnl, iso):
        self.recorded.append((eid, pair, pnl))
        return True, "ok"


class _FakeResp:
    def __init__(self, data): self._data = data
    def json(self): return self._data


class TestSyncRiskEngineK2Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # minimal risk_ledger.db used by sync_risk_engine
        db = os.path.join(self.tmp, "risk_ledger.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sync_meta (id INTEGER PRIMARY KEY, last_close_ts REAL, updated_at TEXT)")
        conn.execute("INSERT INTO sync_meta (id, last_close_ts) VALUES (1, 0)")
        conn.execute("CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY)")
        conn.commit(); conn.close()
        self._orig_home = at.Path.home
        at.Path.home = staticmethod(lambda: Path(self.tmp))
        self._orig_post = at.requests.post
        term = {"data": [{"executor_id": "E1", "status": "TERMINATED",
                          "close_timestamp": 9999999999, "net_pnl_quote": -0.27,
                          "trading_pair": "SOL-USDT"}]}
        at.requests.post = lambda *a, **k: _FakeResp(term)

    def tearDown(self):
        at.Path.home = self._orig_home
        at.requests.post = self._orig_post
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_flat_does_not_record(self):
        risk = _FakeRisk()
        binance = _FakeBinance([{"symbol": "SOLUSDT", "positionAmt": "-0.3"}])  # NOT flat
        at.sync_risk_engine(risk, _FakeHB(), binance=binance)
        self.assertEqual(risk.recorded, [], "recorded a close while exchange not flat")

    def test_api_error_does_not_record(self):
        risk = _FakeRisk()
        binance = _FakeBinance({"code": -1, "msg": "positionRisk down"})
        at.sync_risk_engine(risk, _FakeHB(), binance=binance)
        self.assertEqual(risk.recorded, [], "recorded a close while positionRisk errored")

    def test_flat_records(self):
        risk = _FakeRisk()
        binance = _FakeBinance([{"symbol": "SOLUSDT", "positionAmt": "0"}])  # flat
        at.sync_risk_engine(risk, _FakeHB(), binance=binance)
        self.assertEqual(len(risk.recorded), 1)
        self.assertEqual(risk.recorded[0][1], "SOL-USDT")


if __name__ == "__main__":
    unittest.main()
