"""Faz 4 RED/GREEN — hiyerarşik tarama motoru + Kalite Skoru (Q).

Kapsar: MetricCache hit/miss/TTL, tier due/not-due, volatilite orta-bant eğrisi,
Q monotonluğu + normalize sınırları, adaptör uçtan uca (cache/zamanlama), ve
HIERARCHICAL_SCAN 'off' iken davranış değişmezliği (scan_top intact).
"""
import pytest

import auto_trader
from scan_engine import (
    Q_WEIGHTS,
    TIERS,
    MetricCache,
    compute_quality_scores,
    due_tiers,
    volatility_score,
)


# ------------------------------------------------------------------ MetricCache
def test_cache_hit_within_ttl_and_miss_after():
    t = {"now": 1000.0}
    c = MetricCache(clock=lambda: t["now"])
    c.put("k", 42)
    assert c.get("k", ttl=10) == 42          # hit
    t["now"] = 1009.9
    assert c.get("k", ttl=10) == 42          # hâlâ TTL içinde
    t["now"] = 1011.0
    assert c.get("k", ttl=10) is None        # TTL aştı → miss


def test_cache_get_or_fetch_calls_fn_once():
    t = {"now": 0.0}
    c = MetricCache(clock=lambda: t["now"])
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "v"

    assert c.get_or_fetch("k", 5, fn) == "v"
    assert c.get_or_fetch("k", 5, fn) == "v"   # cache hit → fn tekrar çağrılmaz
    assert calls["n"] == 1
    t["now"] = 6.0
    assert c.get_or_fetch("k", 5, fn) == "v"   # TTL aştı → fn yeniden
    assert calls["n"] == 2


# -------------------------------------------------------------------- due_tiers
def test_due_tiers_never_run_all_due():
    assert set(t.name for t in due_tiers(now=1000.0, last_run={})) == {"hot", "warm", "cold"}


def test_due_tiers_respects_interval():
    now = 10_000.0
    last = {"hot": now - 301, "warm": now - 100, "cold": now - 100}
    due = {t.name for t in due_tiers(now, last)}
    assert "hot" in due        # 301 ≥ 300 → vade geldi
    assert "warm" not in due   # 100 < 900
    assert "cold" not in due   # 100 < 3600


# ------------------------------------------------------------- volatility_score
def test_volatility_midband_full_score():
    assert volatility_score(0.02) == 1.0        # %2 orta-bant
    assert volatility_score(0.015) == 1.0       # alt sınır
    assert volatility_score(0.04) == 1.0        # üst sınır


def test_volatility_falls_off_outside_band():
    assert volatility_score(0.0075) == pytest.approx(0.5)   # lo'nun yarısı → 0.5
    assert volatility_score(0.06) == pytest.approx(0.5)     # hi + hi/2 → 0.5
    assert volatility_score(0.08) == pytest.approx(0.0)     # ~2·hi → 0
    assert volatility_score(0.0) == 0.0                     # fail-safe
    assert volatility_score(None) == 0.0


def test_volatility_extreme_high_is_penalized_vs_midband():
    assert volatility_score(0.02) > volatility_score(0.07)  # aşırı vol cezalı


# ----------------------------------------------------- compute_quality_scores Q
def _raw(symbol, liq, mom, atr, vc):
    return {"symbol": symbol, "liquidity": liq, "momentum": mom,
            "atr_pct": atr, "volume_conf": vc}


def test_Q_empty_returns_empty():
    assert compute_quality_scores([]) == []


def test_Q_increases_with_liquidity():
    raw = [_raw("A", 100, 5, 0.02, 1.0), _raw("B", 200, 5, 0.02, 1.0)]
    out = {r["symbol"]: r["Q"] for r in compute_quality_scores(raw)}
    assert out["B"] > out["A"]


