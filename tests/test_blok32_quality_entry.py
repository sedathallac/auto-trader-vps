"""BLOK32 §10 — ÇİFT YÖNLÜ LONG/SHORT QUALITY-BASED ENTRY TESTLERİ
(FakeExchange; ağ çağrısı YOK, gerçek emir YOK).

Operatör direktifi §10'un 30 zorunlu kategorisi (FakeExchange/mock):
  LONG    1-5  : no-flip kalite girişi / flip+kalite girişi / yetersiz kalite
                 / same-episode yeniden giriş engeli / koruma doğrulama
  SHORT   6-10  : LONG'un aynası
  Reversal 11-14: LONG→SHORT / SHORT→LONG / flat doğrulanmadan ters emir
                 YOK / restart episode dedup
  Quality 15-20 : bağımsız LONG/SHORT skorları / eşik sınırı / fail-closed
                 eksik bileşen / bayat veri / yanlış yön engeli / ardışık
                 barlarda gereksiz yeniden giriş YOK
  T0/risk 21-30 : T0 öncesi hedef bloğu / T0 sonrası ilk geçerli sinyal /
                 eski flip replay bloğu / pozisyon başına 100 cap / toplam
                 100 cap / -5 SAFE_EXIT / -10 HALT / ENTRY PAUSE /
                 çapraz-süreç giriş kilidi / risk döngüsü + koruma regresyonu

Fixture'ler (gerçek kalibre edilmiş eşiklerle — θ_long 66.5 / θ_short 66.0;
 empirical olarak doğrulanmış skorlar yorumlarda):
  clean trend : ±0.5%/bar 400 bar (mom_t > 1 → c_mom=1.0) — bar 0 hacim
                spike'ıyla skor ~76.7 (LONG/SHORT), spike yoksa ~72.9
  mild trend  : net ±0.0167%/bar + ±0.15% zigzag (m ≈ ±0.03, sigma ≈ 0.0015,
                mom_t ≈ 1.5) — reversal fixture'ı; flip ~bar 9, ters giriş
                ~bar 32 (hacim spike'ı)
  zig zayıf   : net ±0.0167%/bar + ±0.5% zigzag (mom_t ≈ 0.45) — skor ~48
                (açıkça yetersiz)
  flip-dizisi : clean düşüş warmup + canlı +4%/bar → flip bar 20'de skor
                ~26 (yeni yön momentumu henüz zayıf)

Bütün BLOK30/31/31.1 regresyonları ayrıca çalıştırılır (legacy dosyalar
FLIP_ONLY varsayılanıyla değişmedi).
"""
import fcntl
import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 T0, stop_trigger, loss_mark)
import canary_quality as cq
from canary_live import set_entry_pause, entry_gate_lock_path, CLIENT_PREFIX

BAR = 4 * 3600 * 1000
SYM = "ETHUSDT"
POLICY = cq.ENTRY_POLICY_QUALITY

# §3 sinyal nesnesi alanları (yalnız GERÇEK hesaplanan bileşenler)
QFIELDS = ("quality_score", "quality_threshold", "quality_pass",
           "momentum_strength", "trend_confirmation", "volatility_context",
           "volume_confirmation", "signal_reason")

LOG_UP = math.log(1.005)     # +0.5%/bar
LOG_DN = math.log(0.995)     # -0.5%/bar
LOG_MILD = 0.000167          # net drift/bar (m ≈ ±0.03 over 180)


