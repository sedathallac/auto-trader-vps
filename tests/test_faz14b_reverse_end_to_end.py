"""FAZ14b — ters politika uçtan uca: veto eşiği, aile eşiği, kalibrasyon DB bağlama.

Üç engel:
  1. trend vetosu 0.60 eşiğinde tam sınırda kalıyordu (formül doğru, eşik bağlayıcı)
  2. p_real=0.000 / kalibre=False, aile kapısında erken dönüşün semptomuydu
  3. ters sinyalde 2 aile bekleniyordu; tek kalibre aile yeterliyken kapı hiç açılmıyordu
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import strategy_engine as se

CFG = dict(auto_trader.RISK_CONFIG)


def _pf(pairs):
    """(sinyal, rejim) → (p_fake, calibrated) sabit haritası."""
    def fn(name, reg):
        return pairs.get((name, reg), (0.5, False))
    return fn


TRENDS_MIXED = [{"tf": "1h", "score": 1.0, "adx": 64, "adx_strength": 1.0},
                {"tf": "4h", "score": -1.0, "adx": 64, "adx_strength": 1.0}]
TRENDS_DOWN = [{"tf": "1h", "score": -1.0, "adx": 64, "adx_strength": 1.0},
               {"tf": "4h", "score": -1.0, "adx": 64, "adx_strength": 1.0}]


def _wick(direction=-1, strength=0.8):
    return [se.SignalReading("structure", "wick_rejection", direction, strength, "15m")]


def test_reverse_policy_flips_wick_rejection_in_live():
    """wick_rejection/trend ters çevrilir ve tek aileyle GEÇER üretir."""
    v = se.evaluate(-1, _wick(), TRENDS_MIXED, "trend",
                    _pf({("wick_rejection", "trend"): (0.725, True)}),
                    CFG, regime_adx=26, policy=se.REVERSE_POLICY)
    assert v.reversed_signals == ("reverse_wick_rejection",), v.reversed_signals
    assert v.effective_side == 1, v.effective_side
    assert v.allow is True, v.reason
    assert v.p_real > 0.65, v.p_real


def test_reverse_policy_flips_rsi_divergence_in_live():
    rd = [se.SignalReading("exhaustion", "rsi_divergence", -1, 0.7, "15m")]
    v = se.evaluate(-1, rd, TRENDS_MIXED, "trend",
                    _pf({("rsi_divergence", "trend"): (0.720, True)}),
                    CFG, regime_adx=26, policy=se.REVERSE_POLICY)
    assert "reverse_rsi_divergence" in v.reversed_signals
    assert v.effective_side == 1
    assert v.p_real > 0.5, v.p_real


def test_reverse_policy_applied_before_trend_veto():
    """Veto NİHAİ yöne göre hesaplanır: politika yönü çevirince against de değişir."""
    # Politika YOK: LONG sinyal, düşen trend → against 1.0, veto
    plain = se.evaluate(1, _wick(1, 0.8), TRENDS_DOWN, "trend",
                        _pf({}), CFG, regime_adx=26, policy=None)
    # Politika VAR: aynı sinyal SHORT'a çevrilir, trendle aynı yön → against 0.0
    rev = se.evaluate(1, _wick(1, 0.8), TRENDS_DOWN, "trend",
                      _pf({}), CFG, regime_adx=26, policy=se.REVERSE_POLICY)
    assert plain.trend_against == pytest.approx(1.0), plain.trend_against
    assert plain.veto is True, plain.reason
    assert rev.effective_side == -1, rev.effective_side
    assert rev.trend_against == pytest.approx(0.0), rev.trend_against
    assert rev.veto is False, rev.reason


def test_sr_proximity_range_not_flipped():
    rd = [se.SignalReading("structure", "sr_proximity", 1, 0.6, "15m")]
    v = se.evaluate(1, rd, TRENDS_MIXED, "range", _pf({}), CFG, regime_adx=18,
                    policy=se.REVERSE_POLICY)
    assert v.reversed_signals == (), v.reversed_signals
    assert v.effective_side == 1


def test_disabled_strategies_never_fire_in_live():
    for name, fam in (("rsi_extreme", "exhaustion"), ("bollinger_z", "exhaustion")):
        rd = [se.SignalReading(fam, name, -1, 0.9, "15m")]
        v = se.evaluate(-1, rd, TRENDS_MIXED, "trend", _pf({}), CFG, regime_adx=26,
                        policy=se.REVERSE_POLICY)
        assert v.families == {}, f"{name} devre dışı olmalıydı: {v.families}"


def test_veto_threshold_07_allows_reversed_signal():
    """0.60 eşiğinde RED, 0.70 eşiğinde GEÇER — eşik bağlayıcıydı."""
    pf = _pf({("wick_rejection", "trend"): (0.725, True)})
    old = se.evaluate(-1, _wick(), TRENDS_MIXED, "trend", pf,
                      dict(CFG, confirm_veto_trend_strength=0.6), regime_adx=26,
                      policy=se.REVERSE_POLICY)
    new = se.evaluate(-1, _wick(), TRENDS_MIXED, "trend", pf, CFG, regime_adx=26,
                      policy=se.REVERSE_POLICY)
    assert old.allow is False and old.veto is True
    assert new.allow is True, new.reason


def test_min_families_relaxed_for_reversed_only():
    """FAZ14j: eşik 1'e indi; ters sinyalde de, düz sinyalde de tek aile aile kapısını geçer.
    Ayrım artık aile sayısında değil, p_real kapısında: düz yön kalibre posteriorla düşer."""
    pf = _pf({("wick_rejection", "trend"): (0.725, True)})
    rev = se.evaluate(-1, _wick(), TRENDS_MIXED, "trend", pf, CFG, regime_adx=26,
                      policy=se.REVERSE_POLICY)
    plain = se.evaluate(1, _wick(1, 0.8), TRENDS_MIXED, "trend", pf, CFG, regime_adx=26,
                        policy=None)
    assert rev.allow is True
    assert plain.allow is False, plain.reason
    assert "teyit yetersiz" not in plain.reason, plain.reason
    assert "p_real=" in plain.reason, plain.reason


def test_calibration_db_env_binding(monkeypatch):
    monkeypatch.setenv("SIGNAL_CALIBRATION_DB", "/tmp/calib_x.db")
    assert se.calib_db_path() == "/tmp/calib_x.db"
    monkeypatch.setenv("SIGNAL_CALIBRATION_DB", "  ")
    assert se.calib_db_path() == se.SIGNAL_DB


def test_p_real_positive_when_calibrated():
    v = se.evaluate(-1, _wick(), TRENDS_MIXED, "trend",
                    _pf({("wick_rejection", "trend"): (0.725, True)}),
                    CFG, regime_adx=26, policy=se.REVERSE_POLICY)
    assert v.p_real > 0.0 and v.calibrated is True, (v.p_real, v.calibrated)


def test_min_strength_relaxed_to_015():
    assert CFG["confirm_min_strength"] == 0.15
    rd = [se.SignalReading("structure", "wick_rejection", -1, 0.20, "15m")]
    v = se.evaluate(-1, rd, TRENDS_MIXED, "trend",
                    _pf({("wick_rejection", "trend"): (0.725, True)}),
                    CFG, regime_adx=26, policy=se.REVERSE_POLICY)
    assert v.families, "0.20 güçte sinyal artık oy sayılmalı"
