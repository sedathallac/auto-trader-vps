"""BLOK27.5 — H1 mark timestamp integrity (F-1) regression tests.

Kapsam (görev §5 zorunlu regresyon listesi):
  1. Warmup timestamp doğruluğu      (replay edilen bar KENDİ kapanış ts'i)
  2. Live timestamp doğruluğu        (canlı bar kapanışı open+4h)
  3. Timestamp eksikliği fail-closed (TypeError/ValueError, satır YAZILMAZ)
  4. UTC dönüşümü                    (epoch ms ↔ UTC duvar saati)
  5. Milisaniye/saniye karışıklığı   (saniye ölçeği / kesirli / bool RED)
  6. Duplicate bar                   (append-only marks + REPLACE bars)
  7. Restart recovery                (_reload_marks deterministik tie-break)
  8. Look-ahead prevention           (mark ancak open+4h'te yürürlüğe girer)
  9. E-aligned mark/proxy comparison (bisect ts<=E, sızıntı YOK)
 10. Bars tablosu değişmedi          (note_bar davranışı birebir korunur)
 11. Signal/PnL yolu tutarlılığı     (upnl/equity math ts'den bağımsız)
"""

import bisect
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from h1_adapter import (H1SignalEngine, H1Router, H1Reconciler,
                        bar_close_ts, BAR_INTERVAL_MS)
from paper_broker import PaperBroker, MS_EPOCH_FLOOR, _require_mark_ts
from risk_checklist import PretradeChecklist
from signal_dedup import SignalDedupStore
from tests.test_blok25_h1_adapter import FakeRisk, FakeWorld, _router, _feed
from tests.test_blok25_paper import (SYM, OTHER, T0, BAR, _decision, _bar,
                                     _broker)

STEP = BAR                      # 4h bar kadansı (adapter testlerindeki ad)


def _broker_router(db=None, execute=True):
    """PaperBroker'ı world+executor olarak kullanan router (üretim şekli)."""
    tmp = tempfile.mkdtemp()
    db = db or os.path.join(tmp, "paper_ledger.db")
    broker = PaperBroker(db, [SYM, OTHER])
    store = SignalDedupStore(os.path.join(tmp, "dedup.sqlite3"))
    store.init_schema()
    router = H1Router(H1SignalEngine(), PretradeChecklist(FakeRisk(),
                                                          signal_dedup=store),
                      broker, broker, reconciler=H1Reconciler(),
                      execute=execute)
    return router, broker, db


# ---------------------------------------------------------------------------
class TestBarCloseTs(unittest.TestCase):
    """Adapter sözleşmesi: bar_close_ts(bar_ts) = open + 4h (UTC epoch ms)."""

    def test_close_ts_is_open_plus_interval(self):
        self.assertEqual(bar_close_ts(T0), T0 + BAR_INTERVAL_MS)
        self.assertEqual(bar_close_ts(T0 + 17 * STEP),
                         T0 + 18 * STEP)

    def test_integral_float_accepted(self):
        self.assertEqual(bar_close_ts(float(T0)), T0 + BAR_INTERVAL_MS)

    def test_rejects_missing_bool_string_fractional_seconds(self):
        for bad in (None, True, False, "1730000000000", T0 + 0.5,
                    1_795_400_000):          # saniye ölçeği (~1.8e9)
            with self.assertRaises(ValueError):
                bar_close_ts(bad)


class TestRequireMarkTs(unittest.TestCase):
    """Broker sözleşmesi: _require_mark_ts yaln geçerli UTC epoch ms kabul."""

    def test_valid_passthrough(self):
        self.assertEqual(_require_mark_ts(T0), T0)
        self.assertEqual(_require_mark_ts(float(T0)), T0)

    def test_rejects_bad_values(self):
        for bad in (None, True, False, T0 + 0.5, "1730000000000",
                    1_795_400_000, 0, -5):
            with self.assertRaises(ValueError):
                _require_mark_ts(bad)
        self.assertLess(MS_EPOCH_FLOOR, T0)   # floor gerçek ms değerlerini
        # geçiyor (T0 = Kas 2024)


