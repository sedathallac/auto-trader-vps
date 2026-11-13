#!/usr/bin/env python3
"""confirm_backtest.py — Faz 8: offline backtest / kalibrasyon koşucusu (CANLI İŞLEM YAPMAZ).

Geçmiş Binance klines (YALNIZ public /fapi/v1/klines, anahtarsız, salt-okunur) ile:
  1) strategy_engine p_fake / p_real posterior'larını eğitir (sinyal × rejim),
  2) whale p_win kalibrasyonunu (taker-akış vekili) eğitir,
  3) WALK-FORWARD + purge/embargo + out-of-sample (OOS) testiyle aşırı uyumu engeller.

AŞIRI UYUM KORUMALARI
  • Zaman sıralı genişleyen pencere; her test dilimi YALNIZ kendinden ÖNCEKİ (purge'lü) veriyle eğitilir.
  • Sinyal seçimi YALNIZ eğitimde: permütasyon p-değeri (sinyal-var vs sinyal-yok, aynı rejim)
    + Benjamini–Hochberg FDR + lift>0 + min_events. Seçilemeyen sinyal nötr (p_fake=0.5).
  • Raporlanan başarı YALNIZ OOS; çıkarılan kural canlıdaki evaluate() ile AYNI koddur.
  • Whale kalibrasyonu OOS Brier skill > 0 değilse DİSKE YAZILMAZ (ve engine kullanmaz).
  • Çakışan olaylar sembol başına H mum aralıkla seyreltilir (bağımsızlık).

KAPSAM İZOLASYONU
  • auto_trader import edilmez; borsaya emir/imzalı istek YOK; API anahtarı kullanılmaz.
  • Varsayılan: hiçbir DB/dosya yazılmaz (yalnız rapor stdout/--out). Kalıcı kalibrasyon yalnız
    açık bayraklarla: --write-db PATH, --write-whale-calibration PATH.
  • Rate-limit: ağırlık izlenir, istekler arası bekleme, 418/429'da DUR (tekrar vurma), disk cache.

Kullanım (örnek):
  python3 confirm_backtest.py --symbols BTCUSDT,ETHUSDT --days 90 --folds 4 --out report.json
"""
import argparse
import bisect
import json
import math
import sys
import time
from pathlib import Path

import strategy_engine as se

BINANCE_FAPI = "https://fapi.binance.com"
KLINES_PATH = "/fapi/v1/klines"          # tek kullanılan endpoint (public, anahtarsız)
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}

# Botun sabitleri (auto_trader'dan import edilmez; test eşitliği doğrular).
SIM_DEFAULTS = {
    "long_dip_pct": -3.0, "short_pump_pct": 3.0, "min_abs_pct": 3.0, "max_abs_pct": 15.0,
    "fee_rt": 0.0008, "slip": 0.0004,
    "atr_sl_mult": 1.5, "sl_min": 0.010, "sl_max": 0.10,
    "lookback_24h": 96, "window": 250, "min_hist": 130,
}
WHALE_ALIGN_MIN = 0.15
WHALE_PROXY_BARS = 4


class RateLimited(Exception):
    pass


# ===================================================================== veri çekme (public GET)
def default_getter(params):
    """(rows, used_weight_1m). 418/429 → RateLimited. Anahtar/imza YOK."""
    import requests
    r = requests.get(BINANCE_FAPI + KLINES_PATH, params=params, timeout=15)
    if r.status_code in (418, 429):
        raise RateLimited(f"HTTP {r.status_code} Retry-After={r.headers.get('Retry-After')}")
    r.raise_for_status()
    return r.json(), int(r.headers.get("X-MBX-USED-WEIGHT-1M", 0) or 0)


