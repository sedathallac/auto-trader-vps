"""BLOK31.1 — WARMUP, SİNYAL DURUMU ve GÖZLENEBİLİRLİK TESTLERİ
(FakeExchange; ağ çağrısı YOK, gerçek emir YOK).

BLOK31.1'in düzelttiği/kanıtlanan davranışlar:
  - `warmup` log alanının semantiği: true = warmup SÜRÜYOR (tamamlanmamış).
    Tamamlanma `warmup_complete`/`warmup_state` alanlarından açıkça görünür;
    eksikse `warmup_missing_bars` + `warmup_reason` söyler. (Canlıda 25
    Kasım cycle kayıtlarındaki kalıcı `warmup=true` alanın kök nedeni:
    alan `s in _warmed` yazıyordu — tamamlanmış sembollerde de true
    okunuyordu. Engine davranışı ZATEN doğruydu; hata yalnızca etikette.)
  - WARMUP_DONE kaydı sembol bazlı tamamlanma durumu taşır; bars_replayed
    restart başına bir kez replay edilen bar sayısıdır (409 kapalı bar ×
    8 sembol = 3272 — canlı kanıtla aynı).
  - STRATEGY_CYCLE kaydı `last_processed_bar_before` ve `_after` taşır
    (cycle35 tutarsızlığının kök nedeni: kayıt işleme ÖNCESİ durumunu
    yazıyordu, sonraki cycle doğruluyordu).
  - KANONİK §7 bar sırası: end_bar'dan önce penceresi kesinlikle kapanmış
    due flip MISSED olarak tüketilir — eskiden end_bar'ın sig!=dir
    karşılaştırması ilerlememiş dir üzerinden yapıldığı için momentum geri
    dönerse due flip SESSİZCE iptal oluyordu.
  - boş kline yanıtı veri hatasıdır (DATA_FETCH_ERROR) —
    NO_NEW_CLOSED_BAR/NO_SIGNAL olarak sessizce geçilmez.
  - döngü kaydı kapı durumlarını taşır: entry_paused, halt_state,
    rm_status, reconciliation_status, feed_status.
  - başlangıç direction=-1 flip-only tasarımın SONUCUDUR: eski (T0 öncesi)
    momentum hedefi yeni canlı sinyal DEĞİLDİR; yeni giriş yalnız YENİ
    flip'te doğar (donmuş §7; paper/shadow ile aynı).

§10 madde eşlemesi (bu dosya): W1-W7 (warmup), E8-E14 (engine),
C15-C16 (kanonik sıra), O31-O36 (gözlemlenebilirlik). T0/emir zinciri
maddeeleri test_blok31_entry_pipeline.py'de (1-49).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 T0)

BAR = 4 * 3600 * 1000
LOOKBACK = 180


class Blok31_1WarmupSignalStateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- yardımcılar ---------------------------------------------------------

    def _hist(self, end_ot, n, start_px=200.0, step=-0.5):
        """`n` barlık sürekli düşen kapanış dizisi (son bar `end_ot`)."""
        return [(end_ot - (n - 1 - i) * BAR, 100.0, start_px + step * i, 1.0)
                for i in range(n)]

    def _cycles(self):
        p = os.path.join(self.tmp.name, "strategy_cycles.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _last_cycle(self):
        cyc = self._cycles()
        assert cyc, "strategy_cycles.jsonl boş"
        return cyc[-1]

    def _symbol_state(self, cyc, symbol):
        for row in cyc["symbols"]:
            if row["symbol"] == symbol:
                return row
        raise AssertionError(f"{symbol} durumu döngü kaydında yok")

    def _orders_log(self):
        p = os.path.join(self.tmp.name, "live_orders.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _entry_posts(self):
        return [c for c in self.ex.post_calls
                if c["method"] == "new_order" and not c["reduce_only"]]

    def _fresh_marks(self, now, px=100.0):
        self.clock.t = now
        for s in self.ex.symbols:
            self.ex.set_mark(s, px)
            self.lc.rm.update_mark(s, px, now)

    # =========================================================================
    # §10/W1-W7 — WARMUP
    # =========================================================================

    def test_w1_initial_warmup_replays_and_completes(self):
        """(W1) İlk tik: geçmiş replay edilir (execute YOK), WARMUP_DONE
        kaydı sembol bazlı tamamlanma taşır, döngü durumu WARMUP_COMPLETE."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": hist})
        warm = [r for r in self._orders_log() if r["kind"] == "WARMUP_DONE"]
        self.assertEqual(len(warm), 1)
        self.assertEqual(warm[0]["bars_replayed"], 200)   # kapalı barlar
        self.assertEqual(warm[0]["execute"], False)
        self.assertTrue(warm[0]["symbols_warmed"]["ETHUSDT"])
        self.assertFalse(warm[0]["symbols_warmed"]["BTCUSDT"])  # veri yok
        self.assertFalse(warm[0]["all_warm"])   # 7 sembol verisiz → tam değil
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertTrue(st["warmup_complete"])
        self.assertEqual(st["warmup_state"], "WARMUP_COMPLETE")
        # DÜZELTİLEN SEMANTİK: warmup=false = warmup SÜRMÜYOR (tamamlandı)
        self.assertFalse(st["warmup"])
        self.assertEqual(self.lc.engine.warm("ETHUSDT"), True)

    def test_w2_warmup_complete_state_logged(self):
        """(W2) Warmup tamamlandıktan sonraki tiklerde durum açıkça
        WARMUP_COMPLETE/warmup=false — 'hâlâ warmup' okunmaz."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        now2 = now + 300_000
        self._fresh_marks(now2)
        self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertTrue(st["warmup_complete"])
        self.assertFalse(st["warmup"])
        self.assertEqual(st["warmup_state"], "WARMUP_COMPLETE")
        # yeni WARMUP_DONE yok (warmup replay tekrar etmez)
        self.assertEqual(len([r for r in self._orders_log()
                              if r["kind"] == "WARMUP_DONE"]), 1)

    def test_w3_per_symbol_warmup_independence(self):
        """(W3) Sembol başına warmup ayrı: yalnız veri verilen sembol
        ısınır; diğerleri WARMUP_INCOMPLETE + eksik bar sayısı."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200),
            "BTCUSDT": self._hist(T0 - BAR, 10)})   # 10 bar < lookback
        eth = self._symbol_state(self._last_cycle(), "ETHUSDT")
        btc = self._symbol_state(self._last_cycle(), "BTCUSDT")
        self.assertTrue(eth["warmup_complete"])
        self.assertFalse(btc["warmup_complete"])     # engine n=10 < 180
        self.assertTrue(btc["warmup"])               # warmup SÜRÜYOR
        self.assertEqual(btc["warmup_state"], "WARMUP_INCOMPLETE")
        self.assertEqual(btc["warmup_missing_bars"], LOOKBACK - 10)
        self.assertIn("lookback", btc["warmup_reason"])
        self.assertEqual(btc["decision"], "WARMUP_INCOMPLETE")

    def test_w4_restart_replays_warmup_engine_rebuilt(self):
        """(W4) Restart: engine durumu geçmişin replay'i ile YENİDEN kurulur
        (dir=-1 korunur); barlar ikinci kez YENİ sayılmaz."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        d_before = self.lc.engine.direction("ETHUSDT")
        n_before = self.lc.engine._state("ETHUSDT")["n"]
        # restart: aynı DB, yeni süreç
        lc2 = make_canary(self.tmp.name, self.ex)
        prime(lc2)
        now2 = T0 + 120_000
        self._fresh_marks(now2)
        lc2.strategy_tick(now_ms=now2, klines_by_sym={"ETHUSDT": hist})
        self.assertEqual(lc2.engine.direction("ETHUSDT"), d_before)
        self.assertEqual(lc2.engine._state("ETHUSDT")["n"], n_before)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["new_bar_count"], 0)          # replay, yeni işleme YOK
        self.assertTrue(st["warmup_complete"])
        # replay restart başına bir kez: bu tikte WARMUP_DONE yazıldı
        warm = [r for r in self._orders_log()
                if r["kind"] == "WARMUP_DONE"]
        self.assertEqual(len(warm), 2)

    def test_w5_new_bar_processed_after_warmup(self):
        """(W5) Warmup sonrası YENİ kapalı bar işlenir: new_bar_count=1,
        last_processed_bar_after=güncel, karar TARGET_UNCHANGED."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        new_ot = T0
        now2 = new_ot + BAR + 60_000                      # bar kapandı
        self._fresh_marks(now2)
        self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200) + [(new_ot, 100.0, 99.0, 1.0)]})
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["new_bar_count"], 1)
        self.assertEqual(st["last_processed_bar_before"], T0 - BAR)
        self.assertEqual(st["last_processed_bar_after"], new_ot)
        self.assertEqual(st["decision"], "TARGET_UNCHANGED")

    def test_w6_no_unbounded_warmup_loop(self):
        """(W6) Sınırsız warmup döngüsü YOK: aynı veriyle art arda tikler
        yeni replay üretmez; bar sayısı sabit kalır."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        for i in range(4):
            self.lc.strategy_tick(now_ms=now + i * 300_000,
                                  klines_by_sym={"ETHUSDT": hist})
        n = self.lc.engine._state("ETHUSDT")["n"]
        self.assertEqual(n, 200)                          # büyüme YOK
        self.assertEqual(len([r for r in self._orders_log()
                              if r["kind"] == "WARMUP_DONE"]), 1)

    def test_w7_warmup_replay_produces_no_orders(self):
        """(W7) Warmup replay asla emir üretmez; canlı flip üretir
        (warmup ↔ canlı sinyal ayrımı)."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        self.assertEqual(self._entry_posts(), [])
        # canlı flip (test_32 deseni): momentum pozitife döner
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2, px=250.0)
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200) + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["direction"], 1)
        self.assertEqual(len(self._entry_posts()), 1)

    # =========================================================================
    # §10/E8-E14 — ENGINE
    # =========================================================================

    def test_e8_initial_direction_minus1_is_target_not_signal(self):
        """(E8) Başlangıç direction=-1 GEÇMİŞ hedeftir: signal_pending=0,
        giriş denemesi YOK (flip-only §7)."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), -1)
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)
        self.assertEqual(self._entry_posts(), [])
        attempts = [r for r in self._orders_log()
                    if r.get("kind") == "LIVE_ENTRY_ATTEMPT"]
        self.assertEqual(attempts, [])                    # deneme bile YOK

    def test_e9_new_bar_same_direction_no_flip(self):
        """(E9) Yeni mum aynı yönde: direction değişmez, pending 0,
        TARGET_UNCHANGED."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2)
        self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 99.0, 1.0)]})  # düşmeye devam
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), -1)
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "TARGET_UNCHANGED")
        self.assertFalse(st["target_changed"])
        self.assertEqual(self._entry_posts(), [])

    def test_e10_genuine_flip_generated_and_attempted(self):
        """(E10) Gerçek flip: momentum işareti değişir → pending doğar →
        karar anında taze fiyatla değerlendirilir (SIGNAL_VALID)."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2, px=250.0)
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["direction"], 1)          # -1 → +1 flip
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "SIGNAL_VALID")
        self.assertTrue(st["flip_detected"])
        self.assertTrue(st["target_changed"])
        self.assertEqual(st["engine_direction_before"], -1)
        self.assertEqual(st["engine_direction_after"], 1)

    def test_e11_same_signal_not_reproduced(self):
        """(E11) Aynı sinyal ikinci kez üretilmez: flip tüketildikten sonra
        aynı veri tekrar sunulursa yeni olay/emir YOK."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2, px=250.0)
        kl = hist + [(T0, 100.0, 250.0, 1.0)]
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": kl})
        self.assertEqual(len(out), 1)
        now3 = now2 + 300_000
        self._fresh_marks(now3)
        out2 = self.lc.strategy_tick(now_ms=now3, klines_by_sym={
            "ETHUSDT": kl})
        self.assertEqual(out2, [])
        self.assertEqual(len(self._entry_posts()), 1)

    def test_e12_pending_signal_survives_price_failure_and_restart(self):
        """(E12) Pending sinyal kaybolmaz: fiyat alınamazsa flip TUTULUR;
        restart sonrası da (replay ile) due flip yeniden değerlendirilir."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2, px=250.0)
        self.ex.book_ticker_fail.add("ETHUSDT")
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(out, [])
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 1)
        self.ex.book_ticker_fail.clear()
        # restart: aynı DB — replay (begin_bar+end_bar dizisi) due flip'i
        # YENİDEN kurar; leftover taraması pencere içinde taze fiyatla icra eder
        lc2 = make_canary(self.tmp.name, self.ex)
        prime(lc2)
        self.assertEqual(lc2.engine.pending("ETHUSDT"), 0)  # henüz replay yok
        now3 = now2 + 300_000
        self._fresh_marks(now3, px=250.0)
        out2 = lc2.strategy_tick(now_ms=now3, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(len(out2), 1)          # sinyal kaybolmadı
        self.assertEqual(out2[0]["direction"], 1)
        self.assertEqual(out2[0]["result"], "FILLED")

    def test_e13_symbol_state_isolation(self):
        """(E13) Sembol state izolasyonu: bir sembolün flip'i diğerinin
        direction/pending/last_bar'ını etkilemez."""
        now = T0 + 60_000
        self._fresh_marks(now)
        down = self._hist(T0 - BAR, 200)
        up = [(ot, 100.0, 100.0 + 0.5 * i, 1.0)
              for i, ot in enumerate(
                  [T0 - (199 - i) * BAR for i in range(200)])]
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": down, "BTCUSDT": up})
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), -1)
        self.assertEqual(self.lc.engine.direction("BTCUSDT"), 1)
        # ETH'te flip (yükseliş) — BTC etkilenmemeli
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2, px=250.0)
        self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": down + [(T0, 100.0, 250.0, 1.0)],
            "BTCUSDT": up})
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), 1)
        self.assertEqual(self.lc.engine.direction("BTCUSDT"), 1)
        self.assertEqual(self.lc.engine.pending("BTCUSDT"), 0)
        # BTC'nin T0 barı tik2'de kapandı ve CANLI işlendi (ETH'ten bağımsız)
        self.assertEqual(self.lc._last_bar["BTCUSDT"], T0)
        cyc = self._last_cycle()
        btc = self._symbol_state(cyc, "BTCUSDT")
        self.assertFalse(btc["target_changed"])

    def test_e14_restart_engine_state_consistency(self):
        """(E14) Restart öncesi/sonrası engine state tutarlı: direction,
        bar sayısı, sigma aynı kalır (replay deterministik)."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        sig_before = self.lc.engine.sigma("ETHUSDT")
        lc2 = make_canary(self.tmp.name, self.ex)
        prime(lc2)
        now2 = T0 + 120_000
        self._fresh_marks(now2)
        lc2.strategy_tick(now_ms=now2, klines_by_sym={"ETHUSDT": hist})
        self.assertAlmostEqual(lc2.engine.sigma("ETHUSDT"), sig_before,
                               places=9)
        self.assertEqual(lc2.engine.direction("ETHUSDT"),
                         self.lc.engine.direction("ETHUSDT"))

    # =========================================================================
    # §10/C15-C16 — KANONİK §7 SIRA KORUMASI (BLOK31.1 düzeltmesi)
    # =========================================================================

    def test_c15_overdue_flip_consumed_before_end_bar(self):
        """(C15) Penceresi kesinlikle kapanmış due flip end_bar'dan ÖNCE
        MISSED olarak tüketilir: dir ilerler, MISSED_FLIP kaydı yazılır,
        emir GÖNDERİLMEZ."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        # due flip: karar barı çok eski (pencere kapandı) + momentum geri döner
        S = self.lc.engine._state("ETHUSDT")
        S["pend"] = 1
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = T0 - 3 * BAR
        S["pend_price"] = 100.0
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2)
        # bar T0 POZİTİF momentumla kapanır (guard +1 flip'ini tükettikten
        # sonra dir=+1; sig=+1 == dir → yeni flip doğmaz)
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(out, [])
        missed = [r for r in self._orders_log()
                  if r["kind"] == "MISSED_FLIP"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["source"], "canonical_order_guard")
        self.assertIn("karar penceresi kapandı", missed[0]["reason"])
        self.assertEqual(self._entry_posts(), [])
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), 1)
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "SIGNAL_STALE")
        self.assertTrue(st["flip_detected"])

    def test_c16_whipsaw_catchup_preserves_canonical_target(self):
        """(C16) Çoklu bar yakalamada momentum geri dönerse (whipsaw):
        kanonik akış due flip'i tüketir (dir ilerler) ve YENİ ters flip
        doğar → taze fiyatla değerlendirilir. Eski kodda due flip
        sessizce iptal olup ters sinyal KAYBOLUYORDU."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        # due flip +1 (penceresi kapandı): fiyat alınamadı, Tik 1'de hayatta kaldı
        S = self.lc.engine._state("ETHUSDT")
        S["pend"] = 1
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = T0 - 2 * BAR
        S["pend_price"] = 100.0
        # Tik: bar T0 kapandı; momentum NEGATİF (dir=-1 ile aynı işaret).
        # Kanonik: +1 flip T0 açılışında icra edilirdi (pencere kapandı →
        # MISSED), sonra T0 kapanışı -1 flip'i üretirdi → taze fiyatla girer.
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2)
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 99.0, 1.0)]})
        # +1 flip MISSED tüketildi (dir -1 → +1), T0 kapanışı -1 flip'i
        # doğdu ve karar anında taze fiyatla İCRA edildi (dir → -1)
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), -1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["direction"], -1)         # YENİ ters sinyal
        self.assertEqual(out[0]["bar_ts"], T0)
        self.assertEqual(out[0]["result"], "FILLED")
        missed = [r for r in self._orders_log()
                  if r["kind"] == "MISSED_FLIP"]
        self.assertEqual(len(missed), 1)

    def test_c17_boundary_price_failure_misses_flip(self):
        """(C17) İcra barının KAPANIŞ anında (karar yaşı == 1 bar, sınırı
        geçerli) taze fiyat alınamazsa flip MISSED tüketilir — end_bar'ın
        sig==dir sessiz iptaline bırakılmaz. Eski kodda bu flip momentum
        geri dönerse İZSİZ kayboluyordu; şimdi görünür MISSED kaydıdır."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        # due flip +1: karar barı T0-BAR → icra barı T0; T0 şimdi kapanıyor
        S = self.lc.engine._state("ETHUSDT")
        S["pend"] = 1
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = T0 - BAR
        S["pend_price"] = 100.0
        now2 = T0 + BAR                    # sınır anı: yaş == 1 bar (geçerli)
        self._fresh_marks(now2)
        self.ex.book_ticker_time["ETHUSDT"] = now2 - 31_000   # taze DEĞİL
        # bar T0 POZİTİF momentumla kapanır (tüketim sonrası dir=+1;
        # sig=+1 == dir → yeni flip doğmaz → out tamamen boş)
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(out, [])
        self.assertEqual(self._entry_posts(), [])
        # önce taze fiyatla DENENDİ (retry kaydı), alınamayınca MISSED tüketildi
        self.assertTrue(any(r["kind"] == "FLIP_PRICE_RETRY"
                            for r in self._orders_log()))
        missed = [r for r in self._orders_log()
                  if r["kind"] == "MISSED_FLIP"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["source"], "canonical_order_guard")
        self.assertIn("karar penceresi kapandı", missed[0]["reason"])
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)
        self.assertEqual(self.lc.engine.direction("ETHUSDT"), 1)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "SIGNAL_STALE")
        self.assertTrue(st["flip_detected"])

    # =========================================================================
    # §10/O31-O36 — GÖZLENENEBİLİRLİK
    # =========================================================================

    def test_o31_cycle_before_after_consistency(self):
        """(O31) Cycle35 tutarsızlığı düzeltmesi: her kayıt BEFORE ve AFTER
        taşır; yeni mumun işlenip işlenmediği tek bakışta anlaşılır."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2)
        self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 99.0, 1.0)]})
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertIn("last_processed_bar_before", st)
        self.assertIn("last_processed_bar_after", st)
        self.assertEqual(st["last_processed_bar_before"], T0 - BAR)
        self.assertEqual(st["last_processed_bar_after"], T0)
        self.assertEqual(st["latest_closed_bar"], T0)

    def test_o32_warmup_done_vs_cycle_consistency(self):
        """(O32) WARMUP_DONE kaydı ile döngü warmup alanı TUTARLI:
        all_warm=true ⟺ warmup_complete=true ve warmup=false."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        warm = [r for r in self._orders_log() if r["kind"] == "WARMUP_DONE"][-1]
        cyc = self._last_cycle()
        for row in cyc["symbols"]:
            s = row["symbol"]
            if s in warm["symbols_warmed"] and warm["symbols_warmed"][s]:
                self.assertTrue(row["warmup_complete"])
                self.assertFalse(row["warmup"])
            else:
                self.assertFalse(row["warmup_complete"])
                self.assertTrue(row["warmup"])
                self.assertIn("warmup_missing_bars", row)

    def test_o33_cycle_gate_fields_present(self):
        """(O33) Başarılı strateji döngüsü kaydı kapı durumlarını taşır:
        entry_paused, halt_state, rm_status, reconciliation_status,
        feed_status, fetch_errors."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": self._hist(T0 - BAR, 200)})
        cyc = self._last_cycle()
        self.assertIs(cyc["entry_paused"], False)
        self.assertEqual(cyc["halt_state"], "CANARY_ACTIVE")
        self.assertEqual(cyc["rm_status"], "ACTIVE")
        self.assertEqual(cyc["reconciliation_status"], "1")
        self.assertEqual(cyc["feed_status"], "OK")
        self.assertEqual(cyc["fetch_errors"], {})

    def test_o34_empty_klines_is_data_error(self):
        """(O34) Boş kline yanıtı DATA_FETCH_ERROR'dur — NO_SIGNAL olarak
        raporlanmaz (fail-closed ayrıştırma)."""
        now = T0 + 60_000
        self._fresh_marks(now)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": []})
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "DATA_FETCH_ERROR")
        self.assertIn("boş", st["decision_reason"])
        self.assertEqual(self._entry_posts(), [])

    def test_o35_no_signal_vs_entry_blocked_distinction(self):
        """(O35) NO_SIGNAL/TARGET_UNCHANGED ile ENTRY_BLOCKED ayrık ve
        ayırt edilebilir: sinyal yokken entry alanları YOK; PAUSE'ta
        sinyal SIGNAL_VALID + final_decision ENTRY_BLOCKED."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2)
        self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 99.0, 1.0)]})
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "TARGET_UNCHANGED")
        self.assertNotIn("entry_result", st)
        self.assertNotIn("entry_blockers", st)
        self.assertEqual(st["final_decision"], "TARGET_UNCHANGED")
        # PAUSE + geçerli sinyal → SIGNAL_VALID + ENTRY_BLOCKED
        from canary_live import set_entry_pause
        set_entry_pause(self.lc.db_path, True, "blok31.1 test", "tester")
        now3 = T0 + 2 * BAR + 60_000
        self._fresh_marks(now3, px=250.0)
        out = self.lc.strategy_tick(now_ms=now3, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 99.0, 1.0),
                               (T0 + BAR, 100.0, 250.0, 1.0)]})
        self.assertEqual(out[0]["result"], "BLOCKED")
        st2 = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st2["decision"], "SIGNAL_VALID")
        self.assertEqual(st2["entry_result"], "BLOCKED")
        self.assertEqual(st2["final_decision"], "ENTRY_BLOCKED")
        self.assertTrue(any("ENTRY_PAUSED" in b
                            for b in st2["entry_blockers"]))

    def test_o36_filled_entry_final_decision(self):
        """(O36) Fill sonucu final_decision=FILL_CONFIRMED + koruma durumu
        döngü kaydında görünür."""
        now = T0 + 60_000
        self._fresh_marks(now)
        hist = self._hist(T0 - BAR, 200)
        self.lc.strategy_tick(now_ms=now, klines_by_sym={"ETHUSDT": hist})
        now2 = T0 + BAR + 60_000
        self._fresh_marks(now2, px=250.0)
        out = self.lc.strategy_tick(now_ms=now2, klines_by_sym={
            "ETHUSDT": hist + [(T0, 100.0, 250.0, 1.0)]})
        self.assertEqual(out[0]["result"], "FILLED")
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["final_decision"], "FILL_CONFIRMED")
        self.assertEqual(st["protection_status"], "PROTECTED")
        self.assertEqual(st["sizing_result"], "0.395")   # floor(99/250.05/0.001)
        self.assertEqual(st["price_source"], "bookTicker")


if __name__ == "__main__":
    unittest.main()
