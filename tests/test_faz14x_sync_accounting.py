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
        # 79bed1d sayfalama sözleşmesi: tamamlanma yalnız geçerli BOŞ sayfa ile
        # kanıtlanır; aynı sayfanın tekrarı KISMİ demektir (fail-closed). Fixture
        # tek-sayfa gerçeğini temsil eder: offset>0 -> boş sayfa. (Aynı uyarlama
        # 79bed1d'nin kendi test_inc6_k2.py'sinde de yapıldı; assert değişmez.)
        off = int((json or {}).get("offset") or 0)
        if off:
            return _Resp({"data": []}, self.search_status)
        return _Resp(self.search_payload, self.search_status)

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


if __name__ == "__main__":
    unittest.main()
