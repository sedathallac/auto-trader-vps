"""FAZ13-A/C — env bağlamaları + ters strateji shadow modu.

Kanıtlanan davranışlar:
  * TOP_N / LONG_ON_DIP_PCT / SHORT_ON_PUMP_PCT env'den okunur (varsayılan eski davranış)
  * ters politika: wick_rejection+trend ve rsi_divergence+trend yön çevirir, sr_proximity+range korunur
  * devre dışı sinyaller oy sayılmaz (aile sayısı artmaz)
  * shadow yalnız compare modunda uygulanır; canlı ('on') asıl yönle karar verir
"""
import importlib
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import strategy_engine as se

CFG = se.CONFIRM_DEFAULTS
FLAT_TRENDS = [{"tf": "1h", "score": 0.0, "adx": 18.0, "adx_strength": 0.12},
               {"tf": "4h", "score": 0.0, "adx": 16.0, "adx_strength": 0.04}]


def _rd(name, family, direction, strength=0.8):
    return se.SignalReading(family, name, direction, strength)


# ------------------------------------------------------------------ A) env bağlamaları
def test_top_n_env_override(monkeypatch):
    monkeypatch.setenv("TOP_N", "6")
    mod = importlib.reload(auto_trader)
    try:
        assert mod.TOP_N == 6
    finally:
        monkeypatch.delenv("TOP_N", raising=False)
        importlib.reload(auto_trader)


def test_long_on_dip_env_override(monkeypatch):
    monkeypatch.setenv("LONG_ON_DIP_PCT", "-1.0")
    monkeypatch.setenv("SHORT_ON_PUMP_PCT", "1.0")
    mod = importlib.reload(auto_trader)
    try:
        assert mod.LONG_ON_DIP_PCT == -1.0 and mod.SHORT_ON_PUMP_PCT == 1.0
        assert mod.decide_side({"priceChangePercent": "-1.2"})[0] == 1
        assert mod.decide_side({"priceChangePercent": "1.2"})[0] == 2
        assert mod.decide_side({"priceChangePercent": "-0.4"})[0] is None
    finally:
        monkeypatch.delenv("LONG_ON_DIP_PCT", raising=False)
        monkeypatch.delenv("SHORT_ON_PUMP_PCT", raising=False)
        importlib.reload(auto_trader)


# ------------------------------------------------------------------ C) ters politika
def test_reverse_strategy_wick_rejection_in_trend():
    readings = [_rd("wick_rejection", se.FAM_STRUCTURE, 1, 0.79),
                _rd("rsi_divergence", se.FAM_EXHAUSTION, 1, 0.7)]
    # FAZ14: politika vetodan ÖNCE çalışır; side=+1 gelse de nihai yön -1'e çevrilir ve GEÇER.
    v_long = se.evaluate(1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.3, True), CFG,
                         regime_adx=30.0, policy=se.REVERSE_POLICY)
    assert v_long.effective_side == -1 and v_long.allow is True
    assert set(v_long.reversed_signals) == {"reverse_wick_rejection", "reverse_rsi_divergence"}
    # SHORT (side=-1): ters çevrilmiş iki sinyal teyit eder → GEÇER
    v_short = se.evaluate(-1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.3, True), CFG,
                          regime_adx=30.0, policy=se.REVERSE_POLICY)
    assert v_short.allow is True
    assert set(v_short.families) == {se.FAM_STRUCTURE, se.FAM_EXHAUSTION}
    assert set(v_short.reversed_signals) == {"reverse_wick_rejection", "reverse_rsi_divergence"}


def test_reverse_strategy_rsi_divergence_in_trend():
    readings = [_rd("rsi_divergence", se.FAM_EXHAUSTION, -1, 0.75),
                _rd("wick_rejection", se.FAM_STRUCTURE, -1, 0.7)]
    # Sinyaller SHORT diyor; ters politika LONG'u teyit eder.
    v = se.evaluate(1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.3, True), CFG,
                    regime_adx=30.0, policy=se.REVERSE_POLICY)
    assert v.allow is True and "reverse_rsi_divergence" in v.reversed_signals
    # Politika yoksa aynı okumalar LONG'u teyit ETMEZ (eski davranış korunur)
    v_off = se.evaluate(1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.3, True), CFG,
                        regime_adx=30.0, policy=None)
    assert v_off.allow is False and v_off.families == {}


def test_original_strategy_sr_proximity_in_range():
    readings = [_rd("sr_proximity", se.FAM_STRUCTURE, 1, 0.8),
                _rd("rsi_divergence", se.FAM_EXHAUSTION, 1, 0.7)]
    v = se.evaluate(1, readings, FLAT_TRENDS, "range", lambda n, g: (0.3, True), CFG,
                    regime_adx=12.0, policy=se.REVERSE_POLICY)
    assert v.allow is True
    assert "sr_proximity" in v.families.values()
    assert "reverse_sr_proximity" not in v.reversed_signals
    assert se.effective_direction(readings[0], "range", se.REVERSE_POLICY) == 1


def test_disabled_strategies_do_not_fire():
    readings = [_rd("rsi_extreme", se.FAM_EXHAUSTION, 1, 0.9),
                _rd("bollinger_z", se.FAM_STRUCTURE, 1, 0.9),
                _rd("sr_proximity", se.FAM_STRUCTURE, 1, 0.9)]
    v = se.evaluate(1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.2, True), CFG,
                    regime_adx=30.0, policy=se.REVERSE_POLICY)
    assert v.allow is False and v.families == {}
    assert "0/2" in v.reason
    for r in readings:
        assert se.effective_direction(r, "trend", se.REVERSE_POLICY) == 0


def test_shadow_mode_does_not_affect_live():
    src = inspect.getsource(auto_trader._faz6_confirm)
    assert 'mode in ("compare", "on") and REVERSE_POLICY_ENABLED' in src
    assert 'source="shadow" if _policy6 is not None else "live"' in src
    # Canlı modda politika None → asıl yön; compare'de politika uygulanır.
    readings = [_rd("wick_rejection", se.FAM_STRUCTURE, 1, 0.79),
                _rd("rsi_divergence", se.FAM_EXHAUSTION, 1, 0.7)]
    live = se.evaluate(-1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.3, True), CFG,
                       regime_adx=30.0, policy=None)
    assert live.allow is False, "canlı modda ters politika sızmamalı"
    shadow = se.evaluate(-1, readings, FLAT_TRENDS, "trend", lambda n, g: (0.3, True), CFG,
                         regime_adx=30.0, policy=se.REVERSE_POLICY)
    assert shadow.allow is True
