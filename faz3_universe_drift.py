"""faz3_universe_drift.py — BLOK 2 İŞ 3 (M-4): EVREN mi, YÖN mü?

Soru: −21.11 USDT gross, sinyalin YÖNÜNDEN mi, işlenen sembollerin AYNI
tutma pencerelerindeki KOŞULSUZ driftinden mi geliyor?

Yöntem (aynı GERÇEK pencereler, t=0'dan t=son'a):
  her tur için ham drift = close(exit-bar)/open(entry-bar) − 1   [15m bar,
  APPROXIMATE: giriş/çıkış anı bar içine yuvarlanır; çözünürlük 15m]
  yön-uyumlu: SHORT'ta işaret çevrilir (long/short TEK konvansiyon).
  drift_usdt    = yön-uyumlu ham drift × entry_notional
  saf sinyal    = Σgross − Σdrift_usdt
  ters-karşıolgusal = −gross (aynı pencerede yönü çevir; gross-only —
  slippage düzeltmesi ÖLÇÜLEMİYOR, FAZ 4 eksikliği burada da geçerli).

HÜKÜM KURALI (sayılar görülmeden ÖNCE yazıldı):
  e = 1.0 USDT + 0.10·|Σdrift| (mutlak+nispi tolerans; ~sermayenin ‰29'u)
  (a) EVREN baskın:   |saf sinyal| ≤ e ve Σdrift < 0 ve |Σdrift| ≥ maliyet(7.15)
  (b) YÖN zararı:     saf sinyal < −e
  (c) MALİYET/İNFAZ:  saf sinyal > +e ve net < 0
  (d) YETERSİZ:       notional-ağırlıklı bar kapsaması < %90 — (a)/(b)/(c) seçilmez.
Evren etkisi YALNIZ ham driftle; yön etkisi YALNIZ net ile açıklanmaz (M-4).
Çevrimdışı-değil: ham veri fetch_nov_klines.py'den (imzasız public); bu script
yalnız yerel CSV okur. Kline dizini: ../faz9-audit/raw/klines/<SYM>/15m/2026-11.csv
"""
import argparse
import bisect
import glob
import sys

import exchange_ledger as EL
import faz3_round_analysis as RA

COST_TOTAL = 7.1527   # BLOK1: fee 6.4635 + funding −0.6892 → maliyet bileşeni
KLINES = "/home/reisfurkan/faz9-audit/raw/klines"


def load_bars(sym):
    """(open_times, opens, closes) — 2026-11 15m dosyaları birleşik."""
    ts, o, c = [], [], []
    for f in sorted(glob.glob(f"{KLINES}/{sym}/15m/2026-1[12].csv")):
        for ln in open(f).read().splitlines()[1:]:
            p = ln.split(",")
            if len(p) < 6:
                continue
            ts.append(int(p[0]) / 1000.0)
            o.append(float(p[1]))
            c.append(float(p[4]))
    return ts, o, c


def window_return(bars, t0, t1):
    """(raw_ret, ok) — t0'yu içeren barın OPEN'ı → t1'i içeren barın CLOSE'u."""
    ts, o, c = bars
    if not ts:
        return 0.0, False
    i = bisect.bisect_right(ts, t0) - 1
    j = bisect.bisect_right(ts, t1) - 1
    if i < 0 or j < 0 or j >= len(ts):
        return 0.0, False
    return c[j] / o[i] - 1.0, True


