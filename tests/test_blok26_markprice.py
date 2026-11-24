"""BLOK26 Bölüm 4 testleri — Mark Price observer: REST tick ayrıştırma,
kayıt zinciri/manifest, stale eşiği, istatistik, proxy karşılaştırması.

Ağ çağrısı YOK: fetch_all/rest_tick/compare_proxy saf veriyle test edilir.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import mark_price_observer as mp


def _row(sym="BTCUSDT", mark="60000.5", index="60001.0", rate="0.0001",
         nxt=1795420800000, ts=1795402064001):
    return {"symbol": sym, "markPrice": mark, "indexPrice": index,
            "lastFundingRate": rate, "interestRate": "0.00010000",
            "nextFundingTime": nxt, "time": ts}


class TestRestTick(unittest.TestCase):
    def test_fields_match_ws_payload(self):
        """REST alanları WS tick düzeniyle birebir: p/i/r/T/E."""
        t = mp.rest_tick(_row(), recv_ms=123456)
        self.assertEqual(t, {"t": 123456, "E": 1795402064001,
                             "s": "BTCUSDT", "p": 60000.5, "i": 60001.0,
                             "r": 0.0001, "T": 1795420800000})

    def test_bad_row_raises_no_silent_invention(self):
        """Bozuk satır sessizce veri uydurmaZ — hata fırlar."""
        with self.assertRaises((KeyError, ValueError, TypeError)):
            mp.rest_tick({"symbol": "BTCUSDT"}, recv_ms=1)
        with self.assertRaises(ValueError):
            mp.rest_tick(_row(mark="not-a-number"), recv_ms=1)


class TestStaleRule(unittest.TestCase):
    def test_stale_symbols_over_threshold(self):
        """Önceden belgelenmiş kural: 60s (REST) üstü güncellenmeyen
        sembol stale'dir; altındaki değildir."""
        now = 1_000_000
        last = {"BTCUSDT": now - 61_000, "ETHUSDT": now - 59_000,
                "SOLUSDT": now - 100_000}
        self.assertEqual(mp.stale_symbols(last, now,
                                          mp.STALE_THRESHOLD_REST_S),
                         ["BTCUSDT", "SOLUSDT"])

    def test_empty_when_fresh(self):
        now = 1_000_000
        self.assertEqual(mp.stale_symbols({"BTCUSDT": now}, now,
                                          mp.STALE_THRESHOLD_REST_S), [])


class TestRecorder(unittest.TestCase):
    def _rec(self):
        d = tempfile.mkdtemp()
        return d, mp.MarkPriceRecorder(d)

    def test_append_manifest_and_reopen(self):
        """Kayıt → günlük manifest sha256'i dosyayla uyuşur; süreç
        yeniden açılırsa sayaç dosyadan DEVAM eder (append-only)."""
        d, r = self._rec()
        t0 = 1735689600000
        for k in range(3):
            r.record({"t": t0 + k * 1000, "E": t0 + k, "s": "BTCUSDT",
                      "p": 1.0, "i": 1.0, "r": 0.0, "T": 0})
        r.close()
        man = json.loads((Path(d) / "2025-01-01.manifest.json").read_text())
        self.assertEqual(man["records"], 3)
        self.assertEqual(man["stream"], "rest:premiumIndex")
        h = mp.hashlib.sha256((Path(d) / "2025-01-01.jsonl").read_bytes()
                              ).hexdigest()
        self.assertEqual(man["sha256"], h)
        self.assertEqual(man["first_ts"], t0)
        self.assertEqual(man["last_ts"], t0 + 2000)
        # yeniden aç: 4. kayıt
        r2 = mp.MarkPriceRecorder(d)
        r2.record({"t": t0 + 3000, "E": t0 + 3, "s": "BTCUSDT",
                   "p": 1.0, "i": 1.0, "r": 0.0, "T": 0})
        r2.close()
        man2 = json.loads((Path(d) / "2025-01-01.manifest.json").read_text())
        self.assertEqual(man2["records"], 4)
        lines = (Path(d) / "2025-01-01.jsonl").read_text().strip().split("\n")
        self.assertEqual(len(lines), 4)

    def test_cross_day_separate_files(self):
        d, r = self._rec()
        tick = {"E": 0, "s": "BTCUSDT", "p": 1.0, "i": 1.0, "r": 0.0, "T": 0}
        r.record(dict(tick, t=1735689600000))       # 2025-01-01
        r.record(dict(tick, t=1735776000000))       # 2025-01-02
        r.close()
        self.assertTrue((Path(d) / "2025-01-01.jsonl").exists())
        self.assertTrue((Path(d) / "2025-01-02.jsonl").exists())

    def test_events_file(self):
        d, r = self._rec()
        r.event("OBSERVER_START", "test")
        ev = json.loads((Path(d) / "events.jsonl").read_text().strip())
        self.assertEqual(ev["kind"], "OBSERVER_START")
        self.assertEqual(ev["detail"], "test")


