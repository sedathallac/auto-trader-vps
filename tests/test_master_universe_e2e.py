"""GÖREV 2026-11-27 (BÖLÜM 3.8) — NEWUSDT UÇTAN UCA KABUL TESTİ.

NEWUSDT: eski 8 sembollük listede OLMAYAN, eligible kurgusal sözleşme.
Zincirin TAMAMI sahte/deterministik ortamda çalışır (ağ YOK, gerçek
Binance emri YOK — FakeExchange bellek içi):

  exchangeInfo → contract discovery → eligible universe → bulk market
  data → liquidity gate → LONG/SHORT scoring (BrainScanner) →
  PredictionRecord → BRAIN_DB → BrainDecision → execution adapter →
  try_entry → CROSS account gate → order preflight → mock order submit

Ayrıca 2026-11-27 düzeltmelerinin regresyon testleri:
  C1) yabancı açık emir (C281 öneksiz) giriş kilidi (§3.6)
  C2) _position_risk_map süzgesiz + mark güncellemesi evren ∪ pozisyon
      (§3.3 — evren daralması altında çıkış izlemesi)
  C3) BRAIN_EXIT_POLICY_UNDEFINED kapısı (§3.7)
  C4) LiveCanary boş/varsayılan evrenle kurulamaz (§3.10)
"""
import json
import os
import tempfile
import unittest

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary,
                                 prime, T0)
import canary_quality as cq
from canary_live import (LiveCanary, CLIENT_PREFIX, set_entry_pause,
                         set_direction_enabled, SYMBOLS as LEGACY_SYMBOLS,
                         BRAIN_EXIT_POLICY_RISK_ONLY)
from canary_brain_adapter import (BrainDecisionSource, brain_fixture_db,
                                  SIGNAL_SOURCE_BRAIN)
from canary_brain import (MODEL_VERSION, BrainScanner, eligible_symbols,
                          gate_market_data, roundtrip_cost_pct,
                          DECISION_CANDIDATE)
from canary_brain_sizing import SizingBrain
from contract_discovery import discover_contracts, LIVE_ELIGIBLE

BAR = 4 * 3600 * 1000
NOW = T0 + 100 * BAR

NEW = "NEWUSDT"          # eski 8 listede YOK — görevün kanıt sembolü
OLD = "ETHUSDT"          # eski 8 listede VAR (kontrol sembolü)


def fapi_symbol(symbol, base, *, status="TRADING", contract_type="PERPETUAL",
                quote="USDT", margin="USDT", underlying="COIN"):
    return {"symbol": symbol, "baseAsset": base, "status": status,
            "contractType": contract_type, "quoteAsset": quote,
            "marginAsset": margin, "underlyingType": underlying}


def make_fake_exchange():
    """NEWUSDT + ETHUSDT sembolleri tam donanımlı FakeExchange."""
    clock = FakeClock()
    ex = FakeExchange(clock_ms=clock)
    ex.symbols = [NEW, OLD]
    novi = {"tickSize": "0.010", "stepSize": "0.01", "minQty": "0.01",
            "minNotional": "5", "quantityPrecision": 2,
            "pricePrecision": 4}
    ex.filters[NEW] = dict(novi)
    ex.brackets_raw.append({
        "symbol": NEW, "brackets": [
            {"bracket": 1, "initialLeverage": 50, "notionalFloor": 0,
             "notionalCap": 50_000, "maintMarginRatio": 0.004, "cum": 0.0}]})
    ex.margin_modes[NEW] = "cross"
    ex.leverages[NEW] = 10
    ex.mark_px[NEW] = 100.0
    ex.mark_ts[NEW] = clock()
    ex.positions[NEW] = {"positionAmt": 0.0, "entryPrice": 0.0}
    return ex, clock


