"""FAZ2 — exchange-kaynaklı ledger writer + salt-okur mutabakat testleri.

Kapsanan sözleşmeler:
  A. build_close_events: pozisyon-delta kümeleri; net = Σrp + Σkomisyon
     (round-trip, giriş komisyonu dâhil); kapanmayan küme EVENT ÜRETMEZ
     (tail raporu); yön dönüşü yeni küme açar.
  B. assign_funding: pencere-içi FUNDING nete eklenir; sembolü boş /
     pencere-dışı funding ATANMAZ (uydurma atama yok), unassigned döner.
  C. write_events_to_ledger (GERÇEK RiskEngine, staging DB):
     source="binance_fill_verified"; net 0.00 DEĞİL (SYSTEM_CLEANUP gerçeği);
     affect_daily=False → risk_state sayacı DOKUNULMAZ; idempotent (ikinci
     yazım already-recorded, satır sayısı 1).
  D. reconcile: tutarlı veride bulgu YOK; Rp uyuşmazlığında R1 bulgusu;
     ledger-eksik sembol GAP olarak listelenir (ADA-tipo F21); GİRDİLER
     MUTASYONA UĞRAMAZ (salt-okur sözleşme).
  E. FillKeysetFetcher F22 sözleşmesi: fromId ASLA startTime/endTime ile
     birlikte GÖNDERİLMEZ; kısa sayfada durur; id dedupe; liste-olmayan
     sayfa → RuntimeError (fail-closed).
  F. loader'lar yalnız kendi dataset'ini okur (kaynak ayrımı disiplini).

Boundary: sahte HTTP yok — fetcher'e enjekte edilen sahte get; DB tmp staging.
Canlı DB/ağ/emir YOK.
"""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import exchange_ledger as XL
from risk_engine import RiskEngine

T0 = 1794902418.0
SYM = "SOLUSDT"

CFG = {
    "db_file": None,
    "max_consecutive_losses": 3,
    "max_trades_per_day": 100,
    "max_drawdown_pct": 0.5,
    "max_daily_loss_pct": 0.5,
    "risk_per_trade_pct": 0.01,
    "min_notional": 10,
    "max_notional": 1000,
}


def _fill(i, side, qty, rp=0.0, fee=0.0, ts=T0, sym=SYM, oid=None):
    return {"id": i, "orderId": oid or (70000000000 + i), "symbol": sym,
            "side": side, "qty": str(qty), "price": "100",
            "realizedPnl": str(rp), "commission": str(fee),
            "time": int(ts * 1000)}


class TestBuildCloseEvents(unittest.TestCase):
    # A — round-trip tek kümeye toplanır; net rp+komisyon (giriş komisyonu dâhil)
    def test_roundtrip_cluster(self):
        fills = [_fill(1, "BUY", 1.7, fee=0.096, ts=T0),
                 _fill(2, "BUY", 0.3, fee=0.010, ts=T0 + 1),
                 _fill(3, "SELL", 1.0, rp=-2.0, fee=0.05, ts=T0 + 100, oid=55),
                 _fill(4, "SELL", 1.0, rp=-2.07, fee=0.05, ts=T0 + 200, oid=56)]
        events, tails = XL.build_close_events(fills)
        self.assertEqual(tails, [])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertAlmostEqual(e["rp"], -4.07)
        self.assertAlmostEqual(e["commission"], 0.206)
        self.assertAlmostEqual(e["net"], -4.276)     # rp − komisyon (maliyet)
        self.assertEqual(e["closed_at_epoch"], T0 + 200)
        self.assertEqual(e["close_order_ids"], [55, 56])
        self.assertEqual(e["executor_id"], "fill-SOLUSDT-1")
        self.assertNotEqual(e["net"], 0.0)      # SYSTEM_CLEANUP 0.00'su YOK

    def test_two_clusters_deterministic_ids(self):
        fills = [_fill(1, "BUY", 1, ts=T0), _fill(2, "SELL", 1, rp=-1, ts=T0 + 10),
                 _fill(3, "BUY", 2, ts=T0 + 20), _fill(4, "SELL", 2, rp=+2, ts=T0 + 30)]
        events, tails = XL.build_close_events(fills)
        self.assertEqual(tails, [])
        self.assertEqual([e["executor_id"] for e in events],
                         ["fill-SOLUSDT-1", "fill-SOLUSDT-3"])

    # A — kapanmayan küme EVENT ÜRETMEZ (uydurma kapanış yok)
    def test_open_position_yields_tail_not_event(self):
        events, tails = XL.build_close_events([_fill(1, "BUY", 1)])
        self.assertEqual(events, [])
        self.assertEqual(len(tails), 1)
        self.assertEqual(tails[0]["unclosed_fill_ids"], [1])

    # A — yön dönüşü: SELL 2, pozisyonu sıfırlayıp YENİ short kümesi açar
    def test_flip_opens_new_cluster(self):
        events, tails = XL.build_close_events(
            [_fill(1, "BUY", 1, ts=T0),
             _fill(2, "SELL", 2, rp=-1, fee=0.3, ts=T0 + 10)])
        self.assertEqual(len(events), 1)        # long kümesi kapandı
        self.assertEqual(len(tails), 1)         # short kümesi AÇIK kaldı
        self.assertEqual(events[0]["fill_ids"], [1, 2])
        self.assertEqual(tails[0]["unclosed_fill_ids"], [2])
        # rp tamamıyla kapanan kümeye; komisyon miktar-oranlı (1/3 kapanan)
        self.assertAlmostEqual(events[0]["rp"], -1.0)
        self.assertAlmostEqual(events[0]["commission"], 0.15)  # 0.3 × 1/2 kapanan
        self.assertAlmostEqual(events[0]["net"], -1.15)        # −1.0 − 0.15


