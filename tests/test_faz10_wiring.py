"""FAZ10 — edge_engine'in üretim koduna bağlanmasının kanıtı.

Kapsam: RiskEngine net R/R + bütçe kapıları, yol-bağımlı outcome'un
strategy_engine'e bağlanması, SignalStore.resolved_rows ve canlı edge kapısı.
"""
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import auto_trader
import edge_engine as ee
import risk_engine as re_mod
import strategy_engine as se


SCHEMA = """
CREATE TABLE risk_state (
    id INTEGER PRIMARY KEY, peak_equity REAL, consecutive_losses INTEGER,
    daily_trades INTEGER, daily_pnl REAL, last_reset_date TEXT,
    is_halted INTEGER, halt_reason TEXT, updated_at TEXT
);
CREATE TABLE trade_ledger (
    executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL,
    closed_at TEXT, recorded_at TEXT, source TEXT
);
INSERT INTO risk_state (id, peak_equity, consecutive_losses, daily_trades, daily_pnl, is_halted)
VALUES (1, 0, 0, 0, 0, 0);
"""

BASE_CFG = {
    "max_drawdown_pct": 0.20, "max_total_margin_pct": 0.40,
    "max_consecutive_losses": 3, "max_trades_per_day": 20,
    "risk_per_trade_pct": 0.015, "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008, "slippage_pct_round_trip": 0.0004,
    "min_net_reward_risk": 1.20, "min_notional_policy": "refuse",
    "min_notional": 5.5, "max_notional": 25.0,
}


def make_risk(tmp, **over):
    db = Path(tmp) / "risk_ledger.db"
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA)
    cfg = dict(BASE_CFG, db_file=str(db), **over)
    return re_mod.RiskEngine(cfg)


# ------------------------------------------------------------------ RiskEngine
def test_risk_engine_refuses_low_net_reward_risk():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp)
        n, status = r.approve_trade(1000.0, 0.03, take_profit_pct=0.031)
        assert n is None and status.startswith("net_rr")


def test_risk_engine_approves_healthy_net_reward_risk():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp)
        n, status = r.approve_trade(1000.0, 0.02, take_profit_pct=0.05)
        assert status == "approved" and 5.5 <= n <= 25.0


def test_risk_engine_refuses_instead_of_breaking_its_loss_budget():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp)
        n, status = r.approve_trade(50.0, 0.08)
        assert n is None and "below_min_notional" in status


def test_risk_engine_upsize_policy_is_still_available_for_legacy_callers():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp, min_notional_policy="upsize")
        n, status = r.approve_trade(50.0, 0.08)
        assert n == pytest.approx(5.5) and status == "approved"


def test_risk_engine_loss_at_stop_never_exceeds_the_budget():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp)
        balance, sl = 1000.0, 0.02
        n, _ = r.approve_trade(balance, sl)
        assert n * (sl + 0.0012) <= balance * 0.015 * 0.25 + 1e-9


def test_risk_engine_still_halts_on_daily_loss():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp, max_daily_loss_usdt=20)
        r.daily_pnl = -20.0
        n, status = r.approve_trade(1000.0, 0.02, take_profit_pct=0.05)
        assert n is None and "daily_loss_limit" in status


def test_equity_drawdown_halts_even_before_realised_loss():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp, max_daily_loss_pct=0.05)
        r.update_equity(1000.0)
        r.update_equity(940.0)
        assert r.is_halted and "daily_loss_pct" in r.halt_reason


def test_equity_within_the_daily_limit_does_not_halt():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp, max_daily_loss_pct=0.05)
        r.update_equity(1000.0)
        r.update_equity(960.0)
        assert not r.is_halted


def test_daily_reset_clears_the_equity_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_risk(tmp, max_daily_loss_pct=0.05)
        r.update_equity(1000.0)
        r.reset_daily()
        assert r.day_start_equity == 0.0


# ------------------------------------------------------------------ strategy_engine
def test_outcome_from_bars_with_tp_is_path_dependent():
    # Son kapanış +5% ama stop 2. mumda tetiklendi → gerçek sonuç ZARAR.
    real, net, bars = se.outcome_from_bars(1, 100.0, [101, 106], [99, 96], [100, 105],
                                           2, 0.0008, 0.03, tp_pct=0.05)
    assert real is False and net == pytest.approx(-0.03 - 0.0008) and bars == 2


def test_outcome_from_bars_without_tp_keeps_legacy_semantics():
    real, fwd, persist = se.outcome_from_bars(1, 100.0, [101, 106], [99, 98], [99.5, 105],
                                              2, 0.0008, 0.03)
    assert real is True and fwd == pytest.approx(0.05) and persist == 1


def test_outcome_from_bars_tp_first_is_a_win():
    real, net, bars = se.outcome_from_bars(1, 100.0, [106, 90], [99, 85], [105, 88],
                                           2, 0.0008, 0.03, tp_pct=0.05)
    assert real is True and net == pytest.approx(0.05 - 0.0008) and bars == 1


