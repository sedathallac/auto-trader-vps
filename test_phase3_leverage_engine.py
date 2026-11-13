"""Faz 3 RED/GREEN — bilimsel kaldıraç + boyutlandırma (leverage_engine).

RED:   Eski formül (auto_trader.calculate_dynamic_leverage) yalnızca ATR'ye bakar;
       sinyal güveni (q) ve drawdown (d) DEĞİŞSE bile çıktı aynı kalır (boşluk).
GREEN: Yeni bilimsel formül q, d, σ ve Kelly tavanını içerir; eski↔yeni çıktı farklıdır;
       margin = S/L tutarlıdır; q trade_ledger rolling win-rate'inden türetilir.
"""
import sqlite3

import pytest

import auto_trader
from leverage_engine import (
    LEV_ENGINE_CONFIG,
    SignalStats,
    kelly_ceiling,
    margin_for,
    scientific_leverage,
    scientific_position_size,
    signal_stats_from_ledger,
)


def _stats(q, f_star, n=30, reliable=True):
    """Pür-fonksiyon testleri için doğrudan SignalStats kur."""
    return SignalStats(n=n, p=q, b=2.0, f_star=f_star, q=q, reliable=reliable)


# --------------------------------------------------------------------------- RED
def test_red_old_leverage_ignores_confidence_and_drawdown():
    """RED: eski formül sadece ATR'ye bakar — q/d ne olursa olsun aynı L.
    (Yeni formülün kapattığı boşluk budur.)"""
    atr = 0.02
    l1 = auto_trader.calculate_dynamic_leverage(atr)
    l2 = auto_trader.calculate_dynamic_leverage(atr)
    assert l1 == l2  # eski formülde q/d girdisi YOK; değiştiremezsin


# ------------------------------------------------------------------------- GREEN
def test_green_old_vs_new_differ():
    """GREEN: aynı ATR'de eski ve yeni formül FARKLI L üretir (kanıt)."""
    atr = 0.02
    old = auto_trader.calculate_dynamic_leverage(atr)          # clip(round(3/2),1,5)=2
    new = scientific_leverage(atr, _stats(q=0.8, f_star=0.9), drawdown=0.0)
    assert old != pytest.approx(new, abs=0.01)


def test_green_leverage_increases_with_confidence():
    """GREEN: güven (q, f*) arttıkça L artar."""
    atr = 0.03
    low = scientific_leverage(atr, _stats(q=0.3, f_star=0.1), drawdown=0.0)
    high = scientific_leverage(atr, _stats(q=0.8, f_star=0.9), drawdown=0.0)
    assert high > low


def test_green_leverage_decreases_with_drawdown():
    """GREEN: drawdown (d) arttıkça L düşer (1+d paydası)."""
    atr = 0.03
    st = _stats(q=0.5, f_star=0.9)  # yüksek f* -> Kelly tavanı bağlamaz (ceiling~hi)
    no_dd = scientific_leverage(atr, st, drawdown=0.0)
    with_dd = scientific_leverage(atr, st, drawdown=0.5)
    assert with_dd < no_dd


def test_green_leverage_decreases_with_volatility():
    """GREEN: volatilite (σ) arttıkça L düşer (σ⁻¹)."""
    st = _stats(q=0.5, f_star=0.9)
    lo_vol = scientific_leverage(0.01, st, drawdown=0.0)
    hi_vol = scientific_leverage(0.05, st, drawdown=0.0)
    assert hi_vol < lo_vol


def test_green_kelly_cap_no_edge_floors_leverage():
    """GREEN: kenar yoksa (f*=0) Kelly tavanı L'yi tabana (lo) indirir."""
    st = _stats(q=0.5, f_star=0.0)
    assert kelly_ceiling(st) == pytest.approx(LEV_ENGINE_CONFIG["lo"])
    assert scientific_leverage(0.01, st, drawdown=0.0) == pytest.approx(LEV_ENGINE_CONFIG["lo"])


def test_green_leverage_clipped_to_bounds():
    """GREEN: aşırı küçük σ'da bile L üst sınırı (hi) aşmaz."""
    st = _stats(q=1.0, f_star=0.9)
    L = scientific_leverage(0.0001, st, drawdown=0.0)
    assert L <= LEV_ENGINE_CONFIG["hi"]


def test_green_leverage_sigma_nonpositive_failsafe():
    """GREEN: σ≤0 → fail-safe L=lo."""
    assert scientific_leverage(0.0, _stats(0.8, 0.9), 0.0) == pytest.approx(LEV_ENGINE_CONFIG["lo"])


def test_green_size_increases_with_confidence_decreases_with_vol():
    """GREEN: S güvenle (q) artar, volatiliteyle (σ) azalır."""
    s_lowq = scientific_position_size(1000, 0.015, 0.03, _stats(0.3, 0.9), 5.5, 1e9)
    s_highq = scientific_position_size(1000, 0.015, 0.03, _stats(0.9, 0.9), 5.5, 1e9)
    assert s_highq > s_lowq
    s_lovol = scientific_position_size(1000, 0.015, 0.01, _stats(0.5, 0.9), 5.5, 1e9)
    s_hivol = scientific_position_size(1000, 0.015, 0.05, _stats(0.5, 0.9), 5.5, 1e9)
    assert s_hivol < s_lovol


