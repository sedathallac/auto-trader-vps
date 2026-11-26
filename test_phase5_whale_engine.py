"""Faz 5 RED/GREEN — balina motoru (whale_engine) ve WHALE_HUNTING bayrağı.

RED:   Eski kod (bayrak 'off') balina sinyallerini GÖRMEZDEN gelir: güçlü ters balina
       baskısında bile giriş (dry-run) devam eder; E[kâr] > maliyet kontrolü yoktur.
GREEN: Yeni kod E[kâr] > maliyet+risk primi kontrolü yapar; 'on' modunda YALNIZ reddeder
       (asla giriş tetiklemez); veri yoksa fail-closed; 'off' ek API çağrısı yapmaz.
"""
import sqlite3

import pytest

import auto_trader
import whale_engine as we
from whale_engine import WhaleSignal, WhaleTracker


# ------------------------------------------------------------------ yardımcılar
def _book(mid=100.0, n=30, tick=0.01, qty=100.0, bid_mult=1.0, ask_mult=1.0):
    bids = [(mid - tick * (i + 1), qty * bid_mult) for i in range(n)]
    asks = [(mid + tick * (i + 1), qty * ask_mult) for i in range(n)]
    return bids, asks


def _trades(buy_notional, sell_notional, n=20):
    out = []
    for _ in range(n):
        out.append({"p": "100", "q": str(buy_notional / n / 100.0), "m": False})
        out.append({"p": "100", "q": str(sell_notional / n / 100.0), "m": True})
    return out


def _inputs(bid_mult=1.0, ask_mult=1.0, buy=1000.0, sell=1000.0, oi=1000.0, symbol="XUSDT"):
    bids, asks = _book(bid_mult=bid_mult, ask_mult=ask_mult)
    return {"symbol": symbol, "bids": bids, "asks": asks,
            "trades": _trades(buy, sell), "oi": oi, "funding": 0.0}


def _edge(side_dir, sig, bids, asks, **kw):
    base = dict(sl_pct=0.01, tp_pct=0.015, atr_pct=0.01, drawdown=0.0, notional=25.0,
                bids=bids, asks=asks, fee_rt=0.0008)
    base.update(kw)
    return we.expected_edge(side_dir, sig, **base)


# ------------------------------------------------------------------ saf bileşenler
def test_obi_sign_and_bounds():
    b, a = _book(bid_mult=3.0)
    assert we.order_book_imbalance(b, a, 25) > 0.4
    b, a = _book(ask_mult=3.0)
    assert we.order_book_imbalance(b, a, 25) < -0.4
    assert we.order_book_imbalance([], [], 25) is None


def test_wall_detection_needs_both_mult_and_absolute_threshold():
    levels = [(100 - 0.01 * i, 100.0) for i in range(1, 20)]
    assert we.find_walls(levels, 8.0, 250_000.0) == []          # duvar yok
    levels[5] = (levels[5][0], 100.0 * 200)                      # ~2M notional
    walls = we.find_walls(levels, 8.0, 250_000.0)
    assert len(walls) == 1 and walls[0][0] == pytest.approx(levels[5][0])
    small = [(100 - 0.01 * i, 1.0) for i in range(1, 20)]
    small[3] = (small[3][0], 50.0)                               # 8x ama mutlak eşik altı
    assert we.find_walls(small, 8.0, 250_000.0) == []


def test_gap_detection():
    asks = [(100 + 0.01 * i, 10.0) for i in range(1, 15)]
    asks.append((101.5, 10.0))                                   # büyük sıçrama
    gaps = we.find_gaps(asks, 100.0, 3.0)
    assert gaps and gaps[0][2] > 100


def test_taker_flow_direction():
    cvd, *_ = we.taker_flow(_trades(9000, 1000), 1e9)
    assert cvd == pytest.approx(0.8)
    cvd, *_ = we.taker_flow(_trades(1000, 9000), 1e9)
    assert cvd == pytest.approx(-0.8)
    assert we.taker_flow([], 1e9)[0] is None


def test_vwap_slippage_grows_with_size_and_none_when_depth_short():
    _, asks = _book(qty=10.0)
    small = we.vwap_slippage(asks, 50.0)
    big = we.vwap_slippage(asks, 5000.0)
    assert 0 <= small < big
    assert we.vwap_slippage(asks, 1e9) is None


