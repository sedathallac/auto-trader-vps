"""FAZ14x — sync_risk_engine muhasebe bütünlüğü regresyonları (yalnız hedefli).

Kanıtlanan kusurlar (kaynak: auto_trader.py sync_risk_engine @1c9f904):
  1. Global watermark KAYBI: `closed_at <= last_ts` atlaması K2 kapısından ve
     ledger-varlık kontrolünden ÖNCE çalışıyor; daha yeni bir kapanış watermark'ı
     ilerletince K2 yüzünden ertelenen ESKİ TERMINATED kapanış kalıcı kayboluyor.
  2. HB `/executors/search` hata cevabı (HTTP hatası / hata JSON / data'sız
     gövde) `r.get("data", [])` ile "0 executor" gibi yutuluyordu (fail-OPEN).
  3. TERMINATED + `net_pnl_quote` 0/eksik kayıtlar doğrulanmadan 0 PnL yazılıyordu
     (live ledger'daki 15 sıfır kaydın aday kök nedeni).
  4. Normal TERMINATED yolu `affect_daily` gün kontrolü yapmıyordu; geçmiş tarihli
     kapanış bugünüm günlük PnL/ardışık-zarar sayacını kirletiyordu (stale yolu
     yapıyor, normal yolu yapmıyor — iki farklı muhasebe standardı).
  5. Provenance: her kayıt source='sync' yazılıyor; hb_terminated ile
     binance fill-kanıtı ayrımı kayboluyordu.

Testler deterministiktir; canlı API'ye dokunmaz.
"""
import os
import sys
import sqlite3
import tempfile
import shutil
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeHB:
    auth = ("u", "p")


