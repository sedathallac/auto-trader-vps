"""BLOK12 devam-E §2: cooldown sırasında ACİL RİSK AZALTMA — doğrulama süiti.

İnceleme bulgusu: "Cooldown sırasında acil risk azaltma → Ek doğrulama
gerekli". Kanıtlanan sözleşme (429/418 yerel cooldown sürerken):

  1. Binance tarafına HİÇBİR istek gitmez (GET/POST/DELETE aynı gardan geçer;
     DELETE = cancel_order dahil). İstek gitmediği için kader UNKNOWN DEĞİLDİR
     — fakat kapanış akışı bunu ASLA "kapatıldı/bitti" saymaz: cancel gönderimi
     local_block döner, durum sorgusu da local_block döner → kod`lu yanıt →
     terminal DEĞİL → UNKNOWN (fail-closed). SL_ABSENT / FLAT iddiası YOK.
  2. ACİL KANAL AÇIK: HB stop_executor (localhost REST, HBAPI) Binance
     cooldown'undan ETKİLENMEZ — pozisyonu HB'nin kendi kapanışına bırakmadan
     (keep_position=True) executor durdurulabilir. Bu bilinçli tasarım:
     HBAPI kaynağı api_guard taşımaz ve bunu bir test sabitler (gelecekte biri
     HBAPI'yi de gardarlarsa acil fren körleşir → test kırılır).
  3. Borsa-üstü algo STOP_MARKET SL'miz sunucuda durur; cooldown onu silmez.
  4. Operasyon notu: 418 sürerken watchdog --once KOŞULMAZ (ban süresini
     yakmış isteklerle uzatır); LIVE_READINESS §10.2.

Bu süit bugünkü davranışı DOĞRULAR (yanıltmaca arandı: cancel'ın local_block
sözlüğü exception değil — adapter query ile teyit ettiği için yine UNKNOWN,
"iptal edildi" yanılması yok) + iki yapısal sabitleme ekler. Ağ YOK: tüm
transport tek sentry ile yakalanır (BinanceAPI ve HBAPI aynı modül _session'ını
kullanır; URL'den ayrılır).
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_guard                                        # noqa: E402
import auto_trader                                      # noqa: E402
from close_adapter import BinanceHBCloseAdapter, CloseAdapterError  # noqa: E402

SYM = "BTCUSDT"


class _Resp:
    def __init__(self, payload=None, status=200):
        self._p = payload if payload is not None else {}
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self):
        return self._p


class _Sentry:
    """Tek taşıyıcı: her çağrıyı kaydeder; Binance URL'lerine dokunulursa
    sayılır; HB URL'leri acil kanal sayılır."""
    def __init__(self, resp=None):
        self.calls = []            # (verb, url)
        self._resp = resp or _Resp({})

    def _rec(self, verb, url):
        self.calls.append((verb, url))
        return self._resp

    def get(self, url, **k):
        return self._rec("GET", url)

    def post(self, url, **k):
        return self._rec("POST", url)

    def delete(self, url, **k):
        return self._rec("DELETE", url)

    def binance(self):
        return [c for c in self.calls if "/fapi/" in c[1]]

    def hb(self):
        return [c for c in self.calls if "/executors" in c[1]]


class _HBFake:
    def active_executors(self):
        return [{"executor_id": "E1", "trading_pair": "BTC-USDT",
                 "custom_info": {"order_ids": ["c-open", "c-close"]}}]


class CooldownBase(unittest.TestCase):
    def setUp(self):
        api_guard.reset()
        # Bu suite KONU bellek gardıdır; süreç-desti dosya katmanı kendi
        # dosyasında testli (test_blok12_guard_file_crossproc). Kurulumdaki
        # 429'un dosyaya yazılması "gard yok" pozitif kontrolünü yanlış
        # bloklamasın → dosya bağlantısını bu suite için kapat.
        api_guard.set_state_file(None)
        self.addCleanup(api_guard.set_state_file,
                        str(auto_trader.API_GUARD_STATE_FILE))
        self.sentry = _Sentry()
        p = mock.patch.object(auto_trader, "_session", self.sentry)
        p.start()
        self.addCleanup(p.stop)
        api_guard.note_http_status(429, {"Retry-After": "600"})
        self.assertTrue(api_guard.blocked(), "cooldown kurulamadı")


