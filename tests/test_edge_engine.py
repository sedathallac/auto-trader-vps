"""FAZ10 — edge_engine akademik çekirdek testleri.

Her test tek bir iddiayı kanıtlar: maliyet muhasebesi, yol-bağımlı sonuç,
istatistiksel çıkarım, çoklu-test düzeltmesi, sızıntısız walk-forward,
Kelly boyutlandırma, R/R kapısı ve monoton trailing stop.
"""
import math
import statistics

import pytest

import edge_engine as ee


# ------------------------------------------------------------------ yuvarlama
def test_round_step_down_never_exceeds_requested_notional():
    assert ee.round_step(10.987654, 0.001) == pytest.approx(10.987)
    assert ee.round_step(10.987654, 0.001) * 1.0 <= 10.987654


def test_round_step_up_is_the_ceiling():
    assert ee.round_step(10.987001, 0.01, "up") == pytest.approx(10.99)


def test_round_step_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ee.round_step(1.0, 0)
    with pytest.raises(ValueError):
        ee.round_step(1.0, 0.1, "sideways")


def test_round_step_has_no_float_artifacts():
    # 0.1 + 0.2 tarzı ikili kayan nokta hatası Decimal ile kaybolur.
    assert ee.round_step(0.3, 0.1) == pytest.approx(0.3)
    assert ee.round_step(1.15, 0.01) == pytest.approx(1.15)


def test_amount_for_notional_rounds_down_and_accepts():
    amt, ok = ee.amount_for_notional(25.0, 100.0, 0.001, 0.001, 5.0)
    assert ok and amt == pytest.approx(0.25)


def test_amount_for_notional_refuses_below_exchange_minimum():
    amt, ok = ee.amount_for_notional(4.0, 100.0, 0.001, 0.001, 5.0)
    assert not ok and amt * 100.0 < 5.0


def test_amount_for_notional_lifts_to_min_qty():
    amt, ok = ee.amount_for_notional(6.0, 100.0, 0.001, 0.01, 5.0)
    assert ok and amt >= 0.01


def test_amount_for_notional_rejects_zero_price():
    assert ee.amount_for_notional(10.0, 0.0, 0.001) == (0.0, False)


# ------------------------------------------------------------------ maliyet
def test_net_return_long_subtracts_round_trip_costs():
    r = ee.net_return_pct(1, 100.0, 102.0, fee_pct=0.0008, slippage_pct=0.0004)
    assert r == pytest.approx(0.02 - 0.0012)


def test_net_return_short_is_direction_aware():
    r = ee.net_return_pct(-1, 100.0, 98.0, fee_pct=0.0008, slippage_pct=0.0004)
    assert r == pytest.approx(0.02 - 0.0012)


def test_net_return_rejects_invalid_direction():
    with pytest.raises(ValueError):
        ee.net_return_pct(0, 100.0, 101.0)


def test_funding_long_pays_and_short_receives():
    assert ee.funding_cost_pct(0.0001, 8.0, 8.0, 1) == pytest.approx(0.0001)
    assert ee.funding_cost_pct(0.0001, 8.0, 8.0, -1) == pytest.approx(-0.0001)


def test_funding_scales_with_holding_time():
    assert ee.funding_cost_pct(0.0001, 24.0, 8.0, 1) == pytest.approx(0.0003)


def test_slippage_from_depth_is_near_zero_for_tiny_order():
    book = [[100.0, 10.0], [100.1, 10.0]]
    assert ee.slippage_from_depth(book, 10.0, "buy") == pytest.approx(0.0)


def test_slippage_from_depth_walks_the_book():
    book = [[100.0, 0.1], [101.0, 100.0]]
    slip = ee.slippage_from_depth(book, 15.0, "buy")
    assert 0.0 < slip < 0.02


def test_slippage_from_depth_fails_closed_on_thin_book():
    assert ee.slippage_from_depth([[100.0, 0.01]], 1000.0, "buy") == 1.0


# ------------------------------------------------------------------ yol-bağımlı sonuç
def test_first_touch_stop_wins_when_stop_comes_first():
    out = ee.first_touch_outcome(1, 100.0, [101, 102, 103], [99, 96, 99], [100, 97, 102],
                                 0.03, 0.05, 0.0, 0.0)
    assert out.outcome == "sl" and out.bars_held == 2 and out.net_ret == pytest.approx(-0.03)


