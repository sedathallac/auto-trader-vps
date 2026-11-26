"""BLOK28.1 §11 — LİKİDASYON MODELİ TESTLERİ (mock/fixture; ağ çağrısı YOK).

§6: bakım marjı katmanı (leverageBracket) tabanlı likidasyon hesabı.
Eksik veri → LIQUIDATION_RISK_UNVERIFIED → o sembolde GERÇEK POZİSYON
ENGELLENİR (fail-closed). Tahmini tier ASLA kullanılmaz.

NOT (dürüst kayıt): ortamdaki API anahtarları geçersiz (-2015) olduğundan
GERÇEK leverageBracket verisi alınamadı — bu testler FIXTURE katmanlarıyla
modelin DOĞRULUĞUNU kanıtlar; canlı tier karşılaştırması (compare_reported)
raporda BLOCKED olarak ayrılmıştır.
"""
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0, FIXTURE_BRACKETS_RAW)

BAR = 4 * 3600 * 1000
from canary_liquidation import (LiquidationModel, parse_leverage_brackets,
                                liquidation_price, maintenance_for_notional,
                                liquidation_distance_bp, round_to_tick,
                                LIQUIDATION_RISK_UNVERIFIED,
                                POSITION_MODE_ONEWAY, POSITION_MODE_HEDGE)


