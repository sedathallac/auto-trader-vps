"""faz3_run.py — FAZ3 tam koşu: grid + null-1 + WF/PBO/DSR/bootstrap → JSON.

SONUÇ DOSYASI yalnız bu protokolün ön-kayıtlı metriklerini üretir; hiçbir
sayı sonradan yeniden-tanımlanmaz. Çıktı Git-dışı: audit alanına yazılır.
"""
import argparse
import datetime
import json
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor

import faz3_backtest as FB
import faz3_stats as FS


def _scan_one(args):
    sym, root, ym_from, ym_to = args
    try:
        sd = FB.SymbolData(sym, root, ym_from, ym_to)
        dec = FB.scan_symbol_decisions(sd, {}, ym_from, ym_to)
        return sym, dec, FB.SCAN_STATS.get(sym, {}).get("error_bars", 0)
    except FB.DataIntegrityError:
        raise                       # İŞ4: veri bütünlüğü yutulmaz — koşu durur (exit≠0)
    except Exception as e:
        return sym, {"error": repr(e)}, -1


def _month(ms):
    return datetime.datetime.fromtimestamp(ms / 1000,
                                           datetime.timezone.utc).strftime("%Y-%m")


def wf_folds(all_months, step=3):
    """Expanding WF: train=başlangıç..k, test=sonraki `step` ay."""
    folds = []
    i = 6                                   # min 6 ay IS
    while i + step <= len(all_months):
        folds.append((all_months[:i], all_months[i:i + step]))
        i += step
    return folds


def month_matrix(trades_by_cfg, months):
    """PBO matrisi: cfg × ay → işlemlerin ortalama net_ret (boş ay 0)."""
    mi = {m: j for j, m in enumerate(months)}
    mat = []
    for cfg_key, trs in trades_by_cfg.items():
        acc = [0.0] * len(months)
        cnt = [0] * len(months)
        for t in trs:
            j = mi.get(_month(t["t_exit"]))
            if j is not None:
                acc[j] += t["net_ret"]
                cnt[j] += 1
        mat.append([acc[j] / cnt[j] if cnt[j] else 0.0 for j in range(len(months))])
    return mat


