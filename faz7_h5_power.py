#!/usr/bin/env python3
"""BLOK7 İŞ2 — H5 GÜÇ ANALİZİ (MDE). SONUÇ ÜRETMEZ: gerçek 2020-23 verisi
okunmaz, sinyal üretilmez; SADECE sentetik akışlarla B.5 kapısının bileşen
bazlı minimum tespit eşiği (MDE) ölçülür.

Model (TÜMÜ VARSAYIM, kanıt değil):
- Empirik çıpalar (2024-26 YAYIN ham best stream, cache): per-trade σ,
  ağır-kuyruklu gürültü havuzu (net−mean yeniden-örnekleme), gün-bazlı işlem
  sayısı dağılımı, sembol-ağırlıkları, gün-ortalaması serisinin γ3/γ4'ü.
- Ek gün-shock'u: iid çekim günlük σ'yu yayına eşitlemez (0.271 vs 0.181) →
  gün-başına N(0, extra_sd) ofset; extra_sd empirik sapmadan kalibre.
- Evren A (birincil B.2): 18 büyük-cap (2024-öncesi listelenen) × 2020-23
  1461 gün; yoğunluk = 18-altının 2024-26 yoğunluğu (rejim-farkı VARSAYIMI).
- Evren B (B.8 teyit): 30 küçük-cap × 2024-26 1035 gün; yoğunluk = 42-ortalı
  × 30/42 (küçük-cap'ın daha seyreğik olabileceği NOT olarak yazılır).
B.5 kapısı (sentetik üzerinde, kötümsemalık maliyet net-birimde gömülü):
 1) sembol-kümeli bootstrap CI(5%) > 0 (B=400; gerçek kapı B=10000 —
    güç-tahmini yaklaşıklık, belgelenir)
 2) çeyrek-blok bootstrap CI(5%) > 0
 3) DSR(N=20) ≥ 0.95 — Bailey-López de Prado; proxy-A=gün-ORTALAMA-serisi
    (birincil/temkinli), proxy-B=gün-TOPLAM-serisi (duyarlılık)
 4) yıl-net'leri hepsi > 0 VE en-büyük-3-sembol çıkarılmış toplam > 0
Kabul = dördü birden. μ ızgarasında kabul-oranı ≥ %80 → MDE80.
Tohumlar sabit. ÇIKTI: faz7_h5_power_20261120.json + stdout.
"""
import json
import math
import pickle
import random
import sys
from bisect import bisect
from collections import Counter, defaultdict

AUD = "/home/reisfurkan/faz9-audit/"
OUT = AUD  # smoke-test'te OUT değiştirilebilir (girdi AUD'da kalır)
sys.path.insert(0, "/home/reisfurkan/faz9-faz3")
import faz3_backtest as FB  # noqa: E402

SEED = 20261121
NSIM = 40
BOOT = 400
GRID = [0.0, 0.02, 0.06, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.55, 0.80]
PRE24 = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "LINKUSDT", "XRPUSDT", "ZECUSDT",
         "BNBUSDT", "DOGEUSDT", "KAVAUSDT", "AVAXUSDT", "SOLUSDT", "UNUSDT",  # UNI
         "NEARUSDT", "FILUSDT", "AAVEUSDT", "ICPUSDT", "INJUSDT", "WLDUSDT"]
NTRIAL = 20
GAMMA = 0.5772156649
DAY = 86400000


def invnorm(p):
    # Acklam ters normal CDF (|hata|<1.15e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= 0.97575:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def dsr(series, ntrial):
    T = len(series)
    m = sum(series) / T
    v = sum((x - m) ** 2 for x in series) / (T - 1)
    if v <= 0:
        return -1.0
    sd = math.sqrt(v)
    g3 = sum((x - m) ** 3 for x in series) / T / sd ** 3
    g4 = sum((x - m) ** 4 for x in series) / T / sd ** 4
    sr = m / sd
    den = 1 - g3 * sr + (g4 - 1) / 4.0 * sr * sr
    if den <= 0:
        return -1.0
    sqrtT = math.sqrt(T - 1)
    z = invnorm
    # E[max SR] under ntrial iid trials: sqrt(V)·((1−γ)z(1−1/N)+γz(1−1/(Ne)))
    vmax = den / (T - 1)
    sr0 = math.sqrt(vmax) * ((1 - GAMMA) * z(1 - 1.0 / ntrial)
                             + GAMMA * z(1 - 1.0 / (ntrial * math.e)))
    return phi((sr - sr0) * sqrtT / math.sqrt(den))


def boot_lo(vals_s, vals_c, rng, B=BOOT):
    """küme-bootstrap: ağırlıklı ortalamanın %5 alt-ucu (0 kütleli eleme yok;
    seçim ağırlıkları eşit — B.5 küme-örnekleme tarifi)."""
    idx = [i for i, c in enumerate(vals_c) if c > 0]
    if len(idx) < 3:
        return -1.0
    means = []
    ch = rng.choices
    K = len(idx)
    for _ in range(B):
        s = c = 0
        for j in ch(idx, k=K):
            s += vals_s[j]
            c += vals_c[j]
        means.append(s / c if c else 0.0)
    means.sort()
    return means[int(0.05 * B)]