def fetch_klines_range(getter, symbol, tf, start_ms, end_ms, *, limit=1000, sleep_s=0.35,
                       max_requests=200, weight_soft_limit=1200, sleeper=time.sleep):
    """[start_ms, end_ms) aralığındaki KAPANMIŞ mumlar (ham Binance satırları). Sayfalama + rate-limit."""
    rows, cursor, reqs = [], int(start_ms), 0
    step = TF_MS[tf]
    while cursor < end_ms and reqs < max_requests:
        data, used = getter({"symbol": symbol, "interval": tf, "startTime": cursor,
                             "endTime": int(end_ms), "limit": limit})
        reqs += 1
        if not data:
            break
        rows.extend(data)
        nxt = int(data[-1][0]) + step
        if nxt <= cursor:
            break
        cursor = nxt
        if used >= weight_soft_limit:
            sleeper(60.0)            # dakikalık ağırlık bütçesine yaklaşıldı → bir pencere bekle
        else:
            sleeper(sleep_s)
    return [r for r in rows if int(r[0]) + step <= end_ms]      # oluşmakta olan mum yok


def load_or_fetch(getter, symbol, tf, start_ms, end_ms, cache_dir=None, **kw):
    """Disk cache: kapsama yeterliyse ağa gitmez."""
    path = Path(cache_dir) / f"{symbol}_{tf}.json" if cache_dir else None
    step = TF_MS[tf]
    if path and path.exists():
        try:
            rows = json.loads(path.read_text())
            if rows and int(rows[0][0]) <= start_ms + step and int(rows[-1][0]) + step >= end_ms - 2 * step:
                return [r for r in rows if start_ms <= int(r[0]) and int(r[0]) + step <= end_ms]
        except Exception:
            pass
    rows = fetch_klines_range(getter, symbol, tf, start_ms, end_ms, **kw)
    if path and rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows))
    return rows


# ===================================================================== olay üretimi (replay)
def _slice(cd, lo, hi):
    return se.Candles(cd.o[lo:hi], cd.h[lo:hi], cd.l[lo:hi], cd.c[lo:hi], cd.v[lo:hi], cd.t[lo:hi],
                      cd.tb[lo:hi] if cd.tb else [])


def cvd_proxy(cd, bars=WHALE_PROXY_BARS):
    """Taker-akış vekili ∈ [-1,1]: (2·tb − v)/v son `bars` mumda. tb yok → None."""
    if not cd.tb or len(cd) < bars:
        return None
    v, tb = sum(cd.v[-bars:]), sum(cd.tb[-bars:])
    return None if v <= 0 else max(-1.0, min(1.0, (2.0 * tb - v) / v))


def replay_events(symbol, c15, c1h, c4h, *, horizon=8, stride=4, sim=None, cfg=None):
    """Botun mean-reversion adaylarını (decide_side kuralı) geçmişte yeniden oynatır.

    LOOK-AHEAD YOK: i. 15m mumu kapandığında yalnız o ana kadar KAPANMIŞ 15m/1h/4h mumları kullanılır;
    giriş fiyatı SONRAKİ mumun açılışı; sonuç sonraki `horizon` mum.
    Dönen her olay: ts, side, regime, readings, trends, real, fwd, persist, sl, cvd, entry."""
    sim = {**SIM_DEFAULTS, **(sim or {})}
    n = len(c15)
    ends1 = [t + TF_MS["1h"] for t in c1h.t]
    ends4 = [t + TF_MS["4h"] for t in c4h.t]
    cost = sim["fee_rt"] + sim["slip"]
    win = sim["window"]
    events, last_i = [], -10 ** 9
    for i in range(max(sim["min_hist"], sim["lookback_24h"]), n - horizon - 1, stride):
        if i - last_i < horizon:
            continue                                            # declustering
        chg = (c15.c[i] / c15.c[i - sim["lookback_24h"]] - 1.0) * 100.0
        if not (sim["min_abs_pct"] <= abs(chg) <= sim["max_abs_pct"]):
            continue
        if chg <= sim["long_dip_pct"]:
            side = 1
        elif chg >= sim["short_pump_pct"]:
            side = -1
        else:
            continue
        t_now = c15.t[i] + TF_MS["15m"]
        k1, k4 = bisect.bisect_right(ends1, t_now), bisect.bisect_right(ends4, t_now)
        if k1 < 210 or k4 < 210:
            continue
        main = _slice(c15, max(0, i + 1 - win), i + 1)
        tr1 = se.trend_reading(_slice(c1h, max(0, k1 - win), k1), "1h")
        tr4 = se.trend_reading(_slice(c4h, max(0, k4 - win), k4), "4h")
        if tr1 is None or tr4 is None:
            continue
        regime = se.regime_from_adx(se.adx_last(main.h, main.l, main.c), cfg)
        atr = se.atr_series(main.h, main.l, main.c, 14)
        if not atr:
            continue
        sl = min(max(atr[-1] / c15.c[i] * sim["atr_sl_mult"], sim["sl_min"]), sim["sl_max"])
        entry = c15.o[i + 1]
        fh, fl, fc = (c15.h[i + 1:i + 1 + horizon], c15.l[i + 1:i + 1 + horizon],
                      c15.c[i + 1:i + 1 + horizon])
        res = se.outcome_from_bars(side, entry, fh, fl, fc, horizon, cost, sl)
        if res is None:
            continue
        real, fwd, persist = res
        events.append({"symbol": symbol, "ts": c15.t[i + 1], "side": side, "regime": regime,
                       "readings": se.compute_readings(main), "trends": [tr1, tr4],
                       "real": int(real), "fwd": fwd, "persist": persist, "sl": sl,
                       "cvd": cvd_proxy(main), "entry": entry, "horizon": horizon, "cost": cost})
        last_i = i
    return events


