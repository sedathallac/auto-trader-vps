"""FAZ18 (Mission C FAZ 1) — exchange-doğrulamalı çıkış koruması.

Kapsanan sözleşmeler (1.1–1.8):
  A. place_algo_sl WIRE DOKTRİNİ (gerçek client, sahte _post):
     closePosition=true → quantity/reduceOnly BİRLİKTE YOK; workingType=MARK_PRICE;
     closePosition+quantity → ValueError; ONEWAY quantity-mode → reduceOnly=true;
     HEDGE positionSide → positionSide var, reduceOnly YOK; newClientAlgoId iletilir.
  B. −4120 (STOP_ORDER_SWITCH_ALGO) ve ack-kaybı: UNPROTECTED sınıflandırma,
     PLACED satırı YOK, giriş kapısı kapalı (gerçek ensure_sl_coverage yolu).
  C. Ack + doğrulama OK → PLACED satırında algoId/clientAlgoId/workingType.
  D. Ack + doğrulama KESİN-MISSING → fail-closed: reduceOnly MARKET kapanış
     çağrısı + sl_protection_events PROTECTION_FAILED satırı (additive).
  E. Restart re-map: ölü algoId'li PLACED satırı, clientAlgoId GET'i veya
     openAlgoOrders ile yeniden bulunur; satır güncellenir, ÇİFT SL YOK.
  F. countdownCancelAll endpoint'i kaynakta HİÇ geçmez (FAZ1.7 guard).
  G. 1.8 partial-fill: PARTIALLY_FILLED SL canlıdır (çift SL yok); ack-kaybı
     üç-durum: FOUND→PLACED benimsenir, ABSENT→epoch serbest, UNKNOWN→PENDING
     kalır (çift-SL guard; kapatma yok).
  H. 1.6 koşullu-emir marjin tamponu: notional/lev × (1+%buf); okunamayan
     girdi fail-closed; RISK_CONFIG varsayılanları.

Boundary: sahte Binance, gerçek çağrı yolları (ensure_sl_coverage /
BinanceClient.place_algo_sl / SlPlacementStore). Deposit/canlı yok.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import (BinanceAPI, ensure_sl_coverage, record_protection_event,
                         remap_placed_algos, verify_sl_ack)
from sl_placement import SlPlacementStore

SYM = "SOLUSDT"
POS = {"symbol": SYM, "positionAmt": "0.3", "entryPrice": "83.08"}


def _stop(sym=SYM, algo_id="555", status="NEW", typ="STOP_MARKET",
          close_position="true"):
    return {"symbol": sym, "algoId": algo_id, "orderType": typ,
            "algoStatus": status, "side": "SELL", "positionSide": "BOTH",
            "closePosition": close_position, "reduceOnly": "false",
            "quantity": "0"}


class WireFakeClient(BinanceAPI):
    """Gerçek imza/parametre kodunu çalıştırır; HTTP'e çıkmaz (_post/_get sahte)."""

    def __init__(self):
        super().__init__("test-key", "test-secret", base="https://testnet.binancefuture.com")
        self.sent = []

    def _post(self, path, params, method="GET"):
        self.sent.append({"path": path, "method": method, "params": dict(params)})
        return {"algoId": 42, "clientAlgoId": params.get("newClientAlgoId")}

    def _get(self, path, params=None, signed=False):
        return {}

    def filters(self, symbol):
        return {"PRICE_FILTER": {"tickSize": "0.01"}}