def simulate(cfg, mu, rng):
    nd = cfg["nd"]
    dayc = cfg["dayc"]
    pool = cfg["pool"]
    P = len(pool)
    cumw, symsum, symcnt = cfg["cumw"], [0.0] * len(cfg["cumw"]), [0] * len(cfg["cumw"])
    qi, yi = cfg["qi"], cfg["yi"]
    NQ, NY = cfg["nq"], cfg["ny"]
    qsum, qcnt, ysum = [0.0] * NQ, [0] * NQ, [0.0] * NY
    dmean, dsum = [], []
    extra = cfg["extra_sd"]
    rand, randrange, gauss = rng.random, rng.randrange, rng.gauss
    for d in range(nd):
        c = dayc[d]
        if c <= 0:
            dmean.append(0.0)
            dsum.append(0.0)
            continue
        delta = gauss(0.0, extra) if extra > 0 else 0.0
        acc = 0.0
        for _ in range(c):
            k = bisect(cumw, rand())
            x = mu + delta + pool[randrange(P)]
            symsum[k] += x
            symcnt[k] += 1
            acc += x
        qsum[qi[d]] += acc
        qcnt[qi[d]] += c
        ysum[yi[d]] += acc
        dmean.append(acc / c)
        dsum.append(acc)
    g1 = boot_lo(symsum, symcnt, rng) > 0
    g2 = boot_lo(qsum, qcnt, rng) > 0
    dA = dsr(dmean, NTRIAL)
    dB = dsr(dsum, NTRIAL)
    g3 = dA >= 0.95
    yrs_ok = all(x > 0 for x in ysum)
    srt = sorted(symsum, reverse=True)
    jack = sum(srt[3:]) > 0
    g4 = yrs_ok and jack
    return dict(g1=g1, g2=g2, g3=g3, g3b=dB >= 0.95, g3A=dA, g3B=dB,
                g4=g4, jack=jack, yrs=yrs_ok,
                joint=(g1 and g2 and g3 and g4),
                jointB=(g1 and g2 and g4 and dB >= 0.95),
                n=sum(symcnt))


def mde80(grid, rates):
    for i, r in enumerate(rates):
        if r >= 0.80:
            if i == 0:
                return grid[0]
            lo, hi = rates[i - 1], r
            if hi == lo:
                return grid[i]
            f = (0.80 - lo) / (hi - lo)
            return round(grid[i - 1] + f * (grid[i] - grid[i - 1]), 4)
    return None


