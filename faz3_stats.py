"""faz3_stats.py — FAZ3 istatistik çekirdeği (saf python; deterministik seed).

Protokol PREREGISTRATION_FAZ3.md §5'te ÖN-KAYITLIDIR:
  * block bootstrap %95 CI (blok=21 gözlem, 2000 yeniden-örnek, tohum=7)
  * Deflated Sharpe Ratio — Bailey & López de Prado: gözlenen SR'nin, N
    denemenin maksimum-beklenen SR'sini aşma olasılığı; çarpıklık/uzunkuyruk
    düzeltmeli standart hata.
  * PBO (Probability of Backtest Overfit) — CSCV: blokların IS/OOS
    bölünmelerinde IS-en-iyi'nin OOS bağıl-rangi; logit negatif oranı.
Sahte-kesinlik YASAK: tüm fonksiyonlar yetersiz veri → None döner.
"""
import math
import random
import statistics as st

Z95 = 1.959963984540054


def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def block_bootstrap_mean_ci(values, block=21, n_res=2000, seed=7):
    """Ortalamanın block-bootstrap %95 CI'ı. Yetersiz veri → None.
    Bloklar ardışık-korumanlı (otokorelasyon için); döngüsel sarılma yok —
    dizi sonundan taşan blok kırpılır."""
    vals = [float(v) for v in values if v is not None and math.isfinite(v)]
    if len(vals) < 2 * block:
        return None
    rng = random.Random(seed)
    n = len(vals)
    means = []
    for _ in range(n_res):
        sample = []
        while len(sample) < n:
            s = rng.randrange(0, n - block + 1)
            sample.extend(vals[s:s + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    lo = means[int(0.025 * n_res)]
    hi = means[min(int(0.975 * n_res), n_res - 1)]
    return (lo, hi)


def sharpe_daily(daily_returns, periods=252):
    r = [float(x) for x in daily_returns if x is not None and math.isfinite(x)]
    if len(r) < 30 or st.pstdev(r) == 0:
        return None
    return st.mean(r) / st.pstdev(r) * math.sqrt(periods)


def _skew(r):
    n = len(r)
    m = st.mean(r)
    s = st.pstdev(r)
    if s == 0:
        return 0.0
    return sum((x - m) ** 3 for x in r) / n / s ** 3


def _kurt(r):
    n = len(r)
    m = st.mean(r)
    s = st.pstdev(r)
    if s == 0:
        return 0.0
    return sum((x - m) ** 4 for x in r) / n / s ** 4


def expected_max_sharpe(trial_sharpes, n_trials=None):
    """E[max SR] ≈ sqrt(2 ln N)·σ_SR + (1−γ)/... — yaklaşıklık:
    sr_max_exp = sqrt(2 ln N) * std(trial SR'leri) (Bailey-LdP basit hâli;
    trial SR'lerinin yayılımı σ_tahmini yerine kullanılır — MUHAFAZAKÂR)."""
    tr = [float(x) for x in trial_sharpes if x is not None and math.isfinite(x)]
    N = n_trials or len(tr)
    if N < 2 or len(tr) < 2:
        return None
    sd = st.pstdev(tr)
    return math.sqrt(2.0 * math.log(N)) * sd


def deflated_sharpe_ratio(sr_period, trial_sharpes, n_obs, skew=None, kurt=None):
    """DSR = Φ( (SR − E[maxSR]) / sqrt((1 − S·SR + (K−1)/4·SR²)/(n−1)) ).

    sr_period: GÖZLEM-BAŞINA (günlük) Sharpe (yıllıklaştırılmamış!);
    trial_sharpes: grid'in TÜM denemelerinin günlük SR'leri (37 bütçe);
    n_obs: günlük gözlem sayısı. Yetersiz → None."""
    if sr_period is None or n_obs is None or n_obs < 30:
        return None
    emax = expected_max_sharpe(trial_sharpes)
    if emax is None:
        return None
    sr = float(sr_period)
    if skew is None or kurt is None:
        return None
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return None
    se = math.sqrt(denom / (n_obs - 1))
    return _phi((sr - emax) / se)


def pbo_cscv(trial_return_matrix, n_blocks=16, n_combos=100, seed=11):
    """PBO — CSCV. trial_return_matrix: [n_trial][n_period] getiriler.
    Bloklara böler; seeded-örneklenmiş IS/OOS tamamlarında IS-en-iyi'nin OOS
    bağıl rangi λ=rank/(T+1); logit(λ); PBO = P(logit<0). Uygun değil → None."""
    T = len(trial_return_matrix)
    if T < 3:
        return None
    n_per = min(len(r) for r in trial_return_matrix)
    if n_per < 2 * n_blocks:
        return None
    blocks = [[trial[j * n_per // n_blocks:(j + 1) * n_per // n_blocks]
               for j in range(n_blocks)] for trial in trial_return_matrix]
    means = [[sum(b) / len(b) if b else 0.0 for b in tb] for tb in blocks]

    def perf(idxs):
        out = []
        for tb in means:
            v = [tb[i] for i in idxs]
            out.append(sum(v) / len(v) if v else 0.0)
        return out

    rng = random.Random(seed)
    all_idx = list(range(n_blocks))
    half = n_blocks // 2
    neg = tot = 0
    combos = set()
    while len(combos) < n_combos:
        isets = tuple(sorted(rng.sample(all_idx, half)))
        if isets in combos:
            continue
        combos.add(isets)
        o = tuple(i for i in all_idx if i not in isets)
        is_p, oos_p = perf(isets), perf(o)
        best_is = max(range(T), key=lambda t: is_p[t])
        rank = 1 + sum(1 for t in range(T) if oos_p[t] < oos_p[best_is])
        lam = rank / (T + 1.0)
        lam = min(max(lam, 1.0 / (T + 1.0)), 1.0 - 1.0 / (T + 1.0))
        if math.log(lam / (1 - lam)) < 0:
            neg += 1
        tot += 1
    return neg / tot if tot else None
