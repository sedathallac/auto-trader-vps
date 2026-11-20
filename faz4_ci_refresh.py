#!/usr/bin/env python3
"""BLOK 4 İŞ 2 — FAZ3 CI'larını GÜN-BLOĞU bootstrap ile bağımlılığa duyarlı yenile.

Eski CI'lar (faz3_stats.block_bootstrap_mean_ci, block=21 ARDIŞIK İŞLEM) işlem
sırası otokorelasyonunu modeller; AYNI GÜNÜN semboller arası ortak şokunu
(gün=tek küme) modele almaz. Burada küme = takvim GÜNÜ (t_entry; bütün
semboollerin aynı gündeki işlemleri TEK küme), B=10000, seed=20261120.

Bellek: her worker kendi sembolünü scan+simulate eder, ANCAK işlem listelerini
döndürür (bar/decision dizileri ana süreçte birikmez — ilk sürüm OOM'da öldü).
null-1 sapması (D-2 etiketi): orijinal faz3_run RNG(7) sıralı-sembol çekilişi
kullanır; burada sembol-bazlı çekiliş — AYNI TARİF, farklı RNG çekilişi
(binlerce işlemde CI'yı istatistiksel olarak kaydırmaz; null-1'in orijinal
n_trades'i JSON'a hiç yazılmadığı için doğrulanamazdı — eksiklik bildirildi).
ÜRETİM-KAPISI: signal GRID[0] ve null-3 n_trades eski JSON'larla birebir
olmalı; değilse exit 1.

VERİ-DURUMU TUZAĞI (kök-neden KAPANDI, BLOK6 İŞ1): orijinal 22:04 koşusundan
SONRA (01:29, parite için) 28 sembole 15m/2026-11.csv eklenmişti; eski
_load_tf pencere süzmediği için Ekim-sonu t_open_next doğuyor ve portföy
56340 oluyordu (yayın 56334; fark concat sırasından bağımsızdı). Artık
SymbolData._load_tf [from, to_excl) süzer (test_faz3_loader_window.py) —
bu betikteki manuel 2026-11 kırpımı (_repub_state) KALDIRILDI; 2026-11
dosyaları dizinde DURURKEN kapılar değişmeden geçmeli.
(Önbellek notu: pkl, eşdeğer filtreli veriyle üretildiği için geçerlidir.)
"""
import datetime as dt
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, "/home/reisfurkan/faz9-faz3")
sys.path.insert(0, "/home/reisfurkan/faz9-faz2")
import faz3_backtest as FB
import faz3_run as FR          # sadece GRID/simulate referansı (veri tutmaz)

AUD = "/home/reisfurkan/faz9-audit/"
ROOT = AUD + "raw/klines"
UNI = AUD + "raw/streams/algo_sembol_evreni.txt"
OLD = AUD + "raw/streams/faz3_results.json"
OLDN = AUD + "raw/streams/faz3_nulls.json"
B, SEED = 10000, 20261120
BEST_GI, NULL_GI = 0, 3


