"""CANARY AI BRAIN çekirdek testleri (yalnız YENİ davranış — §7).

Ağ YOK; bütün veri fixture. Kapsam:
  - maliyet modeli birimleri (yüzde-nokta) ve fail-closed,
  - piyasa kapıları: veri yok/eski/likidite/spread → RED (sıfır varsayılmaz),
  - tarayıcı kararları: CANDIDATE / REJECTED nedenleri / NO_TRADE,
  - iki yönün BAĞIMSIZ değerlendirmesi (ters yön yapısal ret),
  - kalibrasyon dürüstlüğü: probability_is_calibrated=False, alan etiketi,
  - PredictionRecord to_dict gidiş-dönüşü.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract_discovery import discover_contracts
from canary_brain import (BrainScanner, PredictionRecord, gate_market_data,
                          eligible_symbols, cycle_decision, regime_label,
                          roundtrip_cost_pct, slippage_estimate_pct,
                          calibration_confidence, calibration_uncertainty_pct,
                          DECISION_CANDIDATE, DECISION_NO_TRADE,
                          DECISION_REJECTED, BR_ILLIQUID_24H,
                          BR_SPREAD_TOO_WIDE, BR_STALE_MARKET_DATA,
                          BR_INSUFFICIENT_HISTORY, BR_NEUTRAL_MOMENTUM,
                          BR_QUALITY_BELOW_THRESHOLD,
                          BR_COSTS_EXCEED_EXPECTED_EDGE, TAKER_FEE_PCT,
                          SLIPPAGE_FLOOR_PCT, FUNDING_FLOOR_PER_BAR,
                          MODEL_VERSION, CALIBRATION_DOMAIN)

NOW = 1_792_000_000_000
TICKER_OK = {"symbol": "AAAUSDT", "lastPrice": "100.0",
             "quoteVolume": "50000000"}
BOOK_OK = {"symbol": "AAAUSDT", "bidPrice": "99.99", "askPrice": "100.01"}
PREMIUM_OK = {"symbol": "AAAUSDT", "markPrice": "100.0",
              "lastFundingRate": "0.0001", "time": NOW - 5_000}


def _gate(**kw):
    d = dict(symbol="AAAUSDT", ticker=TICKER_OK, book=BOOK_OK,
             premium=PREMIUM_OK, now_ms=NOW, max_age_ms=120_000,
             min_quote_volume_24h=5_000_000, max_spread_pct=0.10)
    d.update(kw)
    return gate_market_data(**d)


def _quality(score, theta, qpass, direction=1, mom_t=2.5, trend=1.0):
    return {"symbol": "AAAUSDT", "direction": direction,
            "quality_score": score, "quality_threshold": theta,
            "quality_pass": qpass, "momentum_strength": mom_t,
            "trend_confirmation": trend, "volatility_context": 0.8,
            "volume_confirmation": 1.5, "signal_reason": "test"}


class CostModelTests(unittest.TestCase):

    def test_units_are_percent_points(self):
        # spread %0.02, funding %0.01/8h, 100 bar
        c = roundtrip_cost_pct(0.02, 0.0001, 100.0)
        # komisyon 2×0.05 + slippage 2×0.03 + funding 100×0.01×100/2=0.5
        self.assertAlmostEqual(c, 2 * (0.05 + 0.03) + 0.5, places=6)

    def test_slippage_floor_beats_tight_spread(self):
        self.assertAlmostEqual(slippage_estimate_pct(0.001),
                               SLIPPAGE_FLOOR_PCT)
        self.assertAlmostEqual(slippage_estimate_pct(0.20), 0.10)

    def test_missing_inputs_fail_closed(self):
        self.assertIsNone(roundtrip_cost_pct(None, 0.0001, 100))
        self.assertIsNone(roundtrip_cost_pct(0.02, None, 100))
        self.assertIsNone(roundtrip_cost_pct(0.02, 0.0001, 0))
        self.assertIsNone(slippage_estimate_pct(None))

    def test_funding_always_counts_as_cost(self):
        # negatif funding (short lehine) bile maliyet sayılır (konservatif)
        c1 = roundtrip_cost_pct(0.02, 0.0002, 10)
        c2 = roundtrip_cost_pct(0.02, -0.0002, 10)
        self.assertAlmostEqual(c1, c2)

    def test_calibration_confidence_below_one(self):
        # CI sıfırı içeriyor → kanıt gücü < 1 (OOS kanıtsız sinyale yüksek
        # sermaye yapısal olarak engelli)
        for d in (1, -1):
            self.assertLess(calibration_confidence(d), 1.0)
            self.assertGreater(calibration_confidence(d), 0.0)
            self.assertGreater(calibration_uncertainty_pct(d), 0.0)


class GateTests(unittest.TestCase):

    def test_pass(self):
        g = _gate()
        self.assertTrue(g.eligible)
        self.assertAlmostEqual(g.spread_pct, 0.02, places=6)  # %-nokta
        self.assertEqual(g.quote_volume_24h, 50_000_000.0)

    def test_missing_component_blocks_not_zero_assumed(self):
        for missing in ("ticker", "book", "premium"):
            kw = {missing: None}
            g = _gate(**kw)
            self.assertFalse(g.eligible)
            self.assertTrue(g.reasons[0].startswith(BR_STALE_MARKET_DATA))

    def test_stale_blocks(self):
        g = _gate(premium=dict(PREMIUM_OK, time=NOW - 1_000_000))
        self.assertFalse(g.eligible)

    def test_illiquid_blocks(self):
        g = _gate(ticker=dict(TICKER_OK, quoteVolume="100"))
        self.assertFalse(g.eligible)
        self.assertTrue(any(r.startswith(BR_ILLIQUID_24H)
                            for r in g.reasons))

    def test_wide_spread_blocks(self):
        g = _gate(book={"symbol": "AAAUSDT", "bidPrice": "99.0",
                        "askPrice": "101.0"})     # %2 spread
        self.assertFalse(g.eligible)
        self.assertTrue(any(r.startswith(BR_SPREAD_TOO_WIDE)
                            for r in g.reasons))

    def test_corrupt_fields_block(self):
        g = _gate(book={"symbol": "AAAUSDT", "bidPrice": "x",
                        "askPrice": "1"})
        self.assertFalse(g.eligible)


class ScannerTests(unittest.TestCase):

    def _scan(self, direction=1, quality=None, warm=True, gate=None,
              momentum=0.05, sigma=0.02, sizing=None):
        s = BrainScanner()
        return s.evaluate("AAAUSDT", direction, cycle_id=1, now_ms=NOW,
                          gate=gate or _gate(), quality=quality, warm=warm,
                          sigma_bar=sigma, momentum=momentum, sizing=sizing)

    def test_candidate_path(self):
        q = _quality(80.0, 66.5, True)
        r = self._scan(quality=q)
        self.assertEqual(r.decision, DECISION_CANDIDATE)
        self.assertEqual(r.rejection_reasons, ())
        # net beklenti = brüt − maliyet > 0 (LONG brüt 1.604)
        self.assertAlmostEqual(r.expected_gross_return_pct, 1.604, places=3)
        self.assertGreater(r.expected_net_return_pct, 0)
        self.assertFalse(r.probability_is_calibrated)
        self.assertEqual(r.calibration_domain, CALIBRATION_DOMAIN)
        self.assertEqual(r.model_version, MODEL_VERSION)

    def test_costs_exceed_edge_blocks_short(self):
        # SHORT brüt 0.989; yüksek funding (%0.02/8h) maliyeti ~1.78'e
        # çıkarır → net ≤ 0 → RED (maliyet hesaba katılmadan pozitif sinyal
        # ilan EDİLMEZ)
        g = _gate(premium=dict(PREMIUM_OK, lastFundingRate="0.0002"))
        q = _quality(80.0, 66.0, True, direction=-1)
        r = self._scan(direction=-1, quality=q, momentum=-0.05, gate=g)
        self.assertEqual(r.decision, DECISION_REJECTED)
        self.assertTrue(any(x.startswith(BR_COSTS_EXCEED_EXPECTED_EDGE)
                            for x in r.rejection_reasons))
        self.assertLessEqual(r.expected_net_return_pct, 0)

    def test_quality_below_threshold(self):
        q = _quality(50.0, 66.5, False)
        r = self._scan(quality=q)
        self.assertEqual(r.decision, DECISION_REJECTED)
        self.assertTrue(any(x.startswith(BR_QUALITY_BELOW_THRESHOLD)
                            for x in r.rejection_reasons))

    def test_opposite_direction_structural_reject(self):
        # momentum +yönlü iken SHORT skoru align=0 yüzünden geçemez
        q = _quality(40.0, 66.0, False, direction=-1)
        r = self._scan(direction=-1, quality=q, momentum=0.05)
        self.assertEqual(r.decision, DECISION_REJECTED)
        self.assertTrue(any("momentum_opposite" in x
                            for x in r.rejection_reasons))

    def test_not_warm_blocks(self):
        r = self._scan(quality=None, warm=False, momentum=None, sigma=None)
        self.assertEqual(r.decision, DECISION_REJECTED)
        self.assertIn(BR_INSUFFICIENT_HISTORY, r.rejection_reasons)

    def test_neutral_momentum_visible(self):
        r = self._scan(quality=None, warm=True, momentum=0)
        self.assertIn(BR_NEUTRAL_MOMENTUM, r.rejection_reasons)

    def test_gate_failure_propagates(self):
        g = _gate(ticker=dict(TICKER_OK, quoteVolume="10"))
        r = self._scan(gate=g, quality=_quality(80.0, 66.5, True))
        self.assertEqual(r.decision, DECISION_REJECTED)
        self.assertTrue(any(x.startswith(BR_ILLIQUID_24H)
                            for x in r.rejection_reasons))

    def test_both_directions_independent(self):
        # aynı sembol: LONG aday, SHORT maliyet/align reddi — bağımsız kayıt
        rlong = self._scan(1, quality=_quality(80.0, 66.5, True))
        rshort = self._scan(-1, quality=_quality(30.0, 66.0, False,
                                                 direction=-1),
                            momentum=0.05)
        self.assertEqual(rlong.decision, DECISION_CANDIDATE)
        self.assertEqual(rshort.decision, DECISION_REJECTED)

    def test_record_to_dict_roundtrip(self):
        q = _quality(80.0, 66.5, True)
        r = self._scan(quality=q, sizing={"ok": True, "notional": 42.0})
        d = r.to_dict()
        self.assertEqual(d["symbol"], "AAAUSDT")
        self.assertEqual(d["direction"], 1)
        self.assertEqual(d["sizing"]["notional"], 42.0)
        self.assertIn("rejection_reasons", d)
        import json
        json.dumps(d)      # serileştirilebilir (DB yazımı)

    def test_regime_labels(self):
        self.assertEqual(regime_label(True, 0.5), "LOW_VOL_UP_TREND")
        self.assertEqual(regime_label(False, 1.5), "HIGH_VOL_DOWN_TREND")
        self.assertEqual(regime_label(None, None), "VOL_UNKNOWN_?_TREND")

    def test_cycle_decision_no_trade(self):
        q = _quality(50.0, 66.5, False)
        recs = [self._scan(quality=q)]
        self.assertEqual(cycle_decision(recs), DECISION_NO_TRADE)
        recs.append(self._scan(quality=_quality(80.0, 66.5, True)))
        self.assertEqual(cycle_decision(recs), DECISION_CANDIDATE)


class UniverseTests(unittest.TestCase):

    def test_eligible_symbols_from_discovery(self):
        fapi = {"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType":
             "PERPETUAL", "baseAsset": "BTC", "quoteAsset": "USDT",
             "marginAsset": "USDT", "underlyingType": "COIN"},
            {"symbol": "ETHUSDC", "status": "TRADING", "contractType":
             "PERPETUAL", "baseAsset": "ETH", "quoteAsset": "USDC",
             "marginAsset": "USDC", "underlyingType": "COIN"},
            {"symbol": "XRPquarter", "status": "TRADING", "contractType":
             "CURRENT_QUARTER", "baseAsset": "XRP", "quoteAsset": "USDT",
             "marginAsset": "USDT", "underlyingType": "COIN"},
            {"symbol": "DEADUSDT", "status": "SETTLING", "contractType":
             "PERPETUAL", "baseAsset": "DEAD", "quoteAsset": "USDT",
             "marginAsset": "USDT", "underlyingType": "COIN"},
        ]}
        res = discover_contracts(fapi_info=fapi, dapi_info=None)
        elig = eligible_symbols(res)
        self.assertEqual(elig, ["BTCUSDT"])


if __name__ == "__main__":
    unittest.main()