def test_green_size_kelly_caps_risk():
    """GREEN: r_eff = min(r, fractional-Kelly). f_used < r iken S daha küçük olur."""
    # f_star düşük -> f_used = 0.25*f_star küçük -> r_eff = f_used < r(0.015)
    small_edge = _stats(q=0.5, f_star=0.02)   # f_used = 0.25*0.02 = 0.005 < 0.015
    big_edge = _stats(q=0.5, f_star=0.9)       # f_used = 0.225 -> r_eff = r
    s_small = scientific_position_size(1000, 0.015, 0.03, small_edge, 5.5, 1e9)
    s_big = scientific_position_size(1000, 0.015, 0.03, big_edge, 5.5, 1e9)
    assert s_small < s_big


def test_green_size_clipped_and_failsafe():
    """GREEN: S min/max'e clip'lenir; σ≤0/bakiye≤0 → min_notional."""
    assert scientific_position_size(1e9, 0.015, 0.001, _stats(1.0, 0.9), 5.5, 25.0) == 25.0
    assert scientific_position_size(0, 0.015, 0.03, _stats(0.5, 0.9), 5.5, 25.0) == 5.5
    assert scientific_position_size(1000, 0.015, 0.0, _stats(0.5, 0.9), 5.5, 25.0) == 5.5


def test_green_margin_consistency():
    """GREEN: birleştirme tutarlılığı margin = S / L."""
    st = _stats(q=0.6, f_star=0.5)
    S = scientific_position_size(1000, 0.015, 0.03, st, 5.5, 25.0)
    L = scientific_leverage(0.03, st, 0.0)
    assert margin_for(S, L) == pytest.approx(S / L)


# ------------------------------------------------- q/f* trade_ledger'dan türetme
def _ledger(tmp_path, pnls):
    db = tmp_path / "risk_ledger.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY, "
                 "trading_pair TEXT, net_pnl_quote REAL, closed_at TEXT, "
                 "recorded_at TEXT NOT NULL, source TEXT)")
    for i, pnl in enumerate(pnls):
        conn.execute("INSERT INTO trade_ledger VALUES (?,?,?,?,?, 'sync')",
                     (f"e{i}", "BTCUSDT", pnl, None, f"2026-11-12T00:00:{i:02d}"))
    conn.commit(); conn.close()
    return str(db)


def test_green_stats_from_ledger_winrate_and_kelly(tmp_path):
    """GREEN: q=win-rate, b=payoff, f*=(bp−(1−p))/b trade_ledger'dan türetilir."""
    db = _ledger(tmp_path, [2.0] * 18 + [-1.0] * 12)  # p=18/30=0.6, b=2
    st = signal_stats_from_ledger(db)
    assert st.reliable and st.n == 30
    assert st.p == pytest.approx(0.6)
    assert st.b == pytest.approx(2.0)
    assert st.f_star == pytest.approx((2.0 * 0.6 - 0.4) / 2.0)  # 0.4
    assert st.q == pytest.approx(0.6)


def test_red_insufficient_history_fallback(tmp_path):
    """RED: yetersiz geçmiş (n<min_sample) → güvenilmez fallback (q=0.5, f*=0)."""
    db = _ledger(tmp_path, [2.0, -1.0, 2.0])  # n=3 < 20
    st = signal_stats_from_ledger(db)
    assert not st.reliable
    assert st.q == pytest.approx(LEV_ENGINE_CONFIG["q_fallback"])
    assert st.f_star == 0.0
    # cold-start: veri yokken tabana ÇÖKMEZ, 2x-3x bandında kalır
    lev = scientific_leverage(0.02, st, 0.0)
    assert LEV_ENGINE_CONFIG["cold_start_lev_lo"] <= lev <= LEV_ENGINE_CONFIG["cold_start_lev_hi"]


def test_green_no_losses_caps_payoff(tmp_path):
    """GREEN: hiç kayıp yoksa b, b_cap ile sınırlanır (sonsuz kaldıraç yok)."""
    db = _ledger(tmp_path, [1.5] * 25)  # tüm kazanç
    st = signal_stats_from_ledger(db)
    assert st.p == pytest.approx(1.0)
    assert st.b == pytest.approx(LEV_ENGINE_CONFIG["b_cap"])
    assert st.f_star == pytest.approx(1.0)  # (10*1 - 0)/10


# ------------------------------------------------------------ Cold-start (n=0)
def test_green_cold_start_size_and_leverage_near_legacy():
    """GREEN: reliable=False → S 12-15 USDT, L 2x-3x (eski sisteme yakın başlangıç)."""
    cold = _stats(q=0.5, f_star=0.0, n=0, reliable=False)
    cfg = LEV_ENGINE_CONFIG
    for atr in (0.0035, 0.01, 0.02, 0.05):
        s = scientific_position_size(2500, 0.015, atr, cold, 5.5, 25.0)
        lev = scientific_leverage(atr, cold, 0.0)
        assert cfg["cold_start_min_notional"] <= s <= cfg["cold_start_max_notional"]
        assert cfg["cold_start_lev_lo"] <= lev <= cfg["cold_start_lev_hi"]


def test_green_cold_start_respects_max_notional():
    """GREEN: cold-start S, çağıranın max_notional'ını aşmaz (min'e rağmen)."""
    cold = _stats(q=0.5, f_star=0.0, n=0, reliable=False)
    assert scientific_position_size(2500, 0.015, 0.02, cold, 5.5, 10.0) == 10.0


def test_green_reliable_no_edge_still_floors():
    """GREEN: reliable=True ve kenar yok → eski muhafazakâr davranış (cold-start bandı YOK)."""
    st = _stats(q=0.5, f_star=0.0, reliable=True)
    assert scientific_leverage(0.01, st, 0.0) == pytest.approx(LEV_ENGINE_CONFIG["lo"])
    assert scientific_position_size(1000, 0.015, 0.03, st, 5.5, 25.0) == 5.5
