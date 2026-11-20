"""BLOK10 §5 (F-F) — K-3 BUSY kilidi: PLACED satırı exchange'te olmayan SL → KALICI KORUMASIZLIK (P1).

Doğrulanan kök neden zinciri:
  1. Pozisyon açılır, SL yerleşir, K-3 satırı PLACED (algo_id kayıtlı).
  2. SL emri exchange'ten DÜŞER (elle iptal, ack sonrası sunucu-taraflı
     reddi, eski-sürüm yeniden başlatma penceresi).
  3. remap_placed_algos (auto_trader.py:1964 "Bulunamazsa satıra
     DOKUNULMAZ") satırı olduğu gibi bırakır.
  4. sl_status güvenilir ABSENT der → sembol `missing`e girer.
  5. sl_placement.acquire aynı token + state PLACED için BUSY döner
     (sl_placement.py:207-209 — TTL/supersede YOK).
  6. ensure_sl_coverage BUSY'yi "başka process yerleştiriyor" sanıp
     sessizce atlar (2244-2246) → POZİSYON HİÇBİR ZAMAN SL'SİZ kalır,
     alarm ÜRETİLMEZ, giriş kapısı açık kalır.

Onarım (minimal, çift-SL korumasını ÖLDÜRMEZ): BUSY + satır durumu PLACED +
aynı token + sl_status'un GÜVENİLİR ABSENT'i (missing listesi zaten bu) →
satır bayattır; release et ve yeniden dene. Satır PENDING ise (gerçek
uçuşta yerleştirme) BUSY KORUNUR — çift-SL koruması aynen yaşar.

tests/conftest.py SL_LEDGER_DB'yi test-başına izole eder; mağaza GERÇEK,
adapter GERÇEK (close_adapter.sl_status), sahte yalnız Binance sınırı.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from sl_placement import SlPlacementStore

SYM = "SOLUSDT"
TOK = "LONG:83.08"


def _pos(amt=0.3, symbol=SYM):
    return {"symbol": symbol, "positionAmt": str(amt), "entryPrice": "83.08"}


class FakeBinance:
    def __init__(self, positions, algo_empty=True):
        self._positions = positions
        self._algo_empty = algo_empty        # reliable [] (hata DEĞİL)
        self.placed = []

    def position_risk(self):
        return list(self._positions)

    def position_mode(self):
        return {"dualSidePosition": False}   # ONEWAY

    def open_algo_orders_raw(self, sym):
        return [] if self._algo_empty else [{"algoId": 111, "type": "STOP_MARKET"}]

    def open_algo_orders_for(self, sym):
        return self.open_algo_orders_raw(sym)

    def get_algo_order(self, algo_id=None, client_algo_id=None):
        # Bayat PLACED satırının 111'i YOK; yeni yerleşen 999 VAR.
        if algo_id == 999:
            return {"algoId": 999, "algoStatus": "NEW"}
        return {"code": -2011, "msg": "Order does not exist."}

    def filters(self, sym):
        return {"PRICE_FILTER": {"tickSize": "0.01"}}

    def _get(self, path, params=None, signed=False):
        return []

    def place_algo_sl(self, sym, trig, qty, side, **_kw):
        self.placed.append({"sym": sym, "trig": trig, "qty": qty, "side": side})
        return {"algoId": 999}


class SlStalePlacedBase(unittest.TestCase):
    def setUp(self):
        # conftest autouse: auto_trader.SL_LEDGER_DB -> tmp/risk_ledger.db
        self.store = SlPlacementStore(auto_trader.SL_LEDGER_DB)
        self.store.init_schema()
        self._saved_unprot = dict(auto_trader._OPEN_UNPROTECTED)
        auto_trader._OPEN_UNPROTECTED.clear()

    def tearDown(self):
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._saved_unprot)


class TestStalePlacedRelease(SlStalePlacedBase):
    def test_placed_row_absent_on_exchange_is_replaced(self):
        # 1) Bayat dönem: PLACED satır, algo 111 exchange'te YOK.
        self.assertEqual(self.store.acquire(SYM, TOK), "ACQUIRED")
        self.store.mark_placed(SYM, TOK, algo_id=111, client_algo_id="old-cid")
        b = FakeBinance([_pos()])
        # 2) Onarım öncesi: BUSY → sessiz skip → placed==[] (KIRMIZI).
        auto_trader.ensure_sl_coverage(b)
        self.assertEqual(len(b.placed), 1,
                         "PLACED satırı exchange'te yokken BUSY skip korundu — "
                         "pozisyon kalıcı korumasız kalırdı")
        row = self.store.get(SYM)
        self.assertEqual(row["state"], "PLACED")
        self.assertEqual(row["algo_id"], "999")   # taze doğrulanmış kimlik

    def test_pending_row_keeps_busy_protection(self):
        # Gerçek uçuşta yerleştirme (PENDING) → BUSY KORUNUR, çift SL yok.
        self.assertEqual(self.store.acquire(SYM, TOK), "ACQUIRED")  # PENDING kaldı
        b = FakeBinance([_pos()])
        auto_trader.ensure_sl_coverage(b)
        self.assertEqual(b.placed, [])
        self.assertEqual(self.store.get(SYM)["state"], "PENDING")

    def test_different_token_supersede_unchanged(self):
        # Eski dönemin PLACED satırı + yeni token → doğal supersede (regresyon).
        self.assertEqual(self.store.acquire(SYM, "LONG:70.0"), "ACQUIRED")
        self.store.mark_placed(SYM, "LONG:70.0", algo_id=111)
        b = FakeBinance([_pos()])
        auto_trader.ensure_sl_coverage(b)
        self.assertEqual(len(b.placed), 1)
        row = self.store.get(SYM)
        self.assertEqual(row["token"], TOK)
        self.assertEqual(row["state"], "PLACED")

    def test_no_row_placement_flow_unchanged(self):
        # Satır yoksa normal yerleştirme aynen çalışır (regresyon çapası).
        b = FakeBinance([_pos()])
        auto_trader.ensure_sl_coverage(b)
        self.assertEqual(len(b.placed), 1)
        self.assertEqual(self.store.get(SYM)["algo_id"], "999")

    def test_no_position_no_writes(self):
        b = FakeBinance([])
        auto_trader.ensure_sl_coverage(b)
        self.assertEqual(b.placed, [])
        self.assertIsNone(self.store.get(SYM))


if __name__ == "__main__":
    unittest.main()