def test_signal_store_resolved_rows_returns_only_scored_rows():
    with tempfile.TemporaryDirectory() as tmp:
        store = se.SignalStore(str(Path(tmp) / "so.db"))
        reading = SimpleNamespace(name="rsi_extreme", family="exhaustion",
                                  direction=1, strength=0.8)
        assert store.record_resolved("BTCUSDT", reading, "range", 1000, 100.0, 8,
                                     True, 0.01, 4, source="offline", sl_pct=0.02)
        rows = store.resolved_rows()
        assert len(rows) == 1 and rows[0]["family"] == "exhaustion"
        assert rows[0]["fwd_ret"] == pytest.approx(0.01)


# ------------------------------------------------------------------ canlı edge kapısı
class FakeCfg(dict):
    is_live = True


def _seed_cell(db_path, family, regime, fwd, n=200, persist=5):
    store = se.SignalStore(str(db_path))
    reading = SimpleNamespace(name="sig", family=family, direction=1, strength=0.8)
    for i in range(n):
        jitter = ((i % 5) - 2) * 0.001
        val = fwd + jitter
        store.record_resolved("BTCUSDT", reading, regime, 1000 + i, 100.0, 8,
                              val > 0, val, persist, source="offline", sl_pct=0.02)


def _wire(monkeypatch, tmp, family="volume", regime="range"):
    db = Path(tmp) / "signal_outcomes.db"
    monkeypatch.setattr(se, "SIGNAL_DB", str(db))
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: FakeCfg())
    monkeypatch.setattr(auto_trader, "_EDGE_CACHE", {"ts": 0.0, "decisions": None})
    auto_trader._LAST_CONFIRM.clear()
    auto_trader._LAST_CONFIRM.update({"families": {family: "sig"}, "regime": regime})
    return db


def test_edge_gate_is_open_when_not_live(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: SimpleNamespace(is_live=False))
    ok, reason = auto_trader._live_edge_gate()
    assert ok and "not live" in reason


def test_edge_gate_shadow_mode_allows_data_collection(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _wire(monkeypatch, tmp)
        ok, reason = auto_trader._live_edge_gate()
        assert ok and reason.startswith("shadow")


def test_edge_gate_refuses_an_unproven_cell(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = _wire(monkeypatch, tmp)
        _seed_cell(db, "volume", "range", -0.004)
        ok, reason = auto_trader._live_edge_gate()
        assert not ok and "no proven edge cell" in reason


def test_edge_gate_allows_a_proven_cell(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = _wire(monkeypatch, tmp)
        _seed_cell(db, "volume", "range", 0.004)
        ok, reason = auto_trader._live_edge_gate()
        assert ok and "mean_net" in reason


def test_edge_gate_refuses_when_no_family_confirmed(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _wire(monkeypatch, tmp)
        auto_trader._LAST_CONFIRM.clear()
        ok, reason = auto_trader._live_edge_gate()
        assert not ok and "no confirming family" in reason


def test_edge_decisions_are_cached(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = _wire(monkeypatch, tmp)
        _seed_cell(db, "volume", "range", 0.004)
        store = se.SignalStore(str(db))
        first = auto_trader._live_edge_decisions(store, BASE_CFG, ttl=300)
        second = auto_trader._live_edge_decisions(store, BASE_CFG, ttl=300)
        assert first is second


def test_risk_config_carries_the_new_gates():
    assert auto_trader.RISK_CONFIG["min_net_reward_risk"] >= 1.0
    assert auto_trader.RISK_CONFIG["min_notional_policy"] == "refuse"
    assert auto_trader.RISK_CONFIG["slippage_pct_round_trip"] > 0
    assert auto_trader.RISK_CONFIG["max_daily_loss_pct"] > 0


def test_net_expectancy_report_is_cost_aware():
    import analyze_live_data as ald
    rows = [{"fwd": 0.02} for _ in range(50)] + [{"fwd": -0.005} for _ in range(50)]
    rep = ald.net_expectancy_report(rows, cost=0.0012)
    assert rep["n"] == 100
    assert rep["mean_net"] == pytest.approx(0.0075 - 0.0012)
    assert rep["ci"][0] > 0 and rep["profit_factor"] > 1


def test_render_surfaces_the_expectancy_block():
    import analyze_live_data as ald
    rep = {"verdict": "EDGE", "decision": "d",
           "whale": {"verdict": "EDGE", "n_resolved_raw": 0, "n_declustered": 0,
                     "distinct_days": 0, "n_accepted": 0, "n_unresolved_skipped": 0},
           "strategy": {"verdict": "EDGE", "n_declustered": 0,
                        "expectancy": {"n": 10, "mean_net": 0.001, "ci": [0.0001, 0.002],
                                       "dsr": 0.97, "profit_factor": 1.4, "payoff": 1.2}}}
    assert "expectancy:" in ald.render(rep)
