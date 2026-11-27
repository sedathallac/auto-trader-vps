"""GÖREV 2026-11-27 ENGEL 1: brain_exit_policy — operatör çıkış politikası
yazım mekanizması + C3 kapısı + RISK_AND_H1'te mevcut H1 hedef-yön çıkışının
beyin pozisyonları için DE çalıştığının kanıtı.

Sözleşme (görev §ENGEL 1):
  - Politika YALNIZ resmî kontrol mekanizmasıyla yazılır
    (set_brain_exit_policy / canary_control.py policy) — canlı DB'ye
    doğrudan SQL YOK. Her yazım audit (risk_events) üretir.
  - Yeni çıkış mantığı/eşiği İCAT EDİLMEZ: RISK_AND_H1 yalnız mevcut
    _quality_target_exits mekanizmasını beyin pozisyonlarına bağlar.
  - H1 GİRİŞ yolları beyin modunda inert kalır (savunma derinliği).
  - Onay yoksa / geçersiz değerde beyin adayı BLOCKED (fail-closed).
"""
import fcntl
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import FakeClock, FakeExchange, make_canary, prime, T0
from tests.test_brain_primary import (BrainPrimaryBase, NOW, SYM, NEW_SYM,
                                      brain_record, make_brain_db)
import canary_quality as cq
from canary_live import (BRAIN_EXIT_POLICIES, BRAIN_EXIT_POLICY_KEY,
                         BRAIN_EXIT_POLICY_RISK_AND_H1,
                         BRAIN_EXIT_POLICY_RISK_ONLY, EntryGateBusy,
                         entry_gate, entry_gate_lock_path,
                         read_entry_pause, set_brain_exit_policy,
                         set_direction_enabled, X_PENDING, X_CONFIRMED,
                         X_UNCONFIRMED)
from canary_brain_adapter import SIGNAL_SOURCE_BRAIN, decision_id_for
from canary_brain import MODEL_VERSION

BAR = 4 * 3600 * 1000
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTRL = os.path.join(REPO, "canary_control.py")


def run_cli(args, timeout=120):
    """canary_control.py'yi GERÇEK ayrı süreçte çalıştır."""
    return subprocess.run([sys.executable, CTRL] + args,
                          capture_output=True, text=True, cwd=REPO,
                          timeout=timeout)


