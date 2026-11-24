"""BLOK28.1 §11 — NOTIONAL LİMİT TESTLERİ (mock/fixture; ağ çağrısı YOK).

Operatör limitleri (§0, DEĞİŞTİRİLEMEZ): pozisyon başına 50 USDT notional
(marjin DEĞİL), toplam 100 USDT, en fazla 2 açık pozisyon.

Kapsam: 50/100 kapıları, üçüncü pozisyon yasağı, eşzamanlı emir yarışı,
kısmi fill, fill sonrası fiyat hareketi (tolerans + aşım → çıkış),
tick/stepSize yuvarlaması (BTCUSDT 50 USDT limiti içinde boyutlanamıyor —
GERÇEK exchangeInfo bulgusu), komisyon rezervi (stop 90.10, 90.00 değil),
komisyon/funding çift sayımı yok.
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

    # ---- 50 USDT pozisyon limiti -----------------------------------------------

    def test_position_notional_50_exact(self):
        rec = open_one(self.lc, "ETHUSDT", 1)      # 0.5 @100 = 50.00
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 50.0, places=6)
        self.assertLessEqual(pos["units"] * pos["entry_px"], 50.0 + 1e-9)

    def test_btcusdt_untradeable_under_50_cap(self):
        """GERÇEK exchangeInfo (2026-11-24 kanıt): BTCUSDT minQty 0.001,
        fiyat ~90k → 0.001 BTC ≈ 90 USDT > 50 USDT limiti → giriş YOK."""
        out = self.lc.try_entry("BTCUSDT", 1, 90_000.0, T0 + BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("minQty", " ".join(out[0]["blockers"]))
        n = self.lc.db.execute(
            "SELECT COUNT(*) FROM live_orders WHERE kind='ENTRY'").fetchone()[0]
        self.assertEqual(n, 0)                     # emir HİÇ gönderilmedi

    def test_step_rounding_doget_and_ada(self):
        # DOGEUSDT: step 1, minQty 1, minNotional 5 → mark 13: qty 3 (39)
        self.ex.set_mark("DOGEUSDT", 13.0)
        self.lc.rm.update_mark("DOGEUSDT", 13.0, self.clock())
        rec = self.lc.try_entry("DOGEUSDT", 1, 13.0, T0 + BAR,
                                self.clock())[0]
        self.assertEqual(rec["result"], "FILLED")
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["units"], 3.0)
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 39.0, places=6)
        # ADAUSDT: step 1 → mark 0.35: qty 142 (49.70)
        self.ex.set_mark("ADAUSDT", 0.35)
        self.lc.rm.update_mark("ADAUSDT", 0.35, self.clock())
        rec2 = self.lc.try_entry("ADAUSDT", 1, 0.35, T0 + BAR,
                                 self.clock())[0]
        self.assertEqual(rec2["result"], "FILLED")
        pos2 = self.lc.rm._pos(rec2["pos_id"])
        self.assertEqual(pos2["units"], 142.0)

    # ---- 100 USDT toplam + 2 pozisyon sınırı ------------------------------------

    def test_third_position_blocked(self):
        open_one(self.lc, "ETHUSDT", 1)
        open_one(self.lc, "SOLUSDT", 1)
        self.assertEqual(len(self.lc.rm.open_positions()), 2)
        out = self.lc.try_entry("ADAUSDT", 1, 0.35, T0 + BAR, self.clock())
        self.assertTrue(out[0]["result"].startswith("REJECTED"))
        self.assertIn("pozisyon sınırı", out[0]["result"])
        self.assertEqual(len(self.lc.rm.open_positions()), 2)
        total = self.lc.rm.total_notional(self.clock())
        self.assertLessEqual(total, 100.0 + 1e-9)

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

    def test_fill_price_movement_over_50_exits(self):
        """Giriş 100 üzerinden boyutlandı, fill 102'den → notional 51 >
        50*(1+%1) → OVER_LIMIT_EXIT (§8: limit ihlali → anında çıkış)."""
        self.ex.set_mark("ETHUSDT", 102.0)
        self.lc.rm.update_mark("ETHUSDT", 102.0, self.clock())
        rec = open_one(self.lc, "ETHUSDT", 1)      # ref_px 100 → qty 0.5
        self.assertEqual(rec["result"],
                         "OVER_LIMIT_EXIT: fill notional limiti aştı")
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(self.lc._live_pos_row(rec["pos_id"])["exit_status"],
                         "EXIT_CONFIRMED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        self.assertEqual(len(self._exit_posts()), 1)

    def test_fill_total_notional_at_tolerance_kept(self):
        """ETH 50 + SOL fill 101'den 50.5 → tek pozisyon limiti tam %1
        tolerans sınırında (50.5 ≤ 50*1.01) → pozisyon KALIR."""
        open_one(self.lc, "ETHUSDT", 1)            # 50 @100
        self.ex.set_mark("SOLUSDT", 101.0)
        self.lc.rm.update_mark("SOLUSDT", 101.0, self.clock())
        rec = open_one(self.lc, "SOLUSDT", 1)      # fill 50.5
        self.assertEqual(rec["result"], "FILLED")
        self.assertEqual(len(self.lc.rm.open_positions()), 2)
        total = self.lc.rm.total_notional(self.clock())
        self.assertLessEqual(total, 100.0 * 1.01)

    # ---- komisyon rezervi + çift sayım yok -----------------------------------------

    def test_stop_price_includes_commission_reserve(self):
        """Stop, -5 bütçesinden KOMİSYON düşülerek hesaplanır: 5 − 0.025
        (giriş) − 0.025 (tahmini çıkış) = 4.95 → 100 − 9.9 = 90.10 (tick'e
        yuvarla, ERKEN taraf). 90.00 DEĞİL (komisyonsuz hesap)."""
        open_one(self.lc, "ETHUSDT", 1)
        a = self.ex.algos["C281P1"]
        self.assertEqual(a["type"], "STOP_MARKET")
        self.assertEqual(a["workingType"], "MARK_PRICE")
        self.assertEqual(float(a["triggerPrice"]), 90.10)
        self.assertGreater(float(a["triggerPrice"]), 90.00)

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
        # zarar: (100-95)*0.5 + 0.025 + 0.5 = 3.025 (funding dahil)
        self.ex.set_mark("ETHUSDT", 95.0)
        self.lc.rm.update_mark("ETHUSDT", 95.0, self.clock())
        loss = self.lc.rm.position_net_loss(self.lc.rm._pos(rec["pos_id"]),
                                            self.clock())
        self.assertAlmostEqual(loss, 3.025, places=6)

    def test_canary_external_pnl_excluded(self):
        """Canary dışı (başka bot) pozisyonu rm zararına GİRMEZ."""
        self.ex.set_position("XRPUSDT", 1000.0, 1.0)
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.set_mark("XRPUSDT", 0.5)           # dış pozisyon -500 uPnL
        self.lc.rm.update_mark("XRPUSDT", 0.5, self.clock())
        loss = self.lc.rm.total_net_loss(self.clock())
        # yalnız ETH: 0.025 komisyon
        self.assertAlmostEqual(loss, 0.025, places=6)


if __name__ == "__main__":
    unittest.main()