# ---------------------------------------------------------------------------
class TestTimestampAccuracy(unittest.TestCase):
    """(1) warmup + (2) live timestamp doğruluğu — F-1 senaryosunun birebir
    yeniden üretimi: canlı bar T1 işlendikten SONRA daha eski T0 replay'i,
    eski kodda T1'in ts'siyle damgalanıyordu; düzeltmede kendi kapanışı
    (open+4h) ile damgalanmalı."""

    def test_live_bar_mark_carries_own_close_ts(self):
        r, b, _ = _broker_router()
        r.run_bar(SYM, T0, 100.0, 110.0)        # canlı bar
        rows = b.marks_rows(SYM)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], T0 + BAR)      # open + 4h
        self.assertEqual(rows[0]["mark_px"], 110.0)

    def test_warmup_replay_mark_carries_replayed_bar_close_ts(self):
        r, b, _ = _broker_router()
        # canlı (yeni) bar T1: runner bunu note_bar + run_bar ile işler
        r.run_bar(SYM, T0 + 3 * BAR, 100.0, 108.0)
        # RESTART warmup replay: daha eski T0 barı execute=False
        # (eski kodda bu satır ts=T1 alıyordu — F-1 kirliliğinin kaynağı)
        r.run_bar(SYM, T0, 90.0, 95.0, execute=False)
        rows = b.marks_rows(SYM)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["ts"], rows[0]["mark_px"]),
                         (T0 + 4 * BAR, 108.0))       # canlı: kendi kapanışı
        self.assertEqual((rows[1]["ts"], rows[1]["mark_px"]),
                         (T0 + BAR, 95.0))            # replay: KENDİ kapanışı
        # F-1 regresyonu: replay satırı asla canlı barın ts'sini taşımaz
        self.assertNotEqual(rows[1]["ts"], T0 + 3 * BAR)
        self.assertNotEqual(rows[1]["ts"], T0 + 4 * BAR)

    def test_adapter_passes_close_ts_through_world_hook(self):
        world = FakeWorld()
        router = _router(world=world)
        _feed(router, SYM, [100.0, 101.0, 102.0])
        hook_calls = [c for c in world.calls if c[0] == "on_bar_close"]
        self.assertEqual(hook_calls,
                         [("on_bar_close", SYM, T0 + STEP),
                          ("on_bar_close", SYM, T0 + 2 * STEP),
                          ("on_bar_close", SYM, T0 + 3 * STEP)])


class TestUTCTimestamp(unittest.TestCase):
    """(4) UTC dönüşümü: mark ts = bar kapanış anının UTC epoch ms değeri."""

    def test_close_ts_maps_to_utc_wall_time(self):
        # 2026-11-23T00:00:00Z barı → kapanış 04:00:00Z
        t = int(datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
                .timestamp() * 1000)
        self.assertEqual(t % BAR_INTERVAL_MS, 0)      # 4h hizalı açık
        ct = bar_close_ts(t)
        self.assertEqual(ct, t + 4 * 3600 * 1000)
        wall = datetime.fromtimestamp(ct / 1000, timezone.utc)
        self.assertEqual((wall.year, wall.month, wall.day, wall.hour),
                         (2026, 11, 23, 4))

    def test_stored_mark_ts_round_trips_utc(self):
        b = _broker()
        t = int(datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
                .timestamp() * 1000)
        b.note_bar(SYM, t, 100.0, 110.0, 1e6)
        b.on_bar_close(SYM, 110.0, t + BAR)
        row = b.marks_rows(SYM)[-1]
        wall = datetime.fromtimestamp(row["ts"] / 1000, timezone.utc)
        self.assertEqual(wall.hour, 4)                 # UTC (yerel DEĞİL)


