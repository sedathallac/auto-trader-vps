"""BLOK31 §10 — GİRİŞ HATTI (ENTRY PIPELINE) TESTLERİ (FakeExchange; ağ
çağrısı YOK, gerçek emir YOK).

BLOK31'in düzelttiği davranışların doğrudan kanıtı:
  - sinyal KARAR ANINDA (bar kapanışı = sonraki bar açılışı) taze
    bookTicker fiyatıyla değerlendirilir (BUY→ask, SELL→bid) — eski bar
    açılış fiyatıyla ~4 saat sonra MARKET gönderme hatası YOK
  - fiyat alınamazsa flip TUTULUR (retry); pencere geçtiyse/sapma
    aşıldıysa MISSED (fail-closed; eski sinyal asla oynatılmaz)
  - qty icra ANI fiyatından boyutlanır (ENTRY_SIZE_BUFFER); 100 USDT
    SERT cap (eski %1 tolerans YOK)
  - her strateji döngüsü strategy_cycles.jsonl'ye görünür şekilde yazılır

§10 madde eşlemesi (bu dosya): 1-10 (sinyal/bar), 11-21 (fiyat/boyut),
22-32 (emir zinciri), 42-43 (pencere/ts ayrımı), 44-49 (gözlemlenebilirlik).
§10'un risk (33-38) ve yarış (39-41) maddeleri mevcut dosyalardadır:
test_blok28_1_risk_exit.py (-5 eşikleri), test_blok28_1_total_halt.py
(-10 HALT), test_blok30_entry_pause.py (PAUSE sırasında risk/çıkış),
test_blok28_1_notional.py (cap'ler, eşzamanlı yarış, 2-pozisyon),
test_blok28_1_first_position.py (aynı-bar kronoloji, restart dedup),
test_blok30_entry_pause.py (çapraz-süreç entry-gate kilidi).
"""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 T0)

BAR = 4 * 3600 * 1000

from canary_live import set_entry_pause
from canary_exchange import ExchangeUnavailable, BinanceApiError


