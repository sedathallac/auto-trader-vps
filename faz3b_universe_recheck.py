"""faz3b_universe_recheck.py — BLOK 3 İŞ A: İŞ 3'ÜN YENİDEN KURULUŞU (1m, yargısız).

A.1 TANIM (kodun başında, bağlayıcı):
  r_i  = UNCONDITIONAL RAW RETURN, LONG konvansiyonu:
         r_i = close(B(t1)) / open(B(t0)) − 1 ; 1m bar; B(t)=t'yi içeren bar.
         Pencere = [ilk giriş fill ts, son çıkış fill ts] = turun open_ts→closed_at.
         ÇÖZÜNÜRLÜK 1m (APPROXIMATE — bar içi yuvarlama yalnız ±1 bar ucu).
  s_i  = +1 LONG / −1 SHORT (tek konvansiyon; A.3 sentetik testlerle kilitli)
  strategy_direction_adjusted_return = s_i·r_i
  reverse_cf_i = −s_i·r_i  (aynı pencerede yön tersi)
  N_i  = entry_notional (gerçek fill notionalı)
  recon_i = s_i·N_i·r_i ; Δ_i = realized_gross_i − recon_i
  "SAF SİNYAL" ifadesi A.2 mutabakatı (Σ|Δ| + SOLUSDT açıklaması) kapanmadan
  KULLANILMAZ — bu modül o ifadeyi hiçbir yere yazmaz.

A.2 Δ AYRIŞMASI (turda tek ortalama giriş ē ve çıkış x̄ ile; cebirsel özdeşlik):
  Δ_i = exit_gap_i + entry_gap_i + resid_i
    exit_gap_i  = s·q·(x̄ − c)              [çıkış anı bar-close'undan sapma;
                                             tetiklenmiş SL'ların slipajı buraya girer]
    entry_gap_i = −s·q·(c/o)·(ē − o)        [giriş, pencere bar-açılışından farklı]
    resid_i     = gross − s·q·(x̄ − ē)      [kısmi çıkış / pyramid / realized-yol]
  q = tur kapanış miktarı (entry_qty≈exit_qty). Sınıflandırma: en büyük
  |terim| belirler; n_fills>2 ayrıca "çok-fill" etiketi taşır.

A.4 AYRIŞTIRMA (gün = UTC giriş tarihi; T = gün blokları):
  U_t  = Σ_{i∈t} N_i r_i / Σ_{i∈t} N_i        (günün notional-ağırlıklı evreni)
  SD_t = Σ_{i∈t} s_i N_i                       (günün net-yön notionalları)
  Σ s_i N_i r_i ≡ Σ_t U_t·SD_t + Σ_i N_i s_i (r_i − U_t(i))
  gross = TERM1(zaman-bloklu net-yön × evren) + TERM2(yön/sembol seçimi)
          + TERM3(ΣΔ infaz) — cebirsel toplam, yaklaşık değil.
  Her terim: sembol-kümeli bootstrap %95 CI (B=10000, seed sabit) +
  leave-one-symbol-out (SOLUSDT ayrı satır).

A.5 YÖN PERMÜTASYONU: girişler/boyutlar/pencereler SABİT; işaretler YALNIZ
  AYNI GÜN İÇİNDE yazı-tura permütelenir (gün-başı LONG/SHORT sayısı korunur;
  B=10000, seed 20261120). Δ sabit kalır (infazın işaret-etiketine duyarsız
  olduğu varsayımı — raporlanır). p = (1+#{gross_perm ≤ gross_obs})/(1+B).
  Güç/MDE: MDE = (z_.95+z_.8)·σ_null (tek taraflı α=0.05, güç 0.8, n=60).

A.6 HÜKÜM KURALI — BU BLOK, GERÇEK SAYILAR ÜRETİLMEDEN ÖNCE COMİT EDİLDİ:
  GATE_b: term2 < 0 VE CI₂üst < 0 VE p ≤ 0.05 VE term2(SOL çıplak) < 0
          → (b) YÖN/SEÇİM hatası İSTATİSTİKSEL OLARAK AYIRT EDİLEBİLİR.
  GATE_a: term1 < 0 VE |term1| ≥ maliyet(7.1527) VE CI₂ sıfır içeriyor VE p>0.05
          → (a) EVREN baskın.
  GATE_c: term3 < 0 VE CI₃üst < 0 VE |term3| ≥ maliyet → (c) İNFAZ baskın.
  Hiçbiri → (d) AYRIM YETERSİZ (zorla seçim yok). Öncelik b→a→c; birden
  fazla gate dolarsa en büyük |terim| sahibi hüküm, diğerleri not düşülür.
Eşitlikler 1e-9 ile doğrulanır; doğrulanamazsa exit 1. Çevrimdışı: salt yerel
CSV/NDJSON. Kaynak etiketi: [FILL] + kline[public 1m].
"""
import argparse
import bisect
import glob
import math
import random
import sys

