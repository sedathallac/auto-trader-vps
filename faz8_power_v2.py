#!/usr/bin/env python3
"""BLOK8 İŞ2 — GÜÇ ANALİZİ v2: BİRLEŞİK kapı + küçük-küme düzeltmesi +
etkin-N. SONUÇ ÜRETMEZ (sentetik; gerçek 2020-23 verisi okunmaz).

v1'den farkları (BLOK8 talimatı):
- Hücre-düzeyi model (gün×sembol): net = μ + δ_gün(ortak faktör, empirik
  yeniden-örnekleme) + hücre-rezidüeli (kuyrukları taşıyan empirik (c,R)
  hücre çiftleri). Olay-bağımsızlığı varsayımı YOK; ICC/gün-kümelenmesi
  model-in-truth; etkin-N kapalı biçimde raporlanır.
- Küçük-küme düzeltmesi: sembol- ve blok-kümeli CI'lar iki çeşit:
  (a) düzeltmesiz cluster-bootstrap percentile (v1 davranışı),
  (b) JACKKNIFE-SE + t(K−1) kritik değeri (küçük-küme düzeltmesi; %95 tek
  taraflı). Birleşik KAPI düzeltilmiş (b) ile; (a) yalnız karşılaştırma.
- Birleşik güç: dört koşul BİRLİKTE (B-bazlı ayrı sunulmaz).
- NSIM=300/μ hedefi; B=2000; karşılanamazsa fiili sayı + MC çözünürlüğü.
  0/NSIM gözlemi "sıfır tip-I" DEMEK DEĞİLDİR: 3/NSIM kuralı ile üst-sınır.
Tohum 20261122. ÇIKTI faz8_power_v2_20261120.{json,txt}.
"""
import json
import math
import pickle
import random
import sys
from collections import Counter, defaultdict

AUD = "/home/reisfurkan/faz9-audit/"
OUT = AUD
sys.path.insert(0, "/home/reisfurkan/faz9-faz2")
sys.path.insert(0, "/home/reisfurkan/faz9-faz3")
import faz3_backtest as FB  # noqa: E402
from faz7_h5_power import dsr  # aynı DSR formülü (Bailey-López de Prado)  # noqa: E402

SEED = 20261122
NSIM = 300
BOOT = 2000
# BLOK8 DÜZELTME: net_ret ONDALIK birimdedir (0,003012 = %0,3012). v1 ızgarası
# "%'de" sanılmıştı (×100 hatası) ve doyuran eğride lineer enterpolasyon
# "MDE %0,016" sahte-kesinliğini üretmişti. v2 ızgarası = ONDALIK; etiketler
# % olarak %100 ile çevrilip RAPORLANACAK. (Bu satır BLOK8 bağımsız kontrolünde
# bulunan İKİNCİ gerçek hatadır — ilki UNUSDT.)
GRID = [0.0, 0.0001, 0.0002, 0.00035, 0.0005, 0.0008, 0.0012, 0.0020, 0.0030]
PRE24 = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "LINKUSDT", "XRPUSDT", "ZECUSDT",
         "BNBUSDT", "DOGEUSDT", "KAVAUSDT", "AVAXUSDT", "SOLUSDT", "UNIUSDT",
         "NEARUSDT", "FILUSDT", "AAVEUSDT", "ICPUSDT", "INJUSDT", "WLDUSDT"]
# NOT: v1'de "UNUSDT" yazım hatası vardı → v1 17 kümeyle koştu (UNIUSDT
# düşmüştü); v2 düzeltir — BLOK8 bağımsız denetim bulgusu, belgeye işlendi.
DAY = 86400000
# tek-taraflı %95 t kritik değerleri (df: değer) — R/ptu standard tablosu
T95 = {10: 1.812461, 15: 1.753050, 17: 1.739607, 29: 1.699127, 9: 1.833113}