class TestNewUsdtDiscovery(unittest.TestCase):
    """Zincirin 1-2. adımları: exchangeInfo → discovery → eligible."""

    def test_newusdt_eligible_and_exclusions_rejected(self):
        info = {"symbols": [
            fapi_symbol(NEW, "NEW"),                      # kanıt sembolü
            fapi_symbol("BTCUSDT", "BTC"),                # eski liste üyesi
            fapi_symbol("NEWQ", "NEWQ", quote="USDC"),    # unsupported quote
            fapi_symbol("NEWC", "NEWC", contract_type="CURRENT_QUARTER"),
            fapi_symbol("NEWI", "NEWI", underlying="INDEX"),
            fapi_symbol("NEWP", "NEWP", status="SETTLING"),
        ]}
        res = discover_contracts(fapi_info=info, dapi_info=None)
        universe = eligible_symbols(res)
        self.assertIn(NEW, universe)
        self.assertIn("BTCUSDT", universe)
        # desteklenmeyen sözleşmeler live eligible OLMAZ (doğru davranış —
        # eski 8 sembol engeliyle KARIŞTIRILMAMALI, görev §18)
        for s in ("NEWQ", "NEWC", "NEWI", "NEWP"):
            self.assertNotIn(s, universe)
        # NEWUSDT sembolü eski 8 listede YOK — kanıt
        self.assertNotIn(NEW, LEGACY_SYMBOLS)

    def test_empty_universe_is_empty_no_legacy_fallback(self):
        """Bozuk/empty exchangeInfo → boş evren (fallback YOK — runner
        FATAL verir; burada sözleşme katmanı kanıtlanır)."""
        res = discover_contracts(fapi_info={"symbols": []}, dapi_info=None)
        self.assertEqual(eligible_symbols(res), [])
        res2 = discover_contracts(fapi_info=None, dapi_info=None)
        self.assertEqual(eligible_symbols(res2), [])