class TestPlaceWireDoctrine(unittest.TestCase):
    # A — 1.1 kural kilitleri
    def test_closeposition_omits_quantity_and_reduceonly(self):
        c = WireFakeClient()
        c.place_algo_sl(SYM, 80.0, None, 1, close_position=True,
                        client_algo_id="cid-1")
        p = c.sent[0]["params"]
        self.assertEqual(c.sent[0]["path"], "/fapi/v1/algoOrder")
        self.assertEqual(p["algoType"], "CONDITIONAL")
        self.assertEqual(p["type"], "STOP_MARKET")
        self.assertEqual(p["closePosition"], "true")
        self.assertNotIn("quantity", p)
        self.assertNotIn("reduceOnly", p)
        self.assertEqual(p["workingType"], "MARK_PRICE")
        self.assertEqual(p["newClientAlgoId"], "cid-1")

    def test_closeposition_with_quantity_rejected(self):
        c = WireFakeClient()
        with self.assertRaises(ValueError):
            c.place_algo_sl(SYM, 80.0, 0.3, 1, close_position=True)
        self.assertEqual(c.sent, [])   # istek HİÇ gitmedi

    def test_quantity_mode_oneway_has_reduceonly(self):
        c = WireFakeClient()
        c.place_algo_sl(SYM, 80.0, 0.3, 2)   # SHORT → BUY tarafı
        p = c.sent[0]["params"]
        self.assertEqual(p["side"], "BUY")
        self.assertEqual(p["quantity"], "0.30000000")
        self.assertEqual(p["reduceOnly"], "true")
        self.assertNotIn("closePosition", p)

    def test_hedge_position_side_no_reduceonly(self):
        c = WireFakeClient()
        c.place_algo_sl(SYM, 80.0, 0.3, 1, position_side="LONG")
        p = c.sent[0]["params"]
        self.assertEqual(p["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", p)   # HEDGE'de reduceOnly reddedilir

    def test_tp_algo_take_profit_market_closeposition(self):
        c = WireFakeClient()
        c.place_algo_tp(SYM, 90.0, 1, client_algo_id="cid-tp")
        p = c.sent[0]["params"]
        self.assertEqual(p["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(p["closePosition"], "true")
        self.assertEqual(p["workingType"], "MARK_PRICE")
        self.assertNotIn("quantity", p)

    # F — FAZ1.7 guard
    def test_countdown_cancel_all_never_referenced(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "auto_trader.py")).read()
        # Uç-nokta çağrısı olarak GEÇEMEZ (docstring'teki yasak-notu serbest).
        self.assertNotIn("/fapi/v1/countdownCancelAll", src)
        self.assertNotIn("countdownCancelAll/futures", src)


class AlgoFakeBinance:
    """ensure_sl_coverage sınırının tam sahtesi — FAZ18 metotlarıyla."""

    def __init__(self, positions, algo, place_result=None, algo_lookup=None,
                 dual=False):
        self._positions = positions
        self._algo = algo                    # {sym: list|dict|Exception}
        self._place_result = place_result if place_result is not None \
            else {"algoId": 777, "clientAlgoId": "cid-placed"}
        self._algo_lookup = algo_lookup or {}   # {("algo",id)|("cid",cid): dict}
        self._dual = dual
        self.placed = []
        self.closed = []

    def position_risk(self):
        return list(self._positions)

    def position_mode(self):
        return {"dualSidePosition": self._dual}

    def open_algo_orders_raw(self, sym):
        v = self._algo.get(sym, [])
        if isinstance(v, Exception):
            raise v
        return v

    def open_algo_orders_for(self, sym):
        v = self._algo.get(sym, [])
        return v if isinstance(v, list) else []

    def filters(self, sym):
        return {"PRICE_FILTER": {"tickSize": "0.01"}}

    def _get(self, path, params=None, signed=False):
        return []                            # ATR fallback

    def get_algo_order(self, algo_id=None, client_algo_id=None):
        key = ("algo", str(algo_id)) if algo_id is not None else ("cid", client_algo_id)
        wkey = (("algo", "*") if algo_id is not None else ("cid", "*"))
        for k in (key, wkey):
            if k in self._algo_lookup:
                r = self._algo_lookup[k]
                return r(self) if callable(r) else r
        return {"code": -2013, "msg": "Order does not exist."}

    def place_algo_sl(self, sym, trig, qty, side, **kw):
        self.placed.append({"sym": sym, "trig": trig, "qty": qty, "side": side,
                            **kw})
        r = self._place_result
        r = r(self) if callable(r) else r
        if isinstance(r, Exception):      # ACK-KAYBI: yanıt alınamıyor, emir oluşmuş OLABİLİR
            raise r
        return r

    def place_algo_tp(self, sym, trig, side, **kw):
        self.placed.append({"tp": True, "sym": sym, "trig": trig})
        return {"algoId": 999}

    def close_position(self, symbol, amount):
        self.closed.append((symbol, amount))
        return {"orderId": 1, "status": "FILLED"}


class StoreBase(unittest.TestCase):
    def setUp(self):
        auto_trader._OPEN_UNPROTECTED.clear()
        self._tmp = tempfile.mkdtemp(prefix="faz18-")
        self.db = os.path.join(self._tmp, "risk_ledger.db")
        self._saved = auto_trader.SL_LEDGER_DB
        auto_trader.SL_LEDGER_DB = self.db

    def tearDown(self):
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader.SL_LEDGER_DB = self._saved

    def store(self):
        s = SlPlacementStore(self.db)
        s.init_schema()
        return s

    def events(self):
        try:
            conn = sqlite3.connect(self.db)
            rows = conn.execute(
                "SELECT symbol,event,detail FROM sl_protection_events").fetchall()
            conn.close()
            return rows
        except sqlite3.OperationalError:
            return []


class TestUnprotectedPaths(StoreBase):
    # B — −4120: exchange reddi → UNPROTECTED, PLACED YOK, giriş kapalı
    def test_minus_4120_no_placed_and_gate_closed(self):
        fb = AlgoFakeBinance([POS], {SYM: []},
                             place_result={"code": -4120,
                                           "msg": "Stop order switch algo."})
        ensure_sl_coverage(fb)
        self.assertEqual(len(fb.placed), 1)          # denendi
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)
        self.assertTrue(auto_trader.should_block_new_entries())
        self.assertIsNone(self.store().get(SYM))      # PLACED satırı YOK
        self.assertEqual(fb.closed, [])               # -4120 KAPATMA tetiklemez

    # D — ack var ama emir exchange'te YOK (kanıtlı) → fail-closed + olay
    def test_ack_then_missing_fail_closed(self):
        fb = AlgoFakeBinance([POS], {SYM: []})         # get → -2013; open → []
        ensure_sl_coverage(fb)
        self.assertEqual(len(fb.placed), 1)
        self.assertEqual(fb.closed, [(SYM, 0.3)])      # reduceOnly MARKET kapanış
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0][0], SYM)
        self.assertEqual(ev[0][1], "PROTECTION_FAILED")
        self.assertIsNone(self.store().get(SYM))       # PLACED işaretlenmedi

    # E — ack doğrulanamıyorsa (boundary'da GET yok) KAPATMA YOK (UNKNOWN)
    def test_no_get_method_cannot_prove_missing(self):
        class NoGetBoundary:            # get_algo_order HİÇ taşımayan sınır
            def __init__(self, fb):
                self._fb = fb

            def open_algo_orders_raw(self, sym):
                return []

            def close_position(self, *a, **k):
                self._fb.closed.append(a)
        fb = AlgoFakeBinance([POS], {SYM: []})
        v, d = verify_sl_ack(NoGetBoundary(fb), SYM, 123)
        self.assertEqual(v, "UNKNOWN")
        self.assertEqual(fb.closed, [])