class TestFailClosed(unittest.TestCase):
    """(3)+(5) timestamp eksikliği / yanlış birim fail-closed."""

    def test_missing_ts_is_typeerror(self):
        b = _broker()
        with self.assertRaises(TypeError):
            b.on_bar_close(SYM, 110.0)          # ts hiç verilmedi

    def test_none_ts_is_valueerror_and_writes_nothing(self):
        b = _broker()
        b.note_bar(SYM, T0, 100.0, 110.0, 1e6)
        n = len(b.marks_rows(SYM))
        with self.assertRaises(ValueError):
            b.on_bar_close(SYM, 110.0, None)
        self.assertEqual(len(b.marks_rows(SYM)), n)   # satır YAZILMADI

    def test_seconds_scale_rejected(self):
        b = _broker()
        with self.assertRaises(ValueError):
            b.on_bar_close(SYM, 110.0, 1_795_400_000)   # saniye ölçeği
        self.assertEqual(b.marks_rows(SYM), [])

    def test_fractional_and_bool_rejected(self):
        b = _broker()
        for bad in (T0 + 0.5, True):
            with self.assertRaises(ValueError):
                b.on_bar_close(SYM, 110.0, bad)
        self.assertEqual(b.marks_rows(SYM), [])

    def test_future_ts_rejected_no_lookahead_write(self):
        # enjekte edilebilir saat: broker T0 anında duruyor varsayalım
        b = _broker(clock_ms=lambda: T0)
        with self.assertRaises(ValueError):
            b.on_bar_close(SYM, 110.0, T0 + 120_000)   # +120s → gelecek
        self.assertEqual(b.marks_rows(SYM), [])
        # sınır: clock+60s slack'e kadar olan geçmiş/değin değer kabul
        b.on_bar_close(SYM, 110.0, T0 + 30_000)
        self.assertEqual(len(b.marks_rows(SYM)), 1)


class TestDuplicateAndIdempotency(unittest.TestCase):
    """(6) duplicate bar: marks append-only kalır, bars REPLACE ile tek.

    (7) restart recovery: _reload_marks ts DESC + id DESC deterministik.
    """

    def test_same_bar_twice_two_mark_rows_one_bars_row(self):
        b = _broker()
        _bar(b, T0, 100.0, 110.0)
        _bar(b, T0, 100.0, 110.0)          # duplicate (restart replay gibi)
        rows = b.marks_rows(SYM)
        self.assertEqual(len(rows), 2)               # append-only korunur
        self.assertEqual({(r["ts"], r["mark_px"]) for r in rows},
                         {(T0 + BAR, 110.0)})       # aynı ts, aynı fiyat
        con = sqlite3.connect(b.db_file)
        n = con.execute("SELECT COUNT(*) FROM bars WHERE symbol=? AND ts=?",
                        (SYM, T0)).fetchone()[0]
        con.close()
        self.assertEqual(n, 1)                       # bars REPLACE idempotent

    def test_reload_marks_deterministic_newest_wins(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "paper.db")
        b = PaperBroker(db, [SYM, OTHER])
        _bar(b, T0, 100.0, 110.0)
        _bar(b, T0 + BAR, 110.0, 105.0)
        _bar(b, T0 + BAR, 110.0, 105.0)     # aynı barın replay kopyası
        b2 = PaperBroker(db, [SYM, OTHER])  # restart: fresh handle
        self.assertEqual(b2._last_mark[SYM], 105.0)  # en yeni bar kapanışı

    def test_reload_tie_break_is_highest_id(self):
        # aynı ts'ye iki FARKLI fiyat (legacy kirlilik şekli): deterministik
        # olarak SON yazılan (id büyük) kazanır — tanımsız sıra yok
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "paper.db")
        b = PaperBroker(db, [SYM, OTHER])
        con = sqlite3.connect(db)
        con.execute("INSERT INTO marks (ts, symbol, mark_px, upnl, equity, "
                    "drawdown) VALUES (?, ?, ?, 0, 0, 0)",
                    (T0, SYM, 100.0))
        con.execute("INSERT INTO marks (ts, symbol, mark_px, upnl, equity, "
                    "drawdown) VALUES (?, ?, ?, 0, 0, 0)",
                    (T0, SYM, 222.0))
        con.commit()
        con.close()
        b2 = PaperBroker(db, [SYM, OTHER])
        self.assertEqual(b2._last_mark[SYM], 222.0)