def _work(sym):
    """Tek sembol: scan → 4 üretim → işlem listeleri (küçük) döner.
    BLOK6 İŞ1: pencere süzgeci artık SymbolData._load_tf'de — manuel kırpım yok."""
    try:
        sd = FB.SymbolData(sym, ROOT, "2024-01", "2026-10")
        dec = FB.scan_symbol_decisions(sd, {}, "2024-01", "2026-10")
        slim = [{"sym": t["sym"], "t_entry": t["t_entry"], "t_exit": t["t_exit"],
                 "sl_pct": t["sl_pct"], "net_ret": t["net_ret"]}
                for t in FB.simulate_trades(sd, dec, FB.GRID[BEST_GI], rng_seed=7)]
        rng = random.Random(7 + (sum(sym.encode()) % 10007))     # sembol-bazlı (D-2)
        idx = rng.sample(range(len(dec)), k=min(len(dec), 600))
        fake = []
        for j in idx:
            d = dict(dec[j])
            d["side"] = rng.choice((1, -1))
            d["veto"], d["fams"] = False, 2
            fake.append(d)
        slim1 = [{"sym": t["sym"], "t_entry": t["t_entry"], "t_exit": t["t_exit"],
                 "sl_pct": t["sl_pct"], "net_ret": t["net_ret"]}
                 for t in FB.simulate_trades(sd, fake, FB.GRID[NULL_GI], rng_seed=7)]
        by_i = {}
        for d in dec:
            by_i.setdefault(d["i"], []).append(d)
        rng3 = random.Random(99 + (sum(sym.encode()) % 10007))   # null-3 orijinal yol
        flip = []
        for i, ds in by_i.items():
            d0 = ds[0]
            if d0["veto"] or d0["fams"] < 1:
                continue
            e = dict(d0)
            e["side"] = rng3.choice((1, -1))
            flip.append(e)
        slim3 = [{"sym": t["sym"], "t_entry": t["t_entry"], "t_exit": t["t_exit"],
                 "sl_pct": t["sl_pct"], "net_ret": t["net_ret"]}
                 for t in FB.simulate_trades(sd, flip, FB.GRID[NULL_GI], rng_seed=7)]
        slim3b = [{"sym": t["sym"], "t_entry": t["t_entry"], "t_exit": t["t_exit"],
                 "sl_pct": t["sl_pct"], "net_ret": t["net_ret"]}
                  for t in FB.simulate_trades(sd, flip, FB.GRID[BEST_GI], rng_seed=7)]
        return sym, (slim, slim1, slim3, slim3b), len(dec)
    except Exception as e:
        return sym, {"error": repr(e)}, -1


def day_of(t_ms):
    return dt.datetime.fromtimestamp(t_ms / 1000, dt.UTC).strftime("%Y-%m-%d")


def day_block_ci(trades, b=B, seed=SEED):
    by_day = defaultdict(list)
    for t in trades:
        by_day[day_of(t["t_entry"])].append(t["net_ret"])
    days = sorted(by_day)
    vals = [by_day[d] for d in days]
    n_d, n_tr = len(vals), sum(len(v) for v in vals)
    rng = random.Random(seed)
    means = []
    for _ in range(b):
        acc = cnt = 0
        for _ in range(n_d):
            g = vals[rng.randrange(n_d)]
            acc += sum(g)
            cnt += len(g)
        means.append(acc / cnt)
    means.sort()
    return {"n_trades": n_tr, "n_days": n_d,
            "mean": sum(map(sum, vals)) / n_tr,
            "ci": [means[int(0.025 * b)], means[min(int(0.975 * b), b - 1)]],
            "B": b, "seed": seed}


