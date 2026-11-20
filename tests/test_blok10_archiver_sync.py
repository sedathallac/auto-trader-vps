"""BLOK10 §5 (F-G) — archiver watermark'ı HATA VAR KENEN İLERLETİYOR (P1, kanıt bütünlüğü).

Doğrulanan kök neden (sync_trades ve sync_orders aynı kalıp):
  * fetch sayfası istisna fırlatır → `except: log.warning` (archiver.py:183-184)
    watermark güncellemesi ONLARI DIŞARIDA çağırır (185-192) → başarısız
    pencere bir daha HİÇ taranmaz (1h buffer sonrası kalıcı boşluk);
  * _get error-dict dönerse (API hatası) 151-153'te "boş liste" ile AYNI
    yoldan break eder → error ≠ empty ihlali; yine watermark ilerler;
  * 100 sayfa × 500 limit dolduysa backlog bitmemiştir ama watermark yine
    ilerler → kuyruk kuyruğu kaybolur (sessiz truncation).

Onarım: sembol-bazlı sync_ok; yalnız GÜVENİLİR tam senkronizasyonda
(liste dönüşleri + sayfa sonu) watermark ilerler. Kayıtlar trade_id/order_id
UNIQUE ile INSERT OR IGNORE/UPSERT olduğundan yeniden tarama güvenlidir
(idempotent), boşluk ise TELAFİSİZDİR — fail-closed tut.

archiver import'ta HOME'a archiver.log açar → import sırasında HOME geçici.
"""
import os
import sys
import tempfile
import unittest

_ORIG_HOME = os.environ.get("HOME")
_TMP_HOME = tempfile.mkdtemp(prefix="blok10-archiver-home-")
os.environ["HOME"] = _TMP_HOME
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import archiver
finally:
    if _ORIG_HOME is not None:
        os.environ["HOME"] = _ORIG_HOME

SYM = "BTCUSDT"
OLD_WATERMARK = "2020-01-01T00:00:00Z"


def _trade(i):
    return {"id": i, "symbol": SYM, "orderId": 1000 + i, "side": "BUY",
            "price": "100", "qty": "0.1", "quoteQty": "10",
            "commission": "0.01", "commissionAsset": "USDT",
            "realizedPnl": "0", "time": 1700000000000 + i}


def _order(i):
    return {"orderId": i, "symbol": SYM, "side": "BUY", "type": "LIMIT",
            "status": "FILLED", "price": "100", "origQty": "0.1",
            "executedQty": "0.1", "stopPrice": "0", "updateTime": 1700000000000 + i}


class FakeBinance:
    def __init__(self, pages=None, behavior="list"):
        # behavior: list | error_dict | raise ; pages: list[list] (bitince tekrar-son)
        self.pages = pages
        self.behavior = behavior
        self.calls = 0

    def position_risk(self):
        return []

    def _get(self, path, params=None, signed=False):
        self.calls += 1
        if self.behavior == "raise":
            raise RuntimeError("network down")
        if self.behavior == "error_dict":
            return {"code": -1121, "msg": "invalid"}
        if self.pages is None:
            return []
        idx = min(self.calls - 1, len(self.pages) - 1)
        return self.pages[idx]


class ArchiverBase(unittest.TestCase):
    def setUp(self):
        self.dbf = os.path.join(tempfile.mkdtemp(prefix="blok10-arch-db-"), "arc.db")
        self.conn = archiver.init_db(self.dbf)
        archiver.register_archive_symbol(self.conn, SYM, "test")
        c = self.conn.cursor()
        c.execute("UPDATE archive_symbols SET last_trade_sync=?, last_order_sync=? "
                  "WHERE symbol=?", (OLD_WATERMARK, OLD_WATERMARK, SYM))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _wm(self, col):
        return self.conn.execute(
            f"SELECT {col} FROM archive_symbols WHERE symbol=?", (SYM,)).fetchone()[0]


class TestSyncTradesWatermark(ArchiverBase):
    def test_exception_keeps_watermark(self):
        archiver.sync_trades(FakeBinance(behavior="raise"), self.conn)
        self.assertEqual(self._wm("last_trade_sync"), OLD_WATERMARK,
                         "fetch hatasına rağmen watermark ilerledi — "
                         "o pencerenin fill'leri bir daha taranmaz")

    def test_error_dict_keeps_watermark(self):
        archiver.sync_trades(FakeBinance(behavior="error_dict"), self.conn)
        self.assertEqual(self._wm("last_trade_sync"), OLD_WATERMARK)

    def test_page_cap_exhausted_keeps_watermark(self):
        full = [[_trade(i) for i in range(k * 500, k * 500 + 500)] for k in range(100)]
        archiver.sync_trades(FakeBinance(pages=full), self.conn)
        self.assertEqual(self._wm("last_trade_sync"), OLD_WATERMARK,
                         "50.000 sayfa limiti doldu (backlog bitmedi) — "
                         "watermark ilerlememeliydi")

    def test_success_advances_watermark(self):
        archiver.sync_trades(FakeBinance(pages=[[_trade(1), _trade(2)]]), self.conn)
        self.assertNotEqual(self._wm("last_trade_sync"), OLD_WATERMARK)
        n = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(n, 2)

    def test_genuine_empty_advances_watermark(self):
        archiver.sync_trades(FakeBinance(pages=None), self.conn)
        self.assertNotEqual(self._wm("last_trade_sync"), OLD_WATERMARK)

    def test_multi_symbol_partial_failure_isolated(self):
        # B sembolü patlarken A temizse: A ilerler, B ilerlemez.
        archiver.register_archive_symbol(self.conn, "ETHUSDT", "test")

        class TwoSymFake(FakeBinance):
            def _get(self, path, params=None, signed=False):
                if (params or {}).get("symbol") == "ETHUSDT":
                    return {"code": -1021, "msg": "timestamp"}
                return [_trade(7)]

        archiver.sync_trades(TwoSymFake(), self.conn)
        a = self.conn.execute("SELECT last_trade_sync FROM archive_symbols "
                              "WHERE symbol=?", (SYM,)).fetchone()[0]
        b = self.conn.execute("SELECT last_trade_sync FROM archive_symbols "
                              "WHERE symbol=?", ("ETHUSDT",)).fetchone()[0]
        self.assertNotEqual(a, OLD_WATERMARK)
        # ETH hiç watermark almadı: hata yolunda UPDATE HİÇ çalışmadı —
        # "ilerlemedi" kanıtı NULL kalmasıdır (register setUp'tan sonra).
        self.assertIsNone(b)


class TestSyncOrdersWatermark(ArchiverBase):
    def test_exception_keeps_watermark(self):
        archiver.sync_orders(FakeBinance(behavior="raise"), self.conn)
        self.assertEqual(self._wm("last_order_sync"), OLD_WATERMARK)

    def test_error_dict_keeps_watermark(self):
        archiver.sync_orders(FakeBinance(behavior="error_dict"), self.conn)
        self.assertEqual(self._wm("last_order_sync"), OLD_WATERMARK)

    def test_success_advances(self):
        archiver.sync_orders(FakeBinance(pages=[[_order(1)]]), self.conn)
        self.assertNotEqual(self._wm("last_order_sync"), OLD_WATERMARK)

    def test_page_cap_keeps_watermark(self):
        full = [[_order(i) for i in range(k * 500, k * 500 + 500)] for k in range(100)]
        archiver.sync_orders(FakeBinance(pages=full), self.conn)
        self.assertEqual(self._wm("last_order_sync"), OLD_WATERMARK)


if __name__ == "__main__":
    unittest.main()
