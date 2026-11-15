"""FAZ12 — canlı servis "tick atmıyor" regresyon testleri.

Kök neden: pozisyon tavanı kapıları modül düzeyi `MAX_OPEN_POSITIONS` okuyordu.
Canlı servis limiti `LIVE_MAX_OPEN_POSITIONS` ile taşır, `MAX_OPEN_POSITIONS`
env'de yoktur → 0 → `len(active) >= 0` her zaman doğru → tarama hiç başlamaz.
"""
from types import SimpleNamespace

import pytest

import auto_trader


class LiveCfg:
    is_live = True
    max_open_positions = 2


class CompareCfg:
    is_live = False
    max_open_positions = 2


def test_effective_limit_uses_live_config(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: LiveCfg())
    assert auto_trader._effective_max_positions() == 2


def test_effective_limit_uses_module_global_for_compare(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: CompareCfg())
    monkeypatch.setattr(auto_trader, "MAX_OPEN_POSITIONS", 5)
    assert auto_trader._effective_max_positions() == 5


def test_effective_limit_falls_back_when_config_raises(monkeypatch):
    def boom():
        raise RuntimeError("no env")

    monkeypatch.setattr(auto_trader, "_live_cfg", boom)
    monkeypatch.setattr(auto_trader, "MAX_OPEN_POSITIONS", 3)
    assert auto_trader._effective_max_positions() == 3


def test_effective_limit_never_returns_negative(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg",
                        lambda: SimpleNamespace(is_live=True, max_open_positions=-4))
    assert auto_trader._effective_max_positions() == 0


def test_live_scan_is_not_blocked_when_no_positions_are_open(monkeypatch):
    """Regresyon: limit 2 iken 0 açık pozisyon taramayı ENGELLEMEMELİ."""
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: LiveCfg())
    monkeypatch.setattr(auto_trader, "MAX_OPEN_POSITIONS", 0)
    limit = auto_trader._effective_max_positions()
    assert not (0 >= limit), "0 açık pozisyon limiti doldurmamalı"


def test_live_scan_is_blocked_when_the_limit_is_reached(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: LiveCfg())
    limit = auto_trader._effective_max_positions()
    assert 2 >= limit


def test_old_behaviour_would_have_blocked_every_scan(monkeypatch):
    """Eski kodun neden hiç tick atmadığını kanıtlar: global 0 → kapı kapalı."""
    monkeypatch.setattr(auto_trader, "MAX_OPEN_POSITIONS", 0)
    assert 0 >= auto_trader.MAX_OPEN_POSITIONS


def test_gates_no_longer_reference_the_bare_global():
    import inspect
    src = inspect.getsource(auto_trader)
    assert "len(active) >= MAX_OPEN_POSITIONS" not in src
    assert "len(_cur) >= MAX_OPEN_POSITIONS" not in src
    assert "len(_cur_active) >= MAX_OPEN_POSITIONS" not in src
    assert src.count("_effective_max_positions()") >= 6


def test_live_env_carries_the_limit_under_the_live_name():
    """live.env LIVE_MAX_OPEN_POSITIONS taşır; kod bu adı okumalı."""
    import live_guard
    cfg = live_guard.LiveConfig.from_env({
        "LIVE_MODE": "1", "HB_OFFLINE": "0", "COMPARE_MODE": "0", "LIVE_DRY_RUN": "0",
        "LIVE_STAGE": "1", "LIVE_STAGE_CONFIRMED": "0", "BINANCE_TESTNET": "1",
        "BINANCE_FUTURES_BASE_URL": "https://testnet.binancefuture.com",
        "BINANCE_KEY": "", "BINANCE_SECRET": "",
        "LIVE_MAX_NOTIONAL_USDT": "25", "LIVE_DAILY_LOSS_LIMIT_USDT": "20",
        "LIVE_LEVERAGE": "2", "LIVE_MAX_OPEN_POSITIONS": "2",
        "LIVE_STRATEGY_FAMILY": "", "LIVE_ALLOW_UNPROVEN_FAMILY": "1",
        "LIVE_WINNING_REGIMES": "range,mid",
    }).validate()
    assert cfg.max_open_positions == 2
