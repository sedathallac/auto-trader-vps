"""FAZ14n — BinanceAPI market emir yolu.

Kök neden: `BinanceAPI` sınıfında market emir metodu yoktu. Drift temizliği
`api.futures_create_order(...)` çağırınca `AttributeError: 'BinanceAPI' object has no
attribute 'futures_create_order'` alınıyordu; pozisyonlar kapatılamıyor, drift döngüsü
sürüyordu. `market_order` ve `close_position` eklendi.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_trader


class _Rec:
    def __init__(self, resp=None):
        self.calls = []
        self.resp = resp or {"status": "FILLED", "avgPrice": "1.0"}

    def __call__(self, path, params, method="POST"):
        self.calls.append((path, dict(params), method))
        return self.resp


def _api():
    return auto_trader.BinanceAPI("k", "s", base="https://fapi.binance.com")


def test_market_order_posts_correct_path_and_params():
    api, rec = _api(), _Rec()
    api._post = rec
    api.market_order("NEARUSDT", "BUY", 7.0)
    path, params, method = rec.calls[0]
    assert path == "/fapi/v1/order"
    assert method == "POST"
    assert params["symbol"] == "NEARUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "MARKET"
    assert params["quantity"] == "7.00000000"
    assert params["reduceOnly"] == "true"
    assert params["newOrderRespType"] == "RESULT"


def test_market_order_accepts_hummingbot_side_convention():
    api, rec = _api(), _Rec()
    api._post = rec
    api.market_order("UNIUSDT", 1, 4.0)
    api.market_order("UNIUSDT", 2, 4.0)
    assert rec.calls[0][1]["side"] == "BUY"
    assert rec.calls[1][1]["side"] == "SELL"


def test_market_order_rejects_invalid_side():
    api = _api()
    api._post = _Rec()
    for bad in (-1, 0, 3, "HOLD"):
        with pytest.raises(ValueError, match="invalid side"):
            api.market_order("NEARUSDT", bad, 1.0)


def test_market_order_can_disable_reduce_only():
    api, rec = _api(), _Rec()
    api._post = rec
    api.market_order("NEARUSDT", "SELL", 1.0, reduce_only=False)
    assert "reduceOnly" not in rec.calls[0][1]


def test_close_position_picks_opposite_side():
    api, rec = _api(), _Rec()
    api._post = rec
    api.close_position("NEARUSDT", -7.0)      # SHORT → BUY
    api.close_position("UNIUSDT", 4.0)        # LONG  → SELL
    assert rec.calls[0][1]["side"] == "BUY" and rec.calls[0][1]["quantity"] == "7.00000000"
    assert rec.calls[1][1]["side"] == "SELL" and rec.calls[1][1]["quantity"] == "4.00000000"
    assert all(c[1]["reduceOnly"] == "true" for c in rec.calls)


def test_close_position_zero_is_noop():
    api, rec = _api(), _Rec()
    api._post = rec
    r = api.close_position("NEARUSDT", 0)
    assert r["status"] == "NOOP"
    assert rec.calls == []


def test_no_futures_create_order_attribute_needed():
    """Eski hatalı çağrı artık gerekmiyor: sınıf kendi market yolunu sunuyor."""
    api = _api()
    assert hasattr(api, "market_order") and hasattr(api, "close_position")
    assert not hasattr(api, "futures_create_order")
