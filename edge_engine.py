"""edge_engine.py — FAZ10: ücret/slipaj farkındalıklı edge ölçümü ve terfi kapıları (SAF).

Bu modül emir göndermez, yön üretmez, dosya yazmaz. Yalnızca ölçer ve karar verir:
  * işlem maliyeti (komisyon + slipaj + funding) düşülmüş NET getiri
  * yol-bağımlı (path-dependent) ilk-temas sonucu: SL mi TP mi önce geldi
  * expectancy, t-istatistiği, bootstrap güven aralığı, profit factor
  * Probabilistic / Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
  * purged walk-forward bölme (López de Prado, 2018) — sızıntısız OOS
  * fraksiyonel Kelly (Kelly, 1956; Thorp, 2006) ve bütçe tavanı
  * net reward/risk kapısı, break-even + ATR trailing stop (monoton)
  * aile×rejim hücreleri için BH-FDR + OOS + DSR + persistence terfi kapısı

Tasarım ilkesi: her fonksiyon deterministik ve yan etkisizdir; rastgelelik
gereken yerlerde `seed` zorunludur. Böylece her iddia testle kanıtlanabilir.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Iterable, Sequence

# Varsayılanlar tek yerde; çağıran cfg ile ezer.
EDGE_DEFAULTS = {
    "fee_pct_round_trip": 0.0008,      # Binance futures taker 2×0.04%
    "slippage_pct_round_trip": 0.0004,  # muhafazakâr varsayılan
    "min_net_reward_risk": 1.20,       # net kazanç / net kayıp alt sınırı
    "min_edge_events": 100,            # hücre için minimum çözülmüş olay
    "min_edge_mean_net": 0.0,          # ortalama net getiri > 0
    "edge_bootstrap_n": 2000,
    "edge_bootstrap_alpha": 0.05,
    "edge_fdr_q": 0.10,
    "edge_min_dsr": 0.95,
    "edge_min_persist_share": 0.50,
    "edge_persist_bars": 3,
    "kelly_fraction": 0.25,
    "kelly_cap": 0.25,
    "trail_atr_mult": 1.5,
    "be_trigger_r": 1.0,
}


def _cfg(cfg, key):
    return (cfg or {}).get(key, EDGE_DEFAULTS[key])


# --------------------------------------------------------------------- yuvarlama
def round_step(value: float, step: float, mode: str = "down") -> float:
    """Borsa adımına Decimal ile yuvarlar (float artefaktı yok).

    mode='down' emniyetli taraftır: istenen notional'ı ASLA aşmaz.
    """
    if step is None or step <= 0:
        raise ValueError("step must be > 0")
    if mode not in ("down", "up"):
        raise ValueError("mode must be 'down' or 'up'")
    d, s = Decimal(str(value)), Decimal(str(step))
    rounding = ROUND_DOWN if mode == "down" else ROUND_UP
    q = (d / s).to_integral_value(rounding=rounding)
    return float(q * s)


def amount_for_notional(notional: float, price: float, step: float,
                        min_qty: float = 0.0, min_notional: float = 0.0):
    """Notional → miktar. Döner (amount, ok). ok=False ise emir GÖNDERİLMEMELİ.

    Aşağı yuvarlama min notional'ın altına düşürürse yukarı çekmek yerine
    reddeder: bütçeyi aşan bir emir açmak, emri açmamaktan daha pahalıdır.
    """
    if price <= 0 or step <= 0 or notional <= 0:
        return 0.0, False
    amt = round_step(notional / price, step, "down")
    if amt <= 0:
        return 0.0, False
    if min_qty and amt < min_qty:
        amt = round_step(min_qty, step, "up")
    if amt * price < min_notional - 1e-12:
        return amt, False
    return amt, True


# --------------------------------------------------------------------- maliyet
def net_return_pct(direction: int, entry: float, exit_price: float,
                   fee_pct: float = 0.0008, slippage_pct: float = 0.0004) -> float:
    """Yön düzeltmeli NET getiri (ondalık, 0.01 = %1). Maliyetler giriş+çıkış toplamı."""
    if direction not in (-1, 1) or entry <= 0:
        raise ValueError("direction ±1 ve entry > 0 olmalı")
    gross = direction * (exit_price - entry) / entry
    return gross - abs(fee_pct) - abs(slippage_pct)


def funding_cost_pct(rate_per_interval: float, hours: float,
                     interval_hours: float = 8.0, direction: int = 1) -> float:
    """Pozisyon tutma funding maliyeti (ondalık). Long pozitif oranda öder, short alır."""
    if interval_hours <= 0 or hours < 0:
        raise ValueError("interval_hours > 0 ve hours >= 0 olmalı")
    return direction * rate_per_interval * (hours / interval_hours)


def slippage_from_depth(levels: Sequence[Sequence[float]], notional: float,
                        side: str = "buy") -> float:
    """Emir defterini yürüyerek notional için VWAP slipajını (ondalık) tahmin eder.

    levels: [[price, qty], ...] fiyat-öncelikli. Yetersiz derinlik → 1.0 (fail-closed:
    defter bu emri kaldıramıyor, slipaj cezası maksimum).
    """
    if not levels or notional <= 0:
        return 1.0
    remaining, cost, qty_sum = float(notional), 0.0, 0.0
    for lv in levels:
        try:
            price, qty = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0 or qty <= 0:
            continue
        take = min(remaining, price * qty)
        cost += take
        qty_sum += take / price
        remaining -= take
        if remaining <= 1e-12:
            break
    if remaining > 1e-9 or qty_sum <= 0:
        return 1.0
    vwap = cost / qty_sum
    best = float(levels[0][0])
    if best <= 0:
        return 1.0
    slip = (vwap - best) / best if side == "buy" else (best - vwap) / best
    return max(0.0, slip)


# --------------------------------------------------------------------- yol-bağımlı sonuç
@dataclass
class TouchOutcome:
    outcome: str            # 'sl' | 'tp' | 'time'
    exit_price: float
    gross_ret: float
    net_ret: float
    bars_held: int
    mae: float              # maksimum olumsuz hareket (ondalık, ≤0)
    mfe: float              # maksimum olumlu hareket (ondalık, ≥0)
    ambiguous: bool = False  # aynı mumda SL ve TP → SL kabul edildi (muhafazakâr)


def first_touch_outcome(direction: int, entry: float, highs: Sequence[float],
                        lows: Sequence[float], closes: Sequence[float],
                        sl_pct: float, tp_pct: float | None = None,
                        fee_pct: float = 0.0008,
                        slippage_pct: float = 0.0004) -> TouchOutcome | None:
    """Mum sırasına göre İLK teması bulur; aynı mumda ikisi de varsa SL kazanır.

    Bu, "kapanışa bak" yaklaşımının en büyük hatasını kapatır: SL içeride
    tetiklenmişken ufuk sonunda kârda görünen bir işlem gerçekte zarardır.
    """
    n = min(len(highs), len(lows), len(closes))
    if direction not in (-1, 1) or entry <= 0 or n == 0 or sl_pct <= 0:
        return None
    sl_price = entry * (1 - direction * sl_pct)
    tp_price = None if tp_pct is None else entry * (1 + direction * tp_pct)
    mae = 0.0
    mfe = 0.0
    for i in range(n):
        hi, lo = float(highs[i]), float(lows[i])
        adverse = direction * ((lo if direction > 0 else hi) - entry) / entry
        favourable = direction * ((hi if direction > 0 else lo) - entry) / entry
        mae = min(mae, adverse)
        mfe = max(mfe, favourable)
        hit_sl = lo <= sl_price if direction > 0 else hi >= sl_price
        hit_tp = tp_price is not None and (hi >= tp_price if direction > 0 else lo <= tp_price)
        if hit_sl:
            return TouchOutcome("sl", sl_price,
                                direction * (sl_price - entry) / entry,
                                net_return_pct(direction, entry, sl_price, fee_pct, slippage_pct),
                                i + 1, mae, mfe, ambiguous=bool(hit_tp))
        if hit_tp:
            return TouchOutcome("tp", tp_price,
                                direction * (tp_price - entry) / entry,
                                net_return_pct(direction, entry, tp_price, fee_pct, slippage_pct),
                                i + 1, mae, mfe)
    last = float(closes[n - 1])
    return TouchOutcome("time", last, direction * (last - entry) / entry,
                        net_return_pct(direction, entry, last, fee_pct, slippage_pct),
                        n, mae, mfe)


# --------------------------------------------------------------------- istatistik
@dataclass
class Expectancy:
    n: int
    mean: float
    stdev: float
    stderr: float
    t_stat: float
    p_value: float
    win_rate: float
    payoff: float
    profit_factor: float
    total: float

    @property
    def positive(self) -> bool:
        return self.n > 0 and self.mean > 0 and self.p_value < 0.05


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expectancy(returns: Iterable[float]) -> Expectancy:
    """Net getiri serisi → expectancy ve tek yönlü (H0: ortalama ≤ 0) t-testi."""
    xs = [float(x) for x in returns]
    n = len(xs)
    if n == 0:
        return Expectancy(0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(xs) / n
    sd = statistics.stdev(xs) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 and sd > 0 else 0.0
    t = mean / se if se > 0 else (math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0))
    p = 1.0 - _norm_cdf(t) if math.isfinite(t) else (0.0 if t > 0 else 1.0)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else (math.inf if avg_win > 0 else 0.0)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    return Expectancy(n, mean, sd, se, t, p, win_rate, payoff, pf, sum(xs))


def bootstrap_ci_mean(returns: Sequence[float], n_boot: int = 2000,
                      alpha: float = 0.05, seed: int = 0):
    """Ortalamanın percentile bootstrap güven aralığı (deterministik seed)."""
    xs = [float(x) for x in returns]
    if not xs:
        return (0.0, 0.0)
    if len(xs) == 1:
        return (xs[0], xs[0])
    rng = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(max(1, int(n_boot))):
        means.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * (len(means) - 1))]
    hi = means[int((1 - alpha / 2) * (len(means) - 1))]
    return (lo, hi)


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark_sr: float = 0.0) -> float:
    """PSR: gözlenen Sharpe'ın benchmark'ı aşma olasılığı (Bailey & LdP, 2012)."""
    xs = [float(x) for x in returns]
    n = len(xs)
    if n < 3:
        return 0.0
    sd = statistics.stdev(xs)
    if sd <= 0:
        return 0.0
    sr = (sum(xs) / n) / sd
    m3 = sum((x - sum(xs) / n) ** 3 for x in xs) / n
    m4 = sum((x - sum(xs) / n) ** 4 for x in xs) / n
    skew = m3 / sd ** 3
    kurt = m4 / sd ** 4
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    if denom <= 0:
        return 0.0
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom)
    return _norm_cdf(z)


def deflated_sharpe_ratio(returns: Sequence[float], n_trials: int,
                          benchmark_sr: float = 0.0) -> float:
    """DSR: çoklu denemede şansla bulunan en iyi Sharpe'ı aşma olasılığı.

    n_trials, aynı veride denenen bağımsız konfigürasyon sayısıdır. Deneme sayısı
    arttıkça eşik yükselir; bu yüzden "en iyi hücre" seçimi cezalandırılır.
    """
    xs = [float(x) for x in returns]
    n = len(xs)
    if n < 3 or n_trials < 1:
        return 0.0
    sd = statistics.stdev(xs)
    if sd <= 0:
        return 0.0
    mean = sum(xs) / n
    sr = mean / sd
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    skew = m3 / sd ** 3
    kurt = m4 / sd ** 4
    var_sr = (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2) / (n - 1)
    if var_sr <= 0:
        return 0.0
    nd = statistics.NormalDist()
    gamma = 0.5772156649
    e = math.e
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var_sr) * (
            (1 - gamma) * nd.inv_cdf(1 - 1.0 / n_trials)
            + gamma * nd.inv_cdf(1 - 1.0 / (n_trials * e))
        )
    z = (sr - benchmark_sr - sr0) / math.sqrt(var_sr)
    return _norm_cdf(z)


def benjamini_hochberg(pvals: Sequence[float], q: float = 0.10) -> list[bool]:
    """BH-FDR: giriş sırasını koruyan reddet/etme listesi."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    k_max = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            k_max = rank
    rej = [False] * m
    for rank, i in enumerate(order, 1):
        if rank <= k_max:
            rej[i] = True
    return rej


