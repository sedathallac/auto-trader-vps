"""BLOK28.1 §11 — POZİSYON ZARARI TESTLERİ (mock/fixture; ağ çağrısı YOK).

-5 USDT pozisyon zararı eşiği (§2): -4.99 / -5.00 / -5.01 sınırları, kısmi
çıkış, UNKNOWN çıkış (körlemesine tekrar POST edilmez), API zaman aşımı,
mark staleness (fail-closed), çıkış/stop yarışı, çift kapanış koruması,
SAFE_EXIT sırasında yeni risk yasağı, F-g (çıkış gerçek borsa miktarını
okur).

Matematik (fixture, BLOK29.1): ETHUSDT qty 1.0 @100, komisyon 0.0005 →
giriş komisyonu 0.05. Zarar = (100-m)*1.0 + 0.05:
  mark 95.06 → -4.99 (tetik YOK)   mark 95.05 → -5.00 (tetik)
  mark 95.04 → -5.01 (tetik)
Koruma stop'u 95.10 (bütçe 5 − 0.05 − 0.05 = 4.90 → 4.9 puan). Stop'un
atlamaması için saf -5 eşik testlerinde koruma dışarıdan iptal edilir;
borsa-korumalı yarış ayrı testte.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0)

BAR = 4 * 3600 * 1000


def cancel_protection(ex, cid):
    """Korumayı 'dışarıdan' iptal et (borsada bir başkası sildi — senaryo)."""
    ex.algos[cid]["status"] = "CANCELED"


class RiskExitTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.tmp.cleanup()

    def _lp(self, pos_id):
        return self.lc._live_pos_row(pos_id)

    def _exit_posts(self):
        return [c for c in self.ex.post_calls
                if c["method"] == "new_order" and c["reduce_only"]]

    # ---- §11: -4.99 / -5.00 / -5.01 eşikleri --------------------------------

    def test_loss_minus_4_99_no_safe_exit(self):
        rec = open_one(self.lc)                      # FILLED + PROTECTED
        self.assertEqual(rec["result"], "FILLED")
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)                   # < 30s koruma kadansı
        self.ex.set_mark("ETHUSDT", 95.06)           # net zarar -4.99
        cyc = self.lc.risk_tick()
        self.assertEqual(cyc["risk_actions"], [])    # eşik ALTINDA: aksiyon yok
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "OPEN")
        self.assertEqual(self._lp(rec["pos_id"])["exit_status"], "NONE")
        self.assertEqual(self._exit_posts(), [])

    def test_loss_minus_5_00_safe_exit_closes(self):
        rec = open_one(self.lc)
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)           # net zarar -5.00
        cyc = self.lc.risk_tick()
        self.assertIn({"pos_id": rec["pos_id"], "action": "SAFE_EXIT"},
                      cyc["risk_actions"])
        # pozisyon CLOSED + flat doğrulandı + çıkış reduce-only MARKET
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(self._lp(rec["pos_id"])["exit_status"],
                         "EXIT_CONFIRMED")
        self.assertEqual(len(self._exit_posts()), 1)
        self.assertTrue(self._exit_posts()[0]["reduce_only"])
        self.assertAlmostEqual(self.ex.positions["ETHUSDT"]["positionAmt"],
                               0.0, places=9)
        # zarar sınırı TETİK, cap değil: market çıkış ~-5.02 (gap/slippage
        # sözkonusu — §0 feragatnamesi)
        loss = self.lc.rm.total_net_loss(self.clock())
        self.assertGreaterEqual(loss, 4.9)
        self.assertLessEqual(loss, 5.2)

    def test_loss_minus_5_01_safe_exit_closes(self):
        rec = open_one(self.lc)
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.04)           # net zarar -5.01
        self.lc.risk_tick()
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(len(self._exit_posts()), 1)

    # ---- §11: UNKNOWN çıkış — körlemesine tekrar POST edilmez -----------------

    def test_exit_unknown_resolved_by_query_no_blind_repost(self):
        rec = open_one(self.lc)
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)
        self.lc.rm.update_mark("ETHUSDT", 95.05, self.clock())
        self.ex.timeout_client_ids.add("C281X1n1")   # UYGULANIR + timeout
        r = self.lc.initiate_risk_exit(rec["pos_id"], "POSITION_LOSS_LIMIT")
        self.assertEqual(r["status"], "EXIT_UNKNOWN")
        self.assertEqual(self._lp(rec["pos_id"])["exit_status"],
                         "EXIT_UNKNOWN")
        self.assertEqual(len(self._exit_posts()), 1)
        # çözüm: client id ile SORGU (yeniden POST değil)
        self.clock.advance(10_000)
        self.lc.risk_tick()
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(self._lp(rec["pos_id"])["exit_status"],
                         "EXIT_CONFIRMED")
        self.assertEqual(len(self._exit_posts()), 1)   # TEK POST — kanıt
        o = self.lc._order_row("C281X1n1")
        self.assertEqual(o["status"], "FILLED")

    def test_exit_timeout_not_applied_new_intent_for_verified_remainder(self):
        rec = open_one(self.lc)
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)
        self.lc.rm.update_mark("ETHUSDT", 95.05, self.clock())
        self.ex.no_apply_timeouts.add("C281X1n1")    # HİÇ uygulanmadı
        r = self.lc.initiate_risk_exit(rec["pos_id"], "POSITION_LOSS_LIMIT")
        self.assertEqual(r["status"], "EXIT_UNKNOWN")
        # sorgu → -2011 (emir yok) → positionRisk → hâlâ açık → UNCONFIRMED
        self.lc.risk_tick()
        lp = self._lp(rec["pos_id"])
        self.assertIn(lp["exit_status"],
                      ("EXIT_PENDING_UNCONFIRMED", "EXIT_UNKNOWN"))
        # çözüm: DOĞRULANMIŞ kalan için YENİ idempotent intent (§2.3 izinli)
        self.clock.advance(10_000)
        self.lc.risk_tick()
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        cids = [c["client_order_id"] for c in self._exit_posts()]
        self.assertEqual(cids, ["C281X1n1", "C281X1n2"])  # yeni intent = n2

    def test_exit_partial_fill_remainder_tracked(self):
        rec = open_one(self.lc)
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)
        self.lc.rm.update_mark("ETHUSDT", 95.05, self.clock())
        self.ex.partial_fill_qty["C281X1n1"] = 0.2    # 1.0'ın 0.2'si dolar
        self.lc.rm.enforce(self.clock())              # SAFE_EXIT işaretle
        self.lc.initiate_risk_exit(rec["pos_id"], "POSITION_LOSS_LIMIT")
        lp = self._lp(rec["pos_id"])
        self.assertEqual(lp["exit_status"], "EXIT_PARTIAL")
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "SAFE_EXIT")  # CLOSED YAZILMADI (F-b)
        # kalan 0.8 → sonraki tick: sorgu EXPIRED → yeni intent 0.800
        self.clock.advance(10_000)
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "CLOSED")
        second = [c for c in self._exit_posts()
                  if c["client_order_id"] == "C281X1n2"]
        self.assertEqual(len(second), 1)
        self.assertEqual(float(second[0]["quantity"]), 0.8)

    # ---- §11: API kesintisi / mark staleness ----------------------------------

    def test_mark_stale_fail_closed_no_exit_no_entry(self):
        rec = open_one(self.lc)
        # mark 31s eski → hesap YAPILAMIYOR (fail-closed; kör çıkış yok);
        # mark stop üstünde seçildi (yalnızca staleness testi)
        self.ex.set_mark("ETHUSDT", 96.0, ts=self.clock() - 31_000)
        cyc = self.lc.risk_tick()
        self.assertIn({"pos_id": rec["pos_id"],
                       "action": "FAIL_CLOSED_NO_DATA"}, cyc["risk_actions"])
        self.assertEqual(self._exit_posts(), [])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "OPEN")
        # yeni giriş de yok
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + BAR,
                                self.clock())
        self.assertTrue(out[0]["result"].startswith("REJECTED"))
        self.assertIn("FAIL_CLOSED", out[0]["result"])

    def test_exit_feed_down_pending_unconfirmed_never_claims_flat(self):
        rec = open_one(self.lc)
        self.ex.outage = True
        r = self.lc.initiate_risk_exit(rec["pos_id"], "POSITION_LOSS_LIMIT")
        # kesinti: positionRisk okunamıyor → flat İDDİA EDİLMEZ
        self.assertEqual(r["status"], "EXIT_PENDING_UNCONFIRMED")
        self.assertEqual(self._exit_posts(), [])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "OPEN")

    # ---- §11: çıkış/stop yarışı + çift kapanış ---------------------------------

    def test_exit_stop_race_exchange_protection_closes_first(self):
        rec = open_one(self.lc)
        self.clock.advance(60_000)                    # trade ts > entry ts
        self.ex.set_mark("ETHUSDT", 90.03)            # stop 95.10 altını geçer
        cyc = self.lc.risk_tick()
        # borsa stop'u premium_index sırasında tetiklendi (95.10'dan)
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")     # userTrades'ten çözüldü
        self.assertAlmostEqual(pos["exit_px"], 95.10, places=6)
        self.assertEqual(self._lp(rec["pos_id"])["protection_status"],
                         "PROTECTION_TRIGGERED")
        # market çıkış POST EDİLMEDİ (stop kazandı) — çift kapanış yok
        self.assertEqual(self._exit_posts(), [])
        self.assertIn({"pos_id": rec["pos_id"], "action": "SAFE_EXIT"},
                      cyc["risk_actions"])

    def test_double_close_reduceonly_rejected_no_reverse_position(self):
        rec = open_one(self.lc)
        self.clock.advance(60_000)
        self.ex.set_mark("ETHUSDT", 90.03)
        self.lc.risk_tick()                           # stop kapattı → CLOSED
        # geç/tekrar çıkış denemesi: borsa -2022 reddi → ters pozisyon YOK
        self.lc._submit_exit(rec["pos_id"], 1.0, "geç_çıkış_denemesi")
        o = self.lc._order_row("C281X1n1")
        self.assertEqual(o["status"], "REJECTED")
        self.assertEqual(self._lp(rec["pos_id"])["exit_status"],
                         "EXIT_PENDING_UNCONFIRMED")
        self.lc._progress_exit(rec["pos_id"])
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")     # defter ezilmedi
        self.assertAlmostEqual(pos["exit_px"], 95.10, places=6)

    # ---- §2: SAFE_EXIT sırasında yeni risk yasağı ------------------------------

    def test_safe_exit_blocks_new_risk_on_symbol(self):
        """BLOK29.1 (100/100): LiveCanary tam-boyut girişi açık pozisyonla
        her durumda önce toplam kapıdan reddedildiğinden kural 4 (SAFE_EXIT
        işaretli sembolde yeni risk yasağı) RiskManager düzeyinde
        doğrulanır — küçük pozisyon + küçük yeni emir kapıları geçer."""
        from canary_risk import OrderRejected
        self.lc.rm.submit_order("ETHUSDT", 1, 20.0, 100.0, self.clock())
        self.lc.rm.update_mark("ETHUSDT", 75.0, self.clock())  # zarar ≥ -5
        self.lc.rm.enforce(self.clock())                       # SAFE_EXIT işaretle
        pos = self.lc.rm.open_positions()[0]
        self.assertEqual(pos["status"], "SAFE_EXIT")
        # aynı sembolde yeni risk: kural 4 reddi
        with self.assertRaises(OrderRejected) as cm:
            self.lc.rm.check_order("ETHUSDT", 1, 10.0, self.clock())
        self.assertIn("pozisyon net zararı", str(cm.exception))
        self.assertIn("SAFE_EXIT", str(cm.exception))

    # ---- F-g: çıkış gerçek borsa miktarını okur ---------------------------------

    def test_exit_uses_exchange_qty_not_ledger(self):
        rec = open_one(self.lc)                       # defter: 1.0
        # borsada pozisyon 0.4'e düşmüş (dış müdahale/kısmi kapanış)
        self.ex.set_position("ETHUSDT", 0.4, 100.0)
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)
        self.lc.rm.update_mark("ETHUSDT", 95.05, self.clock())
        self.lc.initiate_risk_exit(rec["pos_id"], "POSITION_LOSS_LIMIT")
        posts = self._exit_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(float(posts[0]["quantity"]), 0.4)  # borsa gerçeği


if __name__ == "__main__":
    unittest.main()