def daily_series(closed, equity_key="equity_after"):
    days = {}
    for t in closed:
        d = datetime.datetime.fromtimestamp(t["t_exit"] / 1000,
                                            datetime.timezone.utc).strftime("%Y-%m-%d")
        days[d] = t[equity_key]
    return [days[d] for d in sorted(days)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/reisfurkan/faz9-audit/raw/klines")
    ap.add_argument("--universe-file",
                    default="/home/reisfurkan/faz9-audit/raw/streams/algo_sembol_evreni.txt")
    ap.add_argument("--out", default="/home/reisfurkan/faz9-audit/raw/streams/faz3_results.json")
    ap.add_argument("--ym-from", default="2024-01")
    ap.add_argument("--ym-to", default="2026-10")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", default=None)
    a = ap.parse_args(argv)
    syms = [l.strip().upper() for l in open(a.universe_file) if l.strip()]
    if a.only:
        keep = {s.strip().upper() for s in a.only.split(",")}
        syms = [s for s in syms if s in keep]
    print("tara: %d sembol" % len(syms), flush=True)
    decisions, scan_err_bars = {}, {}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for sym, dec, ebar in ex.map(_scan_one, [(s, a.root, a.ym_from, a.ym_to)
                                                 for s in syms]):
            decisions[sym] = dec
            scan_err_bars[sym] = ebar
            n = len(dec) if isinstance(dec, list) else dec
            print("  %s → %s (hata-bar:%d)" % (sym, n, ebar), flush=True)
    err = {s: d for s, d in decisions.items() if isinstance(d, dict)}
    if err:
        print("HATALI:", list(err)[:5], err[list(err)[0]] if err else "")

    data = {s: FB.SymbolData(s, a.root, a.ym_from, a.ym_to) for s in syms
            if isinstance(decisions[s], list)}

    results = {"meta": {"universe": len(syms), "window": [a.ym_from, a.ym_to],
                        "prereg_commit": "93d58d7", "grid_n": len(FB.GRID),
                        "errors": list(err),
                        "skipped_error_bars_total": sum(
                            v for v in scan_err_bars.values() if v > 0),
                        "deviations": ["D-1", "D-2", "D-3", "D-4"]}}
    trades_by_cfg, cfg_summ = {}, []
    all_closed_by_cfg = {}
    for gi, cfg in enumerate(FB.GRID):
        allt = []
        for s in syms:
            if not isinstance(decisions[s], list):
                continue
            allt += FB.simulate_trades(data[s], decisions[s], cfg, rng_seed=7)
        closed, daily = FB.simulate_portfolio(allt)
        trades_by_cfg[gi] = closed
        all_closed_by_cfg[gi] = (closed, daily)
        cfg_summ.append({"cfg": cfg, "n_trades": len(closed),
                         "mean_net": round(sum(t["net_ret"] for t in closed) /
                                           len(closed), 6) if closed else None})
        print("cfg %d %s → %d işlem" % (gi, cfg, len(closed)), flush=True)

    # null-1: aynı giriş- sembolleri/bar'ları, RASTGELE yön, aynı geometri/maliyet
    rng = random.Random(7)
    nullt = []
    for s in syms:
        dec = decisions[s]
        if not isinstance(dec, list) or not dec:
            continue
        idx = rng.sample(range(len(dec)), k=min(len(dec), 600))
        fake = []
        for j in idx:
            d = dict(dec[j])
            d["side"] = rng.choice((1, -1))
            d["veto"], d["fams"] = False, 2
            fake.append(d)
        nullt += FB.simulate_trades(data[s], fake, FB.GRID[3], rng_seed=7)
    null_closed, null_daily = FB.simulate_portfolio(nullt)

    months = sorted({m for t in all_closed_by_cfg[0][0] for m in [_month(t["t_exit"])]}
                    | {m for t in null_closed for m in [_month(t["t_exit"])]})
    mat = month_matrix({i: all_closed_by_cfg[i][0]
                        for i in range(len(FB.GRID))} | {-1: null_closed},
                       months)
    pbo = FS.pbo_cscv(mat) if len(mat) > 2 else None

    # DSR en-iyi-başına-göre (grid+null = 19 deneme bütçesi, DEVIATIONS §D-2)
    sr_daily, sk, ku, nobs, best = [], [], [], 0, None
    per_cfg_sr = {}
    for gi in range(len(FB.GRID)):
        eq = daily_series(all_closed_by_cfg[gi][0])
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
        per_cfg_sr[gi] = FS.sharpe_daily(rets)
        if per_cfg_sr[gi] is not None:
            sr_daily.append(per_cfg_sr[gi])
    best = max(per_cfg_sr, key=lambda g: (per_cfg_sr[g] is not None, per_cfg_sr[g]))
    eq = daily_series(all_closed_by_cfg[best][0])
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    import statistics as st
    dsr = FS.deflated_sharpe_ratio(
        per_cfg_sr[best], list(per_cfg_sr.values()), len(rets),
        skew=FS._skew(rets), kurt=FS._kurt(rets)) if len(rets) >= 30 else None
    ci = FS.block_bootstrap_mean_ci([t["net_ret"] for t in all_closed_by_cfg[best][0]])
    ci_null = FS.block_bootstrap_mean_ci([t["net_ret"] for t in null_closed])
    # WF: expanding-months, IS-toplam-net seçimi
    folds = wf_folds(months)
    wf_rows = []
    for tr_m, te_m in folds:
        scores = []
        for gi in range(len(FB.GRID)):
            isc = sum(t["net_ret"] for t in all_closed_by_cfg[gi][0]
                      if _month(t["t_exit"]) in tr_m)
            scores.append((isc, gi))
        _, sel = max(scores)
        oos = [t["net_ret"] for t in all_closed_by_cfg[sel][0]
               if _month(t["t_exit"]) in te_m]
        wf_rows.append({"train_end": tr_m[-1], "test": te_m, "sel_cfg": gi_repr(FB.GRID[sel]),
                        "oos_n": len(oos),
                        "oos_mean": round(sum(oos) / len(oos), 6) if oos else None})
    results.update({
        "configs": cfg_summ,
        "best_by_sharpe": {"gi": best, "cfg": FB.GRID[best],
                           "sharpe_daily_period": per_cfg_sr[best],
                           "n_days": len(rets)},
        "dsr_best": dsr,
        "pbo_all_incl_null": pbo,
        "bootstrap_ci_mean_net_best": ci,
        "null1": {"n_trades": len(null_closed),
                  "mean_net": round(sum(t["net_ret"] for t in null_closed) /
                                    len(null_closed), 6) if null_closed else None},
        "bootstrap_ci_mean_net_null1": ci_null,
        "wf_folds": wf_rows,
    })
    with open(a.out + ".tmp", "w") as fh:
        json.dump(results, fh, indent=1)
    os.replace(a.out + ".tmp", a.out)
    print(json.dumps({k: v for k, v in results.items() if k != "configs"},
                     indent=1, default=str)[:2000])
    return 0


def gi_repr(cfg):
    return "sl%.1f_tp%.1f_fam%d" % (cfg["atr_sl_multiplier"],
                                     cfg["atr_tp_multiplier"],
                                     cfg["confirm_min_families"])


if __name__ == "__main__":
    sys.exit(main())
