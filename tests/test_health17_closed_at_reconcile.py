"""FAZ17 §12.5 — naive closed_at'lar YALNIZ HB tam eşleşmesiyle epoch'lanır.

Kök neden (F4, kanıt 2026-11-19): canlı ledger'daki 27 naive `closed_at` =
HB UTC `closed_at` + TAM 10800.000s (27/27, mikrosaniye birebir, executor_id
join) → eski yazar (≤01229ce) yerel UTC+03:00'u yermesiz yazmış. 108002c bu
satırları UNRESOLVED sayıyor (doğru: saat UYDURULMUYOR) ama günlük eşik/
ardışık hesap bu yüzden kör.

Bu testler deterministik çözümü kilitler: satır başına YALNIZ executor_id
join'i + "naive − 10800s == hb.close_timestamp (±1ms)" KOŞULU sağlanırsa
closed_at_epoch yazılır; koşulmazsa DOKUNULMAZ. closed_at orijinal metni
hiçbir durumda değişmez (raw preserved); geçerli epoch'a asla yazılmaz
(idempotent); eski şema → ValueError; sync'te yalnız TAM (cursor kanıtlı)
listede çalışır, PARTIAL'da hiç çalışmaz.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at
import risk_engine


DDL = """
CREATE TABLE trade_ledger (
    executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL NOT NULL DEFAULT 0,
    closed_at TEXT, recorded_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'sync',
    closed_at_epoch REAL);