def main(argv=None):
    ap = argparse.ArgumentParser()
    D = "/home/reisfurkan/faz9-audit/raw/streams/"
    ap.add_argument("--fills", default=D + "trades_fromid_fix_20261119.ndjson")
    ap.add_argument("--income", default=D + "income_fix_20261119.ndjson")
    a = ap.parse_args(argv)
    fills = EL.load_fill_rows(a.fills)
    inc = EL.load_income_rows(a.income)
    rounds, _ = RA.walk_rounds(fills)
    EL.assign_funding(rounds, inc)
    cache = {}
    per = {}
    cov_num = cov_den = 0.0
    for r in rounds:
        sym = r["symbol"]
        if sym not in cache:
            cache[sym] = load_bars(sym)
        raw, ok = window_return(cache[sym], r["open_ts"], r["closed_at_epoch"])
        sign = 1.0 if r["direction"] == "LONG" else -1.0
        d_adj = raw * sign
        d_usdt = d_adj * r["entry_notional"]
        p = per.setdefault(sym, dict(n=0, lng=0, sh=0, hold=0.0,
                                     drift_usdt=0.0, gross=0.0, fee=0.0,
                                     fund=0.0, en=0.0, miss=0))
        p["n"] += 1
        p["lng" if r["direction"] == "LONG" else "sh"] += 1
        p["hold"] += r["closed_at_epoch"] - r["open_ts"]
        p["en"] += r["entry_notional"]
        p["gross"] += r["gross"]
        p["fee"] += r["fee"]
        p["fund"] += r["funding"]
        cov_den += r["entry_notional"]
        if ok:
            cov_num += r["entry_notional"]
            p["drift_usdt"] += d_usdt
        else:
            p["miss"] += 1
            print(f"UYARI: {sym} {r['open_ts']} pencere barsız — tur kapsama dışı",
                  file=sys.stderr)
    cov = cov_num / cov_den if cov_den else 0.0
    tot = {k: sum(p[k] for p in per.values())
           for k in ("drift_usdt", "gross", "fee", "fund", "en", "n")}
    net_tot = tot["gross"] - tot["fee"] + tot["fund"]
    print(f"== M-4 EVREN/YÖN — {len(per)} sembol, {tot['n']} tur, "
          f"kapsama {cov*100:.1f}% (notional-ağırlıklı, APPROXIMATE 15m) ==")
    print(f"{'SEMBOL':<12}{'tur':>4}{'L/S':>6}{'tutma':>8}{'giriş-not':>10}"
          f"{'drift$':>9}{'gross$':>9}{'net$':>9}{'sinyal$':>9}{'drift%':>9}{'gros%':>9}")
    for s in sorted(per, key=lambda x: per[x]["gross"] - per[x]["drift_usdt"]):
        p = per[s]
        sig = p["gross"] - p["drift_usdt"]
        net = p["gross"] - p["fee"] + p["fund"]
        print(f"{s:<12}{p['n']:>4}{str(p['lng'])+'/'+str(p['sh']):>6}"
              f"{p['hold']/3600:>8.1f}{p['en']:>10.1f}"
              f"{p['drift_usdt']:>+9.3f}{p['gross']:>+9.3f}{net:>+9.3f}"
              f"{sig:>+9.3f}"
              f"{(p['drift_usdt']/p['en']*100 if p['en'] else 0):>+9.3f}"
              f"{(p['gross']/p['en']*100 if p['en'] else 0):>+9.3f}")
    D_, G_ = tot["drift_usdt"], tot["gross"]
    pure = G_ - D_
    e = 1.0 + 0.10 * abs(D_)
    print(f"\nΣdrift(koşulsuz,yön-uyumlu) = {D_:+.4f} USDT · "
          f"Σgross = {G_:+.4f} · SAF SİNYAL = {pure:+.4f} USDT "
          f"(e={e:.2f}) · Σnet = {net_tot:+.4f}")
    print(f"ters-karşıolgusal gross-only = {-G_:+.4f} USDT "
          f"(slippage düzeltmesi ÖLÇÜLEMİYOR — FAZ4)")
    print(f"VERDICT: {verdict(D_, G_, net_tot, cov)}")
    return 0


def verdict(drift_usdt, gross_usdt, net_usdt, coverage, cost_total=COST_TOTAL):
    """(a)/(b)/(c)/(d) — kural sayılardan önce docstring'de yazıldı."""
    pure = gross_usdt - drift_usdt
    e = 1.0 + 0.10 * abs(drift_usdt)
    if coverage < 0.90:
        return "(d) AYRIM İÇİN YETERSİZ VERİ"
    if abs(pure) <= e and drift_usdt < 0 and abs(drift_usdt) >= cost_total:
        return "(a) EVREN DRIFT'i baskın — FAZ5 koşulmaz"
    if pure < -e:
        return "(b) SİNYAL/YÖN katkısı negatif — FAZ5 geçerli aday"
    if pure > e and net_usdt < 0:
        return "(c) SİNYAL gross'u benchmark'tan iyi; maliyet/infaz baskın"
    return "(d) hükmü kesin ayırmaya yetmiyor — sayılar yukarıda"


if __name__ == "__main__":
    sys.exit(main())