class TestFunding(unittest.TestCase):
    def _event(self):
        events, _ = XL.build_close_events(
            [_fill(1, "BUY", 1, ts=T0), _fill(2, "SELL", 1, rp=-4.0, ts=T0 + 100)])
        return events

    def test_window_inside_assigned_outside_not(self):
        ev = self._event()
        un = XL.assign_funding(ev, [
            {"symbol": SYM, "incomeType": "FUNDING_FEE", "income": "-0.5",
             "time": int((T0 + 50) * 1000)},      # GERÇEK incomeType, pencere içi
            {"symbol": SYM, "incomeType": "FUNDING", "income": "-0.25",
             "time": int((T0 + 60) * 1000)},      # API dokümanı etiketi de kabul
            {"symbol": SYM, "incomeType": "FUNDING_FEE", "income": "-9.0",
             "time": int((T0 + 500) * 1000)},     # pencere DIŞI
            {"symbol": "", "incomeType": "FUNDING_FEE", "income": "-7.0",
             "time": int((T0 + 50) * 1000)},      # sembol belirsiz
        ])
        self.assertAlmostEqual(ev[0]["funding"], -0.75)
        self.assertAlmostEqual(ev[0]["net"], -4.75)
        self.assertEqual(len(un), 2)              # uydurma atama YOK


class StagingHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        conn = sqlite3.connect(self.db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_ledger (
                executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL,
                closed_at TEXT, recorded_at TEXT, source TEXT DEFAULT 'sync')
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY CHECK (id=1),
                peak_equity REAL NOT NULL DEFAULT 0,
                consecutive_losses INTEGER NOT NULL DEFAULT 0,
                daily_trades INTEGER NOT NULL DEFAULT 0,
                daily_pnl REAL NOT NULL DEFAULT 0,
                last_reset_date TEXT NOT NULL,
                is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT,
                updated_at TEXT NOT NULL)
        """)
        conn.execute("INSERT OR IGNORE INTO risk_state "
                     "(id, last_reset_date, updated_at) VALUES (1, '2020-01-01', 'init')")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_meta (
                id INTEGER PRIMARY KEY CHECK (id=1),
                last_close_ts REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)
        """)
        conn.execute("INSERT OR IGNORE INTO sync_meta "
                     "(id, last_close_ts, updated_at) VALUES (1, 0, 'init')")
        conn.commit()
        conn.close()
        import risk_migration
        risk_migration.apply(self.db, do_backup=True)
        self.cfg = dict(CFG, db_file=self.db)
        self.engine = RiskEngine(self.cfg)

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT executor_id, trading_pair, net_pnl_quote,"
                                " source FROM trade_ledger").fetchall()
        finally:
            conn.close()

    def risk_state(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT daily_pnl, consecutive_losses, is_halted"
                                " FROM risk_state WHERE id=1").fetchone()
        finally:
            conn.close()