def purged_walk_forward(n_samples: int, n_folds: int = 4, embargo: int = 0):
    """Sızıntısız walk-forward: test bloğundan hemen önceki `embargo` örnek eğitimden düşer.

    Döner: [(train_idx, test_idx), ...]. Eğitim her zaman testten ÖNCEdir (look-ahead yok).
    """
    if n_samples <= 0 or n_folds <= 0:
        return []
    if n_folds == 1:
        return [(list(range(n_samples)), list(range(n_samples)))]
    fold = n_samples // (n_folds + 1)
    if fold <= 0:
        return []
    out = []
    for k in range(1, n_folds + 1):
        test_start = k * fold
        test_end = n_samples if k == n_folds else (k + 1) * fold
        train_end = max(0, test_start - max(0, int(embargo)))
        if train_end <= 0 or test_end <= test_start:
            continue
        out.append((list(range(0, train_end)), list(range(test_start, test_end))))
    return out


# --------------------------------------------------------------------- boyutlandırma
def fractional_kelly(win_rate: float, payoff: float, fraction: float = 0.25,
                     cap: float = 0.25) -> float:
    """f* = p − (1−p)/b, fraksiyonla çarpılır ve [0, cap] aralığına kırpılır.

    Negatif kenar → 0 (bahis yok). Payoff ∞ (hiç zarar yok) → cap.
    """
    if not (0.0 <= win_rate <= 1.0) or payoff < 0:
        raise ValueError("win_rate ∈ [0,1], payoff ≥ 0 olmalı")
    if payoff == 0:
        return 0.0
    if math.isinf(payoff):
        f = 1.0
    else:
        f = win_rate - (1.0 - win_rate) / payoff
    if f <= 0:
        return 0.0
    return min(f * max(0.0, fraction), max(0.0, cap))


