"""faz3_null1_recount.py — null-1 işlem sayısını/ortalamasını deterministik
yeniden-üretilen kontrol (6GB makine için AKIŞKAN: kararlar biriktirilmez,
simülasyon worker'da). Tohumlar faz3_run ile birebir aynı → aynı trade kümesi.
"""
import json
import random
import sys
from concurrent.futures import ProcessPoolExecutor

import faz3_backtest as FB
import faz3_run as FR
import faz3_stats as FS


def _sim_null(args):
    sym, fake, root, yf, yt = args
    sd = FB.SymbolData(sym, root, yf, yt)
    return sym, FB.simulate_trades(sd, fake, FB.GRID[3], rng_seed=7)


def main():
    root = "/home/reisfurkan/faz9-audit/raw/klines"
    yf, yt = "2024-01", "2026-10"
    syms = [l.strip().upper() for l in
            open("/home/reisfurkan/faz9-audit/raw/streams/algo_sembol_evreni.txt")
            if l.strip()]
    rng = random.Random(7)
    tasks = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for sym, dec, ebar in ex.map(FR._scan_one,
                                     [(s, root, yf, yt) for s in syms]):
            if not isinstance(dec, list) or not dec:
                print("skip", sym, flush=True)
                continue
            idx = rng.sample(range(len(dec)), k=min(len(dec), 600))
            fake = []
            for j in idx:
                d = dict(dec[j])
                d["side"] = rng.choice((1, -1))
                d["veto"], d["fams"] = False, 2
                fake.append(d)
            del dec
            tasks.append((sym, fake, root, yf, yt))
            print("prepared", sym, len(fake), flush=True)
    nullt = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for sym, trs in ex.map(_sim_null, tasks):
            nullt += trs
    closed, _ = FB.simulate_portfolio(nullt)
    mean = sum(t["net_ret"] for t in closed) / len(closed) if closed else None
    out = {"n_trades": len(closed), "mean_net": mean,
           "bootstrap_ci": FS.block_bootstrap_mean_ci(
               [t["net_ret"] for t in closed])}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