# ===================================================================== sinyal eğitimi (train)
def active_signals(ev, cfg=None):
    """Olayda işlem yönünde ve min_strength üstünde aktif sinyal adları (aile başına birden fazla olabilir)."""
    thr = (cfg or {}).get("confirm_min_strength", se.CONFIRM_DEFAULTS["confirm_min_strength"])
    return {r.name for r in ev["readings"] if r.direction == ev["side"] and r.strength >= thr}


def fit_signal_table(train, *, cfg=None, min_events=None, fdr=None, n_perm=500, seed=0):
    """(signal, regime) → {n, wins, lift, p, approved, p_fake}. YALNIZ train verisi.
    Seçim: n ≥ min_events, permütasyon p (sinyal-var vs sinyal-yok, aynı rejim), BH-FDR, lift > 0."""
    cfg = cfg or {}
    min_events = se.CONFIRM_DEFAULTS["confirm_min_events"] if min_events is None else min_events
    fdr = se.CONFIRM_DEFAULTS["confirm_fdr"] if fdr is None else fdr
    a0 = cfg.get("confirm_beta_a0", se.CONFIRM_DEFAULTS["confirm_beta_a0"])
    b0 = cfg.get("confirm_beta_b0", se.CONFIRM_DEFAULTS["confirm_beta_b0"])
    groups = {}
    all_names = {r.name for ev in train for r in ev["readings"]}
    for ev in train:
        act = active_signals(ev, cfg)
        for name in all_names:                       # "no" = aynı rejimde sinyal AKTİF OLMAYAN tüm olaylar
            g = groups.setdefault((name, ev["regime"]), {"yes": [], "no": []})
            g["yes" if name in act else "no"].append(ev["real"])
    table, tested = {}, []
    for key, g in groups.items():
        yes, no = g["yes"], g["no"]
        n, wins = len(yes), sum(yes)
        row = {"n": n, "wins": wins, "n_no": len(no), "lift": None, "p": 1.0,
               "approved": False, "p_fake": 0.5}
        if n >= min_events and no:
            row["lift"] = wins / n - sum(no) / len(no)
            row["p"] = se.permutation_pvalue(yes, no, n_perm, seed)
            tested.append(key)
        table[key] = row
    if tested:
        rej = se.benjamini_hochberg([table[k]["p"] for k in tested], fdr)
        for k, r in zip(tested, rej):
            if r and table[k]["lift"] > 0:
                table[k]["approved"] = True
                row = table[k]
                row["p_fake"] = 1.0 - (a0 + row["wins"]) / (a0 + b0 + row["n"])
    return table