def risk_notional(balance: float, risk_pct: float, sl_pct: float,
                  fee_pct: float = 0.0008, slippage_pct: float = 0.0004,
                  kelly_fraction: float = 0.25, min_notional: float = 5.5,
                  max_notional: float = 25.0, policy: str = "refuse"):
    """Risk bütçesinden notional. Döner (notional|None, reason).

    Maliyetler stop mesafesine eklenir: SL'de kayıp = notional·(sl+ fee+slip).
    `policy='refuse'` iken bütçe borsa minimumunu karşılamıyorsa emir REDDEDİLİR;
    `policy='upsize'` eski (bütçeyi aşabilen) davranıştır.
    """
    if balance <= 0 or sl_pct <= 0 or risk_pct <= 0:
        return None, "invalid_inputs"
    effective_sl = sl_pct + abs(fee_pct) + abs(slippage_pct)
    budget = balance * risk_pct * max(0.0, kelly_fraction)
    raw = budget / effective_sl
    if raw < min_notional:
        if policy == "upsize":
            return float(min_notional), "min_notional_upsize"
        return None, f"below_min_notional ({raw:.2f} < {min_notional:.2f})"
    notional = min(raw, max_notional)
    if notional < min_notional:
        return None, f"below_min_notional ({notional:.2f} < {min_notional:.2f})"
    return float(notional), "approved"


