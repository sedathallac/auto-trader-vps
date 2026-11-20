#!/usr/bin/env python3
"""BLOK8 İŞ1 — KÖTÜMSEN MALİYET KADEMESİNİN ÖLÇÜMÜ (H5 sonucundan ÖNCE).
Salt-okur; yerel ham arşivler: EXCHANGE kayıtları.
  C_commission = Σfee/ΣquoteQty (binance_fills.csv, fill-id dedupe; [EXCHANGE])
  S_exit_p75   = %75 adverse(%) ×1e4 bp — FINISHED STOP_MARKET tetiklemelerinde
                 adverse = (SELL: tp−ap | BUY: ap−tp)/tp  ([EXCHANGE] algo arşivi)
  C_frozen_RT  = 2·C_commission + 2·S_exit_p75   (giriş slipajı = çıkış p75 ASSUMPTION)
ÇIKTI: faz9-audit/faz8_cost_freeze_20261120.{json,txt}. H5 sonucu ÜRETMEZ.
"""
import csv
import datetime as dt
import json

AUD = "/home/reisfurkan/faz9-audit/"
FILLS = AUD + "raw/streams/binance_fills.csv"
ALGO = AUD + "raw/streams/algo_orders_20261119.ndjson"


def p_lin(v, q):
    """type-7 (linear interpolation, R-7/numpy-default) quantile."""
    v = sorted(v)
    k = q * (len(v) - 1)
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (k - f) * (v[c] - v[f])


def p_near(v, q):
    """nearest-rank (ceil) quantile — yöntem-duyarlılık raporu için."""
    import math
    v = sorted(v)
    return v[math.ceil(q * len(v)) - 1]


def main():
    rep = {}
    seen = set()
    sf = sn = 0.0
    tmin = tmax = None
    for r in csv.DictReader(open(FILLS)):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        sf += float(r["commission"])
        sn += float(r["quoteQty"])
        t = r["time_iso"]
        tmin = t if tmin is None or t < tmin else tmin
        tmax = t if tmax is None or t > tmax else tmax
    c_side = sf / sn
    rep["commission"] = {"n_fills_unique": len(seen), "sum_fee_usdt": round(sf, 4),
                         "sum_quote_usdt": round(sn, 2),
                         "C_commission_pct_per_side": round(100 * c_side, 5),
                         "window": [tmin[:10], tmax[:10]],
                         "source": "binance_fills.csv [EXCHANGE userTrades]; "
                                   "commissionAsset=USDT 220/220 "
                                   "(trades_fromid_fix ndjson çapraz-kontrolü)"}
    obs = []
    for line in open(ALGO):
        d = json.loads(line)
        for r in d.get("rows") or []:
            if r.get("algoStatus") == "FINISHED" and r.get("orderType") == "STOP_MARKET":
                tp = float(r.get("triggerPrice") or 0)
                ap = float(r.get("actualPrice") or 0)
                q = float(r.get("actualQty") or 0)
                if tp > 0 and ap > 0 and q > 0:
                    sgn = 1.0 if r["side"] == "BUY" else -1.0
                    adv_bp = sgn * (ap - tp) / tp * 10000.0
                    obs.append({
                        "sym": r["symbol"], "close_side": r["side"],
                        "t": dt.datetime.fromtimestamp(
                            r["triggerTime"] / 1000, dt.timezone.utc).isoformat(),
                        "trigger": tp, "fill_avg": ap, "qty": q,
                        "notional_usdt": round(tp * q, 2),
                        "adverse_bp": round(adv_bp, 2)})
    bps = [o["adverse_bp"] for o in obs]
    rep["exit_slippage"] = {
        "n": len(obs), "obs": sorted(obs, key=lambda o: o["sym"]),
        "mean_bp": round(sum(bps) / len(bps), 3),
        "median_bp": round(p_lin(bps, 0.5), 3),
        "p75_bp_linear_type7": round(p_lin(bps, 0.75), 3),
        "p75_bp_nearest_rank": round(p_near(bps, 0.75), 3),
        "min_bp": min(bps), "max_bp": max(bps),
        "adverse_count": sum(1 for b in bps if b > 0),
        "note": "adverse>0 = kapatış aleyhine; sign-normalizasyon: SELL(long "
                "kapat) → tp−ap, BUY(short kapat) → ap−tp; actualPrice = "
                "borsa-ortalaması fill fiyatı [EXCHANGE]"}
    s75 = p_lin(bps, 0.75) / 10000.0
    c_frozen = 2 * c_side + 2 * s75
    rep["frozen"] = {
        "formula": "C_frozen_RT = 2*C_commission + 2*S_exit_p75",
        "C_commission_per_side_pct": round(100 * c_side, 5),
        "S_exit_p75_pct": round(100 * s75, 4),
        "C_frozen_RT_pct": round(100 * c_frozen, 4),
        "ASSUMPTION": "giriş slipajı ölçülmedi; çıkış p75'ine eşit varsayıldı; "
                      "C_commission = Σfee/Σnotional = fill-başına oran; RT için "
                      "2× (giriş+çıkış fill'i, aynı notional-birimi)",
        "yontem": "P75 = linear-interpolation (type-7); nearest-rank alternatifi "
                  "raporlandı (fark <0.1bp)"}
    json.dump(rep, open(AUD + "faz8_cost_freeze_20261120.json", "w"), indent=1)
    print(json.dumps({k: (v if k != "exit_slippage" else
                          {kk: vv for kk, vv in v.items() if kk != "obs"})
                      for k, v in rep.items()}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