class Blok32Base(unittest.TestCase):
    """Ortak harness: deterministik fixture üreticileri + gözlem yardımcıları.

    Tek warmup kuralı: bir LiveCanary yalnız bir kez ısıtılabilir (ikinci
    warmup, kalıcı last_bar nedeniyle no-op'tur). İkinci ortam gereken
    testler _fresh() ile bağımsız harness kurar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex, entry_policy=POLICY)
        prime(self.lc)
        self._px = {}
        self._bars = {}
        self._extra = []

    def tearDown(self):
        for t in self._extra:
            t.cleanup()
        self.tmp.cleanup()

    def _fresh(self):
        """Aynı test içinde ikinci bağımsız ortam (aynı saat, ayrı DB).
        __new__ ile kurulur — TestCase kurucusu yöntem adı doğrular."""
        tmp = tempfile.TemporaryDirectory(dir=self.tmp.name)
        self._extra.append(tmp)
        ex = FakeExchange(clock_ms=self.clock)
        lc = make_canary(tmp.name, ex, entry_policy=POLICY)
        prime(lc)
        h = Blok32Base.__new__(Blok32Base)
        h.lc, h.ex, h.clock = lc, ex, self.clock
        h.tmp, h._px, h._bars, h._extra = tmp, {}, {}, []
        return h

    # ---- fixture'ler -----------------------------------------------------

    def _warm(self, drifts, wiggle=0.0, vol=100.0, nbars=400):
        """Cold-start warmup replay (tek tik, execute'siz).
        drifts: {sem bol: bar başına net log-drift}; wiggle > 0 → alternating
        ±wiggle zigzag (net drift korunur — mom_t'yi düşürmek için)."""
        kl = {}
        wstart = T0 - 410 * BAR
        for sym, drift in drifts.items():
            px, series = 100.0, []
            for i in range(nbars):
                series.append((wstart + i * BAR, px, px, vol))
                r = (drift + wiggle) if i % 2 == 0 else (drift - wiggle)
                px = px * math.exp(r)
            kl[sym] = series
            self._px[sym] = px
            self._bars[sym] = series
        now1 = T0 - 10 * BAR
        self.clock.t = now1
        self.lc.strategy_tick(now_ms=now1, klines_by_sym=kl)

    def _tick(self, j, moves, ot=None):
        """Canlı bar j (ot = T0 + j·BAR; ot override edilebilir — T0 sınır
        testleri). moves: {sem bol: (log_ret, vol)} — bar kapanışı, mark ve
        risk döngüsü dahil tam strateji tiki."""
        kl = {}
        now = None
        for sym, (ret, vol) in moves.items():
            bar_ot = T0 + j * BAR if ot is None else ot
            o = self._px[sym]
            c = o * math.exp(ret)
            self._bars[sym].append((bar_ot, o, c, vol))
            kl[sym] = list(self._bars[sym])
            self._px[sym] = c
            now = bar_ot + BAR + 60_000
        self.clock.t = now
        for sym in moves:
            self.ex.set_mark(sym, self._px[sym])
            self.lc.rm.update_mark(sym, self._px[sym], now)
        self.lc.risk_tick()
        return self.lc.strategy_tick(now_ms=now, klines_by_sym=kl)

    def _warm_clean(self, sign, sym=SYM):
        """±0.5%/bar kesintisiz trend (skor ~76.7 hacim spike'ıyla)."""
        self._warm({sym: LOG_UP if sign > 0 else LOG_DN})

    def _warm_mild(self, sign, sym=SYM):
        """Reversal fixture'ı: net ±0.0167%/bar + ±0.15% zigzag."""
        self._warm({sym: LOG_MILD if sign > 0 else -LOG_MILD},
                   wiggle=0.0015)

    def _warm_zig(self, sign, sym=SYM):
        """Açıkça yetersiz kalite: mom_t ~0.45 → skor ~48 < 66."""
        self._warm({sym: LOG_MILD if sign > 0 else -LOG_MILD},
                   wiggle=0.005)

    def _inject_pend(self, symbol, direction, signal_bar_ts, price=100.0):
        """Beyaz-kutu flip enjeksiyonu (engine durumu — eski flip testi için)."""
        S = self.lc.engine._state(symbol)
        S["pend"] = direction
        S["pend_vol"] = 0.15
        S["pend_bar_ts"] = signal_bar_ts
        S["pend_price"] = price

    # ---- gözlem yardımcıları ----------------------------------------------

    def _cycles(self, path="strategy_cycles.jsonl"):
        p = os.path.join(self.tmp.name, path)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _last_cycle(self):
        cyc = self._cycles()
        assert cyc, "strategy_cycles.jsonl boş"
        return cyc[-1]

    def _symbol_state(self, cyc, symbol=SYM):
        for row in cyc["symbols"]:
            if row["symbol"] == symbol:
                return row
        raise AssertionError(f"{symbol} durumu döngü kaydında yok")

    def _orders_log(self):
        return self._cycles("live_orders.jsonl")

    def _kinds(self, *kinds, symbol=None):
        return [r for r in self._orders_log()
                if r.get("kind") in kinds
                and (symbol is None or r.get("symbol") == symbol)]

    def _entry_posts(self, symbol=None):
        return [c for c in self.ex.post_calls
                if c["method"] == "new_order" and not c["reduce_only"]
                and (symbol is None or c["symbol"] == symbol)]

    def _episode(self, symbol=SYM, direction=1):
        return self.lc._get_state(f"qep:{symbol}:{direction}")

    def _open_pos(self, symbol=SYM):
        for p in self.lc.rm.open_positions():
            if p["symbol"] == symbol:
                return p
        return None

    def _pos_rows(self):
        cols = [r[1] for r in
                self.lc.db.execute("PRAGMA table_info(positions)").fetchall()]
        return [dict(zip(cols, row)) for row in
                self.lc.db.execute("SELECT * FROM positions").fetchall()]

    def _entry_attempt(self, symbol=SYM):
        recs = self._kinds("LIVE_ENTRY_ATTEMPT", symbol=symbol)
        assert recs, "LIVE_ENTRY_ATTEMPT kaydı yok"
        return recs[-1]


# ============================================================================
# §10/1-5 — LONG
# ============================================================================

class TestLongEntries(Blok32Base):

    def test_01_no_flip_quality_entry(self):
        """(1) Flip OLMADAN LONG: hedef yön kalite eşiğini geçince giriş.
        Empirik: skor 76.67 ≥ 66.5, FILLED + PROTECTED, §3 alanları tam."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "QUALITY_VALID_LONG")
        self.assertEqual(st["final_decision"], "FILL_CONFIRMED")
        self.assertEqual(st["entry_result"], "FILLED")
        self.assertFalse(st.get("flip_detected"))   # flip YOK — kalite yolu
        for k in QFIELDS:
            self.assertIn(k, st, f"§3 alanı eksik: {k}")
        self.assertGreaterEqual(st["quality_score"], 66.5)
        self.assertEqual(st["quality_threshold"], 66.5)
        self.assertTrue(st["quality_pass"])
        self.assertEqual(st["signal_bar_timestamp"], T0)
        # karar ANINDA episode henüz ARMED (ENGAGED işareti emir ÇIKTIKTAN
        # sonra yazılır — kalıcı DB anahtarında doğrulanır)
        self.assertEqual(st["quality_episode"], "ARMED")
        self.assertEqual(self._episode(SYM, +1), "ENGAGED")
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertEqual(self._entry_posts()[0]["side"], "BUY")
        pos = self._open_pos()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["direction"], 1)
        self.assertEqual(self.lc._live_pos_row(pos["pos_id"])
                         ["protection_status"], "PROTECTED")
        self.assertLessEqual(pos["units"] * pos["entry_px"], 100.0)
        # QUALITY_SIGNAL kaydı tüm kalite alanlarıyla görünür
        sig = self._kinds("QUALITY_SIGNAL", symbol=SYM)
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["direction"], 1)
        self.assertTrue(sig[0]["execute"])

    def test_02_new_long_flip_with_quality_entry(self):
        """(2) Yeni LONG flip + kalite: flip barında kalite GEÇERSE giriş.
        (Yapısal not: gerçek eşiklerle flip-bar skoru ~26 < 66.5 — flip
        anında momentum henüz zayıf; bu kod yolunu eşik indirilerek
        doğrularız. Gerçek eşikle davranış = kategori 19.)"""
        with mock.patch.object(cq, "THRESHOLD_LONG", 20.0), \
                mock.patch.object(cq, "THRESHOLD_SHORT", 200.0):
            self._warm_clean(-1)               # düşüş → hedef SHORT
            for j in range(23):
                self._tick(j, {SYM: (0.04, 100.0)})   # +4%/bar
        att = self._entry_attempt()
        self.assertEqual(att["direction"], 1)
        self.assertEqual(att["bar_ts"], T0 + 20 * BAR)   # flip barı
        self.assertEqual(att["result"], "FILLED")
        cyc = self._cycles()
        # flip barındaki karar kaydı: QUALITY_VALID_LONG + flip_detected
        flip_cyc = [c for c in cyc
                    if any(r["symbol"] == SYM and r.get("flip_detected")
                           for r in c["symbols"])]
        self.assertTrue(flip_cyc, "flip_detected'lı döngü yok")
        st = self._symbol_state(flip_cyc[0], SYM)
        self.assertEqual(st["decision"], "QUALITY_VALID_LONG")
        self.assertEqual(self.lc.engine.direction(SYM), 1)
        self.assertEqual(self._episode(SYM, +1), "ENGAGED")
        self.assertEqual(len(self._entry_posts()), 1)

    def test_03_insufficient_long_no_entry(self):
        """(3) LONG kalitesi yetersiz → giriş YOK (skor ~48 < 66.5)."""
        self._warm_zig(+1)
        self._tick(0, {SYM: (0.003, 100.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "QUALITY_INSUFFICIENT")
        self.assertFalse(st["quality_pass"])
        self.assertLess(st["quality_score"], 66.5)
        self.assertEqual(st["target"], "TARGET_LONG")
        self.assertEqual(self._entry_posts(), [])
        self.assertEqual(self._kinds("QUALITY_SIGNAL", "LIVE_ENTRY_ATTEMPT"),
                         [])
        self.assertIsNone(self._episode(SYM, +1))    # episode ARMED (yazılmadı)

    def test_04_same_episode_reentry_prevention(self):
        """(4) Same-episode yeniden giriş engeli + skor düşüş-yükselişi
        (hysteresis): ENGAGED iken geçen skor yeniden tetiklemez; skor θ−h
        altına inince re-arm; pozisyon açıkken yeniden geçen skor YİNE giriş
        üretmez (reversal kapısı)."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})          # giriş
        for j in range(1, 4):                                  # skor ~76 → ENGAGED
            self._tick(j, {SYM: (LOG_UP, 250.0)})
            st = self._symbol_state(self._last_cycle())
            self.assertEqual(st["quality_episode"], "ENGAGED")
        for j in range(4, 8):                                  # hacim kuraklığı
            self._tick(j, {SYM: (0.0, 5.0)})           # skor ~58 < 62.5
        st4 = self._symbol_state(self._cycles()[-4])
        self.assertTrue(st4.get("quality_rearmed"))
        self.assertEqual(st4["quality_episode"], "ARMED")
        self.assertEqual(self._episode(SYM, +1), "ARMED")
        for j in range(8, 11):                                 # toparlanma
            self._tick(j, {SYM: (LOG_UP, 250.0)})
            st = self._symbol_state(self._last_cycle())
            self.assertEqual(st["decision"], "QUALITY_VALID_LONG")
            self.assertIn("pozisyon açık", st["decision_reason"])
        # toplam YALNIZ 1 giriş — drop-and-recover yeniden emir ÜRETMEZ
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertIsNotNone(self._open_pos())     # pozisyon hâlâ açık

    def test_05_long_protection_verified(self):
        """(5) LONG girişte STOP_MARKET koruması doğrulanır (PROTECTED)."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        pos = self._open_pos()
        self.assertEqual(self.lc._live_pos_row(pos["pos_id"])
                         ["protection_status"], "PROTECTED")
        algo = self.ex.algos[f"{CLIENT_PREFIX}P{pos['pos_id']}"]
        self.assertEqual(algo["side"], "SELL")           # LONG → SELL stop
        self.assertEqual(algo["closePosition"], "true")
        self.assertEqual(algo["workingType"], "MARK_PRICE")
        self.assertLess(float(algo["triggerPrice"]), pos["entry_px"])
        self.assertLess(stop_trigger(self.ex, pos["pos_id"]),
                        pos["entry_px"])


# ============================================================================
# §10/6-10 — SHORT
# ============================================================================

class TestShortEntries(Blok32Base):

    def test_06_no_flip_quality_entry(self):
        """(6) Flip OLMADAN SHORT: skor 76.67 ≥ 66.0, SELL, PROTECTED."""
        self._warm_clean(-1)
        self._tick(0, {SYM: (LOG_DN, 250.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "QUALITY_VALID_SHORT")
        self.assertEqual(st["final_decision"], "FILL_CONFIRMED")
        self.assertEqual(st["entry_result"], "FILLED")
        for k in QFIELDS:
            self.assertIn(k, st)
        self.assertGreaterEqual(st["quality_score"], 66.0)
        self.assertEqual(st["quality_threshold"], 66.0)
        self.assertEqual(st["signal_bar_timestamp"], T0)
        self.assertEqual(self._episode(SYM, -1), "ENGAGED")
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertEqual(self._entry_posts()[0]["side"], "SELL")
        pos = self._open_pos()
        self.assertEqual(pos["direction"], -1)
        self.assertEqual(self.lc._live_pos_row(pos["pos_id"])
                         ["protection_status"], "PROTECTED")

    def test_07_new_short_flip_with_quality_entry(self):
        """(7) Yeni SHORT flip + kalite girişi (kategori 2 aynası)."""
        with mock.patch.object(cq, "THRESHOLD_SHORT", 20.0), \
                mock.patch.object(cq, "THRESHOLD_LONG", 200.0):
            self._warm_clean(+1)               # yükseliş → hedef LONG
            for j in range(23):
                self._tick(j, {SYM: (-0.04, 100.0)})  # -4%/bar
        att = self._entry_attempt()
        self.assertEqual(att["direction"], -1)
        self.assertEqual(att["result"], "FILLED")
        cyc = [c for c in self._cycles()
               if any(r["symbol"] == SYM and r.get("flip_detected")
                      for r in c["symbols"])]
        self.assertTrue(cyc)
        st = self._symbol_state(cyc[0], SYM)
        self.assertEqual(st["decision"], "QUALITY_VALID_SHORT")
        self.assertEqual(self.lc.engine.direction(SYM), -1)
        self.assertEqual(self._episode(SYM, -1), "ENGAGED")
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertEqual(self._entry_posts()[0]["side"], "SELL")

    def test_08_insufficient_short_no_entry(self):
        """(8) SHORT kalitesi yetersiz → giriş YOK."""
        self._warm_zig(-1)
        self._tick(0, {SYM: (-0.003, 100.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "QUALITY_INSUFFICIENT")
        self.assertFalse(st["quality_pass"])
        self.assertEqual(st["target"], "TARGET_SHORT")
        self.assertEqual(self._entry_posts(), [])
        self.assertEqual(self._kinds("QUALITY_SIGNAL", "LIVE_ENTRY_ATTEMPT"),
                         [])

    def test_09_short_same_episode_reentry_prevention(self):
        """(9) SHORT same-episode + hysteresis (kategori 4 aynası)."""
        self._warm_clean(-1)
        self._tick(0, {SYM: (LOG_DN, 250.0)})
        for j in range(1, 4):
            self._tick(j, {SYM: (LOG_DN, 250.0)})
            self.assertEqual(
                self._symbol_state(self._last_cycle())["quality_episode"],
                "ENGAGED")
        for j in range(4, 8):
            self._tick(j, {SYM: (0.0, 5.0)})
        self.assertEqual(self._episode(SYM, -1), "ARMED")
        for j in range(8, 11):
            self._tick(j, {SYM: (LOG_DN, 250.0)})
            st = self._symbol_state(self._last_cycle())
            self.assertEqual(st["decision"], "QUALITY_VALID_SHORT")
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertIsNotNone(self._open_pos())

    def test_10_short_protection_verified(self):
        """(10) SHORT girişte STOP_MARKET koruması (BUY stop, trigger üstte)."""
        self._warm_clean(-1)
        self._tick(0, {SYM: (LOG_DN, 250.0)})
        pos = self._open_pos()
        self.assertEqual(self.lc._live_pos_row(pos["pos_id"])
                         ["protection_status"], "PROTECTED")
        algo = self.ex.algos[f"{CLIENT_PREFIX}P{pos['pos_id']}"]
        self.assertEqual(algo["side"], "BUY")            # SHORT → BUY stop
        self.assertEqual(algo["closePosition"], "true")
        self.assertGreater(float(algo["triggerPrice"]), pos["entry_px"])


# ============================================================================
# §10/11-14 — REVERSAL (One-Way protokolü §7)
# ============================================================================

class TestReversal(Blok32Base):

    def _run_short_to_long(self):
        """SHORT→LONG tam reversal (empirik doğrulanmış fixture): düşen
        mild warmup → SHORT girişi → yükselen barlar flip üretir."""
        self._warm_mild(-1)
        self._tick(0, {SYM: (0.003, 200.0)})      # SHORT girişi (hedef -1)
        for j in range(1, 35):
            self._tick(j, {SYM: (0.003,
                                 250.0 if j == 32 else 100.0)})

    def _run_long_to_short(self):
        """LONG→SHORT reversal (ayna): yükselen mild warmup → LONG girişi
        → düşen barlar flip üretir."""
        self._warm_mild(+1)
        self._tick(0, {SYM: (-0.003, 200.0)})     # LONG girişi (hedef +1)
        for j in range(1, 35):
            self._tick(j, {SYM: (-0.003,
                                 250.0 if j == 32 else 100.0)})

    def _assert_reversal(self, first_dir, flip_bar, second_entry_bar):
        rows = self._pos_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["direction"], first_dir)
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(rows[1]["direction"], -first_dir)
        self.assertEqual(rows[1]["status"], "OPEN")
        # flip tüketildi (kalite yetersiz — yeni yön momentumu henüz zayıf)
        ins = self._kinds("QUALITY_INSUFFICIENT_FLIP", symbol=SYM)
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0]["bar_ts"], T0 + flip_bar * BAR)
        self.assertEqual(ins[0]["direction"], -first_dir)
        self.assertFalse(ins[0]["execute"])
        # çıkış QUALITY_TARGET_FLIP_EXIT ile reduce-only başlatıldı
        # (kalıcı kanıt: pozisyon satırının exit_reason alanı)
        rev = self._kinds("QUALITY_REVERSAL_EXIT", symbol=SYM)
        self.assertEqual(len(rev), 1)
        self.assertEqual(rev[0]["pos_id"], rows[0]["pos_id"])
        self.assertIn("reduce-only çıkış", rev[0]["reason"])
        self.assertEqual(
            self.lc._live_pos_row(rows[0]["pos_id"])["exit_reason"],
            "QUALITY_TARGET_FLIP_EXIT")
        self.assertIn("EXIT_CONFIRMED", json.dumps(rev[0]["result"]))
        # ikinci giriş flip'ten SONRA, flat doğrulamasından sonra
        atts = self._kinds("LIVE_ENTRY_ATTEMPT", symbol=SYM)
        self.assertEqual(len(atts), 2)
        self.assertEqual(atts[1]["direction"], -first_dir)
        self.assertGreater(atts[1]["bar_ts"], T0 + flip_bar * BAR)
        self.assertEqual(atts[1]["result"], "FILLED")
        self.assertEqual(atts[1]["bar_ts"], T0 + second_entry_bar * BAR)
        # emir sırası: reduce-only çıkış POST'u ikinci girişten ÖNCE
        posts = self.ex.post_calls
        exit_idx = next(i for i, c in enumerate(posts)
                        if c["method"] == "new_order" and c["reduce_only"])
        entry2_idx = next(i for i, c in enumerate(posts)
                          if c["method"] == "new_order" and not c["reduce_only"]
                          and i > exit_idx)
        self.assertLess(exit_idx, entry2_idx)
        # episode durumları: ilk yön re-arm olmuş, ikinci yön ENGAGED
        self.assertEqual(self._episode(SYM, first_dir), "ARMED")
        self.assertEqual(self._episode(SYM, -first_dir), "ENGAGED")

    def test_11_long_to_short_reversal(self):
        """(11) LONG→SHORT: flip'te koşulsuz reduce-only çıkış → flat
        doğrulama → yeni yönde kalite geçince giriş."""
        self._run_long_to_short()
        self._assert_reversal(+1, flip_bar=9, second_entry_bar=32)

    def test_12_short_to_long_reversal(self):
        """(12) SHORT→LONG reversal (ayna)."""
        self._run_short_to_long()
        self._assert_reversal(-1, flip_bar=10, second_entry_bar=32)

    def test_13_no_opposite_order_before_flat_verified(self):
        """(13) Flat DOĞRULANMADAN ters emir YOK: flip barında kalite geçse
        bile (eşik indirilmiş) açık pozisyon varken giriş zinciri durur;
        çıkış KISMI dolunca (flat belirsiz) bile durur; çıkış tamamlanıp
        flat doğrulanınca giriş gelir."""
        # flip-bar LONG kalitesi ~30+ — eşik 28'e indirilmiş → flip anında
        # geçer; ama SHORT pozisyonu hâlâ açık
        with mock.patch.object(cq, "THRESHOLD_LONG", 28.0):
            self._warm_mild(-1)
            self._tick(0, {SYM: (0.003, 200.0)})    # SHORT girişi (hedef -1)
            # çıkış emri (pos 1, attempt 1) KISMI dolsun → flat belirsiz
            self.ex.partial_fill_qty[f"{CLIENT_PREFIX}X1n1"] = 0.001
            for j in range(1, 10):
                self._tick(j, {SYM: (0.003, 100.0)})   # yükseliş → flip bar 10
            # flip barı 10: kalite GEÇTİ ama pozisyon açık → emir YOK
            self._tick(10, {SYM: (0.003, 100.0)})
            st = self._symbol_state(self._last_cycle())
            self.assertEqual(st["final_decision"], "REVERSAL_EXIT_FIRST")
            self.assertEqual(len(self._entry_posts()), 1)   # yalnız SHORT
            lp = self.lc._live_pos_row(1)
            self.assertEqual(lp["exit_status"], "EXIT_PARTIAL")
            rev = self._kinds("QUALITY_REVERSAL_EXIT", symbol=SYM)
            self.assertEqual(len(rev), 1)
            # risk döngüsü çıkışı tamamlar (kalan miktar yeni intent)
            self.lc.risk_tick()
            lp = self.lc._live_pos_row(1)
            if lp["exit_status"] != "EXIT_CONFIRMED":
                self.lc.risk_tick()
            rows = self._pos_rows()
            self.assertEqual(rows[0]["status"], "CLOSED")   # flat doğrulandı
            # şimdi ters yön kalitesi geçer → giriş (flat SONRASI)
            for j in range(11, 16):
                self._tick(j, {SYM: (0.003,
                                             250.0 if j >= 12 else 100.0)})
                if len(self._entry_posts()) == 2:
                    break
        self.assertEqual(len(self._entry_posts()), 2)
        atts = self._kinds("LIVE_ENTRY_ATTEMPT", symbol=SYM)
        self.assertEqual(atts[1]["direction"], 1)
        self.assertEqual(atts[1]["result"], "FILLED")
        self.assertGreater(atts[1]["bar_ts"], T0 + 10 * BAR)

    def test_14_restart_episode_dedup(self):
        """(14) Restart sonrası episode/sig state DB'den okunur: process
        belleğinde kaybolan state canlı girişin TEKRARINA yol açmaz."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        self.assertEqual(len(self._entry_posts()), 1)
        # restart: aynı DB + aynı borsa durumu, YENİ LiveCanary
        lc2 = make_canary(self.tmp.name, self.ex, entry_policy=POLICY)
        prime(lc2)
        # warmup replay + yeni bar: kalite YİNE geçiyor ama episode ENGAGED
        ot = T0 + BAR
        o = self._px[SYM]
        c = o * math.exp(LOG_UP)
        self._bars[SYM].append((ot, o, c, 250.0))
        now = ot + BAR + 60_000
        self.clock.t = now
        self.ex.set_mark(SYM, c)
        lc2.rm.update_mark(SYM, c, now)
        lc2.risk_tick()
        lc2.strategy_tick(now_ms=now,
                          klines_by_sym={SYM: list(self._bars[SYM])})
        self.assertEqual(len(self._entry_posts()), 1)     # ikinci emir YOK
        self.assertEqual(lc2._get_state(f"qep:{SYM}:1"), "ENGAGED")
        with open(os.path.join(self.tmp.name, "strategy_cycles.jsonl")) as f:
            cyc = [json.loads(l) for l in f if l.strip()][-1]
        st = [r for r in cyc["symbols"] if r["symbol"] == SYM][0]
        self.assertEqual(st["decision"], "QUALITY_INSUFFICIENT")
        self.assertIn("ENGAGED", st["decision_reason"])
        pos = [p for p in lc2.rm.open_positions() if p["symbol"] == SYM]
        self.assertEqual(len(pos), 1)                     # pozisyon devrede
        # aynı bar tekrar işlenmez (kalıcı last_bar) — eski sinyal replay YOK
        lc2.strategy_tick(now_ms=now + 60_000,
                          klines_by_sym={SYM: list(self._bars[SYM])})
        self.assertEqual(len(self._entry_posts()), 1)


# ============================================================================
# §10/15-20 — KALİTE MODELİ
# ============================================================================

class TestQualityModel(Blok32Base):

    def test_15_independent_long_short_scores(self):
        """(15) LONG/SHORT kalitesi BAĞIMSIZ ölçülür: ayrı eşikler, ayrı
        skorlar; §3 alanları tam; ters yön yapısal olarak geçemez."""
        self._warm_clean(+1)
        q_l = self.lc.quality.compute(SYM, +1)
        q_s = self.lc.quality.compute(SYM, -1)
        self.assertIsNotNone(q_l)
        self.assertIsNotNone(q_s)
        for q, d in ((q_l, +1), (q_s, -1)):
            self.assertEqual(q["symbol"], SYM)
            self.assertEqual(q["direction"], d)
            for k in QFIELDS:
                self.assertIn(k, q)
        self.assertEqual(q_l["quality_threshold"], 66.5)
        self.assertEqual(q_s["quality_threshold"], 66.0)
        self.assertTrue(q_l["quality_pass"])
        self.assertFalse(q_s["quality_pass"])
        # yapısal tavan: momentum yön uyumsuzsa skor ≤ 100*(0.15+0.20) = 35
        self.assertLess(q_s["quality_score"], 36.0)
        self.assertGreater(q_l["quality_score"], q_s["quality_score"])
        # ayna: düşüş piyasasında SHORT geçer, LONG geçemez (tek-warmup
        # kuralı: ikinci ortam _fresh ile kurulur — aynı lc'de ikinci
        # warmup no-op'tur)
        sub = self._fresh()
        sub._warm_clean(-1)
        q_s2 = sub.lc.quality.compute(SYM, -1)
        q_l2 = sub.lc.quality.compute(SYM, +1)
        self.assertTrue(q_s2["quality_pass"])
        self.assertFalse(q_l2["quality_pass"])
        self.assertLess(q_l2["quality_score"], 36.0)

    def test_16_threshold_boundary(self):
        """(16) Eşik sınırı: skor 76.67 — θ=76.9 KALIR, θ=76.6 GEÇER."""
        with mock.patch.object(cq, "THRESHOLD_LONG", 76.9):
            self._warm_clean(+1)
            self._tick(0, {SYM: (LOG_UP, 250.0)})
            st = self._symbol_state(self._last_cycle())
            self.assertEqual(st["decision"], "QUALITY_INSUFFICIENT")
            self.assertEqual(self._entry_posts(), [])
        # ikinci bağımsız ortam: aynı fixture → aynı skor, eşik altta
        base = self._fresh()
        base._warm_clean(+1)
        with mock.patch.object(cq, "THRESHOLD_LONG", 76.6):
            base._tick(0, {SYM: (LOG_UP, 250.0)})
        st2 = base._symbol_state(base._last_cycle())
        self.assertEqual(st2["final_decision"], "FILL_CONFIRMED")
        self.assertEqual(len([c for c in base.ex.post_calls
                              if c["method"] == "new_order"
                              and not c["reduce_only"]]), 1)

    def test_17_failclosed_missing_feature(self):
        """(17) Fail-closed: momentum tam olarak 0 (nötr) → bileşen
        hesaplanamaz → skor None → giriş YOK + neden loglanır."""
        # V-şekilli warmup: son 181 barın çarpımı tam 1.0 → m = 0 (exact)
        base_px = 100.0
        series = []
        wstart = T0 - 410 * BAR
        for i in range(400):
            if i < 220:
                c = base_px                       # düz referans bölgesi
            else:
                j = i - 220
                c = base_px * (1.0 - 0.001 * min(j, 180 - j))
            series.append((wstart + i * BAR, c, c, 100.0))
        # canlı bar 0: V'nin tam kapanış noktası — close == closes[220]
        live_close = series[220][2]
        ot = T0
        series.append((ot, base_px, live_close, 250.0))
        self.clock.t = T0 - 10 * BAR
        self.lc.strategy_tick(now_ms=T0 - 10 * BAR,
                              klines_by_sym={SYM: series[:400]})
        now = ot + BAR + 60_000
        self.clock.t = now
        self.ex.set_mark(SYM, live_close)
        self.lc.rm.update_mark(SYM, live_close, now)
        self.lc.risk_tick()
        self.lc.strategy_tick(now_ms=now, klines_by_sym={SYM: series})
        self.assertIsNone(self.lc.quality.compute(SYM, +1))
        self.assertIsNone(self.lc.quality.compute(SYM, -1))
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "QUALITY_INSUFFICIENT")
        self.assertIn("kalite hesaplanamadı", st["decision_reason"])
        self.assertEqual(self._entry_posts(), [])

    def test_18_stale_data_blocks(self):
        """(18) Bayat veri: kalite GEÇER ama bookTicker 60s bayat →
        PRICE_UNAVAILABLE, emir YOK (§8 30s tazelik)."""
        self._warm_clean(+1)
        now = T0 + BAR + 60_000
        self.ex.book_ticker_time[SYM] = now - 60_000    # 60s bayat
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "PRICE_UNAVAILABLE")
        self.assertTrue(st["quality_pass"])   # kalite geçti — kapı VERİ tazeliği
        self.assertEqual(self._entry_posts(), [])
        self.assertEqual(self._kinds("QUALITY_SIGNAL", symbol=SYM), [])
        self.assertEqual(self._episode(SYM, +1), None)  # emir çıkmadı

    def test_19_wrong_direction_score_mismatch_blocked(self):
        """(19) Yanlış yön / skor uyumsuzluğu: (i) ters yön skoru yapısal
        tavanın altında; (ii) flip yeni yön istese de momentum henüz
        zayıfsa (skor ~26 < 66.5) emir ÇIKMAZ."""
        # (i) yapısal
        self._warm_clean(+1)
        q_s = self.lc.quality.compute(SYM, -1)
        self.assertFalse(q_s["quality_pass"])
        self.assertLess(q_s["quality_score"], 36.0)
        # (ii) davranışsal: SHORT hedefinden LONG flip'i — gerçek eşikle
        # (ikinci ortam _fresh: yükselen warmup zaten yapıldı)
        sub = self._fresh()
        with mock.patch.object(cq, "THRESHOLD_SHORT", 200.0):
            sub._warm_clean(-1)
            for j in range(23):
                sub._tick(j, {SYM: (0.04, 100.0)})
        self.assertEqual(sub._entry_posts(), [])        # HİÇ emir yok
        ins = sub._kinds("QUALITY_INSUFFICIENT_FLIP", symbol=SYM)
        self.assertEqual(len(ins), 1)
        self.assertLess(ins[0]["quality_score"], 66.5)  # 26.04
        self.assertEqual(sub.lc.engine.direction(SYM), 1)  # hedef ilerledi

    def test_20_consecutive_bars_no_unnecessary_reentry(self):
        """(20) Ardışık barlarda gereksiz yeniden giriş YOK: kalite her
        barda geçse bile tek giriş; her barın karar kaydı görünür."""
        self._warm_clean(+1)
        for j in range(10):
            self._tick(j, {SYM: (LOG_UP, 250.0)})
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertEqual(len(self._kinds("LIVE_ENTRY_ATTEMPT",
                                         symbol=SYM)), 1)
        self.assertEqual(len(self._kinds("DUPLICATE_SIGNAL")), 0)
        for c in self._cycles():
            st = self._symbol_state(c, SYM)
            if st.get("quality_score") is None:
                continue     # warmup replay döngüsü — §3 alanları yok
            for k in QFIELDS:
                self.assertIn(k, st, f"bar gözlemlenebilirliği: {k} eksik")
            if st.get("new_bar_count"):
                self.assertIn(st["decision"],
                              ("QUALITY_VALID_LONG", "QUALITY_INSUFFICIENT"))
        # bar 1+: ENGAGED (hysteresis) — skor geçse de yeniden tetiklemez
        last = self._symbol_state(self._last_cycle())
        self.assertEqual(last["quality_episode"], "ENGAGED")
        self.assertIn("ENGAGED", last["decision_reason"])