def net_reward_risk(tp_pct: float, sl_pct: float, fee_pct: float = 0.0008,
                    slippage_pct: float = 0.0004):
    """Net R/R: kazançta da kayıpta da maliyet ödenir. Döner (rr, ok, detail)."""
    if sl_pct <= 0 or tp_pct <= 0:
        return 0.0, False, "invalid_geometry"
    cost = abs(fee_pct) + abs(slippage_pct)
    net_win = tp_pct - cost
    net_loss = sl_pct + cost
    if net_loss <= 0:
        return 0.0, False, "invalid_geometry"
    rr = net_win / net_loss
    return rr, net_win > 0, f"net_win={net_win:.5f} net_loss={net_loss:.5f}"


# --------------------------------------------------------------------- çıkış yönetimi
def trailing_stop(direction: int, entry: float, initial_sl: float, current_sl: float,
                  best_price: float, atr_pct: float, be_trigger_r: float = 1.0,
                  trail_mult: float = 1.5) -> float:
    """Break-even + ATR trailing. Dönen stop ASLA gevşemez, fiyatı geçmez.

    long:  new = max(current, entry (R≥tetik), best − mult·ATR)
    short: new = min(current, entry, best + mult·ATR)
    """
    if direction not in (-1, 1) or entry <= 0 or initial_sl <= 0:
        raise ValueError("direction ±1, entry/initial_sl > 0 olmalı")
    risk = abs(entry - initial_sl)
    if risk <= 0:
        return current_sl
    mfe = direction * (best_price - entry)
    candidates = [current_sl]
    if mfe >= be_trigger_r * risk:
        candidates.append(entry)
    if atr_pct and atr_pct > 0:
        candidates.append(best_price - direction * trail_mult * atr_pct * best_price)
    if direction > 0:
        new = max(candidates)
        return min(new, best_price)
    new = min(candidates)
    return max(new, best_price)


# --------------------------------------------------------------------- terfi kapısı
@dataclass
class CellDecision:
    key: str
    allow: bool
    reason: str
    n: int = 0
    mean_net: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    p_value: float = 1.0
    dsr: float = 0.0
    oos_mean: float = 0.0
    persist_share: float = 0.0
    fdr_pass: bool = False


