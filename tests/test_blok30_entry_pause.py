"""BLOK30 testleri — KALICI ENTRY PAUSE (operatör kontrolü; ağ çağrısı YOK).

Görev §7'nin 35 maddelik planının tamamı (numaralar yorumlarda):
  A (1-6)   temel PAUSE: varsayılan 0, kalıcılık, restart korunumu,
            resume --confirm şartı, resume yalnız PAUSE'u kaldırır
  B (7-12)  engelleme: try_entry BLOCKED, neden ENTRY_PAUSED, new_order
            ÇAĞRILMAZ, INTENT OLUŞMAZ, tekrarlı sinyal aşamaz, eski sinyal
            resume sonrası oynatılmaz (sig dedup korunur)
  C (13-19) risk döngüsü: risk_tick/marklar çalışır, -5 SAFE_EXIT, -10
            global HALT (PAUSE'tan BAĞIMSIZ), pozisyon kapanmaz, koruma
            emri iptal edilmez/yeniden yerleşir
  D (20-22) strateji çıkışı: ters H1 flip çıkış bacağı çalışır, aynı
            olayda giriş bacağı bloke, iki bacak bağımsız doğrulanır
  E (23-29) yarış koşulları: süreç içi thread yarışı + GERÇEK ayrı OS
            süreçleriyle (child + canary_control.py CLI) yarış kanıtı
  F (30-35) durum/audit: status salt-okunur, borsa API'si yok, audit
            kayıtları, bekleyen emirler görünür, restart, PAUSE/HALT ayrımı

PAUSE, HALT state machine'i değiştirMEZ ve yeniden kullanMAZ; mevcut
operatör limitleri (2 pozisyon / 100+100 notional / -5 / -10) değişmez.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, loss_mark, T0)

BAR = 4 * 3600 * 1000
from canary_live import (set_entry_pause, read_entry_pause,
                         entry_gate_lock_path, HALT_ACTIVE, HALT_LOCKED)
from canary_risk import STATUS_HALTED, STATUS_ACTIVE

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTRL = os.path.join(REPO, "canary_control.py")


def run_cli(args, timeout=120):
    """canary_control.py'yi GERÇEK ayrı süreçte çalıştır (E.29/F)."""
    return subprocess.run([sys.executable, CTRL] + args,
                          capture_output=True, text=True, cwd=REPO,
                          timeout=timeout)


def cancel_protection(ex, cid):
    ex.algos[cid]["status"] = "CANCELED"


# E.23-27/29: ayrı süreçte çalışan giriş child'ı — new_order, dosya
# bazlı barrier'de bekler (kontrollü yarış); sonuçları JSON'a yazar.
CHILD_SCRIPT = '''
import json, os, sys, time
REPO, TMP = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO)
from tests.blok28_1_fake import FakeExchange, FakeClock, make_canary, prime, T0

IN_FLIGHT = os.path.join(TMP, "in_flight")
RELEASE = os.path.join(TMP, "release")
PAUSED_MARKER = os.path.join(TMP, "paused")
RESULT = os.path.join(TMP, "child_result.json")
BAR = 4 * 3600 * 1000

clock = FakeClock()
ex = FakeExchange(clock_ms=clock)
orig_new_order = ex.new_order
state = {"calls": 0}


def blocking_new_order(*a, **kw):
    state["calls"] += 1
    with open(IN_FLIGHT, "w") as f:
        f.write(str(state["calls"]))
    deadline = time.time() + 60
    while not os.path.exists(RELEASE) and time.time() < deadline:
        time.sleep(0.01)
    return orig_new_order(*a, **kw)


ex.new_order = blocking_new_order
lc = make_canary(TMP, ex)
prime(lc)
rec1 = lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR, clock())[0]
deadline = time.time() + 60
while not os.path.exists(PAUSED_MARKER) and time.time() < deadline:
    time.sleep(0.01)
rec2 = lc.try_entry("ETHUSDT", 1, 100.0, T0 + 2 * BAR, clock())[0]
with open(RESULT, "w") as f:
    json.dump({"first": rec1.get("result"), "second": rec2.get("result"),
               "second_blockers": rec2.get("blockers"),
               "new_order_calls": state["calls"]}, f)
'''


class PauseTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)
        self.db = os.path.join(self.tmp.name, "canary_live.db")

    def tearDown(self):
        self.tmp.cleanup()

    # ---- yardımcılar -------------------------------------------------------
    def _new_order_posts(self):
        return [c for c in self.ex.post_calls
                if c["method"] == "new_order"]

    def _n_orders_db(self):
        return self.lc.db.execute(
            "SELECT COUNT(*) FROM live_orders").fetchone()[0]

    def _risk_events(self, kind=None):
        q = "SELECT ts, kind, detail FROM risk_events"
        if kind:
            q += f" WHERE kind='{kind}'"
        return self.lc.db.execute(q + " ORDER BY ts").fetchall()


class TestBasicPause(PauseTestBase):
    """A (1-6): temel PAUSE davranışı ve kalıcılık."""

    def test_1_fresh_db_defaults_to_not_paused(self):
        """(1) Yeni/boş DB: entry_paused varsayılan 0; anahtar yazılmadan
        bile okuma 0 döner."""
        st = read_entry_pause(self.db)
        self.assertFalse(st["entry_paused"])
        self.assertFalse(self.lc.entry_paused)
        # anahtar hiç yazılmadı — varsayılan davranış
        self.assertIsNone(self.lc._get_state("entry_paused"))

    def test_2_pause_sets_one_with_audit(self):
        """(2) PAUSE → durum 1 + audit kaydı."""
        r = set_entry_pause(self.db, True, "birim test nedeni", "tester")
        self.assertTrue(r["paused"])
        self.assertEqual(r["previous"], "0")
        self.assertTrue(read_entry_pause(self.db)["entry_paused"])
        self.assertTrue(self.lc.entry_paused)
        ev = self._risk_events("ENTRY_PAUSED")
        self.assertEqual(len(ev), 1)
        self.assertIn("birim test nedeni", ev[0][2])
        self.assertIn("tester", ev[0][2])
        self.assertGreater(ev[0][0], 0)      # zaman damgası

    def test_3_4_pause_survives_restart_and_constructor_never_resets(self):
        """(3+4) Yeniden başlatma ve constructor PAUSE'u sıfırlayamaz."""
        set_entry_pause(self.db, True, "restart testi", "tester")
        lc2 = make_canary(self.tmp.name, self.ex)      # "restart"
        self.assertTrue(lc2.entry_paused)
        self.assertEqual(lc2._get_state("entry_paused"), "1")
        prime(lc2)                     # startup reconcile + risk tick
        self.assertTrue(lc2.entry_paused)
        lc3 = make_canary(self.tmp.name, self.ex)      # ikinci restart
        self.assertTrue(lc3.entry_paused)
        self.assertEqual(lc3._get_state("entry_paused"), "1")

    def test_5_resume_requires_explicit_confirm(self):
        """(5) resume yalnız --confirm ile çalışır."""
        set_entry_pause(self.db, True, "confirm testi", "tester")
        r = run_cli(["resume", "--db", self.db])
        self.assertEqual(r.returncode, 3)
        self.assertIn("--confirm", r.stdout)
        self.assertTrue(read_entry_pause(self.db)["entry_paused"])
        r2 = run_cli(["resume", "--confirm", "--db", self.db])
        self.assertEqual(r2.returncode, 0)
        self.assertFalse(read_entry_pause(self.db)["entry_paused"])

    def test_6_resume_only_clears_pause_other_gates_survive(self):
        """(6) resume SADECE PAUSE'u kaldırır: HALT/risk durumu ve diğer
        güvenlik blokları aynen kalır."""
        set_entry_pause(self.db, True, "resume kapsamı", "tester")
        self.lc._set_state("unowned_position_seen", 1)   # başka blok
        r = run_cli(["resume", "--confirm", "--db", self.db])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.lc.entry_paused)
        # diğer bloklar DURUYOR
        self.assertEqual(self.lc._get_state("unowned_position_seen"), "1")
        self.assertEqual(self.lc.halt_state, HALT_ACTIVE)
        self.assertEqual(self.lc.rm.status, STATUS_ACTIVE)
        out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        joined = " ".join(out[0]["blockers"])
        self.assertIn("sahipliği kanıtlanamayan", joined)
        self.assertNotIn("ENTRY_PAUSED", joined)


