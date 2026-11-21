"""BLOK16.3 — Anchorsuz (benimsenmiş) executor PnL atfı: SL-defteri algo yedeği.

Kanıtlanmış boşluk (VPS adliyesi 2026-11-21): kernel-OOM sonrası HB `custom_info`
null kalan benimsenmiş ADA executor'unda x-nb anchor bulunamadı → "found 0" →
net PnL 3 gün UNKNOWN kaldı (BLOK15 elle kapattı). Bu dosyadaki testler ADA
dosyasının küçültülmüş replikasıdır: kapanış, SL defterindeki algo kaydının
actualOrderId'sinden; giriş, penceredeki TEK non-reduceOnly emirden kanıtlanır.
Hiçbir test gerçek ağa çıkmaz; Binance istemcisi tamamen sahtedir ve PnL
yalnız Decimal mutabakatıyla üretilir — belirsiz her senaryo fail-closed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at
from sl_placement import SlPlacementStore

CREATED_S = 1794935139          # 2026-11-17T17:05:39Z
ENTRY_T = CREATED_S * 1000 + 2391
EXIT_T = 1795042106220          # 2026-11-18T22:48:26.220Z (ADA exit fill)
ENTRY_OID = 70344593197
EXIT_OID = 70386915861
ALGO_ID = "2000001720858143"


def _ut(i, oid, side, qty, price, ts, realized="0", fee="0.01"):
    return {"id": i, "orderId": oid, "side": side, "qty": qty, "price": price,
            "time": ts, "realizedPnl": realized, "commission": fee,
            "commissionAsset": "USDT"}


def _ada_dataset():
    """ADA yaşam döngüsünün birebir biçimsel replikası (miktarlar gerçek)."""
    trades = [
        _ut(1, ENTRY_OID, "BUY", "625", "0.1516", ENTRY_T, fee="0.02"),
        _ut(2, ENTRY_OID, "BUY", "700", "0.1516", ENTRY_T + 9, fee="0.01"),
        _ut(3, EXIT_OID, "SELL", "1325", "0.1486", EXIT_T,
            realized="-3.975", fee="0.03"),
    ]
    orders = {
        ("oid", ENTRY_OID): {"orderId": ENTRY_OID, "status": "FILLED",
                             "side": "BUY", "executedQty": "1325",
                             "time": ENTRY_T, "reduceOnly": False},
        ("oid", EXIT_OID): {"orderId": EXIT_OID, "status": "FILLED",
                            "side": "SELL", "executedQty": "1325",
                            "time": EXIT_T, "reduceOnly": True},
    }
    algo = {"algoId": ALGO_ID, "symbol": "ADAUSDT", "algoStatus": "FINISHED",
            "actualOrderId": EXIT_OID, "triggerPrice": "0.1486",
            "actualPrice": "0.1486"}
    income = [{"income": "0.01"}, {"income": "0.02"}, {"income": "0.02"}]
    return trades, orders, algo, income


class FakeBinance:
    def __init__(self, trades, orders, algo, income):
        self.trades = trades
        self.orders = orders
        self.algo = algo
        self.income = income
        self.income_params = None
        self.algo_ids_asked = []

    def _get(self, path, params=None, signed=False):
        p = dict(params or {})
        if path == "/fapi/v1/userTrades":
            rows = [r for r in self.trades
                    if r["time"] >= p.get("startTime", 0)
                    and r["id"] >= p.get("fromId", 0)]
            return rows
        if path == "/fapi/v1/allOrders":
            lo, hi = p.get("startTime", 0), p.get("endTime", 1 << 62)
            return [o for o in self.orders.values()
                    if o["orderId"] in {t["orderId"] for t in self.trades
                                        if lo <= t["time"] <= hi}]
        if path == "/fapi/v1/income":
            self.income_params = p
            return self.income
        raise AssertionError(f"beklenmeyen GET: {path}")

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        if orig_client_order_id is not None:
            return self.orders.get(("cid", orig_client_order_id),
                                   {"code": -2013, "msg": "Order does not exist"})
        return self.orders.get(("oid", order_id),
                               {"code": -2013, "msg": "Order does not exist"})

    def get_algo_order(self, algo_id=None, client_algo_id=None):
        self.algo_ids_asked.append(algo_id)
        if str(algo_id) == str(self.algo.get("algoId")):
            return dict(self.algo)
        return {"code": -1, "msg": "no such algo"}


def _executor():
    # BLOK14/15'teki ADA tam kaydı gibi: custom_info null, tam 62-karakter id.
    return {"executor_id": "HWWnd8nn2eyc" + "x" * 52, "custom_info": None,
            "trading_pair": "ADA-USDT", "created_at": "2026-11-17T17:05:39",
            "close_timestamp": 1794935514.188374, "net_pnl_quote": 0.0,
            "status": "TERMINATED"}


@pytest.fixture
def sl_ledger(tmp_path, monkeypatch):
    """SL yerleştirme defteri: ADAUSDT PLACED + algo_id (FAZ18 yazar)."""
    db = tmp_path / "sl_ledger.db"
    store = SlPlacementStore(str(db))
    store.init_schema()
    assert store.acquire("ADAUSDT", "SIDE:0.1509") == "ACQUIRED"
    store.mark_placed("ADAUSDT", "SIDE:0.1509", algo_id=ALGO_ID,
                      client_algo_id="YLTBQl7NB18Colm2p6D0kj")
    monkeypatch.setattr(at, "SL_LEDGER_DB", str(db))
    return db


class TestAnchorsuzAlgoYedegi:
    def test_ada_replikasi_net_pnl_uretilir(self, sl_ledger):
        trades, orders, algo, income = _ada_dataset()
        fake = FakeBinance(trades, orders, algo, income)
        net, close_s = at._recover_stale_executor_net_pnl(_executor(), fake)
        # net = realized(-3.975) - fee(0.02+0.01+0.03) + funding(0.05)
        assert net == pytest.approx(-3.985)
        assert close_s == pytest.approx(EXIT_T / 1000.0)
        assert fake.algo_ids_asked == [ALGO_ID]     # kimlik SL defterinden
        assert fake.income_params["startTime"] == ENTRY_T
        assert fake.income_params["endTime"] == EXIT_T

    def test_sl_defterinde_algo_id_yoksa_fail_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(at, "SL_LEDGER_DB", str(tmp_path / "bos.db"))
        trades, orders, algo, income = _ada_dataset()
        fake = FakeBinance(trades, orders, algo, income)
        with pytest.raises(ValueError, match="no SL placement algo_id"):
            at._recover_stale_executor_net_pnl(_executor(), fake)

    def test_algo_finished_degilse_fail_closed(self, sl_ledger):
        trades, orders, algo, income = _ada_dataset()
        algo["algoStatus"] = "CANCELED"             # SL tetiklenmeden iptal
        fake = FakeBinance(trades, orders, algo, income)
        with pytest.raises(ValueError, match="not FINISHED"):
            at._recover_stale_executor_net_pnl(_executor(), fake)

    def test_cikis_yaninda_baska_sell_belirsiz(self, sl_ledger):
        trades, orders, algo, income = _ada_dataset()
        trades.append(_ut(4, 999, "SELL", "10", "0.1480", EXIT_T - 5000))
        fake = FakeBinance(trades, orders, algo, income)
        with pytest.raises(ValueError, match="outside the SL algo order"):
            at._recover_stale_executor_net_pnl(_executor(), fake)

    def test_iki_giris_emri_belirsiz(self, sl_ledger):
        trades, orders, algo, income = _ada_dataset()
        # ikinci (yanlış) bir BUY emri: alım iki emre bölünmüş
        trades.insert(0, _ut(0, 555, "BUY", "100", "0.1510", ENTRY_T - 100))
        orders[("oid", 555)] = {"orderId": 555, "status": "FILLED", "side": "BUY",
                                "executedQty": "100", "time": ENTRY_T - 100,
                                "reduceOnly": False}
        fake = FakeBinance(trades, orders, algo, income)
        with pytest.raises(ValueError, match="more than one entry-side order"):
            at._recover_stale_executor_net_pnl(_executor(), fake)

    def test_giris_adayi_reduceonly_ise_red(self, sl_ledger):
        trades, orders, algo, income = _ada_dataset()
        orders[("oid", ENTRY_OID)]["reduceOnly"] = True
        fake = FakeBinance(trades, orders, algo, income)
        with pytest.raises(ValueError, match="marked reduceOnly"):
            at._recover_stale_executor_net_pnl(_executor(), fake)

    def test_miktar_mutabakati_bozuk_sa_red(self, sl_ledger):
        trades, orders, algo, income = _ada_dataset()
        trades[2]["qty"] = "1300"                   # eksik kapanış
        fake = FakeBinance(trades, orders, algo, income)
        with pytest.raises(ValueError, match="quantity mismatch"):
            at._recover_stale_executor_net_pnl(_executor(), fake)


class TestAnchorYolununKorunumu:
    def test_xnb_anchor_aynen_calisir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(at, "SL_LEDGER_DB", str(tmp_path / "bos.db"))
        trades, orders, algo, income = _ada_dataset()
        cid = "x-nbFAZ9abc"
        entry = dict(orders[("oid", ENTRY_OID)], origClientOrderId=cid)
        orders[("cid", cid)] = entry
        fake = FakeBinance(trades, orders, algo, income)
        ex = _executor()
        ex["custom_info"] = {"order_ids": [cid]}
        net, close_s = at._recover_stale_executor_net_pnl(ex, fake)
        assert net == pytest.approx(-3.985)
        assert fake.algo_ids_asked == []            # anchor varken yedek SORGULANMAZ

    def test_cift_anchor_mesaji_korunur(self, tmp_path, monkeypatch):
        monkeypatch.setattr(at, "SL_LEDGER_DB", str(tmp_path / "bos.db"))
        trades, orders, algo, income = _ada_dataset()
        fake = FakeBinance(trades, orders, algo, income)
        ex = _executor()
        ex["custom_info"] = {"a": ["x-nb1", "x-nb2"]}
        with pytest.raises(ValueError,
                           match="expected exactly one x-nb entry client ID"):
            at._recover_stale_executor_net_pnl(ex, fake)


class TestCreatedAtParse:
    def test_iso_naive_ve_epoch_saniye(self):
        assert at._executor_created_ms({"created_at": "2026-11-17T17:05:39"}) \
            == CREATED_S * 1000
        assert at._executor_created_ms({"created_at": CREATED_S}) \
            == CREATED_S * 1000
        assert at._executor_created_ms({"created_at": CREATED_S * 1000}) \
            == CREATED_S * 1000

    def test_boş_created_at_fail_closed(self):
        with pytest.raises(ValueError):
            at._executor_created_ms({})
