"""BLOK28.1 §11 — AĞ KESİNTİSİ TESTLERİ (mock/fixture; ağ çağrısı YOK).

§9 (BLOK27'nin 90dk senaryosu): mark feed kesintisi → 30s içinde girişler
DURUR, staleness KALICI olarak loglanır, borsa tarafı korumalar DOKUNULMAZ,
stale durumu restart'ta korunur, geri dönüşte mutabakat → normalleşme
öncesi giriş yok. Tüm senaryolar Deterministik FakeClock ile — gerçek
bekleme YOK.

Outage semantiği (FakeExchange): GET → ExchangeUnavailable; POST/DELETE →
UnknownResult (sonuç bilinmiyor).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0)

BAR = 4 * 3600 * 1000


class OutageTests(unittest.TestCase):

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

    # ---- 30s kuralı: girişler durur + kalıcı kayıt --------------------------------

    def test_outage_stops_entries_and_logs_persistently(self):
        open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        self.clock.advance(10_000)
        cyc = self.lc.risk_tick()
        self.assertTrue(cyc["feed_down"])
        self.assertIsNotNone(self.lc._get_state("feed_down_ts"))
        # risk_events'te KALICI olay (restart sonrası görülür)
        evs = [r[0] for r in self.lc.rm.db.execute(
            "SELECT kind FROM risk_events WHERE kind='FEED_OUTAGE'"
        ).fetchall()]
        self.assertGreaterEqual(len(evs), 1)
        # yeni giriş YOK (fail-closed)
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("kesintide", " ".join(out[0]["blockers"]))

    def test_outage_no_blind_exits_no_blind_cancels(self):
        """Kesinti sırasında hesap YAPILAMAZ → kör çıkış/iptal YOK (§3:
        bağlantı kesilirse HALT niyeti bekler; burada -5 aşımı olsa bile
        positionRisk okunamadığı için flat İDDİA EDİLMEZ)."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        r = self.lc.initiate_risk_exit(rec["pos_id"], "POSITION_LOSS_LIMIT")
        self.assertEqual(r["status"], "EXIT_PENDING_UNCONFIRMED")
        self.assertEqual(self._exit_posts(), [])
        # borsa koruması DOKUNULMADI (sunucu tarafında YAŞIYOR)
        self.assertEqual(self.ex.algos["C281P1"]["status"], "NEW")
        self.assertEqual(len(self.ex.post_calls), 2)  # entry + algo (iptal yok)

    # ---- 424s / 90dk: deterministik uzun kesinti -----------------------------------

    def test_outage_424_seconds_fail_closed(self):
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        self.clock.advance(424_000)                 # BLOK27'deki 424s boşluk
        cyc = self.lc.risk_tick()
        self.assertTrue(cyc["feed_down"])
        self.assertEqual(self._exit_posts(), [])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "OPEN")

    def test_outage_90_minutes_deterministic_no_realtime_wait(self):
        """90dk kesinti (BLOK27 senaryosu): FakeClock ile anında; koruma
        borsa tarafında sürekli canlı, çıkış kör değil, giriş yok."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        for _ in range(18):                          # 18 × 5dk = 90dk
            self.clock.advance(300_000)
            cyc = self.lc.risk_tick()
            self.assertTrue(cyc["feed_down"])
        self.assertEqual(self._exit_posts(), [])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "OPEN")
        self.assertEqual(self.ex.algos["C281P1"]["status"], "NEW")
        # kesinti süresi KALICI durumda izlendi (down_ts ilk tick'te yazılır:
        # 17×5dk = 85dk > 84dk)
        down_ts = int(self.lc._get_state("feed_down_ts"))
        self.assertGreater(self.clock() - down_ts, 84 * 60_000)

    # ---- geri dönüş: mutabakat ÖNCE, giriş SONRA -------------------------------------

    def test_recovery_reconciles_before_entries(self):
        open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        self.clock.advance(60_000)
        self.lc.risk_tick()                          # kesinti kaydı
        self.ex.outage = False
        self.clock.advance(10_000)
        for s in self.ex.symbols:                    # feed normalleşti
            self.ex.set_mark(s, 100.0)
        cyc = self.lc.risk_tick()                    # FEED_RECOVERED
        self.assertNotIn("feed_down", cyc)
        evs = [r[0] for r in self.lc.rm.db.execute(
            "SELECT kind FROM risk_events WHERE kind='FEED_RECOVERED'"
        ).fetchall()]
        self.assertGreaterEqual(len(evs), 1)
        # mutabakat tamamlandı → kesinti/mutabakat blocker'ı KALKTI: giriş
        # blocker'lardan geçer. BLOK29.1 (100/100): ETH tam bütçeyi tuttuğu
        # için ikinci giriş artık toplam-notional kapısından reddedilir —
        # REJECTED (BLOCKED değil) = outage/reconcile kapısının açıldığının
        # kanıtı.
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertTrue(out[0]["result"].startswith("REJECTED: toplam notional"))

    def test_recovery_positionrisk_still_down_entries_blocked(self):
        """Feed döndü ama positionRisk hâlâ okunamıyor → mutabakat
        bekleniyor → giriş YOK (normalleşmeden risk yok)."""
        open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        self.clock.advance(60_000)
        self.lc.risk_tick()
        self.ex.outage = False
        self.clock.advance(10_000)
        for s in self.ex.symbols:                    # feed normalleşti
            self.ex.set_mark(s, 100.0)
        # positionRisk tarafı hâlâ bozuk (kısmi iyileşme)
        from canary_exchange import ExchangeUnavailable
        orig = self.ex.position_risk
        self.ex.position_risk = lambda: (_ for _ in ()).throw(
            ExchangeUnavailable("positionRisk still down"))
        cyc = self.lc.risk_tick()
        self.assertTrue(cyc.get("reconcile_pending"))
        out = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("mutabakat bekleniyor", " ".join(out[0]["blockers"]))
        self.ex.position_risk = orig
        self.lc.risk_tick()                          # mutabakat tamam
        # BLOK29.1 (100/100): mutabakat blocker'ı kalktı → giriş blocker'ları
        # geçer; reddi artık yalnız toplam-notional kapısından gelir
        out2 = self.lc.try_entry("SOLUSDT", 1, 100.0, T0 + 3 * BAR,
                                 self.clock())
        self.assertTrue(out2[0]["result"].startswith(
            "REJECTED: toplam notional"))

    def test_stale_state_survives_restart(self):
        """Kesinti ortasında restart: yeni süreç feed_down_ts görür →
        giriş yok (kör başlangıç imkânsız)."""
        open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        self.clock.advance(60_000)
        self.lc.risk_tick()
        # RESTART — aynı DB
        from canary_live import LiveCanary
        from canary_liquidation import (LiquidationModel,
                                        parse_leverage_brackets,
                                        POSITION_MODE_ONEWAY)
        liq = LiquidationModel(
            brackets_by_symbol=parse_leverage_brackets(self.ex.brackets_raw),
            margin_modes=self.ex.margin_modes,
            leverages=self.ex.leverages,
            position_mode=POSITION_MODE_ONEWAY, wallet_usdt=self.ex.wallet)
        lc2 = LiveCanary(self.lc.db_path, self.ex, symbols=self.ex.symbols,
                         t0_ms=T0, filters=self.ex.filters, liq_model=liq,
                         clock_ms=self.ex._clock, log_dir=self.tmp.name)
        self.assertIsNotNone(lc2._get_state("feed_down_ts"))
        out = lc2.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                            self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("kesintide", " ".join(out[0]["blockers"]))

    # ---- borsa tarafı koruma sürekliliği (§9 kritik senaryo) ---------------------------

    def test_server_side_protection_fires_during_outage(self):
        """90dk kör dönemde mark stop'u geçti: KORUMA BORSADA tetiklenir
        (biz körüz — emir gönderemeyiz ama sunucu emri çalışır). Geri
        dönüşte mutabakat pozisyonu userTrades'ten ÇÖZER."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.outage = True
        self.clock.advance(60_000)                  # trade ts > entry ts
        # borsa bağımsız davranır: mark stop altına düşer → tetiklenir
        # (BLOK29.1: stop 95.10 — 100 notional'da -5 bütçesi ~%5 mesafe)
        self.ex.set_mark("ETHUSDT", 95.03)          # auto_trigger yok ama:
        self.ex._check_triggers()                   # borsa kendi kuralı
        self.assertEqual(self.ex.algos["C281P1"]["algoStatus"], "TRIGGERED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        # biz hâlâ körüz — defterde OPEN, flat İDDİA EDİLMİYOR
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "OPEN")
        self.assertEqual(self._exit_posts(), [])
        # geri dönüş → mutabakat → çözüm
        self.ex.outage = False
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.03)
        cyc = self.lc.risk_tick()
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")   # stop fill'den çözüldü
        self.assertAlmostEqual(pos["exit_px"], 95.10, places=6)
        self.assertEqual(self.lc._live_pos_row(rec["pos_id"])
                         ["protection_status"], "PROTECTION_TRIGGERED")
        # kör dönemde market çıkış POST EDİLMEDİ (stop yeterliydi)
        self.assertEqual(self._exit_posts(), [])


if __name__ == "__main__":
    unittest.main()
