"""FAZ14k — sürekli kaldıraç yuvarlaması + yapılandırılabilir c + risk logu.

Kök neden: `floor(raw)` kesikli davranıyordu. INJ σ=0.0108 → L_vol=1.852 → floor 1x,
UNI σ=0.0100 → L_vol=2.000 → floor 2x. %8 ATR farkı %100 kaldıraç farkı üretiyordu.
Ayrıca `c=0.02` sabitti (env'e bağlı değil) ve risk/işlem loglanmıyordu.
"""
import importlib
import math

import pytest

import leverage_policy as lp


def _lev(atr_pct, c=None, stage=5, exchange=5):
    cfg = {"c": c} if c is not None else None
    sigma = atr_pct / 100.0
    sl = min(max(sigma * 1.5, 0.005), 0.05)
    return lp.compute_leverage(sigma=sigma, sl_pct=sl, drawdown=0.0, f_star=None,
                               stage_cap=stage, exchange_max=exchange, cfg=cfg)


def test_inj_gets_2x_with_round_not_1x_with_floor():
    d = _lev(1.08)
    assert d.raw == pytest.approx(1.852, abs=1e-3)
    assert d.leverage == 2, f"round(1.852) 2 olmalı, floor 1 verirdi: {d.leverage}"


def test_uni_and_inj_get_same_leverage_at_nearby_atr():
    uni, inj = _lev(1.00), _lev(1.08)
    assert uni.leverage == inj.leverage == 2, (uni.leverage, inj.leverage)


def test_leverage_c_env_configurable(monkeypatch):
    monkeypatch.setenv("LEVERAGE_C", "0.06")
    mod = importlib.reload(lp)
    try:
        assert mod.LEVERAGE_DEFAULTS["c"] == pytest.approx(0.06)
        # stage_cap=20 → volatilite tavanı bağlar, ham değer görünür
        free = mod.compute_leverage(sigma=0.0108, sl_pct=0.0162, drawdown=0.0, f_star=None,
                                    stage_cap=20, exchange_max=20)
        assert free.raw == pytest.approx(0.06 / 0.0108, abs=1e-3)
        assert free.leverage == 6, free.leverage
        # stage_cap=5 → kademe tavanı bağlar
        capped = mod.compute_leverage(sigma=0.0108, sl_pct=0.0162, drawdown=0.0, f_star=None,
                                      stage_cap=5, exchange_max=5)
        assert capped.leverage == 5, capped.leverage
        assert "stage" in capped.caps
    finally:
        monkeypatch.delenv("LEVERAGE_C", raising=False)
        importlib.reload(mod)


def test_risk_per_trade_logged_per_order():
    d = _lev(1.08)
    assert d.risk_per_trade == pytest.approx(0.0162 * d.leverage, abs=1e-9)
    assert d.risk_per_trade > 0
    assert "risk_per_trade" in d.as_dict()
    assert d.caps_detail["volatility"] == pytest.approx(1.852, abs=1e-3)


def test_rounded_leverage_respects_liquidation_cap():
    """Yuvarlama tasfiye tavanını delemez: L_liq küçükse clamp devreye girer."""
    d = lp.compute_leverage(sigma=0.0001, sl_pct=0.20, drawdown=0.0, f_star=None,
                            stage_cap=20, exchange_max=20)
    cap = lp.liquidation_cap(0.20)
    assert d.leverage <= cap, (d.leverage, cap)
    assert "liq_clamp" in d.caps or d.leverage <= math.floor(cap)


def test_btc_still_gets_5x():
    d = _lev(0.40)
    assert d.raw == pytest.approx(5.0, abs=1e-6)
    assert d.leverage == 5


def test_aster_still_gets_1x():
    d = _lev(2.10)
    assert d.leverage == 1


def test_invalid_inputs_fail_safe_to_lowest():
    d = lp.compute_leverage(sigma=0.0, sl_pct=0.01)
    assert d.leverage == 1 and "invalid_inputs" in d.caps