"""


def _hb_iso(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "risk_ledger.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(DDL)
        conn.commit()
        conn.close()

    def conn(self):
        return sqlite3.connect(self.db, isolation_level=None)

    def seed(self, eid, closed_at, epoch=None, pnl=-0.5):
        conn = self.conn()
        conn.execute("INSERT INTO trade_ledger (executor_id, net_pnl_quote, "
                     "closed_at, recorded_at, closed_at_epoch) "
                     "VALUES (?, ?, ?, 'x', ?)", (eid, pnl, closed_at, epoch))
        conn.commit()
        conn.close()

    def row(self, eid):
        conn = self.conn()
        try:
            return conn.execute("SELECT closed_at, closed_at_epoch FROM trade_ledger "
                                "WHERE executor_id=?", (eid,)).fetchone()
        finally:
            conn.close()


class TestReconcileUnit(Base):
    def test_exact_plus3h_match_backfills_epoch_raw_preserved(self):
        # Canlı bulgunun kendisi: DB naive = HB UTC + 3h (mikrosaniye birebir).
        hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(hb_ts, timezone.utc)
                 + timedelta(seconds=10800)).replace(tzinfo=None).isoformat()
        self.seed("E1", naive)
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(conn, {
            "E1": {"close_timestamp": hb_ts, "closed_at": _hb_iso(hb_ts)}})
        conn.close()
        self.assertEqual(res["backfilled"], 1, res)
        iso, epoch = self.row("E1")
        self.assertEqual(iso, naive, "closed_at orijinal metni değişti (raw gitti)")
        self.assertAlmostEqual(epoch, hb_ts, places=6)

    def test_valid_epoch_never_overwritten(self):
        hb_ts = 1794971514.188374
        self.seed("E1", "2026-11-18T06:11:54.188374", epoch=hb_ts)
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(conn, {
            "E1": {"close_timestamp": hb_ts + 777}})
        conn.close()
        self.assertEqual(res["already_ok"], 1, res)
        self.assertEqual(self.row("E1")[1], hb_ts, "geçerli epoch ezildi")

    def test_no_hb_row_stays_unresolved(self):
        self.seed("E2", "2026-11-18T06:11:54.188374")
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(conn, {})
        conn.close()
        self.assertEqual(res["ambiguous"], 1, res)
        self.assertIsNone(self.row("E2")[1], "HB kantı olmadan epoch yazıldı")

    def test_different_offset_is_not_guessed(self):
        # +2s sapma bile TAM eşleşme değildir: ±1ms dışı → dokunulmaz.
        hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(hb_ts, timezone.utc)
                 + timedelta(seconds=10802)).replace(tzinfo=None).isoformat()
        self.seed("E3", naive)
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(conn, {
            "E3": {"close_timestamp": hb_ts}})
        conn.close()
        self.assertEqual(res["ambiguous"], 1, res)
        self.assertIsNone(self.row("E3")[1])

    def test_microsecond_match_within_tolerance(self):
        hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(hb_ts, timezone.utc)
                 + timedelta(seconds=10800)
                 + timedelta(seconds=0.0005)).replace(tzinfo=None).isoformat()
        self.seed("E4", naive)
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(conn, {
            "E4": {"close_timestamp": hb_ts}})
        conn.close()
        self.assertEqual(res["backfilled"], 1, res)
        self.assertAlmostEqual(self.row("E4")[1], hb_ts, places=6)

    def test_garbage_and_bad_hb_are_not_written(self):
        self.seed("G1", "dün akşam üstü")
        self.seed("G2", "2026-11-18T06:11:54.188374")
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(conn, {
            "G2": {"close_timestamp": None},
        })
        conn.close()
        self.assertEqual(res["invalid"], 1, res)
        self.assertEqual(res["ambiguous"], 1, res)
        self.assertIsNone(self.row("G1")[1])
        self.assertIsNone(self.row("G2")[1])

    def test_idempotent_second_run(self):
        hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(hb_ts, timezone.utc)
                 + timedelta(seconds=10800)).replace(tzinfo=None).isoformat()
        self.seed("E1", naive)
        hb = {"E1": {"close_timestamp": hb_ts}}
        c1 = self.conn()
        r1 = risk_engine.reconcile_naive_closed_at(c1, hb)
        c1.close()
        c2 = self.conn()
        r2 = risk_engine.reconcile_naive_closed_at(c2, hb)
        c2.close()
        self.assertEqual(r1["backfilled"], 1)
        self.assertEqual(r2, {"backfilled": 0, "ambiguous": 0,
                              "already_ok": 1, "invalid": 0,
                              "skipped_raced": 0},
                         "ikinci koşu no-op değil")

    def test_dry_run_never_writes(self):
        hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(hb_ts, timezone.utc)
                 + timedelta(seconds=10800)).replace(tzinfo=None).isoformat()
        self.seed("E1", naive)
        conn = self.conn()
        res = risk_engine.reconcile_naive_closed_at(
            conn, {"E1": {"close_timestamp": hb_ts}}, dry_run=True)
        conn.close()
        self.assertEqual(res.get("would_backfill"), 1, res)
        self.assertEqual(res["backfilled"], 0, "dry-run yazdı!")
        self.assertIsNone(self.row("E1")[1], "dry-run epoch'u doldurdu")

    def test_concurrent_epoch_fill_is_never_clobbered(self):
        # SELECT ile UPDATE ARASINDA başka yazar epoch'u doldurduysa üç-koşullu
        # UPDATE rowcount 0 verir → skipped_raced; mevcut epoch ASLA ezilmez.
        hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(hb_ts, timezone.utc)
                 + timedelta(seconds=10800)).replace(tzinfo=None).isoformat()
        self.seed("E1", naive)
        real = self.conn()

        class _RacingConn:
            def __init__(self):
                self._armed = True

            def execute(self, sql, params=()):
                if (self._armed and sql.strip().upper().startswith("UPDATE")
                        and "closed_at_epoch" in sql):
                    self._armed = False
                    # eşzamanlı yazarın COMMIT'ini taklit et (farklı değer)
                    real.execute("UPDATE trade_ledger SET closed_at_epoch=999 "
                                 "WHERE executor_id='E1'")
                return real.execute(sql, params)

            def close(self):
                real.close()

        res = risk_engine.reconcile_naive_closed_at(
            _RacingConn(), {"E1": {"close_timestamp": hb_ts}})
        self.assertEqual(res["skipped_raced"], 1, res)
        self.assertEqual(res["backfilled"], 0)
        self.assertEqual(self.row("E1")[1], 999, "eşzamanlı yazarın değeri ezildi")

    def test_old_schema_raises(self):
        conn = self.conn()
        conn.execute("DROP TABLE trade_ledger")
        conn.execute("CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY)")
        try:
            with self.assertRaises(ValueError):
                risk_engine.reconcile_naive_closed_at(conn, {})
        finally:
            conn.close()


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _HB:
    auth = ("u", "p")


class _FakeBinance:
    def __init__(self, positions):
        self._p = positions

    def position_risk(self):
        return self._p


class _FakeRisk:
    def __init__(self, db):
        self.db_file = db


class TestSyncWiring(Base):
    """Kanca sözleşmesi: backfill YALNIZ tamamlanma kanıtlı listede çalışır."""

    def setUp(self):
        super().setUp()
        conn = self.conn()
        conn.execute("CREATE TABLE sync_meta (id INTEGER PRIMARY KEY, "
                     "last_close_ts REAL, updated_at TEXT)")
        conn.execute("INSERT INTO sync_meta (id, last_close_ts) VALUES (1, 0)")
        conn.commit()
        conn.close()
        self._orig_post = at.requests.post
        self._orig_sl = getattr(at, "SL_LEDGER_DB", None)
        at.SL_LEDGER_DB = self.db
        self.addCleanup(setattr, at, "SL_LEDGER_DB", self._orig_sl)
        self.addCleanup(setattr, at.requests, "post", self._orig_post)
        # _ENTRY_HEALTH global: PARTIAL testi kasıtlı bloklar — test sonrası
        # geri yüklenir (sızıntı başka dosyanın giriş yolunu karartmasın;
        # desen: test_risk_engine_integrity.TestSyncIncompleteBlocksEntries).
        saved = dict(at._entry_health_reasons())
        with at._ENTRY_HEALTH_LOCK:
            at._ENTRY_HEALTH.clear()

        def _restore():
            with at._ENTRY_HEALTH_LOCK:
                at._ENTRY_HEALTH.clear()
                at._ENTRY_HEALTH.update(saved)

        self.addCleanup(_restore)
        self.hb_ts = 1794971514.188374
        naive = (datetime.fromtimestamp(self.hb_ts, timezone.utc)
                 + timedelta(seconds=10800)).replace(tzinfo=None).isoformat()
        self.naive = naive
        self.seed("E_NAIVE", naive)

    def _terminated(self):
        return [{"executor_id": "E_NAIVE", "status": "TERMINATED",
                 "close_timestamp": self.hb_ts, "closed_at": _hb_iso(self.hb_ts),
                 "net_pnl_quote": -0.5, "trading_pair": "PENGU-USDT"}]

    def _run_sync(self, search_body):
        at.requests.post = lambda url, json=None, **k: _Resp(search_body)
        at.sync_risk_engine(_FakeRisk(self.db), _HB(),
                            binance=_FakeBinance([]))

    def _enable_apply(self):
        # Operatör-opt-in: canlı yazım yalnız env bayrağıyla (offline reconciliation
        # modu varsayılan; kod incelemesi 2026-11-19).
        os.environ["FAZ17_CLOSED_AT_BACKFILL"] = "1"
        self.addCleanup(os.environ.pop, "FAZ17_CLOSED_AT_BACKFILL", None)

    def test_apply_flag_backfills_on_complete_list(self):
        self._enable_apply()
        rows = self._terminated()
        self._run_sync({"data": rows, "pagination": {
            "limit": 200, "has_more": False, "next_cursor": None,
            "total_count": len(rows)}})
        iso, epoch = self.row("E_NAIVE")
        self.assertAlmostEqual(epoch, self.hb_ts, 6,
                               msg="APPLY modunda tam listeda backfill çalışmadı")
        self.assertEqual(iso, self.naive, "raw bozuldu")

    def test_default_dry_run_writes_nothing(self):
        rows = self._terminated()
        self._run_sync({"data": rows, "pagination": {
            "limit": 200, "has_more": False, "next_cursor": None,
            "total_count": len(rows)}})
        self.assertIsNone(self.row("E_NAIVE")[1],
                          "varsayılan modda canlı ledger'a yazıldı (onaysız!)")

    def test_incomplete_list_never_touches(self):
        self._enable_apply()  # APPLY açık olsa bile PARTIAL'da YAZILMAZ
        rows = self._terminated()
        self._run_sync({"data": rows})  # pagination yok → PARTIAL
        self.assertIsNone(self.row("E_NAIVE")[1],
                          "PARTIAL sync naive satıra dokundu (fail-open!)")

    def _spy_reconcile(self):
        """Gerçek sync_risk_engine() çağrı yolunu gözle: kaç çağrı, hangi
        dry_run değeri, hangi sonuç döndü (P0: bayrak kontrolsiz yazılabilir
        çağrı YOLU OLMAMALI — tek çağrı, dry_run=not apply)."""
        orig = risk_engine.reconcile_naive_closed_at
        calls = []

        def spy(conn, hb_rows_by_eid, **kw):
            res = orig(conn, hb_rows_by_eid, **kw)
            calls.append({"kwargs": dict(kw), "result": dict(res)})
            return res

        self.addCleanup(setattr, risk_engine, "reconcile_naive_closed_at", orig)
        risk_engine.reconcile_naive_closed_at = spy
        return calls

    def test_real_sync_path_single_dry_run_call_by_default(self):
        # Env YOK (operatör onaysız varsayılan): gerçek sync → TEK çağrı,
        # dry_run=True, would_backfill raporlanır, DB'YE YAZILMAZ.
        calls = self._spy_reconcile()
        rows = self._terminated()
        self._run_sync({"data": rows, "pagination": {
            "limit": 200, "has_more": False, "next_cursor": None,
            "total_count": len(rows)}})
        self.assertEqual(len(calls), 1, f"tek çağrı sözleşmesi bozuldu: {calls}")
        self.assertIs(calls[0]["kwargs"].get("dry_run"), True,
                      "bayrak kapalıyken dry_run=False ile çağrıldı (yazım riski!)")
        self.assertEqual(calls[0]["result"].get("would_backfill"), 1,
                         "APPLY kapalıyken would_backfill raporlanmadı")
        self.assertEqual(calls[0]["result"].get("backfilled"), 0)
        self.assertIsNone(self.row("E_NAIVE")[1], "varsayılan mod canlı DB'ye yazdı")

    def test_real_sync_path_apply_flag_passes_dry_run_false(self):
        # Bayrak AÇIK: tek çağrı, dry_run=False, backfill gerçekleşir, raw durur.
        self._enable_apply()
        calls = self._spy_reconcile()
        rows = self._terminated()
        self._run_sync({"data": rows, "pagination": {
            "limit": 200, "has_more": False, "next_cursor": None,
            "total_count": len(rows)}})
        self.assertEqual(len(calls), 1, f"tek çağrı sözleşmesi bozuldu: {calls}")
        self.assertIs(calls[0]["kwargs"].get("dry_run"), False,
                      "APPLY açıkken dry_run=True geçti — backfill yapılmıyor")
        self.assertEqual(calls[0]["result"].get("backfilled"), 1)
        iso, epoch = self.row("E_NAIVE")
        self.assertAlmostEqual(epoch, self.hb_ts, 6)
        self.assertEqual(iso, self.naive, "raw closed_at bozuldu")


if __name__ == "__main__":
    unittest.main()