class TestLookaheadPrevention(unittest.TestCase):
    """(8)+(9) look-ahead önleme ve E-aligned seçim.

    Bar [ot, ot+4h) kapanışı yalnız ot+4h anında YÜRÜRLÜĞE girer: bu barın
    marks satırı ts=ot+4h taşıdığından ts<=E bisect'i bar sürerken O barı
    SEÇEMEZ (eski kodda ts=ot taşıyordu → bar sürerken 'gelecek' kapanış
    sızıyordu).
    """

    def _marks_with_two_bars(self):
        b = _broker()
        _bar(b, T0, 100.0, 100.0)           # kapanış 100 → bilinme anı T0+4h
        _bar(b, T0 + BAR, 100.0, 110.0)     # kapanış 110 → bilinme anı T0+8h
        return b

    @staticmethod
    def _in_force(ts_list, px_list, E):
        idx = bisect.bisect_right(ts_list, E) - 1
        return px_list[idx] if idx >= 0 else None

    def test_mark_not_in_force_before_bar_completes(self):
        b = self._marks_with_two_bars()
        rows = b.marks_rows(SYM)
        ts_list = [r["ts"] for r in rows]
        px_list = [r["mark_px"] for r in rows]
        self.assertEqual(ts_list, [T0 + BAR, T0 + 2 * BAR])
        # ilk barın yaşamı içinde: henüz hiçbir mark yürürlükte değil
        self.assertIsNone(self._in_force(ts_list, px_list, T0 + BAR - 1))
        # ilk kapanıştan hemen sonra: 100
        self.assertEqual(self._in_force(ts_list, px_list, T0 + BAR + 1), 100.0)
        # İKİNCİ bar sürerken: hâlâ 100 — 110 SIZMAZ (look-ahead yok)
        self.assertEqual(self._in_force(ts_list, px_list, T0 + 2 * BAR - 1),
                         100.0)
        # ikinci kapanıştan sonra: 110
        self.assertEqual(self._in_force(ts_list, px_list, T0 + 2 * BAR + 1),
                         110.0)

    def test_e_aligned_series_matches_runner_in_force_proxy(self):
        # runner'ın gerçek proxy'si: son KAPANMIŞ barın kapanışı. Düzeltilmiş
        # marks serisi üzerinden ts<=E seçimi aynı değeri verir (E-aligned).
        b = self._marks_with_two_bars()
        rows = sorted(b.marks_rows(SYM), key=lambda r: r["ts"])
        ts_list = [r["ts"] for r in rows]
        px_list = [r["mark_px"] for r in rows]
        for E, expected in ((T0 + BAR - 1, None),
                            (T0 + BAR, 100.0),
                            (T0 + 2 * BAR - 1, 100.0),
                            (T0 + 2 * BAR, 110.0),
                            (T0 + 3 * BAR, 110.0)):
            idx = bisect.bisect_right(ts_list, E) - 1
            got = px_list[idx] if idx >= 0 else None
            self.assertEqual(got, expected, f"E={E - T0}ms")


class TestBarsTableUnchanged(unittest.TestCase):
    """(10) bars tablosu davranışı birebir korunur (temiz kaynak)."""

    def test_note_bar_row_exact_and_on_bar_close_adds_nothing(self):
        b = _broker()
        b.note_bar(SYM, T0, 100.5, 110.25, 1.5e6)
        con = sqlite3.connect(b.db_file)
        rows = con.execute("SELECT symbol, ts, open, close, quote_volume "
                           "FROM bars").fetchall()
        con.close()
        self.assertEqual(rows, [(SYM, T0, 100.5, 110.25, 1.5e6)])
        _bar(b, T0, 100.5, 110.25, 1.5e6)         # mark yazımı
        con = sqlite3.connect(b.db_file)
        n = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        con.close()
        self.assertEqual(n, 1)                    # marks yazımı bars'a DOKUNMAZ


class TestSignalPnLPathConsistency(unittest.TestCase):
    """(11) sinyal/PnL yolu: ts sözleşmesi upnl/equity/fill math'ını
    değiştirmez (değerler fiyattan türetilir, ts'den değil)."""

    def test_mark_row_values_independent_of_ts(self):
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0, 100.0, 90.0)          # aleyhte kapanış → negatif uPnL
        row = b.marks_rows(SYM)[-1]
        pos = b._position(SYM)
        expected_upnl = pos["direction"] * pos["units"] * \
            (90.0 - pos["entry_px"])
        self.assertAlmostEqual(row["upnl"], expected_upnl, places=9)
        self.assertAlmostEqual(row["equity"], b.equity(), places=9)
        self.assertGreater(row["drawdown"], 0.0)

    def test_engine_decisions_unchanged_by_ts_contract(self):
        # adapter testlerinin motor-denklik süiti (217 test) birebir geçer;
        # burada özellikle: on_close ts taşısa da karar zinciri aynı kalır
        world = FakeWorld()
        router = _router(world=world)
        ds = _feed(router, SYM, [100.0 + i for i in range(185)])
        self.assertEqual(len(ds), 1)               # tek flip (denklik süitiyle)
        self.assertEqual(router.engine.direction(SYM), 1)


if __name__ == "__main__":
    unittest.main()