# ============================================================================
# §10/21-30 — T0 ve RİSK
# ============================================================================

class TestT0AndRisk(Blok32Base):

    def test_21_pre_t0_target_blocked(self):
        """(21) T0 ÖNCESİ hedef/sinyal emir ÜRETEMEZ (karar anı ≤ T0)."""
        self._warm_clean(+1)
        # bar T0-5·BAR'da açılıp T0-4·BAR'da kapanıyor → karar ≤ T0
        self._tick(-5, {SYM: (LOG_UP, 250.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["final_decision"], "ENTRY_BLOCKED")
        att = self._entry_attempt()
        self.assertEqual(att["result"], "BLOCKED")
        blockers = " ".join(att.get("blockers") or [])
        self.assertIn("T0 öncesi", blockers)
        self.assertEqual(self._entry_posts(), [])
        # sinyal kalıcı olarak tükendi (sig anahtarı yazıldı — replay YOK)
        self.assertEqual(self.lc._get_state(f"sig:{SYM}:{T0 - 5 * BAR}"), "1")

    def test_22_post_t0_first_valid_signal(self):
        """(22) T0'dan önce açılıp sonra KAPANAN bar → ilk GEÇERLİ sinyal
        (§8: karar anı bar kapanışı; T0 sonrası ilk kapanış)."""
        self._warm_clean(+1)
        ot = T0 - BAR + 3_600_000      # T0'dan 3h önce açılır, T0+1h kapanır
        self._tick(0, {SYM: (LOG_UP, 250.0)}, ot=ot)
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["final_decision"], "FILL_CONFIRMED")
        self.assertEqual(st["entry_result"], "FILLED")
        self.assertEqual(len(self._entry_posts()), 1)

    def test_23_old_flip_replay_blocked(self):
        """(23) Eski flip replay edilMEZ: penceresi geçmiş flip MISSED
        tüketilir (fail-closed) — QUALITY politika BLOK31 semantiğini korur."""
        self._warm_clean(+1)
        # 3 bar önce karar verilmiş, penceresi KESİNLİKLE geçmiş flip
        self._inject_pend(SYM, -1, T0 - 3 * BAR, price=self._px[SYM])
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        missed = self._kinds("MISSED_FLIP", symbol=SYM)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["direction"], -1)
        self.assertFalse(missed[0]["execute"])
        # tüketim dir'i −1'e ilerletti; canlı bar (yükselen) end_bar ile
        # GERÇEK +1 pend kurdu → aynı tick'te kalite geçti → meşru LONG
        # girişi (BLOK31.1 kanonik davranış; eski −1 sinyali REPLAY yok)
        self.assertEqual(self.lc.engine.pending(SYM), 0)   # eski pend tükendi
        # eski flip'in yönünde (SELL) HİÇ emir çıkmadı
        self.assertEqual([p for p in self._entry_posts()
                          if p["side"] == "SELL"], [])
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["decision"], "QUALITY_VALID_LONG")
        att = self._entry_attempt()
        self.assertGreaterEqual(att["bar_ts"], T0)   # yeni bar sinyali

    def test_24_per_position_notional_cap(self):
        """(24) Pozisyon başına 100 USDT SERT cap; boyutlandırma İCRA ANI
        fiyatından (bar açılışı DEĞİL — §8)."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        att = self._entry_attempt()
        self.assertLessEqual(att["notional"], 100.0)
        self.assertGreaterEqual(att["notional"], 20.0)   # ETH minNotional
        pos = self._open_pos()
        self.assertLessEqual(pos["units"] * pos["entry_px"], 100.0)
        # icra fiyatı = güncel bookTicker ask (bar kapanışına yakın, açılış
        # DEĞİL): bar açılışı kapanıştan %0.5 farklı
        bar = self._bars[SYM][-1]
        bar_open, bar_close = bar[1], bar[2]
        self.assertGreater(abs(bar_close / bar_open - 1), 0.004)
        self.assertLess(abs(att["ref_px"] / bar_close - 1), 0.001)
        self.assertNotAlmostEqual(att["ref_px"], bar_open, places=2)

    def test_25_total_notional_cap(self):
        """(25) Toplam 100 USDT cap: ETH pozisyonu açıkken (~98) ikinci
        sembol girişi rm.check_order'dan RED (toplam notional)."""
        self._warm({SYM: LOG_UP, "BTCUSDT": LOG_UP})
        # bar 0: ETH kalite geçer (hacim spike), BTC düşük hacimle kalır
        self._tick(0, {SYM: (LOG_UP, 250.0),
                               "BTCUSDT": (LOG_UP, 5.0)})
        self.assertEqual(len(self._entry_posts(SYM)), 1)
        self.assertEqual(self._entry_posts("BTCUSDT"), [])
        # bar 1: BTC kalitesi geçer → toplam notional kapısından RED
        self._tick(1, {SYM: (LOG_UP, 250.0),
                               "BTCUSDT": (LOG_UP, 250.0)})
        atts = self._kinds("LIVE_ENTRY_ATTEMPT", symbol="BTCUSDT")
        self.assertEqual(len(atts), 1)
        self.assertTrue(atts[0]["result"].startswith("REJECTED"))
        self.assertIn("toplam notional", atts[0]["result"])
        self.assertEqual(self._entry_posts("BTCUSDT"), [])
        cyc = self._last_cycle()
        st_btc = self._symbol_state(cyc, "BTCUSDT")
        self.assertEqual(st_btc["final_decision"], "ORDER_REJECTED")
        # yalnız ETH pozisyonu açık; toplam notional ≤ 100
        opens = self.lc.rm.open_positions()
        self.assertEqual(len(opens), 1)
        self.assertLessEqual(sum(p["units"] * p["entry_px"]
                                 for p in opens), 100.0)

    def test_26_safe_exit_minus_5(self):
        """(26) -5 USDT SAFE_EXIT: pozisyon -5'i geçince reduce-only çıkış,
        flat doğrulama, koruma temizliği."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        pos = self._open_pos()
        self.assertIsNotNone(pos)
        # saf −5 eşik testi: stop koruması dışarıdan iptal edilir (legacy
        # deseni — stop tetik fiyatı ~−4.9'da, mark'ın atlaması karışmasın)
        self.ex.algos[f"{CLIENT_PREFIX}P{pos['pos_id']}"]["status"] = "CANCELED"
        mark = loss_mark(self.lc, pos["pos_id"], 5.2)
        now = T0 + 2 * BAR + 60_000
        self.clock.t = now
        self.ex.set_mark(SYM, mark)
        self.lc.rm.update_mark(SYM, mark, now)
        self.lc.risk_tick()
        rows = self._pos_rows()
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertLess(rows[0]["realized"], -4.0)
        self.assertGreater(rows[0]["realized"], -7.0)
        # reduce-only çıkış POST'u gitti; koruma emri temizlendi
        exits = [c for c in self.ex.post_calls
                 if c["method"] == "new_order" and c["reduce_only"]]
        self.assertEqual(len(exits), 1)
        lp = self.lc._live_pos_row(pos["pos_id"])
        self.assertEqual(lp["exit_status"], "EXIT_CONFIRMED")
        self.assertEqual(len(self._entry_posts()), 1)   # yeni giriş YOK

    def test_27_global_halt_minus_10(self):
        """(27) -10 USDT global HALT: pozisyon kapanır, halt zinciri
        terminalduruma (HALT_LOCKED) ilerler, yeni kalite sinyali
        GLOBAL_HALT ile bloklanır."""
        # ETH bar 0'da girer; BTC düşük hacimle kalır (episode ARMED) —
        # halt sonrası GLOBAL_HALT kapısını BTC üzerinden sınarız (ETH'nin
        # kendi episode'u bar-0 girişinden ENGAGED — hysteresis ayrı mekanizma)
        self._warm({SYM: LOG_UP, "BTCUSDT": LOG_UP})
        self._tick(0, {SYM: (LOG_UP, 250.0),
                       "BTCUSDT": (LOG_UP, 5.0)})
        pos = self._open_pos()
        # saf −10 eşik testi: stop koruması dışarıdan iptal (legacy deseni)
        self.ex.algos[f"{CLIENT_PREFIX}P{pos['pos_id']}"]["status"] = "CANCELED"
        mark = loss_mark(self.lc, pos["pos_id"], 10.5)
        now = T0 + 2 * BAR + 60_000
        self.clock.t = now
        self.ex.set_mark(SYM, mark)
        for _ in range(6):
            self.lc.rm.update_mark(SYM, mark, now)   # 30s tazelik korunur
            self.lc.risk_tick()
            if self.lc.halt_state == "CANARY_HALT_LOCKED":
                break
            now += 5_000
            self.clock.t = now
            self.ex.set_mark(SYM, mark)
        # halt zinciri terminal duruma ilerledi: HALT_LOCKED (yalnız operatör
        # sıfırlar; HALT_ACTIVE = normal ÇALIŞMA durumu, halt DEĞİL)
        self.assertEqual(self.lc.halt_state, "CANARY_HALT_LOCKED")
        rows = self._pos_rows()
        self.assertEqual(rows[0]["status"], "CLOSED")
        # kalite YENİ barda BTC'de geçer (episode ARMED) ama HALT kapısı →
        # GLOBAL_HALT (BLOK32 karar etiketi — generic ENTRY_BLOCKED DEĞİL)
        self._tick(1, {SYM: (LOG_UP, 250.0),
                       "BTCUSDT": (LOG_UP, 250.0)})
        st_btc = self._symbol_state(self._last_cycle(), "BTCUSDT")
        self.assertEqual(st_btc["final_decision"], "GLOBAL_HALT")
        self.assertEqual(len(self._entry_posts()), 1)

    def test_28_entry_pause(self):
        """(28) ENTRY PAUSE: kalite sinyali ENTRY_PAUSED ile bloklanır;
        resume sonrası aynı koşullarda giriş gelir (PAUSE tek engeldi)."""
        db = os.path.join(self.tmp.name, "canary_live.db")
        self._warm_clean(+1)
        set_entry_pause(db, True, "BLOK32 test 28", "tester")
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["final_decision"], "ENTRY_PAUSED")
        att = self._entry_attempt()
        self.assertEqual(att["result"], "BLOCKED")
        self.assertIn("ENTRY_PAUSED", " ".join(att.get("blockers") or []))
        self.assertEqual(self._entry_posts(), [])
        # bar 0 tükendi (sig anahtarı) → resume sonrası SONRAKİ bar girer
        set_entry_pause(db, False, "BLOK32 test 28 resume", "tester")
        self._tick(1, {SYM: (LOG_UP, 250.0)})
        st = self._symbol_state(self._last_cycle())
        self.assertEqual(st["final_decision"], "FILL_CONFIRMED")
        self.assertEqual(len(self._entry_posts()), 1)

    def test_29_cross_process_entry_gate(self):
        """(29) Çapraz-süreç giriş kilidi: entry-gate flock başkasındayken
        emir GÖNDERİLMEZ (fail-closed); kilit boşalınca zincir tamamlanır."""
        self._warm_clean(+1)
        lock_path = entry_gate_lock_path(
            os.path.join(self.tmp.name, "canary_live.db"))
        gate = open(lock_path, "w")
        try:
            fcntl.flock(gate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            ot = T0
            o = self._px[SYM]
            c = o * math.exp(LOG_UP)
            bar = (ot, o, c, 250.0)
            kl = {SYM: self._bars[SYM] + [bar]}
            now = ot + BAR + 60_000
            self.clock.t = now
            self.ex.set_mark(SYM, c)
            self.lc.rm.update_mark(SYM, c, now)
            self.lc.risk_tick()
            t = threading.Thread(
                target=lambda: self.lc.strategy_tick(now_ms=now,
                                                     klines_by_sym=kl))
            t.start()
            time.sleep(0.8)
            # kapı başkasında → emir ÇIKMADI (bekliyor, göndermedi)
            self.assertEqual(self._entry_posts(), [])
            fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
            t.join(timeout=40.0)
            self.assertFalse(t.is_alive(), "entry-gate beklemesi 40s'yi aştı")
        finally:
            gate.close()
        self.assertEqual(len(self._entry_posts()), 1)
        self.assertEqual(self._entry_attempt()["result"], "FILLED")
        self.assertIsNotNone(self._open_pos())

    def test_30_risk_loop_protection_regression(self):
        """(30) Risk döngüsü + koruma regresyonu: giriş sonrası stabil
        marklarda pozisyon AÇIK kalır, koruma DOĞRULANIR, gereksiz çıkış/
        iptal YOK; borsa tarafı stop tetiklenirse düzgün kapanır."""
        self._warm_clean(+1)
        self._tick(0, {SYM: (LOG_UP, 250.0)})
        pos = self._open_pos()
        for i in range(3):
            now = T0 + 2 * BAR + 60_000 + i * 5_000
            self.clock.t = now
            self.ex.set_mark(SYM, self._px[SYM])
            self.lc.rm.update_mark(SYM, self._px[SYM], now)
            self.lc.risk_tick()
            pos = self._open_pos()
            self.assertIsNotNone(pos, f"stabil marklarda pozisyon kapandı "
                                      f"(döngü {i})")
            self.assertEqual(self.lc._live_pos_row(pos["pos_id"])
                             ["protection_status"], "PROTECTED")
        # gereksiz çıkış/iptal POST'u YOK
        self.assertEqual([c for c in self.ex.post_calls
                          if c["method"] == "new_order"
                          and c["reduce_only"]], [])
        self.assertEqual([c for c in self.ex.post_calls
                          if c["method"].startswith("cancel")], [])
        # borsa tarafı koruma sürekliliği: mark stop altına inceye kadar
        self.ex.auto_trigger = True
        trig = stop_trigger(self.ex, pos["pos_id"])
        now = T0 + 3 * BAR + 60_000
        self.clock.t = now
        self.ex.set_mark(SYM, trig - 0.5)     # stop tetiklenir (borsa tarafı)
        self.lc.rm.update_mark(SYM, trig - 0.5, now)
        self.lc.risk_tick()
        rows = self._pos_rows()
        self.assertEqual(rows[0]["status"], "CLOSED")
        algo = self.ex.algos[f"{CLIENT_PREFIX}P{pos['pos_id']}"]
        self.assertEqual(algo["status"], "TRIGGERED")


if __name__ == "__main__":
    unittest.main()
