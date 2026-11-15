"""FAZ14o — kaldıraç beyni coine göre karar verir, tavan 20.

Kök neden: `c` global sabitti (0.02). L_vol = c/σ olduğu için tavan 20 olsa da hiçbir
coin 5x'i geçemiyordu (20x için σ=0.001 gerekir). Ölçüm: BTC 5x, ETH 3x, UNI 2x,
ASTER 1x — hepsi `binding=volatility`. Artık c_eff = c · (floor + gain·quality) ve
coinin kendi σ'sı ile kalite skoru kararı belirler.
"""
import importlib

import pytest

import leverage_policy as lp

COINS = [('BTC', 0.40, 0.859), ('ETH', 0.60, 0.820), ('AAVE', 0.77, 0.790),
         ('UNI', 1.00, 0.817), ('INJ', 1.08, 0.780), ('ASTER', 2.10, 0.700)]


def _lev(atr_pct, quality=None, c=0.04, stage=20, exchange=20, drawdown=0.0):
    s = atr_pct / 100.0
    sl = min(max(s * 1.5, 0.005), 0.05)
    return lp.compute_leverage(sigma=s, sl_pct=sl, drawdown=drawdown, f_star=None,
                               stage_cap=stage, exchange_max=exchange,
                               quality=quality, cfg={'c': c})


def test_ceiling_is_20():
    assert lp.LEVERAGE_DEFAULTS["hi"] == 20.0


def test_ceiling_20_is_reachable_for_a_good_coin():
    """σ=0.4% ve kalite 1.0 → c_eff=0.08 → L_vol=20 → 20x."""
    d = _lev(0.40, quality=1.0)
    assert d.caps_detail["c_eff"] == pytest.approx(0.08, abs=1e-9)
    assert d.caps_detail["volatility"] == pytest.approx(20.0, abs=1e-6)
    assert d.leverage == 20, d.leverage


def test_each_coin_gets_its_own_leverage():
    """Tek global sabit yerine coin başına farklı karar."""
    got = {name: _lev(atr, q).leverage for name, atr, q in COINS}
    assert got == {'BTC': 19, 'ETH': 12, 'AAVE': 9, 'UNI': 7, 'INJ': 7, 'ASTER': 3}, got
    assert len(set(got.values())) >= 5, f'kararlar farklilasmali: {got}'


def test_leverage_is_monotonic_in_volatility():
    """Aynı kalitede ATR arttıkça kaldıraç azalır."""
    levs = [_lev(atr, 0.8).leverage for _, atr, _ in COINS]
    assert levs == sorted(levs, reverse=True), levs


def test_higher_quality_gets_higher_leverage():
    low, high = _lev(0.60, 0.30), _lev(0.60, 0.95)
    assert high.leverage > low.leverage, (low.leverage, high.leverage)
    assert high.caps_detail["c_eff"] > low.caps_detail["c_eff"]


def test_quality_none_preserves_legacy_behaviour():
    """quality verilmezse ölçek 1.0 → eski davranış birebir."""
    d = _lev(0.60, None, c=0.02)
    assert d.caps_detail["quality_scale"] == pytest.approx(1.0)
    assert d.caps_detail["c_eff"] == pytest.approx(0.02, abs=1e-9)
    assert d.leverage == 3, d.leverage


def test_liquidation_cap_still_guards_high_leverage():
    """20x tavanı tasfiye güvenliğini delemez: SL çok genişse clamp devreye girer."""
    d = lp.compute_leverage(sigma=0.0001, sl_pct=0.20, drawdown=0.0, f_star=None,
                            stage_cap=20, exchange_max=20, quality=1.0, cfg={'c': 0.04})
    cap = lp.liquidation_cap(0.20)
    assert d.leverage <= cap, (d.leverage, cap)
    assert "liq_clamp" in d.caps or d.leverage <= int(cap)


def test_drawdown_penalty_still_applies():
    flat, dd = _lev(0.60, 0.82, drawdown=0.0), _lev(0.60, 0.82, drawdown=0.5)
    assert dd.leverage < flat.leverage, (flat.leverage, dd.leverage)
    assert "drawdown" in dd.caps


def test_stage_cap_still_binds_below_ceiling():
    d = _lev(0.40, 1.0, stage=5, exchange=5)
    assert d.leverage == 5 and "stage" in d.caps


def test_quality_scale_env_configurable(monkeypatch):
    monkeypatch.setenv("LEVERAGE_QUALITY_GAIN", "2.0")
    monkeypatch.setenv("LEVERAGE_QUALITY_FLOOR", "0.5")
    mod = importlib.reload(lp)
    try:
        assert mod.LEVERAGE_DEFAULTS["quality_gain"] == pytest.approx(2.0)
        assert mod.LEVERAGE_DEFAULTS["quality_floor"] == pytest.approx(0.5)
        assert mod.quality_scale(0.0) == pytest.approx(0.5)
        assert mod.quality_scale(1.0) == pytest.approx(2.5)
    finally:
        monkeypatch.delenv("LEVERAGE_QUALITY_GAIN", raising=False)
        monkeypatch.delenv("LEVERAGE_QUALITY_FLOOR", raising=False)
        importlib.reload(mod)


def test_caps_detail_exposes_quality_inputs():
    d = _lev(0.60, 0.82)
    for key in ("c_eff", "quality", "quality_scale", "volatility", "liquidation"):
        assert key in d.caps_detail, key
    assert d.caps_detail["quality"] == pytest.approx(0.82)
