"""FAZ11 Katman 3 — boyutlandırma ve SL/TP geometrisi testleri."""
import pytest

import sizing_policy as sp


def test_quality_multiplier_is_zero_at_the_floor():
    assert sp.quality_multiplier(0.55) == 0.0


def test_quality_multiplier_is_one_at_full_quality():
    assert sp.quality_multiplier(1.0) == pytest.approx(1.0)


def test_quality_multiplier_is_linear_between():
    assert sp.quality_multiplier(0.775) == pytest.approx(0.5)


def test_quality_multiplier_refuses_unknown_quality():
    assert sp.quality_multiplier(None) == 0.0


def test_size_position_uses_the_loss_budget():
    d = sp.size_position(100, 0.05)
    assert d.ok and d.notional == pytest.approx(7.32, abs=0.01)
    assert d.effective_sl == pytest.approx(0.0512)


def test_size_position_caps_at_max_notional():
    d = sp.size_position(1000, 0.02)
    assert d.ok and d.notional == pytest.approx(25.0) and "max_notional" in d.caps


def test_size_position_refuses_below_exchange_minimum():
    d = sp.size_position(50, 0.08)
    assert not d.ok and "below_min_notional" in d.reason


def test_size_position_refuses_low_quality():
    d = sp.size_position(1000, 0.02, quality=0.5)
    assert not d.ok and d.reason.startswith("quality=")


def test_size_position_halves_size_at_mid_quality():
    full = sp.size_position(200, 0.05)
    half = sp.size_position(200, 0.05, quality=0.775)
    assert full.ok and half.ok
    assert half.notional == pytest.approx(full.notional * 0.5, rel=1e-6)


def test_size_position_adds_funding_to_the_stop_distance():
    base = sp.size_position(100, 0.05)
    funded = sp.size_position(100, 0.05, funding_bps=4.0)
    assert funded.effective_sl > base.effective_sl
    assert funded.notional < base.notional


def test_size_position_refuses_invalid_inputs():
    assert sp.size_position(0, 0.02).ok is False
    assert sp.size_position(100, 0.0).ok is False


def test_size_decision_as_dict_is_json_safe():
    d = sp.size_position(100, 0.05).as_dict()
    assert d["ok"] is True and isinstance(d["notional"], float)


def test_suggest_sl_tp_scales_with_atr():
    g = sp.suggest_sl_tp(0.01)
    assert g.sl_pct == pytest.approx(0.015) and g.tp_pct == pytest.approx(0.03)
    assert g.ok and g.net_rr > 1.2


def test_suggest_sl_tp_clips_a_dead_market_to_the_floor():
    g = sp.suggest_sl_tp(0.001)
    assert g.sl_pct == pytest.approx(0.004) and "sl_min" in g.clipped
    # FAZ14m: TP tabanı 2 x SL (0.008) → net_rr 1.31, eşiği geçer.
    assert g.tp_pct == pytest.approx(0.008) and "tp_min" in g.clipped
    assert g.ok and g.net_rr >= sp.SIZING_DEFAULTS["min_net_reward_risk"]


def test_suggest_sl_tp_clips_a_wild_market_to_the_ceiling():
    g = sp.suggest_sl_tp(0.05)
    assert g.sl_pct == pytest.approx(0.06) and "sl_max" in g.clipped
    assert g.tp_pct == pytest.approx(0.12) and "tp_max" in g.clipped
    assert g.ok


def test_suggest_sl_tp_refuses_invalid_atr():
    assert sp.suggest_sl_tp(0.0).ok is False
    assert sp.suggest_sl_tp(None).reason == "invalid_atr"


def test_suggest_sl_tp_refuses_invalid_direction():
    assert sp.suggest_sl_tp(0.01, direction=0).reason == "invalid_direction"


def test_suggest_sl_tp_honours_a_stricter_rr_requirement():
    g = sp.suggest_sl_tp(0.01, min_rr=3.0)
    assert not g.ok and "net_rr" in g.reason


def test_geometry_as_dict_reports_clipping():
    # FAZ14m: TP tabanı 2 x SL olduğu için ölü piyasa artık R/R kapısını geçiyor;
    # reddi kanıtlamak için daha sıkı bir R/R şartı verilir.
    d = sp.suggest_sl_tp(0.001, min_rr=3.0).as_dict()
    assert d["ok"] is False and "sl_min" in d["clipped"]