class TestEntryBlocking(PauseTestBase):
    """B (7-12): PAUSE aktifken gerçek emir gönderilemez."""

    def test_7_8_9_10_blocked_reason_no_order_no_intent(self):
        """(7-10) try_entry → BLOCKED; neden ENTRY_PAUSED; new_order
        çağrısı 0; INTENT kaydı 0."""
        set_entry_pause(self.db, True, "engelleme testi", "tester")
        out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("ENTRY_PAUSED", " ".join(out[0]["blockers"]))
        self.assertEqual(self._new_order_posts(), [])
        self.assertEqual(self._n_orders_db(), 0)
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)

    def test_11_repeated_signals_cannot_bypass(self):
        """(11) Aynı sinyal tekrar tekrar denendiğinde PAUSE aşılamaz."""
        set_entry_pause(self.db, True, "tekrar testi", "tester")
        for i in range(1, 6):        # farklı bar_ts'ler (dedup'a takılmaz)
            out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + i * BAR,
                                    self.clock())
            self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertEqual(self._new_order_posts(), [])
        self.assertEqual(self._n_orders_db(), 0)

    def test_12_resume_does_not_replay_old_signal(self):
        """(12) PAUSE sırasında engellenen sinyal resume sonrası OTOMATİK
        oynatılmaz (sig dedup PAUSE'dan ÖNCE yazılır — mevcut davranış
        korunur); yalnız YENİ sinyal değerlendirilir."""
        set_entry_pause(self.db, True, "dedup testi", "tester")
        out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        set_entry_pause(self.db, False, "resume", "tester")
        # aynı sinyal (aynı bar_ts) → DUPLICATE_SIGNAL, emir YOK
        out2 = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR,
                                 self.clock())
        self.assertEqual(out2[0]["result"], "DUPLICATE_SIGNAL")
        self.assertEqual(self._new_order_posts(), [])
        # YENİ sinyal (yeni bar_ts) → normal akış
        out3 = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + 2 * BAR,
                                 self.clock())
        self.assertEqual(out3[0]["result"], "FILLED")
        self.assertEqual(out3[0]["protection"]["status"], "PROTECTED")


