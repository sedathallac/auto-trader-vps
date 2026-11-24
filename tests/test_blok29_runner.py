"""BLOK29 — RUNNER (deployment tutkalı) TESTLERİ (stub; ağ çağrısı YOK).

canary_runner.py çekirdek strateji/risk kodunu DEĞİŞTİRMEZ; yalnızca
kurulum verilerini okur ve LiveCanary'yi inşa eder. Burada tutkalın
doğruluğu kanıtlanır: T0 kalıcılığı (§7 restart dedup), filtre ayrıştırma,
model kurulumu, eksik veride fail-closed.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import canary_runner as cr
from canary_live import SYMBOLS


class StubExchange:
    """Runner'ın okuduğu uçları sabit veriyle döndürür (ağ YOK)."""

    def exchange_info(self):
        return {"symbols": [
            {"symbol": s, "quantityPrecision": 3, "pricePrecision": 2,
             "filters": [
                 {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001",
                  "minQty": "0.001"},
                 {"filterType": "MIN_NOTIONAL", "notional": "20"},
             ]} for s in SYMBOLS]}

    def leverage_brackets(self):
        return [{"symbol": s, "brackets": [
            {"bracket": 1, "initialLeverage": 50, "notionalFloor": 0,
             "notionalCap": 50_000, "maintMarginRatio": 0.004,
             "cummulativeMaintAmount": 0.0}]} for s in SYMBOLS]

    def position_risk(self):
        return [{"symbol": s, "marginType": "cross", "leverage": "10",
                 "positionSide": "BOTH", "positionAmt": "0"}
                for s in SYMBOLS]

    def position_mode(self):
        return {"dualSidePosition": False}

    def balance(self):
        return [{"asset": "USDT", "balance": "3491.46",
                 "crossWalletBalance": "3491.46"}]


class RunnerTests(unittest.TestCase):

    def test_t0_first_start_then_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "canary_live.db")
            t0_a, first_a = cr._load_t0(db)
            self.assertTrue(first_a)
            self.assertGreater(t0_a, 0)
            # "LiveCanary __init__" tabloları yaratır — burada elle
            import sqlite3
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE IF NOT EXISTS live_state "
                        "(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO live_state VALUES ('t0', ?)",
                        (str(t0_a),))
            con.commit()
            con.close()
            t0_b, first_b = cr._load_t0(db)
            self.assertFalse(first_b)
            self.assertEqual(t0_a, t0_b)     # restart aynı T0'ı görür

    def test_filters_parsed_for_all_symbols(self):
        f = cr._build_filters(StubExchange())
        self.assertEqual(set(f), set(SYMBOLS))
        self.assertEqual(f["ETHUSDT"]["minNotional"], "20")
        self.assertEqual(f["BTCUSDT"]["stepSize"], "0.001")

    def test_liq_model_built_verified_cross_wallet(self):
        liq, info = cr._build_liq_model(StubExchange())
        self.assertTrue(info["one_way"])
        self.assertEqual(info["wallet_usdt"], 3491.46)
        self.assertEqual(info["margin_modes"]["ETHUSDT"], "cross")
        for s in SYMBOLS:
            ok, reason = liq.verify(s)
            self.assertTrue(ok, f"{s}: {reason}")

    def test_liq_model_hedge_fails_closed(self):
        st = StubExchange()
        st.position_mode = lambda: {"dualSidePosition": True}
        liq, info = cr._build_liq_model(st)
        self.assertFalse(info["one_way"])
        ok, _ = liq.verify("ETHUSDT")
        self.assertFalse(ok)               # hedge → UNVERIFIED → giriş yok

    def test_missing_bracket_fails_closed(self):
        st = StubExchange()
        st.leverage_brackets = lambda: []  # veri yok
        liq, _ = cr._build_liq_model(st)
        ok, reason = liq.verify("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("UNVERIFIED", reason)


if __name__ == "__main__":
    unittest.main()