# ---------------------------------------------------------------------------
# 1) Resmî yazım mekanizması: set_brain_exit_policy
# ---------------------------------------------------------------------------
class TestSetBrainExitPolicy(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "canary_live.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self):
        # tablolar hiç oluşmadıysa (hiçbir yazım yapılmadı) → boş durum
        if not os.path.exists(self.db):
            return None, []
        con = sqlite3.connect(self.db)
        try:
            has = con.execute(
                "SELECT name FROM sqlite_master WHERE "
                "name='live_state'").fetchone()
            if not has:
                return None, []
            row = con.execute(
                "SELECT value FROM live_state WHERE key=?",
                (BRAIN_EXIT_POLICY_KEY,)).fetchone()
            evs = con.execute(
                "SELECT kind, detail FROM risk_events WHERE "
                "kind='BRAIN_EXIT_POLICY_SET' ORDER BY ts").fetchall()
            return (row[0] if row else None), evs
        finally:
            con.close()

    def test_valid_write_sets_key_and_audit(self):
        r = set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_AND_H1,
                                  "ENGEL 1 onayı", "test:op")
        self.assertEqual(r["action"], "BRAIN_EXIT_POLICY_SET")
        self.assertEqual(r["policy"], BRAIN_EXIT_POLICY_RISK_AND_H1)
        self.assertIsNone(r["previous"])
        val, evs = self._state()
        self.assertEqual(val, BRAIN_EXIT_POLICY_RISK_AND_H1)
        self.assertEqual(len(evs), 1)
        self.assertIn("policy=RISK_AND_H1", evs[0][1])
        self.assertIn("reason=ENGEL 1 onayı", evs[0][1])

    def test_second_write_updates_previous(self):
        set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_ONLY,
                              "ilk", "test:op")
        r = set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_AND_H1,
                                  "ikinci", "test:op")
        self.assertEqual(r["previous"], BRAIN_EXIT_POLICY_RISK_ONLY)
        val, evs = self._state()
        self.assertEqual(val, BRAIN_EXIT_POLICY_RISK_AND_H1)
        self.assertEqual(len(evs), 2)          # her yazım ayrı audit

    def test_invalid_policy_rejected_nothing_written(self):
        for bad in ("", None, "risk_only", "RISK_AND_H2", "HEPSI_ACIK", 1):
            with self.assertRaises(ValueError):
                set_brain_exit_policy(self.db, bad, "x", "test:op")
        val, evs = self._state()
        self.assertIsNone(val)                  # anahtar YOK (fail-closed)
        self.assertEqual(evs, [])               # audit DE YOK

    def test_entry_gate_busy_leaves_state_unchanged(self):
        set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_ONLY,
                              "ilk", "test:op")
        # giriş kapısı kilidini BAŞKA fd'den tut (devam eden giriş zinciri
        # simülasyonu) — süreç içi farklı fd'ler birbirini engeller
        f = open(entry_gate_lock_path(self.db), "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(EntryGateBusy):
                set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_AND_H1,
                                      "x", "test:op", timeout_s=0.3)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()
        val, evs = self._state()
        self.assertEqual(val, BRAIN_EXIT_POLICY_RISK_ONLY)   # DEĞİŞMEDİ
        self.assertEqual(len(evs), 1)                        # yeni audit YOK

    def test_write_does_not_touch_other_control_keys(self):
        set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_AND_H1,
                              "op", "test:op")
        con = sqlite3.connect(self.db)
        try:
            # pause/yön kilitleri gibi DİĞER kontrol anahtarları yazılmadı
            keys = [r[0] for r in con.execute(
                "SELECT key FROM live_state").fetchall()]
            self.assertEqual(keys, [BRAIN_EXIT_POLICY_KEY])
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 2) canary_control.py policy komutu (resmî operatör CLI)
# ---------------------------------------------------------------------------
class TestCanaryControlPolicyCLI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "canary_live.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _policy_value(self):
        st = read_entry_pause(self.db)
        return st.get("brain_exit_policy")

    def test_policy_without_confirm_fails(self):
        r = run_cli(["policy", BRAIN_EXIT_POLICY_RISK_AND_H1,
                     "--db", self.db, "--reason", "op onayı"])
        self.assertEqual(r.returncode, 3)       # EXIT_USAGE
        self.assertIsNone(self._policy_value()) # yazılmadı

    def test_policy_with_confirm_writes_and_audits(self):
        r = run_cli(["policy", BRAIN_EXIT_POLICY_RISK_AND_H1, "--confirm",
                     "--db", self.db, "--reason", "ENGEL 1 operatör onayı"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("RESULT: OK", r.stdout)
        self.assertEqual(self._policy_value(), BRAIN_EXIT_POLICY_RISK_AND_H1)
        con = sqlite3.connect(self.db)
        try:
            n = con.execute("SELECT COUNT(*) FROM risk_events WHERE "
                            "kind='BRAIN_EXIT_POLICY_SET'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 1)

    def test_policy_invalid_value_rejected_by_choices(self):
        # argparse choices → bilinmeyen değer hiç işlenmez (SystemExit 2)
        with self.assertRaises(SystemExit) as cm:
            import canary_control
            canary_control.main(["policy", "HEPSI_ACIK", "--confirm",
                                 "--db", self.db])
        self.assertEqual(cm.exception.code, 2)
        self.assertIsNone(self._policy_value())

    def test_status_shows_policy(self):
        set_brain_exit_policy(self.db, BRAIN_EXIT_POLICY_RISK_AND_H1,
                              "op", "test:op")
        r = run_cli(["status", "--db", self.db])
        self.assertEqual(r.returncode, 0)
        self.assertIn("brain_exit_policy", r.stdout)
        self.assertIn(BRAIN_EXIT_POLICY_RISK_AND_H1, r.stdout)

    def test_status_flags_missing_policy(self):
        r = run_cli(["status", "--db", self.db])
        self.assertEqual(r.returncode, 0)
        self.assertIn("YOK", r.stdout)          # politika yok → görünür


# ---------------------------------------------------------------------------
# 3) C3 kapısı: politika değeri → beyin adayı akışı
# ---------------------------------------------------------------------------
class TestBrainExitPolicyGate(BrainPrimaryBase):

    def test_risk_and_h1_candidate_proceeds_past_c3(self):
        set_brain_exit_policy(self.lc.db_path, BRAIN_EXIT_POLICY_RISK_AND_H1,
                              "ENGEL 1", "test:op")
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
        self.assertEqual(rec["result"], "FILLED")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    def test_risk_only_candidate_proceeds_past_c3(self):
        # setUp zaten RISK_ONLY yazdı — açık ayrı kanıt
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
        self.assertEqual(rec["result"], "FILLED")

    def test_garbage_policy_value_blocks(self):
        # bozuk/manuel yazılmış değer → fail-closed BLOCKED
        self.lc._set_state(BRAIN_EXIT_POLICY_KEY, "HOP_DURUM")
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "BRAIN_DECISION"][0]
        self.assertEqual(rec["result"], "BLOCKED")
        self.assertIn("BRAIN_EXIT_POLICY_UNDEFINED",
                      " ".join(rec["blockers"]))
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out))
        self.assertEqual(len(self.lc.rm.open_positions()), 0)

    def test_missing_policy_blocks(self):
        con = sqlite3.connect(self.lc.db_path)
        try:
            con.execute("DELETE FROM live_state WHERE key=?",
                        (BRAIN_EXIT_POLICY_KEY,))
            con.commit()
        finally:
            con.close()
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "BRAIN_DECISION"][0]
        self.assertEqual(rec["result"], "BLOCKED")
        self.assertIn("BRAIN_EXIT_POLICY_UNDEFINED",
                      " ".join(rec["blockers"]))

    def test_blocked_by_missing_policy_consumes_decision(self):
        """Politika kapısında bloklanan karar sayılır (brain sinyal dedup) —
        ikinci tick'te DUPLICATE; politika SONRA açılsa da eski karar
        oynatılmaz."""
        con = sqlite3.connect(self.lc.db_path)
        try:
            con.execute("DELETE FROM live_state WHERE key=?",
                        (BRAIN_EXIT_POLICY_KEY,))
            con.commit()
        finally:
            con.close()
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        self._brain_tick()
        # politika onaylanıyor — ama aynı karar geri gelmez
        set_brain_exit_policy(self.lc.db_path, BRAIN_EXIT_POLICY_RISK_AND_H1,
                              "geç onay", "test:op")
        out2 = self._brain_tick()
        self.assertTrue(any(r.get("result") == "DUPLICATE_BRAIN_DECISION"
                            for r in out2))
        self.assertEqual(len(self.lc.rm.open_positions()), 0)