class TestPlacedIdentity(StoreBase):
    # C — doğrulanmış yerleştirme: PLACED + kimlik kolonları
    def test_placed_row_carries_exchange_identity(self):
        fb = AlgoFakeBinance(
            [POS], {SYM: []},
            algo_lookup={("algo", "777"): {"algoId": 777, "algoStatus": "NEW",
                                           "clientAlgoId": "cid-placed"}})
        ensure_sl_coverage(fb)
        row = self.store().get(SYM)
        self.assertEqual(row["state"], "PLACED")
        self.assertEqual(row["algo_id"], "777")
        self.assertEqual(row["client_algo_id"], "cid-placed")
        self.assertEqual(row["working_type"], "MARK_PRICE")
        self.assertNotIn(SYM, auto_trader._OPEN_UNPROTECTED)
        self.assertEqual(fb.closed, [])

    # E — restart re-map: ölü algoId → clientAlgoId GET ile bul → satır güncel;
    #     tek geçerli SL (901) PRESENT olduğu için ÇİFT SL YOK.
    def test_restart_remap_adopt_client_algo_id(self):
        s = self.store()
        tok = "LONG:83.08"
        s.acquire(SYM, tok)
        s.mark_placed(SYM, tok, algo_id=900, client_algo_id="cid-placed",
                      working_type="MARK_PRICE")
        live = _stop(algo_id="901")
        fb = AlgoFakeBinance([POS], {SYM: [live]},
                             algo_lookup={
                                 ("algo", "900"): {"code": -2013},
                                 ("cid", "cid-placed"): {"algoId": 901,
                                                         "algoStatus": "NEW"}})
        ensure_sl_coverage(fb)
        row = self.store().get(SYM)
        self.assertEqual(row["algo_id"], "901")
        self.assertEqual(row["state"], "PLACED")
        self.assertEqual(fb.placed, [])                # çift SL gönderilmedi

    # watchdog: PRESENT satır doğrulanır, hiçbir şey değişmez (idempotent)
    def test_remap_idempotent_when_live(self):
        s = self.store()
        tok = "LONG:83.08"
        s.acquire(SYM, tok)
        s.mark_placed(SYM, tok, algo_id=901, client_algo_id="cid-placed")
        fb = AlgoFakeBinance([POS], {SYM: [_stop(algo_id="901")]},
                             algo_lookup={("algo", "901"): {"algoId": 901,
                                                            "algoStatus": "NEW"}})
        out = remap_placed_algos(fb, s)
        self.assertEqual(out, {})
        self.assertEqual(self.store().get(SYM)["algo_id"], "901")


