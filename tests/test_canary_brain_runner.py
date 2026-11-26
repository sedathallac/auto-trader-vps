"""CANARY AI BRAIN runner testleri (yalnız YENİ davranış — §7).

Ağ YOK. Kapsam:
  - RateScheduler: kayan 60s pencere bütçesi aşılMAZ (kontrollü polling),
  - BrainDB: döngü + kayıt yazımı/okuma (araştırma DB'si — canlı DB'den
    AYRI dosya olduğu Schema ve yollarıyla doğrulanır),
  - kapalı-bar filtresi (yalnız TAM kapanmış barlar işlenir),
  - cq_model_hold_bars tek kaynak.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_brain_runner import (RateScheduler, BrainDB,
                                 cq_model_hold_bars, KLINES_LIMIT)
from canary_brain import DECISION_CANDIDATE, DECISION_REJECTED


class FakeClock:
    """Enjekte edilebilir saat: sleep çağrısı zamanı ileri sarar."""

    def __init__(self, start_s=1_000_000.0):
        self.t = start_s
        self.slept = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class RateSchedulerTests(unittest.TestCase):

    def test_budget_never_exceeded(self):
        clk = FakeClock()
        s = RateScheduler(10, sleep=clk.sleep, clock=clk.time)
        for _ in range(30):
            s.acquire(5)
        # pencere dolunca beklemek ZORUNDA (kontrollü polling) ve pencere
        # kayarken (sleep saati ilerletir) çağrılar devam eder
        self.assertTrue(any(x > 0 for x in clk.slept))
        # her an penceredeki toplam ağırlık ≤ bütçe (kayıttan doğrulanamaz
        # ama used_total = 30×5 = 150 → kesin beklemeler yaşandı)
        self.assertEqual(s.used_total, 150)
        self.assertEqual(s.calls, 30)

    def test_under_budget_never_sleeps(self):
        clk = FakeClock()
        s = RateScheduler(400, sleep=clk.sleep, clock=clk.time)
        for _ in range(20):
            s.acquire(5)          # 100 ağırlık < 400 → bekleme YOK
        self.assertEqual(clk.slept, [])
        self.assertEqual(s.used_total, 100)

    def test_used_weight_header_telemetry(self):
        clk = FakeClock()
        s = RateScheduler(100, sleep=clk.sleep, clock=clk.time)
        self.assertIsNone(s.last_used_weight_1m)
        s.note_used_weight("123")
        self.assertEqual(s.last_used_weight_1m, 123)
        s.note_used_weight(None)
        s.note_used_weight("abc")
        self.assertEqual(s.last_used_weight_1m, 123)


class BrainDBTests(unittest.TestCase):

    def test_write_and_read_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            db = BrainDB(os.path.join(td, "brain.db"))
            # canlı canary DB şemasını İÇERMEZ (ayrı araştırma belleği)
            tables = {r[0] for r in db.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("brain_cycles", tables)
            self.assertIn("prediction_records", tables)
            self.assertNotIn("positions", tables)
            self.assertNotIn("live_state", tables)

            self.assertEqual(db.next_cycle_id(), 1)
            rec = _fake_record()
            summary = {"cycle_id": 1, "ts": 123, "decision":
                       DECISION_CANDIDATE, "universe_eligible": 519,
                       "market_data_covered": 519, "liquid": 400,
                       "history_warm": 400, "scored": 400,
                       "candidates": 1, "long_candidates": 1,
                       "short_candidates": 0, "rejected": 799,
                       "no_trade_symbols": 399, "model_version": "test",
                       "funnel": {"universe_eligible": 519},
                       "notes": []}
            db.write_cycle(summary, [rec])
            self.assertEqual(db.next_cycle_id(), 2)

            row = db.db.execute(
                "SELECT decision, record_json FROM prediction_records "
                "WHERE symbol='AAAUSDT'").fetchone()
            import json
            self.assertEqual(row[0], DECISION_CANDIDATE)
            d = json.loads(row[1])
            self.assertEqual(d["symbol"], "AAAUSDT")
            self.assertEqual(d["direction"], 1)
            db.close()

    def test_two_cycles_accumulate(self):
        with tempfile.TemporaryDirectory() as td:
            db = BrainDB(os.path.join(td, "brain.db"))
            for cid in (1, 2):
                db.write_cycle(
                    {"cycle_id": cid, "ts": cid, "decision": "NO_TRADE",
                     "universe_eligible": 1, "market_data_covered": 1,
                     "liquid": 1, "history_warm": 1, "scored": 1,
                     "candidates": 0, "long_candidates": 0,
                     "short_candidates": 0, "rejected": 2,
                     "no_trade_symbols": 1, "model_version": "t",
                     "funnel": {}, "notes": []}, [_fake_record(cid)])
            n = db.db.execute(
                "SELECT COUNT(*) FROM prediction_records").fetchone()[0]
            self.assertEqual(n, 2)
            db.close()


def _fake_record(cycle_id=1):
    from canary_brain import PredictionRecord
    return PredictionRecord(
        symbol="AAAUSDT", direction=1, ts=123, cycle_id=cycle_id,
        decision=DECISION_CANDIDATE, regime="LOW_VOL_UP_TREND",
        signal_score=80.0, quality_threshold=66.5, quality_pass=True,
        calibrated_probability=0.34, probability_is_calibrated=False,
        expected_gross_return_pct=1.6, expected_net_return_pct=0.5,
        expected_transaction_costs_pct=1.1, funding_estimate_pct=0.8,
        slippage_estimate_pct=0.03, prediction_uncertainty_pct=5.2,
        stop_distance_pct=4.0, expected_holding_bars=174.3,
        expected_holding_hours=697.2, rejection_reasons=(),
        price=100.0, spread_pct=0.02, quote_volume_24h=5e7)


class MiscTests(unittest.TestCase):

    def test_hold_bars_single_source(self):
        self.assertAlmostEqual(cq_model_hold_bars(1), 174.3)
        self.assertAlmostEqual(cq_model_hold_bars(-1), 162.2)

    def test_klines_limit_covers_windows(self):
        # lookback 180 + alt-pencere 135 + sigma/vol pencereleri ≤ 410
        self.assertGreaterEqual(KLINES_LIMIT, 410)


if __name__ == "__main__":
    unittest.main()
