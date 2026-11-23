"""BLOK27.3 testleri — Shadow Canary runner: H1 sinyalinden risk kapısına
uçtan uca (ağ YOK — sentetik klines; GERÇEK H1SignalEngine kullanılır).

Paper_runner semantiği birebir: İLK evaluate tüm geçmişi warmup replay
eder (execute YOK — shadow flat başlar); pozisyonlar yalnız SONRAKİ
yön değişimlerinde açılır (flip-only + backfill yok).

Kapsam:
  - warmup replay execute'siz (motor durumu inşası, shadow icra YOK)
  - canlı flip → SHADOW_OPEN: 50 USDT notional, bar-açılış fill modeli,
    komisyon; risk kapısından ATOMİK geçiş
  - mark yok → fail-closed RET (emir yok)
  - flip'in çıkış bacağı → SHADOW_CLOSE (risk azaltan) + yeni SHADOW_OPEN
  - restart sonrası dedup: son işlenen bar kalıcı → çift icra YOK (kural 7)
  - 8h STALE_BAR: eski açılıştan icra YOK, karar journal'lanır
  - paper hedef karşılaştırması (salt-okunur recon parse)
"""
import json
import os
import sqlite3
import tempfile
import unittest

import canary_shadow as cs

BAR_MS = cs.BAR_MS
SYM = "BTCUSDT"
T0 = 1735689600000        # 2025-01-01T00:00Z — sentetik zaman ekseni


def _rising(n, p0=100.0, r=1.01, qv=1e6):
    """Yükselen kapanışlar → momentum +1 (warmup sonrası dir=+1)."""
    bars, p = [], p0
    for i in range(n):
        bars.append((T0 + i * BAR_MS, round(p, 8), round(p * r, 8), qv))
        p *= r
    return bars


def _mk(canary_dir=None):
    d = canary_dir or tempfile.mkdtemp()
    return cs.ShadowCanary(d), d


def _fresh_mark(rm, px, now_ms):
    rm.update_mark(SYM, px, now_ms - 1000)   # MARK_STALE_S=30s içinde


def _scenario():
    """Ortak fixture: warmup (yükseliş → çöküş → dir=-1) + canlı flip
    barları (tepki → pend=+1, D açılışında flip +1)."""
    W = _rising(220)
    p_end = W[-1][2]
    A = (T0 + 220 * BAR_MS, p_end, 50.0, 1e6)    # çöküş barı → dir=-1
    B = (T0 + 221 * BAR_MS, 50.0, 49.0, 1e6)
    C = (T0 + 222 * BAR_MS, 49.0, 200.0, 1e6)    # tepki → pend=+1
    D = (T0 + 223 * BAR_MS, 200.0, 210.0, 1e6)   # flip BU açılışta (+1)
    return W, A, B, C, D


