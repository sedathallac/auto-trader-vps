"""Faz 7 RED/GREEN — Faz 3 (leverage_engine) × RiskEngine uyumu (birleşik boyutlandırma).

RED:   Eski 'on' yolu `target_notional = scientific_position_size(...)` ile RiskEngine'in onayladığı
       notional'ı EZİYORDU → Faz 3, RiskEngine'in kayıp bütçesini (risk·Kelly/(SL+fee)) aşabilir,
       kaldıraç tasfiye mesafesine bakmıyordu.
GREEN: unified_sizing: S ≤ RiskEngine notional, L ≤ tasfiye-güvenli tavan, margin ≤ bütçe;
       konfigürasyonlar RISK_CONFIG ile tutarlı.
"""
import types

import pytest

import auto_trader
from leverage_engine import (
    LEV_ENGINE_CONFIG,
    SignalStats,
    liquidation_safe_leverage_cap,
    scientific_leverage,
    scientific_position_size,
    unified_sizing,
)
from risk_engine import RiskEngine

RC = auto_trader.RISK_CONFIG


def _stats(q=0.6, f_star=0.4, reliable=True):
    return SignalStats(n=30, p=q, b=2.0, f_star=f_star, q=q, reliable=reliable)


def _risk_notional(balance, sl_total, cfg=None):
    """GERÇEK RiskEngine.approve_trade formülü (DB'siz sahte örnek üzerinde çağrılır)."""
    cfg = cfg or RC
    fake = types.SimpleNamespace(is_halted=False, daily_trades=0, consecutive_losses=0, cfg=cfg)
    n, status = RiskEngine.approve_trade(fake, balance, sl_total)
    assert status == "approved"
    return n


# ------------------------------------------------------------------ konfigürasyon uyumu
def test_green_configs_are_consistent_between_engines():
    """İki motor aynı sabitleri kullanmalı: sapma = sessiz uyumsuzluk."""
    assert LEV_ENGINE_CONFIG["kelly_fraction"] == RC["kelly_fraction"]
    assert LEV_ENGINE_CONFIG["hi"] == RC["max_leverage"]
    assert LEV_ENGINE_CONFIG["lo"] == RC["min_leverage"]
    assert auto_trader.MAX_NOTIONAL_USDT == RC["max_notional"]
    assert auto_trader.MIN_NOTIONAL_USDT == RC["min_notional"]


def test_green_cold_start_bands_inside_hard_caps():
    c = LEV_ENGINE_CONFIG
    assert RC["min_notional"] <= c["cold_start_min_notional"] <= c["cold_start_max_notional"] <= RC["max_notional"]
    assert RC["min_leverage"] <= c["cold_start_lev_lo"] <= c["cold_start_lev_hi"] <= RC["max_leverage"]


# ------------------------------------------------------------------ RED: eski 'on' RiskEngine'i aşar
def test_red_old_on_path_can_exceed_riskengine_notional():
    """RED: sert tavan gevşetilince Faz 3 tek başına RiskEngine'in kayıp bütçesini aşar."""
    cfg = dict(RC, max_notional=1000.0)                  # yalnız sert tavan gevşek (senaryo)
    balance, sl_total, sigma = 100.0, 0.0108, 0.01
    re_n = _risk_notional(balance, sl_total, cfg)        # 100·0.015/(0.0108+0.0008)·0.25 ≈ 32.3
    old_s = scientific_position_size(balance, 0.015, sigma, _stats(q=0.9, f_star=0.9), 5.5, 1000.0)
    assert old_s > re_n * 1.5                            # eski yol: RiskEngine'in çok üstünde (BOŞLUK)


def test_green_unified_never_exceeds_riskengine_notional():
    cfg = dict(RC, max_notional=1000.0)
    for balance in (50.0, 100.0, 1000.0, 2500.0):
        for sigma in (0.004, 0.01, 0.03):
            for sl in (0.01, 0.03, 0.08):
                re_n = _risk_notional(balance, sl + 0.0002, cfg)
                u = unified_sizing(balance, sigma, sl + 0.0002, 0.0, _stats(q=0.9, f_star=0.9),
                                   cfg, 5.5, 1000.0, risk_notional=re_n)
                assert u.notional <= re_n + 1e-9
                # SL'de kayıp ≤ RiskEngine kayıp bütçesi (risk·Kelly·bakiye) — RiskEngine kendi
                # min_notional tabanına çarpmadıysa (aşağıdaki bulgu testine bakın).
                if re_n > cfg["min_notional"] + 1e-9:
                    assert u.notional * (sl + 0.0008) <= balance * cfg["risk_per_trade_pct"] * cfg["kelly_fraction"] + 1e-6


