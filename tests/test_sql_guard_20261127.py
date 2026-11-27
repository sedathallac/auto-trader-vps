"""GÖREV 2026-11-27 (hedefli güvenlik düzeltmeleri) — CANARY SQL
identifier allowlist'leri + strategy_engine sessiz hata görünürlüğü.

§3 A) canary_live.read_entry_pause iç get() tablo adı f-string ile SQL'e
     gömülüyordu → _read_state_value allowlist (live_state, canary_state);
     bilinmeyen tablo RED, değerler ? parametresi.
§3 B) canary_live._update_live_pos alan adları f-string ile SQL'e
     gömülüyordu → gerçek şemadan sütun allowlist'i; bilinmeyen sütun RED
     (ValueError), değerler ? parametresi. Tüm mevcut çağrı noktaları
     (protection_*/exit_*) allowlist içinde — ENA remediation akışı
     DEĞİŞMEZ.
§3 C) paper_broker._set_account alan adları f-string ile SQL'e gömülüyordu
     → gerçek şemadan (CREATE TABLE account) allowlist; bilinmeyen alan
     RED, değerler ? parametresi.
§4   strategy_engine.SignalStore.resolve_pending `except: pass` hata
     durumunu done=0 ile karıştırıyordu → hata artık last_resolve_error
     üzerinde ölçülebilir + stderr log; sözleşme (int dönüş, istisna YOK)
     KORUNUR.

AĞ ÇAĞRISI YOK (FakeExchange); canlı DB'ye YAZIM YOK (geçici dosyalar).
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import canary_live
from canary_live import (read_entry_pause, ENTRY_PAUSE_KEY,
                         DIR_ENABLED_KEY_LONG, DIR_ENABLED_KEY_SHORT)
from paper_broker import PaperBroker
from strategy_engine import SignalStore, Candles

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary,
                                 open_one, prime)


def _make_state_db(path):
    """read_entry_pause'ın okuduğu tabloları içeren geçici salt-yazma DB
    (test kendi DB'sini yaratır — canlı DB'ye DOKUNULMAZ)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE live_state "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE canary_state "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE live_orders (client_id TEXT, symbol TEXT,"
                     " status TEXT, updated_ts INTEGER, kind TEXT)")
        conn.executemany(
            "INSERT INTO live_state (key, value) VALUES (?, ?)",
            [(ENTRY_PAUSE_KEY, "1"),
             (DIR_ENABLED_KEY_LONG, "1"),
             (DIR_ENABLED_KEY_SHORT, "0"),
             ("entry_pause_ts", "1732700000000"),
             ("entry_pause_by", "operator"),
             ("entry_pause_reason", "test"),
             ("halt_state", "CANARY_ACTIVE"),
             ("worker_pid", "4242")])
        conn.executemany(
            "INSERT INTO canary_state (key, value) VALUES (?, ?)",
            [("status", "ACTIVE"), ("mode", "test")])
        conn.execute("INSERT INTO live_orders VALUES "
                     "('C281X1','ETHUSDT','INTENT',1,'ENTRY')")
        conn.commit()
    finally:
        conn.close()
    return str(path)


class StateReadTableAllowlistTest(unittest.TestCase):
    """§3 A — get() tablo allowlist'i."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = _make_state_db(os.path.join(self._tmp.name, "s.db"))

    def test_valid_tables_read(self):
        out = read_entry_pause(self.db_path)
        self.assertTrue(out["entry_paused"])
        self.assertTrue(out["dir_enabled_long"])
        self.assertFalse(out["dir_enabled_short"])
        self.assertEqual(out["rm_status"], "ACTIVE")      # canary_state
        self.assertEqual(out["worker_pid"], "4242")       # live_state
        self.assertEqual(len(out["pending_entries"]), 1)

    def test_unknown_table_rejected(self):
        """Bilinmeyen tablo adı SQL'e ASLA gömülmez — ValueError."""
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(ValueError) as cm:
                canary_live._read_state_value(
                    conn, "k", table="evil; DROP TABLE live_state")
            self.assertIn("izin verilmeyen state tablosu",
                          str(cm.exception))
            # red sonrası tablo HALA duruyor (SQL çalışmadı)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM live_state").fetchone())
        finally:
            conn.close()

    def test_allowlist_covers_exactly_used_tables(self):
        self.assertEqual(canary_live._STATE_READ_TABLES,
                         ("live_state", "canary_state"))