def p_fake_fn_from_table(table):
    def fn(name, regime):
        row = table.get((name, regime))
        return (row["p_fake"], True) if row and row["approved"] else (0.5, False)
    return fn


# ===================================================================== walk-forward
def time_folds(events, n_folds):
    """Olay zamanına göre n_folds eşit-sayılı dilim sınırları: [start_ts_0, ..., start_ts_{n-1}, +inf]."""
    ts = sorted(e["ts"] for e in events)
    if len(ts) < n_folds * 2:
        return None
    bounds = [ts[int(len(ts) * k / n_folds)] for k in range(n_folds)]
    return bounds + [float("inf")]


def _rate(flags):
    return sum(flags) / len(flags) if flags else None


def walk_forward(events, *, n_folds=4, cfg=None, min_events=None, fdr=None, n_perm=500, seed=0,
                 min_oos_events=30, bar_ms=TF_MS["15m"]):
    """Genişleyen pencere. Fold 0 yalnız eğitir; fold k≥1 test: eğitim = ts + horizon·bar < fold_start (purge)."""
    cfg = cfg or {}
    bounds = time_folds(events, n_folds)
    if bounds is None:
        return {"verdict": "INSUFFICIENT_DATA", "reason": f"olay sayısı ({len(events)}) < {n_folds * 2}",
                "n_events": len(events), "folds": []}
    ordered = sorted(events, key=lambda e: e["ts"])
    all_flags, acc_flags, rej_flags, all_net, acc_net, folds = [], [], [], [], [], []
    vetoed = 0
    for k in range(1, n_folds):
        start, end = bounds[k], bounds[k + 1]
        test = [e for e in ordered if start <= e["ts"] < end]
        train = [e for e in ordered if e["ts"] + e["horizon"] * bar_ms < start]
        if not test or not train:
            continue
        table = fit_signal_table(train, cfg=cfg, min_events=min_events, fdr=fdr, n_perm=n_perm, seed=seed)
        pf = p_fake_fn_from_table(table)
        f_acc, f_all = [], []
        for e in test:
            v = se.evaluate(e["side"], e["readings"], e["trends"], e["regime"], pf, cfg)
            net = e["fwd"] - e["cost"]
            all_flags.append(e["real"]); all_net.append(net); f_all.append(e["real"])
            if v.allow:
                acc_flags.append(e["real"]); acc_net.append(net); f_acc.append(e["real"])
            else:
                rej_flags.append(e["real"])
                vetoed += v.veto
        folds.append({"fold": k, "n_train": len(train), "n_test": len(test), "n_accepted": len(f_acc),
                      "approved_signals": sorted(f"{n}|{r}" for (n, r), row in table.items() if row["approved"]),
                      "base_real_rate": _rate(f_all), "accepted_real_rate": _rate(f_acc)})
    n_test, n_acc = len(all_flags), len(acc_flags)
    base, acc = _rate(all_flags), _rate(acc_flags)
    out = {"n_events": len(events), "n_test": n_test, "n_accepted": n_acc, "n_rejected": len(rej_flags),
           "n_vetoed": vetoed, "accept_rate": (n_acc / n_test) if n_test else None,
           "base_real_rate": base, "accepted_real_rate": acc,
           "base_wilson": se.wilson_interval(sum(all_flags), n_test) if n_test else None,
           "accepted_wilson": se.wilson_interval(sum(acc_flags), n_acc) if n_acc else None,
           "base_expectancy": (sum(all_net) / len(all_net)) if all_net else None,
           "accepted_expectancy": (sum(acc_net) / len(acc_net)) if acc_net else None,
           "folds": folds}
    if n_acc >= min_oos_events and rej_flags:
        out["lift"] = acc - base
        out["p_value"] = se.permutation_pvalue(acc_flags, rej_flags, n_perm, seed)
        edge = (out["accepted_expectancy"] or 0) > (out["base_expectancy"] or 0)
        out["verdict"] = ("PASS" if out["lift"] > 0 and out["p_value"] < 0.10 and edge else "NO_EDGE")
    else:
        out["verdict"] = "INSUFFICIENT_DATA"
        out["reason"] = f"OOS kabul edilen olay ({n_acc}) < {min_oos_events} (veya hiç ret yok)"
    return out