class TestRiskLoopPreservation(PauseTestBase):
    """C (13-19): PAUSE sırasında risk yönetimi kesintisiz çalışır."""

    def test_13_14_risk_tick_and_marks_continue(self):
        """(13+14) risk_tick çalışır; mark kayıtları güncellenir."""
        open_one(self.lc, "ETHUSDT", 1)
        set_entry_pause(self.db, True, "risk döngüsü testi", "tester")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 101.0)
        cyc = self.lc.risk_tick()
        self.assertEqual(cyc["kind"], "RISK_TICK")
        self.assertTrue(cyc["entry_paused"])          # izleme alanı
        self.assertGreater(cyc["marks_n"], 0)
        self.assertEqual(self.lc.rm._mark("ETHUSDT", self.clock()),
                         101.0)
        self.assertEqual(cyc["rm_status"], STATUS_ACTIVE)
        self.assertEqual(cyc["open_positions"], 1)

    def test_15_safe_exit_minus_5_while_paused(self):
        """(15) PAUSE açıkken -5 USDT net zarar → SAFE_EXIT çalışır."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        cancel_protection(self.ex, f"C281P{rec['pos_id']}")
        set_entry_pause(self.db, True, "safe exit testi", "tester")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT",
                         loss_mark(self.lc, rec["pos_id"], 5.00))  # tetik
        cyc = self.lc.risk_tick()
        self.assertIn({"pos_id": rec["pos_id"], "action": "SAFE_EXIT"},
                      cyc["risk_actions"])
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"],
                         "CLOSED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        exits = [c for c in self._new_order_posts() if c["reduce_only"]]
        self.assertEqual(len(exits), 1)          # reduce-only çıkış gitti

    def test_16_17_total_minus_10_halts_independently_of_pause(self):
        """(16+17) PAUSE açıkken toplam -10 → global HALT zinciri
        PAUSE'tan bağımsız yürür (PAUSE bastırmaz/geciktirmez)."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        cancel_protection(self.ex, f"C281P{rec['pos_id']}")
        set_entry_pause(self.db, True, "halt testi", "tester")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT",
                         loss_mark(self.lc, rec["pos_id"], 10.00))  # toplam
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertNotEqual(self.lc.halt_state, HALT_ACTIVE)
        for _ in range(12):                      # zinciri LOCKED'a sürükle
            self.clock.advance(10_000)
            self.lc.risk_tick()
            if self.lc.halt_state == HALT_LOCKED:
                break
        self.assertEqual(self.lc.halt_state, HALT_LOCKED)
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        # PAUSE hâlâ AÇIK — iki durum bağımsız okunur (F.35 önizlemesi)
        self.assertTrue(self.lc.entry_paused)
        self.assertEqual(self.lc.halt_state, HALT_LOCKED)

    def test_18_pause_alone_does_not_close_position(self):
        """(18) Pozisyon yalnızca PAUSE açıldı diye KAPANMAZ."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        n_posts = len(self._new_order_posts())   # yalnız ilk giriş emri
        set_entry_pause(self.db, True, "pozisyon korunması testi", "tester")
        for _ in range(5):                       # nötr markla çoklu tick
            self.clock.advance(10_000)
            self.ex.set_mark("ETHUSDT", 100.0)
            self.lc.risk_tick()
        pos = self.lc.rm._pos(rec["pos_id"])
        self.assertEqual(pos["status"], "OPEN")
        self.assertAlmostEqual(self.ex.positions["ETHUSDT"]["positionAmt"],
                               pos["units"], places=9)
        self.assertEqual(len(self._new_order_posts()), n_posts)  # ek emir YOK

    def test_19_protection_orders_maintained_not_canceled(self):
        """(19) PAUSE koruma emirlerini iptal etmez; doğrulama/yeniden
        yerleştirme döngüsü çalışmaya devam eder."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        cid = f"C281P{rec['pos_id']}"
        set_entry_pause(self.db, True, "koruma testi", "tester")
        cancel_protection(self.ex, cid)      # DIŞARIDAN silindi (simülasyon)
        self.clock.advance(60_000)           # koruma kadansı (30s)
        self.ex.set_mark("ETHUSDT", 100.0)
        self.lc.risk_tick()
        # koruma YENİDEN yerleştirildi → borsada AÇIK
        self.assertEqual(self.ex.algos[cid]["status"], "NEW")
        self.assertEqual(self.lc._live_pos_row(rec["pos_id"])
                         ["protection_status"], "PROTECTED")
        self.assertEqual(self.lc.rm._pos(rec["pos_id"])["status"], "OPEN")


