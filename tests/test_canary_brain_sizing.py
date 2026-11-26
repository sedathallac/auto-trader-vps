"""CANARY AI BRAIN boyutlama testleri (yalnız YENİ davranış — §7).

Kapsam: stop-önce boyutlama matematiği, bütçe tavanları (5/10), kanıt gücü
kesintisi, korelasyon harcırahı, guardrail tavanlar (politika notional +
availableBalance), fail-closed yollar. Kelly TEK BAŞINA kullanılmaz —
test edilen mekanizma risk-bütçe × kanıt × portföy bölüşümüdür.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_brain_sizing import (SizingBrain, SR_STOP_BELOW_COSTS,
                                 SR_NO_RISK_BUDGET, SR_INVALID_INPUT,
                                 DEFAULT_STOP_SIGMA_MULT)
from canary_risk import MAX_POSITION_NOTIONAL, MAX_POSITION_NET_LOSS, \
    MAX_CANARY_NET_LOSS

BRACKETS = [{"bracket": 1, "floor": 0.0, "cap": 50_000.0, "ratio": 0.004,
             "cum": 0.0, "max_leverage": 20}]


class SizingMathTests(unittest.TestCase):

    def test_stop_first_notional(self):
        """notional = risk×conf×portfolio / stop — SABİT notional DEĞİL."""
        sb = SizingBrain(stop_sigma_mult=2.0)
        out = sb.size(price=100.0, sigma_bar=0.02,      # stop = %4
                      roundtrip_cost_pct=1.0,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0,
                      confidence=0.5, held_positions=[],
                      available_balance=None, brackets=None,
                      notional_hard_cap=None)           # guardrail'siz matematik
        self.assertTrue(out["ok"], out["reasons"])
        self.assertAlmostEqual(out["stop_distance_pct"], 4.0)
        # risk = 5 × 0.5 = 2.5 USDT; notional = 2.5/0.04 = 62.5
        self.assertAlmostEqual(out["notional"], 62.5)
        self.assertAlmostEqual(out["qty"], 0.625)
        self.assertAlmostEqual(out["risk_usdt"], 2.5)

    def test_budget_caps_apply(self):
        sb = SizingBrain()
        out = sb.size(price=100.0, sigma_bar=0.01, roundtrip_cost_pct=0.5,
                      position_budget_remaining=3.0,   # kalan pozisyon bütçesi
                      total_budget_remaining=7.0, confidence=1.0,
                      held_positions=[], notional_hard_cap=None)
        # risk = min(3, 7, 5, 10) = 3 → notional = 3/0.02 = 150
        self.assertAlmostEqual(out["notional"], 150.0)

    def test_no_risk_budget_rejects(self):
        sb = SizingBrain()
        out = sb.size(price=100.0, sigma_bar=0.02, roundtrip_cost_pct=1.0,
                      position_budget_remaining=0.0,
                      total_budget_remaining=0.0, confidence=1.0,
                      held_positions=[])
        self.assertFalse(out["ok"])
        self.assertTrue(any(r.startswith(SR_NO_RISK_BUDGET)
                            for r in out["reasons"]))

    def test_stop_below_costs_rejects(self):
        # %0.5 stop ile %1.0 maliyet → mekanik zarar → RED
        sb = SizingBrain(stop_sigma_mult=0.25)          # sigma %2 → stop %0.5
        out = sb.size(price=100.0, sigma_bar=0.02, roundtrip_cost_pct=1.0,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0, confidence=1.0,
                      held_positions=[])
        self.assertFalse(out["ok"])
        self.assertTrue(any(r.startswith(SR_STOP_BELOW_COSTS)
                            for r in out["reasons"]))

    def test_confidence_scales_down_unproven_edge(self):
        """OOS kanıtı zayıf (conf < 1) → risk orantılı küçülür."""
        sb = SizingBrain()
        common = dict(price=100.0, sigma_bar=0.02, roundtrip_cost_pct=1.0,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0, held_positions=[],
                      notional_hard_cap=None)
        o1 = sb.size(confidence=1.0, **common)
        o2 = sb.size(confidence=0.2, **common)
        self.assertAlmostEqual(o2["notional"], o1["notional"] * 0.2)

    def test_correlation_haircut(self):
        """Tutulan pozisyonla korelasyon → etkili risk küçülür; korelasyon
        hesaplanamadıysa KONSERVATİF 1.0 sayılır."""
        sb = SizingBrain()
        common = dict(price=100.0, sigma_bar=0.02, roundtrip_cost_pct=1.0,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0, confidence=1.0,
                      notional_hard_cap=None)
        o_none = sb.size(held_positions=[], **common)
        o_corr = sb.size(held_positions=[(0.5, 5.0)], **common)
        o_unk = sb.size(held_positions=[(None, 5.0)], **common)
        # factor = 1/(1 + |0.5|×5/10) = 1/1.25 = 0.8
        self.assertAlmostEqual(o_corr["notional"],
                               o_none["notional"] * 0.8)
        # bilinmeyen korelasyon → 1.0 → factor = 1/(1+0.5) = 2/3 (daha katı)
        self.assertAlmostEqual(o_unk["notional"],
                               o_none["notional"] * 2 / 3)
        self.assertTrue(any("korelasyon" in w for w in o_unk["warnings"]))

    def test_policy_notional_hard_cap(self):
        """100 USDT canlı politika tavanı guardrail'dir: notional'ı keser,
        etkin risk küçülür (sabit hedef DEĞİL)."""
        sb = SizingBrain()
        out = sb.size(price=100.0, sigma_bar=0.005,   # stop %1 → notional 250
                      roundtrip_cost_pct=0.5,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0, confidence=1.0,
                      held_positions=[], notional_hard_cap=100.0)
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["notional"], 100.0)
        self.assertTrue(any(c.startswith("POLICY_NOTIONAL_CAP")
                            for c in out["capped_by"]))
        self.assertAlmostEqual(out["risk_usdt"], 1.0)   # 100 × %1

    def test_available_balance_cap(self):
        sb = SizingBrain()
        out = sb.size(price=100.0, sigma_bar=0.005, roundtrip_cost_pct=0.5,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0, confidence=1.0,
                      held_positions=[], available_balance=10.0,
                      brackets=BRACKETS, notional_hard_cap=None)
        # IM = notional/20 ≤ 10 → notional ≤ 200 … stop %1 → 250 isteniyordu
        self.assertAlmostEqual(out["notional"], 200.0)
        self.assertTrue(any(c.startswith("AVAILABLE_BALANCE_CAP")
                            for c in out["capped_by"]))

    def test_no_account_data_unverified_warning(self):
        sb = SizingBrain()
        out = sb.size(price=100.0, sigma_bar=0.02, roundtrip_cost_pct=1.0,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0, confidence=1.0,
                      held_positions=[], available_balance=None,
                      brackets=BRACKETS)
        self.assertTrue(out["ok"])
        self.assertTrue(any("UNVERIFIED" in w for w in out["warnings"]))

    def test_invalid_inputs_fail_closed(self):
        sb = SizingBrain()
        for bad in [dict(price=0.0, sigma_bar=0.02),
                    dict(price=100.0, sigma_bar=None),
                    dict(price=100.0, sigma_bar=0.0)]:
            out = sb.size(roundtrip_cost_pct=1.0,
                          position_budget_remaining=5.0,
                          total_budget_remaining=10.0, **bad)
            self.assertFalse(out["ok"])
            self.assertTrue(any(r.startswith(SR_INVALID_INPUT)
                                for r in out["reasons"]))
        out = sb.size(price=100.0, sigma_bar=0.02, roundtrip_cost_pct=None,
                      position_budget_remaining=5.0,
                      total_budget_remaining=10.0)
        self.assertFalse(out["ok"])

    def test_default_mult_positive_and_budgets_sane(self):
        self.assertGreater(DEFAULT_STOP_SIGMA_MULT, 0)
        # operatör bütçeleri tek kaynaktan: 5/10 USDT
        self.assertEqual(MAX_POSITION_NET_LOSS, 5.0)
        self.assertEqual(MAX_CANARY_NET_LOSS, 10.0)
        self.assertEqual(MAX_POSITION_NOTIONAL, 100.0)


if __name__ == "__main__":
    unittest.main()