class TestProtectionEventAdditive(StoreBase):
    # 9 — olay tablosu yalnız-eklemedir; başka tabloya dokunmaz
    def test_event_row_only_appends(self):
        s = self.store()
        tok = "LONG:1.0"
        s.acquire("XAUUSDT", tok)
        ok = record_protection_event(self.db, "XAUUSDT", "PROTECTION_FAILED",
                                     "deney", algo_id=5, client_algo_id="c5")
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT ts,symbol,event,algo_id,client_algo_id "
                           "FROM sl_protection_events").fetchone()
        st = conn.execute("SELECT state FROM sl_placements WHERE symbol='XAUUSDT'"
                          ).fetchone()
        conn.close()
        self.assertEqual(row[1:3], ("XAUUSDT", "PROTECTION_FAILED"))
        self.assertEqual(row[3], "5")
        self.assertEqual(row[4], "c5")
        self.assertEqual(st[0], "PENDING")            # epoch satırı DEĞİŞMEDİ


class TestAckLossAndPartialFill(StoreBase):
    # G — 1.8 partial-fill: PARTIALLY_FILLED SL CANLIDIR; ikinci SL GİTMEZ.
    def test_partial_fill_sl_is_live_no_double(self):
        fb = AlgoFakeBinance(
            [POS], {SYM: [_stop(algo_id="905", status="PARTIALLY_FILLED")]})
        ensure_sl_coverage(fb)
        self.assertEqual(fb.placed, [])
        self.assertEqual(fb.closed, [])
        self.assertNotIn(SYM, auto_trader._OPEN_UNPROTECTED)

    # G — 1.8 ack-kaybı FOUND: place istisna → cid GET emri bulur → PLACED
    #     benimsenir; epoch serbest KALMAZ, çift SL GİTMEZ.
    def test_ack_loss_found_adopt_no_double(self):
        fb = AlgoFakeBinance([POS], {SYM: []},
                             place_result=RuntimeError("connection reset"),
                             algo_lookup={("cid", "*"): {"algoId": 888,
                                                         "algoStatus": "NEW"}})
        ensure_sl_coverage(fb)
        row = self.store().get(SYM)
        self.assertEqual(row["state"], "PLACED")
        self.assertEqual(row["algo_id"], "888")
        self.assertEqual(len(fb.placed), 1)     # yalnız time-out olan tek istek
        self.assertEqual(fb.closed, [])

    # G — 1.8 ack-kaybı ABSENT: kesin-yok kanıtı → epoch serbest (retry güvenli);
    #     yıkıcı kapatma YOK.
    def test_ack_loss_absent_releases_epoch(self):
        fb = AlgoFakeBinance([POS], {SYM: []},
                             place_result=RuntimeError("connection reset"))
        ensure_sl_coverage(fb)
        self.assertIsNone(self.store().get(SYM))   # epoch silindi → sonraki tur dener
        self.assertEqual(fb.closed, [])

    # G — 1.8 ack-kaybı UNKNOWN: sorgu uçları güvensiz → PENDING KALIR
    #     (çift-SL guard'ı yeniden denemeden önce gelir), KAPATMA YOK.
    def test_ack_loss_unknown_keeps_pending(self):
        # cid GET anlamsız döner (boundary arızası) → kanıt YETERSİZ → UNKNOWN.
        fb = AlgoFakeBinance([POS], {SYM: []},
                             place_result=RuntimeError("connection reset"),
                             algo_lookup={("cid", "*"): RuntimeError("http 500")})
        ensure_sl_coverage(fb)
        row = self.store().get(SYM)
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "PENDING")
        self.assertEqual(fb.closed, [])

    # find_algo_by_client_id üç durumunun doğrudan kilidi
    def test_find_by_client_id_tri_state(self):
        f = auto_trader.find_algo_by_client_id
        fb = AlgoFakeBinance([], {SYM: []},
                             algo_lookup={("cid", "*"): {"algoId": 888}})
        self.assertEqual(f(fb, SYM, "x"), ("FOUND", "888"))
        fb2 = AlgoFakeBinance([], {SYM: []})          # GET -2013 + liste []
        self.assertEqual(f(fb2, SYM, "x")[0], "ABSENT")
        fb3 = AlgoFakeBinance([], {SYM: RuntimeError("http 500")})
        self.assertEqual(f(fb3, SYM, "x")[0], "UNKNOWN")


class TestConditionalMarginGate(unittest.TestCase):
    # 1.6 — koşullu emir marjin tamponu: gereken = notional/lev × (1+%buf)
    def test_gate_math(self):
        g = auto_trader.conditional_margin_gate
        ok, why = g(10.0, 40.0, 4, buffer_pct=20)     # need 10×1.2=12 > 10
        self.assertFalse(ok)
        self.assertIn("yetersiz", why)
        self.assertTrue(g(12.0, 40.0, 4, buffer_pct=20)[0])
        self.assertFalse(g(9.9, 40.0, 4, buffer_pct=0)[0])   # kapalı bile marjin ister
        self.assertTrue(g(10.0, 40.0, 4, buffer_pct=0)[0])
        ok, why = g(None, 40.0, 4, buffer_pct=20)     # okunamayan girdi fail-closed
        self.assertFalse(ok)

    def test_config_defaults(self):
        self.assertEqual(
            auto_trader.RISK_CONFIG["faz18_conditional_margin_buffer_pct"], 20)
        self.assertFalse(auto_trader.RISK_CONFIG["faz18_tp_algo_enabled"])


if __name__ == "__main__":
    unittest.main()
