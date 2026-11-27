"""GÖREV 2026-11-27 (ENGEL 3 önkoşulu): canary_brain_runner.serve() kök
neden düzeltmesinin regresyon testleri.

KÖK NEDEN (canlı kanıtlı): eski serve() hedefi daima
'(sıradaki sınır)+60s' olarak hesaplıyor, uyanınca BAŞTAN hesaplıyordu.
Uyanış anında `now` her zaman geçerli barın içinde olduğundan sıradaki
sınır BİR SONRAKİ bara atlar; wait_s daima >60s kalır ve run_cycle
MATEMATİKSEL olarak hiç çağrılamazdı. Canlı süreç kanıtı: 14+ saatte
0,2s CPU, 0 döngü; DB'deki 2 döngü elle --once ile atılmıştı; py-spy
locals wait_s=7199.999 gösterdi.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_brain_runner import (BAR_MS, MARKET_SNAPSHOT_DELAY_S,
                                 BrainRunner, serve_next_action)

DELAY_MS = MARKET_SNAPSHOT_DELAY_S * 1000
# bar sınırına HİZALI taban (tüm testlerde ortak)
BASE = (1_795_000_000_000 // BAR_MS + 1) * BAR_MS


class TestOldFormulaUnreachable(unittest.TestCase):
    """Eski formülün ulaşılmazlığının matematiksel kanıtı (düzelmenin
    GEREKLİLİĞİNİ sabitler)."""

    def test_old_wait_always_positive(self):
        # eski kod: next_close=(now//BAR_MS+1)*BAR_MS; wait=target-now
        # next_close her zaman now'dan BÜYÜK olduğundan wait > 60s kalır
        for frac in (0, 1, 59_999, 60_000, 60_001, 3_600_000,
                     BAR_MS - 1):
            now = BASE + frac
            next_close = (now // BAR_MS + 1) * BAR_MS
            wait_ms = next_close + DELAY_MS - now
            self.assertGreater(
                wait_ms, DELAY_MS,
                f"eski formül pos={frac} ms'de tetiklenebilirdi — "
                "kök neden varsayımı çürür")


class TestServeNextAction(unittest.TestCase):
    def test_before_delay_sleeps_remaining(self):
        act, payload = serve_next_action(BASE, last_cycled_data_bar=-1)
        self.assertEqual(act, "sleep")
        self.assertAlmostEqual(payload, DELAY_MS / 1000.0, places=6)

    def test_mid_first_minute(self):
        act, payload = serve_next_action(BASE + 30_000, -1)
        self.assertEqual(act, "sleep")
        self.assertAlmostEqual(payload, 30.0, places=6)

    def test_at_delay_with_new_data_bar_cycles(self):
        act, _ = serve_next_action(BASE + DELAY_MS, -1)
        self.assertEqual(act, "cycle")

    def test_same_data_bar_not_rescanned(self):
        bar = BASE // BAR_MS
        act, payload = serve_next_action(BASE + DELAY_MS, bar - 1)
        self.assertEqual(act, "sleep")
        # tam bar bekleme (14400s) 3600s parçalara bölünür (clamp);
        # toplam beklenen = (BAR-pos)+delay = tam bar — parçalar halinde
        self.assertEqual(payload, 3600.0)

    def test_late_in_bar_catches_up(self):
        # restart/downtime sonrası: bar içinde 3 saat geç, veri barı yeni
        act, _ = serve_next_action(BASE + 3 * 3600 * 1000, -1)
        self.assertEqual(act, "cycle")

    def test_late_in_bar_already_scanned_sleeps(self):
        bar = BASE // BAR_MS
        act, payload = serve_next_action(BASE + 3 * 3600 * 1000, bar - 1)
        self.assertEqual(act, "sleep")
        self.assertLessEqual(payload, 3600.0)   # parçalı uyku clamp'i
        self.assertGreater(payload, 0.0)

    def test_sleep_never_exceeds_3600(self):
        bar = BASE // BAR_MS
        for frac in (0, DELAY_MS, DELAY_MS + 1, BAR_MS // 2, BAR_MS - 1):
            act, payload = serve_next_action(BASE + frac, bar - 1)
            self.assertEqual(act, "sleep")
            self.assertLessEqual(payload, 3600.0)


class _Stop(BaseException):
    pass


class TestServeLoop(unittest.TestCase):
    """Enjekte edilebilir saat/uyku ile serve() tüm davranışı.
    (conftest time.sleep'i global no-op yapar — bu yüzden serve uykusu
    kurucudan ENJEKTE edilir; sahte uyku sahte saati ilerletir.)"""

    def _runner(self, tmp, *, state):
        clock = lambda: state["now"]

        def sleep_fn(s):
            state["now"] += int(s * 1000)
            if state.get("stop_after") is not None and \
                    state["now"] > state["stop_after"]:
                raise _Stop()

        return BrainRunner(db_path=os.path.join(tmp, "brain.db"),
                           log_dir=tmp, sleep=sleep_fn, now_ms=clock)

    def test_cycles_once_per_bar_close_plus_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"now": BASE + 10_000, "calls": 0,
                     "stop_after": BASE + 2 * BAR_MS + DELAY_MS + 120_000}

            def run_cycle():
                state["calls"] += 1
                state["now"] += 50_000        # döngü ~50s sürer
                if state["calls"] >= 3:
                    raise _Stop()

            r = self._runner(tmp, state=state)
            r.run_cycle = run_cycle
            with self.assertRaises(_Stop):
                r.serve()
            # 3 döngü = B+60s, B+BAR+60s, B+2BAR+60s (her kapanış+60s,
            # her veri barı YALNIZ bir kez; 3. çağrıda durduruldu)
            self.assertEqual(state["calls"], 3)
            self.assertGreaterEqual(
                state["now"], BASE + 2 * BAR_MS + DELAY_MS)

    def test_cold_start_mid_bar_catches_up_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            # DB boş + bar içinde 3h → ilk fırsatta TEK catch-up döngüsü
            state = {"now": BASE + 3 * 3600 * 1000, "calls": 0,
                     "stop_after": BASE + 3 * 3600 * 1000 + 200_000}
            r = self._runner(tmp, state=state)
            r.run_cycle = lambda: (
                state.__setitem__("calls", state["calls"] + 1),
                state.__setitem__("now", state["now"] + 50_000))
            with self.assertRaises(_Stop):
                r.serve()
            self.assertEqual(state["calls"], 1)   # catch-up: tek döngü
            # aynı veri barı ikinci kez TARANMADI (uykuya geçti)
            self.assertGreater(state["now"], BASE + 3 * 3600 * 1000)

    def test_error_retries_same_data_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"now": BASE + DELAY_MS + 5_000, "fails": 0, "oks": 0,
                     "stop_after": BASE + DELAY_MS + 5_000 + 2_000_000}

            def run_cycle():
                if state["fails"] < 2:
                    state["fails"] += 1
                    raise RuntimeError("sahte ag hatasi")
                state["oks"] += 1

            r = self._runner(tmp, state=state)
            r.run_cycle = run_cycle
            with self.assertRaises(_Stop):
                r.serve()
            self.assertEqual(state["fails"], 2)
            self.assertEqual(state["oks"], 1)   # retry aynı veri barında

    def test_last_cycled_data_bar_from_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = BASE + 3 * 3600 * 1000          # bar içinde 3h
            state = {"now": ts, "stop_after": None}
            r = self._runner(tmp, state=state)
            try:
                self.assertEqual(r._last_cycled_data_bar(), -1)  # DB boş
                summary = {"cycle_id": 1, "ts": ts, "decision":
                           "CANDIDATE", "universe_eligible": 519,
                           "market_data_covered": 519, "liquid": 152,
                           "history_warm": 152, "scored": 152,
                           "candidates": 0, "long_candidates": 0,
                           "short_candidates": 0, "rejected": 304,
                           "no_trade_symbols": 143, "model_version": "m",
                           "funnel": {}, "notes": []}
                r.db.write_cycle(summary, [])
                self.assertEqual(r._last_cycled_data_bar(),
                                 ts // BAR_MS - 1)
            finally:
                r.db.close()


if __name__ == "__main__":
    unittest.main()