import exchange_ledger as EL
import faz3_round_analysis as RA

KL1 = "/home/reisfurkan/faz9-audit/raw/klines1m"
COST_TOTAL = 7.1527
BOOT_B = 10000
BOOT_SEED = 20261120


def load_bars_1m(sym):
    ts, o, c = [], [], []
    for f in sorted(glob.glob(f"{KL1}/{sym}/1m/part*.csv")):
        for ln in open(f).read().splitlines()[1:]:
            p = ln.split(",")
            if len(p) < 6:
                continue
            ts.append(int(p[0]) / 1000.0)
            o.append(float(p[1]))
            c.append(float(p[4]))
    return ts, o, c


def window_edges(bars, t0, t1):
    """(open0, close1, ret, ok) — 1m bar ucu yuvarlaması (A.1 tanımı)."""
    ts, o, c = bars
    if not ts:
        return 0.0, 0.0, 0.0, False
    i = bisect.bisect_right(ts, t0) - 1
    j = bisect.bisect_right(ts, t1) - 1
    if i < 0 or j < 0 or j >= len(ts):
        return 0.0, 0.0, 0.0, False
    return o[i], c[j], c[j] / o[i] - 1.0, True


def walk_rounds_priced(fills):
    """RA.walk_rounds'a ek: entry/exit miktar ve ortalama fiyat (A.2 için)."""
    by_sym = {}
    for raw in fills:
        n = EL.norm_fill(raw)
        by_sym.setdefault(n["symbol"], []).append(n)
    rounds = []
    EPS = 1e-9
    for sym in sorted(by_sym):
        rows = sorted(by_sym[sym], key=lambda x: (x["ts"], x["id"]))
        pos, cur = 0.0, None

        def close(ts):
            cur["closed_at_epoch"] = ts
            cur["net"] = cur["gross"] - cur["fee"] + cur["funding"]
            cur["rp"] = cur["gross"]          # EL.assign_funding alias
            cur["commission"] = cur["fee"]
            cur["e_avg"] = cur["e_cost"] / cur["e_qty"] if cur["e_qty"] else 0.0
            cur["x_avg"] = cur["x_cost"] / cur["x_qty"] if cur["x_qty"] else 0.0
            rounds.append(cur)

        for n in rows:
            delta = n["qty"] if n["side"] == "BUY" else -n["qty"]
            nt = abs(n["qty"] * n["price"])
            if cur is None:
                if delta == 0:
                    continue
                cur = {"symbol": sym, "open_ts": n["ts"], "gross": 0.0,
                       "fee": n["fee"], "funding": 0.0,
                       "entry_notional": nt, "exit_notional": 0.0,
                       "direction": "LONG" if delta > 0 else "SHORT",
                       "n_fills": 1, "e_qty": n["qty"], "e_cost": nt,
                       "x_qty": 0.0, "x_cost": 0.0}
                pos = delta
                continue
            reducing = pos != 0 and (pos > 0) != (delta > 0)
            pos_new = pos + delta
            cur["n_fills"] += 1
            if reducing and pos_new != 0 and (pos_new > 0) != (pos > 0):
                frac = abs(pos) / abs(delta)
                cur["fee"] += n["fee"] * frac
                cur["gross"] += n["rp"]
                cur["exit_notional"] += nt * frac
                cur["x_qty"] += n["qty"] * frac
                cur["x_cost"] += nt * frac
                close(n["ts"])
                cur = {"symbol": sym, "open_ts": n["ts"], "gross": 0.0,
                       "fee": n["fee"] * (1 - frac), "funding": 0.0,
                       "entry_notional": nt * (1 - frac), "exit_notional": 0.0,
                       "direction": "LONG" if pos_new > 0 else "SHORT",
                       "n_fills": 1, "e_qty": n["qty"] * (1 - frac),
                       "e_cost": nt * (1 - frac), "x_qty": 0.0, "x_cost": 0.0}
                pos = pos_new
                continue
            cur["fee"] += n["fee"]
            cur["gross"] += n["rp"]
            if reducing:
                cur["exit_notional"] += nt
                cur["x_qty"] += n["qty"]
                cur["x_cost"] += nt
            else:
                cur["entry_notional"] += nt
                cur["e_qty"] += n["qty"]
                cur["e_cost"] += nt
            pos = pos_new
            if abs(pos) < EPS:
                pos = 0.0
                close(n["ts"])
                cur = None
    return rounds


