"""faz3_cost_breakdown.py — BLOK 1 İŞ 3: FAZ 3 maliyet ayrıştırma [FILL].

220 doğrulanmış fill (fromId-keyset yeniden-çekim) üzerinden SEMBOL bazında
gross(realizedPnl)/fee(komisyon, GİDER)/funding(FUNDING_FEE)/slippage/net.

KAYNAK DİSİPLİNİ:
  * [FILL]  = trades_fromid_fix_20261119.ndjson (userTrades_fromid_*)
  * [INC]   = income_fix_20261119.ndjson (income_90d) — ÇAPRAZ doğrulama
              (REALIZED_PNL, COMMISSION, FUNDING_FEE; TRANSFER HARİÇ)
  * [ALGO]  = algo_orders_20261119.ndjson — slippage YALNIZ tetiklenme
              kapanışlarında ölçülebilir (triggerPrice↔actualPrice); giriş
              karar-fiyatı arşivi YOK → giriş slippage'i INSUFFICIENT EVIDENCE.
  Etiketler TOPLAMDA birleştirilmez: [ALGO] shortfall net'e EKLENMEZ.

MUTABAKAT: Σnet ≈ −28.2636 USDT (Mission B doğrulanmış taban; rp −21.11092998
− com 6.46352885 + fund −0.68915844). Tolerans 0.01 USDT — aynı-veriden aynı
aritmetik beklentisi tam eşitlik; sapma olursa YUVARLAMA YOK, neden aranır.

VERDICT KURALI (BLOK 1): işlem-değerine oranla gross ≈ 0 (|g|≤0.05%) →
İNFAZ sorunu (maliyet öldürüyor); gross ≈ −%0.2 (g ≤ −0.15%) → SİNYAL sorunu.
Aralarda kalırsa KARIŞIK denir ve sayılarla söylenir.

Çevrimdışıdır: ağ/anahtar YOK; salt-okur ndjson.
KULLANIM: python3 faz3_cost_breakdown.py [--fills YOL] [--income YOL] [--algo YOL]
"""
import argparse
import json
import sys

import exchange_ledger as EL

BASELINE_NET = -28.2636      # Mission B doğrulanmış taban (USDT)
TOL = 0.01                   # USDT — tam-eşitlik beklenir; 0.01 veri-yenileme payı
INFRAZ_T = -0.0005           # gross_pct ≥ −0.05% → İNFAZ
SINYAL_T = -0.0015           # gross_pct ≤ −0.15% → SİNYAL
ALGO_FIN = "FINISHED"


def load_algo_finished(path):
    out = []
    try:
        fh = open(path)
    except OSError:
        return out, False
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("dataset", "").startswith("algo_hist_"):
                continue
            for r in rec.get("rows") or []:
                if (r.get("algoStatus") == ALGO_FIN and r.get("actualPrice")
                        and r.get("triggerPrice")):
                    out.append(r)
    return out, True


def algo_shortfall_usdt(r):
    """Tetiklenme slippage'i (UYGULAMA KISMASI, USDT): satıcıysa
    (SL SELL) gerçekleşen tetikten düşükse eksidir. Yalnız BRACKET işareti;
    [FILL] toplamına EKLENMEZ."""
    trig, act = float(r["triggerPrice"]), float(r["actualPrice"])
    qty = float(r.get("actualQty") or r.get("quantity") or 0.0)
    sgn = 1.0 if str(r.get("side", "")).upper() == "SELL" else -1.0
    return (act - trig) * qty * sgn


def breakdown(fills, income_rows):
    per = {}
    for raw in fills:
        n = EL.norm_fill(raw)
        s = per.setdefault(n["symbol"], {"n_fills": 0, "notional": 0.0,
                                         "gross": 0.0, "fee": 0.0,
                                         "funding": 0.0})
        s["n_fills"] += 1
        s["notional"] += abs(n["qty"] * n["price"])
        s["gross"] += n["rp"]
        s["fee"] += n["fee"]                     # pozitif = GİDER
    n_fund = 0
    for r in income_rows:
        if r.get("incomeType") == "FUNDING_FEE":
            sym = str(r.get("symbol") or "")
            if sym in per:
                per[sym]["funding"] += float(r["income"])
            n_fund += 1
    for s in per.values():
        s["net"] = s["gross"] - s["fee"] + s["funding"]
    return per, n_fund


def reconcile(fills, income_rows, baseline=BASELINE_NET, tol=TOL):
    """fills↔income çapraz + baseline mutabakatı. Döner dict; 'ok' anahtarı."""
    rp_f = sum(EL._f(f.get("realizedPnl")) for f in fills)
    fee_f = sum(EL._f(f.get("commission")) for f in fills)
    rp_i = com_i = fun_i = 0.0
    for r in income_rows:
        t = r.get("incomeType")
        v = EL._f(r.get("income"))
        if t == "REALIZED_PNL":
            rp_i += v
        elif t == "COMMISSION":
            com_i += v                            # negatif gelir = gider
        elif t == "FUNDING_FEE":
            fun_i += v
    net = rp_f - fee_f + fun_i
    checks = {
        "rp_fills_vs_income": abs(rp_f - rp_i),
        "fee_fills_vs_income": abs(fee_f + com_i),
        "net_vs_baseline": abs(net - baseline),
    }
    return {"rp": rp_f, "fee": fee_f, "funding": fun_i, "net": net,
            "rp_income": rp_i, "com_income": com_i, "checks": checks,
            "ok": all(d <= tol for d in checks.values())}