def test_first_touch_take_profit_wins_when_tp_comes_first():
    out = ee.first_touch_outcome(1, 100.0, [102, 106, 90], [99, 101, 85], [101, 105, 88],
                                 0.03, 0.05, 0.0, 0.0)
    assert out.outcome == "tp" and out.bars_held == 2 and out.net_ret == pytest.approx(0.05)


def test_first_touch_same_bar_is_conservatively_a_stop():
    out = ee.first_touch_outcome(1, 100.0, [106], [96], [100], 0.03, 0.05, 0.0, 0.0)
    assert out.outcome == "sl" and out.ambiguous is True


def test_first_touch_time_exit_uses_last_close():
    out = ee.first_touch_outcome(1, 100.0, [101, 101], [99, 99], [100.5, 101.0],
                                 0.03, 0.05, 0.0, 0.0)
    assert out.outcome == "time" and out.net_ret == pytest.approx(0.01)


def test_first_touch_short_direction_is_mirrored():
    out = ee.first_touch_outcome(-1, 100.0, [104], [99], [103], 0.03, 0.05, 0.0, 0.0)
    assert out.outcome == "sl" and out.net_ret == pytest.approx(-0.03)


def test_first_touch_tracks_mae_and_mfe():
    out = ee.first_touch_outcome(1, 100.0, [103, 104], [98, 99], [102, 103],
                                 0.10, None, 0.0, 0.0)
    assert out.mae == pytest.approx(-0.02) and out.mfe == pytest.approx(0.04)


def test_first_touch_rejects_invalid_inputs():
    assert ee.first_touch_outcome(1, 0.0, [1], [1], [1], 0.01) is None
    assert ee.first_touch_outcome(1, 100.0, [], [], [], 0.01) is None


# ------------------------------------------------------------------ istatistik
def test_expectancy_matches_hand_computation():
    st = ee.expectancy([0.02, -0.01, 0.03, -0.01])
    assert st.n == 4 and st.mean == pytest.approx(0.0075)
    assert st.win_rate == pytest.approx(0.5)
    assert st.payoff == pytest.approx(2.5)
    assert st.profit_factor == pytest.approx(0.05 / 0.02)


def test_expectancy_flags_a_real_edge():
    st = ee.expectancy([0.01] * 50 + [-0.001] * 10)
    assert st.mean > 0 and st.p_value < 0.01 and st.positive


def test_expectancy_of_empty_series_is_neutral():
    st = ee.expectancy([])
    assert st.n == 0 and st.p_value == 1.0 and not st.positive


def test_bootstrap_ci_is_deterministic_and_contains_the_mean():
    xs = [0.01, -0.02, 0.03, 0.005, -0.001] * 20
    a = ee.bootstrap_ci_mean(xs, 500, 0.05, seed=7)
    b = ee.bootstrap_ci_mean(xs, 500, 0.05, seed=7)
    assert a == b and a[0] <= statistics.mean(xs) <= a[1]


def test_bootstrap_ci_narrows_with_more_samples():
    small = ee.bootstrap_ci_mean([0.01, -0.01] * 10, 400, 0.05, seed=1)
    large = ee.bootstrap_ci_mean([0.01, -0.01] * 400, 400, 0.05, seed=1)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_psr_is_zero_without_enough_samples():
    assert ee.probabilistic_sharpe_ratio([0.01, 0.02]) == 0.0


def test_psr_increases_with_the_mean():
    weak = ee.probabilistic_sharpe_ratio([0.001, -0.001] * 100)
    strong = ee.probabilistic_sharpe_ratio([0.01, -0.001] * 100)
    assert strong > weak


def test_dsr_penalises_more_trials():
    xs = [0.01, -0.005] * 100
    assert ee.deflated_sharpe_ratio(xs, 1) > ee.deflated_sharpe_ratio(xs, 500)


def test_dsr_is_zero_for_short_series():
    assert ee.deflated_sharpe_ratio([0.01, 0.02], 10) == 0.0