class TestWarmupAndLiveFlip(unittest.TestCase):
    def test_first_evaluate_is_pure_warmup(self):
        """İlk evaluate: tüm geçmiş replay — icra YOK, motor dir=-1 inşa."""
        sc, d = _mk()
        W, A, B, C, D = _scenario()
        now1 = T0 + 222 * BAR_MS + 1000          # A,B kapandı; C,D henüz yok
        decisions = sc.evaluate(now1, {SYM: W + [A, B]})
        self.assertEqual(decisions, [])
        self.assertEqual(len(sc.rm.open_positions()), 0)
        self.assertEqual(sc.engine.direction(SYM), -1)
        log = open(os.path.join(d, "shadow_orders.jsonl")).read()
        self.assertIn("WARMUP_DONE", log)
        self.assertEqual(sc.last_bar[SYM], T0 + 221 * BAR_MS)

    def test_live_flip_opens_shadow(self):
        """Canlı flip → uzun shadow pozisyon: 50 USDT notional, bar-açılış
        fill'i (alım YUKARI), komisyon, risk DB'de atomik kayıt."""
        sc, _ = _mk()
        W, A, B, C, D = _scenario()
        now1 = T0 + 222 * BAR_MS + 1000
        sc.evaluate(now1, {SYM: W + [A, B]})           # warmup
        now2 = T0 + 224 * BAR_MS + 1000                # C,D kapandı
        _fresh_mark(sc.rm, 200.0, now2)
        decisions = sc.evaluate(now2, {SYM: W + [A, B, C, D]})
        self.assertEqual(len(decisions), 1)
        rec = decisions[0]
        self.assertEqual(rec["kind"], "SHADOW_OPEN")
        self.assertEqual(rec["direction"], +1)
        self.assertEqual(rec["result"], "ACCEPTED")
        self.assertEqual(rec["notional"], 50.0)
        self.assertEqual(rec["bar_ts"], T0 + 223 * BAR_MS)
        # fill modeli: long alış YUKARI dolar (ref 200)
        self.assertGreater(rec["entry_px"], 200.0)
        self.assertLess(rec["entry_px"], 200.25)   # ~11bp fill (1+3+7.07)
        # komisyon 5bp: 50 * 0.0005 = 0.025
        self.assertAlmostEqual(rec["entry_comm"], 0.025, places=6)
        # risk DB: 1 açık uzun pozisyon, notional 50
        pos = sc.rm.open_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["direction"], +1)
        self.assertAlmostEqual(abs(pos[0]["units"]) * pos[0]["entry_px"],
                               50.0, places=6)
        self.assertEqual(sc.engine.direction(SYM), +1)

    def test_no_mark_fail_closed(self):
        """Mark yok/stale → shadow emir bile RED (fail-closed, kural 8)."""
        sc, _ = _mk()
        W, A, B, C, D = _scenario()
        now1 = T0 + 222 * BAR_MS + 1000
        sc.evaluate(now1, {SYM: W + [A, B]})
        now2 = T0 + 224 * BAR_MS + 1000
        decisions = sc.evaluate(now2, {SYM: W + [A, B, C, D]})  # mark YOK
        self.assertEqual(len(decisions), 1)
        self.assertIn("REJECTED", decisions[0]["result"])
        self.assertIn("FAIL_CLOSED", decisions[0]["result"])
        self.assertEqual(len(sc.rm.open_positions()), 0)


class TestFlipCloseLeg(unittest.TestCase):
    def test_flip_closes_then_opens(self):
        """Flip çıkış bacağı: mevcut pozisyon KAPATILIR (risk azaltan),
        ardından yeni yön risk kapısından geçer."""
        sc, _ = _mk()
        W, A, B, C, D = _scenario()
        now1 = T0 + 222 * BAR_MS + 1000
        sc.evaluate(now1, {SYM: W + [A, B]})
        now2 = T0 + 224 * BAR_MS + 1000
        _fresh_mark(sc.rm, 200.0, now2)
        sc.evaluate(now2, {SYM: W + [A, B, C, D]})
        pid0 = sc.rm.open_positions()[0]["pos_id"]     # uzun ~200
        # yeni flip: -1'e dön (uzun pozisyon 190'tan kapanır → zarar)
        now3 = now2 + 60_000
        _fresh_mark(sc.rm, 190.0, now3)
        out = sc._shadow_flip(SYM, -1, 190.0, 1e6, T0 + 224 * BAR_MS, now3)
        kinds = [r["kind"] for r in out]
        self.assertEqual(kinds, ["SHADOW_CLOSE", "SHADOW_OPEN"])
        # çıkış: uzun KAPANIŞ SATIŞI → fill ref'in ALTINDA → realized negatif
        self.assertLess(out[0]["exit_px"], 190.0)
        self.assertLess(out[0]["realized"], 0.0)
        # yeni pozisyon kısa, kabul
        self.assertEqual(out[1]["direction"], -1)
        self.assertEqual(out[1]["result"], "ACCEPTED")
        # DB: eski CLOSED, yeni OPEN -1
        opens = sc.rm.open_positions()
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["direction"], -1)
        self.assertNotEqual(opens[0]["pos_id"], pid0)
        old = sc.rm._pos(pid0)
        self.assertEqual(old["status"], "CLOSED")
        self.assertLess(old["realized"], 0.0)


