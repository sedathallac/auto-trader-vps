"""BLOK28.1 §11 — NOTIONAL LİMİT TESTLERİ (mock/fixture; ağ çağrısı YOK).

Operatör limitleri (BLOK29.1 güncellemesi 2026-11-25): pozisyon başına
100 USDT notional (marjin DEĞİL; eski değer 50), toplam 100 USDT, en fazla
2 açık pozisyon, -5/-10 zarar limitleri DEĞİŞMEDİ.

100/100 aritmetiği: hedef notional = pozisyon limiti = 100 → tam boyutlu
pozisyon açıkken ikinci giriş TOPLAM kapısından reddedilir (operatör
kuralı "iki pozisyonun toplamı 100'ü geçmez"nin bizzat uygulaması);
ikinci pozisyon ancak ilki kapandıktan sonra açılabilir.

Kapsam: 100 kapısı, toplam kapı reddi, RiskManager 2-pozisyon üst sınırı,
eşzamanlı emir yarışı, kısmi fill, fill sonrası fiyat hareketi (tolerans +
aşım → çıkış), tick/stepSize yuvarlaması (BTCUSDT 100 kapısında artık
ELIGIBLE — gerçek veri 2026-11-25: 0.001 BTC ≈ 59.9 USDT), komisyon rezervi
(stop 95.10, 95.00 değil), komisyon/funding çift sayımı yok.
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0)

BAR = 4 * 3600 * 1000


class NotionalTests(unittest.TestCase):

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

    # ---- 100 USDT pozisyon limiti (BLOK29.1) ------------------------------------

    def test_position_notional_100_exact(self):
        rec = open_one(self.lc, "ETHUSDT", 1)      # 1.0 @100 = 100.00
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 100.0, places=6)
        self.assertLessEqual(pos["units"] * pos["entry_px"], 100.0 + 1e-9)

    def test_btcusdt_tradeable_under_100_cap(self):
        """BLOK29.1: 100 USDT kapıda BTCUSDT ELIGIBLE — gerçek veri
        (2026-11-25): minQty 0.001, mark ~59.9k → 0.001 BTC ≈ 59.9 USDT
        ≤ 100; minNotional 50 karşılanır."""
        self.ex.set_mark("BTCUSDT", 59_900.0)
        self.lc.rm.update_mark("BTCUSDT", 59_900.0, self.clock())
        rec = self.lc.try_entry("BTCUSDT", 1, 59_900.0, T0 + BAR,
                                self.clock())[0]
        self.assertEqual(rec["result"], "FILLED")
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["units"], 0.001)
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 59.9, places=6)
        self.assertLessEqual(pos["units"] * pos["entry_px"], 100.0)

    def test_btcusdt_blocked_when_price_prevents_min_qty(self):
        """Fiyat ~120k olsaydı: qty@100 = 0.000 < minQty 0.001 → giriş
        YOK (emir hiç gönderilmez). Limit yükseltilmez (§13)."""
        out = self.lc.try_entry("BTCUSDT", 1, 120_000.0, T0 + BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("minQty", " ".join(out[0]["blockers"]))
        n = self.lc.db.execute(
            "SELECT COUNT(*) FROM live_orders WHERE kind='ENTRY'").fetchone()[0]
        self.assertEqual(n, 0)                     # emir HİÇ gönderilmedi

    def test_step_rounding_doget(self):
        # DOGEUSDT: step 1, minQty 1, minNotional 5 → mark 13: qty 7 (91)
        self.ex.set_mark("DOGEUSDT", 13.0)
        self.lc.rm.update_mark("DOGEUSDT", 13.0, self.clock())
        rec = self.lc.try_entry("DOGEUSDT", 1, 13.0, T0 + BAR,
                                self.clock())[0]
        self.assertEqual(rec["result"], "FILLED")
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["units"], 7.0)
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 91.0, places=6)

    def test_step_rounding_ada(self):
        # ADAUSDT: step 1, minQty 1 → mark 0.35: qty 285 (99.75 ≤ 100)
        self.ex.set_mark("ADAUSDT", 0.35)
        self.lc.rm.update_mark("ADAUSDT", 0.35, self.clock())
        rec2 = self.lc.try_entry("ADAUSDT", 1, 0.35, T0 + BAR,
                                 self.clock())[0]
        self.assertEqual(rec2["result"], "FILLED")
        pos2 = self.lc.rm._pos(rec2["pos_id"])
        self.assertEqual(pos2["units"], 285.0)
        self.assertAlmostEqual(pos2["units"] * pos2["entry_px"], 99.75,
                               places=6)

    # ---- 100 USDT toplam + 2 pozisyon sınırı ------------------------------------

    def test_second_entry_rejected_while_total_cap_full(self):
        """BLOK29.1 (100/100): hedef notional = pozisyon limiti = 100 →
        tam boyutlu pozisyon açıkken ikinci giriş TOPLAM kapısından
        reddedilir. Operatör kuralı 'iki pozisyonun toplamı 100 USDT'yi
        geçmez'in doğrudan uygulaması; limit yükseltilmez."""
        rec = open_one(self.lc, "ETHUSDT", 1)      # 100 @100
        self.assertEqual(rec["result"], "FILLED")
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertTrue(out[0]["result"].startswith("REJECTED"))
        self.assertIn("toplam notional", out[0]["result"])
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        total = self.lc.rm.total_notional(self.clock())
        self.assertLessEqual(total, 100.0 + 1e-9)

    def test_rm_two_position_cap_enforced(self):
        """MAX_OPEN_POSITIONS=2 üst sınırı merkez RiskManager katmanında
        geçerliliğini korur (100/100'de LiveCanary tam-boyut girişleri
        ikinciyi zaten toplam kapıdan reddeder; üst sınır ayrıca test
        edilir — küçük notional'larla 2 pozisyon + üçüncü RED)."""
        from canary_risk import OrderRejected
        self.lc.rm.submit_order("ETHUSDT", 1, 50.0, 100.0, self.clock())
        self.lc.rm.submit_order("SOLUSDT", 1, 50.0, 100.0, self.clock())
        self.assertEqual(len(self.lc.rm.open_positions()), 2)
        with self.assertRaises(OrderRejected) as cm:
            self.lc.rm.submit_order("ADAUSDT", 1, 10.0, 100.0, self.clock())
        self.assertIn("pozisyon sınırı", str(cm.exception))
        self.assertEqual(len(self.lc.rm.open_positions()), 2)

    def test_concurrent_entries_cannot_exceed_limits(self):
        """Üç eşzamanlı giriş denemesi (§8): en fazla 2 pozisyon, toplam
        ≤ 100 USDT. _entry_lock + atomik check_order yarışı önler."""
        results = []
        barrier = threading.Barrier(3)

        def attempt(sym):
            barrier.wait()
            rec = open_one(self.lc, sym, 1, bar_ts=T0 + BAR)
            results.append(rec)

        threads = [threading.Thread(target=attempt, args=(s,))
                   for s in ("ETHUSDT", "SOLUSDT", "ADAUSDT")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        # §7 ilk-pozisyon kuralı yarış zamanlamasına göre 1 veya 2 pozisyon
        # üretir (ikincisi ilkinin fill+koruma DOĞRULAMASINI bekler) —
        # her ikisi de spec-uyumlu. DEĞİŞMEZLER: maks 2, toplam ≤ 100,
        # dolanların hepsi PROTECTED, üçüncü ASLA açılmaz.
        opens = self.lc.rm.open_positions()
        self.assertIn(len(opens), (1, 2))
        for r in results:
            if r["result"] == "FILLED":
                self.assertIn(r["symbol"], [p["symbol"] for p in opens])
            else:
                # bloke/reddedilen denemeler spec-uyumlu nedenlerle
                # (ilk-pozisyon kuralı, pozisyon sınırı, koruma doğrulaması)
                self.assertTrue(
                    r["result"].startswith(("BLOCKED", "REJECTED:")) or
                    r["result"] == "DUPLICATE_SIGNAL",
                    f"beklenmedik sonuç: {r['result']}")
        self.assertEqual(len(opens), len([r for r in results
                                           if r["result"] == "FILLED"]))
        total = self.lc.rm.total_notional(self.clock())
        self.assertIsNotNone(total)
        self.assertLessEqual(total, 100.0 + 1e-9)
        # doldurulan pozisyonların hepsi PROTECTED
        for p in self.lc.rm.open_positions():
            self.assertEqual(self.lc._live_pos_row(p["pos_id"])
                             ["protection_status"], "PROTECTED")

    # ---- fill sonrası fiyat hareketi ---------------------------------------------

    def test_fill_price_movement_over_100_exits(self):
        """Giriş 100 üzerinden boyutlandı, fill 102'den → notional 102 >
        100*(1+%1) → OVER_LIMIT_EXIT (§8: limit ihlali → anında çıkış)."""
        self.ex.set_mark("ETHUSDT", 102.0)
        self.lc.rm.update_mark("ETHUSDT", 102.0, self.clock())
        rec = open_one(self.lc, "ETHUSDT", 1)      # ref_px 100 → qty 1.0
        self.assertEqual(rec["result"],
                         "OVER_LIMIT_EXIT: fill notional limiti aştı")
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(self.lc._live_pos_row(rec["pos_id"])["exit_status"],
                         "EXIT_CONFIRMED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        self.assertEqual(len(self._exit_posts()), 1)

    def test_fill_notional_at_tolerance_kept(self):
        """Fill 100.9 → notional 100.9, tek pozisyon limiti tam %1
        tolerans sınırında (100.9 ≤ 100*1.01) → pozisyon KALIR."""
        self.ex.set_mark("ETHUSDT", 100.9)
        self.lc.rm.update_mark("ETHUSDT", 100.9, self.clock())
        rec = open_one(self.lc, "ETHUSDT", 1)      # qty 1.0, fill 100.9
        self.assertEqual(rec["result"], "FILLED")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        total = self.lc.rm.total_notional(self.clock())
        self.assertLessEqual(total, 100.0 * 1.01)

    # ---- komisyon rezervi + çift sayım yok -----------------------------------------

    def test_stop_price_includes_commission_reserve(self):
        """Stop, -5 bütçesinden KOMİSYON düşülerek hesaplanır: 5 − 0.05
        (giriş, 100 notional × %0.05) − 0.05 (tahmini çıkış) = 4.90 →
        100 − 4.90 = 95.10 (tick'e yuvarla, ERKEN taraf). 95.00 DEĞİL
        (komisyonsuz hesap)."""
        open_one(self.lc, "ETHUSDT", 1)
        a = self.ex.algos["C281P1"]
        self.assertEqual(a["type"], "STOP_MARKET")
        self.assertEqual(a["workingType"], "MARK_PRICE")
        self.assertEqual(float(a["triggerPrice"]), 95.10)
        self.assertGreater(float(a["triggerPrice"]), 95.00)

    def test_funding_counted_once_not_double(self):
        rec = open_one(self.lc, "ETHUSDT", 1)
        # funding ödemesi: Binance income -0.5 (ödenmiş) → rm amount -0.5
        self.ex.add_income("ETHUSDT", -0.5, self.clock() + 1)
        self.clock.advance(70_000)                 # funding sync (>60s)
        self.lc.risk_tick()
        fund = self.lc.rm.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM funding WHERE pos_id=?",
            (rec["pos_id"],)).fetchone()[0]
        self.assertAlmostEqual(fund, -0.5, places=9)
        # ikinci sync → çift sayım YOK (INSERT OR REPLACE)
        self.clock.advance(70_000)
        self.lc.risk_tick()
        fund2 = self.lc.rm.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM funding WHERE pos_id=?",
            (rec["pos_id"],)).fetchone()[0]
        self.assertAlmostEqual(fund2, -0.5, places=9)
        # zarar: (100-95)*1.0 + 0.05 + 0.5 = 5.55 (funding dahil)
        self.ex.set_mark("ETHUSDT", 95.0)
        self.lc.rm.update_mark("ETHUSDT", 95.0, self.clock())
        loss = self.lc.rm.position_net_loss(self.lc.rm._pos(rec["pos_id"]),
                                            self.clock())
        self.assertAlmostEqual(loss, 5.55, places=6)

    def test_canary_external_pnl_excluded(self):
        """Canary dışı (başka bot) pozisyonu rm zararına GİRMEZ."""
        self.ex.set_position("XRPUSDT", 1000.0, 1.0)
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.set_mark("XRPUSDT", 0.5)           # dış pozisyon -500 uPnL
        self.lc.rm.update_mark("XRPUSDT", 0.5, self.clock())
        loss = self.lc.rm.total_net_loss(self.clock())
        # yalnız ETH: 0.05 komisyon (100 notional × %0.05)
        self.assertAlmostEqual(loss, 0.05, places=6)


if __name__ == "__main__":
    unittest.main()