def test_benjamini_hochberg_known_example():
    rej = ee.benjamini_hochberg([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205], 0.05)
    assert rej[:2] == [True, True] and rej[-1] is False


def test_benjamini_hochberg_handles_empty_and_all_pass():
    assert ee.benjamini_hochberg([]) == []
    assert all(ee.benjamini_hochberg([0.001, 0.002], 0.10))


def test_purged_walk_forward_never_trains_on_the_future():
    for train, test in ee.purged_walk_forward(100, 4, embargo=5):
        assert max(train) < min(test)


def test_purged_walk_forward_embargo_removes_adjacent_samples():
    train, test = ee.purged_walk_forward(100, 4, embargo=5)[0]
    assert len(train) == min(test) - 5


def test_purged_walk_forward_single_fold_is_in_sample():
    assert ee.purged_walk_forward(10, 1) == [(list(range(10)), list(range(10)))]


# ------------------------------------------------------------------ boyutlandırma
def test_fractional_kelly_matches_the_formula():
    assert ee.fractional_kelly(0.6, 1.0, 1.0, 1.0) == pytest.approx(0.2)


def test_fractional_kelly_returns_zero_without_edge():
    assert ee.fractional_kelly(0.4, 1.0, 0.25, 0.25) == 0.0


def test_fractional_kelly_is_capped():
    assert ee.fractional_kelly(0.9, 10.0, 1.0, 0.25) == pytest.approx(0.25)


def test_fractional_kelly_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        ee.fractional_kelly(1.5, 1.0)


def test_risk_notional_refuses_when_budget_cannot_fund_the_minimum():
    n, reason = ee.risk_notional(50.0, 0.015, 0.08, 0.0008, 0.0004, 0.25, 5.5, 25.0, "refuse")
    assert n is None and "below_min_notional" in reason


def test_risk_notional_upsize_policy_keeps_legacy_behaviour():
    n, reason = ee.risk_notional(50.0, 0.015, 0.08, 0.0008, 0.0004, 0.25, 5.5, 25.0, "upsize")
    assert n == pytest.approx(5.5) and reason == "min_notional_upsize"


def test_risk_notional_respects_the_hard_cap():
    n, _ = ee.risk_notional(100000.0, 0.015, 0.01, 0.0008, 0.0004, 0.25, 5.5, 25.0)
    assert n == pytest.approx(25.0)


def test_risk_notional_loss_at_stop_stays_inside_the_budget():
    balance, sl = 1000.0, 0.02
    n, _ = ee.risk_notional(balance, 0.015, sl, 0.0008, 0.0004, 0.25, 5.5, 25.0)
    budget = balance * 0.015 * 0.25
    assert n * (sl + 0.0012) <= budget + 1e-9


def test_net_reward_risk_requires_profit_after_costs():
    rr, ok, _ = ee.net_reward_risk(0.05, 0.03, 0.0008, 0.0004)
    assert ok and rr == pytest.approx((0.05 - 0.0012) / (0.03 + 0.0012))


def test_net_reward_risk_rejects_geometry_below_costs():
    rr, ok, detail = ee.net_reward_risk(0.001, 0.03, 0.0008, 0.0004)
    assert not ok and rr < 0 and detail.startswith("net_win=")


def test_net_reward_risk_rejects_invalid_geometry():
    assert ee.net_reward_risk(0.0, 0.03)[1] is False


# ------------------------------------------------------------------ trailing stop
def test_trailing_stop_moves_to_breakeven_after_one_r():
    new = ee.trailing_stop(1, 100.0, 97.0, 97.0, 103.0, 0.0, 1.0, 1.5)
    assert new == pytest.approx(100.0)


def test_trailing_stop_never_loosens_a_long():
    new = ee.trailing_stop(1, 100.0, 97.0, 99.5, 100.5, 0.02, 1.0, 1.5)
    assert new >= 99.5


def test_trailing_stop_never_crosses_the_price():
    new = ee.trailing_stop(1, 100.0, 97.0, 97.0, 101.0, 0.05, 1.0, 3.0)
    assert new <= 101.0


def test_trailing_stop_is_monotone_for_shorts():
    new = ee.trailing_stop(-1, 100.0, 103.0, 101.0, 98.0, 0.01, 1.0, 1.5)
    assert new <= 101.0 and new >= 98.0