class TestStats(unittest.TestCase):
    def test_stats_per_symbol_and_gaps(self):
        d = tempfile.mkdtemp()
        t0 = 1735689600000
        rows = []
        for k in range(5):      # BTC: 5 tick, 2s aralık
            rows.append({"t": t0 + k * 2000, "E": t0 + k * 2000 - 100,
                         "s": "BTCUSDT", "p": 1.0, "i": 1.0, "r": 0.0,
                         "T": 0})
        rows.append({"t": t0 + 30_000, "E": t0 + 30_000, "s": "ETHUSDT",
                     "p": 1.0, "i": 1.0, "r": 0.0, "T": 0})
        with open(Path(d) / "2025-01-01.jsonl", "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        s = mp.stats(d, "2025-01-01")
        self.assertEqual(s["per_symbol"]["BTCUSDT"]["ticks"], 5)
        self.assertEqual(s["per_symbol"]["ETHUSDT"]["ticks"], 1)
        self.assertAlmostEqual(s["inter_tick_s"]["p50"], 2.0)
        self.assertFalse(s["manifest_match"])    # manifest yok
        self.assertIn("p", s["fields"])


class TestCompareProxy(unittest.TestCase):
    """Gerçek mark vs 4h-close proxy sapma hesabı (sentetik ledger).

    BLOK27.5: proxy kaynağı 'bars' tablosu (yalnız canlı işlenen barlar);
    bir bar yalnız AÇIK+4h'te (tamamlandığında) yürürlüğe girer —
    E-aligned, look-ahead YOK.
    """

    def _db(self, with_polluted_marks=False):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "paper.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE bars (symbol TEXT, ts INTEGER, "
                    "open REAL, close REAL, quote_volume REAL)")
        # 00:00Z'de AÇAN bar 100 kapanar (yürürlük 04:00Z'de), 04:00Z'de
        # açan bar 110 kapanar (yürürlük 08:00Z'de)
        base = 1735689600000     # 2025-01-01T00:00Z
        con.executemany("INSERT INTO bars VALUES (?,?,?,?,?)",
                        [("BTCUSDT", base - 14_400_000, 99.0, 100.0, 1e6),
                         ("BTCUSDT", base, 105.0, 110.0, 1e6)])
        if with_polluted_marks:
            # F-1 kirlilik şekli: geçmiş bar kapanışları GELECEK ts'lerle
            # damgalanmış marks satırları — compare bunları OKUMAZ bile
            con.execute("CREATE TABLE marks (symbol TEXT, ts INTEGER, "
                        "mark_px REAL)")
            con.executemany("INSERT INTO marks VALUES (?,?,?)",
                            [("BTCUSDT", base + 14_400_000, 999.0),
                             ("BTCUSDT", base + 20_000_000, 888.0)])
        con.commit()
        con.close()
        return d, db

    def test_deviation_vs_in_effect_proxy(self):
        d, db = self._db()
        t0 = 1735689600000     # 2025-01-01T00:00Z
        ticks = [
            # bar kapanışından HEMEN ÖNCE: proxy hala 100, gerçek 100.5
            {"t": t0 + 14_399_000, "E": t0 + 14_399_000, "s": "BTCUSDT",
             "p": 100.5, "i": 100.5, "r": 0.0, "T": 0},
            # bar kapanışından HEMEN SONRA: proxy 110, gerçek 110.2
            {"t": t0 + 14_401_000, "E": t0 + 14_401_000, "s": "BTCUSDT",
             "p": 110.2, "i": 110.2, "r": 0.0, "T": 0},
            # proxy'den çok sonra: gerçek 111.0 → dev = 10/110 bp ≈ 90.9
            {"t": t0 + 20_000_000, "E": t0 + 20_000_000, "s": "BTCUSDT",
             "p": 111.0, "i": 111.0, "r": 0.0, "T": 0},
        ]
        with open(Path(d) / "2025-01-01.jsonl", "w") as f:
            for tick in ticks:
                f.write(json.dumps(tick) + "\n")
        out = mp.compare_proxy(d, db, "2025-01-01")
        rec = out["per_symbol"]["BTCUSDT"]
        self.assertEqual(rec["n"], 3)
        devs = [50.0, (110.2 - 110.0) / 110.0 * 1e4,
                (111.0 - 110.0) / 110.0 * 1e4]
        self.assertAlmostEqual(rec["dev_bp_mean"], sum(devs) / 3, delta=0.01)
        self.assertAlmostEqual(rec["dev_bp_max"], 90.909, delta=0.01)
        self.assertEqual(out["skew_s"]["n"], 3)
        self.assertIn("bars", out["proxy_source"])   # kaynak belgeli

    def test_tick_before_first_completed_bar_skipped(self):
        d, db = self._db()
        t0 = 1735689600000
        with open(Path(d) / "2025-01-01.jsonl", "w") as f:
            f.write(json.dumps({"t": t0 - 5_000, "E": t0 - 5_000,
                                "s": "BTCUSDT", "p": 99.0, "i": 99.0,
                                "r": 0.0, "T": 0}) + "\n")
        out = mp.compare_proxy(d, db, "2025-01-01")
        self.assertNotIn("BTCUSDT", out["per_symbol"])   # proxy yok → atl

    def test_future_bar_cannot_leak_into_past_tick(self):
        """F-1 look-ahead kanıtı: bar ancak open+4h'te yürürlüğe girer;
        bar SÜRERKEN (E < open+4h) proxy ÖNCEKİ barın kapanışı kalır."""
        d, db = self._db(with_polluted_marks=True)
        t0 = 1735689600000
        ticks = [
            # ikinci bar [t0, t0+4h) SÜRERKEN: proxy 100 olmalı (110 DEĞİL)
            {"t": t0 + 7_200_000, "E": t0 + 7_200_000, "s": "BTCUSDT",
             "p": 100.2, "i": 100.2, "r": 0.0, "T": 0},
            # kirlendirilmiş marks tablosunda 999/888 fiyatlı GELECEK
            # ts'li satırlar var — compare bars'tan okuduğu için etkisiz
        ]
        with open(Path(d) / "2025-01-01.jsonl", "w") as f:
            for tick in ticks:
                f.write(json.dumps(tick) + "\n")
        out = mp.compare_proxy(d, db, "2025-01-01")
        rec = out["per_symbol"]["BTCUSDT"]
        self.assertEqual(rec["n"], 1)
        # |100.2 − 100|/100 · 1e4 = 20 bp (110 kullansaydı ~925 bp olurdu)
        self.assertAlmostEqual(rec["dev_bp_mean"], 20.0, delta=0.01)


class TestNoCredentials(unittest.TestCase):
    def test_source_has_no_credentials_or_signed_calls(self):
        """Public endpoint zorunluluğu: credential/signed çağrı yolu yok."""
        src = Path(mp.__file__).read_text()
        for banned in ("BINANCE_KEY", "BINANCE_SECRET", "X-MBX-APIKEY",
                       "signature", "/fapi/v1/leverageBracket",
                       "os.kill", "Popen"):
            self.assertNotIn(banned, src)
        self.assertIn("/fapi/v1/premiumIndex", src)


if __name__ == "__main__":
    unittest.main()