def test_finding_riskengine_min_notional_floor_can_exceed_its_own_loss_budget():
    """BULGU (mevcut RiskEngine davranışı, Faz 3'ten bağımsız): approve_trade notional'ı
    min_notional'a YUKARI çeker; küçük bakiye + geniş SL'de kayıp bütçesi (risk·Kelly) aşılır.
    Borsa min-notional'ı nedeniyle kısmen kaçınılmaz; birleşik boyutlandırma bunu KÖTÜLEŞTİRMEZ."""
    balance, sl = 50.0, 0.0808
    re_n = _risk_notional(balance, sl)
    budget = balance * RC["risk_per_trade_pct"] * RC["kelly_fraction"]
    assert re_n == RC["min_notional"]                          # tabana çekildi
    assert re_n * (sl + 0.0008) > budget                       # bütçe aşımı (mevcut)
    u = unified_sizing(balance, 0.01, sl, 0.0, _stats(q=0.9, f_star=0.9), RC, 5.5, 25.0,
                       risk_notional=re_n)
    assert u.notional <= re_n                                  # Faz 3 daha da artırmaz


def test_green_unified_risk_engine_cap_reported():
    cfg = dict(RC, max_notional=1000.0)
    re_n = _risk_notional(100.0, 0.0108, cfg)
    u = unified_sizing(100.0, 0.01, 0.0108, 0.0, _stats(q=0.9, f_star=0.9), cfg, 5.5, 1000.0,
                       risk_notional=re_n)
    assert "risk_engine" in u.caps and u.notional == pytest.approx(re_n) and u.s_faz3 > re_n


def test_green_faz3_can_still_shrink_below_riskengine():
    """Faz 3 küçültebilir: cold-start (12-15) < RiskEngine'in 25 tavanı → Faz 3 değeri kullanılır."""
    cold = SignalStats(0, 0.0, 0.0, 0.0, 0.5, False)
    re_n = _risk_notional(2500.0, 0.0112)
    u = unified_sizing(2500.0, 0.02, 0.0112, 0.0, cold, RC, 5.5, 25.0, risk_notional=re_n)
    assert u.notional == pytest.approx(15.0) and u.notional < re_n and "risk_engine" not in u.caps


def test_green_risk_guards_off_uses_only_hard_caps():
    u = unified_sizing(2500.0, 0.004, 0.011, 0.0, _stats(q=0.9, f_star=0.9), RC, 5.5, 25.0,
                       risk_notional=None)
    assert u.notional == 25.0 and "risk_engine" not in u.caps


# ------------------------------------------------------------------ kaldıraç / tasfiye
def test_green_liquidation_cap_formula():
    assert liquidation_safe_leverage_cap(0.10) == pytest.approx(1 / (0.2 + 0.005))   # ≈4.88
    assert liquidation_safe_leverage_cap(0.01) == pytest.approx(1 / (0.02 + 0.005))  # 40
    assert liquidation_safe_leverage_cap(0.0) == 1.0
    assert liquidation_safe_leverage_cap(0.9) == 1.0                                 # alt sınır


def test_red_old_leverage_ignores_sl_distance():
    """RED: Faz 3 kaldıracı SL mesafesine bakmaz; geniş SL'de tasfiye SL'den önce gelebilir."""
    lev = scientific_leverage(0.004, _stats(q=0.9, f_star=1.0), 0.0)      # tam kenar → hi=5
    sl = 0.30                                                              # senaryo: çok geniş SL
    assert lev == 5.0 and 1.0 / lev < sl                                   # tasfiye (%20) < SL (%30)


def test_green_unified_leverage_keeps_liquidation_beyond_sl():
    for sl in (0.01, 0.03, 0.05, 0.08, 0.10, 0.20, 0.30):
        u = unified_sizing(2500.0, 0.004, sl, 0.0, _stats(q=0.9, f_star=0.9), RC, 5.5, 25.0,
                           risk_notional=20.0)
        assert isinstance(u.leverage, int) and u.leverage >= 1
        if u.leverage > 1:                       # L=1 tabanında fiziksel tasfiye yok sayılır
            assert 1.0 / u.leverage - 0.005 >= 2.0 * sl - 1e-9
    wide = unified_sizing(2500.0, 0.004, 0.30, 0.0, _stats(q=0.9, f_star=0.9), RC, 5.5, 25.0, 20.0)
    assert wide.leverage == 1 and "liquidation" in wide.caps


