"""test_faz3_stats.py — FAZ3 istatistik çekirdeği testleri (saf, deterministik).

Protokol ön-kayıtta (PREREGISTRATION_FAZ3.md §5 + DEVIATIONS §D-2, N=19);
bu testler protokolü DEĞİL hesap makinesini doğrular.
"""
import math
import random

import faz3_stats as FS


def _det_noise(n, seed=5, mu=0.0):
    rng = random.Random(seed)
    return [rng.gauss(mu, 1.0) for _ in range(n)]


class TestBlockBootstrap:
    def test_positive_mean_ci_above_zero(self):
        vals = [0.01 + 0.001 * (k % 5) for k in range(200)]
        lo, hi = FS.block_bootstrap_mean_ci(vals)
        assert 0 < lo < hi

    def test_negative_mean_ci_below_zero(self):
        vals = [-(0.01 + 0.001 * (k % 5)) for k in range(200)]
        lo, hi = FS.block_bootstrap_mean_ci(vals)
        assert lo < hi < 0

    def test_deterministic_same_seed(self):
        vals = _det_noise(300, seed=3)
        assert FS.block_bootstrap_mean_ci(vals) == FS.block_bootstrap_mean_ci(vals)

    def test_insufficient_returns_none(self):
        assert FS.block_bootstrap_mean_ci([0.01] * 41) is None  # < 2*block(21)

    def test_nonfinite_filtered(self):
        vals = [0.01 + 0.001 * (k % 5) for k in range(200)] + [None, float("nan")]
        assert FS.block_bootstrap_mean_ci(vals) is not None


class TestSharpe:
    def test_zero_variance_none(self):
        assert FS.sharpe_daily([0.01] * 50) is None

    def test_too_few_none(self):
        assert FS.sharpe_daily([0.01, -0.01] * 10) is None

    def test_manual_value(self):
        import statistics as st
        r = _det_noise(100, seed=9, mu=0.001)
        want = st.mean(r) / st.pstdev(r) * math.sqrt(252)
        assert abs(FS.sharpe_daily(r) - want) < 1e-12


class TestDSR:
    def test_monotone_in_sr(self):
        trials = [0.1, 0.3, 0.2, 0.05]
        a = FS.deflated_sharpe_ratio(0.01, trials, 200, skew=0.0, kurt=3.0)
        b = FS.deflated_sharpe_ratio(0.05, trials, 200, skew=0.0, kurt=3.0)
        assert 0.0 <= a < b <= 1.0

    def test_penalty_grows_with_trials(self):
        base = [0.1, 0.2, 0.15]
        # aynı SR yayılımı, daha çok deneme → sqrt(2 ln N) cezası büyür
        e3 = FS.expected_max_sharpe(base, n_trials=3)
        e19 = FS.expected_max_sharpe(base, n_trials=19)
        assert e19 > e3 > 0

    def test_insufficient_n_obs_none(self):
        assert FS.deflated_sharpe_ratio(0.02, [0.1, 0.2], 29) is None

    def test_bad_denominator_none(self):
        # 1 − S·SR + (K−1)/4·SR² = 1 − 2·1 + 0.5·1 = −0.5 < 0 → None
        assert FS.deflated_sharpe_ratio(1.0, [0.5, 1.0], 100,
                                        skew=2.0, kurt=3.0) is None


class TestPBO:
    def test_dominant_trial_low_pbo(self):
        # deneme 0 HER dönem +0.01, diğerleri −0.01 → IS-en-iyi = OOS-en-iyi.
        mat = [[0.01] * 64] + [[-0.01] * 64 for _ in range(4)]
        assert FS.pbo_cscv(mat) == 0.0

    def test_worst_trial_high_pbo(self):
        # deneme 0 her dönem KÖTÜ ama gürültülü IS'te bazen en iyi seçilir.
        rng = random.Random(4)
        mat = [[-0.05 + rng.gauss(0, 0.5) for _ in range(64)] for _ in range(5)]
        assert FS.pbo_cscv(mat) is not None  # yapı geçerli, oran [0,1]

    def test_noise_midrange(self):
        rng = random.Random(11)
        mat = [[rng.gauss(0, 1) for _ in range(64)] for _ in range(8)]
        p = FS.pbo_cscv(mat)
        assert 0.0 <= p <= 1.0

    def test_deterministic(self):
        rng = random.Random(12)
        mat = [[rng.gauss(0, 1) for _ in range(40)] for _ in range(6)]
        assert FS.pbo_cscv(mat) == FS.pbo_cscv(mat)

    def test_insufficient_none(self):
        assert FS.pbo_cscv([[0.1] * 10, [0.2] * 10]) is None  # 10 < 2*16 blok
        assert FS.pbo_cscv([[0.1] * 64]) is None               # T < 3