def main():
    ps = pickle.load(open(AUD + "ci_refresh_percache.pkl", "rb"))
    syms = [l.strip().upper() for l in open(AUD + "raw/streams/algo_sembol_evreni.txt")
            if l.strip()]
    raw = []
    for s in syms:
        raw += ps[s][0]
    nets = [t["net_ret"] for t in raw]
    m_all = sum(nets) / len(nets)
    pool_all = [x - m_all for x in nets]
    dens_all = len(raw) / (len(syms) * 1035)

    # evren A: 18 büyük-cap
    raw18 = [t for t in raw if t["sym"] in PRE24]
    dens18 = len(raw18) / (18 * 1035)
    pool18 = [t["net_ret"] for t in raw18]
    m18 = sum(pool18) / len(pool18)
    pool18 = [x - m18 for x in pool18]
    sc18 = Counter(t["sym"] for t in raw18)
    order18 = sorted(sc18)
    cw, acc = [], 0.0
    tot = sum(sc18.values())
    for s in order18:
        acc += sc18[s] / tot
        cw.append(acc)
    cw[-1] = 1.0
    d18 = Counter(t["t_entry"] // DAY for t in raw18)
    daylist18 = [d18.get(d, 0) for d in sorted(d18)]
    # gün-shock kalibrasyonu (18-alt, yayın penceresi)
    dm18 = defaultdict(list)
    for t in raw18:
        dm18[t["t_entry"] // DAY].append(t["net_ret"])
    means18 = [sum(v) / len(v) for v in dm18.values() if v]
    mm = sum(means18) / len(means18)
    sdd = math.sqrt(sum((x - mm) ** 2 for x in means18) / (len(means18) - 1))
    iid = (math.sqrt(sum((x - m18) ** 2 for x in pool18) / (len(pool18) - 1))
           / math.sqrt(dens18 * 18))
    extra18 = math.sqrt(max(0.0, sdd ** 2 - iid ** 2))

    d0 = 1577836800  # 2020-01-01
    ndA = 1461
    qiA, yiA = [], []
    for i in range(ndA):
        q = (i // 91)
        qiA.append(min(q, 15))
        y = 2020 + (1 if i >= 366 else 0) + (1 if i >= 731 else 0) + (1 if i >= 1096 else 0)
        yiA.append(y - 2020)
    rA = random.Random(SEED)
    cfgA = dict(nd=ndA,
               dayc=[daylist18[rA.randrange(len(daylist18))] for _ in range(ndA)],
               pool=pool18, cumw=cw, qi=qiA, yi=yiA, nq=16, ny=4,
               extra_sd=extra18)

    # evren B: 30 küçük-cap (VARSAYIM: 42-ortalı yoğunluk × 30/42, pool=42)
    densB = dens_all * 30 / 42
    dall = Counter(t["t_entry"] // DAY for t in raw)
    daylist_all = [dall.get(d, 0) for d in sorted(dall)]
    ndB = 1035
    rB = random.Random(SEED + 7)
    scale = 30 / 42
    qiB, yiB = [], []
    for i in range(ndB):
        qiB.append(min(i // 91, 10))
        y = 2024 + (1 if i >= 366 else 0) + (1 if i >= 731 else 0)
        yiB.append(y - 2024)
    dm42 = defaultdict(list)
    for t in raw:
        dm42[t["t_entry"] // DAY].append(t["net_ret"])
    means42 = [sum(v) / len(v) for v in dm42.values() if v]
    mm42 = sum(means42) / len(means42)
    sdd42 = math.sqrt(sum((x - mm42) ** 2 for x in means42) / (len(means42) - 1))
    iid42 = (math.sqrt(sum((x - m_all) ** 2 for x in pool_all) / (len(pool_all) - 1))
             / math.sqrt(dens_all * 42))
    # gün-shock pazar-geneli: sembol sayısıyla ölçeklenmez → ham kullanılır
    extra42 = math.sqrt(max(0.0, sdd42 ** 2 - iid42 ** 2))
    cwb = [i / 30 for i in range(1, 31)]
    cfgB = dict(nd=ndB,
                dayc=[max(1, round(daylist_all[rB.randrange(len(daylist_all))] * scale))
                      for _ in range(ndB)],
                pool=pool_all, cumw=cwb, qi=qiB, yi=yiB, nq=11, ny=3,
                extra_sd=extra42)

    rep = {"inputs": {
        "raw_n": len(raw), "dens18_sym_day": round(dens18, 3),
        "densB": round(densB, 3), "sd18_pct": round(
            100 * math.sqrt(sum(x * x for x in pool18) / (len(pool18) - 1)), 3),
        "sd42_pct": round(100 * math.sqrt(sum(x * x for x in pool_all)
                                          / (len(pool_all) - 1)), 3),
        "sdd18": round(100 * sdd, 3), "extra18": round(100 * extra18, 3),
        "extra42": round(100 * extra42, 3),
        "seed": SEED, "nsim": NSIM, "boot": BOOT,
        "units": "net_ret YÜZDE birimindedir; μ ızgarası da %/işlem",
        "varsayim": "2020-23 yoğunluk/varyans = 2024-26 altı; küçük-cap = "
                    "42-ortalı; gün-shock iid açığını kalibre eder"},
        "grid_pct": GRID}

    for name, cfg in (("largecap18_2020_23", cfgA), ("smallcap30_2024_26", cfgB)):
        rows = {"g1": [], "g2": [], "g3": [], "g4": [], "jack": [], "yrs": [],
                "joint": [], "jointB": []}
        dsrc = []
        nmean = []
        for mu in GRID:
            cnt = {k: 0 for k in rows}
            a_sum = b_sum = 0.0
            nn = 0
            uo = 1 if name.startswith("large") else 2
            for s in range(NSIM):
                rng = random.Random(SEED * 100003 + uo * 7919
                                    + int(mu * 100000) * 977 + s)
                r = simulate(cfg, mu, rng)
                for k in rows:
                    cnt[k] += bool(r[k])
                a_sum += r["g3A"]
                b_sum += r["g3B"]
                nn += r["n"]
            for k in rows:
                rows[k].append(round(cnt[k] / NSIM, 3))
            dsrc.append([round(a_sum / NSIM, 3), round(b_sum / NSIM, 3)])
            nmean.append(round(nn / NSIM))
        rep[name] = {
            "rates": rows, "dsr_mean_A_B": dsrc, "trades_mean": nmean,
            "MDE80": {k: mde80(GRID, v) for k, v in rows.items()},
            "typeI_mu0_joint": rows["joint"][0],
        }

    json.dump(rep, open(OUT + "faz7_h5_power_20261120.json", "w"), indent=1)
    for name in ("largecap18_2020_23", "smallcap30_2024_26"):
        r = rep[name]
        print("==", name)
        print(" grid(%):", rep["grid_pct"])
        for k in ("g1", "g2", "g3", "g4", "joint", "jointB"):
            print(f"  {k:6s}", r["rates"][k])
        print("  trades/sim:", r["trades_mean"])
        print("  MDE80 (%net/işlem):", {k: (v if v is not None else ">0.80!")
                                        for k, v in r["MDE80"].items()})
        print("  tip-I(mu=0) joint:", r["typeI_mu0_joint"])
    print("done")


if __name__ == "__main__":
    main()
