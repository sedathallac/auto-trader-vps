"""FAZ13-C: signal_outcomes üzerinde TERS YÖN backtest'i (salt-okunur, canlıya dokunmaz).

Her (sinyal, rejim) hücresi için:
  * n, kazanan, kazanma oranı + Wilson %95 GA
  * ters yön kazanma oranı (= 1 - oran) + Wilson GA
  * ortalama fwd_ret (yön işaretli) ve ters yön (-fwd_ret), net maliyet sonrası
  * R-katları: gerçekleşen getiri / kayıtlı sl_pct; ters yön = -R
  * tek örneklem t-testi (H0: ortalama R = 0) + bootstrap %95 GA
  * başabaş kazanma oranı (net_rr=1.45 → 0.408) üzerinden binom z-testi
"""
import math
import random
import sqlite3
import statistics
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/so_compare.db"
COST = 0.0008          # fee_pct_round_trip (auto_trader.RISK_CONFIG)
NET_RR = 1.45          # canlı logdaki tipik net_rr
BREAKEVEN = 1.0 / (1.0 + NET_RR)
MIN_N = 30             # rapor için alt sınır
BOOT = 4000


def wilson(k, n, z=1.959963985):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def boot_mean_ci(xs, iters=BOOT, seed=7):
    if len(xs) < 5:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(iters):
        means.append(sum(xs[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(0.025 * iters)], means[int(0.975 * iters) - 1])


def t_stat(xs):
    if len(xs) < 3:
        return (float("nan"), float("nan"))
    sd = statistics.stdev(xs)
    if sd == 0:
        return (float("nan"), float("nan"))
    t = statistics.mean(xs) / (sd / math.sqrt(len(xs)))
    # iki yönlü p, normal yaklaşım (n büyük)
    p = math.erfc(abs(t) / math.sqrt(2))
    return (t, p)


def binom_z(k, n, p0):
    if n == 0:
        return (float("nan"), float("nan"))
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0:
        return (float("nan"), float("nan"))
    z = (k / n - p0) / se
    return (z, math.erfc(abs(z) / math.sqrt(2)))


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT signal, regime, direction, real, fwd_ret, sl_pct FROM signal_outcomes "
        "WHERE resolved=1 AND real IS NOT NULL AND sl_pct IS NOT NULL AND fwd_ret IS NOT NULL"
    ).fetchall()
    cells = {}
    for sig, reg, direction, real, fwd, sl in rows:
        cells.setdefault((sig, reg), []).append((int(real), float(fwd), float(sl)))

    print(f"DB={DB}  skorlanabilir satır={len(rows)}  hücre={len(cells)}  "
          f"maliyet={COST:.4%}  net_rr={NET_RR}  başabaş={BREAKEVEN:.3f}\n")
    hdr = (f"{'sinyal/rejim':32s} {'n':>4s} {'kaz':>4s} {'oran':>6s} {'Wilson95':>15s} "
           f"{'ters_oran':>9s} {'ters_Wilson':>15s} {'ort_fwd':>9s} {'ters_net':>9s} "
           f"{'ortR':>7s} {'tersR':>7s} {'tersR_boot95':>18s} {'t':>6s} {'p':>7s} {'z_vs_be':>8s}")
    print(hdr)
    print("-" * len(hdr))

    out = []
    for (sig, reg), rs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        if n < MIN_N:
            continue
        wins = sum(r[0] for r in rs)
        rate = wins / n
        lo, hi = wilson(wins, n)
        rlo, rhi = 1 - hi, 1 - lo
        fwd = [r[1] for r in rs]
        mean_fwd = statistics.mean(fwd)
        rev_net = -mean_fwd - COST
        r_mult = [r[1] / r[2] for r in rs]
        rev_r = [-x for x in r_mult]
        mean_r = statistics.mean(r_mult)
        mean_rev_r = statistics.mean(rev_r)
        blo, bhi = boot_mean_ci(rev_r)
        t, p = t_stat(rev_r)
        z, pz = binom_z(n - wins, n, BREAKEVEN)
        print(f"{sig + '/' + reg:32s} {n:4d} {wins:4d} {rate:6.3f} "
              f"[{lo:.3f},{hi:.3f}] {1 - rate:9.3f} [{rlo:.3f},{rhi:.3f}] "
              f"{mean_fwd:+9.4%} {rev_net:+9.4%} {mean_r:+7.3f} {mean_rev_r:+7.3f} "
              f"[{blo:+.3f},{bhi:+.3f}] {t:6.2f} {p:7.4f} {z:8.2f}")
        out.append({"cell": f"{sig}/{reg}", "n": n, "wins": wins, "rate": rate,
                    "wilson": (lo, hi), "rev_rate": 1 - rate, "rev_wilson": (rlo, rhi),
                    "mean_fwd": mean_fwd, "rev_net": rev_net, "mean_r": mean_r,
                    "mean_rev_r": mean_rev_r, "rev_boot": (blo, bhi), "t": t, "p": p,
                    "z_be": z, "p_be": pz})

    print("\n=== ÖZET: ters yön anlamlı mı? (p<0.05 ve GA 0'ı dışlıyor) ===")
    for o in out:
        sig = (o["p"] < 0.05) and (o["rev_boot"][0] > 0 or o["rev_boot"][1] < 0)
        print(f"  {o['cell']:32s} tersR={o['mean_rev_r']:+.3f} "
              f"GA=[{o['rev_boot'][0]:+.3f},{o['rev_boot'][1]:+.3f}] p={o['p']:.4f} "
              f"→ {'ANLAMLI' if sig else 'anlamlı DEĞİL'}")
    return out


if __name__ == "__main__":
    main()
