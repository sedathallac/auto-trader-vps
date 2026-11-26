"""GÖREV (2026-11-26): CANARY AI BRAIN ana sinyal kaynağı — entegrasyon
testleri (FakeExchange + sentetik BRAIN_DB; ağ çağrısı YOK, gerçek emir
YOK — FakeExchange bellek içi).

Kapsam (görev §1-§3, §7 kanıt maddeleri):
  1. Sözleşme: BrainDecision alanları (symbol, direction, ts,
     model_version, expected_net_return, estimated_costs, probability/
     calibration, uncertainty, stop_distance, suggested_notional/qty,
     rejection_reasons, decision_id) beynin kaydından TAM gelir.
  2. Idempotency: aynı decision_id ikinci kez emir ÜRETEMEZ.
  3. Eski 8 sembol stratejisi bağımsız emir AÇAMAZ (BRAIN_PRIMARY modda
     H1 flip/quality yolları kilitli; kline beslense bile giriş yok).
  4. Beyin önerisi emir DEĞİL: suggested_notional politika tavanıyla
     kırpılır; CROSS risk kapısı/ENTRY PAUSE/yön kilidi son kararı verir.
  5. Fail-closed: bayat döngü / model uyuşmazlığı / DB yok / boyutlama
     geçersiz → aday YOK veya BLOCKED (asla politika boyutu icat edilmez).
  6. Uzlanabilirlik: tek karar kaydının beyin DB satırından execution
     değerlendirmesine izi (decision_id → BRAIN_DECISION → ENTRYAttempt
     → live_orders.params).
"""
import json
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary,
                                 prime, T0)
import canary_quality as cq
from canary_live import LiveCanary, set_entry_pause, set_direction_enabled
from canary_brain_adapter import (BrainDecisionSource, brain_fixture_db,
                                  SIGNAL_SOURCE_BRAIN, decision_id_for,
                                  BA_CYCLE_STALE, BA_MODEL_MISMATCH,
                                  BA_DB_MISSING, BA_NO_CYCLE,
                                  BA_CYCLE_NO_TRADE)
from canary_brain import MODEL_VERSION

BAR = 4 * 3600 * 1000
SYM = "ETHUSDT"          # eski 8 listede VAR
NEW_SYM = "APTUSDT"      # eski 8 listede YOK — evren kanıtı
NOW = T0 + 100 * BAR     # T0'dan sonra


def brain_record(symbol, direction, *, ts=NOW, price=100.0,
                 notional=80.0, ok=True, reasons=(), **extra):
    """Beyin kaydı (canary_brain.PredictionRecord.to_dict biçimi)."""
    rec = {"symbol": symbol, "direction": direction, "ts": ts,
           "cycle_id": 7, "decision": "CANDIDATE", "regime": "HIGH_VOL_UP",
           "signal_score": 77.0, "quality_threshold": 66.5,
           "quality_pass": True, "calibrated_probability": 0.34,
           "probability_is_calibrated": False,
           "expected_gross_return_pct": 1.6,
           "expected_net_return_pct": 1.1,
           "expected_transaction_costs_pct": 0.5,
           "funding_estimate_pct": 0.1, "slippage_estimate_pct": 0.03,
           "prediction_uncertainty_pct": 5.2, "stop_distance_pct": 2.0,
           "expected_holding_bars": 174.3, "expected_holding_hours": 697.2,
           "rejection_reasons": list(reasons),
           "model_version": MODEL_VERSION, "calibration_domain": "8_SYMBOLS",
           "price": price, "spread_pct": 0.02, "quote_volume_24h": 9e7,
           "last_funding_rate": 0.0001, "momentum": 0.05, "mom_t": 2.0,
           "sigma_bar": 0.01,
           "sizing": {"ok": ok, "reasons": [], "warnings": [],
                      "capped_by": [], "stop_distance_pct": 2.0,
                      "notional": notional if ok else None,
                      "qty": (notional / price) if ok else None,
                      "risk_usdt": notional * 0.02 if ok else None}}
    rec.update(extra)
    return rec


def make_brain_db(path, records, *, cycle_ts=NOW, decision="CANDIDATE",
                  model_version=MODEL_VERSION):
    return brain_fixture_db(path, [{
        "cycle_id": 7, "ts": cycle_ts, "decision": decision,
        "model_version": model_version, "universe_eligible": 519,
        "market_data_covered": 519, "liquid": 152, "history_warm": 152,
        "scored": 152, "records": records}])


