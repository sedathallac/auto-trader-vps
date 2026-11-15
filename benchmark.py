"""benchmark.py — FAZ11 Katman 5: benchmark ve doğruluk ölçümü (SAF).

Amaç: "bot iyileşti mi?" sorusunu öznel olmaktan çıkarıp ölçülebilir kılmak.
Üç eksen:

  1. `benchmark_equity`  — bir getiri serisinden toplam getiri, CAGR, maksimum
                           drawdown, Sharpe, Sortino, Calmar, kazanma oranı.
  2. `compare_to_baseline` — strateji serisini "hep long tut" (buy & hold) ve
                           "sıfır getiri" (nakit) tabanlarıyla karşılaştırır;
                           fark ve üstünlük bayrağı döner.
  3. `accuracy_report`   — sınıflandırma doğruluğu: isabet oranı, Brier skoru,
                           kalibrasyon hatası (ECE) ve log-loss. Modelin
                           "kazanç/zaras" tahmini gerçeğe ne kadar yakın?

Tasarım kuralları:
  * Saf ve deterministik: rastgelelik yok, saat yok.
  * Boş/tek elemanlı seriler çökmez; nötr sonuç ve `n` alanı döner.
  * Tüm oranlar ondalıktır (0.01 = %1); yıllıklandırma `periods_per_year` ile.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TRADING_DAYS = 365


def _safe_div(a, b, default=0.0):
    return a / b if b else default


@dataclass
class EquityStats:
    n: int
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    calmar: float
    win_rate: float
    best: float
    worst: float

    def as_dict(self):
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def equity_curve(returns, start=1.0):
    """Kümülatif equity eğrisi (bileşik). Boş seri → [start]."""
    curve = [float(start)]
    for r in returns:
        curve.append(curve[-1] * (1.0 + float(r)))
    return curve


def max_drawdown(curve):
    """Tepe-dip maksimum düşüş (pozitif ondalık)."""
    peak, mdd = None, 0.0
    for v in curve:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def benchmark_equity(returns, periods_per_year=TRADING_DAYS, start=1.0) -> EquityStats:
    """Getiri serisi → performans istatistikleri. Tek eleman → stdev 0, Sharpe 0."""
    xs = [float(r) for r in returns]
    n = len(xs)
    if n == 0:
        return EquityStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    curve = equity_curve(xs, start)
    total = curve[-1] / curve[0] - 1.0
    years = n / float(periods_per_year)
    cagr = ((curve[-1] / curve[0]) ** (1.0 / years) - 1.0) if years > 0 and curve[-1] > 0 else 0.0
    mdd = max_drawdown(curve)
    mean = sum(xs) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0
    downside = [min(0.0, x) for x in xs]
    dvar = sum(d * d for d in downside) / n if n else 0.0
    dsd = math.sqrt(dvar)
    ann = math.sqrt(float(periods_per_year))
    sharpe = _safe_div(mean, sd) * ann if sd > 0 else 0.0
    sortino = _safe_div(mean, dsd) * ann if dsd > 0 else 0.0
    calmar = _safe_div(cagr, mdd) if mdd > 0 else 0.0
    wins = sum(1 for x in xs if x > 0)
    return EquityStats(n, total, cagr, mdd, sharpe, sortino, calmar,
                       wins / n, max(xs), min(xs))


@dataclass
class Comparison:
    strategy_total: float
    baseline_total: float
    excess: float
    beats_baseline: bool
    strategy_mdd: float
    baseline_mdd: float
    notes: list = field(default_factory=list)

    def as_dict(self):
        return {"strategy_total": round(self.strategy_total, 6),
                "baseline_total": round(self.baseline_total, 6),
                "excess": round(self.excess, 6),
                "beats_baseline": self.beats_baseline,
                "strategy_mdd": round(self.strategy_mdd, 6),
                "baseline_mdd": round(self.baseline_mdd, 6),
                "notes": list(self.notes)}


def compare_to_baseline(returns, baseline_returns=None) -> Comparison:
    """Strateji vs taban. baseline None → sıfır getiri (nakit) tabanı."""
    xs = [float(r) for r in returns]
    bs = [0.0] * len(xs) if baseline_returns is None else [float(r) for r in baseline_returns]
    s_total = equity_curve(xs)[-1] - 1.0 if xs else 0.0
    b_total = equity_curve(bs)[-1] - 1.0 if bs else 0.0
    s_mdd = max_drawdown(equity_curve(xs)) if xs else 0.0
    b_mdd = max_drawdown(equity_curve(bs)) if bs else 0.0
    notes = []
    if len(xs) != len(bs):
        notes.append(f"length_mismatch strategy={len(xs)} baseline={len(bs)}")
    if not xs:
        notes.append("empty_series")
    return Comparison(s_total, b_total, s_total - b_total, s_total > b_total,
                      s_mdd, b_mdd, notes)


@dataclass
class AccuracyStats:
    n: int
    hit_rate: float
    brier: float
    log_loss: float
    ece: float
    bins: int

    def as_dict(self):
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def brier_score(probs, outcomes):
    """Ortalama kare hata. Boş → 1.0 (en kötü, fail-closed)."""
    pairs = [(float(p), 1.0 if o else 0.0) for p, o in zip(probs, outcomes)]
    if not pairs:
        return 1.0
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(probs, outcomes, eps=1e-12):
    """İkili log-loss; p 0/1'e kırpılır (log(0) koruması)."""
    pairs = [(min(1 - eps, max(eps, float(p))), 1.0 if o else 0.0)
             for p, o in zip(probs, outcomes)]
    if not pairs:
        return 1.0
    return -sum(o * math.log(p) + (1 - o) * math.log(1 - p) for p, o in pairs) / len(pairs)


def calibration_error(probs, outcomes, bins=10):
    """Beklenen kalibrasyon hatası (ECE): kutu |ortalama p − gözlenen oran| ağırlıklı."""
    pairs = [(min(1.0, max(0.0, float(p))), 1.0 if o else 0.0)
             for p, o in zip(probs, outcomes)]
    if not pairs:
        return 0.0
    b = max(1, int(bins))
    total = 0.0
    for i in range(b):
        lo, hi = i / b, (i + 1) / b
        bucket = [(p, o) for p, o in pairs
                  if (lo <= p < hi) or (i == b - 1 and p == 1.0)]
        if not bucket:
            continue
        avg_p = sum(p for p, _ in bucket) / len(bucket)
        avg_o = sum(o for _, o in bucket) / len(bucket)
        total += abs(avg_p - avg_o) * len(bucket) / len(pairs)
    return total


def accuracy_report(probs, outcomes, bins=10) -> AccuracyStats:
    """İsabet oranı + Brier + log-loss + ECE tek pakette."""
    pairs = [(float(p), bool(o)) for p, o in zip(probs, outcomes)]
    n = len(pairs)
    if n == 0:
        return AccuracyStats(0, 0.0, 1.0, 1.0, 0.0, int(bins))
    hits = sum(1 for p, o in pairs if (p >= 0.5) == o)
    return AccuracyStats(
        n, hits / n,
        brier_score([p for p, _ in pairs], [o for _, o in pairs]),
        log_loss([p for p, _ in pairs], [o for _, o in pairs]),
        calibration_error([p for p, _ in pairs], [o for _, o in pairs], bins),
        int(bins),
    )


def skill_score(brier, base_rate):
    """Brier skill: 1 − Brier/Brier_taban. >0 tabandan iyi, ≤0 tabandan kötü."""
    base = float(base_rate) * (1.0 - float(base_rate))
    if base <= 0:
        return 0.0
    return 1.0 - float(brier) / base