class TestStagingWriter(StagingHarness):
    # C — gerçek record_trade yolu; provenance + gerçek net + sayaç dokunmaz
    def test_write_provenance_and_idempotent(self):
        events, _ = XL.build_close_events(
            [_fill(1, "BUY", 1, fee=0.1, ts=T0),
             _fill(2, "SELL", 1, rp=-4.07, fee=0.1, ts=T0 + 100)])
        out = XL.write_events_to_ledger(self.engine, events)
        self.assertEqual(out["inserted"], 1)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], SYM)
        self.assertAlmostEqual(rows[0][2], -4.27)      # rp−komisyon, 0.00 DEĞİL
        self.assertEqual(rows[0][3], XL.SOURCE_FILL_VERIFIED)
        self.assertEqual(self.risk_state(), (0.0, 0, 0))   # sayaçlar DOKUNULMADI
        out2 = XL.write_events_to_ledger(self.engine, events)
        self.assertEqual(out2, {"inserted": 0, "duplicate": 1, "error": []})
        self.assertEqual(len(self.rows()), 1)             # çift satır YOK


class TestReconcile(unittest.TestCase):
    def _data(self):
        fills = [_fill(1, "BUY", 1, fee=0.1, ts=T0),
                 _fill(2, "SELL", 1, rp=-4.07, fee=0.1, ts=T0 + 100)]
        income = [
            {"symbol": SYM, "incomeType": "REALIZED_PNL", "income": "-4.07",
             "time": int((T0 + 100) * 1000)},
            {"symbol": "", "incomeType": "COMMISSION", "income": "-0.2",
             "time": int((T0 + 100) * 1000)},
        ]
        ledger = [{"trading_pair": "SOL-USDT", "net_pnl_quote": -4.27}]
        return fills, income, ledger

    def test_consistent_no_findings(self):
        fills, income, ledger = self._data()
        rep = XL.reconcile(fills, income, ledger)
        self.assertEqual(rep["findings"], [])
        self.assertAlmostEqual(rep["totals"]["fill_rp"], -4.07)
        self.assertAlmostEqual(rep["totals"]["fill_commission"], 0.2)

    def test_income_mismatch_flagged(self):
        fills, income, ledger = self._data()
        income[0]["income"] = "-0.07"            # F21-tipo eksik yazım
        rep = XL.reconcile(fills, income, ledger)
        self.assertEqual([f["check"] for f in rep["findings"]],
                         ["R1 rp fill≡income"])
        self.assertAlmostEqual(rep["findings"][0]["delta"], -4.0)

    # D — ledger'da HİÇ olmayan sembol GAP olarak listelenir (ZORLA EŞLEŞTİRME YOK)
    def test_missing_ledger_row_surfaces_as_gap(self):
        fills, income, _ = self._data()
        rep = XL.reconcile(fills, income, [])
        gap = rep["ledger_vs_exchange_gaps"]
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0]["symbol"], "SOLUSDT")
        self.assertAlmostEqual(gap[0]["delta"], -4.27)

    def test_reconcile_never_mutates_inputs(self):
        fills, income, ledger = self._data()
        snap = copy.deepcopy((fills, income, ledger))
        XL.reconcile(fills, income, ledger)
        self.assertEqual((fills, income, ledger), snap)


class TestKeysetFetcher(unittest.TestCase):
    # E — F22 sözleşmesi: fromId + startTime/endTime BİRLİKTE YOK
    def test_contract_pages_dedupe_stop(self):
        pages = [
            [_fill(i, "BUY", 1) for i in range(1, 4)],      # tam sayfa (limit 3)
            [_fill(3, "BUY", 1), _fill(4, "BUY", 1)],       # mükerrer 4 + yeni
        ]

        def fake_get(path, params):
            return pages.pop(0) if pages else []

        f = XL.FillKeysetFetcher(fake_get)
        out = f.fetch_symbol(SYM, start_id=1, limit=3)
        self.assertEqual([r["id"] for r in out], [1, 2, 3, 4])   # dedupe + sıra
        for req in f.requests:
            self.assertIn("fromId", req)
            self.assertNotIn("startTime", req)
            self.assertNotIn("endTime", req)
            self.assertEqual(set(req), {"symbol", "fromId", "limit"})

    def test_non_list_page_fails_closed(self):
        f = XL.FillKeysetFetcher(lambda p, pr: {"code": -1121})
        with self.assertRaises(RuntimeError):
            f.fetch_symbol(SYM, limit=10)