class BrainPrimaryBase(unittest.TestCase):
    """Ortak harness: FakeExchange + sentetik BRAIN_DB + BRAIN_PRIMARY
    canary (kalite politikası — üretim varsayılanı)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        # evren: 8 eski sembolden BAĞIMSIZ (yeni semboller de işlenebilir)
        self.syms = ["APTUSDT", "ETHUSDT", "WLDUSDT"]
        self.ex.symbols = self.syms
        base = FakeExchange()
        self.ex.filters = dict(base.filters)
        # evren-novi semboller: gerçekçi filtre/braket/marj ekleri
        novi = {"tickSize": "0.010", "stepSize": "0.01", "minQty": "0.01",
                "minNotional": "5", "quantityPrecision": 2,
                "pricePrecision": 4}
        for s in ("APTUSDT", "WLDUSDT"):
            self.ex.filters.setdefault(s, dict(novi))
            self.ex.brackets_raw.append({
                "symbol": s, "brackets": [
                    {"bracket": 1, "initialLeverage": 50,
                     "notionalFloor": 0, "notionalCap": 50_000,
                     "maintMarginRatio": 0.004, "cum": 0.0}]})
            self.ex.margin_modes[s] = "cross"
            self.ex.leverages[s] = 10
            self.ex.mark_px.setdefault(s, 100.0)
            self.ex.mark_ts.setdefault(s, self.clock())
            self.ex.positions.setdefault(
                s, {"positionAmt": 0.0, "entryPrice": 0.0})
        self.brain_path = os.path.join(self.tmp.name, "brain.db")
        self.records = [brain_record(SYM, 1), brain_record(NEW_SYM, -1)]
        make_brain_db(self.brain_path, self.records)
        self.brain = BrainDecisionSource(
            db_path=self.brain_path, expected_model_version=MODEL_VERSION)
        self.lc = self._make()
        prime(self.lc)

    def _make(self, **kw):
        return make_canary(self.tmp.name, self.ex, entry_policy=(
            cq.ENTRY_POLICY_QUALITY), symbols=self.syms,
            signal_source=SIGNAL_SOURCE_BRAIN, brain_source=self.brain,
            **kw)

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


class TestBrainDecisionSource(unittest.TestCase):
    """Sözleşme katmanı: BRAIN_DB okuma + tazelik + model doğrulama."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW

    def tearDown(self):
        self.tmp.cleanup()

    def test_decision_id_deterministic_and_traceable(self):
        self.assertEqual(decision_id_for(7, "ETHUSDT", 1), "BR7:ETHUSDT:1")
        self.assertEqual(decision_id_for(7, "ETHUSDT", -1),
                         "BR7:ETHUSDT:-1")

    def test_contract_fields_complete(self):
        p = make_brain_db(os.path.join(self.tmp.name, "b.db"),
                          [brain_record(SYM, 1)])
        src = BrainDecisionSource(p, expected_model_version=MODEL_VERSION,
                                  now_ms=lambda: self.now)
        out = src.fresh_candidates(self.now)
        self.assertTrue(out["ok"])
        d = out["candidates"][0]
        # görev §2 zorunlu alanların TAMAMI
        for f in ("decision_id", "symbol", "direction", "ts",
                  "model_version", "expected_net_return_pct",
                  "expected_transaction_costs_pct", "calibrated_probability",
                  "probability_is_calibrated", "prediction_uncertainty_pct",
                  "stop_distance_pct", "suggested_notional",
                  "suggested_quantity", "rejection_reasons"):
            self.assertTrue(hasattr(d, f), f"sözleşme alanı yok: {f}")
        self.assertEqual(d.symbol, SYM)
        self.assertEqual(d.direction, 1)
        self.assertEqual(d.decision_id, "BR7:ETHUSDT:1")
        self.assertEqual(d.suggested_notional, 80.0)
        self.assertEqual(d.model_version, MODEL_VERSION)
        # tam kayıt provenance için durur
        self.assertEqual(d.record["cycle_id"], 7)

    def test_stale_cycle_fail_closed(self):
        p = make_brain_db(os.path.join(self.tmp.name, "b.db"),
                          [brain_record(SYM, 1)],
                          cycle_ts=self.now - 10 * 3600 * 1000)
        src = BrainDecisionSource(p, now_ms=lambda: self.now)
        out = src.fresh_candidates(self.now)
        self.assertFalse(out["ok"])
        self.assertEqual(out["candidates"], [])
        self.assertTrue(any(r.startswith(BA_CYCLE_STALE)
                            for r in out["reasons"]))

    def test_model_mismatch_fail_closed(self):
        p = make_brain_db(os.path.join(self.tmp.name, "b.db"),
                          [brain_record(SYM, 1)],
                          model_version="BASKA_MODEL_v9")
        src = BrainDecisionSource(p, expected_model_version=MODEL_VERSION,
                                  now_ms=lambda: self.now)
        out = src.fresh_candidates(self.now)
        self.assertFalse(out["ok"])
        self.assertTrue(any(r.startswith(BA_MODEL_MISMATCH)
                            for r in out["reasons"]))

    def test_missing_db_fail_closed(self):
        src = BrainDecisionSource(os.path.join(self.tmp.name, "yok.db"))
        out = src.fresh_candidates(self.now)
        self.assertFalse(out["ok"])
        self.assertTrue(any(r.startswith(BA_DB_MISSING)
                            for r in out["reasons"]))

    def test_no_trade_cycle_gives_no_candidates(self):
        p = make_brain_db(os.path.join(self.tmp.name, "b.db"), [],
                          decision="NO_TRADE")
        src = BrainDecisionSource(p, now_ms=lambda: self.now)
        out = src.fresh_candidates(self.now)
        self.assertFalse(out["ok"])
        self.assertEqual(out["candidates"], [])
        self.assertTrue(any(r.startswith(BA_CYCLE_NO_TRADE)
                            for r in out["reasons"]))


