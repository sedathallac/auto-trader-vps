"""FAZ11 Katman 4 — 12 adımlı uçuş öncesi kontrol testleri."""
import pytest

import preflight as pf
import sizing_policy as sp


class Cfg:
    is_live = True

    def validate(self):
        return self


def providers(**over):
    base = dict(
        cfg=Cfg(),
        hb_offline=0,
        account={"assets": [{"asset": "USDT", "availableBalance": "1000"}], "canTrade": True},
        exchange_info={"symbols": [{"symbol": "BTCUSDT"}]},
        symbol="BTCUSDT",
        filters={"LOT_SIZE": {"stepSize": "0.001"}, "MIN_NOTIONAL": {"notional": "5"}},
        quality={"CQ": 0.8, "quality_vetoes": []},
        leverage=3,
        sl_pct=0.02,
        size=sp.SizeDecision(10.0, "approved"),
        geometry=sp.suggest_sl_tp(0.01),
        risk=(10.0, "approved"),
        exposure={"open_positions": 0, "max_positions": 2, "margin_usage": 0.1,
                  "margin_limit": 0.4},
        edge=(True, "shadow"),
    )
    base.update(over)
    return base


def test_step_names_are_exactly_twelve_and_ordered():
    assert len(pf.STEP_NAMES) == 12
    assert pf.STEP_NAMES[0] == "config" and pf.STEP_NAMES[-1] == "edge"


def test_all_twelve_steps_pass_on_a_healthy_setup():
    r = pf.pre_flight_check(**providers())
    assert r.ok and len(r.steps) == 12 and r.failed_at is None
    assert all(s.ok for s in r.steps)


def test_config_failure_stops_the_chain_immediately():
    r = pf.pre_flight_check(**providers(cfg=None))
    assert not r.ok and r.failed_at == "config" and len(r.steps) == 1


def test_offline_flag_must_be_zero_for_live():
    r = pf.pre_flight_check(**providers(hb_offline=1))
    assert not r.ok and r.failed_at == "offline_flag" and len(r.steps) == 2


def test_offline_flag_must_be_one_for_compare():
    class CompareCfg:
        is_live = False

    r = pf.pre_flight_check(**providers(cfg=CompareCfg(), hb_offline=0))
    assert not r.ok and r.failed_at == "offline_flag"


def test_account_with_zero_balance_is_refused():
    r = pf.pre_flight_check(**providers(
        account={"assets": [{"asset": "USDT", "availableBalance": "0"}], "canTrade": True}))
    assert not r.ok and r.failed_at == "account" and "balance=0" in r.reason


def test_account_with_can_trade_false_is_refused():
    r = pf.pre_flight_check(**providers(
        account={"assets": [{"asset": "USDT", "availableBalance": "10"}], "canTrade": False}))
    assert not r.ok and r.failed_at == "account" and "canTrade" in r.reason


def test_unlisted_symbol_is_refused():
    r = pf.pre_flight_check(**providers(symbol="DOGEUSDT"))
    assert not r.ok and r.failed_at == "exchange_info" and "not listed" in r.reason


def test_missing_step_size_is_refused():
    r = pf.pre_flight_check(**providers(filters={"LOT_SIZE": {}, "MIN_NOTIONAL": {"notional": "5"}}))
    assert not r.ok and r.failed_at == "filters" and "stepSize" in r.reason


def test_missing_min_notional_is_refused():
    r = pf.pre_flight_check(**providers(filters={"LOT_SIZE": {"stepSize": "0.001"}}))
    assert not r.ok and r.failed_at == "filters" and "MIN_NOTIONAL" in r.reason


def test_quality_veto_is_refused():
    r = pf.pre_flight_check(**providers(quality={"CQ": 0.9, "quality_vetoes": ["spread_bps=40>25"]}))
    assert not r.ok and r.failed_at == "quality" and r.reason.startswith("veto:")


def test_unknown_quality_is_refused():
    r = pf.pre_flight_check(**providers(quality={"CQ": None, "quality_vetoes": []}))
    assert not r.ok and r.failed_at == "quality" and r.reason == "no_quality_data"


def test_unsafe_leverage_is_refused():
    r = pf.pre_flight_check(**providers(leverage=40))
    assert not r.ok and r.failed_at == "leverage"


def test_refused_sizing_stops_the_chain():
    r = pf.pre_flight_check(**providers(size=sp.SizeDecision(None, "below_min_notional")))
    assert not r.ok and r.failed_at == "sizing" and "below_min_notional" in r.reason


def test_refused_geometry_stops_the_chain():
    # FAZ14m: TP tabanı 2 x SL olduğu için ölü piyasa R/R kapısını geçiyor;
    # reddi kanıtlamak için daha sıkı bir R/R şartı verilir.
    r = pf.pre_flight_check(**providers(geometry=sp.suggest_sl_tp(0.001, min_rr=3.0)))
    assert not r.ok and r.failed_at == "geometry" and "net_rr" in r.reason


def test_risk_engine_refusal_stops_the_chain():
    r = pf.pre_flight_check(**providers(risk=(None, "halted: drawdown")))
    assert not r.ok and r.failed_at == "risk" and "halted" in r.reason


def test_position_cap_is_enforced():
    r = pf.pre_flight_check(**providers(
        exposure={"open_positions": 2, "max_positions": 2, "margin_usage": 0.1}))
    assert not r.ok and r.failed_at == "exposure" and "open=2>=2" in r.reason


def test_margin_usage_limit_is_enforced():
    r = pf.pre_flight_check(**providers(
        exposure={"open_positions": 0, "max_positions": 2, "margin_usage": 0.9,
                  "margin_limit": 0.4}))
    assert not r.ok and r.failed_at == "exposure" and "margin=" in r.reason


def test_unproven_edge_cell_is_refused():
    r = pf.pre_flight_check(**providers(edge=(False, "no proven edge cell")))
    assert not r.ok and r.failed_at == "edge" and "no proven edge" in r.reason


def test_missing_provider_is_refused_not_skipped():
    r = pf.pre_flight_check(**providers(edge=None))
    assert not r.ok and r.failed_at == "edge" and r.reason == "not_provided"


def test_a_raising_provider_is_caught_and_refused():
    def boom():
        raise RuntimeError("exchange down")

    r = pf.pre_flight_check(**providers(account=boom))
    assert not r.ok and r.failed_at == "account" and "exchange down" in r.reason


def test_callable_providers_are_supported():
    r = pf.pre_flight_check(**providers(
        account=lambda: {"assets": [{"asset": "USDT", "availableBalance": "500"}],
                         "canTrade": True},
        edge=lambda: (True, "shadow")))
    assert r.ok and len(r.steps) == 12


def test_result_as_dict_is_json_safe():
    d = pf.pre_flight_check(**providers()).as_dict()
    assert d["ok"] is True and len(d["steps"]) == 12
    assert d["steps"][0]["step"] == "config"


def test_step_lookup_returns_the_named_step():
    r = pf.pre_flight_check(**providers())
    assert r.step("quality").ok is True and r.step("nope") is None