class TestLoaders(unittest.TestCase):
    # F — kaynak ayrımı: loader yalnız kendi dataset'ini okur
    def test_loader_dataset_filtering(self):
        path = os.path.join(tempfile.mkdtemp(), "coll.ndjson")
        with open(path, "w") as fh:
            fh.write(json.dumps({"dataset": "userTrades_fromid_XAUUSDT",
                                 "rows": [_fill(9, "BUY", 1, sym="XAUUSDT")]}) + "\n")
            fh.write(json.dumps({"dataset": "allOrders_XAUUSDT_90d",
                                 "rows": [{"orderId": 1}]}) + "\n")
            fh.write(json.dumps({"dataset": "income_90d",
                                 "rows": [{"symbol": "XAUUSDT",
                                           "incomeType": "REALIZED_PNL",
                                           "income": "1.5", "time": 1}]}) + "\n")
        self.assertEqual([r["id"] for r in XL.load_fill_rows(path)], [9])
        self.assertEqual(len(XL.load_income_rows(path)), 1)


class TestExchangeDayPnl(unittest.TestCase):
    # 2.3 — gate gün-neti YALNIZ exchange verisinden
    def test_day_net_from_events_and_funding(self):
        events, _ = XL.build_close_events(
            [_fill(1, "BUY", 1, fee=0.1, ts=T0),
             _fill(2, "SELL", 1, rp=-4.07, fee=0.1, ts=T0 + 100)])
        day = XL._utc_day(T0 + 100)
        income = [{"symbol": SYM, "incomeType": "FUNDING_FEE", "income": "-0.5",
                   "time": int((T0 + 100) * 1000)}]
        tot = XL.exchange_day_pnl(day, events, income)
        self.assertEqual(tot["closed_events"], 1)
        self.assertAlmostEqual(tot["net"], -4.07 - 0.2 - 0.5)
        other = XL.exchange_day_pnl("2000-01-01", events, income)
        self.assertEqual(other["net"], 0.0)


class TestReconcileCLI(unittest.TestCase):
    def _files(self, income_rp="-4.07"):
        d = tempfile.mkdtemp()
        fp, ip, lp = (os.path.join(d, n) for n in ("f.ndjson", "i.ndjson",
                                                   "l.ndjson"))
        with open(fp, "w") as fh:
            fh.write(json.dumps({"dataset": "userTrades_fromid_SOLUSDT",
                                 "rows": [_fill(1, "BUY", 1, fee=0.1),
                                          _fill(2, "SELL", 1, rp=-4.07,
                                                fee=0.1, ts=T0 + 100)]}) + "\n")
        with open(ip, "w") as fh:
            fh.write(json.dumps({"dataset": "income_90d", "rows": [
                {"symbol": SYM, "incomeType": "REALIZED_PNL",
                 "income": income_rp, "time": int((T0 + 100) * 1000)},
                {"symbol": "", "incomeType": "COMMISSION", "income": "-0.2",
                 "time": int((T0 + 100) * 1000)}]}) + "\n")
        with open(lp, "w") as fh:
            fh.write(json.dumps({"dataset": "ledger_trade_rows", "rows": [
                {"trading_pair": "SOL-USDT", "net_pnl_quote": -4.27}]}) + "\n")
        return fp, ip, lp

    def test_cli_green_then_diff_exit_codes(self):
        fp, ip, lp = self._files()
        self.assertEqual(XL.main(["--fills", fp, "--income", ip,
                                  "--ledger", lp]), 0)
        fp, ip, lp = self._files(income_rp="-4.00")   # F21-tipo 0.07 fark > tol
        self.assertEqual(XL.main(["--fills", fp, "--income", ip,
                                  "--ledger", lp]), 1)


if __name__ == "__main__":
    unittest.main()