class TestBrainPrimaryExecution(BrainPrimaryBase):

    def test_entry_pause_blocks_brain_candidate(self):
        """ENTRY PAUSE=1 → beyin adayı emir OLMAZ; karar görünür kalır."""
        set_entry_pause(self.lc.db_path, True, "test pause", "test")
        out = self._brain_tick()
        # her iki aday da BLOCKED — ENTRY_PAUSED blocker ile
        blocked = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                   and r.get("result") == "BLOCKED"]
        self.assertEqual(len(blocked), 2)
        for r in blocked:
            self.assertIn("ENTRY_PAUSED", " ".join(r["blockers"]))
        # decision_id kayıtta görünür (izlenebilirlik)
        tick = self._orders_log()[-1]
        self.assertEqual(tick["kind"], "BRAIN_TICK")
        self.assertEqual(tick["brain_cycle_id"], 7)
        ids = {e["decision_id"] for e in tick["evaluated"]}
        self.assertEqual(ids, {"BR7:ETHUSDT:1", "BR7:APTUSDT:-1"})

    def test_duplicate_decision_id_no_second_order(self):
        """Aynı decision_id ikinci kez emir ÜRETEMEZ (idempotency)."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        set_direction_enabled(self.lc.db_path, -1, True, "t", "test")
        out1 = self._brain_tick()
        # ikinci tick — aynı döngü/adaylar
        out2 = self._brain_tick()
        dups = [r for r in out2
                if r.get("result") == "DUPLICATE_BRAIN_DECISION"]
        self.assertEqual(len(dups), 2)
        # ikinci tick'te HİÇ emir denenmedi (early dedup)
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out2))

    def test_brain_notional_clamped_to_policy_cap(self):
        """AI önerisi emir DEĞİL: suggested_notional > politika tavanı →
        100×(1−buffer)'e kırpılır; emir yine risk kapısından geçer."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        big = brain_record(SYM, 1, notional=500.0)
        make_brain_db(self.brain_path, [big])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
        # FakeExchange fill fiyatı 100 → notional = qty×100 ≤ 99
        self.assertLessEqual(rec["notional"], 99.0 + 1e-9)
        self.assertGreater(rec["notional"], 0)

    def test_brain_sizing_not_ok_blocks(self):
        """Boyutlama önerisi geçersiz → BLOCKED; politika boyutu icat
        EDİLMEZ (AI önerisi ile emir ayrımı)."""
        bad = brain_record(SYM, 1, ok=False,
                           sizing={"ok": False,
                                   "reasons": ["SR_STOP_BELOW_COSTS:x"]})
        make_brain_db(self.brain_path, [bad])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "BRAIN_DECISION"][0]
        self.assertEqual(rec["result"], "BLOCKED")
        self.assertIn("BRAIN_SIZING_NOT_OK",
                      " ".join(rec["blockers"]))
        # emir DENENMEDİ
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out))

    def test_brain_rejection_reasons_visible(self):
        """CANDIDATE olup ret nedeni taşıyan kayıt (tutarsız beyin
        çıktısı) BLOCKED — nedenler execution kaydına taşınır."""
        odd = brain_record(SYM, 1, reasons=["BR_SPREAD_TOO_WIDE:x"])
        make_brain_db(self.brain_path, [odd])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "BRAIN_DECISION"][0]
        self.assertEqual(rec["result"], "BLOCKED")
        self.assertIn("BRAIN_REJECTION:BR_SPREAD_TOO_WIDE:x",
                      rec["blockers"])
        # emir DENENMEDİ
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out))
        # ret nedeni sözleşme alanı olarak da görünür
        self.assertEqual(rec["rejection_reasons"],
                         ["BR_SPREAD_TOO_WIDE:x"])

    def test_direction_lock_blocks_brain_candidate(self):
        """Yön kilidi (varsayılan KAPALI) beyin adayını da bloklar."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        # SHORT kilidi kapalı → APTUSDT SHORT bloklanır
        make_brain_db(self.brain_path, [brain_record(NEW_SYM, -1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
        self.assertEqual(rec["result"], "BLOCKED")
        self.assertIn("DIRECTION_DISABLED_SHORT",
                      " ".join(rec["blockers"]))

    def test_price_deviation_consumes_decision(self):
        """Taze fiyat beyin fiyatından çok uçtuysa karar kalıcı tüketilir
        (eski sinyal oynatılmaz)."""
        self.ex.book_spread = 0.0002
        # beyin fiyatı 100; icra fiyatını 10% yukarı çek
        self.ex.set_mark(SYM, 110.0)
        for s in self.ex.symbols:
            self.ex.mark_px[s] = 110.0
        rec_far = brain_record(SYM, 1, price=100.0)
        make_brain_db(self.brain_path, [rec_far])
        out = self._brain_tick()
        stale = [r for r in out if r.get("result") == "BRAIN_SIGNAL_STALE"]
        self.assertEqual(len(stale), 1)
        # kalıcı tüketildi: ikinci tick'te DUPLICATE
        out2 = self._brain_tick()
        self.assertTrue(any(r.get("result") == "DUPLICATE_BRAIN_DECISION"
                            for r in out2))

    def test_stale_brain_cycle_no_candidates(self):
        """Bayat beyin döngüsü → fail-closed: aday yok, emir yok."""
        make_brain_db(self.brain_path, self.records,
                      cycle_ts=self.clock() - 10 * 3600 * 1000)
        out = self._brain_tick()
        self.assertEqual(out, [])
        tick = self._orders_log()[-1]
        self.assertFalse(tick["brain_ok"])
        self.assertTrue(any("BA_CYCLE_STALE" in r
                            for r in tick["brain_reasons"]))

    def test_new_universe_symbol_entry_allowed(self):
        """Eski 8 listede OLMAYAN sembol (APTUSDT) beyin adayıysa giriş
        zinciri ÇALIŞIR — evren kısıtı kalktı (LONG+SHORT ayrı)."""
        set_direction_enabled(self.lc.db_path, -1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(NEW_SYM, -1)])
        out = self._brain_tick()
        attempts = [r for r in out
                    if r.get("kind") == "LIVE_ENTRY_ATTEMPT"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["symbol"], NEW_SYM)
        self.assertEqual(attempts[0]["direction"], -1)
        self.assertEqual(attempts[0]["decision_id"], "BR7:APTUSDT:-1")
        # dolum FakeExchange'te anında → pozisyon defteri + koruma
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        pos = self.lc.rm.open_positions()[0]
        self.assertEqual(pos["symbol"], NEW_SYM)
        self.assertEqual(pos["direction"], -1)

    def test_decision_id_in_order_params(self):
        """decision_id live_orders.params'a yazılır — beyin kaydından
        emre tek anahtarla inilir."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        self._brain_tick()
        row = self.lc._db_execute(
            "SELECT params FROM live_orders WHERE kind='ENTRY'"
        ).fetchone()
        params = json.loads(row[0])
        self.assertEqual(params["decision_id"], "BR7:ETHUSDT:1")
        self.assertEqual(params["signal_source"], SIGNAL_SOURCE_BRAIN)

    def test_both_directions_evaluated(self):
        """LONG ve SHORT adaylar AYNI döngüde bağımsız değerlendirilir."""
        out = self._brain_tick()   # pause yok, yönler kilitli → BLOCKED
        attempted = [r for r in out
                     if r.get("kind") == "LIVE_ENTRY_ATTEMPT"]
        dirs = {(r["symbol"], r["direction"]) for r in attempted}
        self.assertEqual(dirs, {(SYM, 1), (NEW_SYM, -1)})


