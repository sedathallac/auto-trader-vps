"""FAZ14j — aile teyidi eşiği regresyon testleri.

Ölçüm (canlı log, 368 Faz6 çağrısı): 294 çağrı 0 aile (%79.9), 65 çağrı 1 aile (%17.7),
yalnız 9 çağrı 2+ aile (%2.4). `confirm_min_families=2` bu yüzden kapıyı yapısal olarak
kilitliyordu. Eşik 1'e indirildi; tek aileli hücrelerde p_real prior'a düşer (0.500) ve
`confirm_min_p_real=0.40` kapısını geçer.
"""
import math
import os

import pytest

import strategy_engine as se


def test_min_families_default_is_one():
    assert se.CONFIRM_DEFAULTS["confirm_min_families"] == 1


def test_min_families_env_override(monkeypatch):
    import importlib
    monkeypatch.setenv("CONFIRM_MIN_FAMILIES", "3")
    mod = importlib.reload(se)
    try:
        assert mod.CONFIRM_DEFAULTS["confirm_min_families"] == 3
    finally:
        monkeypatch.delenv("CONFIRM_MIN_FAMILIES", raising=False)
        importlib.reload(mod)


def test_single_family_reaches_p_real_gate():
    """Tek aile + kalibre olmayan hücre → p_real = prior = 0.5 ≥ 0.40."""
    prior = se.CONFIRM_DEFAULTS["confirm_prior_real"]
    p_fake = 0.5  # n < min_events → nötr
    logit = math.log(prior / (1 - prior)) + math.log((1 - p_fake) / p_fake)
    p_real = 1.0 / (1.0 + math.exp(-logit))
    assert p_real == pytest.approx(0.5, abs=1e-9)
    assert p_real >= se.CONFIRM_DEFAULTS["confirm_min_p_real"]


def test_min_strength_is_relaxed_to_015():
    assert se.CONFIRM_DEFAULTS["confirm_min_strength"] == 0.15


def test_min_p_real_is_040():
    assert se.CONFIRM_DEFAULTS["confirm_min_p_real"] == 0.40


def test_reverse_policy_still_allows_single_family():
    """Ters politikada min_families zaten 1'e düşürülüyordu; bu korunmalı."""
    import inspect
    src = inspect.getsource(se.evaluate)
    assert "_min_fam = 1 if reversed_all else" in src