def day_of(ts):
    return int(ts // 86400)


def build_round_data(rounds, bars_cache):
    """Tur listesine r/o/c/recon/Δ ve terim ayrışmasını ekler; kapsama sayar."""
    tot_cov = tot_all = 0.0
    for r in rounds:
        r["s"] = 1.0 if r["direction"] == "LONG" else -1.0
        r["day"] = day_of(r["open_ts"])
        bars = bars_cache.setdefault(r["symbol"], load_bars_1m(r["symbol"]))
        o0, c1, ret, ok = window_edges(bars, r["open_ts"], r["closed_at_epoch"])
        r["ok"] = ok
        tot_all += r["entry_notional"]
        if not ok:
            continue
        tot_cov += r["entry_notional"]
        r["r"], r["o0"], r["c1"] = ret, o0, c1
        r["recon"] = r["s"] * r["entry_notional"] * ret
        r["delta"] = r["gross"] - r["recon"]
        q = r["x_qty"] if r["x_qty"] else r["e_qty"]
        r["exit_gap"] = r["s"] * q * (r["x_avg"] - c1)
        r["entry_gap"] = -r["s"] * q * (c1 / o0) * (r["e_avg"] - o0)
        r["resid"] = r["gross"] - r["s"] * q * (r["x_avg"] - r["e_avg"])
        ssum = r["exit_gap"] + r["entry_gap"] + r["resid"]
        if abs(ssum - r["delta"]) > 1e-6 * max(1.0, abs(r["delta"])):
            print(f"HATA: Δ özdeşliği bozuk {r['symbol']} {r['open_ts']}",
                  file=sys.stderr)
            return None, 0.0, 0.0
    return rounds, tot_cov, tot_all


def classify(r):
    terms = {"SL/çıkış slipajı": abs(r["exit_gap"]),
             "giriş slipajı/pencere": abs(r["entry_gap"]),
             "kısmi-çıkış/yol(realized)": abs(r["resid"])}
    k = max(terms, key=terms.get)
    return k + (" · çok-fill" if r["n_fills"] > 2 else "")


def decomp_terms(rs):
    """(term1, term2) — A.4 özdeşliği. rs = ok=True turlar."""
    by_day = {}
    for r in rs:
        by_day.setdefault(r["day"], []).append(r)
    t1 = t2 = 0.0
    for d, grp in by_day.items():
        den = sum(r["entry_notional"] for r in grp)
        U = (sum(r["entry_notional"] * r["r"] for r in grp) / den) if den else 0.0
        SD = sum(r["s"] * r["entry_notional"] for r in grp)
        t1 += U * SD
        for r in grp:
            t2 += r["entry_notional"] * r["s"] * (r["r"] - U)
    return t1, t2


def cluster_bootstrap_terms(rs, syms_map, b=BOOT_B, seed=BOOT_SEED):
    """Sembol-kümeli: her örneklemde turlar yerinde kalır, semboller çekilir."""
    rng = random.Random(seed)
    keys = sorted(syms_map)
    k = len(keys)
    t1s, t2s, t3s = [], [], []
    for _ in range(b):
        pooled = []
        for _j in range(k):
            pooled.extend(syms_map[keys[rng.randrange(k)]])
        t1, t2 = decomp_terms(pooled)
        t1s.append(t1)
        t2s.append(t2)
        t3s.append(sum(r["delta"] for r in pooled))
    def ci(v):
        v = sorted(v)
        return v[int(0.025 * b)], v[min(int(0.975 * b), b - 1)]
    return ci(t1s), ci(t2s), ci(t3s)


def perm_signs_within_day(rs, rng):
    """Gün-içi işaret permütasyonu: gün-başı LONG/SHORT sayısı KORUNUR."""
    s_new = {id(r): r["s"] for r in rs}
    by_day = {}
    for r in rs:
        by_day.setdefault(r["day"], []).append(r)
    for grp in by_day.values():
        signs = [r["s"] for r in grp]
        rng.shuffle(signs)
        for r, sg in zip(grp, signs):
            s_new[id(r)] = sg
    return s_new


def perm_test(rs, gross_recon_obs, b=BOOT_B, seed=BOOT_SEED):
    rng = random.Random(seed)
    null = []
    for _ in range(b):
        s2 = perm_signs_within_day(rs, rng)
        g = sum(s2[id(r)] * r["entry_notional"] * r["r"] for r in rs) \
            + sum(r["delta"] for r in rs)
        null.append(g)
    null.sort()
    p = (1 + sum(1 for g in null if g <= gross_recon_obs)) / (1 + b)
    mu = sum(null) / b
    sd = math.sqrt(sum((g - mu) ** 2 for g in null) / (b - 1))
    return p, mu, sd, null[0], null[-1]


def verdict(term1, term2, term3, ci2, ci3, p, term2_nosol, cost=COST_TOTAL):
    """A.6 kuralları (docstring'de ÖNDEN yazıldı)."""
    b_ok = term2 < 0 and ci2[1] < 0 and p <= 0.05 and term2_nosol < 0
    a_ok = term1 < 0 and abs(term1) >= cost and ci2[0] <= 0 <= ci2[1] and p > 0.05
    c_ok = term3 < 0 and ci3[1] < 0 and abs(term3) >= cost
    hits = [("b", b_ok, abs(term2)), ("a", a_ok, abs(term1)),
            ("c", c_ok, abs(term3))]
    got = [h for h in hits if h[1]]
    if not got:
        return "(d) AYRIM YETERSİZ — gate'lerin hiçbiri dolmadı"
    got.sort(key=lambda x: -x[2])
    lbl = {"b": "(b) YÖN/SEÇİM hatası ayırt edilebilir (CI₂<0, p≤0.05, SOL-çıkık<0)",
           "a": "(a) EVREN baskın (term1≤−maliyet, seçim CI'ı sıfır içeriyor)",
           "c": "(c) İNFAZ baskın (Δ CI'ı <0, |Δ|≥maliyet)"}[got[0][0]]
    other = " ".join(f"gate_{h[0]} de dolu;" for h in got[1:])
    return lbl + ((" " + other) if other else "")


def main(argv=None):
    ap = argparse.ArgumentParser()
    D = "/home/reisfurkan/faz9-audit/raw/streams/"
    ap.add_argument("--fills", default=D + "trades_fromid_fix_20261119.ndjson")
    ap.add_argument("--income", default=D + "income_fix_20261119.ndjson")
    a = ap.parse_args(argv)
    fills = EL.load_fill_rows(a.fills)
    inc = EL.load_income_rows(a.income)
    rounds = walk_rounds_priced(fills)
    ok_chk, ev, my = RA.cross_check(fills, rounds)
    if not ok_chk:
        print(f"HATA: walk cross-check uyuşmadı {ev} {my}"); return 1
    EL.assign_funding(rounds, inc)
    rounds, cov_num, cov_den = build_round_data(rounds, {})
    if rounds is None:
        return 1
    cov = cov_num / cov_den if cov_den else 0.0
    rs = [r for r in rounds if r.get("ok")]

    # A.2 — Δ mutabakatı (SOL açıklanmadan hüküm yok)
    print(f"== A.1/A.2 — 1m pencere ({len(rs)}/{len(rounds)} tur, "
          f"kapsama {cov*100:.1f}% notional, APPROXIMATE ±1bar) ==")
    Dsum = sum(r["delta"] for r in rs)
    absD = sum(abs(r["delta"]) for r in rs)
    print(f"Σrecon = {sum(r['recon'] for r in rs):+.4f} · Σgross = "
          f"{sum(r['gross'] for r in rs):+.4f} · ΣΔ = {Dsum:+.4f} · "
          f"Σ|Δ| = {absD:.4f}")
    big = sorted(rs, key=lambda r: -abs(r["delta"]))[:10]
    print(f"{'|Δ|>0.5 olan turlar (en büyük 10):':<38}")
    print(f"{'sembol':<11}{'yön':<6}{'Δ$':>8}{'exit_gap':>10}{'entry_gap':>11}"
          f"{'resid':>9}{'nf':>3}  sınıf")
    for r in big:
        if abs(r["delta"]) <= 0.5:
            break
        print(f"{r['symbol']:<11}{r['direction']:<6}{r['delta']:>+8.3f}"
              f"{r['exit_gap']:>+10.3f}{r['entry_gap']:>+11.3f}"
              f"{r['resid']:>+9.3f}{r['n_fills']:>3}  {classify(r)}")
    sol = [r for r in rs if r["symbol"] == "SOLUSDT"]
    print(f"SOLUSDT: {len(sol)} tur · Σrecon(s·N·r) = "
          f"{sum(r['recon'] for r in sol):+.4f} · Σgross = "
          f"{sum(r['gross'] for r in sol):+.4f} · ΣΔ = "
          f"{sum(r['delta'] for r in sol):+.4f} "
          f"(exit_gap {sum(r['exit_gap'] for r in sol):+.3f} / "
          f"entry_gap {sum(r['entry_gap'] for r in sol):+.3f} / "
          f"resid {sum(r['resid'] for r in sol):+.3f})")

    # A.4 — terimler + bootstrap CI + LOSO
    syms_map = {}
    for r in rs:
        syms_map.setdefault(r["symbol"], []).append(r)
    t1, t2 = decomp_terms(rs)
    t3 = sum(r["delta"] for r in rs)
    gross_obs = sum(r["gross"] for r in rs)
    ident = abs((t1 + t2 + t3) - gross_obs)
    print(f"\n== A.4 — gross ayrıştırması (gün blokları, özdeşlik Δ={ident:.2e}) ==")
    if ident > 1e-6:
        print("HATA: A.4 özdeşliği bozuk"); return 1
    ci1, ci2, ci3 = cluster_bootstrap_terms(rs, syms_map)
    print(f"TERM1 net-yön×evren = {t1:+.4f}  CI [{ci1[0]:+.3f}, {ci1[1]:+.3f}]")
    print(f"TERM2 yön/sembol    = {t2:+.4f}  CI [{ci2[0]:+.3f}, {ci2[1]:+.3f}]")
    print(f"TERM3 infaz(ΣΔ)     = {t3:+.4f}  CI [{ci3[0]:+.3f}, {ci3[1]:+.3f}]")
    print("leave-one-symbol-out (en etkili 5 sembol, TERM2 bazında):")
    base2 = t2
    loso = []
    for s in sorted(syms_map):
        sub = [r for r in rs if r["symbol"] != s]
        a1, a2 = decomp_terms(sub)
        loso.append((abs(a2 - base2), s, a1, a2,
                     sum(r["delta"] for r in sub)))
    loso.sort(reverse=True)
    for d, s, a1, a2, a3 in loso[:5]:
        print(f"  −{s:<11} term1={a1:+.3f} term2={a2:+.3f} "
              f"(Δter2 {a2-base2:+.3f}) term3={a3:+.3f}")
    sub_nosol = [r for r in rs if r["symbol"] != "SOLUSDT"]
    _, t2_nosol = decomp_terms(sub_nosol)
    print(f"TERM2 (SOLUSDT çıkık) = {t2_nosol:+.4f}")

    # A.5 — permütasyon
    gross_recon_obs = sum(r["recon"] for r in rs) + t3
    p, mu, sd, lo_n, hi_n = perm_test(rs, gross_recon_obs)
    mde = (1.645 + 0.842) * sd
    tot_n = sum(r["entry_notional"] for r in rs)
    print(f"\n== A.5 — yön permütasyonu (gün-içi, B={BOOT_B}, seed {BOOT_SEED}) ==")
    print(f"gözlenen gross {gross_obs:+.4f} · null ortalama {mu:+.4f} "
          f"[{lo_n:+.2f}, {hi_n:+.2f}] · σ={sd:.3f} · p(tek taraflı)={p:.4f}")
    print(f"MDE(α=0.05, güç=0.8, n=60) = {mde:.3f} USDT "
          f"({mde/tot_n*100:.4f}% toplam-notional / {mde/60:.3f} USDT tur-başı)")

    # A.6 — hüküm
    print(f"\nVERDICT: {verdict(t1, t2, t3, ci2, ci3, p, t2_nosol)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
