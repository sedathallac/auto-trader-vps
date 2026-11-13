#!/usr/bin/env python3
"""Faz 9 final analiz: canlı compare verisi → "Kenar Var / Kenar Yok / Veri Yetersiz" kararı.

SALT-OKUNUR: whale_metrics.db ve signal_outcomes.db `mode=ro` açılır; hiçbir DB/dosya yazılmaz (rapor `--out` ve
`--write-whale-calibration` açık bayrakları hariç). Emir/imza YOK; yalnız public klines (sonuç çözümü için).

İKİ MODÜL, plan §7 ölçütleri (önceden sabit):
  WHALE (Faz 5): whale_metrics satırları kaydedilirken sonuçsuzdur → satır zamanından sonraki 15m mumlarla çözülür
     (giriş = sonraki mum açılışı, ufuk = 8 mum, real = fwd > maliyet ∧ SL aşılmadı; backtest ile AYNI tanım).
     PASS ⇔ Brier skill > 0 (p_win vs genişleyen-taban) ∧ kabul n ≥ 30 ∧ Wilson alt(kabul) > taban
            ∧ maliyet sonrası beklenti > 0 ∧ permütasyon p < 0.10.
  STRATEJİ (Faz 6): signal_outcomes canlı çözülmüş satırlar; genişleyen pencere walk-forward (purge'lü). Eğitimde
     sinyal×rejim hücresi: n ≥ min_events ∧ permütasyon p (BH-FDR) ∧ lift > 0; test'te yalnız onaylı hücre ∧
     p_real (Beta posterior) ≥ eşik olaylar KABUL. PASS ⇔ OOS kabul n ≥ 30 ∧ Wilson alt > taban ∧ beklenti > 0 ∧ p < 0.10.
Satırlar ufuk kadar declustered (aynı sembolde örtüşen sonuçlar tek sayılır) — şişirilmiş n yok.
Sonuç: EDGE_EXISTS / NO_EDGE / INSUFFICIENT_DATA. Çıkış kodu 0 / 1 / 2. `on`a geçiş ayrıca insan onayı ister.
"""
import argparse, json, sqlite3, sys, time
from pathlib import Path

import confirm_backtest as cb
import strategy_engine as se
import whale_engine as we

HOME = Path.home()
M15 = cb.TF_MS["15m"]
EDGE, NO_EDGE, INSUFFICIENT = "EDGE_EXISTS", "NO_EDGE", "INSUFFICIENT_DATA"
DEFAULT_COST = cb.SIM_DEFAULTS["fee_rt"] + cb.SIM_DEFAULTS["slip"]


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _rate(x):
    return sum(x) / len(x) if x else None


def _mean(x):
    return sum(x) / len(x) if x else None


def decluster(rows, key, ts_key, gap_ms):
    """Her anahtar için, öncekinden ≥ gap_ms sonraki satırı tut (örtüşen ufuklar tek sayılır)."""
    last, out = {}, []
    for r in sorted(rows, key=lambda r: r[ts_key]):
        k = key(r)
        if k in last and r[ts_key] - last[k] < gap_ms:
            continue
        last[k] = r[ts_key]
        out.append(r)
    return out


# ===================================================================== veri yükleme (salt-okunur)
def load_whale_rows(path, modes=("compare", "on")):
    if not Path(path).exists():
        return []
    c = _ro(path)
    try:
        q = ("SELECT recorded_at,symbol,mode,side,quality_ok,spoof,p_win,e_net,cost,allow,components_json "
             "FROM whale_metrics WHERE quality_ok=1 AND p_win IS NOT NULL AND side IN (-1,1) AND mode IN (%s)"
             % ",".join("?" * len(modes)))
        out = []
        for ra, sym, mode, side, _q, spoof, pw, en, cost, allow, comp in c.execute(q, modes):
            try:
                cvd = json.loads(comp or "{}").get("cvd")
            except Exception:
                cvd = None
            out.append({"ts": int(ra * 1000), "symbol": sym, "side": int(side), "spoof": int(spoof or 0),
                        "p_win": float(pw), "e_net": en, "cost": float(cost) if cost is not None else DEFAULT_COST,
                        "allow": int(allow or 0), "cvd": cvd})
        return out
    finally:
        c.close()