class TestStrategyExit(PauseTestBase):
    """D (20-22): PAUSE açıkken H1 flip'in risk-azaltan çıkış bacağı."""

    def _warmup(self, now):
        kl = {s: [(now - 4 * BAR + i * BAR, 100.0, 100.0, 1.0)
                  for i in range(3)] for s in self.ex.symbols}
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

    def _fresh_marks(self, now):
        self.clock.t = now
        for s in self.ex.symbols:
            self.ex.set_mark(s, 100.0)
            self.lc.rm.update_mark(s, 100.0, now)

    def _flip(self, symbol, direction, bar_ot, now):
        S = self.lc.engine._state(symbol)
        S["pend"] = direction
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = bar_ot - BAR
        S["pend_price"] = 100.0
        kl = {symbol: [(bar_ot, 100.0, 101.0, 1.0)]}
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

    def test_20_21_22_flip_exit_works_entry_leg_blocked(self):
        """(20+21+22) Aynı flip olayında: çıkış bacağı ÇALIŞIR, giriş
        bacağı BLOKE olur; ikisi bağımsız doğrulanır."""
        self._warmup(T0)
        now = T0 + 2 * BAR
        self._fresh_marks(now)
        out = self._flip("ETHUSDT", 1, T0 + BAR, now)     # long aç
        self.assertEqual(out[0]["result"], "FILLED")
        pos_id = out[0]["pos_id"]
        set_entry_pause(self.db, True, "flip exit testi", "tester")
        now2 = T0 + 4 * BAR
        self._fresh_marks(now2)
        out2 = self._flip("ETHUSDT", -1, T0 + 3 * BAR, now2)   # ters flip
        # (20) ÇIKIŞ bacağı: reduce-only + CLOSED + borsada flat
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["exit_reason"], "H1_FLIP_STRATEGY_EXIT")
        self.assertEqual(lp["exit_status"], "EXIT_CONFIRMED")
        self.assertEqual(self.lc.rm._pos(pos_id)["status"], "CLOSED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        # (21) GİRİŞ bacağı: aynı olayda BLOKE
        self.assertEqual(out2[0]["result"], "BLOCKED")
        self.assertIn("ENTRY_PAUSED", " ".join(out2[0]["blockers"]))
        # (22) bağımsız doğrulama: çıkış emri FILLED, giriş emri YOK
        exits = [c for c in self._new_order_posts() if c["reduce_only"]]
        entries = [c for c in self._new_order_posts() if not c["reduce_only"]]
        self.assertEqual(len(exits), 1)          # çıkış gitti (SELL)
        self.assertEqual(exits[0]["side"], "SELL")
        self.assertEqual(len(entries), 1)        # yalnız ilk long girişi
        row = self.lc._order_row(exits and
                                 [c["client_order_id"] for c in exits][0])
        self.assertEqual(row["status"], "FILLED")


class TestRaceConditions(PauseTestBase):
    """E (23-29): eşzamanlılık — ortak flock kapısı."""

    def test_23_to_27_inprocess_thread_race(self):
        """(23-27) Giriş emir gönderim aşamasındayken PAUSE talebi:
        pause, giriş sonuçlanmadan TAMAMLANMAZ; sonuçlandıktan sonra
        kalıcı olur ve yeni try_entry emir GÖNDERMEZ."""
        in_flight = threading.Event()
        release = threading.Event()
        orig = self.ex.new_order
        calls = []

        def blocking_new_order(*a, **kw):
            calls.append(1)
            in_flight.set()
            release.wait(10)                 # barrier: emir 'yolda'
            return orig(*a, **kw)
        self.ex.new_order = blocking_new_order
        results = {}

        def do_entry():
            results["rec"] = self.lc.try_entry(
                "ETHUSDT", 1, 100.0, T0 + BAR, self.clock())[0]
        t_entry = threading.Thread(target=do_entry)
        t_entry.start()
        self.assertTrue(in_flight.wait(5))    # emir gönderim aşamasında

        # (24) ayrı 'operatör' akışı: PAUSE talebi
        pause_done = threading.Event()

        def do_pause():
            set_entry_pause(self.db, True, "yarış testi", "race-tester")
            pause_done.set()
        t_pause = threading.Thread(target=do_pause)
        t_pause.start()
        # (25) giriş sonuçlanmadan PAUSE etkinleşti bildirilMEdi
        self.assertFalse(pause_done.wait(1.0))
        self.assertFalse(pause_done.is_set())
        self.assertFalse(read_entry_pause(self.db)["entry_paused"])
        # (26) giriş tamamlansın → PAUSE kalıcı hale gelir
        release.set()
        t_entry.join(10)
        t_pause.join(10)
        self.assertFalse(t_entry.is_alive())
        self.assertFalse(t_pause.is_alive())
        self.assertEqual(results["rec"]["result"], "FILLED")
        self.assertTrue(read_entry_pause(self.db)["entry_paused"])
        # (27) bu noktadan sonra yeni try_entry → new_order ÇAĞRILMAZ
        out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("ENTRY_PAUSED", " ".join(out[0]["blockers"]))
        self.assertEqual(len(calls), 1)

    def test_28_pause_via_cli_then_entry_not_sent(self):
        """(28) Alternatif senaryo: PAUSE önce tamamlanır → ardından
        başlayan giriş isteği kesinlikle gönderilMEZ (CLI + gerçek süreç)."""
        r = run_cli(["pause", "--db", self.db, "--reason",
                     "cli pause yarışı"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("RESULT: OK", r.stdout)
        self.assertTrue(self.lc.entry_paused)
        out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("ENTRY_PAUSED", " ".join(out[0]["blockers"]))
        self.assertEqual(self._new_order_posts(), [])
        self.assertEqual(self._n_orders_db(), 0)

    def test_29_cross_process_race_real_os_processes(self):
        """(29) GERÇEK ayrı OS süreçleri: child süreci emir gönderim
        aşamasındayken ayrı süreçte CLI pause — pause bekler, sonra
        kalıcı olur; child'ın ikinci denemesi emir gönderMEZ."""
        with tempfile.TemporaryDirectory() as tmp:
            child = os.path.join(tmp, "entry_child.py")
            with open(child, "w") as f:
                f.write(CHILD_SCRIPT)
            db = os.path.join(tmp, "canary_live.db")
            p_child = subprocess.Popen(
                [sys.executable, child, REPO, tmp], cwd=REPO,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # child emir gönderim aşamasına ulaşana dek bekle
            marker = os.path.join(tmp, "in_flight")
            deadline = time.time() + 30
            while not os.path.exists(marker) and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(os.path.exists(marker),
                            "child giriş aşamasına ulaşamadı")
            # PAUSE — ayrı süreç (canary_control.py)
            p_pause = subprocess.Popen(
                [sys.executable, CTRL, "pause", "--db", db, "--reason",
                 "cross-process yarış testi", "--timeout", "60"],
                cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            # (25) giriş sonuçlanmadan PAUSE başarı bildirmemiş olmalı
            time.sleep(2.0)
            self.assertIsNone(p_pause.poll(),
                              "pause, giriş sürerken erken döndü!")
            self.assertFalse(read_entry_pause(db)["entry_paused"])
            # girişi serbest bırak → tamamlansın
            open(os.path.join(tmp, "release"), "w").close()
            out_pause, _ = p_pause.communicate(timeout=60)
            self.assertEqual(p_pause.returncode, 0, out_pause)
            self.assertIn("RESULT: OK", out_pause)
            self.assertTrue(read_entry_pause(db)["entry_paused"])
            # child ikinci denemeyi yapar (paused işaretinden sonra)
            open(os.path.join(tmp, "paused"), "w").close()
            _, err = p_child.communicate(timeout=60)
            self.assertEqual(p_child.returncode, 0, err)
            with open(os.path.join(tmp, "child_result.json")) as f:
                r = json.load(f)
            self.assertEqual(r["first"], "FILLED")
            self.assertEqual(r["second"], "BLOCKED")
            self.assertIn("ENTRY_PAUSED",
                          " ".join(r["second_blockers"] or []))
            self.assertEqual(r["new_order_calls"], 1)


class TestStatusAndAudit(PauseTestBase):
    """F (30-35): status/audit/kalıcılık/PAUSE-HALT ayrımı."""

    def _snapshot(self):
        db = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        ls = sorted(db.execute("SELECT key, value FROM live_state"))
        ev = sorted(db.execute("SELECT ts, kind, detail FROM risk_events"))
        db.close()
        return ls, ev

    def test_30_31_status_read_only_no_exchange(self):
        """(30+31) status salt-okunur: DB içeriği DEĞİŞMEZ; borsa emir
        API'si çağrılmaz (araçta emir/iptal çağrısı yok — yapısal)."""
        set_entry_pause(self.db, True, "status testi", "tester")
        before = self._snapshot()
        r = run_cli(["status", "--db", self.db])
        self.assertEqual(r.returncode, 0)
        self.assertIn("entry_paused             : 1", r.stdout)
        self.assertIn("halt_state", r.stdout)
        self.assertIn("rm_status", r.stdout)
        self.assertIn(self.db, r.stdout)
        self.assertIn(entry_gate_lock_path(self.db), r.stdout)
        self.assertIn("salt-okunur", r.stdout)
        after = self._snapshot()
        self.assertEqual(before, after)         # hiçbir şey yazılmadı
        # yapısal kanıt: araç kaynak kodunda emir/iptal çağrısı yok
        with open(CTRL) as f:
            src = f.read()
        for forbidden in ("new_order", "cancel_order", "new_algo_order",
                          "cancel_algo_order"):
            self.assertNotIn(forbidden, src)

    def test_32_pause_resume_audit_events(self):
        """(32) PAUSE/RESUME olayları zaman damgasıyla audit kaydına geçer."""
        r1 = run_cli(["pause", "--db", self.db, "--reason",
                      "audit nedeni XYZ"])
        r2 = run_cli(["resume", "--confirm", "--db", self.db,
                      "--reason", "audit resume"])
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        ev = self._risk_events("ENTRY_PAUSED") + \
            self._risk_events("ENTRY_RESUMED")
        kinds = {e[1] for e in ev}
        self.assertEqual(kinds, {"ENTRY_PAUSED", "ENTRY_RESUMED"})
        self.assertTrue(all(e[0] > 0 for e in ev))
        joined = " ".join(e[2] for e in ev)
        self.assertIn("audit nedeni XYZ", joined)
        self.assertIn("cli:pid=", joined)       # işlemi yapan araç bilgisi
        # kalıcı durum anahtarları
        st = read_entry_pause(self.db)
        self.assertEqual(st["entry_pause_last_action"], "ENTRY_RESUMED")
        self.assertIsNotNone(st["entry_pause_ts"])
        self.assertEqual(st["entry_pause_reason"], "audit resume")

    def test_33_status_shows_pending_entry_orders(self):
        """(33) Bekleyen/UNKNOWN giriş emirleri status çıktısında görünür."""
        self.ex.no_apply_timeouts.add("C281E1")   # giriş UNKNOWN kalır
        rec = open_one(self.lc, "ETHUSDT", 1)
        self.assertIn("ENTRY_UNKNOWN", rec["result"])
        r = run_cli(["status", "--db", self.db])
        self.assertEqual(r.returncode, 0)
        self.assertIn("C281E1", r.stdout)
        self.assertIn("UNKNOWN", r.stdout)
        self.assertIn("bekleyen giriş emirleri  : 1", r.stdout)

    def test_34_service_restart_keeps_entries_paused(self):
        """(34) PAUSE sonrası servis yeniden başlasa girişler PAUSED
        kalır (restart, prime dahil)."""
        set_entry_pause(self.db, True, "restart kalıcılık", "tester")
        lc2 = make_canary(self.tmp.name, self.ex)      # restart simülasyonu
        prime(lc2)
        self.assertTrue(lc2.entry_paused)
        out = lc2.try_entry("ETHUSDT", 1, 100.0, T0 + BAR,
                            self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("ENTRY_PAUSED", " ".join(out[0]["blockers"]))
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)

    def test_35_pause_and_halt_read_independently_resume_keeps_halt(self):
        """(35) PAUSE ve global HALT birbirinden bağımsız okunur; resume
        HALT'ı sıfırlaMAZ (operator_reset/operator_release çağrılmaz)."""
        rec = open_one(self.lc, "ETHUSDT", 1)
        cancel_protection(self.ex, f"C281P{rec['pos_id']}")
        set_entry_pause(self.db, True, "ayrım testi", "tester")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT",
                         loss_mark(self.lc, rec["pos_id"], 10.00))  # HALT
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        # bağımsız okuma: PAUSE=1 VE HALT zinciri aynı anda
        st = read_entry_pause(self.db)
        self.assertTrue(st["entry_paused"])
        self.assertNotEqual(st["halt_state"], HALT_ACTIVE)
        self.assertEqual(st["rm_status"], STATUS_HALTED)
        # resume → PAUSE kalkar, HALT KALIR
        r = run_cli(["resume", "--confirm", "--db", self.db])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(read_entry_pause(self.db)["entry_paused"])
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertNotEqual(self.lc.halt_state, HALT_ACTIVE)
        # HALT'ta giriş hâlâ bloklu — neden bu kez HALT, PAUSE değil
        out = self.lc.try_entry("ETHUSDT", 1, 100.0, T0 + 2 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        joined = " ".join(out[0]["blockers"])
        self.assertIn("HALT", joined)
        self.assertNotIn("ENTRY_PAUSED", joined)


if __name__ == "__main__":
    unittest.main()