def test_green_leverage_rounds_down_not_up_against_cap():
    """cap=2.7 iken round(2.6)=3 tasfiye tavanını delerdi → taban(2.7)=2 kullanılmalı."""
    sl = (1 / 2.7 - 0.005) / 2.0
    u = unified_sizing(2500.0, 0.004, sl, 0.0, _stats(q=0.9, f_star=0.9), RC, 5.5, 25.0, 20.0)
    assert u.leverage == 2


# ------------------------------------------------------------------ marjin
def test_green_margin_budget_binds_for_small_balance():
    u = unified_sizing(10.0, 0.004, 0.011, 0.0, _stats(q=0.9, f_star=0.9), RC, 5.5, 25.0,
                       risk_notional=None)
    assert u.margin <= RC["max_total_margin_pct"] * 10.0 + 1e-9
    assert "margin" in u.caps


def test_green_margin_consistency_S_over_L():
    u = unified_sizing(2500.0, 0.02, 0.0112, 0.05, _stats(), RC, 5.5, 25.0, risk_notional=20.0)
    assert u.margin == pytest.approx(u.notional / u.leverage)


def test_green_drawdown_reduces_leverage_through_unified():
    lo = unified_sizing(2500.0, 0.03, 0.045, 0.0, _stats(q=0.5, f_star=0.9), RC, 5.5, 25.0, 25.0)
    hi = unified_sizing(2500.0, 0.03, 0.045, 0.6, _stats(q=0.5, f_star=0.9), RC, 5.5, 25.0, 25.0)
    assert hi.leverage <= lo.leverage


# ------------------------------------------------------------------ open_coin entegrasyonu
def _ledger(tmp_path, pnls):
    import sqlite3
    db = tmp_path / "led.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY, trading_pair TEXT, "
              "net_pnl_quote REAL, closed_at TEXT, recorded_at TEXT NOT NULL, source TEXT)")
    for i, p in enumerate(pnls):
        c.execute("INSERT INTO trade_ledger VALUES (?,?,?,?,?, 'sync')",
                  (f"e{i}", "BTCUSDT", p, None, f"2026-11-12T00:00:{i:02d}"))
    c.commit(); c.close()
    return str(db)


class _FakeBinance:
    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"}, "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 100.0

    def order_book(self, symbol, limit=20):
        return {"bids": [["99.99", "1000"]], "asks": [["100.01", "1000"]]}


class _FakeRisk:
    current_drawdown = 0.0
    daily_trades = 0
    consecutive_losses = 0

    def approve_trade(self, balance, stop):       # RiskEngine'in onayladığı (küçük) notional
        return 10.0, "approved"


def test_green_open_coin_on_respects_riskengine_notional(monkeypatch, tmp_path, caplog):
    """GREEN: 'on' + güçlü kenar: Faz 3 S>10 isterdi; RiskEngine 10 onayladı → amount≈10 USDT."""
    import logging
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    monkeypatch.setattr(auto_trader, "SCIENTIFIC_SIZING", "on")
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "off")
    monkeypatch.setattr(auto_trader, "STRATEGY_CONFIRMATION", "off")
    monkeypatch.setattr(auto_trader, "SL_LEDGER_DB", _ledger(tmp_path, [2.0] * 18 + [-1.0] * 12))
    monkeypatch.setattr(auto_trader, "check_liquidity", lambda b, s: (True, "ok"))
    monkeypatch.setattr(auto_trader, "check_correlation_limit", lambda s, b: True)
    monkeypatch.setattr(auto_trader, "calculate_atr", lambda *a, **k: 0.01)
    monkeypatch.setattr(auto_trader, "get_real_slippage", lambda b, s: 0.0002)
    monkeypatch.setitem(auto_trader.RISK_CONFIG, "risk_guards_enabled", True)
    coin = {"symbol": "XUSDT", "lastPrice": "100", "quoteVolume": "1e9",
            "priceChangePercent": "-5", "_score": 0.9}
    with caplog.at_level(logging.INFO):
        assert auto_trader.open_coin(_FakeBinance(), None, coin, dry_run=True, risk=_FakeRisk()) is True
    amount_line = [r.message for r in caplog.records if "amount=" in r.message][0]
    notional = float(amount_line.split("≈")[1].split(" ")[0])
    assert notional <= 10.0 + 0.1                      # RiskEngine sınırı (lot yuvarlaması payı)
    assert any("risk_engine" in r.message for r in caplog.records if "Faz3[on]" in r.message)