class LivePosColumnAllowlistTest(unittest.TestCase):
    """§3 B — _update_live_pos sütun allowlist'i (gerçek pozisyon üzerinde)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()
        self.lc = make_canary(
            self._tmp.name, FakeExchange(clock_ms=self.clock))
        prime(self.lc)
        rec = open_one(self.lc)  # tam doğrulanmış giriş: FILLED + PROTECTED
        self.pos_id = rec["pos_id"]

    def test_valid_fields_update(self):
        self.lc._update_live_pos(
            self.pos_id, exit_status="EXIT_PENDING", exit_reason="test")
        row = self.lc._live_pos_row(self.pos_id)
        self.assertEqual(row["exit_status"], "EXIT_PENDING")
        self.assertEqual(row["exit_reason"], "test")
        # koruma alanları etkilenmedi
        self.assertEqual(row["protection_status"], "PROTECTED")

    def test_unknown_column_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.lc._update_live_pos(self.pos_id, evil_col="x")
        self.assertIn("bilinmeyen sütun", str(cm.exception))
        # red → UPDATE yapılmadı: satır değişmedi
        row = self.lc._live_pos_row(self.pos_id)
        self.assertEqual(row["exit_status"], "NONE")

    def test_allowlist_matches_real_schema_columns(self):
        """Allowlist yalnız gerçek live_positions sütunlarının güncellenebilir
        alt kümesi (pos_id/symbol kimlik, updated_ts metodun kendisinin)."""
        self.assertEqual(
            canary_live.LiveCanary._LIVE_POS_UPDATE_COLS,
            {"protection_client_id", "protection_status",
             "exit_client_id", "exit_status", "exit_reason"})


class PaperAccountAllowlistTest(unittest.TestCase):
    """§3 C — paper_broker._set_account alan allowlist'i."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.broker = PaperBroker(
            os.path.join(self._tmp.name, "paper.db"), ["BTCUSDT"])

    def test_valid_fields_update(self):
        self.broker._set_account(cash=555.0)
        self.assertEqual(self.broker._account()["cash"], 555.0)
        self.broker._set_account(halted=1, halt_reason="test-dur")
        acc = self.broker._account()
        self.assertEqual(acc["halted"], 1)
        self.assertEqual(acc["halt_reason"], "test-dur")

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.broker._set_account(evil="x")
        self.assertIn("bilinmeyen sütun", str(cm.exception))
        # red → UPDATE yapılmadı: bakiye değişmedi
        self.assertEqual(self.broker._account()["cash"], 1000.0)

    def test_allowlist_matches_real_schema(self):
        self.assertEqual(
            PaperBroker._ACCOUNT_UPDATE_COLS,
            {"cash", "peak_equity", "halted", "halt_reason"})


class ResolvePendingErrorVisibilityTest(unittest.TestCase):
    """§4 — resolve_pending: hata artık sessiz DEĞİL; done=0 ile KARIŞMAZ;
    çağrı sözleşmesi (int dönüş, istisna YOK) KORUNUR."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cd = Candles(o=[], h=[], l=[], c=[], v=[], t=[])

    def test_error_is_visible_and_measurable(self):
        store = SignalStore(os.path.join(self._tmp.name, "sig.db"))
        with mock.patch.object(store, "_conn",
                               side_effect=RuntimeError("boom")):
            err_out = io.StringIO()
            with redirect_stderr(err_out):
                done = store.resolve_pending("BTCUSDT", self.cd, 0.0, 0.02)
        # sözleşme: istisna YÜKSELMEDİ, dönüş yine int
        self.assertEqual(done, 0)
        # ama hata done=0 ile KARIŞMAZ — ölçülebilir durum + log
        self.assertIsNotNone(store.last_resolve_error)
        self.assertIn("RuntimeError", store.last_resolve_error)
        self.assertIn("resolve_pending HATA", err_out.getvalue())

    def test_zero_done_without_error_is_distinct(self):
        """Hatayla karışmaz: boş mağazada 0 çözüm normal — hata durumu
        None kalır ('çözülecek satır yoktu')."""
        store = SignalStore(os.path.join(self._tmp.name, "sig2.db"))
        done = store.resolve_pending("BTCUSDT", self.cd, 0.0, 0.02)
        self.assertEqual(done, 0)
        self.assertIsNone(store.last_resolve_error)

    def test_error_state_reset_on_next_call(self):
        store = SignalStore(os.path.join(self._tmp.name, "sig3.db"))
        with mock.patch.object(store, "_conn",
                               side_effect=RuntimeError("boom")):
            store.resolve_pending("BTCUSDT", self.cd, 0.0, 0.02)
        self.assertIsNotNone(store.last_resolve_error)
        # sonraki başarılı çağrı durumu temizler
        store.resolve_pending("BTCUSDT", self.cd, 0.0, 0.02)
        self.assertIsNone(store.last_resolve_error)


if __name__ == "__main__":
    unittest.main()