class NewUsdtBase(unittest.TestCase):
    """Ortak harness: NEWUSDT içeren tam zincir kurulumu."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ex, self.clock = make_fake_exchange()
        self.syms = [NEW, OLD]
        self.brain_path = os.path.join(self.tmp.name, "brain.db")
        self.records = [self.build_record(NEW, -1)]
        self.rebuild_brain_db()
        self.brain = BrainDecisionSource(
            db_path=self.brain_path, expected_model_version=MODEL_VERSION)
        self.lc = self._make()
        prime(self.lc)
        # operatör onayları (test harness temsili): çıkış politikası +
        # yön kilidi (pause KAPALI bırakılır; pause testi ayrı)
        self.lc._set_state("brain_exit_policy", BRAIN_EXIT_POLICY_RISK_ONLY)
        set_direction_enabled(self.lc.db_path, -1, True, "test", "test")
        set_direction_enabled(self.lc.db_path, 1, True, "test", "test")

    def build_record(self, symbol, direction, *, price=100.0, ok=True):
        """GERÇEK beyin zinciriyle kayıt üret: gate → scanner → sizing."""
        now = self.clock() if hasattr(self, "clock") else NOW
        ticker = {"symbol": symbol, "quoteVolume": "90000000",
                  "lastPrice": str(price)}
        book = {"bidPrice": str(price * 0.9998), "askPrice": str(price * 1.0002)}
        premium = {"markPrice": str(price), "lastFundingRate": "0.0001",
                   "time": now}
        gate = gate_market_data(
            symbol, ticker=ticker, book=book, premium=premium,
            now_ms=now, max_age_ms=30_000, min_quote_volume_24h=5_000_000.0,
            max_spread_pct=0.10)
        self.assertTrue(gate.eligible, f"gate reddi: {gate.reasons}")
        sigma_bar = 0.01
        costs = roundtrip_cost_pct(gate.spread_pct, gate.last_funding_rate,
                                   hold_bars=174.3)
        sizing = SizingBrain().size(
            price=price, sigma_bar=sigma_bar, roundtrip_cost_pct=costs,
            position_budget_remaining=5.0, total_budget_remaining=10.0,
            available_balance=10_000.0,
            brackets=[{"floor": 0, "cap": 50_000, "max_leverage": 50,
                       "maint_margin_ratio": 0.004}])
        self.assertTrue(sizing["ok"], f"sizing reddi: {sizing['reasons']}")
        quality = {"quality_score": 77.0, "quality_threshold": 66.5,
                   "quality_pass": True, "momentum_strength": 2.0,
                   "trend_confirmation": True, "volatility_context": 1.2,
                   "volume_confirmation": True}
        rec = BrainScanner().evaluate(
            symbol, direction, cycle_id=11, now_ms=now, gate=gate,
            quality=quality, sigma_bar=sigma_bar, momentum=-0.05,
            warm=True, sizing=sizing)
        return rec.to_dict()

    def rebuild_brain_db(self):
        brain_fixture_db(self.brain_path, [{
            "cycle_id": 11, "ts": NOW, "decision": "CANDIDATE",
            "model_version": MODEL_VERSION, "universe_eligible": 2,
            "records": self.records}])

    def _make(self, **kw):
        return make_canary(self.tmp.name, self.ex,
                           entry_policy=cq.ENTRY_POLICY_QUALITY,
                           symbols=self.syms,
                           signal_source=SIGNAL_SOURCE_BRAIN,
                           brain_source=self.brain, **kw)

    def tearDown(self):
        self.lc.stop()
        self.tmp.cleanup()

    def _brain_tick(self):
        return self.lc.strategy_tick(now_ms=self.clock())

    def _orders_log(self):
        p = os.path.join(self.tmp.name, "live_orders.jsonl")
        if not os.path.exists(p):
            return []
        return [json.loads(x) for x in open(p) if x.strip()]


class TestNewUsdtEndToEnd(NewUsdtBase):
    """§3.8 kabul şartları — tam zincir."""

    def test_full_pipeline_newusdt_reaches_order_submission(self):
        """NEWUSDT adayı 8-liste kontrolünden dolayı REDDEDİLMEZ; tam
        zincir yöürür: brain kaydı → BrainDecision → try_entry →
        preflight → mock emir. Pozisyon + SL koruması NEWUSDT'de."""
        # 1) beyin kaydı gerçek zincirle üretildi (setUp) ve CANDIDATE
        self.assertEqual(self.records[0]["decision"], DECISION_CANDIDATE)
        self.assertEqual(self.records[0]["symbol"], NEW)
        # 2) adapter adayı okur
        fresh = self.brain.fresh_candidates(self.clock())
        self.assertTrue(fresh["ok"], fresh["reasons"])
        self.assertEqual([d.symbol for d in fresh["candidates"]], [NEW])
        self.assertEqual(fresh["candidates"][0].decision_id, "BR11:NEWUSDT:-1")
        # 3) execution: brain tick → try_entry → (pause yok, yön açık,
        #    exit policy onaylı) → FakeExchange'te emir
        out = self._brain_tick()
        attempts = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["symbol"], NEW)
        self.assertEqual(attempts[0]["decision_id"], "BR11:NEWUSDT:-1")
        # 4) gerçek emir gönderildi (FakeExchange — gerçek borsa DEĞİL)
        posts = [c for c in self.ex.post_calls
                 if c["method"] == "new_order" and c["symbol"] == NEW]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["side"], "SELL")   # SHORT
        # 5) pozisyon defterde + clientOrderId C281 prefix (sahiplik)
        opens = self.lc.rm.open_positions()
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["symbol"], NEW)
        self.assertEqual(opens[0]["direction"], -1)
        row = self.lc._db_execute(
            "SELECT client_id FROM live_orders WHERE kind='ENTRY'"
        ).fetchone()
        self.assertTrue(row[0].startswith(CLIENT_PREFIX))
        # 6) SL koruması NEWUSDT'de doğru hassasiyetle duruyor
        algos = [a for a in self.ex.algos.values() if a["symbol"] == NEW]
        self.assertTrue(algos, "NEWUSDT STOP_MARKET koruması yok")
        self.assertEqual(algos[0]["type"], "STOP_MARKET")
        self.assertEqual(algos[0]["closePosition"], "true")
        self.assertEqual(algos[0]["workingType"], "MARK_PRICE")
        # tetik fiyatı tickSize(0.010)'a hizalı (hassasiyet kanıtı)
        trig = float(algos[0]["triggerPrice"])
        self.assertAlmostEqual(trig / 0.010, round(trig / 0.010), places=9)
        # 7) karşılıklı: eski listeden ETHUSDT için aday YOK → emir YOK
        eth_posts = [c for c in self.ex.post_calls
                     if c["method"] == "new_order" and c["symbol"] == OLD]
        self.assertEqual(eth_posts, [])

    def test_duplicate_decision_no_second_order(self):
        """Aynı decision_id ikinci kez emir ÜRETMEZ."""
        self._brain_tick()
        n_posts = len([c for c in self.ex.post_calls
                       if c["method"] == "new_order"])
        out2 = self._brain_tick()
        dups = [r for r in out2
                if r.get("result") == "DUPLICATE_BRAIN_DECISION"]
        self.assertEqual(len(dups), 1)
        n_posts2 = len([c for c in self.ex.post_calls
                        if c["method"] == "new_order"])
        self.assertEqual(n_posts2, n_posts)

    def test_entry_pause_blocks_real_order(self):
        """ENTRY PAUSE=1 iken beyin adayı emir zincirine girer ama emir
        ÇIKMAZ (fail-closed)."""
        set_entry_pause(self.lc.db_path, True, "test", "test")
        out = self._brain_tick()
        blocked = [r for r in out if r.get("result") == "BLOCKED"]
        self.assertTrue(blocked)
        self.assertIn("ENTRY_PAUSED", " ".join(blocked[0]["blockers"]))
        self.assertEqual(
            [c for c in self.ex.post_calls if c["method"] == "new_order"], [])
        set_entry_pause(self.lc.db_path, False, "test", "test")

    def test_cross_account_gate_blocks_newusdt(self):
        """CROSS hesap kapısı NEWUSDT adayını da değerlendirir ve BLOCK
        üretebilir (dinamik sembol — 8 liste bağımsız)."""
        class BlockedGate:
            def evaluate(self, **kw):
                from canary_account_risk_gate import AccountGateDecision
                return AccountGateDecision(
                    passed=False, reasons=("AG_TEST_BLOCK",))
        self.lc.rm.account_gate = BlockedGate()
        out = self._brain_tick()
        rejected = [r for r in out if "REJECTED" in str(r.get("result"))]
        self.assertTrue(rejected)
        self.assertIn("ACCOUNT_GATE", str(rejected[0]["result"]))
        self.assertIn("AG_TEST_BLOCK", str(rejected[0]["result"]))
        self.assertEqual(
            [c for c in self.ex.post_calls if c["method"] == "new_order"], [])

    def test_brain_exit_policy_gate_blocks_without_operator_approval(self):
        """C3 düzeltmesi: brain_exit_policy onayı YOKSA beyin adayı
        BRAIN_EXIT_POLICY_UNDEFINED ile bloklanır (§3.7 — sessiz
        dormansi değil somut bloker)."""
        self.lc._set_state("brain_exit_policy", "")   # onay geri alındı
        out = self._brain_tick()
        blocked = [r for r in out if r.get("result") == "BLOCKED"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("BRAIN_EXIT_POLICY_UNDEFINED",
                      " ".join(blocked[0]["blockers"]))
        self.assertEqual(
            [c for c in self.ex.post_calls if c["method"] == "new_order"], [])

    def test_old_strategy_cannot_open_order_in_brain_mode(self):
        """Eski 8 sembollük strateji yolu BRAIN modunda bağımsız emir
        AÇAMAZ (NEWUSDT bağlamında da)."""
        # eski flip tüketme + quality değerlendirme yolları inert
        r1 = self.lc._attempt_due_flip(OLD, self.clock())
        r2 = self.lc._quality_evaluate(OLD, NOW, 100.0, self.clock(),
                                       states={}, decisions=[])
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(
            [c for c in self.ex.post_calls if c["method"] == "new_order"], [])


class TestUnownedOrderGate(NewUsdtBase):
    """C1 düzeltmesi (§3.6): yabancı açık emir giriş kilidi."""

    def _foreign_order(self):
        """Yabancı (C281 öneksiz) bekleyen LIMIT emri — FakeExchange her
        gönderilen emri anında doldurduğu için doğrudan deftere yazılır
        (status NEW → openOrders'da görünür)."""
        order = {"orderId": 99999, "symbol": NEW, "side": "BUY",
                 "type": "LIMIT", "status": "NEW", "executedQty": "0",
                 "avgPrice": "0", "cumQuote": "0",
                 "clientOrderId": "MANUALXYZ", "reduceOnly": False}
        self.ex.orders["MANUALXYZ"] = order
        return order

    def test_unowned_order_blocks_entry(self):
        self._foreign_order()
        self.lc._unowned_order_check(self.clock())
        self.assertEqual(
            str(self.lc._get_state("unowned_order_seen")), "1")
        out = self._brain_tick()
        blocked = [r for r in out if r.get("result") == "BLOCKED"]
        self.assertTrue(blocked)
        self.assertIn("açık EMİR", " ".join(blocked[0]["blockers"]))
        self.assertEqual(
            [c for c in self.ex.post_calls if c["method"] == "new_order"
             and c["client_order_id"] != "MANUALXYZ"], [])

    def test_clean_state_after_foreign_order_cancelled(self):
        self._foreign_order()
        self.lc._unowned_order_check(self.clock())
        self.assertEqual(
            str(self.lc._get_state("unowned_order_seen")), "1")
        self.ex.cancel_order(NEW, client_order_id="MANUALXYZ")
        res = self.lc._unowned_order_check(self.clock())
        self.assertEqual(res["unowned_orders"], [])
        self.assertEqual(
            str(self.lc._get_state("unowned_order_seen")), "0")

    def test_startup_reconcile_sets_latch_for_foreign_order(self):
        self._foreign_order()
        self.lc.startup_reconcile()
        self.assertEqual(
            str(self.lc._get_state("unowned_order_seen")), "1")

    def test_unreadable_algos_never_clear_latch_without_evidence(self):
        """Algo emirleri okunamazsa latch KANITSIZ temizlenmez."""
        self.lc._set_state("unowned_order_seen", 1)

        def _boom(symbol=None):
            from canary_exchange import ExchangeUnavailable
            raise ExchangeUnavailable("algo read fail")
        self.ex.open_algo_orders = _boom
        res = self.lc._unowned_order_check(self.clock())
        self.assertFalse(res["algos_ok"])
        self.assertEqual(
            str(self.lc._get_state("unowned_order_seen")), "1")


class TestDynamicUniversePosition(unittest.TestCase):
    """C2 düzeltmesi (§3.3): evren daralması pozisyon izlemesini KIRMAZ."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        # borsa ETHUSDT + WLDUSDT raporlar; canary evreni SADECE ETHUSDT
        # (WLDUSDT pozisyonu "evren dışında kalan canary pozisyonu")
        self.ex.symbols = ["ETHUSDT", "WLDUSDT"]
        novi = {"tickSize": "0.010", "stepSize": "0.01", "minQty": "0.01",
                "minNotional": "5", "quantityPrecision": 2,
                "pricePrecision": 4}
        self.ex.filters["WLDUSDT"] = dict(novi)
        self.ex.margin_modes["WLDUSDT"] = "cross"
        self.ex.leverages["WLDUSDT"] = 10
        self.ex.mark_px["WLDUSDT"] = 100.0
        self.ex.mark_ts["WLDUSDT"] = self.clock()
        self.ex.positions["WLDUSDT"] = {"positionAmt": 0.0,
                                        "entryPrice": 0.0}
        self.lc = make_canary(self.tmp.name, self.ex,
                              symbols=["ETHUSDT"])
        prime(self.lc)

    def tearDown(self):
        self.lc.stop()
        self.tmp.cleanup()

    def test_position_risk_map_unfiltered(self):
        pr = self.lc._position_risk_map()
        self.assertIn("WLDUSDT", pr)
        self.assertIn("ETHUSDT", pr)

    def test_mark_updated_for_out_of_universe_position(self):
        """Defterde WLDUSDT pozisyonu varken risk tick, mark'ı
        güncellemeye DEVAM eder (SAFE_EXIT izlemesi çalışır)."""
        # deftere canary pozisyonu yaz (evren dışı sembol — restart sonrası
        # discovery değişimi senaryosu)
        self.lc.rm.open_position("WLDUSDT", 1, 50.0, 100.0, self.clock())
        self.ex.set_position("WLDUSDT", 0.5)
        self.lc.risk_tick(now_ms=self.clock())
        self.assertIsNotNone(
            self.lc.rm._mark("WLDUSDT", self.clock()))

    def test_foreign_position_outside_universe_flags_drift(self):
        """Evren DIŞI sembolde yabancı pozisyon UNOWNED_POSITION olarak
        görünür → giriş bloğu (süzgeç gizlemesi yok)."""
        self.ex.set_position("WLDUSDT", 2.0)
        drift = self.lc._drift_check(self.clock())
        kinds = {(d["symbol"], d["kind"]) for d in drift}
        self.assertIn(("WLDUSDT", "UNOWNED_POSITION"), kinds)
        self.assertEqual(
            str(self.lc._get_state("unowned_position_seen")), "1")

    def test_out_of_universe_canary_position_not_flagged_unowned(self):
        """Defterdeki canary pozisyonu evren dışına düşse UNOWNED
        sayılmaz (bizim pozisyon — izlemesi sürer)."""
        self.lc.rm.open_position("WLDUSDT", 1, 50.0, 100.0, self.clock())
        self.ex.set_position("WLDUSDT", 0.5)
        drift = self.lc._drift_check(self.clock())
        self.assertFalse(any(d["kind"] == "UNOWNED_POSITION"
                             for d in drift))
        self.assertEqual(
            str(self.lc._get_state("unowned_position_seen")), "0")


class TestLegacyDefaultRemoved(unittest.TestCase):
    """C4 düzeltmesi (§3.10): 8 sembole sessiz geri dönüş yok."""

    def test_empty_symbols_raises(self):
        ex, _ = make_fake_exchange()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                LiveCanary(os.path.join(tmp, "db.sqlite"), ex, symbols=[])

    def test_none_symbols_raises(self):
        ex, _ = make_fake_exchange()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                LiveCanary(os.path.join(tmp, "db.sqlite"), ex)


if __name__ == "__main__":
    unittest.main()