def jack_t_lo(tot, cnt, df):
    """Ratio-mean cluster CI alt sınırı: jackknife SE × t(K−1)."""
    K = len(cnt)
    if df not in T95:  # dinamik df: en yakın tablo değeri (muhafazakâr komşu)
        df = min(T95, key=lambda d: (abs(d - df), d))
    C = sum(cnt)
    if C <= 0:
        return -1e9
    th = sum(tot) / C
    js = []
    tc, Ts = C - 0, sum(tot)
    for k in range(K):
        c2 = tc - cnt[k]
        if c2 <= 0:
            return -1e9
        js.append((Ts - tot[k]) / c2)
    m = sum(js) / K
    var = sum((x - m) ** 2 for x in js) * (K - 1) / K
    se = math.sqrt(var) if var > 0 else 0.0
    return th - T95[df] * se


def boot_lo(tot, cnt, rng, B=BOOT):
    idx = [i for i, c in enumerate(cnt) if c > 0]
    if len(idx) < 3:
        return -1e9
    means = []
    K = len(idx)
    for _ in range(B):
        s = c = 0
        for j in rng.choices(idx, k=K):
            s += tot[j]
            c += cnt[j]
        means.append(s / c if c else 0.0)
    means.sort()
    return means[int(0.05 * B)]