def test_wall_persistence_and_spoof_use_short_interval_samples_not_cross_call_memory():
    """Faz 9: duvar kalıcılığı/spoof artık aynı çağrıdaki 5 sn aralıklı örneklerden (ayrıntılı testler:
    test_phase9_spoof_and_probe.py). Tracker çağrılar arası duvar hafızası TUTMAZ."""
    assert not hasattr(WhaleTracker(), "update_walls")
    wall = [(98.855, 5_000_000.0 / 98.855)]
    book = lambda t, w: {"t": t, "bids": sorted(w + [(99.0 - 0.01 * i, 10.0) for i in range(30)], reverse=True),
                         "asks": [(100.1 + 0.01 * i, 10.0) for i in range(30)]}
    bid_p, ask_p, spoof = we.analyze_wall_samples([book(0, wall), book(5, wall), book(10, wall)])
    assert bid_p == [98.855] and not spoof                        # 3 örnekte de duvar → kalıcı
    bid_p, ask_p, spoof = we.analyze_wall_samples([book(0, wall), book(5, wall), book(10, [])])
    assert spoof                                                 # 10 sn içinde kayboldu = spoof


def test_oi_delta_first_sample_none():
    tr = WhaleTracker(clock=lambda: 0.0)
    assert tr.oi_delta("X", 1000.0) is None
    assert tr.oi_delta("X", 1020.0) == pytest.approx(0.02)


# ------------------------------------------------------------------ sinyal
def test_signal_bullish_and_bearish_pressure():
    tr = WhaleTracker()
    bull = we.compute_signal(_inputs(bid_mult=3.0, buy=9000, sell=1000), tr)
    assert bull.quality_ok and bull.direction == 1 and bull.strength > 0.3
    bear = we.compute_signal(_inputs(ask_mult=3.0, buy=1000, sell=9000), WhaleTracker())
    assert bear.quality_ok and bear.direction == -1 and bear.strength > 0.3


def test_signal_missing_data_is_fail_closed():
    sig = we.compute_signal({"symbol": "X", "bids": [], "asks": [], "trades": None}, WhaleTracker())
    assert not sig.quality_ok and sig.direction == 0


def test_fetch_inputs_error_returns_none():
    class Bad:
        def order_book(self, *a, **k):
            raise RuntimeError("429")
    assert we.fetch_whale_inputs(Bad(), "XUSDT") is None


# ------------------------------------------------------------------ RED/GREEN: E[kâr] > maliyet
def test_red_old_code_has_no_edge_check_and_ignores_whales(monkeypatch):
    """RED: bayrak 'off' (eski davranış) → güçlü ters balina baskısında bile giriş devam eder."""
    fake = _FakeBinance(_inputs(ask_mult=4.0, buy=500, sell=9500))
    _patch_open_coin(monkeypatch, fake)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "off")
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is True
    assert fake.whale_calls == 0        # off: balina verisi hiç istenmedi


def test_green_on_rejects_strong_opposing_whale(monkeypatch, tmp_path):
    """GREEN: aynı girdi, 'on' → LONG girişi ters (satış) balina baskısı nedeniyle REDDEDİLİR."""
    fake = _FakeBinance(_inputs(ask_mult=4.0, buy=500, sell=9500))
    _patch_open_coin(monkeypatch, fake)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "on")
    monkeypatch.setattr(we, "WHALE_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(auto_trader, "_WHALE_TRACKER", None)
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is False


def test_green_edge_cost_and_riskpremium_reduce_net_edge():
    b, a = _book(bid_mult=3.0)
    sig = we.compute_signal(_inputs(bid_mult=3.0, buy=9000, sell=1000), WhaleTracker())
    base = _edge(1, sig, b, a)
    assert _edge(1, sig, b, a, fee_rt=0.004).e_net < base.e_net            # maliyet↑ → E_net↓
    assert _edge(1, sig, b, a, drawdown=0.5, atr_pct=0.03).e_net < base.e_net   # d,σ↑ → RP↑
    assert _edge(1, sig, b, a, calibrated=True).e_net > base.e_net         # belirsizlik primi
    assert base.e_net == pytest.approx(base.e_profit - base.cost - base.risk_premium)


def test_green_aligned_whale_has_higher_edge_than_opposed():
    b, a = _book()
    bull = we.compute_signal(_inputs(bid_mult=3.0, buy=9000, sell=1000), WhaleTracker())
    bear = we.compute_signal(_inputs(ask_mult=3.0, buy=1000, sell=9000), WhaleTracker())
    assert _edge(1, bull, b, a).p_win > 0.5 > _edge(1, bear, b, a).p_win
    assert _edge(1, bull, b, a).e_net > _edge(1, bear, b, a).e_net


def test_green_tp_clamped_by_opposing_wall():
    b, a = _book()
    sig = WhaleSignal(1, 0.5, True, {"mid": 100.0, "ask_wall_px": 100.5, "bid_wall_px": None})
    e = _edge(1, sig, b, a, tp_pct=0.05)
    assert e.gain == pytest.approx(0.005 * 0.8)        # %0.5 mesafenin %80'i


def test_green_filter_blocks_when_net_edge_below_threshold():
    b, a = _book()
    weak = WhaleSignal(0, 0.0, True, {"mid": 100.0})
    allow, why = we.evaluate_filter(1, weak, _edge(1, weak, b, a))
    assert not allow and "E_net" in why


def test_green_filter_allows_aligned_strong_signal_with_positive_edge():
    b, a = _book()
    sig = WhaleSignal(1, 1.0, True, {"mid": 100.0})
    e = _edge(1, sig, b, a, sl_pct=0.01, tp_pct=0.03, atr_pct=0.003, calibrated=True)
    allow, _ = we.evaluate_filter(1, sig, e)
    assert allow and e.e_net >= we.WHALE_DEFAULTS["whale_min_net_edge_pct"]


def test_green_filter_fail_closed_without_data():
    allow, why = we.evaluate_filter(1, WhaleSignal(), None)
    assert not allow and "fail-closed" in why


def test_green_filter_never_adds_entries():
    """on = yalnız filtre: sinyal yönü ne olursa olsun evaluate_filter bool'dur, yön üretmez."""
    b, a = _book()
    sig = WhaleSignal(-1, 0.1, True, {"mid": 100.0})   # zayıf ters sinyal (blok eşiği altı)
    allow, _ = we.evaluate_filter(1, sig, _edge(1, sig, b, a))
    assert isinstance(allow, bool)


# ------------------------------------------------------------------ bayrak + config + ledger
def test_default_flag_off_and_config_in_risk_config():
    assert auto_trader.WHALE_HUNTING == "off"
    for k in we.WHALE_DEFAULTS:
        assert k in auto_trader.RISK_CONFIG


def test_compare_logs_and_records_but_does_not_block(monkeypatch, tmp_path):
    db = str(tmp_path / "w.db")
    fake = _FakeBinance(_inputs(ask_mult=4.0, buy=500, sell=9500))
    _patch_open_coin(monkeypatch, fake)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "compare")
    monkeypatch.setattr(we, "WHALE_DB", db)
    monkeypatch.setattr(auto_trader, "_WHALE_TRACKER", None)
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is True   # karar etkilenmedi
    rows = sqlite3.connect(db).execute("SELECT mode, allow, direction FROM whale_metrics").fetchall()
    assert rows == [("compare", 0, -1)]


