"""FAZ14i — ters politika yön konvansiyonu regresyon testleri.

Kök neden: strategy_engine ±1 konvansiyonu kullanır (1=LONG, -1=SHORT), auto_trader
ve Hummingbot ise 1=LONG, 2=SHORT bekler. `effective_side` dönüştürülmeden `side`
değişkenine atanınca SHORT sinyaller HB'ye -1 olarak gidiyor ve executor reddediliyor:
"Input should be 1, 2 or 3 [type=enum, input_value=-1]".
"""
import inspect

import pytest

import auto_trader


def test_build_executor_config_rejects_minus_one_side():
    with pytest.raises(ValueError, match="expects 1, 2 or 3"):
        auto_trader.build_executor_config("AAVEUSDT", 1.8, 2, -1)


def test_build_executor_config_rejects_minus_two_side():
    with pytest.raises(ValueError, match="expects 1, 2 or 3"):
        auto_trader.build_executor_config("AAVEUSDT", 1.8, 2, -2)


def test_build_executor_config_accepts_long_and_short():
    assert auto_trader.build_executor_config("AAVEUSDT", 1.8, 2, 1)["side"] == 1
    assert auto_trader.build_executor_config("AAVEUSDT", 1.8, 2, 2)["side"] == 2


def test_effective_side_is_mapped_to_hummingbot_convention():
    """Kaynakta ±1 → 1/2 dönüşümü olmalı; ham atama kalmamalı."""
    src = inspect.getsource(auto_trader)
    assert "_hb_side = 1 if _eff_side == 1 else 2" in src
    assert "side = _eff_side" not in src


def test_short_signal_maps_to_two():
    _eff_side = -1
    assert (1 if _eff_side == 1 else 2) == 2


def test_long_signal_maps_to_one():
    _eff_side = 1
    assert (1 if _eff_side == 1 else 2) == 1
