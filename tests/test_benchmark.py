"""FAZ11 Katman 5 — benchmark ve doğruluk testleri."""
import math

import pytest

import benchmark as bm


def test_equity_curve_compounds():
    assert bm.equity_curve([0.10, -0.10]) == pytest.approx([1.0, 1.1, 0.99])


def test_equity_curve_of_empty_series_is_flat():
    assert bm.equity_curve([]) == [1.0]


def test_max_drawdown_is_peak_to_trough():
    assert bm.max_drawdown([1.0, 2.0, 1.0]) == pytest.approx(0.5)


def test_max_drawdown_of_a_monotone_curve_is_zero():
    assert bm.max_drawdown([1.0, 1.1, 1.2]) == 0.0


def test_benchmark_total_return_matches_compounding():
    st = bm.benchmark_equity([0.01] * 10)
    assert st.n == 10 and st.total_return == pytest.approx(1.01 ** 10 - 1)


def test_benchmark_win_rate_counts_positive_periods():
    st = bm.benchmark_equity([0.01, -0.01, 0.02, 0.0])
    assert st.win_rate == pytest.approx(0.5)


def test_benchmark_sharpe_is_zero_without_variance():
    assert bm.benchmark_equity([0.01] * 5).sharpe == 0.0


def test_benchmark_sharpe_is_positive_for_a_positive_drift():
    assert bm.benchmark_equity([0.01, -0.005] * 50).sharpe > 0


def test_benchmark_sortino_ignores_upside_volatility():
    st = bm.benchmark_equity([0.05, 0.05, -0.01])
    assert st.sortino > st.sharpe


def test_benchmark_calmar_uses_cagr_over_drawdown():
    st = bm.benchmark_equity([0.10, -0.20, 0.05])
    assert st.max_drawdown > 0 and st.calmar == pytest.approx(st.cagr / st.max_drawdown)


def test_benchmark_of_empty_series_is_neutral():
    st = bm.benchmark_equity([])
    assert st.n == 0 and st.total_return == 0.0 and st.sharpe == 0.0


def test_benchmark_of_single_period_has_no_sharpe():
    st = bm.benchmark_equity([0.02])
    assert st.n == 1 and st.sharpe == 0.0 and st.total_return == pytest.approx(0.02)


def test_benchmark_as_dict_is_json_safe():
    d = bm.benchmark_equity([0.01, -0.01]).as_dict()
    assert d["n"] == 2 and isinstance(d["sharpe"], float)


def test_compare_to_baseline_beats_cash():
    c = bm.compare_to_baseline([0.01] * 5)
    assert c.beats_baseline and c.excess > 0 and c.baseline_total == 0.0


def test_compare_to_baseline_loses_to_a_stronger_baseline():
    c = bm.compare_to_baseline([0.001] * 5, [0.01] * 5)
    assert not c.beats_baseline and c.excess < 0


def test_compare_to_baseline_flags_length_mismatch():
    c = bm.compare_to_baseline([0.01] * 3, [0.01] * 5)
    assert any("length_mismatch" in n for n in c.notes)


def test_compare_to_baseline_flags_empty_series():
    c = bm.compare_to_baseline([])
    assert any("empty_series" in n for n in c.notes) and not c.beats_baseline


def test_brier_score_of_perfect_predictions_is_zero():
    assert bm.brier_score([1.0, 0.0], [True, False]) == pytest.approx(0.0)


def test_brier_score_of_worst_predictions_is_one():
    assert bm.brier_score([0.0, 1.0], [True, False]) == pytest.approx(1.0)


def test_brier_score_of_empty_input_is_fail_closed():
    assert bm.brier_score([], []) == 1.0


def test_log_loss_is_zero_for_confident_correct_predictions():
    assert bm.log_loss([0.999999, 0.000001], [True, False]) < 1e-5


def test_log_loss_survives_a_zero_probability():
    assert math.isfinite(bm.log_loss([0.0, 1.0], [True, False]))


def test_calibration_error_is_zero_for_a_calibrated_model():
    probs = [0.5] * 100
    outcomes = [True] * 50 + [False] * 50
    assert bm.calibration_error(probs, outcomes, bins=10) == pytest.approx(0.0)


def test_calibration_error_detects_overconfidence():
    probs = [0.9] * 100
    outcomes = [True] * 50 + [False] * 50
    assert bm.calibration_error(probs, outcomes, bins=10) == pytest.approx(0.4)


def test_accuracy_report_hit_rate_and_brier():
    st = bm.accuracy_report([0.9, 0.1], [True, False])
    assert st.n == 2 and st.hit_rate == pytest.approx(1.0)
    assert st.brier == pytest.approx(0.01)


def test_accuracy_report_of_empty_input_is_fail_closed():
    st = bm.accuracy_report([], [])
    assert st.n == 0 and st.brier == 1.0 and st.log_loss == 1.0


def test_accuracy_report_as_dict_is_json_safe():
    d = bm.accuracy_report([0.6, 0.4], [True, False]).as_dict()
    assert d["n"] == 2 and isinstance(d["ece"], float)


def test_skill_score_is_positive_when_better_than_the_base_rate():
    assert bm.skill_score(0.01, 0.5) == pytest.approx(0.96)


def test_skill_score_is_negative_when_worse_than_the_base_rate():
    assert bm.skill_score(0.30, 0.5) < 0


def test_skill_score_is_zero_for_a_degenerate_base_rate():
    assert bm.skill_score(0.1, 0.0) == 0.0
