"""faz3_nulls.py — ön-kayıttaki betimleyici null-2 / null-3 (KABUL-KAPISI DEĞİL).

null-2: majörlerde buy&hold (2024-01→2026-10), tek seferlik komisyon+slipaj.
null-3: en-iyi-config'in GİRİŞLERİ aynı, YÖN yazı-tura — aynı geometri/maliyet.
Çıktı: JSON (audit alanı). Etiketsiz sayı yok.
"""
import argparse
import datetime
import json
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor

import faz3_backtest as FB
import faz3_run as FR
import faz3_stats as FS

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
          "ADAUSDT", "DOGEUSDT", "LINKUSDT"]


def _null3_scan(args):
    sym, root, ym_from, ym_to = args
    try:
        sd = FB.SymbolData(sym, root, ym_from, ym_to)
        dec = FB.scan_symbol_decisions(sd, {}, ym_from, ym_to)
        by_i = {}
        for d in dec:
            by_i.setdefault(d["i"], []).append(d)
        rng = random.Random(99 + (sum(sym.encode()) % 10007))
        flip = []
        for i, ds in by_i.items():
            d0 = ds[0]
            if d0["veto"] or d0["fams"] < 1:
                continue                      # aynı kapı: geçmeyen bar atlanır
            e = dict(d0)
            e["side"] = rng.choice((1, -1))   # tek fark: yön yazı-tura
            flip.append(e)
        return sym, FB.simulate_trades(sd, flip, FB.GRID[3], rng_seed=7)
    except Exception as ex:
        return sym, {"error": repr(ex)}


def buyhold(root, ym_from, ym_to):
    out = {}
    for s in MAJORS:
        sd = FB.SymbolData(s, root, ym_from, ym_to)
        rows = [r for r in sd.r1h
                if ym_from <= _ym(r[0]) <= ym_to]
        if len(rows) < 200:
            continue
        eq = [rows[0][1]]
        for r in rows[1:]:
            eq.append(r[4])
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
        # günlük: 24 saatlik blok kapanışları
        daily = [eq[i] / eq[i - 24] - 1 for i in range(24, len(eq), 24)]
        out[s] = {"total_ret": round(eq[-1] / eq[0] - 1, 4),
                  "sharpe_daily_annual": FS.sharpe_daily(daily),
                  "cost_one_way_pct": FB.FEE_ROUND_TRIP / 2}
    return out


def _ym(ms):
    return datetime.datetime.fromtimestamp(ms / 1000,
                                           datetime.timezone.utc).strftime("%Y-%m")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/reisfurkan/faz9-audit/raw/klines")
    ap.add_argument("--universe-file",
                    default="/home/reisfurkan/faz9-audit/raw/streams/algo_sembol_evreni.txt")
    ap.add_argument("--out", default="/home/reisfurkan/faz9-audit/raw/streams/faz3_nulls.json")
    ap.add_argument("--ym-from", default="2024-01")
    ap.add_argument("--ym-to", default="2026-10")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    res = {"null2_buyhold_majors": buyhold(a.root, a.ym_from, a.ym_to)}
    syms = [l.strip().upper() for l in open(a.universe_file) if l.strip()]
    n3 = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for sym, trs in ex.map(_null3_scan, [(s, a.root, a.ym_from, a.ym_to)
                                             for s in syms]):
            if isinstance(trs, dict):
                print("ERR", sym, trs, flush=True)
                continue
            n3 += trs
            print("null3 %s → %d" % (sym, len(trs)), flush=True)
    closed3, _ = FB.simulate_portfolio(n3)
    mean3 = sum(t["net_ret"] for t in closed3) / len(closed3) if closed3 else None
    res["null3_coinflip_direction"] = {
        "cfg": FB.GRID[3], "n_trades": len(closed3),
        "mean_net": mean3,
        "bootstrap_ci": FS.block_bootstrap_mean_ci(
            [t["net_ret"] for t in closed3])}
    with open(a.out + ".tmp", "w") as fh:
        json.dump(res, fh, indent=1)
    os.replace(a.out + ".tmp", a.out)
    print(json.dumps(res, indent=1)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
