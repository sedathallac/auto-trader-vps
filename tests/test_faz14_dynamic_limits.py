"""FAZ14 — dinamik matematik limitleri: sabit tavan yok, tüm 5 beyin tam kapasite.

Sabit USDT tavanları (100 notional, 2x kaldıraç, 20 USDT günlük zarar) beynin
hesapladığı değerleri eziyordu. Burada her limit equity'ye bağlı yüzdeye çevrildi.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import leverage_policy
import live_guard
import risk_engine
import sizing_policy

EQUITY = 2524.0


def _env(**over):
    base = {
        "LIVE_MODE": "1", "HB_OFFLINE": "0", "COMPARE_MODE": "0", "LIVE_DRY_RUN": "0",
        "LIVE_STAGE": "2", "LIVE_STAGE_CONFIRMED": "1", "BINANCE_TESTNET": "0",
        "BINANCE_FUTURES_BASE_URL": "https://fapi.binance.com",
        "BINANCE_KEY": "", "BINANCE_SECRET": "",
        "LIVE_MAX_POSITION_PCT": "20.0", "LIVE_MAX_LEVERAGE": "5",
        "LIVE_DAILY_LOSS_PCT": "2.0", "LIVE_MAX_OPEN_POSITIONS": "5",
        "LIVE_STRATEGY_FAMILY": "", "LIVE_ALLOW_UNPROVEN_FAMILY": "0",
        "LIVE_WINNING_REGIMES": "range,mid",
    }
    base.update(over)
    return base


def _cfg(**over):
    return live_guard.LiveConfig.from_env(_env(**over)).validate()


def test_dynamic_notional_pct_20():
    """Boyut equity'nin %20'sini aşamaz: 2524 → 504.80 USDT."""
    cfg = dict(auto_trader.RISK_CONFIG, max_notional=0, max_position_pct=20.0, max_leverage=5)
    d = sizing_policy.size_position(EQUITY, 0.015, cfg=cfg)
    assert d.notional is not None
    assert d.notional <= EQUITY * 0.20 + 1e-9, f"%20 tavanı aşıldı: {d.notional}"
    assert "max_position_pct" in d.caps or d.notional < EQUITY * 0.20


def test_leverage_full_range_1_to_5():
    """compute_leverage 1..5 aralığının tamamını üretebilmeli; stage_cap kırpmamalı."""
    seen = set()
    for sigma in (0.002, 0.004, 0.008, 0.015, 0.03):
        d = leverage_policy.compute_leverage(sigma, max(0.005, sigma * 1.5),
                                             stage_cap=5, exchange_max=5)
        seen.add(d.leverage)
    assert max(seen) == 5, f"5x'e ulaşılamadı: {sorted(seen)}"
    assert min(seen) >= 1


def test_leverage_btc_gets_5x_aster_gets_1x():
    """Düşük volatilite yüksek kaldıraç, yüksek volatilite düşük kaldıraç."""
    btc = leverage_policy.compute_leverage(0.004, 0.006, stage_cap=5, exchange_max=5)
    aster = leverage_policy.compute_leverage(0.021, 0.0315, stage_cap=5, exchange_max=5)
    assert btc.leverage == 5, f"BTC 5x olmalıydı, {btc.leverage}x"
    assert aster.leverage == 1, f"ASTER 1x olmalıydı, {aster.leverage}x"


def test_max_open_positions_5():
    cfg = _cfg()
    assert cfg.max_open_positions == 5
    assert cfg.risk_overrides()["max_leverage"] == 5


def test_daily_loss_limit_equity_pct_2(tmp_path):
    """Günlük limit equity %2 = 50.48 USDT; sabit 20 USDT değil."""
    cfg = dict(auto_trader.RISK_CONFIG, max_daily_loss_pct=0.02,
               db_file=str(tmp_path / "risk.db"))
    r = risk_engine.RiskEngine(cfg)
    r.update_equity(EQUITY)
    r.update_equity(EQUITY * 0.985)   # %1.5 zarar → halt YOK
    assert not r.is_halted
    r.update_equity(EQUITY * 0.979)   # %2.1 zarar → halt
    assert r.is_halted and "daily_loss_pct" in r.halt_reason
    assert "50.48" in r.halt_reason, r.halt_reason


def test_sizing_respects_risk_budget_and_cap():
    """Bütçe ve yüzde tavanı birlikte: sonuç ikisinin de altında kalır."""
    cfg = dict(auto_trader.RISK_CONFIG, max_notional=0, max_position_pct=20.0, max_leverage=5)
    d = sizing_policy.size_position(EQUITY, 0.015, cfg=cfg)
    budget = EQUITY * cfg["risk_per_trade_pct"] * cfg["kelly_fraction"]
    assert d.notional <= budget / d.effective_sl + 1e-9
    assert d.notional <= EQUITY * 0.20 + 1e-9


def test_all_5_brains_logged_per_order():
    src = __import__("inspect").getsource(auto_trader.open_coin)
    for tag in ("[LIVE] coin_quality:", "[LIVE] leverage:", "[LIVE] sizing:",
                "[LIVE] preflight:", "[LIVE] open "):
        assert tag in src, f"beyin logu eksik: {tag}"


def test_coin_quality_gate_still_active():
    cfg = dict(auto_trader.RISK_CONFIG, max_notional=0, max_position_pct=20.0)
    d = sizing_policy.size_position(EQUITY, 0.015, quality=0.0, cfg=cfg)
    assert d.notional is None and "quality" in d.reason


def test_preflight_still_12_steps():
    import preflight
    r = preflight.pre_flight_check(
        cfg=_cfg(), hb_offline=0,
        account=lambda: {"assets": [{"asset": "USDT", "availableBalance": "2524.0"}],
                         "canTrade": True},
        exchange_info=lambda: {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
                                            "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001",
                                                         "minQty": "0.001"},
                                                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                                        {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]},
        symbol="BTCUSDT",
        filters={"LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
                 "PRICE_FILTER": {"tickSize": "0.1"},
                 "MIN_NOTIONAL": {"notional": "5"}},
        quality={"CQ": 0.9, "quality_ok": True, "quality_vetoes": []},
        leverage=5, sl_pct=0.01,
        size=type("S", (), {"ok": True, "reason": "notional=400"})(),
        geometry=type("G", (), {"ok": True, "reason": "ok", "net_rr": 1.5})(),
        risk=(400.0, "approved"),
        exposure={"open_positions": 0, "max_positions": 5, "margin_usage": 0.0, "margin_limit": 0.4},
        edge=(True, "shadow"))
    assert len(r.steps) == 12, f"preflight adım sayısı {len(r.steps)}: {[s.name for s in r.steps]}"


def test_no_daily_trade_count_limit():
    assert auto_trader.RISK_CONFIG["max_trades_per_day"] >= 10000
    cfg = dict(auto_trader.RISK_CONFIG, max_daily_loss_pct=0.02)
    r = risk_engine.RiskEngine(cfg)
    r.update_equity(EQUITY)
    r.daily_trades = 500
    ok, reason = r.approve_trade(EQUITY, 0.015)
    assert "daily_limit" not in str(reason), reason
