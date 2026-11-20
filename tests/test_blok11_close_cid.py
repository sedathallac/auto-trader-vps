"""BLOK11 §6.2 — kapatma POST'u newClientOrderId taşır (idempotency kanıtı).

Doğrulanan gerçek (§2): SL algo yolu FAZ18'den beri clientAlgoId taşıyor
(place_algo_sl + find_algo_by_client_id telafisi) — R3'ün "hiç client order id
yok" iddiası KISMEN YANLIŞTI. Gerçek boşluk: close_adapter.close_reduce_only
/fapi/v1/order POST'una newClientOrderId EKLEMİYORDU (query/cancel tarafı
orig_client_order_id destekliyordu; yerleştirme tarafı boştu).

GİRİŞ emri notu: girişi HB executor yerleştirir; CID üretimi boundary
sınırıdır — giriş kimlikleri sonradan hasat edilir (entry_client_order_ids,
FAZ15 Stage2b). Bu test yalnız KAPANIŞ CID sözleşmesini çapalar.

Gerçek ağa tek istek yok: _post yakalanır.
"""
import os
import sys
import unittest
from decimal import Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import close_adapter
from close_adapter import BinanceHBCloseAdapter


class _Cap:
    def __init__(self, amt="0.5"):
        self.posts = []
        self.amt = amt

    def _post(self, path, params):
        self.posts.append((path, dict(params)))
        return {"orderId": 1, "status": "FILLED",
                "clientOrderId": params.get("newClientOrderId")}

    def position_risk(self):
        return [{"symbol": "ETHUSDT", "positionAmt": self.amt,
                 "entryPrice": "80"}]


class TestCloseClientOrderId(unittest.TestCase):
    def setUp(self):
        self.b = _Cap()
        self.a = BinanceHBCloseAdapter(self.b, None)
        self._t = close_adapter.time.time
        self._seq = iter([1700000000.111, 1700000000.222, 1700000000.333])
        close_adapter.time.time = lambda: next(self._seq)

    def tearDown(self):
        close_adapter.time.time = self._t

    def _cid_of(self, i=0):
        p = self.b.posts[i][1]
        self.assertIn("newClientOrderId", p)
        cid = p["newClientOrderId"]
        self.assertTrue(cid.startswith("fz9cETHUSDT"), cid)
        self.assertLessEqual(len(cid), 36)
        return cid

    def test_oneway_close_cid_tasir(self):
        out = self.a.close_reduce_only("ETHUSDT", Decimal("0.5"))
        self.assertEqual(out, Decimal("0.5"))
        self.assertEqual(self.b.posts[0][0], "/fapi/v1/order")
        self._cid_of(0)
        self.assertEqual(self.b.posts[0][1].get("reduceOnly"), "true")

    def test_hedge_close_cid_ve_positionSide(self):
        self.a.close_reduce_only("ETHUSDT", Decimal("0.5"), position_side="LONG")
        p = self.b.posts[0][1]
        self._cid_of(0)
        self.assertEqual(p.get("positionSide"), "LONG")
        self.assertNotIn("reduceOnly", p)

    def test_cid_ms_basis_tekil(self):
        """Aynı sembolün iki kapanış denemesi (retry) aynı CID'yi ÜRETEMEZ —
        Binance aynı CID'yi reddederdi; red → FAILED_SAFE (güvenli taraf)."""
        self.a.close_reduce_only("ETHUSDT", Decimal("0.5"))
        self.a.close_reduce_only("ETHUSDT", Decimal("0.4"))
        c1, c2 = self._cid_of(0), self._cid_of(1)
        self.assertNotEqual(c1, c2)


if __name__ == "__main__":
    unittest.main()
