"""BLOK28.1 §11 — İLK POZİSYON / T0 KAPISI / SEÇİM POLİTİKASI TESTLERİ
(mock/fixture; ağ çağrısı YOK).

§7: 8 sembollük H1 koruması; eski -1x8 hedefleri EMİR OLUŞTURAMAZ (T0
kapısı); ilk GERÇEK flip T0 sonrası; restart dedup; 8 eşzamanlı sinyalde
maks 2 pozisyon (var olan deterministik kronolojik (bar_ts, symbol)
politikası — yeni kural icat edilmedi); strateji flip çıkışı reduce-only.
Tüm flip'ler H1SignalEngine beyaz-kutu pend enjeksiyonuyla üretilir.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 T0)

BAR = 4 * 3600 * 1000
FLIP_SYMS = ("ETHUSDT", "LINKUSDT", "SOLUSDT")


class FirstPositionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- yardımcılar -----------------------------------------------------------

    def _warmup(self, now):
        """İlk strategy_tick: tüm semboller warmup replay (kapalı barlar)."""
        kl = {s: [(now - 4 * BAR + i * BAR, 100.0, 100.0, 1.0)
                  for i in range(3)] for s in self.ex.symbols}
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

    def _fresh_marks(self, now):
        """Saati ileri al + markları tazele (risk_tick ÇAĞRILMAZ — koruma
        UNKNOWN senaryosunda arada çözülmesin diye)."""
        self.clock.t = now
        for s in self.ex.symbols:
            self.ex.set_mark(s, 100.0)
            self.lc.rm.update_mark(s, 100.0, now)

    def _inject_pend(self, symbol, direction, signal_bar_ts):
        """Beyaz-kutu flip enjeksiyonu: bir sonraki canlı bar açılışında
        begin_bar pend'i patlatır (H1 sinyal yolu)."""
        S = self.lc.engine._state(symbol)
        S["pend"] = direction
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = signal_bar_ts
        S["pend_price"] = 100.0

    def _flip(self, symbol, direction, bar_ot, now):
        """Tek sembol flip'i bar açılışında icra eder (strateji yolu)."""
        self._inject_pend(symbol, direction, bar_ot - BAR)
        kl = {symbol: [(bar_ot, 100.0, 101.0, 1.0)]}
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

    def _exit_posts(self):
        return [c for c in self.ex.post_calls
                if c["method"] == "new_order" and c["reduce_only"]]

    # ---- warmup: replay emirsiz ---------------------------------------------------

    def test_warmup_replay_produces_no_orders(self):
        out = self._warmup(T0)
        self.assertEqual(out, [])
        n = self.lc.db.execute(
            "SELECT COUNT(*) FROM live_orders").fetchone()[0]
        self.assertEqual(n, 0)
        self.assertEqual(self.ex.post_calls, [])
        # tüm semboller ısındı — sonraki barlar canlı sayılır
        self.assertEqual(len(self.lc._warmed), len(self.ex.symbols))

    # ---- T0 kapısı: eski -1x8 hedefleri emir oluşturamaz ----------------------------

    def test_pre_t0_flip_cannot_order(self):
        self._warmup(T0)
        self._fresh_marks(T0 + BAR)
        # bar T0'da AÇILDI → bar_ts = T0 <= t0 → eski sinyal, emir YOK
        out = self._flip("ETHUSDT", 1, T0, T0 + BAR)
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("T0 öncesi", " ".join(out[0]["blockers"]))
        self.assertEqual(self.ex.post_calls, [])
        n = self.lc.db.execute(
            "SELECT COUNT(*) FROM live_orders").fetchone()[0]
        self.assertEqual(n, 0)

    # ---- T0 sonrası ilk flip: FILLED + PROTECTED ------------------------------------

    def test_first_flip_after_t0_fills_and_protects(self):
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        pos_id = out[0]["pos_id"]
        self.assertEqual(self.lc._live_pos_row(pos_id)
                         ["protection_status"], "PROTECTED")
        self.assertEqual(self.lc._get_state("first_position_verified"), "1")
        # borsa koruması gerçekten yerleşik
        self.assertEqual(self.ex.algos[f"C281P{pos_id}"]["workingType"],
                         "MARK_PRICE")

    # ---- ikinci giriş: ilk fill+koruma doğrulanana dek bloke --------------------------

    def test_second_entry_blocked_until_first_verified(self):
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        self.ex.timeout_client_ids.add("C281P1")     # koruma UNKNOWN
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        self.assertEqual(self.lc._live_pos_row(out[0]["pos_id"])
                         ["protection_status"], "PROTECTION_PENDING")
        # ikinci sembol flip'i bloklu (ilk pozisyon doğrulanmadı)
        now2 = T0 + 3 * BAR
        self._fresh_marks(now2)
        out2 = self._flip("LINKUSDT", 1, T0 + 2 * BAR, now2)
        self.assertEqual(out2[0]["result"], "BLOCKED")
        joined = " ".join(out2[0]["blockers"])
        self.assertIn("ilk pozisyonun fill+koruma doğrulaması", joined)
        self.assertIn("PROTECTED değil", joined)
        # koruma doğrulama döngüsü (30s kadans) çözümü getirir
        self.clock.advance(60_000)
        for s in self.ex.symbols:
            self.ex.set_mark(s, 100.0)
        self.lc.risk_tick()
        self.assertEqual(self.lc._get_state("first_position_verified"), "1")
        # artık ikinci giriş serbest (bloklu deneme dir=1'e ilerlettiği
        # için retry flip'i ters yönde — begin_bar yalnız dir≠pend'i patlatır)
        now3 = T0 + 4 * BAR
        self._fresh_marks(now3)
        out3 = self._flip("LINKUSDT", -1, T0 + 3 * BAR, now3)
        self.assertEqual(out3[0]["result"], "FILLED")
        self.assertEqual(len(self.lc.rm.open_positions()), 2)

    # ---- sinyal dedup (§7: aynı sinyal ikinci kez emir oluşturamaz) -------------------

    def test_duplicate_signal_same_process(self):
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        again = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR, now)
        self.assertEqual(again[0]["result"], "DUPLICATE_SIGNAL")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    def test_duplicate_signal_survives_restart(self):
        """Restart sonrası aynı (symbol, bar_ts) sinyali yeniden işlenmez —
        kalıcı sig anahtarı DB'de."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        self.assertEqual(out[0]["result"], "FILLED")
        lc2 = make_canary(self.tmp.name, self.ex)    # aynı DB
        again = lc2.try_entry("ETHUSDT", 1, 100.0, T0 + BAR, now)
        self.assertEqual(again[0]["result"], "DUPLICATE_SIGNAL")
        self.assertEqual(len(lc2.rm.open_positions()), 1)

    # ---- eşzamanlı flip'ler: deterministik seçim + maks 2 ------------------------------

    def test_three_same_bar_flips_chronological_max_two(self):
        """8 sinyal durumunun minimal örneği: aynı barda 3 flip — kronolojik
        (bar_ts, symbol) sırayla işlenir, ilk 2 dolar, üçüncüsü ret."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        for s in FLIP_SYMS:
            self._inject_pend(s, 1, T0)
        kl = {s: [(T0 + BAR, 100.0, 101.0, 1.0)] for s in FLIP_SYMS}
        out = self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)
        results = {r["symbol"]: r["result"] for r in out}
        # kronolojik sıra: ETHUSDT < LINKUSDT < SOLUSDT
        self.assertEqual(results["ETHUSDT"], "FILLED")
        self.assertEqual(results["LINKUSDT"], "FILLED")
        self.assertIn("pozisyon sınırı", results["SOLUSDT"])
        self.assertIn("üçüncü pozisyona izin YOK", results["SOLUSDT"])
        self.assertEqual(len(self.lc.rm.open_positions()), 2)
        total = self.lc.rm.total_notional(now)
        self.assertLessEqual(total, 100.0 + 1e-9)

    # ---- strateji flip çıkışı: reduce-only, risk azaltan -------------------------------

    def test_strategy_flip_exit_reduce_only(self):
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)
        pos_id = out[0]["pos_id"]
        self.assertEqual(out[0]["result"], "FILLED")
        # ters flip: long → short (kapanış bacağı + açılış bacağı)
        now2 = T0 + 4 * BAR
        self._fresh_marks(now2)
        out2 = self._flip("ETHUSDT", -1, T0 + 3 * BAR, now2)
        # uzun pozisyon flip KAPANIŞI: reduce-only market çıkış + CLOSED
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["exit_reason"], "H1_FLIP_STRATEGY_EXIT")
        self.assertEqual(lp["exit_status"], "EXIT_CONFIRMED")
        self.assertEqual(self.lc.rm._pos(pos_id)["status"], "CLOSED")
        exits = self._exit_posts()
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["side"], "SELL")   # long kapanışı
        self.assertTrue(exits[0]["reduce_only"])
        # flip AÇILIŞI: short giriş aynı bar açılışında
        self.assertEqual(out2[0]["result"], "FILLED")
        self.assertEqual(out2[0]["direction"], -1)
        # ters pozisyon YOK — borsada tek (short) pozisyon
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], -0.5)
        self.assertEqual(len(self.lc.rm.open_positions()), 1)


if __name__ == "__main__":
    unittest.main()