# ===================================================================== whale p_win kalibrasyonu
def whale_align(ev):
    c = ev.get("cvd")
    if c is None or abs(c) < WHALE_ALIGN_MIN:
        return 0.0
    return ev["side"] * c


def fit_pwin_buckets(events, edges, min_events):
    """Kova başına n, wins, p_win (Wilson alt/üst ile). Kova n<min_events → p_win yine hesaplanır ama n raporlanır."""
    import whale_engine as we
    b = [{"lo": edges[i], "hi": edges[i + 1], "n": 0, "wins": 0} for i in range(len(edges) - 1)]
    for e in events:
        k = we.pwin_bucket(whale_align(e), edges)
        b[k]["n"] += 1
        b[k]["wins"] += e["real"]
    base = _rate([e["real"] for e in events])
    for x in b:
        x["p_win"] = (x["wins"] / x["n"]) if x["n"] >= min_events else base
        lo, hi = se.wilson_interval(x["wins"], x["n"])
        x["wilson"] = [lo, hi]
    return b, base


def whale_pwin_walk_forward(events, *, n_folds=4, min_events=100, edges=None, bar_ms=TF_MS["15m"]):
    """OOS Brier skill: kova-model vs sabit train taban oranı. skill>0 ⇒ kalibrasyon aşırı uyum değil."""
    import whale_engine as we
    edges = edges or we.PWIN_EDGES
    bounds = time_folds(events, n_folds)
    ordered = sorted(events, key=lambda e: e["ts"])
    if bounds is None:
        return {"oos_brier_skill": 0.0, "n_oos": 0}, None
    bm = bb = 0.0
    n_oos = 0
    for k in range(1, n_folds):
        start, end = bounds[k], bounds[k + 1]
        test = [e for e in ordered if start <= e["ts"] < end]
        train = [e for e in ordered if e["ts"] + e["horizon"] * bar_ms < start]
        if not test or not train:
            continue
        buckets, base = fit_pwin_buckets(train, edges, min_events)
        for e in test:
            p = buckets[we.pwin_bucket(whale_align(e), edges)]["p_win"]
            bm += (p - e["real"]) ** 2
            bb += (base - e["real"]) ** 2
            n_oos += 1
    skill = (1.0 - bm / bb) if (n_oos and bb > 0) else 0.0
    final, base_all = fit_pwin_buckets(ordered, edges, min_events)
    return ({"oos_brier_skill": skill, "n_oos": n_oos, "base_rate": base_all,
             "edges": list(edges), "buckets": final,
             "note": "p_win = takerBuy/Sell akış VEKİLİ (kline[9]) ile eğitildi; OBI/duvar tarihsel REST'te yok."},
            final)


# ===================================================================== kalıcı çıktılar (yalnız açık bayrakla)
def write_outcomes_db(events, table, path, horizon):
    """Onaylı (signal,regime) için çözülmüş offline satırları signal_outcomes.db'ye yazar (risk_ledger'a DOKUNMAZ)."""
    store = se.SignalStore(path)
    n = 0
    for e in events:
        act = {r.name: r for r in e["readings"] if r.direction == e["side"]}
        for name, r in act.items():
            row = table.get((name, e["regime"]))
            if row and row["approved"]:
                n += store.record_resolved(e["symbol"], r, e["regime"], e["ts"], e["entry"],
                                           horizon, e["real"], e["fwd"], e["persist"], "offline")
    return n


def write_whale_calibration(calib, path):
    """OOS skill > 0 değilse YAZMAZ (engine de kullanmaz). Döner: yazıldı mı."""
    if calib.get("oos_brier_skill", 0.0) <= 0.0:
        return False
    Path(path).write_text(json.dumps(calib, indent=2))
    return True