class _FakeRisk:
    """record_trade sözleşmesini taklit eder: ledger'a INSERT + kw kaydı."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = []

    def record_trade(self, eid, pair, pnl, iso=None, **kw):
        self.calls.append({"eid": eid, "pair": pair, "pnl": pnl, "iso": iso, **kw})
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT INTO trade_ledger (executor_id) VALUES (?)", (eid,))
            conn.commit()
        finally:
            conn.close()
        return True, "ok"


class _FakeBinance:
    def __init__(self, positions_fn):
        self._fn = positions_fn

    def position_risk(self):
        return self._fn()


class SyncHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE sync_meta (id INTEGER PRIMARY KEY, last_close_ts REAL, updated_at TEXT)")
        conn.execute("INSERT INTO sync_meta (id, last_close_ts) VALUES (1, 0)")
        conn.execute("CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        self._orig_home = at.STATE_DIR
        at.STATE_DIR = Path(self.tmp)
        self._orig_post = at.requests.post
        self.search_payload = {"data": []}
        self.search_status = 200
        self.positions = []
        at.requests.post = self._post
        self.risk = _FakeRisk(self.db)
        self._orig_recover = at._recover_stale_executor_net_pnl
        self._orig_sl_db = getattr(at, "SL_LEDGER_DB", None)
        at.SL_LEDGER_DB = self.db  # sync içi SL-yaşam-hane yardımcısı tmp DB'ye bağlansın

    def tearDown(self):
        at.STATE_DIR = self._orig_home
        at.requests.post = self._orig_post
        at._recover_stale_executor_net_pnl = self._orig_recover
        if self._orig_sl_db is not None:
            at.SL_LEDGER_DB = self._orig_sl_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, url, json=None, **k):
        # FAZ17 gerçek HB sözleşmesi (canlı probe 2026-11-19): offset YOK SAYILIR;
        # tamamlanma yalnız pagination={has_more:False,total_count} ile kanıtlanır.
        # Fixture tek-sayfa-tam gerçeğini temsil eder: her istekte aynı sayfa +
        # dürüst pagination. Hata biçimleri (data liste değil / status≠200)
        # istemcide pagination'dan ÖNCE yakalanır — assertler aynen korunur.
        body = dict(self.search_payload)
        data = body.get("data")
        if isinstance(data, list):
            body["pagination"] = {"limit": max(1, len(data)), "has_more": False,
                                  "next_cursor": None, "total_count": len(data)}
        return _Resp(body, self.search_status)

    def run_sync(self):
        # Fixture uyarlamasi: 120sn recovery-throttle testler arasinda sifirlanir
        # (test hedefi throttle degil, kaydin kanit-standardi ve kaybolmamasi).
        at.sync_risk_engine._stale_retry = {}
        at.sync_risk_engine(self.risk, _FakeHB(), binance=_FakeBinance(lambda: self.positions))

    def wm(self):
        conn = sqlite3.connect(self.db)
        try:
            return float(conn.execute("SELECT last_close_ts FROM sync_meta WHERE id=1").fetchone()[0])
        finally:
            conn.close()

    def eids(self):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0] for r in conn.execute("SELECT executor_id FROM trade_ledger")}
        finally:
            conn.close()


def term(eid, pair, ts, pnl=0.5):
    return {"executor_id": eid, "status": "TERMINATED", "close_timestamp": ts,
            "net_pnl_quote": pnl, "trading_pair": pair,
            "custom_info": json_info(eid)}


def json_info(eid):
    import json
    return json.dumps({"config": {"entry_side": {"creation_time": {"client_order_id": f"x-nb-{eid}"}}}})


class TestWatermarkLoss(SyncHarness):
    """KUSUR 1 — K2 ertelemesi watermark'a kurban gitmemeli."""

    def test_deferred_close_survives_newer_watermark(self):
        old_ts = 1_600_000_000
        new_ts = 1_600_001_000
        self.search_payload = {"data": [
            term("E_OLD", "BTC-USDT", old_ts, pnl=-0.5),
            term("E_NEW", "ETH-USDT", new_ts, pnl=-0.7),
        ]}
        # Tur 1: BTC hâlâ açık (yeni dönem), ETH flat -> yalnız E_NEW yazılır.
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0.1"},
                          {"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.run_sync()
        self.assertEqual(self.eids(), {"E_NEW"},
                         "E_NEW kaydedilemedi (temel akış bozuk)")
        self.assertNotIn("E_OLD", self.eids(), "K2 açık sembolde yazıldı (fail-open!)")

        # Tur 2: BTC dönemi gerçekten kapandı. Eski kod: watermark (new_ts)
        # E_OLD'un closed_at'ını geçtiği için E_OLD KALICI KAYBOLUYOR.
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"},
                          {"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.run_sync()
        self.assertIn("E_OLD", self.eids(),
                      "P1 KAPANIŞ KAYBI: K2 ertelemesi global watermark tarafından yutuldu")


class TestHbSearchFailClosed(SyncHarness):
    """KUSUR 2 — HB arama hatası '0 executor' sayılmamalı."""

    def _seed_one_terminable(self):
        self.search_payload = {"data": [term("E1", "ETH-USDT", 1_600_000_000, pnl=-0.3)]}
        self.positions = [{"symbol": "ETHUSDT", "positionAmt": "0"}]

    def test_http_error_does_not_record_or_advance(self):
        self._seed_one_terminable()
        self.search_status = 500
        self.run_sync()
        self.assertEqual(self.eids(), set(), "HTTP hatasına rağmen kayıt yazıldı")
        self.assertEqual(self.wm(), 0.0, "HTTP hatasına rağmen watermark ilerledi")

    def test_error_json_body_does_not_swallow(self):
        self._seed_one_terminable()
        self.search_payload = {"code": -1002, "msg": "Unauthorized"}
        self.run_sync()
        self.assertEqual(self.eids(), set(), "hata JSON'u '0 executor' sayıldı")

    def test_nonlist_data_is_rejected(self):
        self._seed_one_terminable()
        self.search_payload = {"data": {"unexpected": "shape"}}
        self.run_sync()
        self.assertEqual(self.eids(), set(), "data list değilken devam edildi")


class TestPnlStandard(SyncHarness):
    """KUSUR 3/4/5 — tek kanonik standart + provenance + gün kapısı."""

    def test_zero_hb_pnl_requires_fill_evidence_and_marks_source(self):
        e = term("EZ", "XRP-USDT", 1_600_000_000, pnl=0)
        e["net_pnl_quote"] = None  # HB muhasebesi hiç üretmemiş
        self.search_payload = {"data": [e]}
        self.positions = [{"symbol": "XRPUSDT", "positionAmt": "0"}]

        calls = {"n": 0}

        def _recover(executor, binance):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("fill kanıtı henüz eksik")
            return -1.25, 1_600_000_000

        at._recover_stale_executor_net_pnl = _recover

        # Tur 1: kanıt yok -> YAZILMAZ (0 PnL uydurulmaz), ertelenir.
        self.run_sync()
        self.assertEqual(self.eids(), set(), "kanıtsız 0-PnL kaydı yazıldı")

        # Tur 2: kanıt üretildi -> gerçek PnL, doğru provenance ile yazılmalı.
        self.run_sync()
        self.assertIn("EZ", self.eids(), "fill-kanıtlı kapanış hâlâ ledger'da yok")
        rec = next(c for c in self.risk.calls if c["eid"] == "EZ")
        self.assertAlmostEqual(rec["pnl"], -1.25, places=8)
        self.assertEqual(rec.get("source"), "binance_fill_verified")

    def test_normal_hb_pnl_marks_hb_source(self):
        self.search_payload = {"data": [term("EH", "ETH-USDT", int(time.time()) - 60, pnl=-0.4)]}
        self.positions = [{"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.run_sync()
        rec = next(c for c in self.risk.calls if c["eid"] == "EH")
        self.assertEqual(rec.get("source"), "hb_terminated")

    def test_historical_close_does_not_touch_daily(self):
        old_ts = time.time() - 2 * 86400  # 2 gün önce kapandı
        self.search_payload = {"data": [term("EO", "ETH-USDT", int(old_ts), pnl=-0.9)]}
        self.positions = [{"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.run_sync()
        rec = next(c for c in self.risk.calls if c["eid"] == "EO")
        self.assertFalse(rec.get("affect_daily", True),
                         "geçmiş kapanış bugünün günlük sayaçlarına işlendi")

    def test_today_close_affects_daily(self):
        self.search_payload = {"data": [term("ET", "ETH-USDT", int(time.time()) - 30, pnl=-0.9)]}
        self.positions = [{"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.run_sync()
        rec = next(c for c in self.risk.calls if c["eid"] == "ET")
        self.assertTrue(rec.get("affect_daily", False))


class TestFlatEvidenceContract(unittest.TestCase):
    """FAZ14x-2-A — katı flat-kanıtı sözleşmesi (birim düzeyi, deterministik).

    Zorunlu durumlar: eksik alan / None / boş string / hatalı string / NaN /
    Infinity -> False; açık miktar -> False; açıkça 0 -> True; sembol yok ->
    False; çelişkili çoklu kayıt -> False. Boolean miktar sayısal sayılmaz.
    """

    def _rec(self, amt):
        return {"symbol": "BTCUSDT", "positionAmt": amt}

    def test_list_not_positions(self):
        self.assertFalse(at.is_flat_confirmed(None, "BTCUSDT"))
        self.assertFalse(at.is_flat_confirmed({"code": -1130}, "BTCUSDT"))

    def test_symbol_absent_is_never_flat(self):
        self.assertFalse(at.is_flat_confirmed([self._rec("0")], "ETHUSDT"))

    def test_missing_positionAmt_field(self):
        self.assertFalse(at.is_flat_confirmed([{"symbol": "BTCUSDT"}], "BTCUSDT"),
                         "positionAmt alanı yokken flat dendi")

    def test_none_amount(self):
        self.assertFalse(at.is_flat_confirmed([self._rec(None)], "BTCUSDT"))

    def test_empty_string_amount(self):
        self.assertFalse(at.is_flat_confirmed([self._rec("")], "BTCUSDT"))
        self.assertFalse(at.is_flat_confirmed([self._rec("   ")], "BTCUSDT"))

    def test_bad_string_amount(self):
        self.assertFalse(at.is_flat_confirmed([self._rec("abc")], "BTCUSDT"))

    def test_nan_amount(self):
        self.assertFalse(at.is_flat_confirmed([self._rec("nan")], "BTCUSDT"),
                         "NaN string flat sayıldı")
        self.assertFalse(at.is_flat_confirmed([self._rec(float("nan"))], "BTCUSDT"),
                         "NaN float flat sayıldı")

    def test_infinity_amount(self):
        self.assertFalse(at.is_flat_confirmed([self._rec("inf")], "BTCUSDT"))
        self.assertFalse(at.is_flat_confirmed([self._rec(float("inf"))], "BTCUSDT"))
        self.assertFalse(at.is_flat_confirmed([self._rec(float("-inf"))], "BTCUSDT"))

    def test_open_amount(self):
        self.assertFalse(at.is_flat_confirmed([self._rec("-0.3")], "BTCUSDT"))
        self.assertFalse(at.is_flat_confirmed([self._rec("0.1")], "BTCUSDT"))

    def test_explicit_zero_is_flat(self):
        self.assertTrue(at.is_flat_confirmed([self._rec("0")], "BTCUSDT"))
        self.assertTrue(at.is_flat_confirmed([self._rec("0.00")], "BTCUSDT"))
        self.assertTrue(at.is_flat_confirmed([self._rec(0)], "BTCUSDT"))

    def test_bool_amount_not_numeric(self):
        # hayati nokta: False -> float 0.0 -> flat TUZAĞI.
        self.assertFalse(at.is_flat_confirmed([self._rec(False)], "BTCUSDT"),
                         "boolean False miktar flat sayıldı")
        self.assertFalse(at.is_flat_confirmed([self._rec(True)], "BTCUSDT"))

    def test_duplicate_conflicting_records_ambiguous(self):
        recs = [{"symbol": "BTCUSDT", "positionAmt": "0"},
                {"symbol": "BTCUSDT", "positionAmt": "-5"}]
        self.assertFalse(at.is_flat_confirmed(recs, "BTCUSDT"),
                         "çelişkili çoklu kayıt flat sayıldı")

    def test_duplicate_records_even_identical_are_ambiguous(self):
        # Sözleşme 'açık ve TEKİL kayıt' ister: yinelenen kayıt kanıt değil.
        recs = [self._rec("0"), self._rec("0")]
        self.assertFalse(at.is_flat_confirmed(recs, "BTCUSDT"))

    def test_non_dict_rows_ignored_not_crash(self):
        recs = ["garbage", None, 42, self._rec("0")]
        self.assertTrue(at.is_flat_confirmed(recs, "BTCUSDT"))


class TestSweepMissingAmt(SyncHarness):
    """Sweep düzeyi: positionAmt alanı eksik/bozuk kayıt PLACED epoch'u
    kapatmamalı (birim + entegrasyon aynı standardı görmeli)."""

    def _seed(self, symbol="BTCUSDT", token="LONG:100"):
        from sl_placement import SlPlacementStore
        store = SlPlacementStore(self.db)
        store.init_schema()
        store.acquire(symbol, token)
        store.mark_placed(symbol, token)
        return store

    def test_missing_amt_field_spared(self):
        store = self._seed()
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT"}]  # positionAmt YOK
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "positionAmt eksik kayıtla epoch CLOSED edildi")

    def test_bool_false_amt_spared(self):
        store = self._seed()
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": False}]
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "boolean False snapshot ile epoch CLOSED edildi")


class TestOldSnapshotVsNewEpoch(SyncHarness):
    """B-4/B-6 deterministik yarış kilidi (sync düzeyi): sweep aday listesi
    okunduktan SONRA yeni epoch oluşursa eski snapshot onu kapayamaz."""

    def test_stale_snapshot_after_new_epoch_noop(self):
        from sl_placement import SlPlacementStore
        store = SlPlacementStore(self.db)
        store.init_schema()
        store.acquire("BTCUSDT", "LONG:100")
        store.mark_placed("BTCUSDT", "LONG:100")
        # Snapshot T1 döneminde alındı (flat).
        stale = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.assertTrue(at.is_flat_confirmed(stale, "BTCUSDT"))
        self.search_payload = {"data": []}
        self.positions = stale
        self.run_sync()  # T1 CLOSED
        self.assertEqual(store.get("BTCUSDT")["state"], "CLOSED")
        # Aynı (bayat) snapshot ile YENİ epoch: farklı token -> sweep'in
        # mark_closed_flat'ı T1 token'ı ile koşullu; T2 kendi token'ında
        # AÇIK pozisyon varsayılarak korunur (flat değilse kimse kapatmaz).
        store.acquire("BTCUSDT", "SHORT:95")  # yeni dönem PENDING
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "-0.4"}]  # açık
        self.run_sync()
        row = store.get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PENDING", "SHORT:95"),
                         "eski snapshot mantığı yeni epoch'u bozdu")


class TestSlFlatSweep(SyncHarness):
    """FAZ14x-2 — geçmiş yetim PLACED satırları reported_ids'ten bağımsız
    exchange-flat kanıtıyla CLOSED olmalı; AÇIK pozisyon korunmalı."""

    def _seed_store(self, symbol, token):
        from sl_placement import SlPlacementStore
        store = SlPlacementStore(self.db)
        store.init_schema()
        store.acquire(symbol, token)
        store.mark_placed(symbol, token)
        return store

    def test_orphan_placed_closes_despite_reported_ids_skip(self):
        store = self._seed_store("BTCUSDT", "LONG:100")
        # Eski kapanış ZATEN ledger'da: executor akışı `reported_ids` yüzünden
        # atlar — sweep yine de kapatmalı (bulgu 3'un çekirdek senaryosu).
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO trade_ledger (executor_id) VALUES ('OLD-EID')")
        conn.commit()
        conn.close()
        self.search_payload = {"data": [term("OLD-EID", "BTC-USDT", 1_600_000_000, pnl=-0.1)]}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"},
                          {"symbol": "FILUSDT", "positionAmt": "-326.6"}]
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "CLOSED",
                         "yetim PLACED, flat kanıtına rağmen CLOSED olmadı")
        self.assertEqual(self.eids(), {"OLD-EID"}, "ledger değişmemeliydi (skip sürer)")

    def test_open_position_epoch_spared(self):
        store = self._seed_store("FILUSDT", "SHORT:2")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "FILUSDT", "positionAmt": "-326.6"}]
        self.run_sync()
        self.assertEqual(store.get("FILUSDT")["state"], "PLACED",
                         "AÇık pozisyonun SL epoch'u kapatıldı (KORUMA İHLALİ)")

    def test_sweep_fail_closed_on_bad_snapshot(self):
        store = self._seed_store("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = {"code": -1130, "msg": "positionRisk down"}  # dict = bozuk
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "bozuk snapshot'ta kayıt kapatıldı (fail-open)")

    def test_symbol_absent_from_snapshot_not_closed(self):
        store = self._seed_store("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "ETHUSDT", "positionAmt": "0"}]  # BTC YOK
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "sembol listede yokken flat sayıldı")

    def test_empty_snapshot_closes_nothing(self):
        # Canlı sözleşme: positionRisk hiç boş liste dönmez (kapalı semboller
        # amt=0 ile gelir — 08:01-08:03 kapanışlarının canlı yazılma kanıtı).
        # Boş liste = sözleşme ihlali/mock → fail-closed: hiçbir şey kapanmaz.
        store = self._seed_store("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = []
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "boş snapshot'ta kayıt kapatıldı")

    def test_pending_epoch_never_swept(self):
        # PENDING = sahibinin yerleştirmesi sürüyor; flat snapshot bayat
        # olabilir → sweep PENDING'e dokunmaz (yarış sıkılaştırması).
        from sl_placement import SlPlacementStore
        store = SlPlacementStore(self.db)
        store.init_schema()
        store.acquire("BTCUSDT", "LONG:100")  # PENDING (placed değil)
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PENDING",
                         "sweep devam eden yerleştirmeyi bozdu")

    def test_sweep_idempotent_across_ticks(self):
        store = self._seed_store("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        first = store.get("BTCUSDT")["state"]
        self.run_sync()
        second = store.get("BTCUSDT")
        self.assertEqual((first, second["state"]), ("CLOSED", "CLOSED"),
                         "sweep idempotent değil")

    def test_isolation_ledger_db_redirected_to_tmp(self):
        # Kanıt: sweep'in açtığı SL_LEDGER_DB bu testin tmp dosyasıdır;
        # import-zamanı varsayılanına (gerçek home/state DB) dokunulmaz.
        import tempfile
        self.assertEqual(str(at.SL_LEDGER_DB), self.db)
        self.assertTrue(str(at.SL_LEDGER_DB).startswith(tempfile.gettempdir()),
                        "SL_LEDGER_DB tmp dizini dışında — izolasyon ihlali")
        store = self._seed_store("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        conn = sqlite3.connect(self.db)
        try:
            st = conn.execute("SELECT state FROM sl_placements WHERE symbol='BTCUSDT'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(st, "CLOSED", "CLOSED tmp DB'ye yazılmadı")


if __name__ == "__main__":
    unittest.main()


class TestSnapshotEpochRace(SyncHarness):
    """FAZ14x-2 BULGU 1/2 — snapshot↔SL-epoch yarışı (gerçek interleave).

    Hook, position_risk() çağrısı İÇİNDE (snapshot alındıktan hemen sonra,
    sweep list_active'ı çağırmadan ÖNCE) çalışır: T1 snapshot → T2 acquire →
    T3 mark_placed → T4 list_active → T5 bayat snapshot uygulanır. Doğrulama
    mock çağrı sayısıyla değil, gerçek SQLite satır durumuyla yapılır.
    """

    class _RacyBinance:
        def __init__(self, owner):
            self._owner = owner

        def position_risk(self):
            snap = self._owner.positions  # T1: snapshot (bayat kalacak)
            hook = self._owner.race_hook
            if hook:
                self._owner.race_hook = None
                hook()  # T2/T3: snapshot SONRASI epoch hareketi
            return snap

    def _store(self):
        from sl_placement import SlPlacementStore
        store = SlPlacementStore(self.db)
        store.init_schema()
        return store

    def _seed_placed(self, symbol, token):
        store = self._store()
        store.acquire(symbol, token)
        store.mark_placed(symbol, token)
        return store

    def setUp(self):
        super().setUp()
        self.race_hook = None

    def _run_racy(self):
        at.sync_risk_engine._stale_retry = {}
        at.sync_risk_engine(self.risk, _FakeHB(),
                            binance=self._RacyBinance(self))

    # A — snapshot sonrası, list_active ÖNCESİ yeni epoch acquire+PLACED.
    # Eski (kusurlu) davranış: bayat flat snapshot yeni PLACED epoch'u CLOSED
    # eder (token/sıra koşulu yok). Beklenen güvenli son durum: PLACED korunur.
    def test_A_epoch_placed_between_snapshot_and_list_active_survives(self):
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        def hook():
            from sl_placement import ACQUIRED
            store = self._store()
            assert store.acquire("BTCUSDT", "LONG:100") == ACQUIRED
            store.mark_placed("BTCUSDT", "LONG:100")

        self.race_hook = hook
        self._run_racy()
        row = self._store().get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PLACED", "LONG:100"),
                         "snapshot'tan SONRA doğan PLACED epoch, bayat flat "
                         "snapshot ile CLOSED edildi (BULGU 1)")

    # B — snapshot sonrası eski epoch yeni epoch tarafından supersede edilir
    # (yeni token, PENDING). Bayat snapshot hiçbir durumda onu bozamamalı.
    def test_B_superseded_epoch_after_snapshot_survives(self):
        store = self._seed_placed("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.race_hook = lambda: store.acquire("BTCUSDT", "SHORT:95")
        self._run_racy()
        row = store.get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PENDING", "SHORT:95"),
                         "supersede edilmiş yeni epoch bayat snapshot'la bozuldu")

    # C — yeni epoch AYNI symbol+side+entryPrice token'ını taşıyor (token
    # eşleşmesi yetmez kanıtı). Eski davranış: token eşleştiği için bayat
    # snapshot yeni dönemi kapatır. Beklenen: PLACED korunur.
    def test_C_same_token_revival_not_closed_by_stale_snapshot(self):
        store = self._seed_placed("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()  # meşru kapanış: eski dönem CLOSED olur
        self.assertEqual(store.get("BTCUSDT")["state"], "CLOSED")

        def hook():
            from sl_placement import ACQUIRED
            assert store.acquire("BTCUSDT", "LONG:100") == ACQUIRED  # aynı token
            store.mark_placed("BTCUSDT", "LONG:100")  # PLACED, güncel updated_at

        self.race_hook = hook
        self._run_racy()  # aynı (artık bayat) flat snapshot tekrar uygulanıyor
        row = store.get("BTCUSDT")
        self.assertEqual(row["state"], "PLACED",
                         "aynı token'lı YENİ dönem bayat snapshot ile CLOSED "
                         "edildi — symbol+side+entryPrice eşleşmesi yeterli değil")

    # D1 — BULGU 2: executor-akışı helper'ı GÜNCEL satırı yeniden okuyup
    # token'ını kendisi alıyor; kapanan executor'la ilinti yok. Eski
    # davranış: ilintisiz güncel PLACED epoch'u kapattı. Beklenen: korunur.
    def test_D1_executor_helper_cannot_close_unrelated_current_epoch(self):
        store = self._seed_placed("BTCUSDT", "LONG:999")
        at._mark_sl_epoch_closed("BTCUSDT")
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "helper, kapanan executor'la ilintisi kanıtlanmamış "
                         "güncel epoch'u CLOSED etti (BULGU 2)")

    # D2 — aynı bulgunun sync-ucu: TERMINATED executor kapanışı, snapshot
    # sonrası doğmuş yeni PLACED epoch'u kapatmamalı; kapanış yine kaydedilmeli.
    def test_D2_executor_close_preserves_new_placed_epoch(self):
        self.search_payload = {"data": [term("OLDX", "BTC-USDT", 1_600_000_000, pnl=-0.2)]}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        def hook():
            store = self._store()
            store.acquire("BTCUSDT", "LONG:100")
            store.mark_placed("BTCUSDT", "LONG:100")

        self.race_hook = hook
        self._run_racy()
        self.assertIn("OLDX", self.eids(), "executor kapanışı kaydedilmeliydi")
        row = self._store().get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PLACED", "LONG:100"),
                         "eski executor kapanışı yeni PLACED epoch'u kapattı")

    # E — snapshot ve DB güncellemesi ARASINDA (list_active OKUNDUKTAN SONRA)
    # yeni epoch açılır: token koşulu bayat güncellemeyi 0 satırda bırakmalı.
    def test_E_epoch_created_between_list_active_and_update(self):
        import sl_placement as sp
        store = self._seed_placed("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        _orig = sp.SlPlacementStore.list_active
        seen = {"n": 0}

        def racy(self_):
            rows = _orig(self_)
            seen["n"] += 1
            if seen["n"] == 1:  # aday listesi okundu; TAM O ANDA yeni sahiplenici
                store.acquire("BTCUSDT", "SHORT:95")
            return rows

        sp.SlPlacementStore.list_active = racy
        try:
            self.run_sync()
        finally:
            sp.SlPlacementStore.list_active = _orig
        row = store.get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PENDING", "SHORT:95"),
                         "list_active sonrası doğan epoch bayat kanıtla bozuldu")

    # F — aynı sweep iki kez: idempotent; CLOSED kalan satır değişmez,
    # ikinci tur hiçbir şey yapmaz.
    def test_F_repeated_sweep_is_idempotent(self):
        store = self._seed_placed("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        first = store.get("BTCUSDT")
        self.run_sync()
        second = store.get("BTCUSDT")
        self.assertEqual((first["state"], second["state"]), ("CLOSED", "CLOSED"))
        self.assertEqual(second["token"], "LONG:100")

    # G1 — sweep PENDING'e hiçbir koşulda dokunmaz (flat snapshot'a rağmen).
    def test_G1_pending_survives_sweep(self):
        store = self._store()
        store.acquire("BTCUSDT", "LONG:100")  # PENDING kalır
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "PENDING")

    # G2 — executor-akışı da PENDING'i yalnız-flat-kanıtıyla kapatmamalı.
    # Eski davranış: helper PENDING satırını CLOSED etti. Beklenen: PENDING.
    def test_G2_executor_flow_never_closes_pending(self):
        store = self._store()
        store.acquire("BTCUSDT", "LONG:100")  # PENDING (yerleştirme sürüyor)
        self.search_payload = {"data": [term("OLDP", "BTC-USDT", 1_600_000_000, pnl=-0.1)]}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        self.assertIn("OLDP", self.eids(), "executor kapanışı kaydedilmeliydi")
        self.assertEqual(store.get("BTCUSDT")["state"], "PENDING",
                         "PENDING kayıt flat kanıtıyla CLOSED edildi (madde 7)")

    # H — guard meşru kapanışı ÖLDÜRMEMELİ: snapshot öncesi oluşmuş PLACED
    # epoch, exchange-flat görüldüğünde CLOSED olur (FAZ14x canlı düzeltisi).
    def test_H_legit_pre_snapshot_placed_epoch_still_closes(self):
        store = self._seed_placed("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()
        self.assertEqual(store.get("BTCUSDT")["state"], "CLOSED",
                         "zaman guard'ı meşru kapanışı engelledi")


class TestGenerationSafety(SyncHarness):
    """FAZ14x-2 nesil-2: updated_at DUVAR SAATİDİR; NTP/saat geri alınırsa
    yeni epoch, snapshot kesitinden DAHA ESKİ timestamp alabilir ve saat-koşullu
    guard'ı atlatır. Bu sınıf saat-geri-alımı simülasyonlarıyla bunu kanıtlar;
    düzeltme SQLite üzerinde monoton INTEGER generation kullanır (saat-bağımsız).

    Simülasyon: race_hook, position_risk() çağrısı İÇİNDE (snapshot istek-anı
    kaydedildikten sonra) sl_placement._now'u 1 saat GERİYE alır ve o saatle
    yeni epoch acquire/mark_placed yapar — gerçek bir NTP step-back'in DB'ye
    yazdırdığı tam durumdur.
    """

    class _RacyBinance:
        def __init__(self, owner):
            self._owner = owner

        def position_risk(self):
            snap = self._owner.positions
            hook = self._owner.race_hook
            if hook:
                self._owner.race_hook = None
                hook()
            return snap

    def _store(self):
        from sl_placement import SlPlacementStore
        store = SlPlacementStore(self.db)
        store.init_schema()
        return store

    def _seed_placed(self, symbol, token):
        store = self._store()
        store.acquire(symbol, token)
        store.mark_placed(symbol, token)
        return store

    def setUp(self):
        super().setUp()
        self.race_hook = None

    def _run_racy(self):
        at.sync_risk_engine._stale_retry = {}
        at.sync_risk_engine(self.risk, _FakeHB(),
                            binance=self._RacyBinance(self))

    def _rewound(self, fn):
        """fn() boyunca sl_placement._now 1 saat geriye alınır (NTP step-back)."""
        import sl_placement as sp
        from datetime import datetime as _dt, timezone as _tzu, timedelta as _td
        real = sp._now
        try:
            sp._now = lambda: (_dt.now(_tzu.utc) - _td(hours=1)).isoformat()
            fn()
        finally:
            sp._now = real

    # N1 — SAAT GERİ GERİDERKEN yeni epoch korunmalı. (Eski kod: geri-saatli
    # updated_at < snap_iso'yu sağladığı için bayat snapshot yeni PLACED epoch'u
    # kapatır → RED.)
    def test_N1_clock_rewind_new_epoch_survives(self):
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        def hook():
            from sl_placement import ACQUIRED

            def born_in_the_past():
                store = self._store()
                assert store.acquire("BTCUSDT", "LONG:100") == ACQUIRED
                store.mark_placed("BTCUSDT", "LONG:100")
            self._rewound(born_in_the_past)

        self.race_hook = hook
        self._run_racy()
        row = self._store().get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PLACED", "LONG:100"),
                         "saat geriye alınmış yeni PLACED epoch, bayat flat "
                         "snapshot ile CLOSED edildi (saat-guard'ı aşıldı)")

    # N2 — saat geriye giderken AYNI TOKEN'la yeniden doğan epoch korunmalı.
    def test_N2_clock_rewind_same_token_revival_survives(self):
        store = self._seed_placed("BTCUSDT", "LONG:100")
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.run_sync()  # meşru kapanış (normal saat)
        self.assertEqual(store.get("BTCUSDT")["state"], "CLOSED")

        def hook():
            from sl_placement import ACQUIRED

            def reborn_in_the_past():
                assert store.acquire("BTCUSDT", "LONG:100") == ACQUIRED
                store.mark_placed("BTCUSDT", "LONG:100")
            self._rewound(reborn_in_the_past)

        self.race_hook = hook
        self._run_racy()
        self.assertEqual(store.get("BTCUSDT")["state"], "PLACED",
                         "aynı token'lı, geri-saatli yeniden doğan dönem bayat "
                         "snapshot ile CLOSED edildi")

    # N3 — saat geriye gitse de PENDING sweep'e asla yakalanmaz.
    def test_N3_pending_survives_sweep_under_clock_rewind(self):
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        def hook():
            def pending_in_the_past():
                self._store().acquire("BTCUSDT", "LONG:100")  # PENDING
            self._rewound(pending_in_the_past)

        self.race_hook = hook
        self._run_racy()
        row = self._store().get("BTCUSDT")
        self.assertEqual((row["state"], row["token"]), ("PENDING", "LONG:100"),
                         "geri-saatli PENDING kayıt sweep ile kapatıldı")

    # N4 — açık pozisyonun SL kaydı, saat karmaşasında da korunur.
    def test_N4_open_position_epoch_survives_under_clock_rewind(self):
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "-0.4"}]  # AÇIK

        def hook():
            def placed_in_the_past():
                store = self._store()
                store.acquire("BTCUSDT", "LONG:100")
                store.mark_placed("BTCUSDT", "LONG:100")
            self._rewound(placed_in_the_past)

        self.race_hook = hook
        self._run_racy()
        row = self._store().get("BTCUSDT")
        self.assertEqual(row["state"], "PLACED",
                         "açık pozisyonun SL epoch'u kapatıldı")

    # N5 — Yaşamlılık: saat geriye alınsa bile snapshot-öncesi PLACED epoch'un
    # meşru exchange-flat kapanışı çalışmaya devam etmeli.
    def test_N5_legit_close_survives_clock_rewind_during_sweep(self):
        store = self._seed_placed("BTCUSDT", "LONG:100")  # normal saatli eski satır
        self.search_payload = {"data": []}
        self.positions = [{"symbol": "BTCUSDT", "positionAmt": "0"}]

        def hook():
            import sl_placement as sp
            from datetime import datetime as _dt, timezone as _tzu, timedelta as _td
            real = sp._now
            try:
                sp._now = lambda: (_dt.now(_tzu.utc) - _td(hours=1)).isoformat()
                time.sleep(0)  # hook tek ömürlük; satırlar normal saatli
            finally:
                sp._now = real

        # hook sweep sırasında hiçbir satır YAZMIYOR (saat yalnız okuma
        # evresinde karışık); CLOSED yine de gerçekleşmeli.
        self.race_hook = lambda: None
        self._run_racy()
        self.assertEqual(store.get("BTCUSDT")["state"], "CLOSED",
                         "meşru kapanış bozuldu")