def main():
    ps = pickle.load(open(AUD + "ci_refresh_percache.pkl", "rb"))
    syms = [l.strip().upper() for l in open(AUD + "raw/streams/algo_sembol_evreni.txt")
            if l.strip()]
    raw = []
    for s in syms:
        raw += ps[s][0]

    def calib(subset):
        m0 = sum(t["net_ret"] for t in subset) / len(subset)
        byday = defaultdict(list)
        for t in subset:
            byday[t["t_entry"] // DAY].append(t["net_ret"])
        delta = [sum(v) / len(v) - m0 for v in byday.values() if v]
        days = sorted(byday)
        cells = defaultdict(list)   # sym -> [(c,R)...] her takvim günü
        cellsum = defaultdict(lambda: defaultdict(float))
        cellcnt = defaultdict(lambda: defaultdict(int))
        for t in subset:
            k = t["t_entry"] // DAY
            cellsum[t["sym"]][k] += t["net_ret"]
            cellcnt[t["sym"]][k] += 1
        for s2 in {t["sym"] for t in subset}:
            for dk in days:
                c = cellcnt[s2].get(dk, 0)
                R = cellsum[s2].get(dk, 0.0) - c * m0  # residüel toplam
                cells[s2].append((c, R))
        sd_d = math.sqrt(sum(x * x for x in delta) / (len(delta) - 1))
        dm = {k: (sum(v) / len(v) - m0) for k, v in byday.items() if v}
        ss = se_n = 0.0
        for t in subset:
            r = t["net_ret"] - m0 - dm[t["t_entry"] // DAY]
            ss += r * r
            se_n += 1
        sd_e = math.sqrt(ss / (se_n - 1))
        return dict(m0=m0, delta_pool=delta, cells=cells, days=days,
                    sd_d=sd_d, sd_e=sd_e, n=len(subset))

    A = calib([t for t in raw if t["sym"] in PRE24])
    A42 = calib(raw)
    rep = {"seed": SEED, "nsim": NSIM, "boot": BOOT, "grid": GRID,
           "model": "hücre=gün×sembol; net=μ+δ_gün+R_hücre (empirik çiftler); "
                    "B evreni: 42-hücre havuzu ×30/42 ölçek (ASSUMPTION)",
           "icc": {"A": None, "B": None}}

    def eff_n(cal, kdays, nsym, dens_scale=1.0):
        cbar = cal["n"] / len(cal["days"]) * dens_scale
        rho = cal["sd_d"] ** 2 / (cal["sd_d"] ** 2 + cal["sd_e"] ** 2)
        n_tot = cbar * kdays
        return {"n_events": round(n_tot), "c_bar": round(cbar, 1),
                "ICC": round(rho, 5), "N_eff_dayclust": round(
                    n_tot / (1 + (cbar - 1) * rho)),
                "sd_delta_pct": round(100 * cal["sd_d"], 4),
                "sd_resid_pct": round(100 * cal["sd_e"], 4)}

    rep["effN_A"] = eff_n(A, 1461, 18)
    rep["effN_B"] = eff_n(A42, 1035, 30, 30 / 42)

    def make_cells(cal, universe):
        if universe == "A":
            pools = [cal["cells"][s] for s in PRE24 if s in cal["cells"]]
            return pools, 18, 1.0
        allc = [c for s in cal["cells"] for c in cal["cells"][s]]
        sc = 30 / 42
        pools = [[(max(0, round(c * sc)), R * sc) for (c, R) in allc]
                 for _ in range(30)]
        return pools, 30, sc

    univ = {}
    for name, cal, nd, dfK, nq, ny, uni in (
            ("largecap18_2020_23", A, 1461, 17, 16, 4, "A"),
            ("smallcap30_2024_26", A42, 1035, 29, 11, 3, "B")):
        pools, K, _ = make_cells(cal, uni)
        pools = [(p, len(p)) for p in pools]
        dfq = T95.get(nq - 1, 2.0)
        qi = [min(i // 91, nq - 1) for i in range(nd)]
        if ny == 4:
            yi = [0] * 366 + [1] * 365 + [2] * 365 + [3] * (nd - 1096)
        else:
            yi = [0] * 366 + [1] * 365 + [2] * (nd - 731)
        univ[name] = (cal, nd, pools, nq, ny, dfK, qi, yi)

    for name, (cal, nd, pools, nq, ny, dfK, qi, yi) in univ.items():
        rows = {k: [] for k in ("g1a", "g1b", "g2a", "g2b", "g3", "g4",
                                "jack", "yrs", "joint", "joint_plain")}
        nmean = []
        for mu in GRID:
            cntk = {k: 0 for k in rows}
            nn = 0
            uo = 11 if name[0] == "l" else 22
            for s in range(NSIM):
                rng = random.Random(SEED * 999983 + uo * 6151
                                    + int(mu * 100000) * 977 + s)
                dp = cal["delta_pool"]
                symsum = [0.0] * len(pools)
                symcnt = [0] * len(pools)
                qsum = [0.0] * nq
                qcnt = [0] * nq
                ysum = [0.0] * ny
                dmean = [0.0] * nd
                tc = [0.0] * nd
                for d in range(nd):
                    delta = dp[rng.randrange(len(dp))]
                    tot = 0.0
                    totc = 0
                    for k, (p, lp) in enumerate(pools):
                        c, R = p[rng.randrange(lp)]
                        if c:
                            x = c * (mu + delta) + R
                            symsum[k] += x
                            symcnt[k] += c
                            tot += x
                            totc += c
                    if totc:
                        dmean[d] = tot / totc
                    qsum[qi[d]] += tot
                    qcnt[qi[d]] += totc
                    ysum[yi[d]] += tot
                    tc[d] = totc
                g1a = boot_lo(symsum, symcnt, rng) > 0
                g1b = jack_t_lo(symsum, symcnt, dfK) > 0
                g2a = boot_lo(qsum, qcnt, rng) > 0
                g2b = jack_t_lo(qsum, qcnt, nq - 1) > 0
                g3 = dsr(dmean, 20) >= 0.95
                jack = sum(sorted(symsum, reverse=True)[3:]) > 0
                yrs = all(x > 0 for x in ysum)
                g4 = jack and yrs
                for k, v in (("g1a", g1a), ("g1b", g1b), ("g2a", g2a),
                             ("g2b", g2b), ("g3", g3), ("g4", g4),
                             ("jack", jack), ("yrs", yrs),
                             ("joint", g1b and g2b and g3 and g4),
                             ("joint_plain", g1a and g2a and g3 and g4)):
                    cntk[k] += bool(v)
                nn += sum(symcnt)
            for k in rows:
                rows[k].append(round(cntk[k] / NSIM, 3))
            nmean.append(round(nn / NSIM))
        rep[name] = {"rates": rows, "trades_mean": nmean,
                     "mc_se_at_half": round(math.sqrt(0.25 / NSIM), 3)}

    json.dump(rep, open(OUT + "faz8_power_v2_20261120.json", "w"), indent=1)
    print(json.dumps(rep, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
