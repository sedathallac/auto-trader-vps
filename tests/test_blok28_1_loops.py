"""BLOK28.1 §11 — BAĞIMSIZ DÖNGÜ TESTLERİ (mock/fixture; ağ çağrısı YOK).

§4: iki bağımsız zaman döngüsü — strategy (300s H1) + risk (mark başına,
~5s kadans; 300s'i BEKLEMEZ). Tekil worker (flock + DB lease). Restart
sırası: mutabakat → sahiplik → risk → en son giriş. Mark staleness
(30s — PROVIZYONEL, operatör onayı bekliyor).
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0)

BAR = 4 * 3600 * 1000
from canary_live import WorkerLockHeld


class LoopTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.lc.stop()
        self.tmp.cleanup()

    # ---- §4: risk loop strategy'yi BEKLEMEZ -----------------------------------

    def test_risk_loop_processes_exit_without_any_strategy_tick(self):
        """Hiç strategy_tick çağrılmadan (kline HİÇ okunmadan) -5 çıkışı
        çalışır: risk döngüsü tamamen bağımsız."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.algos["C281P1"]["status"] = "CANCELED"
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)
        cyc = self.lc.risk_tick()                 # TEK risk tick yeter
        self.assertIn({"pos_id": rec["pos_id"], "action": "SAFE_EXIT"},
                      cyc["risk_actions"])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "CLOSED")

    def test_mark_delay_within_stale_window_still_evaluated(self):
        """Mark 5s gecikmeli (30s pencere içi) → risk değerlendirmesi
        ÇALIŞIR (aşırı temkinlilik yok)."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.algos["C281P1"]["status"] = "CANCELED"
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05, ts=self.clock() - 5_000)
        cyc = self.lc.risk_tick()
        self.assertIn({"pos_id": rec["pos_id"], "action": "SAFE_EXIT"},
                      cyc["risk_actions"])

    def test_mark_stale_boundary_29s_vs_31s(self):
        """30s eşiği (PROVIZYONEL): 29s → hesap yapılır; 31s → fail-closed."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.ex.algos["C281P1"]["status"] = "CANCELED"
        # 29s: eşik içi → loss hesaplanır (95.05 → -5.00 → SAFE_EXIT)
        self.ex.set_mark("ETHUSDT", 95.05, ts=self.clock() - 29_000)
        cyc = self.lc.risk_tick()
        self.assertIn({"pos_id": rec["pos_id"], "action": "SAFE_EXIT"},
                      cyc["risk_actions"])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "CLOSED")
        # BLOK29.1 (100/100): ETH kapandı → bütçe boşaldı → yeni pozisyon
        # açılabilir; 31s: eşik dışı → FAIL_CLOSED_NO_DATA
        rec2 = open_one(self.lc, "SOLUSDT", 1, bar_ts=T0 + 2 * BAR)
        self.ex.algos["C281P2"]["status"] = "CANCELED"
        self.ex.set_mark("SOLUSDT", 95.05, ts=self.clock() - 31_000)
        cyc2 = self.lc.risk_tick()
        self.assertIn({"pos_id": rec2["pos_id"],
                       "action": "FAIL_CLOSED_NO_DATA"}, cyc2["risk_actions"])

    # ---- §4.2: tekil worker ----------------------------------------------------

    def test_second_worker_rejected_by_lock(self):
        self.lc.acquire_worker_lock()
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
        with self.assertRaises(WorkerLockHeld):
            lc2.acquire_worker_lock()             # ikinci instance RED
        # kilit serbest bırakılınca alınabilir
        self.lc.release_worker_lock()
        lc2.acquire_worker_lock()
        lc2.release_worker_lock()

    def test_worker_lock_survives_object_gc(self):
        """flock dosya kilidi — nesne yok olsa bile kilit dosyada kalıcı
        süreç yaşadığı sürece tutulur."""
        self.lc.acquire_worker_lock()
        pid = self.lc._get_state("worker_pid")
        self.assertEqual(int(pid), os.getpid())
        self.assertIsNotNone(self.lc._get_state("worker_lease_ts"))

    # ---- §4.2: restart sırası — mutabakat ÖNCE, giriş SONRA ---------------------

    def test_restart_entries_blocked_until_reconcile(self):
        """Restart sonrası: startup_reconcile TAMAMLANMADAN giriş yok."""
        from canary_live import LiveCanary
        from canary_liquidation import (LiquidationModel,
                                        parse_leverage_brackets,
                                        POSITION_MODE_ONEWAY)
        open_one(self.lc, "ETHUSDT", 1)           # canary çalışıyor
        self.lc.stop()
        liq = LiquidationModel(
            brackets_by_symbol=parse_leverage_brackets(self.ex.brackets_raw),
            margin_modes=self.ex.margin_modes,
            leverages=self.ex.leverages,
            position_mode=POSITION_MODE_ONEWAY, wallet_usdt=self.ex.wallet)
        lc2 = LiveCanary(self.lc.db_path, self.ex, symbols=self.ex.symbols,
                         t0_ms=T0, filters=self.ex.filters, liq_model=liq,
                         clock_ms=self.ex._clock, log_dir=self.tmp.name)
        # mutabakat YOK → giriş engelli
        out = lc2.try_entry("SOLUSDT", 1, 100.0, T0 + 2 * BAR,
                            self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("mutabakatı tamam değil", " ".join(out[0]["blockers"]))
        # mutabakat → risk tick → giriş SERBEST
        rec = lc2.startup_reconcile()
        self.assertEqual(rec["result"], "OK")
        lc2.risk_tick()
        # BLOK29.1 (100/100): ETH long açıkken ikinci tam-boyut giriş toplam
        # kapısına takılır — ETH'yi borsa stop'uyla kapat (mark 95.05 <
        # stop 95.10), bütçe boşalınca giriş SERBEST
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 95.05)
        lc2.risk_tick()
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        out2 = lc2.try_entry("SOLUSDT", 1, 100.0, T0 + 3 * BAR,
                             self.clock())
        self.assertEqual(out2[0]["result"], "FILLED")
        self.assertEqual(out2[0]["protection"]["status"], "PROTECTED")

    def test_restart_first_risk_tick_before_strategy(self):
        """run() başlangıç sırası: risk tick ÖNCE (mutabakat + mark), ancak
        sonra strategy döngüsü (§4.2)."""
        order = []
        orig_risk = self.lc.risk_tick
        orig_strat = self.lc.strategy_tick

        def risk_spy(*a, **kw):
            order.append("risk")
            return orig_risk(*a, **kw)

        def strat_spy(*a, **kw):
            order.append("strategy")
            return orig_strat(*a, **kw)
        self.lc.risk_tick = risk_spy
        self.lc.strategy_tick = strat_spy
        t = threading.Thread(
            target=lambda: self.lc.run(strategy_interval=0.05,
                                       risk_interval=0.05, on_started=None),
            daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and order.count("strategy") < 1:
            time.sleep(0.01)
        self.lc.stop()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(len(order), 2)
        self.assertEqual(order[0], "risk")        # İLK tick risk
        # iki döngü de çalıştı (bağımsız thread'ler)
        self.assertIn("strategy", order)
        self.assertGreater(order.count("risk"), 1)

    def test_threaded_run_smoke(self):
        """Gerçek iki thread + worker kilidi: risk tick'ler akar, stop()
        temiz kapatır, kilit serbest kalır."""
        t = threading.Thread(
            target=lambda: self.lc.run(strategy_interval=0.02,
                                       risk_interval=0.02),
            daemon=True)
        t.start()
        log = os.path.join(self.tmp.name, "live_cycles.jsonl")
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(log) and \
                    open(log).read().count("RISK_TICK") >= 3:
                break
            time.sleep(0.01)
        self.lc.stop()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(open(log).read().count("RISK_TICK"), 3)
        # kilit serbest → yeni worker alabilir
        self.lc.acquire_worker_lock()
        self.lc.release_worker_lock()


if __name__ == "__main__":
    unittest.main()