class LiquidationModelTests(unittest.TestCase):

    def setUp(self):
        self.brackets = parse_leverage_brackets(FIXTURE_BRACKETS_RAW)

    # ---- bracket ayrıştırma -----------------------------------------------------

    def test_parse_brackets_structure(self):
        self.assertEqual(len(self.brackets), 8)
        for sym, brs in self.brackets.items():
            self.assertEqual(len(brs), 3)
            self.assertEqual([b["bracket"] for b in brs], [1, 2, 3])
            self.assertEqual(brs[0]["floor"], 0.0)
            self.assertEqual(brs[0]["ratio"], 0.004)
            self.assertEqual(brs[1]["cum"], 50.0)
            self.assertEqual(brs[2]["cap"], 1_000_000.0)

    def test_parse_cummulative_alias(self):
        raw = [{"symbol": "ETHUSDT", "brackets": [
            {"bracket": 1, "initialLeverage": 50, "notionalFloor": 0,
             "notionalCap": 50_000, "maintMarginRatio": 0.004,
             "cummulativeMaintAmount": 12.5}]}]
        b = parse_leverage_brackets(raw)["ETHUSDT"]
        self.assertEqual(b[0]["cum"], 12.5)      # eski alan adı da destekli

    def test_maintenance_for_notional_tiers(self):
        brs = self.brackets["ETHUSDT"]
        self.assertEqual(maintenance_for_notional(brs, 1000), (0.004, 0.0))
        self.assertEqual(maintenance_for_notional(brs, 50_000), (0.005, 50.0))
        self.assertEqual(maintenance_for_notional(brs, 250_000), (0.01, 1300.0))
        # cap dışı → None (fail-closed sinyali)
        self.assertIsNone(maintenance_for_notional(brs, 2_000_000))

    # ---- likidasyon fiyatı formülü ------------------------------------------------

    def test_liquidation_price_long_short_known_values(self):
        # long 0.5 @100, izole marj 5 → 90.36144578...
        liq_l = liquidation_price(1, 0.5, 100.0, 5.0, self.brackets["ETHUSDT"])
        self.assertAlmostEqual(liq_l, 90.36144578313252, places=9)
        # short → 109.56175298...
        liq_s = liquidation_price(-1, 0.5, 100.0, 5.0, self.brackets["ETHUSDT"])
        self.assertAlmostEqual(liq_s, 109.56175298804781, places=9)
        # çözüm tutarlılığı: MB == MM (long)
        q, m = 0.5, liq_l
        mb = 5.0 + 1 * q * (m - 100.0)
        mm = q * m * 0.004 - 0.0
        self.assertLess(abs(mb - mm), 1e-9)

    def test_liquidation_price_tier_transition_iterative(self):
        """Likidasyon notional'ı bracket sınırını aşınca iterasyon katman
        değiştirir (600 @100 cross cüzdan 20k → tier1'de yakınsar)."""
        brs = self.brackets["ETHUSDT"]
        liq = liquidation_price(1, 600.0, 100.0, 20_000.0, brs)
        self.assertAlmostEqual(liq, 66.93440428380359, places=6)
        # yakınsadığı katman: notional 600*66.93 ≈ 40160 < 50k → tier 1
        ratio, cum = maintenance_for_notional(brs, 600.0 * liq)
        self.assertEqual(ratio, 0.004)

    def test_cross_mode_wallet_and_other_positions(self):
        brs = self.brackets["ETHUSDT"]
        # cross cüzdan 5 → liq 90.3614; diğer pozisyon -1 uPnL → etkin 4
        base = liquidation_price(1, 0.5, 100.0, 5.0, brs)
        with_other = liquidation_price(1, 0.5, 100.0, 4.0, brs)
        self.assertAlmostEqual(base, 90.36144578313252, places=9)
        # cüzdan azaldı → likidasyon girişe YAKINLAŞTI (long: yukarı)
        self.assertGreater(with_other, base)
        self.assertAlmostEqual(with_other, 92.36947791164658, places=9)

    def test_liquidation_price_fail_closed(self):
        self.assertIsNone(liquidation_price(1, 0.5, 100.0, 5.0, []))
        self.assertIsNone(liquidation_price(1, 0.0, 100.0, 5.0,
                                            self.brackets["ETHUSDT"]))
        # marj pozisyonu kapatamıyor → None
        self.assertIsNone(liquidation_price(1, 0.5, 100.0, 1e9,
                                            self.brackets["ETHUSDT"]))

    def test_mark_change_updates_distance(self):
        d1 = liquidation_distance_bp(90.36, 100.0)
        d2 = liquidation_distance_bp(90.36, 95.0)
        self.assertAlmostEqual(d1, 964.0, places=6)
        self.assertLess(d2, d1)
        self.assertIsNone(liquidation_distance_bp(None, 100.0))

    # ---- §6.2 doğrulama kapısı -----------------------------------------------------

    def _model(self, **kw):
        d = dict(brackets_by_symbol=self.brackets,
                 margin_modes={s: "isolated" for s in self.brackets},
                 leverages={s: 10 for s in self.brackets},
                 position_mode=POSITION_MODE_ONEWAY, wallet_usdt=1000.0)
        d.update(kw)
        return LiquidationModel(**d)

    def test_verify_ok(self):
        ok, reason = self._model().verify("ETHUSDT")
        self.assertTrue(ok)

    def test_verify_missing_bracket(self):
        ok, reason = self._model(brackets_by_symbol={}).verify("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn(LIQUIDATION_RISK_UNVERIFIED, reason)
        self.assertIn("katman", reason)

    def test_verify_hedge_mode_blocked(self):
        ok, reason = self._model(position_mode=POSITION_MODE_HEDGE)\
            .verify("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("Hedge", reason)

    def test_verify_position_mode_unknown(self):
        ok, reason = self._model(position_mode=None).verify("ETHUSDT")
        self.assertFalse(ok)

    def test_verify_missing_leverage(self):
        ok, reason = self._model(leverages={}).verify("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("kaldıraç", reason)

    def test_verify_missing_margin_mode(self):
        ok, reason = self._model(margin_modes={}).verify("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("margin", reason)

    def test_verify_cross_without_wallet(self):
        ok, reason = self._model(wallet_usdt=None,
                                 margin_modes={s: "cross"
                                               for s in self.brackets}
                                 ).verify("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("bakiye", reason)

    # ---- §6.1 değerlendirme + karşılaştırma ------------------------------------------

    def test_evaluate_isolated_full_table(self):
        m = self._model()
        ev = m.evaluate("ETHUSDT", 1, 50.0, 100.0, 100.0)
        self.assertTrue(ev["verified"])
        self.assertEqual(ev["direction"], "LONG")
        self.assertEqual(ev["leverage"], 10)
        self.assertEqual(ev["margin_mode"], "isolated")
        self.assertAlmostEqual(ev["initial_margin"], 5.0, places=6)
        self.assertAlmostEqual(ev["maintenance_margin_ratio"], 0.004)
        self.assertAlmostEqual(ev["maintenance_margin"], 0.2, places=6)
        self.assertAlmostEqual(ev["qty"], 0.5, places=9)
        self.assertAlmostEqual(ev["est_liquidation_px"], 90.36144578313252,
                               places=9)
        self.assertAlmostEqual(ev["liquidation_distance_bp"], 963.855, places=2)

    def test_compare_reported_match_and_mismatch(self):
        m = self._model()
        # borsa raporu 90.36 → 25bp tolerans İÇİ → eşleşme
        r = m.compare_reported("ETHUSDT", 1, 50.0, 100.0, 100.0, 90.36)
        self.assertTrue(r["match"])
        # rapor 80.00 → sapma → eşleşme YOK + açıklama adayları
        r2 = m.compare_reported("ETHUSDT", 1, 50.0, 100.0, 100.0, 80.0)
        self.assertFalse(r2["match"])
        self.assertIn("nedenleri", r2["note"])
        # taraf yoksa → None
        r3 = m.compare_reported("ETHUSDT", 1, 50.0, 100.0, 100.0, None)
        self.assertIsNone(r3["match"])

    def test_round_to_tick_toward_entry(self):
        # long: erken tetik → YUKARI yuvarla; short → AŞAĞI
        self.assertEqual(round_to_tick(90.056, 0.01, True, True), 90.06)
        self.assertEqual(round_to_tick(90.056, 0.01, True, False), 90.05)
        self.assertEqual(round_to_tick(90.056, 0.01, False, True), 90.05)
        self.assertIsNone(round_to_tick(90.056, 0.0, True, True))


class LiquidationGateLiveTests(unittest.TestCase):
    """§6.2: kapı GERÇEK giriş zincirine bağlı — UNVERIFIED → emir YOK."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_entry_blocked_when_model_empty(self):
        lc = make_canary(self.tmp.name, self.ex, verified_liq=False)
        prime(lc)
        out = lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR, self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn(LIQUIDATION_RISK_UNVERIFIED,
                      " ".join(out[0]["blockers"]))
        n = lc.db.execute(
            "SELECT COUNT(*) FROM live_orders").fetchone()[0]
        self.assertEqual(n, 0)

    def test_entry_blocked_when_hedge_mode(self):
        lc = make_canary(self.tmp.name, self.ex)
        # modeli Hedge moda çevir → kapı kapanır
        lc.liq.position_mode = POSITION_MODE_HEDGE
        prime(lc)
        out = lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR, self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn(LIQUIDATION_RISK_UNVERIFIED,
                      " ".join(out[0]["blockers"]))

    def test_entry_allowed_with_verified_model(self):
        lc = make_canary(self.tmp.name, self.ex)
        prime(lc)
        rec = open_one(lc, "ETHUSDT", 1)
        self.assertEqual(rec["result"], "FILLED")

    def test_bracket_disappears_mid_flight_blocks_new_entries(self):
        lc = make_canary(self.tmp.name, self.ex)
        prime(lc)
        rec = open_one(lc, "ETHUSDT", 1)
        self.assertEqual(rec["result"], "FILLED")
        # bracket verisi kayboldu (canlıda okunamaz oldu) → YENİ giriş yok
        lc.liq.brackets = {}
        out = lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR, self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn(LIQUIDATION_RISK_UNVERIFIED,
                      " ".join(out[0]["blockers"]))
        # mevcut pozisyon DOKUNULMADI (yalnız yeni risk kapalı)
        self.assertEqual(len(lc.rm.open_positions()), 1)


if __name__ == "__main__":
    unittest.main()