class Blok31EntryPipelineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- yardımcılar -----------------------------------------------------------

    def _warmup(self, now, bars=200):
        """Gerçekten TAMAMLANAN warmup (BLOK31.1): 3 barlık sabit kline
        dizisi engine'i asla ısıtamıyordu (n=3 < lookback 180, var=0) —
        eski kod bu durumu NO_NEW_CLOSED_BAR olarak yanlış etiketliyordu.
        Sürekli DÜŞEN kapanışlar (30→10): n=200 ≥ 180, var>0, replay sonu
        dir=-1, pend=0 (warm replay invariant'ı: dir == son momentum).
        Testlerin enjekte ettiği +1 flip böylece GERÇEK flip'tir (dir=-1 →
        +1); `_flip`'in icra barı kapanışı (101) 180 bar önceki kapanışın
        (~28) üzerine çıkar → momentum pozitife döner → end_bar karşı-flip
        ÜRETMEZ (test_32'nin genuine-sinyal deseni ile aynı)."""
        kl = {s: [(now - bars * BAR + i * BAR, 100.0, 30.0 - 0.1 * i, 1.0)
                  for i in range(bars)] for s in self.ex.symbols}
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

    def _fresh_marks(self, now, px=100.0):
        self.clock.t = now
        for s in self.ex.symbols:
            self.ex.set_mark(s, px)
            self.lc.rm.update_mark(s, px, now)

    def _inject_pend(self, symbol, direction, signal_bar_ts, price=100.0):
        """Beyaz-kutu flip enjeksiyonu (engine durumu)."""
        S = self.lc.engine._state(symbol)
        S["pend"] = direction
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = signal_bar_ts
        S["pend_price"] = price

    def _flip(self, symbol, direction, bar_ot, now, price=100.0):
        """Tek sembol flip'i: pend bar_ot−BAR kararında verildi, bar_ot
        açılışında (= karar anı) icra edilir."""
        self._inject_pend(symbol, direction, bar_ot - BAR, price)
        kl = {symbol: [(bar_ot, 100.0, 101.0, 1.0)]}
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

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

    # =========================================================================
    # §10/1-10 — SİNYAL ve BAR
    # =========================================================================

    def test_01_unclosed_bar_never_evaluated(self):
        """(1) AÇIK mum (ot+BAR > now) asla sinyal girdisi DEĞİLDİR."""
        self._warmup(T0)
        now = T0 + BAR + 60_000
        self._fresh_marks(now)
        open_ot = now - BAR + 60_000            # henüz kapanmamış bar
        kl = {"ETHUSDT": [(open_ot, 100.0, 101.0, 1.0)]}
        out = self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertIsNone(st["latest_closed_bar"])
        self.assertEqual(st["new_bar_count"], 0)
        self.assertEqual(out, [])
        self.assertEqual(self._entry_posts(), [])

    def test_02_closed_bar_processed_exactly_once(self):
        """(2) Aynı kapalı bar ikinci tikte YENİ sayılmaz (kalıcı last_bar).
        (Bar kapanışı warmup serisini sürdürür → momentum flip'i YOK —
        bu test bar-dedup'ı izoler; genuine flip davranışı test_32'de.)"""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self.lc.strategy_tick(
            now_ms=now, klines_by_sym={"ETHUSDT": [(T0 + BAR, 10.0, 10.0,
                                                    1.0)]})
        self.assertEqual(out, [])               # flip yok, bar işlendi
        now2 = now + 60_000
        self._fresh_marks(now2)
        self.lc.strategy_tick(
            now_ms=now2, klines_by_sym={"ETHUSDT": [(T0 + BAR, 10.0, 10.0,
                                                     1.0)]})
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["new_bar_count"], 0)
        self.assertEqual(st["decision"], "NO_NEW_CLOSED_BAR")
        self.assertEqual(st["last_processed_bar_after"], T0 + BAR)

    def test_03_pre_t0_decision_blocked(self):
        """(3) Karar anı ≤ T0 olan sinyal emir oluşturAMAZ (T0 kapısı)."""
        self._warmup(T0)
        now = T0 + BAR
        self._fresh_marks(now)
        # pend_bar_ts = T0−BAR → karar T0'da (≤ t0) → BLOCKED
        out = self._flip("ETHUSDT", 1, T0, now)
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("T0 öncesi", " ".join(out[0]["blockers"]))
        self.assertEqual(self._entry_posts(), [])

    def test_04_first_post_t0_decision_eligible(self):
        """(4) BLOK31 düzeltmesi: T0'dan ÖNCE açılıp T0'dan SONRA kapanan
        barın kararı (T0 < karar < T0+BAR) İLK GEÇERLİ sinyaldir — emir
        oluşturur. (Karar tam T0'da olan bar hâlâ eski sayılır — madde 3.)"""
        self._warmup(T0)
        now = T0 + BAR
        self._fresh_marks(now)
        # pend_bar_ts = T0−BAR/2 → karar T0+BAR/2 > T0 → GEÇERLİ
        out = self._flip("ETHUSDT", 1, T0 + BAR // 2, now)
        self.assertEqual(out[0]["result"], "FILLED")
        self.assertEqual(out[0]["bar_ts"], T0 - BAR // 2)   # karar barı
        pos = self.lc.rm._pos(out[0]["pos_id"])
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 98.9, places=6)

    def test_05_dedup_same_process_decision_key(self):
        """(5) Aynı KARAR barının sinyali aynı süreçte ikinci kez emir
        oluşturmaz — sig anahtarı karar barı ts'sidir."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        again = self.lc.try_entry("ETHUSDT", 1, 100.0, T0, now)
        self.assertEqual(again[0]["result"], "DUPLICATE_SIGNAL")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    def test_06_dedup_survives_restart_decision_key(self):
        """(6) Restart sonrası aynı karar barı sinyali yeniden işlenmez."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        lc2 = make_canary(self.tmp.name, self.ex)      # aynı DB
        again = lc2.try_entry("ETHUSDT", 1, 100.0, T0, now)
        self.assertEqual(again[0]["result"], "DUPLICATE_SIGNAL")
        self.assertEqual(len(lc2.rm.open_positions()), 1)

    def test_07_kline_fetch_error_fail_closed_and_logged(self):
        """(7) Kline GET hatası sembol bazında fail-closed: sessizce
        yutulmaz — döngü kaydına fetch_errors + DATA_FETCH_ERROR yazar,
        emir oluşturmaz."""
        now = T0 + BAR
        self._fresh_marks(now)
        self.ex.outage = True
        out = self.lc.strategy_tick(now_ms=now)        # klines_by_sym=None
        self.assertEqual(out, [])
        cyc = self._last_cycle()
        self.assertEqual(len(cyc["fetch_errors"]), len(self.ex.symbols))
        st = self._symbol_state(cyc, "ETHUSDT")
        self.assertEqual(st["decision"], "DATA_FETCH_ERROR")
        self.assertIn("ExchangeUnavailable", st["decision_reason"])
        self.assertEqual(self._entry_posts(), [])

    def test_08_other_symbols_continue_on_single_symbol_error(self):
        """(8) Tek sembolün kline hatası DİĞER sembollerin işlenmesini
        durdurmaz."""
        self._warmup(T0)
        now = T0 + BAR
        self._fresh_marks(now)
        self.ex.kline_data["SOLUSDT"] = [
            [T0 - BAR, "100.0", "0", "0", "100.0", "0", T0, "1.0"]]
        orig = self.ex.klines

        def flaky(symbol, interval="4h", limit=410):
            if symbol == "ETHUSDT":
                raise ExchangeUnavailable("tek sembol kesinti")
            return orig(symbol, interval, limit)
        self.ex.klines = flaky
        self.lc.strategy_tick(now_ms=now)
        self.ex.klines = orig
        cyc = self._last_cycle()
        self.assertEqual(list(cyc["fetch_errors"].keys()), ["ETHUSDT"])
        self.assertEqual(self._symbol_state(cyc, "ETHUSDT")["decision"],
                         "DATA_FETCH_ERROR")
        sol = self._symbol_state(cyc, "SOLUSDT")
        self.assertIsNotNone(sol.get("bar_count"))     # SOL işlendi
        self.assertIn(sol["decision"], ("TARGET_UNCHANGED", "NO_NEW_CLOSED_BAR",
                                        "WARMUP_INCOMPLETE",
                                        "WARMUP_COMPLETE"))

    def test_09_canonical_interval_4h_everywhere(self):
        """(9) Kanonik aralık 4h'dir (BLOK22 prereg donmuş tasarım) ve
        TÜM katmanlar aynı değeri kullanır."""
        import canary_live
        import h1_adapter
        import canary_exchange
        self.assertEqual(h1_adapter.BAR_INTERVAL_MS, 4 * 3600 * 1000)
        self.assertEqual(canary_live.BAR_MS, h1_adapter.BAR_INTERVAL_MS)
        self.assertEqual(h1_adapter.STRATEGY_ID, "H1_TSMOM_4H_L180")
        self.assertEqual(h1_adapter.LOOKBACK, 180)
        self.assertEqual(canary_live.STRATEGY_LOOP_S, 300.0)
        import inspect
        sig = inspect.signature(canary_exchange.BinanceFuturesClient.klines)
        self.assertEqual(sig.parameters["interval"].default, "4h")
        # döngü kayıtları da aralığı bildirir
        self._warmup(T0)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["interval"], "4h")

    def test_10_boundary_bar_not_double_processed(self):
        """(10) Sınır barı (ot == last_processed) yeniden işlenmez; bir
        sinyal yalnızca TEK sig kaydı üretir."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        n_sig = self.lc.db.execute(
            "SELECT COUNT(*) FROM live_state WHERE key LIKE 'sig:%'"
        ).fetchone()[0]
        self.assertEqual(n_sig, 1)

    # =========================================================================
    # §10/11-21 — FİYAT ve BOYUTLAMA
    # =========================================================================

    def test_11_executable_price_buy_ask_sell_bid(self):
        """(11) İcra fiyatı: BUY→ask, SELL→bid (bookTicker)."""
        now = T0 + BAR
        self._fresh_marks(now)
        px_l, meta_l = self.lc._executable_price("ETHUSDT", 1, now)
        px_s, meta_s = self.lc._executable_price("ETHUSDT", -1, now)
        self.assertAlmostEqual(px_l, 100.0 * 1.0002, places=9)   # ask
        self.assertAlmostEqual(px_s, 100.0 * 0.9998, places=9)   # bid
        self.assertGreater(px_l, px_s)
        self.assertAlmostEqual(meta_l["ask"], px_l, places=9)
        self.assertAlmostEqual(meta_s["bid"], px_s, places=9)

    def test_12_stale_bookticker_blocks_then_retries(self):
        """(12) KARAR ANINDA (bar kapanışı) bookTicker `time` 31s eski →
        giriş YOK, flip TUTULUR; karar penceresi İÇİNDE sonraki tikte taze
        fiyatla retry → emir. (BLOK31.1: flip karar barının işlendiği
        tikte end_bar'dan doğar; fiyat alınamazsa TUTULUR — yalnız icra
        barının kapanışına kadar. İcra barı kapanış anında fiyat hâlâ
        yoksa flip MISSED tüketilir; bkz. test_blok31_1 c17.)"""
        self._warmup(T0)
        now = T0 + BAR                     # karar barı T0 şimdi KAPANIYOR
        self._fresh_marks(now)
        self.ex.book_ticker_time["ETHUSDT"] = now - 31_000
        out = self.lc.strategy_tick(
            now_ms=now, klines_by_sym={"ETHUSDT": [(T0, 100.0, 101.0,
                                                    1.0)]})
        self.assertEqual(out, [])                     # emir YOK
        self.assertEqual(self._entry_posts(), [])
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "PRICE_UNAVAILABLE")
        self.assertIn("stale", st["decision_reason"])
        self.assertTrue(any(r["kind"] == "FLIP_PRICE_RETRY"
                            for r in self._orders_log()))
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 1)  # flip YAŞIYOR
        # taze fiyatla retry (karar penceresi içinde — age < 1 bar)
        del self.ex.book_ticker_time["ETHUSDT"]
        now2 = now + 60_000
        self._fresh_marks(now2)
        out2 = self.lc.strategy_tick(
            now_ms=now2, klines_by_sym={"ETHUSDT": [(T0, 100.0, 101.0,
                                                     1.0)]})
        self.assertEqual(out2[0]["result"], "FILLED")
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)

    def test_13_bookticker_failure_keeps_pend(self):
        """(13) bookTicker ÇAĞRISI hata verirse flip TUTULUR (kayıp yok),
        pencere içinde fiyat gelince icra edilir."""
        self._warmup(T0)
        now = T0 + BAR                     # karar barı T0 şimdi KAPANIYOR
        self._fresh_marks(now)
        self.ex.book_ticker_fail.add("ETHUSDT")
        out = self.lc.strategy_tick(
            now_ms=now, klines_by_sym={"ETHUSDT": [(T0, 100.0, 101.0,
                                                    1.0)]})
        self.assertEqual(out, [])
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "PRICE_UNAVAILABLE")
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 1)
        self.ex.book_ticker_fail.clear()
        now2 = now + 60_000
        self._fresh_marks(now2)
        out2 = self.lc.strategy_tick(
            now_ms=now2, klines_by_sym={"ETHUSDT": [(T0, 100.0, 101.0,
                                                     1.0)]})
        self.assertEqual(out2[0]["result"], "FILLED")

    def test_14_price_deviation_gate_misses_flip(self):
        """(14) Taze icra fiyatı sinyal kapanışından >%2 saparsa flip
        TÜKETİLİR + MISSED kaydı (fail-closed; uçuk fiyatla girilmez)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self.ex.set_mark("ETHUSDT", 103.0)            # ask ~103.2 → %3+ sapma
        self.lc.rm.update_mark("ETHUSDT", 103.0, now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now, price=100.0)
        self.assertEqual(out, [])
        self.assertEqual(self._entry_posts(), [])
        missed = [r for r in self._orders_log()
                  if r["kind"] == "MISSED_FLIP"]
        self.assertEqual(len(missed), 1)
        self.assertIn("sapma", missed[0]["reason"])
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "PRICE_DEVIATION")
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)  # tüketildi
        # retry YOK: sonraki tikte eski sinyal oynatılmaz
        now2 = now + 60_000
        self._fresh_marks(now2)
        out2 = self.lc.strategy_tick(
            now_ms=now2, klines_by_sym={"ETHUSDT": [(T0 + BAR, 100.0, 101.0,
                                                     1.0)]})
        self.assertEqual(out2, [])

    def test_15_qty_sized_from_execution_price(self):
        """(15) Qty İCRA ANI fiyatından boyutlanır — sinyal kapanışı/bar
        açılışı DEĞİL (kök neden 1'in doğrudan kanıtı)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)                        # ask = 100.02
        out = self._flip("ETHUSDT", 1, T0 + BAR, now, price=100.0)
        rec = out[0]
        self.assertEqual(rec["result"], "FILLED")
        self.assertAlmostEqual(rec["ref_px"], 100.02, places=9)  # icra anı
        self.assertEqual(rec["signal_px"], 100.0)               # sinyal kapanışı
        self.assertEqual(rec["qty"], "0.989")   # floor(99/100.02/0.001)*0.001
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertAlmostEqual(pos["units"], 0.989, places=9)
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 98.9, places=6)

    def test_16_step_rounding_from_execution_price(self):
        """(16) stepSize yuvarlaması icra fiyatına göre (SOLUSDT step 0.01)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("SOLUSDT", 1, T0 + BAR, now, price=100.0)
        rec = out[0]
        self.assertEqual(rec["result"], "FILLED")
        self.assertEqual(rec["qty"], "0.98")    # floor(99/100.02/0.01)*0.01

    def test_17_filter_violation_blocks_no_order(self):
        """(17) Borsa filtresi (minQty) karşılanamıyorsa giriş YOK — emir
        hiç gönderilmez, limit YÜKSELTİLMEZ."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now, px=120_000.0)
        self.ex.set_mark("BTCUSDT", 120_000.0)
        self.lc.rm.update_mark("BTCUSDT", 120_000.0, now)
        out = self._flip("BTCUSDT", 1, T0 + BAR, now, price=120_000.0)
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("minQty", " ".join(out[0]["blockers"]))
        self.assertEqual(self._entry_posts(), [])
        n = self.lc.db.execute(
            "SELECT COUNT(*) FROM live_orders WHERE kind='ENTRY'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_18_position_notional_hard_cap(self):
        """(18) Pozisyon notional ≤ 100 SERT cap — buffer'lı boyutlama
        (99 hedef) fill sonrası da aşmaz."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        pos = self.lc.rm._pos(out[0]["pos_id"])
        self.assertLessEqual(pos["units"] * pos["entry_px"],
                             100.0 + 1e-9)

    def test_19_total_notional_cap_rejects_second(self):
        """(19) Tam boyutlu pozisyon açıkken ikinci giriş TOPLAM kapıdan
        reddedilir (toplam ≤ 100)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self._flip("ETHUSDT", 1, T0 + BAR, now)
        now2 = T0 + 3 * BAR
        self._fresh_marks(now2)
        out2 = self._flip("SOLUSDT", 1, T0 + 2 * BAR, now2)
        self.assertIn("REJECTED", out2[0]["result"])
        self.assertIn("toplam notional", out2[0]["result"])
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        self.assertLessEqual(self.lc.rm.total_notional(now2), 100.0 + 1e-9)

    def test_20_fill_accounting_from_exchange_response(self):
        """(20) Defter GERÇEK borsa fill'inden yazılır: executedQty ve
        avgPrice — katsayı/varsayım YOK."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        rec = out[0]
        pos = self.lc.rm._pos(rec["pos_id"])
        o = self.lc._order_row(rec["entry_client_id"])
        self.assertEqual(o["status"], "FILLED")
        resp = json.loads(o["result"]) if isinstance(o["result"], str) \
            else o["result"]
        self.assertAlmostEqual(pos["units"],
                               float(resp["executedQty"]), places=9)
        self.assertAlmostEqual(pos["entry_px"],
                               float(resp["avgPrice"]), places=9)
        self.assertAlmostEqual(pos["entry_comm"],
                               sum(float(f["commission"])
                                   for f in resp["fills"]), places=9)

    def test_21_over_cap_fill_reduces_and_records_violation(self):
        """(21) Fill notional SERT cap'i aşarsa: reduce-only çıkış + açık
        ihlal kaydı — 'sınır içinde' gibi RAPORLANMAZ."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        # bookTicker taze ama fiyat fill anında 102'ye sıçradı (slippage):
        # qty 100.02 üzerinden 0.989 boyutlandı, fill 102'den → 100.88 > 100
        self.ex.set_mark("ETHUSDT", 102.0)
        self.lc.rm.update_mark("ETHUSDT", 102.0, now)
        orig_bt = self.ex.book_ticker

        def book_at_100(symbol):
            return {"symbol": symbol, "bidPrice": "99.98", "bidQty": "1.0",
                    "askPrice": "100.02", "askQty": "1.0",
                    "time": self.clock()}
        self.ex.book_ticker = book_at_100
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.ex.book_ticker = orig_bt
        rec = out[0]
        self.assertEqual(rec["result"],
                         "OVER_LIMIT_EXIT: fill notional limiti aştı")
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "CLOSED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        evs = [r[0] for r in self.lc.rm.db.execute(
            "SELECT detail FROM risk_events WHERE kind="
            "'OVER_LIMIT_NOTIONAL'").fetchall()]
        self.assertTrue(any("HARD cap" in e for e in evs))

    # =========================================================================
    # §10/22-32 — EMİR ZİNCİRİ
    # =========================================================================

    def test_22_intent_recorded_before_order_post(self):
        """(22) INTENT kaydı new_order POST'undan ÖNCE yazılır
        (idempotency anchor — POST başarısız olsa da emir izlenebilir)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        seen = {}
        orig = self.ex.new_order

        def probe(*a, **k):
            seen["row"] = self.lc._order_row("C281E1")
            return orig(*a, **k)
        self.ex.new_order = probe
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.ex.new_order = orig
        self.assertEqual(out[0]["result"], "FILLED")
        self.assertIsNotNone(seen["row"])            # POST anında INTENT var
        self.assertEqual(seen["row"]["kind"], "ENTRY")
        self.assertEqual(seen["row"]["status"], "INTENT")
        self.assertEqual(seen["row"]["client_id"], "C281E1")

    def test_23_client_order_id_format(self):
        """(23) clientOrderId sahiplik önekli ve deterministik art arda."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        cid = out[0]["entry_client_id"]
        self.assertTrue(re.fullmatch(r"C281E\d+", cid))

    def test_24_entry_order_params(self):
        """(24) Giriş emri: MARKET, doğru side, boyut, RESULT yanıt tipi,
        reduce-only DEĞİL."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self._flip("ETHUSDT", 1, T0 + BAR, now)
        posts = self._entry_posts()
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["symbol"], "ETHUSDT")
        self.assertEqual(p["side"], "BUY")
        self.assertEqual(p["quantity"], "0.989")
        self.assertIsNone(p["reduce_only"])
        self.assertEqual(p["client_order_id"], "C281E1")

    def test_25_entry_unknown_resolved_by_query(self):
        """(25) POST timeout (UnknownResult) → körlemesine tekrar POST
        EDİLMEZ; client id ile sorgu çözülür, fill deftere işlenir."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self.ex.timeout_client_ids.add("C281E1")
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertIn("ENTRY_UNKNOWN", out[0]["result"])
        self.assertEqual(len(self._entry_posts()), 1)   # TEK POST
        self.assertEqual(len(self.lc.rm.open_positions()), 0)
        now2 = now + 10_000
        self._fresh_marks(now2)
        self.lc.risk_tick()
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        self.assertEqual(self.lc._live_pos_row(o["pos_id"])
                         ["protection_status"], "PROTECTED")
        self.assertEqual(len(self._entry_posts()), 1)   # hâlâ TEK POST

    def test_26_entry_rejection_recorded(self):
        """(26) Borsa kesin reddi (BinanceApiError) → REJECTED kaydı,
        pozisyon YOK."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        orig = self.ex.new_order

        def reject(*a, **k):
            raise BinanceApiError(-4061, "Order's notional must be "
                                          "no smaller than minNotional.")
        self.ex.new_order = reject
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.ex.new_order = orig
        self.assertTrue(out[0]["result"].startswith("REJECTED"))
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "REJECTED")
        self.assertEqual(len(self.lc.rm.open_positions()), 0)

    def test_27_partial_entry_fill_pending_blocks_new(self):
        """(27) Kısmi fill → 'risk döngüsü izleyecek'; bekleyen giriş emri
        yeni girişleri bloklar (tek giriş zinciri)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self.ex.partial_fill_qty["C281E1"] = 0.5
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertIn("PARTIALLY_FILLED", out[0]["result"])
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "PARTIALLY_FILLED")
        # bekleyen giriş → yeni sinyal bloklu
        now2 = T0 + 3 * BAR
        self._fresh_marks(now2)
        out2 = self._flip("SOLUSDT", 1, T0 + 2 * BAR, now2)
        self.assertEqual(out2[0]["result"], "BLOCKED")
        self.assertIn("bekleyen giriş emri var",
                      " ".join(out2[0]["blockers"]))

    def test_29_stop_protection_after_fill(self):
        """(29) Gerçek fill sonrası STOP_MARKET closePosition=MARK_PRICE
        koruması borsada yerleşik."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        a = self.ex.algos[f"C281P{out[0]['pos_id']}"]
        self.assertEqual(a["type"], "STOP_MARKET")
        self.assertEqual(a["workingType"], "MARK_PRICE")
        self.assertEqual(a["closePosition"], "true")
        self.assertEqual(a["side"], "SELL")           # long → SELL stop

    def test_30_31_protection_verified_before_second_position(self):
        """(30+31) İkinci pozisyon ancak ilk fill+koruma DOĞRULANDIKTAN
        (PROTECTED) sonra açılabilir."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self.ex.timeout_client_ids.add("C281P1")      # koruma UNKNOWN
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        self.assertEqual(self.lc._live_pos_row(out[0]["pos_id"])
                         ["protection_status"], "PROTECTION_PENDING")
        now2 = T0 + 3 * BAR
        self._fresh_marks(now2)
        out2 = self._flip("SOLUSDT", 1, T0 + 2 * BAR, now2)
        self.assertEqual(out2[0]["result"], "BLOCKED")
        joined = " ".join(out2[0]["blockers"])
        self.assertIn("ilk pozisyonun fill+koruma doğrulaması", joined)
        # 30s doğrulama kadansı çözümü getirir
        self.clock.advance(60_000)
        for s in self.ex.symbols:
            self.ex.set_mark(s, 100.0)
        self.lc.risk_tick()
        self.assertEqual(self.lc._live_pos_row(out[0]["pos_id"])
                         ["protection_status"], "PROTECTED")
        # koruma doğrulandı → ikinci giriş artık kapılardan geçer (toplam
        # kapısından reddedilir — 100/100)
        now3 = T0 + 4 * BAR
        self._fresh_marks(now3)
        out3 = self._flip("SOLUSDT", -1, T0 + 3 * BAR, now3)
        self.assertIn("toplam notional", out3[0]["result"])

    def test_32_genuine_signal_end_to_end(self):
        """(32) UÇTAN UCA GERÇEK SİNYAL: 182 barlık gerçek momentum
        dizisinden (enjeksiyon YOK) flip doğar → karar anında taze fiyatla
        FILLED + PROTECTED."""
        # warmup: 182 bar sürekli DÜŞEN kapanış → engine dir=-1, pend=0
        n_warm = 182
        warm = [(T0 - (n_warm - i) * BAR, 100.0, 200.0 - 0.5 * i, 1.0)
                for i in range(n_warm)]
        self.lc.strategy_tick(now_ms=T0 - 60_000, klines_by_sym={
            "ETHUSDT": warm})
        S = self.lc.engine._state("ETHUSDT")
        self.assertEqual(S["dir"], -1)
        self.assertEqual(S["pend"], 0)
        # canlı bar: kapanış, 180 bar önceki kapanışın ÜZERİNE çıkar →
        # momentum pozitife döner → GERÇEK flip (+1)
        now = T0 + BAR + 60_000
        self._fresh_marks(now, px=200.0)
        live = [(T0, 150.0, 200.0, 1.0)]
        out = self.lc.strategy_tick(now_ms=now, klines_by_sym={
            "ETHUSDT": live})
        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertEqual(rec["result"], "FILLED")
        self.assertEqual(rec["direction"], 1)
        self.assertEqual(rec["bar_ts"], T0)           # karar barı
        self.assertAlmostEqual(rec["signal_px"], 200.0, places=9)
        self.assertAlmostEqual(rec["ref_px"], 200.0 * 1.0002, places=9)
        self.assertEqual(rec["qty"], "0.494")   # floor(99/200.04/0.001)*0.001
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 98.8, places=6)
        self.assertEqual(self.lc._live_pos_row(rec["pos_id"])
                         ["protection_status"], "PROTECTED")
        self.assertEqual(self.lc._get_state("first_position_verified"), "1")
        # sinyal kaydı gerçek: sig anahtarı karar barında
        sig = self.lc._get_state(f"sig:ETHUSDT:{T0}")
        self.assertEqual(sig, "1")

    # =========================================================================
    # §10/42-43 — KARAR PENCERESİ ve ZAMAN DAMGALARI
    # =========================================================================

    def test_42_window_gate_old_flip_missed(self):
        """(42) Karar penceresi (> 1 bar) kapandıysa flip MISSED — eski
        sinyal asla oynatılmaz (fail-closed)."""
        self._warmup(T0)
        now = T0 + 3 * BAR + 60_000
        self._fresh_marks(now)
        # pend_bar_ts = T0+BAR → karar T0+2BAR; yaşı BAR+60s > 1 bar
        out = self._flip("ETHUSDT", 1, T0 + 2 * BAR, now + 60_000)
        self.assertEqual(out, [])
        missed = [r for r in self._orders_log() if r["kind"] == "MISSED_FLIP"]
        self.assertEqual(len(missed), 1)
        self.assertIn("karar penceresi kapandı", missed[0]["reason"])
        self.assertEqual(self.lc.engine.pending("ETHUSDT"), 0)
        self.assertEqual(self._entry_posts(), [])

    def test_43_signal_vs_execution_price_separation(self):
        """(43) Kayıtta sinyal KAPANIŞI (signal_px) ve icra ANI fiyatı
        (ref_px) ayrı ayrı görünür; karar barı ts'si bar_ts'tir."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now, price=100.0)
        rec = out[0]
        self.assertEqual(rec["bar_ts"], T0)           # karar barı (dedup anahtarı)
        self.assertEqual(rec["signal_px"], 100.0)     # sinyal kapanışı
        self.assertAlmostEqual(rec["ref_px"], 100.02, places=9)  # icra anı
        self.assertNotEqual(rec["ref_px"], rec["signal_px"])

    # =========================================================================
    # §10/44-49 — GÖZLEMLENEBİLİRLİK
    # =========================================================================

    def test_44_strategy_cycle_logged_every_tick(self):
        """(44) Her strateji döngüsü strategy_cycles.jsonl'ye yazılır;
        cycle_id art arda artar."""
        self._warmup(T0)
        now = T0 + BAR
        self.lc.strategy_tick(now_ms=now)             # klines yok
        now2 = T0 + 2 * BAR
        self.lc.strategy_tick(now_ms=now2)
        cyc = self._cycles()
        self.assertEqual(len(cyc), 3)                 # warmup + 2 tik
        self.assertEqual([c["cycle_id"] for c in cyc], [1, 2, 3])
        for c in cyc:
            self.assertEqual(c["kind"], "STRATEGY_CYCLE")
            self.assertIn("fetch_errors", c)
            self.assertIn("symbols", c)
        self.assertEqual(len(cyc[-1]["symbols"]), len(self.ex.symbols))
        st = self._symbol_state(cyc[-1], "ETHUSDT")
        # BLOK31.1: boş kline yanıtı veri hatasıdır — NO_NEW_CLOSED_BAR
        # olarak sessizce geçilmez (FakeExchange kline_data boş → [])
        self.assertEqual(st["decision"], "DATA_FETCH_ERROR")
        self.assertIn("boş", st["decision_reason"])

    def test_45_fetch_error_states_in_cycle(self):
        """(45) Kline hatası döngü kaydında sembol bazlı görünür (madde 7
        ile aynı tık — burada kayıt içeriği)."""
        now = T0 + BAR
        self._fresh_marks(now)
        self.ex.outage = True
        self.lc.strategy_tick(now_ms=now)
        cyc = self._last_cycle()
        for s in self.ex.symbols:
            self.assertEqual(self._symbol_state(cyc, s)["decision"],
                             "DATA_FETCH_ERROR")

    def test_46_signal_and_blockers_visible_in_cycle(self):
        """(46) Geçerli sinyal + giriş reddi nedeni döngü kaydında görünür:
        PAUSE açıkken sinyal SIGNAL_VALID, giriş ENTRY_BLOCKED."""
        self._warmup(T0)
        set_entry_pause(self.lc.db_path, True, "blok31 gözlemlenebilirlik",
                        "tester")
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "BLOCKED")
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "SIGNAL_VALID")
        self.assertEqual(st["entry_result"], "BLOCKED")
        self.assertTrue(any("ENTRY_PAUSED" in b
                            for b in st["entry_blockers"]))
        self.assertIsNotNone(st.get("current_executable_price"))
        self.assertIsNotNone(st.get("price_age_ms"))

    def test_47_jsonl_files_all_lines_valid(self):
        """(47) Tüm JSONL kayıtları geçerli JSON'dur (ayrıştırma kanıtı)."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.ex.outage = True
        self.lc.strategy_tick(now_ms=now + 60_000)
        self.ex.outage = False
        names = [f for f in os.listdir(self.tmp.name) if f.endswith(".jsonl")]
        self.assertTrue(names)
        for name in names:
            with open(os.path.join(self.tmp.name, name)) as f:
                for line in f:
                    if line.strip():
                        json.loads(line)              # hata atmaz

    def test_48_no_secrets_in_logs(self):
        """(48) Kayıtlarda API anahtarı/parola İZİ YOK."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.lc.risk_tick()
        for name in os.listdir(self.tmp.name):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(self.tmp.name, name)) as f:
                blob = f.read()
            for secret in ("api_key", "apiKey", "APIKEY", "X-MBX",
                           "api_secret", "apiSecret", "secret=",
                           "signature="):
                self.assertNotIn(secret, blob)

    def test_49_price_age_and_states_visible(self):
        """(49) Başarılı flip sonrası döngü kaydında icra fiyatı + yaşı
        görünür; karar ve fiyat tazelikleri ölçülebilir."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self._flip("ETHUSDT", 1, T0 + BAR, now)
        st = self._symbol_state(self._last_cycle(), "ETHUSDT")
        self.assertEqual(st["decision"], "SIGNAL_VALID")
        self.assertAlmostEqual(st["current_executable_price"], 100.02,
                               places=9)
        self.assertLessEqual(st["price_age_ms"], 1_000)
        self.assertEqual(st["entry_result"], "FILLED")


if __name__ == "__main__":
    unittest.main()
