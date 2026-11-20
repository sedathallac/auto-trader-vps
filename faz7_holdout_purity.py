#!/usr/bin/env python3
"""BLOK7 İŞ1 — HOLDOUT SAFLIĞI: FAZ3 yayın kararları 2024-01-01 öncesi DEĞİL;
2022-23 verisi yalnız lookback/ısıtma. SONUÇ ÜRETMEZ — mevcut yayın setinin
denetimidir.

Katmanlar:
A) TAM KAPSAM (42 sembol × 4 stream, cache'ten): her t_entry/t_exit
   ∈ [2024-01-01T00Z, 2026-11-01T00Z) olmalı (yayın veri durumu Kasım'sız).
B) DOSYA DENETİMİ: 2024-öncesi CSV hangi sembollerde var (yalnız 8 majör
   beklenir; diğerleri için lookback-iddia TRIVIAL-doğru: dosya yok).
C) MAJÖR KARŞILAŞTIRMA (8/8): tam-geçmiş vs 2024'te-kesik tarama —
   1) ikisinde de 2024-öncesi KARAR yok; 2) ortak anahtarlarda sözlükler
   BİREBİR; 3) farklar yalnız pencere başı ısınma kuşağında (≤4h-lookback
   250×4h≈41.7gün + tampon; sınır AMPİRİK hesaplanır ve <2024-04-01
   assert'iyle bağlanır); 4) yayın closed setinde ısınma-kuşağındaki işlem
   sayısı/satırı raporlanır (etki kuantumu).
ÇIKIŞ: faz9-audit/holdout_purity_20261120.json + stdout; ihlal → exit 1.
"""
import datetime as dt
import json
import os
import pickle
import sys

sys.path.insert(0, "/home/reisfurkan/faz9-faz3")
sys.path.insert(0, "/home/reisfurkan/faz9-faz2")
import faz3_backtest as FB

AUD = "/home/reisfurkan/faz9-audit/"
ROOT = AUD + "raw/klines"
UNI = AUD + "raw/streams/algo_sembol_evreni.txt"
C0 = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
C1 = int(dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
BOUND_HARD = int(dt.datetime(2024, 4, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
MAJORS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT",
          "LINKUSDT", "SOLUSDT", "DOGEUSDT")


def tdate(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def main():
    rep = {"assert_fail": []}
    ps = pickle.load(open(AUD + "ci_refresh_percache.pkl", "rb"))
    syms = [l.strip().upper() for l in open(UNI) if l.strip()]

    # A) tam-kapsam pencere asserti (ham stream'ler + portföy closed)
    for k in ("best", "null1", "null3", "null3b"):
        allt = []
        for s in syms:
            allt += ps[s][{"best": 0, "null1": 1, "null3": 2, "null3b": 3}[k]]
        te = [t["t_entry"] for t in allt]
        tx = [t["t_exit"] for t in allt]
        lo, hi = min(te), max(max(te), max(tx))
        ok = lo >= C0 and hi < C1
        rep[f"stream_{k}"] = {"n": len(allt), "min_t_entry": tdate(lo),
                              "max_t": tdate(hi), "window_ok": ok}
        if not ok:
            rep["assert_fail"].append(f"stream {k} pencere dışı: {tdate(lo)}")
    closed, _ = FB.simulate_portfolio(
        [t for s in syms for t in ps[s][0]])
    lo = min(t["t_entry"] for t in closed)
    hi = max(t["t_exit"] for t in closed)
    rep["closed_best"] = {"n": len(closed), "min_t_entry": tdate(lo),
                          "max_t_exit": tdate(hi),
                          "window_ok": lo >= C0 and hi < C1}
    if not (lo >= C0 and hi < C1):
        rep["assert_fail"].append("closed_best pencere dışı")

    # B) dosya denetimi
    pre24 = {}
    for s in syms:
        d = os.path.join(ROOT, s, "15m")
        pre24[s] = sorted(fn[:7] for fn in os.listdir(d)
                          if fn.endswith(".csv") and fn[:7] < "2024-01")
    rep["pre2024_files"] = {s: v for s, v in pre24.items() if v}
    if sorted(k for k, v in pre24.items() if v) != sorted(MAJORS):
        rep["assert_fail"].append("2024-öncesi CSV beklenmeyen sembollerde")

    # C) majör karşılaştırma: tam vs kesik
    worst_boundary = 0
    for sym in MAJORS:
        sd = FB.SymbolData(sym, ROOT, "2024-01", "2026-10")        # tam (alt trim yok)
        full = FB.scan_symbol_decisions(sd, {}, "2024-01", "2026-10")
        sd2 = FB.SymbolData(sym, ROOT, "2024-01", "2026-10")
        sd2.r15 = [r for r in sd2.r15 if r[0] >= C0]
        sd2.r1h = [r for r in sd2.r1h if r[0] >= C0]
        sd2.r4h = [r for r in sd2.r4h if r[0] >= C0]
        sd2.ct1h = [r[6] for r in sd2.r1h]
        sd2.ct4h = [r[6] for r in sd2.r4h]
        tr = FB.scan_symbol_decisions(sd2, {}, "2024-01", "2026-10")
        # "i" bar-indeksi muhasebe alanıdır: kesik taramada tüm indeksler
        # kayar; kıyas KARAR YÜKÜ (veto, fams, atr_pct) üzerinden yapılır.
        pay = lambda d: (d["veto"], d["fams"], d["atr_pct"])  # noqa: E731
        kf = {(d["t"], d["side"]): pay(d) for d in full}
        kt = {(d["t"], d["side"]): pay(d) for d in tr}
        pre = [k for k in kf if k[0] < C0] + [k for k in kt if k[0] < C0]
        only_full, only_tr = set(kf) - set(kt), set(kt) - set(kf)
        valdiff = {k for k in set(kf) & set(kt) if kf[k] != kt[k]}
        bnd = max([k[0] for k in only_full | only_tr | valdiff] or [0])
        worst_boundary = max(worst_boundary, bnd)
        rep[f"cmp_{sym}"] = {
            "dup_keys_full": len(full) - len(kf),
            "dup_keys_trunc": len(tr) - len(kt),
            "pre2024_decisions": len(pre),
            "full_n": len(kf), "trunc_n": len(kt),
            "only_full": len(only_full), "only_trunc": len(only_tr),
            "value_diffs": len(valdiff),
            "warmup_boundary": tdate(bnd) if bnd else None}
        if pre:
            rep["assert_fail"].append(f"{sym}: 2024-öncesi karar var! {pre[:3]}")
        if only_tr:
            rep["assert_fail"].append(f"{sym}: kesik-tarama YENİ karar üretti")
        if bnd >= BOUND_HARD:
            rep["assert_fail"].append(f"{sym}: ısınma sınırı {tdate(bnd)} ≥ 2024-04-01")
    rep["worst_warmup_boundary"] = tdate(worst_boundary)
    infl = sum(1 for t in closed if t["t_entry"] < worst_boundary)
    rep["closed_trades_in_warmup_zone"] = {
        "n": infl, "of": len(closed),
        "pct": round(100.0 * infl / len(closed), 3)}

    bad = rep["assert_fail"]
    json.dump(rep, open(AUD + "holdout_purity_20261120.json", "w"), indent=1)
    for k, v in rep.items():
        if k != "assert_fail":
            print(k, v)
    print("ASSERT_FAIL:", bad if bad else "YOK — SAFLIK DOĞRULANDI")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