class TestBinanceSideRefused(CooldownBase):
    def test_cancel_delete_gonderilmez_local_block(self):
        r = auto_trader.BinanceAPI("k", "s").cancel_order(SYM, order_id=7)
        self.assertIsInstance(r, dict)
        self.assertTrue(r.get("local_block"))
        self.assertEqual(self.sentry.binance(), [])   # tel yok

    def test_query_order_gonderilmez(self):
        r = auto_trader.BinanceAPI("k", "s").query_order(SYM, order_id=7)
        self.assertTrue(r.get("local_block"))
        self.assertEqual(self.sentry.binance(), [])

    def test_pozitif_kontrol_gardi_yokken_delete_gider(self):
        api_guard.reset()
        auto_trader.BinanceAPI("k", "s").cancel_order(SYM, order_id=7)
        self.assertTrue(any(v == "DELETE" and "/fapi/v1/order" in u
                            for v, u in self.sentry.binance()))


class TestCloseAdapterFailClosed(CooldownBase):
    def setUp(self):
        super().setUp()
        self.adapter = BinanceHBCloseAdapter(
            binance=auto_trader.BinanceAPI("k", "s"), hb=_HBFake())

    def test_adapter_cancel_UNKNOWN_asla_iptal_edildi_degil(self):
        # cancel gönderimi local_block (exception DEĞİL) → adapter sorguyla
        # teyit eder; sorgu da local_block → kod'lu → terminal değil → False.
        self.assertFalse(self.adapter.cancel_order(
            {"symbol": SYM, "orderId": 7}))
        self.assertEqual(self.sentry.binance(), [])

    def test_order_terminal_UNKNOWN(self):
        self.assertFalse(self.adapter.order_terminal(
            {"symbol": SYM, "orderId": 7}))

    def test_sl_status_ASLA_SL_ABSENT_DEGIL(self):
        # Kesinti "SL yok" olarak YANILTIMAZ (raise → UNKNOWN/HALT).
        try:
            st = self.adapter.sl_status(SYM)
        except CloseAdapterError:
            return                                    # UNKNOWN — kabul
        self.assertNotIn(st, ("SL_ABSENT",), "kesinti SL yok sayıldı")


class TestEmergencyChannelOpen(CooldownBase):
    def test_hb_stop_cooldown_dan_etkilenmez(self):
        # Acil fren: HB stop localhost'tur, Binance gardı DEVREYE GİRMEZ.
        hb = auto_trader.HBAPI("http://127.0.0.1:9999", "u", "p")
        self.sentry._resp = _Resp({"stopped": True})
        out = hb.stop_executor("E1", keep_position=True)
        self.assertEqual(out, {"stopped": True})
        self.assertEqual(len(self.sentry.hb()), 1)
        self.assertEqual(self.sentry.binance(), [])

    def test_HBAPI_kaynagi_api_guard_tasimaz(self):
        # Sabitleme: HBAPI'yi gardarlamak acil frene ateş eder — kasıtlı değil.
        src = __import__("inspect").getsource(auto_trader.HBAPI)
        self.assertNotIn("api_guard", src)

    def test_BinanceAPI_guard_kaynak_pin(self):
        src = __import__("inspect").getsource(auto_trader.BinanceAPI._get)
        self.assertIn("api_guard.blocked()", src)
        src = __import__("inspect").getsource(auto_trader.BinanceAPI._post)
        self.assertIn("api_guard.blocked()", src)


if __name__ == "__main__":
    unittest.main()