def test_trailing_stop_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        ee.trailing_stop(0, 100.0, 97.0, 97.0, 100.0, 0.01)


# ------------------------------------------------------------------ terfi kapısı
def _positive(n=200, mean=0.004, seed=3, sd=0.01):
    import random
    rng = random.Random(seed)
    return [mean + rng.gauss(0, sd) for _ in range(n)]


def test_cell_decision_refuses_insufficient_samples():
    d = ee.cell_decision("a:range", [0.01] * 10)
    assert not d.allow and "n=10" in d.reason


def test_cell_decision_promotes_a_strong_positive_cell():
    d = ee.cell_decision("a:range", _positive(), [5] * 200, _positive(60), 1)
    assert d.allow and d.ci_low > 0 and d.dsr >= 0.95


def test_cell_decision_refuses_a_negative_cell():
    d = ee.cell_decision("a:range", [-0.004] * 200, [5] * 200, [-0.004] * 60, 1)
    assert not d.allow and "mean_net" in d.reason


def test_cell_decision_refuses_low_persistence():
    d = ee.cell_decision("a:range", _positive(), [1] * 200, _positive(60), 1)
    assert not d.allow and "persist_share" in d.reason


def test_cell_decision_refuses_negative_out_of_sample():
    d = ee.cell_decision("a:range", _positive(), [5] * 200, [-0.01] * 60, 1)
    assert not d.allow and "oos_mean" in d.reason


def test_cell_decision_refuses_when_many_trials_deflate_the_sharpe():
    d = ee.cell_decision("a:range", _positive(200, 0.0006, 3, 0.002), [5] * 200,
                         _positive(60, 0.0006, 3, 0.002), 5000)
    assert not d.allow and "dsr" in d.reason


def test_promotion_gate_applies_fdr_across_cells():
    cells = {
        "a:range": {"net_returns": _positive(), "persist_bars": [5] * 200,
                    "oos_returns": _positive(60)},
        "b:trend": {"net_returns": _positive(200, 0.0002, 9), "persist_bars": [5] * 200,
                    "oos_returns": _positive(60, 0.0002, 9)},
    }
    d = ee.promotion_gate(cells)
    assert d["a:range"].allow and d["a:range"].fdr_pass
    assert not d["b:trend"].allow


def test_gate_lookup_is_fail_closed_for_unknown_cells():
    ok, reason = ee.gate_lookup({}, "volume", "range")
    assert not ok and "no_edge_cell" in reason


def test_cells_from_rows_groups_and_nets_costs():
    rows = [
        {"family": "volume", "regime": "range", "fwd_ret": 0.01, "persist_bars": 4, "entry_ts": 1},
        {"family": "volume", "regime": "range", "fwd_ret": 0.02, "persist_bars": 5, "entry_ts": 2},
        {"family": "structure", "regime": "trend", "fwd_ret": -0.01, "persist_bars": 1, "entry_ts": 3},
    ]
    cells = ee.cells_from_rows(rows, cost=0.0012, oos_frac=0.0)
    assert set(cells) == {"volume:range", "structure:trend"}
    assert cells["volume:range"]["net_returns"][0] == pytest.approx(0.01 - 0.0012)


def test_cells_from_rows_splits_out_of_sample_by_time():
    rows = [{"family": "f", "regime": "r", "fwd_ret": 0.01, "persist_bars": 3, "entry_ts": i}
            for i in range(10)]
    cells = ee.cells_from_rows(rows, cost=0.0, oos_frac=0.30)
    assert len(cells["f:r"]["net_returns"]) == 7
    assert len(cells["f:r"]["oos_returns"]) == 3


def test_cells_from_rows_skips_malformed_rows():
    rows = [{"family": "f", "regime": "r", "fwd_ret": float("nan"), "entry_ts": 1},
            {"family": "f", "regime": "r", "fwd_ret": 0.01, "entry_ts": 2},
            {"nope": 1}]
    cells = ee.cells_from_rows(rows, cost=0.0, oos_frac=0.0)
    assert len(cells["f:r"]["net_returns"]) == 1
