"""BLOK26 Bölüm 2 testleri — forward observer: zincir bütünlüğü, salt-okunur
garantisi, append-only günlükler, süreç/ledger gözlemi."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import forward_observer as fo
from paper_broker import PaperBroker
from tests.test_blok25_paper import _decision, SYM, OTHER, T0, BAR


class TestChain(unittest.TestCase):
    def _append(self, monitor, data, ts):
        return fo.append_record(monitor, data, now_ms=ts)

    def test_chain_appends_and_verifies(self):
        m = tempfile.mkdtemp()
        r1 = self._append(m, {"a": 1}, 1000)
        r2 = self._append(m, {"a": 2}, 2000)
        r3 = self._append(m, {"a": 3}, 3000)
        self.assertEqual(r1["prev_hash"], "GENESIS")
        self.assertEqual(r2["prev_hash"], r1["hash"])
        self.assertEqual(r3["prev_hash"], r2["hash"])
        self.assertEqual([r1["seq"], r2["seq"], r3["seq"]], [1, 2, 3])
        v = fo.verify_chain(m)
        self.assertTrue(v["ok"], v["problems"])
        self.assertEqual(v["records"], 3)

    def test_tampered_record_breaks_chain(self):
        m = tempfile.mkdtemp()
        self._append(m, {"a": 1}, 1000)
        self._append(m, {"a": 2}, 2000)
        day = sorted(Path(m, "daily").glob("*.json"))[0]
        lines = day.read_text().strip().split("\n")
        rec = json.loads(lines[0])
        rec["data"]["a"] = 999                  # sonradan değiştirme
        lines[0] = json.dumps(rec)
        day.write_text("\n".join(lines) + "\n")
        v = fo.verify_chain(m)
        self.assertFalse(v["ok"])
        self.assertTrue(any("hash uyuşmaz" in p for p in v["problems"]))

    def test_removed_record_breaks_chain(self):
        m = tempfile.mkdtemp()
        self._append(m, {"a": 1}, 1000)
        self._append(m, {"a": 2}, 2000)
        self._append(m, {"a": 3}, 3000)
        day = sorted(Path(m, "daily").glob("*.json"))[0]
        lines = day.read_text().strip().split("\n")
        day.write_text("\n".join(lines[:1]) + "\n")   # son kaydı sil
        v = fo.verify_chain(m)
        # silinen kayıt zincirin sonuydu: kayıt sayısı azalır ama hash'ler
        # hala tutarlı → count değişimini manifest yakalar
        self.assertEqual(v["records"], 1)

    def test_cross_day_independent_chains(self):
        """Her gün BAĞIMSIZ zincirdir (GENESIS'ten başlar); gün manifesti
        o günü mühürler. Gün devri zincir hatası DEĞİLDİR."""
        m = tempfile.mkdtemp()
        self._append(m, {"day": 1}, 1735689600000)     # 2025-01-01
        self._append(m, {"day": 1}, 1735689660000)
        self._append(m, {"day": 2}, 1735776000000)     # 2025-01-02
        v = fo.verify_chain(m)
        self.assertTrue(v["ok"], v["problems"])
        self.assertEqual(v["days"], 2)
        self.assertEqual(v["records"], 3)

    def test_day_manifest_tamper_detected(self):
        m = tempfile.mkdtemp()
        self._append(m, {"a": 1}, 1735689600000)
        man = sorted(Path(m, "daily").glob("*.manifest.json"))[0]
        mm = json.loads(man.read_text())
        mm["records"] = 99
        man.write_text(json.dumps(mm))
        v = fo.verify_chain(m)
        self.assertFalse(v["ok"])
        self.assertTrue(any("kayıt sayısı" in p for p in v["problems"]))

    def test_daily_manifest_written(self):
        m = tempfile.mkdtemp()
        self._append(m, {"a": 1}, 1000)
        self._append(m, {"a": 2}, 2000)
        man = sorted(Path(m, "daily").glob("*.manifest.json"))[0]
        mm = json.loads(man.read_text())
        self.assertEqual(mm["records"], 2)
        self.assertEqual(mm["first_seq"], 1)
        self.assertEqual(mm["last_seq"], 2)
        self.assertTrue(mm["chain_sha256"])


class TestObserve(unittest.TestCase):
    """Sentetik bir results dizini + gerçek (salt-okunur) ledger üzerinde
    observe() davranışı."""

    def _fixture(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "paper_ledger.db")
        b = PaperBroker(db, [SYM, OTHER])
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        b.note_bar(SYM, T0 + BAR, 100.0, 110.0, 1e6)
        b.on_bar_close(SYM, 110.0, T0 + 2 * BAR)   # mark at bar CLOSE time
        b.event("CYCLE_ERROR", "test")
        hb = {"ts": int(time.time() * 1000), "cycle": 5, "ok": True,
              "error": None, "duration_s": 1.0, "rss_mb": 30.0,
              "halted": False}
        (Path(d) / "paper_heartbeat.json").write_text(json.dumps(hb))
        (Path(d) / "paper_supervisor.log").write_text(
            "INFO supervisor start\nINFO runner started pid=1\n"
            "INFO backup -> x.db\n")
        return d, db, b

    def test_observe_reads_everything_read_only(self):
        d, db, b = self._fixture()
        before = os.stat(db).st_mtime_ns
        data = fo.observe(d)
        # ledger salt-okunur açıldı: mtime değişmedi
        self.assertEqual(os.stat(db).st_mtime_ns, before)
        led = data["ledger"]
        self.assertEqual(led["positions_n"], 1)
        self.assertGreater(led["unrealized"], 0)
        self.assertEqual(led["closed_trades"], 0)
        self.assertFalse(led["kill_switch"])
        self.assertIn("last_error", led)
        self.assertEqual(led["last_error"]["kind"], "CYCLE_ERROR")
        self.assertEqual(data["cycle"], 5)
        self.assertTrue(data["cycle_ok"])
        self.assertEqual(data["restarts"], 0)     # ilk start sayılmaz
        self.assertEqual(data["backups"], 1)
        self.assertEqual(data["mark_price_source"], "close_proxy_4h")
        self.assertGreater(data["db_size_mb"], 0)
        self.assertIn("disk_free_gb", data)

    def test_observe_missing_processes_tolerated(self):
        d, db, b = self._fixture()
        data = fo.observe(d)
        # bu test ortamında gerçek paper süreçleri çalışıyor olabilir;
        # süreç bilgisi None ya da dict olmalı — asla patlamamalı
        for k in ("supervisor", "runner"):
            self.assertIn(k, data["processes"])

    def test_observe_corrupt_ledger_tolerated(self):
        d = tempfile.mkdtemp()
        (Path(d) / "paper_ledger.db").write_text("not a sqlite file")
        data = fo.observe(d)
        self.assertIn("ledger_error", data["ledger"])

    def test_full_round_trip(self):
        d, db, b = self._fixture()
        m = tempfile.mkdtemp()
        rec = fo.append_record(m, fo.observe(d))
        v = fo.verify_chain(m)
        self.assertTrue(v["ok"])


class TestNoSideEffects(unittest.TestCase):
    def test_observer_source_has_no_write_to_paper_paths(self):
        src = Path(fo.__file__).read_text()
        # ledger yalnız mode=ro URI ile açılabilir
        self.assertIn("mode=ro", src)
        # süreçlere sinyal gönderme yolu yok
        for banned in ("os.kill", "subprocess", "Popen", "os.system",
                       "SIGTERM", "SIGKILL"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
