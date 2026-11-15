"""FAZ11 Katman 1 — coin kalite motoru testleri.

Her test tek bir iddiayı kanıtlar: normalizasyon bantları, bant puanlama,
yön-bağımlı funding cezası, sert veto, eksik-metrik fail-closed davranışı,
ağırlık yeniden dağıtımı ve üretim koduna bağlanma.
"""
import pytest

import auto_trader
import coin_quality as cq


GOOD = {
    "spread_bps": 1.5,
    "depth_usdt_1pct": 250_000.0,
    "quote_volume_24h": 400_000_000.0,
    "atr_pct": 0.012,
    "funding_rate": 0.00005,
    "open_interest_usdt": 80_000_000.0,
    "taker_buy_sell_ratio": 1.05,
    "price_change_24h_pct": 0.04,
}


# ------------------------------------------------------------------ normalizasyon
def test_spread_score_is_one_at_the_good_edge():
    assert cq.spread_score(1.0) == pytest.approx(1.0)


def test_spread_score_is_zero_at_the_bad_edge():
    assert cq.spread_score(20.0) == pytest.approx(0.0)


def test_spread_score_is_monotone_decreasing():
    vals = [cq.spread_score(x) for x in (1, 5, 10, 15, 20)]
    assert vals == sorted(vals, reverse=True)


def test_spread_score_handles_missing_value():
    assert cq.spread_score(None) is None


def test_depth_score_saturates_towards_one():
    assert cq.depth_score(50_000.0) == pytest.approx(0.5)
    assert cq.depth_score(5_000_000.0) > 0.98


def test_depth_score_is_zero_for_empty_book():
    assert cq.depth_score(0.0) == 0.0


def test_volume_score_rewards_larger_turnover():
    assert cq.volume_score(500_000_000.0) > cq.volume_score(50_000_000.0)


def test_volume_score_missing_is_none():
    assert cq.volume_score(None) is None


# ------------------------------------------------------------------ bant puanlama
def test_atr_band_scores_inside_the_band_as_one():
    assert cq.atr_band_score(0.003) == pytest.approx(1.0)
    assert cq.atr_band_score(0.012) == pytest.approx(1.0)
    assert cq.atr_band_score(0.020) == pytest.approx(1.0)


def test_atr_band_punishes_a_dead_coin():
    assert cq.atr_band_score(0.0015) == pytest.approx(0.5)


def test_atr_band_punishes_an_over_volatile_coin():
    assert cq.atr_band_score(0.030) == pytest.approx(0.5)
    assert cq.atr_band_score(0.040) == pytest.approx(0.0)


def test_atr_band_is_symmetric_around_the_band():
    assert cq.atr_band_score(0.0015) == pytest.approx(cq.atr_band_score(0.030))


def test_atr_band_rejects_non_positive_atr():
    assert cq.atr_band_score(0.0) == 0.0
    assert cq.atr_band_score(-0.01) == 0.0


# ------------------------------------------------------------------ funding
def test_funding_score_is_one_below_the_good_threshold():
    assert cq.funding_score(0.0001) == pytest.approx(1.0)


def test_funding_score_is_zero_at_the_bad_threshold():
    assert cq.funding_score(0.0005) == pytest.approx(0.0)


def test_funding_score_uses_absolute_value():
    assert cq.funding_score(-0.0005) == pytest.approx(0.0)


def test_funding_cost_is_directional_for_longs():
    assert cq.funding_cost_bps(0.0003, 1) == pytest.approx(3.0)


def test_funding_cost_is_a_credit_for_shorts_but_reported_as_zero():
    assert cq.funding_cost_bps(0.0003, -1) == 0.0


def test_funding_cost_scales_with_intervals():
    assert cq.funding_cost_bps(0.0001, 1, intervals=3) == pytest.approx(3.0)


def test_funding_cost_is_capped_at_the_bad_band():
    assert cq.funding_cost_bps(0.01, 1) == pytest.approx(5.0)


# ------------------------------------------------------------------ taker / change / OI
def test_taker_score_is_one_inside_the_healthy_band():
    assert cq.taker_score(1.0) == pytest.approx(1.0)
    assert cq.taker_score(0.8) == pytest.approx(1.0)
    assert cq.taker_score(1.25) == pytest.approx(1.0)


def test_taker_score_is_zero_at_extremes():
    assert cq.taker_score(0.67) == pytest.approx(0.0)
    assert cq.taker_score(1.50) == pytest.approx(0.0)


def test_taker_score_decays_between_band_and_extreme():
    assert 0.0 < cq.taker_score(1.40) < 1.0