# ---------------------------------------------------------------------------
# 4) RISK_AND_H1: mevcut H1 hedef-yön çıkışı beyin pozisyonlarında ÇALIŞIR
# ---------------------------------------------------------------------------
class TestBrainH1TargetExit(BrainPrimaryBase):
    """Diferansiyel kanıt: aynı girdiyle RISK_AND_H1 çıkışı AÇAR, RISK_ONLY
    açmaz; kline çekimi YALNIZ açık pozisyonlu semboller için."""

    def setUp(self):
        super().setUp()
        # resmî mekanizmayla RISK_AND_H1 (ENGEL 1 operatör onayı simülasyonu)
        set_brain_exit_policy(self.lc.db_path, BRAIN_EXIT_POLICY_RISK_AND_H1,
                              "ENGEL 1 onayı", "test:op")

    def _open_long_position(self):
        set_direction_enabled(self.lc.db_path, 1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, 1)])
        out = self._brain_tick()
        rec = [r for r in out if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
        self.assertEqual(rec["result"], "FILLED")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    def _feed_falling_klines(self, *, n_bars=400, start_i=2):
        """DÜŞEN seri → H1 motoru ısınır, direction=-1. Barlar KAPALI olacak
        şekilde saat ilerletilir (açık mum girdi DEĞİLDİR). Satırlar borsa
        kline biçimindedir (k[4]=close, k[7]=quoteVolume)."""
        kl = []
        for i in range(n_bars):
            ot = T0 + (start_i + i) * BAR
            px = 100.0 * math.pow(0.995, i)
            kl.append([ot, str(px), str(px), str(px), str(px), "0",
                       ot + BAR, "1e6"])
        self.ex.kline_data[SYM] = kl
        last_close = T0 + (start_i + n_bars) * BAR
        self.clock.advance(last_close - self.clock())
        return kl

    def _wrap_klines_recorder(self):
        calls = []
        orig = self.ex.klines

        def rec(symbol, *a, **kw):
            calls.append(symbol)
            return orig(symbol, *a, **kw)

        self.ex.klines = rec
        return calls

    def _logs(self):
        return self._orders_log()

    def test_h1_target_exit_closes_brain_long_on_opposite_direction(self):
        self._open_long_position()
        pos = self.lc.rm.open_positions()[0]
        self.assertEqual(pos["direction"], 1)
        self._feed_falling_klines()
        calls = self._wrap_klines_recorder()
        out = self._brain_tick()          # strategy_tick (beyin + çıkış turu)
        # kline YALNIZ pozisyonlu sembol için çekildi (519 evren değil)
        self.assertEqual(calls, [SYM])
        # H1 giriş yolu inert: hiç giriş denemesi yok
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out))
        # BRAIN_H1_EXIT_TICK kaydı: pozisyonlu sembol + sonrası durum
        # (giriş tick'i de bir tur yazdı — SON tur bu testin turudur)
        ticks = [x for x in self._logs()
                 if x.get("kind") == "BRAIN_H1_EXIT_TICK"]
        self.assertEqual(len(ticks), 2)
        self.assertEqual(ticks[-1]["exit_symbols"], [SYM])
        # motor yönü -1 → LONG pozisyon hedef-yön çıkışına alındı
        self.assertEqual(self.lc.engine.direction(SYM), -1)
        rev = [x for x in self._logs()
               if x.get("kind") == "QUALITY_REVERSAL_EXIT"]
        self.assertEqual(len(rev), 1)
        self.assertEqual(rev[0]["position_direction"], 1)
        self.assertEqual(rev[0]["target_direction"], -1)
        # reduce-only EXIT emri DB'de — kalıcı iz
        rows = self.lc._db_execute(
            "SELECT params FROM live_orders WHERE kind='EXIT'").fetchall()
        self.assertEqual(len(rows), 1)
        params = json.loads(rows[0][0])
        self.assertTrue(params["reduceOnly"])
        self.assertEqual(params["side"], "SELL")   # LONG kapanışı
        lp = self.lc._live_pos_row(pos["pos_id"])
        self.assertIn(lp["exit_status"],
                      (X_PENDING, X_CONFIRMED, X_UNCONFIRMED))
        self.assertIn("QUALITY_TARGET_FLIP_EXIT", lp["exit_reason"])

    def test_risk_only_does_not_run_h1_exit_tick(self):
        """Diferansiyel kontrol: aynı girdi, RISK_AND_H1 DEĞİL → çıkış turu
        hiç çalışmaz, kline çekilmez, pozisyon açık kalır (çıkışlar yalnız
        risk katmanı: SL/SAFE_EXIT/HALT)."""
        set_brain_exit_policy(self.lc.db_path, BRAIN_EXIT_POLICY_RISK_ONLY,
                              "kontrol", "test:op")
        self._open_long_position()
        pos = self.lc.rm.open_positions()[0]
        self._feed_falling_klines()
        calls = self._wrap_klines_recorder()
        self._brain_tick()
        self.assertEqual(calls, [])           # kline çekilmedi
        self.assertFalse(any(x.get("kind") == "BRAIN_H1_EXIT_TICK"
                             for x in self._logs()))
        self.assertFalse(any(x.get("kind") == "QUALITY_REVERSAL_EXIT"
                             for x in self._logs()))
        rows = self.lc._db_execute(
            "SELECT COUNT(*) FROM live_orders WHERE kind='EXIT'"
        ).fetchone()
        self.assertEqual(rows[0], 0)          # çıkış emri yok
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    def test_no_positions_no_kline_fetch(self):
        """Açık pozisyon yoksa kline ÇEKİLMEZ (not kayda yazılır)."""
        make_brain_db(self.brain_path, [], decision="NO_TRADE")
        calls = self._wrap_klines_recorder()
        self._brain_tick()
        self.assertEqual(calls, [])
        ticks = [x for x in self._logs()
                 if x.get("kind") == "BRAIN_H1_EXIT_TICK"]
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0]["exit_symbols"], [])
        self.assertIn("kline cekilmedi", ticks[0].get("note", ""))

    def test_h1_entry_paths_still_inert_under_risk_and_h1(self):
        """Savunma derinliği (diferansiyel): RISK_AND_H1 AÇIKKEN bile H1
        GİRİŞ yolları beyin modunda emir ÜRETEMEZ. SHORT pozisyon + hedef
        +1'e FLİP: H1 modunda bu flip bir LONG GİRİŞ emri doğururdu; beyin
        modunda flip kanonik guard'la MISSED olarak tüketilir (dir ilerler),
        yalnız SHORT çıkış bacağı canlanır."""
        # SHORT pozisyon aç (beyin kaydıyla)
        set_direction_enabled(self.lc.db_path, -1, True, "t", "test")
        make_brain_db(self.brain_path, [brain_record(SYM, -1)])
        out = self._brain_tick()
        self.assertEqual([r for r in out
                          if r.get("kind") == "LIVE_ENTRY_ATTEMPT"][0]
                         ["result"], "FILLED")
        pos = self.lc.rm.open_positions()[0]
        self.assertEqual(pos["direction"], -1)
        make_brain_db(self.brain_path, [], decision="NO_TRADE")
        # düşen seri: hedef -1 (pozisyonla AYNI yön) → çıkış yok, ısınma
        kl = self._feed_falling_klines(n_bars=400)
        self._brain_tick()
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        self.assertEqual(self.lc.engine.direction(SYM), -1)
        # flip barı (+1'e dönüş) + bir bar daha (due flip kanonik tüketim
        # penceresi): H1 modunda LONG GİRİŞ doğardı — beyin modunda MISSED
        flip_ot = kl[-1][0] + BAR
        rows = [[ot, "100.0", "100.0", "100.0", "100.0", "0",
                 ot + BAR, "1e6"] for ot in (flip_ot, flip_ot + BAR)]
        self.ex.kline_data[SYM] = kl + rows
        self.clock.advance(2 * BAR)           # her iki bar KAPALI
        out = self._brain_tick()
        # H1 giriş yolu inert: giriş denemesi YOK
        self.assertFalse(any(r.get("kind") == "LIVE_ENTRY_ATTEMPT"
                             for r in out))
        # flip emre dönüşMEDİ — kanonik guard'la MISSED tüketildi (dir
        # ilerledi; target book kanonik kaldı)
        missed = [x for x in self._logs() if x.get("kind") == "MISSED_FLIP"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["direction"], 1)
        self.assertEqual(self.lc.engine.direction(SYM), 1)
        # yalnız ÇIKIŞ bacağı canlandı: SHORT → reduce-only BUY kapanışı
        rev = [x for x in self._logs()
               if x.get("kind") == "QUALITY_REVERSAL_EXIT"]
        self.assertEqual(len(rev), 1)
        self.assertEqual(rev[0]["position_direction"], -1)
        self.assertEqual(rev[0]["target_direction"], 1)
        rows_db = self.lc._db_execute(
            "SELECT params FROM live_orders WHERE kind='EXIT'").fetchall()
        self.assertEqual(len(rows_db), 1)
        params = json.loads(rows_db[0][0])
        self.assertTrue(params["reduceOnly"])
        self.assertEqual(params["side"], "BUY")   # SHORT kapanışı

    def test_exit_tick_holds_position_when_kline_fetch_fails(self):
        """Kline çekilemezse o sembol için hedef-yön çıkışı değerlendirmeZ —
        verisiz çıkış kararı VERİLMEZ (fail-closed); pozisyon DOKUNULMAZ."""
        self._open_long_position()

        def broken_klines(symbol, *a, **kw):
            raise RuntimeError("sahte borsa hatasi")

        self.ex.klines = broken_klines
        out = self._brain_tick()
        ticks = [x for x in self._logs()
                 if x.get("kind") == "BRAIN_H1_EXIT_TICK"]
        self.assertEqual(len(ticks), 2)          # giriş turu + bu tur
        self.assertEqual(ticks[-1]["kline_errors"], [SYM])
        self.assertFalse(any(x.get("kind") == "QUALITY_REVERSAL_EXIT"
                             for x in self._logs()))
        rows = self.lc._db_execute(
            "SELECT COUNT(*) FROM live_orders WHERE kind='EXIT'"
        ).fetchone()
        self.assertEqual(rows[0], 0)
        self.assertEqual(len(self.lc.rm.open_positions()), 1)


if __name__ == "__main__":
    unittest.main()