def verdict(gross_pct):
    if gross_pct >= INFRAZ_T:
        return ("İNFAZ", f"gross={gross_pct*100:+.4f}% ≥ −0.05% → işlem öncesi "
                         f"kenar ~sıfır; maliyet/uygulama öldürüyor")
    if gross_pct <= SINYAL_T:
        return ("SİNYAL", f"gross={gross_pct*100:+.4f}% ≤ −0.15% → komisyon "
                          f"öncesi dahi negatif; sorun işarette")
    return ("KARIŞIK", f"gross={gross_pct*100:+.4f}% iki eşik arasında "
                       f"(−0.15% .. −0.05%) — tek etikete zorlanmaz")


def main(argv=None):
    ap = argparse.ArgumentParser()
    D = "/home/reisfurkan/faz9-audit/raw/streams/"
    ap.add_argument("--fills", default=D + "trades_fromid_fix_20261119.ndjson")
    ap.add_argument("--income", default=D + "income_fix_20261119.ndjson")
    ap.add_argument("--algo", default=D + "algo_orders_20261119.ndjson")
    a = ap.parse_args(argv)

    fills = EL.load_fill_rows(a.fills)
    inc = EL.load_income_rows(a.income)
    per, n_fund = breakdown(fills, inc)
    rec = reconcile(fills, inc)

    tn = sum(s["notional"] for s in per.values())
    print(f"== [FILL] MALİYET AYRIŞTIRMA — {len(fills)} fill, "
          f"{len(per)} sembol, işlem-değeri {tn:.2f} USDT ==")
    print(f"{'SEMBOL':<14}{'fill':>5}{'işlem-değeri':>14}{'gross':>10}"
          f"{'fee':>9}{'fund':>8}{'net':>10}{'gross%':>9}")
    for sym in sorted(per, key=lambda s: per[s]["net"]):
        s = per[sym]
        gp = s["gross"] / s["notional"] * 100 if s["notional"] else 0.0
        print(f"{sym:<14}{s['n_fills']:>5}{s['notional']:>14.2f}"
              f"{s['gross']:>+10.4f}{-s['fee']:>9.4f}{s['funding']:>+8.4f}"
              f"{s['net']:>+10.4f}{gp:>+9.4f}")
    G = sum(s["gross"] for s in per.values())
    F = sum(s["fee"] for s in per.values())
    U = sum(s["funding"] for s in per.values())
    N = sum(s["net"] for s in per.values())
    print(f"{'TOPLAM':<14}{len(fills):>5}{tn:>14.2f}{G:>+10.4f}{-F:>9.4f}"
          f"{U:>+8.4f}{N:>+10.4f}{G/tn*100:>+9.4f}")
    print(f"oranlar: gross={G/tn*100:+.4f}%  fee={-F/tn*100:+.4f}%  "
          f"funding={U/tn*100:+.4f}%  net={N/tn*100:+.4f}%  (işlem-değeri=2×tek-yön)")

    print("\n== MUTABAKAT (tolerans ±0.01 USDT) ==")
    print(f"[FILL]  Σrp={rec['rp']:+.8f}  Σfee={rec['fee']:.8f}  "
          f"Σfund={rec['funding']:+.8f}  Σnet={rec['net']:+.8f}")
    print(f"[INC]   ΣRP={rec['rp_income']:+.8f}  ΣCOM={rec['com_income']:+.8f}  "
          f"(TRANSFER hariç, n_fund={n_fund})")
    for k, d in rec["checks"].items():
        print(f"  {k}: |Δ|={d:.10f} {'OK' if d <= TOL else 'SAPMA'}")
    print(f"BASELINE {BASELINE_NET} → Σnet {rec['net']:+.4f} : "
          + ("MUTABAKAT VAR" if rec["ok"] else "MUTABAKAT YOK — neden aranmalı"))

    algos, ok = load_algo_finished(a.algo)
    print("\n== [ALGO] SLIPPAGE (yalnız tetiklenme kapanışları; nete EKLENMEZ) ==")
    if not ok:
        print("  algo dosyası yok: INSUFFICIENT EVIDENCE")
    elif not algos:
        print("  FINISHED tetiklenme yok: INSUFFICIENT EVIDENCE")
    else:
        sh = [(r["symbol"], algo_shortfall_usdt(r)) for r in algos]
        tot = sum(v for _, v in sh)
        worst = min(sh, key=lambda x: x[1])
        print(f"  n={len(sh)} tetiklenme; Σ uygulama-kısmesi {tot:+.4f} USDT; "
              f"en kötü {worst[0]} {worst[1]:+.4f}")
        for sym, v in sorted(sh, key=lambda x: x[1])[:5]:
            print(f"    {sym:<14}{v:+.4f}")
    print("  giriş slippage'i: KARAR-FİYAT ARŞİVİ YOK → INSUFFICIENT EVIDENCE")

    v, why = verdict(G / tn)
    print(f"\nVERDICT: {v} — {why}")
    print(f"  (komisyon {F/tn*100:.4f}% + funding {abs(U)/tn*100:.4f}% = "
          f"toplam maliyet {(F+abs(U))/tn*100:.4f}%; net {N/tn*100:+.4f}%)")
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