def test_taker_score_rejects_non_positive_ratio():
    assert cq.taker_score(0.0) == 0.0


def test_change_score_is_one_below_ten_percent():
    assert cq.change_score(0.10) == pytest.approx(1.0)
    assert cq.change_score(-0.10) == pytest.approx(1.0)


def test_change_score_is_zero_at_twenty_percent():
    assert cq.change_score(0.20) == pytest.approx(0.0)


def test_oi_score_rewards_larger_open_interest():
    assert cq.oi_score(80_000_000.0) > cq.oi_score(1_000_000.0)


# ------------------------------------------------------------------ birleşik skor
def test_score_coin_accepts_a_healthy_market():
    q = cq.score_coin("BTCUSDT", GOOD)
    assert q.ok and q.score > 0.9 and not q.vetoes and not q.missing


def test_score_coin_is_bounded_between_zero_and_one():
    q = cq.score_coin("BTCUSDT", GOOD)
    assert 0.0 <= q.score <= 1.0


def test_score_coin_fails_closed_when_spread_is_missing():
    m = dict(GOOD)
    m.pop("spread_bps")
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and q.score == 0.0
    assert any("missing_critical" in v for v in q.vetoes)


def test_score_coin_fails_closed_when_depth_is_missing():
    m = dict(GOOD)
    m.pop("depth_usdt_1pct")
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and q.score == 0.0


def test_score_coin_redistributes_weights_for_optional_metrics():
    m = dict(GOOD)
    m.pop("funding_rate")
    q = cq.score_coin("BTCUSDT", m)
    assert q.ok and "funding" in q.missing and q.score > 0.9


def test_score_coin_vetoes_a_wide_spread():
    m = dict(GOOD, spread_bps=40.0)
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and any("spread_bps" in v for v in q.vetoes)


def test_score_coin_vetoes_a_thin_book():
    m = dict(GOOD, depth_usdt_1pct=1_000.0)
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and any("depth=" in v for v in q.vetoes)


def test_score_coin_vetoes_a_tiny_open_interest():
    m = dict(GOOD, open_interest_usdt=100_000.0)
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and any("oi=" in v for v in q.vetoes)


def test_score_coin_vetoes_an_extreme_24h_move():
    m = dict(GOOD, price_change_24h_pct=0.30)
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and any("change=" in v for v in q.vetoes)


def test_score_coin_vetoes_a_wild_atr():
    m = dict(GOOD, atr_pct=0.08)
    q = cq.score_coin("BTCUSDT", m)
    assert not q.ok and any("atr=" in v for v in q.vetoes)


def test_score_coin_refuses_below_min_quality():
    m = dict(GOOD, spread_bps=19.0, atr_pct=0.001, taker_buy_sell_ratio=1.49,
             price_change_24h_pct=0.19)
    q = cq.score_coin("BTCUSDT", m, min_quality=0.55)
    assert not q.ok and q.score < 0.55 and not q.vetoes


def test_score_coin_reports_funding_cost_for_the_direction():
    q_long = cq.score_coin("BTCUSDT", dict(GOOD, funding_rate=0.0004), direction=1)
    q_short = cq.score_coin("BTCUSDT", dict(GOOD, funding_rate=0.0004), direction=-1)
    assert q_long.funding_cost_bps == pytest.approx(4.0)
    assert q_short.funding_cost_bps == 0.0


def test_score_coin_with_no_metrics_is_refused():
    q = cq.score_coin("BTCUSDT", {})
    assert not q.ok and q.score == 0.0


def test_quality_as_dict_is_json_safe():
    d = cq.score_coin("BTCUSDT", GOOD).as_dict()
    assert d["symbol"] == "BTCUSDT" and isinstance(d["components"], dict)
    assert all(v is None or isinstance(v, float) for v in d["components"].values())


# ------------------------------------------------------------------ defter yardımcıları
def test_spread_bps_from_book_computes_two_bps():
    assert cq.spread_bps_from_book([["99.99", "1"]], [["100.01", "1"]]) == pytest.approx(2.0)


def test_spread_bps_from_book_rejects_crossed_book():
    assert cq.spread_bps_from_book([["101", "1"]], [["100", "1"]]) is None


def test_spread_bps_from_book_rejects_empty_book():
    assert cq.spread_bps_from_book([], []) is None


def test_depth_within_pct_sums_both_sides_inside_the_band():
    bids = [["99.9", "100"], ["98.0", "100"]]
    asks = [["100.1", "100"], ["102.0", "100"]]
    d = cq.depth_within_pct(bids, asks, 0.01)
    assert d == pytest.approx(99.9 * 100 + 100.1 * 100)