def load_signal_rows(path):
    if not Path(path).exists():
        return []
    c = _ro(path)
    try:
        return [{"ts": int(ts), "symbol": s, "signal": sg, "regime": rg, "side": int(d), "real": int(real),
                 "fwd": float(fwd), "horizon": int(hz)}
                for s, sg, rg, d, ts, hz, real, fwd in c.execute(
                    "SELECT symbol,signal,regime,direction,entry_ts,horizon,real,fwd_ret FROM signal_outcomes "
                    "WHERE source='live' AND resolved=1 AND real IS NOT NULL AND fwd_ret IS NOT NULL")]
    finally:
        c.close()


# ===================================================================== whale: sonuç çözümü + istatistik
def resolve_whale_rows(rows, getter, *, horizon=8, sl_pct=0.03, cache_dir=None, now_ms=None, sleeper=time.sleep):
    """Her satır için (real, fwd) hesapla. Eksik/yetersiz mum → satır atlanır (sayılır)."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    end = now_ms - now_ms % M15
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    out, skipped = [], 0
    for sym, rs in by_sym.items():
        start = min(r["ts"] for r in rs) - M15
        raw = cb.load_or_fetch(getter, sym, "15m", start - start % M15, end, cache_dir, sleeper=sleeper)
        cd = se.parse_klines(raw, drop_unclosed=False) if raw else None
        if not cd:
            skipped += len(rs)
            continue
        import bisect
        for r in rs:
            j = bisect.bisect_left(cd.t, r["ts"])              # ilk mum açılışı ≥ kayıt zamanı
            if j + horizon > len(cd.t) or j >= len(cd.t):
                skipped += 1
                continue
            res = se.outcome_from_bars(r["side"], cd.o[j], cd.h[j:j + horizon], cd.l[j:j + horizon],
                                       cd.c[j:j + horizon], horizon, r["cost"], sl_pct)
            if res is None:
                skipped += 1
                continue
            real, fwd, _ = res
            out.append({**r, "real": int(real), "fwd": fwd, "net": fwd - r["cost"], "horizon": horizon})
    return out, skipped


def brier_skill_expanding(rows):
    """Model: p_win. Taban: o ana kadarki Laplace oranı (ileri bakış YOK). p_win sabit formül/kalibrasyondan geldiği
    için satırlar üzerinde eğitilmemiştir → doğal OOS."""
    bm = bb = 0.0
    wins = n = 0
    for r in sorted(rows, key=lambda r: r["ts"]):
        base = (wins + 1) / (n + 2)
        bm += (r["p_win"] - r["real"]) ** 2
        bb += (base - r["real"]) ** 2
        wins += r["real"]; n += 1
    return (1.0 - bm / bb) if bb > 0 else 0.0


def whale_analysis(resolved, *, min_rows=300, min_days=14, min_acc=30, n_perm=2000, seed=0, horizon=8):
    rows = decluster(resolved, lambda r: r["symbol"], "ts", horizon * M15)
    n = len(rows)
    days = len({time.strftime("%Y-%m-%d", time.gmtime(r["ts"] / 1000)) for r in rows})
    acc = [r for r in rows if r["allow"]]
    rej = [r for r in rows if not r["allow"]]
    all_flags = [r["real"] for r in rows]
    base = _rate(all_flags)
    out = {"n_resolved_raw": len(resolved), "n_declustered": n, "distinct_days": days, "n_accepted": len(acc),
           "n_rejected": len(rej), "base_real_rate": base,
           "base_net_expectancy": _mean([r["net"] for r in rows]),
           "spoof_rate": _rate([r["spoof"] for r in rows])}
    if n:
        out["brier_skill"] = brier_skill_expanding(rows)
    if acc:
        lo, hi = se.wilson_interval(sum(r["real"] for r in acc), len(acc))
        out.update(accepted_real_rate=_rate([r["real"] for r in acc]), accepted_wilson=[lo, hi],
                   accepted_net_expectancy=_mean([r["net"] for r in acc]),
                   predicted_mean_e_net=_mean([r["e_net"] for r in acc if r["e_net"] is not None]))
        if rej:
            out["p_value"] = se.permutation_pvalue([r["real"] for r in acc], [r["real"] for r in rej], n_perm, seed)
    if n < min_rows or days < min_days or len(acc) < min_acc:
        out["verdict"] = INSUFFICIENT
        out["reason"] = (f"declustered n={n} (<{min_rows}) veya gün={days} (<{min_days}) "
                         f"veya kabul={len(acc)} (<{min_acc})")
        return out
    checks = {"brier_skill>0": out["brier_skill"] > 0,
              "wilson_lo>taban": out["accepted_wilson"][0] > base,
              "net_beklenti>0": (out["accepted_net_expectancy"] or 0) > 0,
              "p<0.10": out.get("p_value", 1.0) < 0.10}
    out["checks"] = checks
    out["verdict"] = EDGE if all(checks.values()) else NO_EDGE
    return out


def whale_pwin_posterior(resolved, *, horizon=8, min_events=100):
    """Canlı veriyle p_win kovası (taker-akış hizası) kalibrasyonu + OOS skill. Dosya YAZMAZ."""
    evs = [{"ts": r["ts"], "side": r["side"], "cvd": r["cvd"], "real": r["real"], "horizon": horizon}
           for r in decluster(resolved, lambda r: r["symbol"], "ts", horizon * M15)]
    if len(evs) < 8:
        return {"note": "yetersiz satır"}, None
    return cb.whale_pwin_walk_forward(evs, n_folds=4, min_events=min_events)


# ===================================================================== strateji: p_fake posterior + walk-forward
def p_real_table(rows, *, min_events=100, a0=1.0, b0=1.0):
    cells = {}
    for r in rows:
        c = cells.setdefault((r["signal"], r["regime"]), [0, 0])
        c[0] += r["real"]; c[1] += 1
    out = {}
    for (sg, rg), (w, n) in sorted(cells.items()):
        a, b = a0 + w, b0 + n - w
        lo, hi = se.wilson_interval(w, n)
        out[f"{sg}|{rg}"] = {"n": n, "wins": w, "p_real": a / (a + b), "p_fake": 1 - a / (a + b),
                             "calibrated": n >= min_events, "wilson": [lo, hi]}
    return out


def _approved_cells(train, min_events, fdr, n_perm, seed):
    cells, tested = {}, []
    for r in train:
        cells.setdefault((r["signal"], r["regime"]), []).append(r)
    for k, rs in cells.items():
        rest = [x["real"] for kk, v in cells.items() if kk != k for x in v]
        if len(rs) >= min_events and rest:
            p = se.permutation_pvalue([x["real"] for x in rs], rest, n_perm, seed)
            lift = _rate([x["real"] for x in rs]) - _rate(rest)
            tested.append((k, p, lift, rs))
    if not tested:
        return {}
    rej = se.benjamini_hochberg([t[1] for t in tested], fdr)
    out = {}
    for (k, p, lift, rs), ok in zip(tested, rej):
        if ok and lift > 0:
            w = sum(x["real"] for x in rs)
            out[k] = (1.0 + w) / (2.0 + len(rs))                  # p_real posterior ortalaması
    return out


def strategy_analysis(rows, *, cost=DEFAULT_COST, min_events=100, fdr=0.10, min_p_real=0.65, folds=4,
                      min_acc=30, n_perm=1000, seed=0, horizon=8):
    rows = decluster(rows, lambda r: (r["symbol"], r["signal"], r["side"]), "ts", horizon * M15)
    out = {"n_declustered": len(rows), "posteriors": p_real_table(rows, min_events=min_events)}
    if len(rows) < folds * 2:
        out.update(verdict=INSUFFICIENT, reason=f"satır {len(rows)} < {folds * 2}")
        return out
    ts = sorted(r["ts"] for r in rows)
    bounds = [ts[int(len(ts) * k / folds)] for k in range(folds)] + [float("inf")]
    acc, rej, all_ = [], [], []
    approved_any = {}
    for k in range(1, folds):
        start, end = bounds[k], bounds[k + 1]
        test = [r for r in rows if start <= r["ts"] < end]
        train = [r for r in rows if r["ts"] + horizon * M15 < start]
        if not test or not train:
            continue
        ok = _approved_cells(train, min_events, fdr, n_perm, seed)
        for kk, v in ok.items():
            approved_any[f"{kk[0]}|{kk[1]}"] = v
        for r in test:
            all_.append(r)
            p_real = ok.get((r["signal"], r["regime"]))
            (acc if (p_real is not None and p_real >= min_p_real) else rej).append(r)
    base = _rate([r["real"] for r in all_])
    out.update(n_test=len(all_), n_accepted=len(acc), n_rejected=len(rej), base_real_rate=base,
               approved_cells_by_fold=approved_any, min_p_real=min_p_real)
    if acc:
        lo, hi = se.wilson_interval(sum(r["real"] for r in acc), len(acc))
        out.update(accepted_real_rate=_rate([r["real"] for r in acc]), accepted_wilson=[lo, hi],
                   accepted_net_expectancy=_mean([r["fwd"] - cost for r in acc]),
                   base_net_expectancy=_mean([r["fwd"] - cost for r in all_]))
        if rej:
            out["p_value"] = se.permutation_pvalue([r["real"] for r in acc], [r["real"] for r in rej], n_perm, seed)
    if len(acc) < min_acc:
        out.update(verdict=INSUFFICIENT, reason=f"OOS kabul n={len(acc)} < {min_acc}")
        return out
    checks = {"wilson_lo>taban": out["accepted_wilson"][0] > base,
              "net_beklenti>0": (out["accepted_net_expectancy"] or 0) > 0,
              "p<0.10": out.get("p_value", 1.0) < 0.10}
    out["checks"] = checks
    out["verdict"] = EDGE if all(checks.values()) else NO_EDGE
    return out


# ===================================================================== birleşik karar
def overall(whale, strat):
    vs = {"whale": whale["verdict"], "strategy": strat["verdict"]}
    on = [k for k, v in vs.items() if v == EDGE]
    if on:
        flags = {"whale": "WHALE_HUNTING", "strategy": "STRATEGY_CONFIRMATION"}
        return EDGE, ("Kenar Var (Edge Exists) → on moduna geçilebilir: " + ", ".join(flags[k] for k in on)
                      + " (diğerleri off/compare kalır; geçiş ayrıca insan onayı gerektirir)")
    if all(v == NO_EDGE for v in vs.values()):
        return NO_EDGE, "Kenar Yok (No Edge) → off modunda kalınmalı"
    if any(v == NO_EDGE for v in vs.values()):
        return INSUFFICIENT, ("Kısmen Kenar Yok: " + ", ".join(f"{k}={v}" for k, v in vs.items())
                              + " → hiçbir modül on'a geçemez; yetersiz olan toplamaya devam")
    return INSUFFICIENT, "Veri Yetersiz (Insufficient Data) → compare'da veri toplamaya devam; on'a GEÇİLMEZ"


def run(whale_db, signal_db, getter, *, cache_dir=None, now_ms=None, sleeper=time.sleep, **kw):
    wrows = load_whale_rows(whale_db)
    resolved, skipped = ([], 0)
    if wrows:
        resolved, skipped = resolve_whale_rows(wrows, getter, horizon=kw.get("horizon", 8),
                                               sl_pct=kw.get("sl_pct", 0.03), cache_dir=cache_dir,
                                               now_ms=now_ms, sleeper=sleeper)
    w = whale_analysis(resolved, min_rows=kw.get("min_rows", 300), min_days=kw.get("min_days", 14),
                       n_perm=kw.get("n_perm", 2000), horizon=kw.get("horizon", 8))
    w["n_rows_loaded"], w["n_unresolved_skipped"] = len(wrows), skipped
    pw, pw_final = whale_pwin_posterior(resolved, horizon=kw.get("horizon", 8))
    s = strategy_analysis(load_signal_rows(signal_db), cost=kw.get("cost", DEFAULT_COST),
                          min_p_real=kw.get("min_p_real", 0.65), n_perm=kw.get("n_perm", 2000),
                          horizon=kw.get("horizon", 8))
    verdict, text = overall(w, s)
    return {"verdict": verdict, "decision": text, "whale": w, "whale_pwin_posterior": pw,
            "strategy": s}, pw


def render(rep):
    w, s = rep["whale"], rep["strategy"]
    f = lambda x, p=3: "-" if x is None else f"{x:.{p}f}"
    L = [f"=== Faz 9 final analiz: {rep['verdict']} ===", rep["decision"], "",
         f"WHALE [{w['verdict']}] ham={w['n_resolved_raw']} declustered={w['n_declustered']} gün={w['distinct_days']} "
         f"kabul={w['n_accepted']} atlanan(çözülmemiş)={w['n_unresolved_skipped']}",
         f"  Brier skill={f(w.get('brier_skill'), 4)} taban oran={f(w.get('base_real_rate'))} "
         f"kabul oran={f(w.get('accepted_real_rate'))} Wilson alt={f((w.get('accepted_wilson') or [None])[0])}",
         f"  E_net gerçekleşen (maliyet sonrası)={f(w.get('accepted_net_expectancy'), 5)} "
         f"tahmin(E_net)={f(w.get('predicted_mean_e_net'), 5)} p={f(w.get('p_value'))}",
         f"STRATEJİ [{s['verdict']}] declustered={s['n_declustered']} OOS kabul={s.get('n_accepted', '-')} "
         f"taban={f(s.get('base_real_rate'))} kabul oran={f(s.get('accepted_real_rate'))} "
         f"Wilson alt={f((s.get('accepted_wilson') or [None])[0])} "
         f"E_net={f(s.get('accepted_net_expectancy'), 5)} p={f(s.get('p_value'))}"]
    for name in ("whale", "strategy"):
        d = rep[name]
        if d.get("reason"):
            L.append(f"  {name}: {d['reason']}")
        if d.get("checks"):
            L.append(f"  {name} ölçütler: {d['checks']}")
    return "\n".join(L)


EXIT = {EDGE: 0, NO_EDGE: 1, INSUFFICIENT: 2}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Faz 9 final analiz (salt-okunur; emir YOK)")
    ap.add_argument("--whale-db", default=str(HOME / "whale_metrics.db"))
    ap.add_argument("--signal-db", default=str(HOME / "signal_outcomes.db"))
    ap.add_argument("--min-rows", type=int, default=300, help="whale declustered asgari satır")
    ap.add_argument("--min-days", type=int, default=14)
    ap.add_argument("--min-p-real", type=float, default=0.65)
    ap.add_argument("--sl-pct", type=float, default=0.03, help="whale sonuç çözümünde SL (bot STOP_LOSS_PCT)")
    ap.add_argument("--n-perm", type=int, default=2000)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--out", default=None, help="rapor JSON yolu")
    ap.add_argument("--write-whale-calibration", default=None,
                    help="AÇIK ONAY: canlı-fit p_win kalibrasyonunu yazar (OOS skill>0). Toplama BİTMEDEN kullanma: "
                         "bot compare'da bu dosyayı okur ve deneyi bozar.")
    a = ap.parse_args(argv)
    try:
        rep, _ = run(a.whale_db, a.signal_db, cb.default_getter, cache_dir=a.cache_dir, min_rows=a.min_rows,
                     min_days=a.min_days, min_p_real=a.min_p_real, sl_pct=a.sl_pct, n_perm=a.n_perm)
    except cb.RateLimited as e:
        print(f"[analyze_live_data] RATE-LIMIT, durduruldu (tekrar denemeyin): {e}", file=sys.stderr)
        return 2
    if a.write_whale_calibration:
        resolved_calib = rep["whale_pwin_posterior"]
        rep["whale_calibration_written"] = cb.write_whale_calibration(resolved_calib, a.write_whale_calibration)
    print(render(rep))
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=2, default=str))
    return EXIT[rep["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
