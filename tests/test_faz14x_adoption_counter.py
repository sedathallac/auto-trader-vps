"""FAZ14x — günlük sayaç / adoption idempotency regresyonları (yalnız hedefli).

Bulgu 2 doğrulaması (kod: auto_trader.py @1c9f904+01229ce):
  register_open'ın TEK çağrı yeri _finalize_open içindedir (SL doğrulama
  sonrası). _finalize_open yalnızca _process_pending_fills'in iki dalından
  çağrılır (doldu / BENİMSENDİ) ve her iki dal da çağırmadan ÖNCE
  _drop_pending_fill(eid) çalıştırır → kayıt diskten+bellekten silinir →
  aynı executor ikinci tick'te tekrar finalize/sayaçlanamaz.
  reconcile tarafındaki BENİMSENDİ (drift-sahiplenme) register_open çağırmaz
  — dışarıdan bulunan pozisyon yeni işlem SAYILMAZ (yapısal: tek çağrı yeri).

Bu testler bu sözleşmeyi sabitler (regresyon kilidi):
  1) Dolan dolum tam olarak BİR kez finalize edilir (iki tick).
  2) Benimsenen (borsada pozisyon olan) dolum tam olarak BİR kez finalize edilir.
  3) Borsa flat "dolmadı" dalı HİÇ finalize etmez (sayaç artmaz, slot boşalır).

FAZ15 Stage2b entegrasyon notu (FAZ14x-2 port — assertler DEĞİŞMEDİ, yalnız
çiftler güncel sözleşmeye uyarlandı; test GEVŞETİLMEDİ):
  * `_process_pending_fills` dönüş sözlüğüne `released` sayacı eklendi
    (rezervasyon-release kanıtı; beklenen değer yine 0).
  * "dolmadı + flat" dalı artık TAM kanıt zinciri ister (eski 01229ce yolu
    yalnız flat bakıyordu — FAZ15 Stage2b bunu kasten SIKILAŞTIRDI):
    hb.stop_executor başarılı + get_executor TERMINATED dönmeli +
    _entry_order_terminal True (giriş emri kimlikleri CANCELED/EXPIRED +
    executedQty=0 + açık emir yok + flat). Kanıt eksikse pending ve
    rezervasyon KORUNUR (fail-closed). Çiftler bu zinciri sağlar; invariant
    aynı: cancelled YALNIZ tam kanıtla 1 olur ve finalize HİÇ çağrılmaz.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at

_ZEROS = {"filled": 0, "adopted": 0, "cancelled": 0, "waiting": 0, "failed": 0,
          "released": 0}


class _HB:
    def __init__(self, ex):
        self._ex = ex
        self._stopped = False

    def get_executor(self, eid):
        if self._stopped:
            # stop sonrası TERMINATED doğrulaması (FAZ15 Stage2b zinciri).
            return {"status": "TERMINATED"}
        return self._ex

    def stop_executor(self, eid, keep_position=True):
        self._stopped = True
        return True


class _Bin:
    def filters(self, sym):
        return {}

    def open_orders(self, symbol):
        return []

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        # Giriş emri: terminal + SIFIR dolum (terminal-order zinciri kanıtı).
        return {"status": "CANCELED", "executedQty": "0"}


class FinalizeHarness(unittest.TestCase):
    def setUp(self):
        self._saved_fills = dict(at._PENDING_FILLS)
        self._orig_fin = at._finalize_open
        self._orig_amt = at._exchange_position_amt
        self.calls = []

        def _fin(*a, **k):
            self.calls.append(a)
            return True

        at._finalize_open = _fin
        at._PENDING_FILLS.clear()
        at._PENDING_FILLS["E1"] = {"symbol": "AAVEUSDT", "side": 1, "amount": 1.7,
                                   "sl_pct": 0.03, "tp_pct": 0.05, "price": 100.0,
                                   "deadline": time.time() + 600, "res_id": None,
                                   # FAZ15 Stage2b terminal-emir zinciri kimlikleri
                                   # (dolmadı dalının release için bunlara ihtiyacı var).
                                   "entry_client_order_ids": ["cid-e1"]}

    def tearDown(self):
        at._finalize_open = self._orig_fin
        at._exchange_position_amt = self._orig_amt
        at._PENDING_FILLS.clear()
        at._PENDING_FILLS.update(self._saved_fills)


class TestAdoptionCounterOnce(FinalizeHarness):
    def test_filled_finalizes_exactly_once_across_ticks(self):
        hb = _HB({"filled_amount_quote": 2.0, "error_count": 0})
        out1 = at._process_pending_fills(_Bin(), hb, None, None, None)
        self.assertEqual(out1["filled"], 1, "ilk tick finalize etmedi")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("E1", at._PENDING_FILLS, "kayıt düşürülmedi (çift sayım riski)")
        out2 = at._process_pending_fills(_Bin(), hb, None, None, None)
        self.assertEqual(out2, _ZEROS, "ikinci tick yeniden işledi")
        self.assertEqual(len(self.calls), 1, "aynı executor iki kez finalize edildi")

    def test_adopted_finalizes_exactly_once(self):
        hb = _HB({"filled_amount_quote": 0.0, "error_count": 1})
        at._exchange_position_amt = lambda b, s: -326.6  # borsada pozisyon var
        out1 = at._process_pending_fills(_Bin(), hb, None, None, None)
        self.assertEqual(out1["adopted"], 1, "benimseme finalize etmedi")
        out2 = at._process_pending_fills(_Bin(), hb, None, None, None)
        self.assertEqual(out2, _ZEROS)
        self.assertEqual(len(self.calls), 1, "benimseme iki kez sayıldı")

    def test_not_filled_flat_never_finalizes(self):
        hb = _HB({"filled_amount_quote": 0.0, "error_count": 1})
        at._exchange_position_amt = lambda b, s: 0.0  # borsa flat
        out1 = at._process_pending_fills(_Bin(), hb, None, None, None)
        self.assertEqual(out1["cancelled"], 1)
        self.assertEqual(len(self.calls), 0, "dolmamış açılış sayaçlandı")


if __name__ == "__main__":
    unittest.main()
