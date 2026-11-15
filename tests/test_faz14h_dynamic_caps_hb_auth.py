"""FAZ14h — sabit tavanlarin kaldirilmasi + HB auth gerekliligi kanitlari.

P1: RISK_CONFIG["max_notional"] sabit USDT tavani degil; baglayici tavan
    max_position_pct (equity %20). 200 USDT tavani 504.80'lik matematik tavani eziyordu.
P2: confirm_min_p_real 0.65 -> 0.40; kalibre hucreler 0.128-0.725 uretiyor.
SORUN 1: HB API GERCEKTEN kullaniliyor (HBAPI.create_executor -> POST /executors/),
    bu yuzden kimlik bilgisi zorunlu; fail-closed kaldirilamaz.
"""
import inspect

import pytest

import auto_trader
import strategy_engine


# ------------------------------------------------------------------ P1
def test_max_notional_dynamic():
    """Sabit 200 USDT tavani kalkti; tavan artik equity yuzdesinden gelir."""
    assert auto_trader.RISK_CONFIG["max_notional"] >= 999999.0
    assert auto_trader.RISK_CONFIG["max_position_pct"] == 20.0


def test_max_position_pct_is_the_binding_cap():
    """Equity 2524.21 -> %20 = 504.84; sabit tavan bunu ezmemeli."""
    equity = 2524.21
    pct_cap = equity * auto_trader.RISK_CONFIG["max_position_pct"] / 100.0
    assert pct_cap == pytest.approx(504.842, abs=1e-3)
    assert min(pct_cap, auto_trader.RISK_CONFIG["max_notional"]) == pytest.approx(pct_cap)


def test_sizing_policy_uses_pct_cap_not_fixed_notional():
    import sizing_policy
    d = sizing_policy.size_position(
        2524.21, 0.015, quality=0.9,
        cfg={"max_notional": 999999.0, "max_position_pct": 20.0,
             "risk_per_trade_pct": 0.5, "kelly_fraction": 0.25,
             "fee_pct_round_trip": 0.0008, "slippage_pct_round_trip": 0.0002,
             "leverage": 5})
    assert d.notional > 200.0, "sabit 200 tavani hala baglayici"
    assert d.notional <= 504.85
    assert "max_position_pct" in d.caps


# ------------------------------------------------------------------ P2
def test_min_p_real_threshold_0_40():
    assert strategy_engine.CONFIRM_DEFAULTS["confirm_min_p_real"] == 0.40


def test_calibrated_cell_above_new_threshold():
    """Olculen kalibre hucre 0.725 yeni esigi gecer, eski 0.65'i de gecerdi;
    0.128 ve 0.500 ise yalniz yeni esikte gecer."""
    thr = strategy_engine.CONFIRM_DEFAULTS["confirm_min_p_real"]
    assert 0.725 >= thr
    assert 0.500 >= thr
    assert 0.128 < thr


def test_threshold_is_read_from_config_not_hardcoded():
    src = inspect.getsource(strategy_engine)
    assert '"confirm_min_p_real": 0.40' in src
    assert '"confirm_min_p_real": 0.65' not in src


# ------------------------------------------------------------------ SORUN 1
def test_hb_auth_required_because_hb_api_is_used():
    """HB API kullaniliyor: create_executor Hummingbot /executors/ endpoint'ine POST atar."""
    src = inspect.getsource(auto_trader.HBAPI.create_executor)
    assert "/executors/" in src
    assert "auth=self.auth" in src


def test_hb_auth_guard_is_fail_closed_in_live():
    """Kimlik yoksa emir yolu kapanir; sessizce kimliksiz istek atilmaz."""
    src = inspect.getsource(auto_trader)
    assert "HB_USERNAME/HB_PASSWORD yok" in src
    assert "_HB_AUTH_BROKEN = True" in src


def test_hb_auth_not_required_when_hb_offline():
    """HB_OFFLINE=1 iken HB API hic cagrilmaz, kimlik bos olabilir."""
    src = inspect.getsource(auto_trader)
    assert "if not HB_OFFLINE and (not hu or not hp):" in src


def test_hb_auth_not_required_when_hb_api_unused():
    """HB_OFFLINE=1 (compare/shadow) yolunda kimlik kapisi devreye girmez."""
    assert auto_trader.HB_OFFLINE is True or auto_trader.HB_OFFLINE is False
    src = inspect.getsource(auto_trader)
    guard = "if not HB_OFFLINE and (not hu or not hp):"
    assert guard in src, "kapi HB_OFFLINE ile kisa devre olmali"
