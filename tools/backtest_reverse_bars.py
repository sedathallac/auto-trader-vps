"""FAZ13-C2: ters yön sonucunu MUM VERİSİNDEN yeniden hesaplar (proxy değil).

`real` alanı outcome_from_bars() ile üretildi: (fwd > cost) and (mae > -sl_pct).
Bu script her satır için entry_ts'den itibaren 15m mumları yeniden çeker, kayıtlı
fwd_ret'i doğrular (eşleşmezse satır atılır) ve AYNI barlar üzerinde ters yönün
sonucunu aynı fonksiyonla hesaplar. Böylece "ters çevir" iddiası SL/TP yoluna
bağlı olarak sınanır, -fwd_ret proxy'sine dayanmaz.
"""
import json
import math
import random
import sqlite3
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, "/home/reisfurkan/faz9-live")
import strategy_engine as se

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/so_compare.db"
COST = 0.0008
NET_RR = 1.45
BREAKEVEN = 1.0 / (1.0 + NET_RR)
BOOT = 4000
BASE = "https://fapi.binance.com"


def klines(symbol, start_ms, limit):
    url = f"{BASE}/fapi/v1/klines?symbol={symbol}&interval=15m&startTime={start_ms}&limit={limit}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.0)
    return None


def wilson(k, n, z=1.959963985):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def boot_ci(xs, iters=BOOT, seed=11):
    if len(xs) < 5:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    n = len(xs)
    ms = sorted(sum(xs[rnd.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return (ms[int(0.025 * iters)], ms[int(0.975 * iters) - 1])


def t_stat(xs):
    if len(xs) < 3:
        return (float("nan"), float("nan"))
    sd = statistics.stdev(xs)
    if sd == 0:
        return (float("nan"), float("nan"))
    t = statistics.mean(xs) / (sd / math.sqrt(len(xs)))
    return (t, math.erfc(abs(t) / math.sqrt(2)))


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT symbol, signal, regime, direction, entry_ts, entry_price, horizon, "
        "real, fwd_ret, sl_pct FROM signal_outcomes "
        "WHERE resolved=1 AND real IS NOT NULL AND sl_pct IS NOT NULL AND fwd_ret IS NOT NULL "
        "ORDER BY signal, regime, entry_ts"
    ).fetchall()

    cells, checked, mismatch, fetch_fail = {}, 0, 0, 0
    for sym, sig, reg, direction, ts, entry, horizon, real, fwd, sl in rows:
        # entry_ts sinyal mumunun açılışıdır; ileri mumlar BİR SONRAKİ mumdan başlar.
        # Yine de kayıtlı fwd_ret ile eşleşmeyi doğrulamadan satır kullanılmaz.
        base = None
        for offset in (900_000, 0):
            kl = klines(sym, int(ts) + offset, int(horizon) + 2)
            if not kl or len(kl) < int(horizon):
                continue
            fut_h = [float(k[2]) for k in kl]
            fut_l = [float(k[3]) for k in kl]
            fut_c = [float(k[4]) for k in kl]
            cand = se.outcome_from_bars(int(direction), float(entry), fut_h, fut_l, fut_c,
                                        int(horizon), COST, float(sl))
            if cand is not None and abs(cand[1] - float(fwd)) <= 1e-9:
                base = (cand, fut_h, fut_l, fut_c)
                break
        if base is None:
            fetch_fail += 1
            continue
        checked += 1
        cand, fut_h, fut_l, fut_c = base
        rev = se.outcome_from_bars(-int(direction), float(entry), fut_h, fut_l, fut_c,
                                   int(horizon), COST, float(sl))
        if rev is None:
            continue
        cells.setdefault((sig, reg), []).append((int(real), int(bool(rev[0])),
                                                 float(fwd), float(rev[1]), float(sl)))

    print(f"DB={DB}  satır={len(rows)}  doğrulanan={checked}  fwd_ret uyuşmazlığı={mismatch}  "
          f"veri yok={fetch_fail}")
    print(f"maliyet={COST:.4%}  net_rr={NET_RR}  başabaş={BREAKEVEN:.3f}\n")
    hdr = (f"{'sinyal/rejim':30s} {'n':>4s} {'asıl_kaz':>8s} {'asıl_oran':>9s} "
           f"{'ters_kaz':>8s} {'ters_oran':>9s} {'ters_Wilson95':>16s} "
           f"{'ters_ortR':>9s} {'tersR_boot95':>18s} {'t':>6s} {'p':>7s}")
    print(hdr)
    print("-" * len(hdr))
    summary = []
    for (sig, reg), rs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        if n < 30:
            continue
        ow = sum(r[0] for r in rs)
        rw = sum(r[1] for r in rs)
        lo, hi = wilson(rw, n)
        rev_r = [r[3] / r[4] for r in rs]
        mr = statistics.mean(rev_r)
        blo, bhi = boot_ci(rev_r)
        t, p = t_stat(rev_r)
        print(f"{sig + '/' + reg:30s} {n:4d} {ow:8d} {ow / n:9.3f} {rw:8d} {rw / n:9.3f} "
              f"[{lo:.3f},{hi:.3f}] {mr:+9.3f} [{blo:+.3f},{bhi:+.3f}] {t:6.2f} {p:7.4f}")
        summary.append({"cell": f"{sig}/{reg}", "n": n, "orig": ow / n, "rev": rw / n,
                        "rev_wilson": (lo, hi), "rev_r": mr, "boot": (blo, bhi), "p": p})

    print("\n=== KARAR (p<0.05 ve bootstrap GA 0'ı dışlıyor) ===")
    for s in summary:
        ok = s["p"] < 0.05 and (s["boot"][0] > 0 or s["boot"][1] < 0)
        print(f"  {s['cell']:30s} ters_oran={s['rev']:.3f} tersR={s['rev_r']:+.3f} "
              f"GA=[{s['boot'][0]:+.3f},{s['boot'][1]:+.3f}] p={s['p']:.4f} → "
              f"{'ANLAMLI' if ok else 'anlamlı DEĞİL'}")


if __name__ == "__main__":
    main()