class TestRestartDedup(unittest.TestCase):
    def test_restart_replays_without_double_execution(self):
        """Restart (kural 7): son işlenen bar kalıcıdır; aynı barlar yeniden
        evaluate edilirse ÇİFT icra olmaz."""
        sc, d = _mk()
        W, A, B, C, D = _scenario()
        now1 = T0 + 222 * BAR_MS + 1000
        sc.evaluate(now1, {SYM: W + [A, B]})
        now2 = T0 + 224 * BAR_MS + 1000
        _fresh_mark(sc.rm, 200.0, now2)
        sc.evaluate(now2, {SYM: W + [A, B, C, D]})
        self.assertEqual(len(sc.rm.open_positions()), 1)
        # "restart": yeni ShadowCanary, AYNI dizin (kalıcı DB)
        sc2 = cs.ShadowCanary(d)
        self.assertEqual(sc2.last_bar.get(SYM), T0 + 223 * BAR_MS)
        now3 = now2 + BAR_MS
        _fresh_mark(sc2.rm, 200.0, now3)
        decisions = sc2.evaluate(now3, {SYM: W + [A, B, C, D]})
        self.assertEqual(decisions, [])               # hepsi warmup replay
        self.assertEqual(len(sc2.rm.open_positions()), 1)  # çift icra YOK
        self.assertEqual(sc2.engine.direction(SYM), +1)


class TestStaleBar(unittest.TestCase):
    def test_stale_bar_executes_nothing(self):
        """8h'den eski bar açılışından icra YOK (paper_runner kuralı):
        karar journal'lanır, pozisyon değişmez, hedef ilerler."""
        sc, d = _mk()
        W, A, B, C, D = _scenario()
        now1 = T0 + 222 * BAR_MS + 1000
        sc.evaluate(now1, {SYM: W + [A, B]})           # warmup → dir=-1
        # C taze işlenir → pend=+1 (flip kararı C kapanışında)
        now2 = T0 + 223 * BAR_MS + 1000
        _fresh_mark(sc.rm, 200.0, now2)
        self.assertEqual(sc.evaluate(now2, {SYM: W + [A, B, C]}), [])
        # D 9 saat sonra işlenir → STALE: icra YOK
        now3 = T0 + 223 * BAR_MS + 9 * 3600 * 1000
        decisions = sc.evaluate(now3, {SYM: W + [A, B, C, D]})
        self.assertEqual(decisions, [])
        self.assertEqual(len(sc.rm.open_positions()), 0)   # hiç açılmadı
        log = open(os.path.join(d, "shadow_orders.jsonl")).read()
        self.assertIn("STALE_BAR", log)
        # hedef defteri ilerledi (WARMUP_SKIPPED semantiği)
        self.assertEqual(sc.engine.direction(SYM), +1)


class TestPaperTargets(unittest.TestCase):
    def test_paper_targets_parse(self):
        """Paper recon detayından hedef yönler ayrıştırılır (salt-okunur)."""
        d = tempfile.mkdtemp()
        f = os.path.join(d, "paper_ledger.db")
        db = sqlite3.connect(f)
        db.execute("CREATE TABLE recon (ts INTEGER NOT NULL, "
                   "items TEXT NOT NULL)")
        db.execute("INSERT INTO recon VALUES (?,?)", (1, json.dumps([
            {"symbol": "BTCUSDT", "status": "DRIFT",
             "detail": "target -1 vs actual +0 (explained: warmup)"},
            {"symbol": "ETHUSDT", "status": "OK",
             "detail": "target 0 vs actual 0"},
        ])))
        db.commit()
        db.close()
        sc, _ = _mk(os.path.join(d, "canary"))
        sc.paper_db = type(sc.paper_db)(f)   # test paper DB'sine yönlendir
        got = sc.paper_targets()
        self.assertEqual(got, {"BTCUSDT": -1, "ETHUSDT": 0})


if __name__ == "__main__":
    unittest.main()
