"""FAZ11 Katman 2 — kaldıraç politikası testleri."""
import math

import pytest

import leverage_policy as lp


def test_volatility_leverage_is_inverse_in_sigma():
    assert lp.volatility_leverage(0.02) == pytest.approx(1.0)
    assert lp.volatility_leverage(0.01) == pytest.approx(2.0)


def test_volatility_leverage_is_capped_at_hi():
    assert lp.volatility_leverage(0.0001) == pytest.approx(lp.LEVERAGE_DEFAULTS["hi"])


def test_volatility_leverage_fails_safe_on_zero_sigma():
    assert lp.volatility_leverage(0.0) == pytest.approx(lp.LEVERAGE_DEFAULTS["lo"])
    assert lp.volatility_leverage(None) == pytest.approx(lp.LEVERAGE_DEFAULTS["lo"])


def test_drawdown_penalty_halves_leverage_at_one_hundred_percent():
    assert lp.drawdown_penalty(4.0, 1.0) == pytest.approx(2.0)


def test_drawdown_penalty_ignores_negative_drawdown():
    assert lp.drawdown_penalty(4.0, -0.5) == pytest.approx(4.0)


def test_kelly_cap_is_lo_without_edge():
    assert lp.kelly_leverage_cap(0.0) == pytest.approx(lp.LEVERAGE_DEFAULTS["lo"])


def test_kelly_cap_reaches_hi_at_full_reference_edge():
    assert lp.kelly_leverage_cap(1.0) == pytest.approx(lp.LEVERAGE_DEFAULTS["hi"])


def test_liquidation_cap_matches_the_formula():
    assert lp.liquidation_cap(0.02) == pytest.approx(1.0 / (2 * 0.02 + 0.005))


def test_liquidation_cap_is_lo_for_invalid_sl():
    assert lp.liquidation_cap(0.0) == pytest.approx(lp.LEVERAGE_DEFAULTS["lo"])


def test_compute_leverage_picks_the_volatility_cap():
    d = lp.compute_leverage(0.02, 0.02)
    assert d.leverage == 1 and "drawdown" not in d.caps


def test_compute_leverage_floors_instead_of_rounding_up():
    d = lp.compute_leverage(0.006, 0.02)   # c/σ = 3.33 → 3, asla 4
    assert d.leverage == 3 and d.raw == pytest.approx(3.3333, abs=1e-4)


def test_compute_leverage_applies_the_drawdown_penalty():
    d = lp.compute_leverage(0.005, 0.02, drawdown=1.0)
    assert d.leverage == 2 and "drawdown" in d.caps


def test_compute_leverage_respects_the_kelly_cap():
    # FAZ14k: round-half-to-even. Kelly tavanı 5.75 → 6x (floor 5 verirdi).
    # Kelly yumuşak tavandır; sert güvenlik sınırı tasfiye tavanıdır ve o clamp'lenir.
    d = lp.compute_leverage(0.002, 0.02, f_star=0.25)
    assert d.raw == pytest.approx(5.75, abs=1e-6)
    assert d.leverage == 6 and "kelly" in d.caps
    assert d.leverage <= math.ceil(d.raw)


def test_compute_leverage_respects_liquidation_safety():
    d = lp.compute_leverage(0.0001, 0.05)
    assert d.leverage == 9 and "liquidation" in d.caps


def test_compute_leverage_respects_the_stage_cap():
    d = lp.compute_leverage(0.0001, 0.02, stage_cap=2)
    assert d.leverage == 2 and "stage" in d.caps


def test_compute_leverage_respects_the_exchange_cap():
    d = lp.compute_leverage(0.0001, 0.02, exchange_max=3)
    assert d.leverage == 3 and "exchange" in d.caps


def test_compute_leverage_never_returns_zero():
    d = lp.compute_leverage(0.0001, 0.02, stage_cap=0)
    assert d.leverage >= 1 and "floor" in d.caps


def test_compute_leverage_fails_safe_on_invalid_inputs():
    d = lp.compute_leverage(0.0, 0.0)
    assert d.leverage == 1 and d.caps == ["invalid_inputs"]


def test_compute_leverage_reports_the_binding_cap():
    d = lp.compute_leverage(0.005, 0.02)
    assert d.reason.startswith("binding=")


def test_leverage_ok_accepts_a_safe_leverage():
    ok, detail = lp.leverage_ok(5, 0.02)
    assert ok and "cap=" in detail


def test_leverage_ok_refuses_above_the_liquidation_cap():
    ok, detail = lp.leverage_ok(30, 0.02)
    assert not ok and ">" in detail


def test_leverage_ok_refuses_zero():
    assert lp.leverage_ok(0, 0.02)[0] is False
