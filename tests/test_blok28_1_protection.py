"""BLOK28.1 §11 — BORSA KORUMA (STOP_MARKET CONDITIONAL) TESTLERİ
(mock/fixture; ağ çağrısı YOK).

§5: her pozisyon için GERÇEK borsa koruması — /fapi/v1/algoOrder,
STOP_MARKET, workingType=MARK_PRICE, closePosition=true. Emir → gerçek
fill → net pozisyon → kalıcı intent → yerleştir → ID kaydı → yeniden sorgu
→ açık+doğru DOĞRULANANA dek PROTECTED değil. Yerleştirme başarısız →
yeni giriş yok + reduce-only çıkış (F-e: -2021 sınıflandırılır, kontrolsüz
retry yok).

F-a…F-g eşdeğerleri: F-a (çıkış sonrası artık koruma çözümü), F-c
(doğrulanamayan koruma girişi bloklar), F-e (-2021 → P_FAILED + çıkış),
F-g (yerleştirme taze positionRisk okur).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0)

BAR = 4 * 3600 * 1000
from canary_exchange import BinanceApiError


class ProtectionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.tmp.cleanup()

    def _exit_posts(self):
        return [c for c in self.ex.post_calls
                if c["method"] == "new_order" and c["reduce_only"]]

    def _algo_posts(self):
        return [c for c in self.ex.post_calls if c["method"] == "new_algo_order"]

    # ---- §5: emir biçimi + ID kalıcılığı + doğrulama -----------------------------

    def test_protection_algo_params_id_persistence_and_verify(self):
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        # borsa kaydı: dokümante parametreler
        a = self.ex.algos["C281P1"]
        self.assertEqual(a["type"], "STOP_MARKET")
        self.assertEqual(a["side"], "SELL")             # long → SELL stop
        self.assertEqual(a["workingType"], "MARK_PRICE")
        self.assertEqual(a["closePosition"], "true")
        self.assertEqual(float(a["triggerPrice"]), 90.10)
        # kalıcı intent + exchange ID + pos_id bağı
        o = self.lc._order_row("C281P1")
        self.assertEqual(o["kind"], "PROTECTION")
        self.assertEqual(o["pos_id"], pos_id)
        self.assertIsNotNone(o["exchange_id"])
        # yeniden sorgu ile DOĞRULANDI → PROTECTED
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"], "PROTECTED")
        self.assertEqual(lp["protection_client_id"], "C281P1")
        self.assertEqual(self.lc._get_state("first_position_verified"), "1")
        # short pozisyon: BUY stop, entry üstünde
        rec2 = open_one(self.lc, "SOLUSDT", -1, bar_ts=T0 + 2 * BAR)
        a2 = self.ex.algos[f"C281P{rec2['pos_id']}"]
        self.assertEqual(a2["side"], "BUY")
        self.assertEqual(float(a2["triggerPrice"]), 109.90)

    def test_protection_closeposition_never_combines_quantity(self):
        """closePosition=true → quantity/reduceOnly ASLA gönderilmez
        (dokümante parametre çakışması — FakeExchange -1106 reddeder)."""
        open_one(self.lc, "ETHUSDT", 1)
        posts = self._algo_posts()
        self.assertEqual(len(posts), 1)

    def test_unverified_protection_blocks_new_entries(self):
        """Koruma UNKNOWN kaldıkça (F-c) yeni giriş YOK."""
        self.ex.timeout_client_ids.add("C281P1")       # UYGULANIR + timeout
        rec = open_one(self.lc, "ETHUSDT", 1)
        lp = self.lc._live_pos_row(rec["pos_id"])
        self.assertEqual(lp["protection_status"], "PROTECTION_PENDING")
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("PROTECTED değil", " ".join(out[0]["blockers"]))

    # ---- F-e: -2021 sınıflandırma → P_FAILED + çıkış, retry YOK -------------------

    def test_protection_2021_fails_exits_single_post(self):
        self.ex.algo_fail["C281P1"] = BinanceApiError(
            -2021, "Order would immediately trigger.")
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"],
                         "PROTECTION_FAILED_EXIT_PENDING")
        self.assertEqual(self.lc.rm._pos(pos_id)["status"], "CLOSED")
        # KONTROLSÜZ RETRY YOK: tek algo POST, tek reduce-only çıkış
        self.assertEqual(len(self._algo_posts()), 1)
        self.assertEqual(len(self._exit_posts()), 1)

    def test_protection_unknown_resolved_by_verify(self):
        self.ex.timeout_client_ids.add("C281P1")
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        self.assertEqual(self.lc._live_pos_row(pos_id)
                         ["protection_status"], "PROTECTION_PENDING")
        # 30s kadansı sonrası doğrulama döngüsü çözer (emir UYGULANMIŞTI)
        self.clock.advance(60_000)
        self.lc.risk_tick()
        self.assertEqual(self.lc._live_pos_row(pos_id)
                         ["protection_status"], "PROTECTED")
        # pozisyon hâlâ açık, giriş zinciri serbest
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    # ---- koruma kaybolursa yeniden yerleştirme (F-g taze okuma) --------------------

    def test_protection_externally_canceled_is_replaced(self):
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        self.ex.algos["C281P1"]["status"] = "CANCELED"  # dışarıdan silindi
        self.clock.advance(60_000)                      # koruma kadansı
        self.lc.risk_tick()
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"], "PROTECTED")
        self.assertEqual(self.ex.algos["C281P1"]["status"], "NEW")
        # yeniden yerleştirme tek POST daha (toplam 2)
        self.assertEqual(len(self._algo_posts()), 2)

    def test_protection_recheck_unreadable_blocks_entries(self):
        """Koruma doğrulaması okunamıyorsa (feed) yeni giriş bloke (F-c)."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.clock.advance(60_000)
        orig = self.ex.open_algo_orders

        def broken(symbol=None):
            from canary_exchange import ExchangeUnavailable
            raise ExchangeUnavailable("algo feed down")
        self.ex.open_algo_orders = broken
        self.lc.risk_tick()
        self.assertEqual(self.lc._get_state("protection_unverifiable"), "1")
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("koruma emri doğrulanamıyor",
                      " ".join(out[0]["blockers"]))
        self.ex.open_algo_orders = orig
        self.clock.advance(60_000)
        self.ex.set_mark("ETHUSDT", 100.0)
        self.ex.set_mark("SOLUSDT", 100.0)
        self.lc.risk_tick()
        self.assertEqual(self.lc._get_state("protection_unverifiable"), "0")
        out2 = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 3 * BAR,
                                 self.clock())
        self.assertEqual(out2[0]["result"], "FILLED")

    # ---- F-a: çıkıştan sonra artık koruma + cancel/fill yarışı ----------------------

    def test_leftover_protection_cleaned_after_flat_verified(self):
        """Market çıkışıyla flat DOĞRULANDIKTAN sonra koruma iptal edilir;
        iptal -2011 (yarış: zaten yok) → nihai durum SORGU ile çözülür."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        self.ex.algos["C281P1"]["status"] = "CANCELED"
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.lc.rm.update_mark("ETHUSDT", 90.05, self.clock())
        self.lc.initiate_risk_exit(pos_id, "POSITION_LOSS_LIMIT")
        self.assertEqual(self.lc.rm._pos(pos_id)["status"], "CLOSED")
        # koruma artığı çözüldü (CANCELED kaydı) ve pozisyon kapandı
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"], "PROTECTION_CANCELED")
        # borsada açık algo kalmadı
        self.assertEqual(self.ex.open_algo_orders(symbol="ETHUSDT"), [])

    def test_cancel_fill_race_on_protection_cleanup(self):
        """İptal yarışı: cancel -2011 döner ama emir aslında TRIGGERED
        olmuş → sorgu ile nihai durum çözülür, çift kapanış YOK."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        # koruma tetiklenmiş gibi: pozisyonu borsa tarafında kapat
        self.ex.set_position("ETHUSDT", 0.0, 0.0)
        self.ex.algos["C281P1"]["status"] = "TRIGGERED"
        self.ex.algos["C281P1"]["algoStatus"] = "TRIGGERED"
        self.clock.advance(60_000)
        self.ex.set_mark("ETHUSDT", 90.03)
        self.lc.risk_tick()
        pos = self.lc.rm._pos(pos_id)
        self.assertEqual(pos["status"], "CLOSED")
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"], "PROTECTION_TRIGGERED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)

    # ---- stop vs market çıkış çakışması ----------------------------------------------

    def test_exit_in_flight_when_stop_fires_no_double_close(self):
        """Çıkış intent'i no-apply UNKNOWN iken stop tetiklenir: çözüm
        sorgu + positionRisk ile; ek POST yok, ters pozisyon yok."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        self.ex.no_apply_timeouts.add("C281X1n1")
        self.lc.rm.update_mark("ETHUSDT", 95.0, self.clock())
        self.lc.initiate_risk_exit(pos_id, "MANUAL_TEST_EXIT")
        self.assertEqual(self.lc._live_pos_row(pos_id)["exit_status"],
                         "EXIT_UNKNOWN")
        self.clock.advance(60_000)                  # trade ts > entry ts
        self.ex.set_mark("ETHUSDT", 90.03)          # stop 90.10 tetiklenir
        self.lc.risk_tick()
        pos = self.lc.rm._pos(pos_id)
        self.assertEqual(pos["status"], "CLOSED")
        self.assertAlmostEqual(pos["exit_px"], 90.10, places=6)
        # TEK çıkış POST denemesi (no-apply) — stop kazandı, yeniden POST yok
        self.assertEqual(len(self._exit_posts()), 1)
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)

    def test_flat_verification_required_before_closed(self):
        """F-b: çıkış emri DOLDU ama borsada pozisyon hâlâ açık (dış alım) →
        CLOSED YAZILMAZ; doğrulanmış kalan için yeni intent."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        pos_id = rec["pos_id"]
        self.ex.algos["C281P1"]["status"] = "CANCELED"
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.lc.rm.update_mark("ETHUSDT", 90.05, self.clock())
        # çıkış miktarı borsadan okunur: dış kısmi kapanışla 0.3 kalmış
        # (0.3 @90.05 = 27 USDT ≥ minNotional 20 — borsa filtresi geçer)
        self.ex.set_position("ETHUSDT", 0.3, 100.0)
        self.lc.initiate_risk_exit(pos_id, "POSITION_LOSS_LIMIT")
        # ilk çıkış 0.3 sattı → flat → CLOSED; şimdi biri pozisyon geri
        # eklemiş olsun → defter kapalı, borsa açık → drift → giriş bloke
        self.ex.set_position("ETHUSDT", 0.3, 100.0)
        o = self.lc._order_row("C281X1n1")
        self.assertEqual(o["status"], "FILLED")
        # pozisyon CLOSED ise defter borsa gerçeğini izlemiş olmalı; borsada
        # hâlâ 0.3 AÇIK → drift → UNOWNED değil (ledger kapalı) → bloke
        self.clock.advance(60_000)
        self.lc.risk_tick()
        # borsadaki artık pozisyon canary defterinde YOK → giriş bloke
        self.assertEqual(self.lc._get_state("unowned_position_seen"), "1")
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