def cell_decision(key: str, net_returns: Sequence[float], persist_bars: Sequence[int] | None = None,
                  oos_returns: Sequence[float] | None = None, n_trials: int = 1,
                  cfg=None) -> CellDecision:
    """Tek hücre (aile×rejim) için terfi kararı. Tüm eşikler cfg'den gelir."""
    xs = [float(x) for x in net_returns]
    n = len(xs)
    min_n = int(_cfg(cfg, "min_edge_events"))
    if n < min_n:
        return CellDecision(key, False, f"n={n} < {min_n}", n=n)
    st = expectancy(xs)
    lo, hi = bootstrap_ci_mean(xs, int(_cfg(cfg, "edge_bootstrap_n")),
                               float(_cfg(cfg, "edge_bootstrap_alpha")))
    dsr = deflated_sharpe_ratio(xs, max(1, int(n_trials)))
    pb = list(persist_bars or [])
    share = (sum(1 for b in pb if b >= int(_cfg(cfg, "edge_persist_bars"))) / len(pb)) if pb else 0.0
    oos = list(oos_returns or [])
    oos_mean = (sum(oos) / len(oos)) if oos else 0.0
    d = CellDecision(key, False, "", n, st.mean, lo, hi, st.p_value, dsr, oos_mean, share)
    if st.mean <= float(_cfg(cfg, "min_edge_mean_net")):
        d.reason = f"mean_net={st.mean:+.5f} <= 0"
        return d
    if lo <= 0:
        d.reason = f"bootstrap_ci_low={lo:+.5f} <= 0"
        return d
    if dsr < float(_cfg(cfg, "edge_min_dsr")):
        d.reason = f"dsr={dsr:.3f} < {_cfg(cfg, 'edge_min_dsr')}"
        return d
    if oos and oos_mean <= 0:
        d.reason = f"oos_mean={oos_mean:+.5f} <= 0"
        return d
    if pb and share < float(_cfg(cfg, "edge_min_persist_share")):
        d.reason = f"persist_share={share:.2f} < {_cfg(cfg, 'edge_min_persist_share')}"
        return d
    d.allow = True
    d.reason = f"mean_net={st.mean:+.5f} ci=[{lo:+.5f},{hi:+.5f}] dsr={dsr:.3f}"
    return d


def promotion_gate(cells: dict, cfg=None) -> dict:
    """Hücre sözlüğü → kararlar. BH-FDR tüm hücreler üzerinde uygulanır.

    cells[key] = {"net_returns": [...], "persist_bars": [...], "oos_returns": [...]}
    """
    keys = list(cells.keys())
    n_trials = max(1, len(keys))
    decisions = {}
    for k in keys:
        c = cells[k]
        decisions[k] = cell_decision(k, c.get("net_returns", []), c.get("persist_bars"),
                                     c.get("oos_returns"), n_trials, cfg)
    passed = benjamini_hochberg([decisions[k].p_value for k in keys],
                                float(_cfg(cfg, "edge_fdr_q")))
    for k, ok in zip(keys, passed):
        decisions[k].fdr_pass = ok
        if decisions[k].allow and not ok:
            decisions[k].allow = False
            decisions[k].reason += " | fdr_fail"
    return decisions


def gate_lookup(decisions: dict, family: str, regime: str):
    """(allow, reason). Hücre yoksa fail-closed: kanıtlanmamış hücreye emir yok."""
    key = f"{family}:{regime}"
    d = decisions.get(key)
    if d is None:
        return False, f"no_edge_cell {key}"
    return d.allow, d.reason


def cells_from_rows(rows: Iterable[dict], cost: float = 0.0012,
                    oos_frac: float = 0.30) -> dict:
    """signal_outcomes satırları → promotion_gate hücreleri.

    Net getiri = fwd_ret − cost (komisyon+slipaj). OOS, entry_ts sırasına göre son
    `oos_frac` dilimdir; böylece terfi kararı zamanda ileriye bakmaz.
    """
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        try:
            fam, reg = str(r["family"]), str(r["regime"])
            fwd = float(r["fwd_ret"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(fwd):
            continue
        grouped.setdefault(f"{fam}:{reg}", []).append({
            "net": fwd - abs(cost),
            "persist": int(r.get("persist_bars") or 0),
            "ts": float(r.get("entry_ts") or 0.0),
        })
    out = {}
    for key, items in grouped.items():
        items.sort(key=lambda x: x["ts"])
        cut = int(len(items) * (1.0 - max(0.0, min(1.0, oos_frac))))
        train, oos = items[:cut], items[cut:]
        out[key] = {
            "net_returns": [x["net"] for x in train],
            "persist_bars": [x["persist"] for x in train],
            "oos_returns": [x["net"] for x in oos],
        }
    return out