def test_Q_increases_with_momentum_and_volume_conf():
    raw = [_raw("A", 100, 3, 0.02, 1.0), _raw("B", 100, 9, 0.02, 1.0)]
    out = {r["symbol"]: r["Q"] for r in compute_quality_scores(raw)}
    assert out["B"] > out["A"]
    raw2 = [_raw("A", 100, 5, 0.02, 1.0), _raw("B", 100, 5, 0.02, 3.0)]
    out2 = {r["symbol"]: r["Q"] for r in compute_quality_scores(raw2)}
    assert out2["B"] > out2["A"]


def test_Q_midband_volatility_beats_extreme():
    raw = [_raw("MID", 100, 5, 0.02, 1.0), _raw("EXTREME", 100, 5, 0.09, 1.0)]
    out = {r["symbol"]: r["Q"] for r in compute_quality_scores(raw)}
    assert out["MID"] > out["EXTREME"]


def test_Q_sorted_desc_bounds_and_fields_preserved():
    raw = [_raw("A", 50, 5, 0.02, 1.0), _raw("B", 200, 9, 0.02, 2.0)]
    out = compute_quality_scores(raw)
    assert [r["symbol"] for r in out] == ["B", "A"]       # Q'ya göre azalan
    for r in out:
        assert 0.0 <= r["Q"] <= 1.0 + 1e-9                # ağırlık toplamı 1 → [0,1]
        assert "vol_score" in r and "symbol" in r         # alanlar korunur/eklenir


# --------------------------------------------------- adaptör uçtan uca (auto_trader)
class FakeBinance:
    def __init__(self):
        self.calls = {"tickers": 0, "order_book": 0, "klines": 0}

    def tickers(self):
        self.calls["tickers"] += 1
        return [
            {"symbol": "AAAUSDT", "quoteVolume": "500000000", "priceChangePercent": "6"},
            {"symbol": "BBBUSDT", "quoteVolume": "300000000", "priceChangePercent": "4"},
            {"symbol": "CCCBTC", "quoteVolume": "900000000", "priceChangePercent": "5"},   # USDT değil → ele
            {"symbol": "DDDUSDT", "quoteVolume": "1000000", "priceChangePercent": "5"},    # vol<200M → ele
        ]

    def order_book(self, symbol, limit=20):
        self.calls["order_book"] += 1
        return {"bids": [["100", "10"], ["99.9", "5"]],
                "asks": [["100.1", "10"], ["100.2", "5"]]}

    def _get(self, path, params=None, signed=False):
        if "klines" in path:
            self.calls["klines"] += 1
            return [[0, 0, "102", "98", "100", str(10 + i)] for i in range(25)]
        return {}


@pytest.fixture(autouse=True)
def _reset_scan_state():
    auto_trader._SCAN_CACHE = None
    auto_trader._SCAN_LAST_RUN.clear()
    yield
    auto_trader._SCAN_CACHE = None
    auto_trader._SCAN_LAST_RUN.clear()


def test_adapter_returns_q_sorted_pool_then_not_due():
    b = FakeBinance()
    out = auto_trader.run_tiered_scan(b)
    # AAA/BBB filtreyi geçer; CCCBTC ve DDDUSDT elenir
    syms = [r["symbol"] for r in out]
    assert set(syms) == {"AAAUSDT", "BBBUSDT"}
    assert all("Q" in r for r in out)
    assert out == sorted(out, key=lambda r: r["Q"], reverse=True)
    # ikinci çağrı: tüm katmanlar az önce çalıştı → vade yok → []
    assert auto_trader.run_tiered_scan(b) == []


def test_adapter_uses_real_depth_for_liquidity():
    b = FakeBinance()
    auto_trader.run_tiered_scan(b)
    assert b.calls["order_book"] >= 1   # hot katman gerçek depth kullanır


# ------------------------------------------------- 'off' davranış değişmezliği
def test_default_flag_off():
    assert auto_trader.HIERARCHICAL_SCAN == "off"


def test_scan_top_intact():
    b = FakeBinance()
    picks = auto_trader.scan_top(b, 5)
    assert [p["symbol"] for p in picks] == ["AAAUSDT", "BBBUSDT"]  # eski 2-faktör sıralama
    assert all("Q" not in p for p in picks)                        # scan_top Q üretmez