def test_on_fail_closed_when_data_fetch_fails(monkeypatch, tmp_path):
    fake = _FakeBinance(None)
    _patch_open_coin(monkeypatch, fake)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "on")
    monkeypatch.setattr(we, "WHALE_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(auto_trader, "_WHALE_TRACKER", None)
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is False


def test_whale_metrics_separate_db_does_not_touch_risk_ledger(tmp_path):
    db = tmp_path / "w.db"
    assert we.record_metrics(db, "X", "compare", 1, WhaleSignal(), None, False, "t")
    tables = {r[0] for r in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"whale_metrics", "sqlite_sequence"}


# ------------------------------------------------------------------ open_coin sahte altyapı
def _coin():
    return {"symbol": "XUSDT", "lastPrice": "100", "quoteVolume": "1e9",
            "priceChangePercent": "-5", "_score": 0.9}


class _FakeBinance:
    """whale girdisi → order_book/aggTrades/OI/premiumIndex; open_coin'in diğer ihtiyaçları sahte."""

    def __init__(self, winputs):
        self.w = winputs
        self.whale_calls = 0

    def order_book(self, symbol, limit=20):
        if limit == 100:
            self.whale_calls += 1
            if self.w is None:
                raise RuntimeError("fetch failed")
            return {"bids": [[str(p), str(q)] for p, q in self.w["bids"]],
                    "asks": [[str(p), str(q)] for p, q in self.w["asks"]]}
        b, a = _book()
        return {"bids": [[str(p), str(q)] for p, q in b], "asks": [[str(p), str(q)] for p, q in a]}

    def _get(self, path, params=None, signed=False):
        if path == "/fapi/v1/aggTrades":
            return self.w["trades"]
        if path == "/fapi/v1/openInterest":
            return {"openInterest": str(self.w["oi"])}
        if path == "/fapi/v1/premiumIndex":
            return {"lastFundingRate": "0.0"}
        raise AssertionError(path)

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 1000.0


def _patch_open_coin(monkeypatch, fake):
    monkeypatch.setattr(we, "WHALE_PWIN_CALIBRATION", "/nonexistent/whale_pwin.json")   # cold-start izolasyonu
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    monkeypatch.setattr(auto_trader, "SCIENTIFIC_SIZING", "off")
    monkeypatch.setattr(auto_trader, "check_liquidity", lambda b, s: (True, "ok"))
    monkeypatch.setattr(auto_trader, "check_correlation_limit", lambda s, b: True)
    monkeypatch.setattr(auto_trader, "calculate_atr", lambda *a, **k: 0.01)
    monkeypatch.setattr(auto_trader, "get_real_slippage", lambda b, s: 0.0002)
    monkeypatch.setitem(auto_trader.RISK_CONFIG, "risk_guards_enabled", False)