def main():
    syms = [l.strip().upper() for l in open(UNI) if l.strip()]
    print("tara+üret: %d sembol (2024-01→2026-10), worker-içi bellek" % len(syms),
          flush=True)
    allt = {k: [] for k in ("best", "null1", "null3", "null3b")}
    errs = []
    import os
    import pickle
    cache = AUD + "ci_refresh_percache.pkl"
    per_sym = {}
    if os.path.exists(cache):
        per_sym = pickle.load(open(cache, "rb"))
        print(f"önbellek yüklendi: {len(per_sym)} sembol", flush=True)
    todo = [s for s in syms if s not in per_sym]
    if todo:
        with ProcessPoolExecutor(max_workers=3) as ex:
            for n, (sym, res, ndec) in enumerate(ex.map(_work, todo), 1):
                if isinstance(res, dict):
                    errs.append((sym, res["error"]))
                else:
                    per_sym[sym] = res
                    pickle.dump(per_sym, open(cache + ".tmp", "wb"))
                    os.replace(cache + ".tmp", cache)
                print(f"  [{n}/{len(todo)}] {sym} dec={ndec} "
                      f"best={len(res[0]) if not isinstance(res, dict) else '-'}",
                      flush=True)
    # EŞLEŞME-DÜZENİ: orijinal faz3_run `for s in syms` sembol-sıralı birleştirme
    # yaptı; sorted girdi sırası + paralel tamamlanma aynı SETİ üretir ama
    # sorted()'in kararlılığı farklı kalır (6/56334 uç vakası) — burada eski
    # düzen birebir yeniden kurulur.
    for s in syms:
        if s in per_sym:
            for k, v in zip(("best", "null1", "null3", "null3b"), per_sym[s]):
                allt[k] += v
    if errs:
        print("HATALAR:", errs[:5])
        return 1

    # portfolio süzgeci orijinalle aynı sırada: simgeler arası zaman-sıralı
    closed = {}
    for k in allt:
        closed[k], _ = FB.simulate_portfolio(allt[k])

    old = json.load(open(OLD))
    oldn = json.load(open(OLDN))
    gates = [("signal GRID0", len(closed["best"]), old["configs"][BEST_GI]["n_trades"]),
             ("null3", len(closed["null3"]),
              oldn["null3_coinflip_direction"]["n_trades"])]
    for nm, mine, theirs in gates:
        if mine != theirs:
            print(f"ÜRETİM-KAPISI BAŞARISIZ {nm}: n={mine} != beklenen {theirs}")
            return 1
    print("üretim-kapısı OK:", gates, flush=True)

    out = {}
    for nm in ("best", "null1", "null3", "null3b"):
        out[nm] = day_block_ci(closed[nm])
        print(f"2.1 {nm:7s}: n={out[nm]['n_trades']:6d} gün={out[nm]['n_days']:5d} "
              f"mean={out[nm]['mean']:+.6f} günCI=[{out[nm]['ci'][0]:+.6f},"
              f"{out[nm]['ci'][1]:+.6f}]", flush=True)

    n3 = {(t["sym"], t["t_entry"]): t for t in closed["null3"]}
    diffs, miss = [], 0
    for t in closed["best"]:
        k = (t["sym"], t["t_entry"])
        if k in n3:
            diffs.append({"net_ret": t["net_ret"] - n3[k]["net_ret"], "t_entry": t["t_entry"]})
        else:
            miss += 1
    out["matched_best_minus_null3"] = day_block_ci(diffs)
    out["matched_best_minus_null3"]["eslesmeyen_best"] = miss
    n1 = {(t["sym"], t["t_entry"]) for t in closed["null1"]}
    out["null1_etiket"] = {
        "eslesen_giris": sum(1 for t in closed["best"] if (t["sym"], t["t_entry"]) in n1),
        "n_best": len(closed["best"]),
        "mesaj": "null-1 alt-küme örnekleme + farklı RNG çekilişi (D-2) — "
                 "eşleşme ZORUNLU DEĞİL, eşleşli fark ÜRETİLMEZ"}
    m = out["matched_best_minus_null3"]
    print(f"2.2 eşli best−null3: n={m['n_trades']} (eşleşmeyen {miss}) "
          f"meanΔ={m['mean']:+.6f} günCI=[{m['ci'][0]:+.6f},{m['ci'][1]:+.6f}]",
          flush=True)
    print(f"    null-1 etiket: {out['null1_etiket']}", flush=True)

    old_ci = {"best": old["bootstrap_ci_mean_net_best"],
              "null1": old["bootstrap_ci_mean_net_null1"],
              "null3": oldn["null3_coinflip_direction"]["bootstrap_ci"]}
    print("\n2.3 ESKİ(block=21 işlem) vs YENİ(gün-küme) — %95 CI, mean_net:")
    for k in ("best", "null1", "null3"):
        oc, nc = old_ci.get(k), out[k]["ci"]
        if oc:
            print(f"  {k:6s} eski [{oc[0]:+.5f},{oc[1]:+.5f}]  "
                  f"yeni [{nc[0]:+.5f},{nc[1]:+.5f}]  "
                  f"sıfır: eski={'içerir' if oc[0] <= 0 <= oc[1] else 'içermez'} "
                  f"yeni={'içerir' if nc[0] <= 0 <= nc[1] else 'içermez'}")
    json.dump(out, open(AUD + "faz4_ci_refresh_20261120.json", "w"), indent=1)
    print("yazıldı: faz4_ci_refresh_20261120.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