def test_depth_within_pct_ignores_malformed_levels():
    d = cq.depth_within_pct([["99.9", "100"], ["bad", "1"]], [["100.1", "100"]], 0.01)
    assert d == pytest.approx(99.9 * 100 + 100.1 * 100)


def test_taker_ratio_from_klines_uses_taker_buy_column():
    kl = [["0", "0", "0", "0", "0", "100", "0", "0", "0", "60"]]
    assert cq.taker_ratio_from_klines(kl) == pytest.approx(1.5)


def test_taker_ratio_from_klines_returns_none_without_sell_volume():
    kl = [["0", "0", "0", "0", "0", "100", "0", "0", "0", "100"]]
    assert cq.taker_ratio_from_klines(kl) is None


# ------------------------------------------------------------------ sıralama / kapı
def test_rank_orders_by_quality_and_pushes_unknown_last():
    items = [{"CQ": 0.4}, {"CQ": None}, {"CQ": 0.9}]
    assert [i["CQ"] for i in cq.rank(items)] == [0.9, 0.4, None]


def test_quality_gate_refuses_when_quality_is_unknown():
    ok, reason = cq.quality_gate({"symbol": "X"})
    assert not ok and reason == "no_quality_data"


def test_quality_gate_refuses_on_veto():
    ok, reason = cq.quality_gate({"CQ": 0.9, "quality_vetoes": ["spread_bps=40>25"]})
    assert not ok and reason.startswith("veto:")


def test_quality_gate_refuses_below_threshold():
    ok, reason = cq.quality_gate({"CQ": 0.3, "quality_vetoes": []})
    assert not ok and "cq=0.300" in reason


def test_quality_gate_allows_a_clean_coin():
    ok, reason = cq.quality_gate({"CQ": 0.8, "quality_vetoes": []})
    assert ok and reason == "cq=0.800"


# ------------------------------------------------------------------ üretim bağlantısı
class FakeBinance:
    def __init__(self, spread=0.02, depth_qty=1000.0, funding="0.00005",
                 oi="1000000", taker_buy="60"):
        self.spread = spread
        self.depth_qty = depth_qty
        self.funding = funding
        self.oi = oi
        self.taker_buy = taker_buy
        self.calls = []

    def order_book(self, symbol, limit=20):
        self.calls.append("order_book")
        return {"bids": [[str(100 - self.spread / 2), str(self.depth_qty)]],
                "asks": [[str(100 + self.spread / 2), str(self.depth_qty)]]}

    def funding_rate(self, symbol):
        self.calls.append("funding_rate")
        return {"lastFundingRate": self.funding}

    def open_interest(self, symbol):
        self.calls.append("open_interest")
        return {"openInterest": self.oi}

    def taker_klines(self, symbol, interval="5m", limit=1):
        self.calls.append("taker_klines")
        return [["0", "0", "0", "0", "0", "100", "0", "0", "0", self.taker_buy]]


TICKER = {"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "900000000",
          "priceChangePercent": "3.0"}


def test_attach_coin_quality_fills_every_field():
    out = auto_trader._attach_coin_quality(FakeBinance(), "BTCUSDT", TICKER, None, 0.012)
    assert out["CQ"] is not None and out["quality_ok"] is True
    assert out["quality_vetoes"] == [] and out["funding_cost_bps"] >= 0.0


def test_attach_coin_quality_uses_the_cache_on_second_call():
    from scan_engine import MetricCache
    b, cache = FakeBinance(), MetricCache()
    auto_trader._attach_coin_quality(b, "BTCUSDT", TICKER, cache, 0.012)
    first = len(b.calls)
    auto_trader._attach_coin_quality(b, "BTCUSDT", TICKER, cache, 0.012)
    assert len(b.calls) == first


def test_attach_coin_quality_survives_a_broken_exchange():
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    out = auto_trader._attach_coin_quality(Broken(), "BTCUSDT", TICKER)
    assert out["CQ"] == 0.0 and out["quality_ok"] is False
    assert any("missing_critical" in v for v in out["quality_vetoes"])


def test_attach_coin_quality_vetoes_a_wide_spread():
    out = auto_trader._attach_coin_quality(FakeBinance(spread=0.5), "BTCUSDT", TICKER, None, 0.012)
    assert out["quality_ok"] is False and out["quality_vetoes"]


def test_risk_config_carries_the_quality_threshold():
    assert auto_trader.RISK_CONFIG["min_coin_quality"] == pytest.approx(cq.MIN_QUALITY)
