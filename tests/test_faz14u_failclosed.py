"""FAZ14u — run_once fail-closed: doğrulanamayan durumda yeni giriş yok.

Kök neden (canlı, 2026-11-15 22:40:21): HB'ye `RemoteDisconnected` geldiğinde
`active = set()` yapılıp devam ediliyordu; pozisyon limiti "0/5" görünüp yeni emir
açılabiliyordu. Aynı şekilde equity/DD/halt okunamazsa yalnız uyarı basılıp devam
ediliyordu. İkisi de artık fail-closed.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


class _HB:
    def active_executors(self):
        raise ConnectionError("RemoteDisconnected('Remote end closed connection')")


class _HBEmpty:
    def active_executors(self):
        return []


class _Binance:
    def get_balance(self, asset="USDT"):
        return 1000.0

    def position_risk(self):
        return []


class _BinanceBroken:
    def get_balance(self, asset="USDT"):
        raise ConnectionError("RemoteDisconnected")


class TestRunOnceFailClosed(unittest.TestCase):
    def _run(self, hb, binance):
        called = []
        stubs = {
            "HBAPI": lambda *a, **k: hb,
            "BinanceAPI": lambda *a, **k: binance,
            "open_coin": lambda *a, **k: called.append(1) or True,
            "heartbeat_tick": lambda: None,
            "sync_risk_engine": lambda *a, **k: None,
            "reconcile_positions": lambda *a, **k: True,
            "check_stale_data": lambda *a, **k: True,
            "protect_then_gate": lambda *a, **k: (True, {"drift": False, "stale": False,
                                                         "mode": "ONEWAY", "mode_ok": True,
                                                         "unprotected": False}),
            "scan_top": lambda *a, **k: [{"symbol": "SOLUSDT", "lastPrice": "80",
                                          "quoteVolume": "9e8", "priceChangePercent": "3",
                                          "_score": 0.9}],
            "RiskEngine": lambda *a, **k: MagicMock(db_file="/tmp/x.db", is_halted=False,
                                                    state={}, current_drawdown=0.0,
                                                    peak_equity=1000.0),
        }
        saved = {n: getattr(auto_trader, n) for n in stubs}
        for n, v in stubs.items():
            setattr(auto_trader, n, v)
        env = {k: os.environ.get(k) for k in ("BINANCE_KEY", "BINANCE_SECRET",
                                              "HB_USERNAME", "HB_PASSWORD")}
        os.environ.update({"BINANCE_KEY": "k", "BINANCE_SECRET": "s",
                           "HB_USERNAME": "u", "HB_PASSWORD": "p"})
        try:
            auto_trader.run_once(dry_run=False)
        finally:
            for n, v in saved.items():
                setattr(auto_trader, n, v)
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return called

    def test_executor_count_failure_blocks_new_entries(self):
        called = self._run(_HB(), _Binance())
        self.assertEqual(called, [], "executor sayısı bilinmiyorken emir açılmamalı")

    def test_risk_control_failure_blocks_new_entries(self):
        called = self._run(_HBEmpty(), _BinanceBroken())
        self.assertEqual(called, [], "equity/halt doğrulanamıyorken emir açılmamalı")


if __name__ == "__main__":
    unittest.main()
