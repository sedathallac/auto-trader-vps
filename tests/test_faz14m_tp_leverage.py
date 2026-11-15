"""FAZ14m — TP = 2 x SL ve kaldıraç tavanı 20x regresyon testleri.

Kök neden (R/R reddi): ATR küçükken SL `sl_min_pct=1.00%`'e, TP `tp_min_pct=1.50%`'e
kırpılıyordu. Brüt R/R 1.50, slippage (%0.055 tur) sonrası 1.17 < 1.20 eşiği → her sinyal
RiskEngine'de reddediliyordu. TP tabanı SL tabanının 2 katına çıkarıldı (brüt 2.0,
slippage sonrası ~1.94). Kaldıraç tavanı 5 -> 20; bağlayıcı olan yine 9-faktör formülü.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_trader
import edge_engine
import leverage_policy as lp
import live_guard
import sizing_policy as sp

CFG = auto_trader.RISK_CONFIG


def _geo(atr_pct):
    return sp.suggest_sl_tp(atr_pct, direction=1, cfg=CFG)


def test_tp_is_2x_sl():
    for atr in (0.002, 0.0044, 0.01, 0.02):
        g = _geo(atr)
        assert g.tp_pct == pytest.approx(2.0 * g.sl_pct, rel=1e-9), (atr, g.sl_pct, g.tp_pct)


def test_net_rr_above_threshold_after_slippage():
    g = _geo(0.0044)   # eski reddin ATR'si
    assert g.ok, g.reason
    assert g.net_rr >= CFG["min_net_reward_risk"], g.net_rr
    # net_win = tp - fee - slip = 2.00% - 0.12% = 1.88%; net_loss = sl + fee + slip = 1.12%
    assert g.net_rr == pytest.approx(1.678, abs=0.01), g.net_rr


def test_commission_ratio_below_3pct():
    """Komisyon / TP oranı: 0.08% tur maliyeti, TP %3.0 → %2.7."""
    fee = CFG["fee_pct_round_trip"]
    g = _geo(0.01)
    assert fee / g.tp_pct < 0.03, (fee, g.tp_pct)


def test_leverage_cap_20_for_stage_2():
    assert live_guard.STAGE_CAPS[2]["leverage"] == 20
    assert CFG["max_leverage"] == 20
    assert lp.LEVERAGE_DEFAULTS["hi"] == pytest.approx(20.0)


def test_liquidation_safe_at_20x_with_1_5pct_sl():
    """20x + SL %1.5: tasfiye mesafesi SL'in çok üstünde kalmalı."""
    l_liq = lp.liquidation_cap(0.015)
    assert l_liq > 20.0, l_liq
    d = lp.compute_leverage(sigma=0.0001, sl_pct=0.015, drawdown=0.0, f_star=None,
                            stage_cap=20, exchange_max=20)
    assert d.leverage <= 20
    assert d.leverage <= l_liq


def test_stage_2_leverage_env_cannot_exceed_cap():
    cfg = live_guard.LiveConfig.from_env({
        "LIVE_MODE": "1", "LIVE_STAGE": "2", "LIVE_STAGE_CONFIRMED": "1", "BINANCE_TESTNET": "0",
        "LIVE_MAX_LEVERAGE": "50",
    })
    assert cfg.max_leverage == 50          # env okunur
    with pytest.raises(live_guard.LiveConfigError):
        cfg.validate()                      # ama kademe tavanını aşamaz


def test_tp_max_still_bounded():
    g = _geo(0.10)   # ATR %10 → TP 30% tp_max_pct=15%'e kırpılır
    assert g.tp_pct <= CFG["tp_max_pct"]
    assert "tp_max" in g.clipped