class TestOldStrategyDisabled(BrainPrimaryBase):
    """Görev §1/§4: eski 8 sembollük strateji bağımsız emir AÇAMAZ."""

    def _feed_flip_klines(self):
        """Eski H1 motorunda ısınma + flip ÜRETECEK kline dizisi.

        H1 sinyali 180 bar momentumudur (close/close[-180]): ısınma
        DÜŞEN seriyle dir=-1 kurulur; flip barı close(t-180)'in ÜSTÜNE
        sıçrar → m>0 → +1 flip (tek barlık düşüş yetmez — momentum
        penceresi gerçekten dönmeli)."""
        up = []
        for i in range(400):
            ot = T0 + i * BAR
            px = 100.0 * math.pow(0.995, i)
            up.append((ot, px, px, 1e6))
        return {s: list(up) for s in self.syms}

    def _h1_control_canary(self):
        """Diferansiyel kontrol: aynı girdiyle H1_LOCAL modunda kanarya
        (yalnız signal_source farklı — flip yolunun beyin modunda ÖLÜ,
        H1 modunda CANLI olduğunu kanıtlar)."""
        from canary_liquidation import (LiquidationModel,
                                        parse_leverage_brackets,
                                        POSITION_MODE_ONEWAY)
        liq = LiquidationModel(
            brackets_by_symbol=parse_leverage_brackets(
                self.ex.brackets_raw),
            margin_modes=self.ex.margin_modes,
            leverages=self.ex.leverages,
            position_mode=POSITION_MODE_ONEWAY, wallet_usdt=self.ex.wallet)
        return LiveCanary(os.path.join(self.tmp.name, "h1_control.db"),
                          self.ex, symbols=self.syms, t0_ms=T0,
                          filters=self.ex.filters, liq_model=liq,
                          clock_ms=self.ex._clock, log_dir=self.tmp.name,
                          entry_policy=cq.ENTRY_POLICY_FLIP_ONLY)

    def test_h1_flip_cannot_open_order_in_brain_mode(self):
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        set_direction_enabled(self.lc.db_path, -1, True, "t", "test")
        # beyni NO_TRADE yap — kalan tek sinyal kaynağı eski motor olurdu
        make_brain_db(self.brain_path, [], decision="NO_TRADE")
        # 1) warmup replay (beyin modunda da motor beslenebilir)
        kl = self._feed_flip_klines()
        self.clock.advance(400 * BAR)   # replay barları KAPALI olmalı
        decisions, states = self.lc._evaluate(self.clock(), kl)
        self.assertEqual(decisions, [])     # warmup → execute'siz
        # 2) FLİP barı: düşen seriden yükselen kapanışa geçiş (m>0 → +1). Saat bir
        # bar ilerletilir (bookTicker/mark tazeliği); icra fiyatı flip
        # kapanışına yakın — H1 kontrolde flip GERÇEKTEN olay üretmeli
        ot = kl[SYM][-1][0] + BAR
        # close(t-180) = 100*0.995^220 ≈ 33.2 → 100.0 onu aşar: m>0 → +1
        px = 100.0
        flip_kl = {s: [(ot, px, px, 1e6)] for s in self.syms}
        self.clock.advance(BAR)             # now = ot + BAR (karar anı)
        for s in self.syms:
            self.ex.set_mark(s, px)
        decisions, states = self.lc._evaluate(self.clock(), flip_kl)
        # BEYİN MODUNDA: flip yolu kilitli → karar olayı YOK
        self.assertEqual(decisions, [])
        # strategy_tick de emir denemedi
        out = self.lc.strategy_tick(now_ms=self.clock(),
                                    klines_by_sym=flip_kl)
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out))
        self.assertEqual(len(self.lc.rm.open_positions()), 0)
        # 3) DİFERANSİYEL KANIT: aynı girdiler H1_LOCAL kontrol kanaryasında
        #    flip olayı ÜRETİR (motor canlı; kilit yalnız beyin modunda)
        h1 = self._h1_control_canary()
        try:
            h1._evaluate(self.clock(), kl)
            decisions_h1, _ = h1._evaluate(self.clock(), flip_kl)
            self.assertTrue(any(ev[0] == SYM and ev[1] == 1
                                for ev in decisions_h1),
                            f"H1 kontrolde flip beklenirdi: {decisions_h1}")
        finally:
            h1.stop()

    def test_attempt_due_flip_inert_in_brain_mode(self):
        """_attempt_due_flip doğrudan çağrılsa bile beyin modunda None."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        # ısınma: düşen fiyatlar → engine dir=-1
        for i in range(200):
            self.lc.engine.begin_bar(SYM, 100.0 * math.pow(0.995, i))
            self.lc.engine.end_bar(SYM, T0 + i * BAR,
                                   100.0 * math.pow(0.995, i))
        self.assertEqual(self.lc.engine.direction(SYM), -1)
        # yükselen kapanış (begin_bar POP ETMEDEN) → pending LONG flip
        self.lc.engine.end_bar(SYM, T0 + 200 * BAR, 200.0)
        self.assertEqual(self.lc.engine.pending(SYM), 1)
        # beyin modunda flip yolu ölü — None döner, hedef TÜKETİLMEZ
        self.assertIsNone(self.lc._attempt_due_flip(
            SYM, self.clock(), {}))
        self.assertEqual(self.lc.engine.pending(SYM), 1)

    def test_quality_evaluate_inert_in_brain_mode(self):
        """_quality_evaluate doğrudan çağrılsa bile beyin modunda giriş
        doğurmaz (savunma-derinliği)."""
        decisions = []
        states = {SYM: {"symbol": SYM}}
        self.lc._quality_evaluate(SYM, T0, 100.0, self.clock(),
                                  states, decisions)
        self.assertEqual(decisions, [])
        self.assertIsNone(states[SYM].get("decision"))


class TestRiskGatesIntact(BrainPrimaryBase):
    """Görev §3: risk kuralları beyin yolunda da aynen çalışır."""

    def test_cross_account_gate_blocks_brain_candidate(self):
        """Hesap kapısı (AccountRiskGate) BLOCK üretir → OrderRejected
        → beyin adayı emir OLMAZ (AI önerisi risk kapısını atlayamaz)."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")

        class BlockedGate:
            def evaluate(self, **kw):
                from canary_account_risk_gate import AccountGateDecision
                return AccountGateDecision(
                    passed=False, reasons=("AG_TEST_BLOCK",))

        self.lc.rm.account_gate = BlockedGate()
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
        self.assertTrue(rec["result"].startswith("REJECTED"))
        self.assertIn("AG_TEST_BLOCK", rec["result"])
        self.assertEqual(len(self.lc.rm.open_positions()), 0)

    def test_position_loss_budget_enforced_for_brain_entry(self):
        """Pozisyon başına -5 USDT zarar bütçesi beyin girişinde de
        uygulanur (risk AZALTAN çıkış serbest)."""
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        self._brain_tick()
        pos = self.lc.rm.open_positions()[0]
        # mark'ı -6 USDT zarar üretecek kadar düşür
        units = pos["units"]
        entry = pos["entry_px"]
        mark_px = entry - 6.5 / units
        self.ex.set_mark(SYM, mark_px)
        self.lc.risk_tick(now_ms=self.clock())
        pos2 = [p for p in self.lc.rm.open_positions()
                if p["pos_id"] == pos["pos_id"]][0]
        self.assertEqual(pos2["status"], "SAFE_EXIT")


if __name__ == "__main__":
    unittest.main()