# ===================================================================== orkestrasyon
def run_backtest(getter, symbols, *, days=90, folds=4, horizon=8, stride=4, cache_dir=None,
                 min_events=None, fdr=None, n_perm=500, seed=0, now_ms=None, sleeper=time.sleep):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    end = now_ms - (now_ms % TF_MS["15m"])
    start15 = end - days * 86_400_000 - 3 * 86_400_000
    start1h = end - (days + 10) * 86_400_000
    start4h = end - (days + 40) * 86_400_000          # EMA200@4h için ~35 gün ısınma
    events = []
    for sym in symbols:
        r15 = load_or_fetch(getter, sym, "15m", start15, end, cache_dir, sleeper=sleeper)
        r1h = load_or_fetch(getter, sym, "1h", start1h, end, cache_dir, sleeper=sleeper)
        r4h = load_or_fetch(getter, sym, "4h", start4h, end, cache_dir, sleeper=sleeper)
        c15, c1h, c4h = (se.parse_klines(r, drop_unclosed=False) for r in (r15, r1h, r4h))
        if not (c15 and c1h and c4h):
            continue
        events += replay_events(sym, c15, c1h, c4h, horizon=horizon, stride=stride)
    cfg = {}
    wf = walk_forward(events, n_folds=folds, cfg=cfg, min_events=min_events, fdr=fdr,
                      n_perm=n_perm, seed=seed)
    whale, _ = whale_pwin_walk_forward(
        events, n_folds=folds,
        min_events=se.CONFIRM_DEFAULTS["confirm_min_events"] if min_events is None else min_events)
    final_table = fit_signal_table(events, cfg=cfg, min_events=min_events, fdr=fdr, n_perm=n_perm, seed=seed)
    return events, {"params": {"symbols": list(symbols), "days": days, "folds": folds, "horizon": horizon,
                               "stride": stride, "min_events": min_events, "fdr": fdr,
                               "n_events": len(events)},
                    "strategy_walk_forward": wf,
                    "final_signal_table": {f"{n}|{r}": row for (n, r), row in final_table.items()},
                    "whale_pwin": whale}, final_table, whale


def main(argv=None):
    ap = argparse.ArgumentParser(description="Faz 8 offline backtest/kalibrasyon (CANLI İŞLEM YOK)")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--horizon", type=int, default=8)
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--min-events", type=int, default=None, help="varsayılan: confirm_min_events (100)")
    ap.add_argument("--fdr", type=float, default=None)
    ap.add_argument("--n-perm", type=int, default=500)
    ap.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "confirm_backtest"))
    ap.add_argument("--out", default=None, help="rapor JSON yolu (varsayılan: yalnız stdout)")
    ap.add_argument("--write-db", default=None, help="signal_outcomes.db yolu (açık onay; varsayılan YAZMAZ)")
    ap.add_argument("--write-whale-calibration", default=None,
                    help="whale_pwin_calibration.json yolu (OOS skill>0 ise; varsayılan YAZMAZ)")
    a = ap.parse_args(argv)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    print(f"[confirm_backtest] public klines, salt-okunur; semboller={syms} gün={a.days}", file=sys.stderr)
    try:
        events, report, table, whale = run_backtest(
            default_getter, syms, days=a.days, folds=a.folds, horizon=a.horizon, stride=a.stride,
            cache_dir=a.cache_dir, min_events=a.min_events, fdr=a.fdr, n_perm=a.n_perm)
    except RateLimited as e:
        print(f"[confirm_backtest] RATE-LIMIT, durduruldu (tekrar denemeyin, cache kullanılır): {e}",
              file=sys.stderr)
        return 2
    if a.write_db:
        report["written_outcome_rows"] = write_outcomes_db(events, table, a.write_db, a.horizon)
    if a.write_whale_calibration:
        report["whale_calibration_written"] = write_whale_calibration(whale, a.write_whale_calibration)
    text = json.dumps(report, indent=2, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
