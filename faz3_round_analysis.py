"""faz3_round_analysis.py — BLOK 2 İŞ 1+2 (M-1 + M-3): TUR bazlı normalizasyon
ve sembol-kümeli bootstrap. Yeni VERİ YOK; aynı 220 [FILL].

M-1: "220 fill / 2 = 110 tur" varsayımı YASAK. Turlar fill'lerin
sembol-içi pozisyon-delta yürüyüşüyle eşleştirilmesiyle bulunur (testlenmiş
exchange_ledger.build_close_events semantiği; bu script aynı yürüyüşü
entry/exit NOTIONAL biriktirerek yeniden üretir ve TESTED builder ile
karşılaştırarak kilitler — rp/event sayısı birebir eşleşmek ZORUNDA).
Eşleşemeyen/açık kalan kümeler (open tails) AYRI raporlanır.

Oran tanımları (tur başına):
  entry_notional = kümeyi 0'dan UZAKLAŞTIRAN fill'lerin Σ|qty·price|
  exit_notional  = küme POZİSYONU AZALTAN fill kısımlarının Σ|qty·price|
                   (sıfır-geçişi: frac miktar-oranlı pay, builder ile aynı)
  turnover       = entry + exit
  gross          = ΣrealizedPnl · fee = Σkomisyon (POZİTİF maliyet büyüklüğü)
  funding        = işaretli gelir/gider (ayrı kalır)
  net            = gross − fee + funding
  sermaye-oranı  = net / entry_notional        (ANA tablo)
  turnover-oranı = net / turnover              (ikinci tablo)

M-3 bootstrap: yeniden-örnekleme birimi SEMBOL KÜMESİ (sembol içi bağımlılık
korunur). B sabit, seed sabit, percentil CI, Kish n_eff, date-block duyarlılığı.

KABUK MUTABAKATI: Σ-event net, BLOK1 Σnet −28.26361727 ile ±0.01 olmalı
(open-tail komisyon farkı hariç — raporlanır); uyuşmazlıkta YUVARLAMA YOK.
Çevrimdışı: ağ/anahtar yok, salt-okur ndjson.
"""
import argparse
import json
import sys

import exchange_ledger as EL

BASELINE_NET = -28.26361727      # BLOK1 [FILL] Σnet (tam değer)
TOL = 0.01
BOOT_B = 10000
BOOT_SEED = 20261119


def walk_rounds(fills):
    """build_close_events yürüyüşünün notional-birikirtilmiş hâli.
    Döner: (rounds, open_tails). round = event alanları + entry_notional,
    exit_notional, direction, n_fills, turnover."""
    by_sym = {}
    for raw in fills:
        n = EL.norm_fill(raw)
        by_sym.setdefault(n["symbol"], []).append(n)
    rounds, tails = [], []
    EPS = 1e-9

    def close_round(cur, ts):
        cur["closed_at_epoch"] = ts
        cur["turnover"] = cur["entry_notional"] + cur["exit_notional"]
        cur["net"] = cur["gross"] - cur["fee"] + cur["funding"]
        # EL.assign_funding event şemasını (rp/commission) bekler — alias.
        cur["rp"] = cur["gross"]
        cur["commission"] = cur["fee"]
        rounds.append(cur)

    for sym in sorted(by_sym):
        rows = sorted(by_sym[sym], key=lambda x: (x["ts"], x["id"]))
        pos, cur = 0.0, None
        for n in rows:
            delta = n["qty"] if n["side"] == "BUY" else -n["qty"]
            nt = abs(n["qty"] * n["price"])
            if cur is None:
                if delta == 0:
                    continue
                cur = {"symbol": sym, "open_ts": n["ts"], "gross": 0.0,
                       "fee": n["fee"], "funding": 0.0,
                       "entry_notional": nt, "exit_notional": 0.0,
                       "direction": "LONG" if delta > 0 else "SHORT",
                       "n_fills": 1, "fill_ids": [n["id"]]}
                pos = delta
                continue
            reducing = pos != 0 and (pos > 0) != (delta > 0)
            pos_new = pos + delta
            cur["n_fills"] += 1
            cur["fill_ids"].append(n["id"])
            if reducing and pos_new != 0 and (pos_new > 0) != (pos > 0):
                frac = abs(pos) / abs(delta)          # sıfır-geçişi
                cur["fee"] += n["fee"] * frac
                cur["gross"] += n["rp"]
                cur["exit_notional"] += nt * frac
                close_round(cur, n["ts"])
                cur = {"symbol": sym, "open_ts": n["ts"], "gross": 0.0,
                       "fee": n["fee"] * (1 - frac), "funding": 0.0,
                       "entry_notional": nt * (1 - frac), "exit_notional": 0.0,
                       "direction": "LONG" if pos_new > 0 else "SHORT",
                       "n_fills": 1, "fill_ids": [n["id"]]}
                pos = pos_new
                continue
            cur["fee"] += n["fee"]
            cur["gross"] += n["rp"]
            if reducing:
                cur["exit_notional"] += nt
            else:
                cur["entry_notional"] += nt
            pos = pos_new
            if abs(pos) < EPS:
                pos = 0.0
                close_round(cur, n["ts"])
                cur = None
        if cur is not None:
            tails.append({"symbol": sym, "open_ts": cur["open_ts"],
                          "n_fills": cur["n_fills"],
                          "unclosed_commission": cur["fee"],
                          "open_notional": cur["entry_notional"]})
    return rounds, tails


def cross_check(fills, rounds):
    """Yeni yürüyüş TESTED builder ile birebir uyuşmalı (rp + event sayısı)."""
    ev, _ = EL.build_close_events(fills)
    a = (len(ev), round(sum(e["rp"] for e in ev), 8))
    b = (len(rounds), round(sum(r["gross"] for r in rounds), 8))
    return a == b, a, b


def wls_mean(vals, w):
    sw = sum(w)
    return sum(v * wi for v, wi in zip(vals, w)) / sw if sw else 0.0


def median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def cluster_bootstrap(rounds, b=BOOT_B, seed=BOOT_SEED):
    """Sembol-kümeli bootstrap (M-3): turlar yerinde kalarak semboller
    yeniden örneklenir. Percentil %95 CI, tur-başına EŞİT-AĞIRLIKLI net
    sermaye-oranı için."""
    import random
    by_sym = {}
    for r in rounds:
        by_sym.setdefault(r["symbol"], []).append(
            r["net"] / r["entry_notional"] if r["entry_notional"] else 0.0)
    syms = sorted(by_sym)
    rng = random.Random(seed)
    means = []
    k = len(syms)
    for _ in range(b):
        pooled = []
        for _i in range(k):
            pooled.extend(by_sym[syms[rng.randrange(k)]])
        means.append(sum(pooled) / len(pooled))
    means.sort()
    lo = means[int(0.025 * b)]
    hi = means[min(int(0.975 * b), b - 1)]
    n_eff_kish = (sum(len(v) for v in by_sym.values()) ** 2 /
                  sum(len(v) ** 2 for v in by_sym.values()))
    return lo, hi, n_eff_kish, k


def date_blocks(rounds, n=3):
    """Tarih-blok duyarlılığı: kapanış tarihine göre n blok, eşit-ağırlıklı
    tur ortalaması (zaman bağımlılığı için ucuz duyarlılık testi)."""
    rs = sorted(rounds, key=lambda r: r["closed_at_epoch"])
    out = []
    k = len(rs) // n
    for i in range(n):
        blk = rs[i * k:(i + 1) * k] if i < n - 1 else rs[(n - 1) * k:]
        if blk:
            v = [r["net"] / r["entry_notional"] for r in blk if r["entry_notional"]]
            out.append((i + 1, len(blk), sum(v) / len(v) if v else 0.0,
                        blk[0]["closed_at_epoch"], blk[-1]["closed_at_epoch"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    D = "/home/reisfurkan/faz9-audit/raw/streams/"
    ap.add_argument("--fills", default=D + "trades_fromid_fix_20261119.ndjson")
    ap.add_argument("--income", default=D + "income_fix_20261119.ndjson")
    a = ap.parse_args(argv)

    fills = EL.load_fill_rows(a.fills)
    inc = EL.load_income_rows(a.income)
    rounds, tails = walk_rounds(fills)
    ok, ev_pair, my_pair = cross_check(fills, rounds)
    print(f"== M-1 EŞLEŞTİRME: {len(fills)} fill → {len(rounds)} TAMAMLANDI "
          f"tur, {len(tails)} AÇIK küme (eşleşemedi; ayrı rapor) ==")
    print(f"cross-check build_close_events: {'BİREBİR' if ok else 'UYUŞMAZ'} "
          f"(tested={ev_pair} yeni={my_pair})")
    if not ok:
        print("HATA: yürüyüş testlenmiş builder ile uyuşmuyor — oranlar "
              "raporlanmaz"); return 1
    un = EL.assign_funding(rounds, inc)     # rounds event-şemasındadır
    un_amt = sum(EL._f(r.get("income")) for r in un)
    n_long = sum(1 for r in rounds if r["direction"] == "LONG")
    n_short = len(rounds) - n_long
    tot_en = sum(r["entry_notional"] for r in rounds)
    tot_turn = sum(r["turnover"] for r in rounds)
    G = sum(r["gross"] for r in rounds)
    F = sum(r["fee"] for r in rounds)
    U = sum(r["funding"] for r in rounds)
    N = G - F + U
    tail_com = sum(t["unclosed_commission"] for t in tails)
    tail_fills = sum(t["n_fills"] for t in tails)
    print(f"turlar: {len(rounds)} (LONG {n_long} / SHORT {n_short}) · "
          f"tur-başı giriş-notional {tot_en/len(rounds):.2f} USDT · "
          f"açık kümeler: {len(tails)} küme {tail_fills} fill, "
          f"{tail_com:.4f} USDT komisyon TUR DIŞI")
    print(f"funding atanmadı: {len(un)} satır {un_amt:+.4f} USDT (net-dışı)")

    ratios_cap = [r["net"] / r["entry_notional"] for r in rounds]
    ratios_turn = [r["net"] / r["turnover"] for r in rounds]
    w = [r["entry_notional"] for r in rounds]
    g_r = [r["gross"] / r["entry_notional"] for r in rounds]
    f_r = [r["fee"] / r["entry_notional"] for r in rounds]
    u_r = [r["funding"] / r["entry_notional"] for r in rounds]
    print("\n== TUR-BAŞINA SERMAYE-BAZLI ORANLAR (ana tablo; fee=maliyet,+; "
          "funding=işaretli) ==")
    print(f"{'gösterge':<34}{'gross':>10}{'fee(−)':>10}{'funding':>10}{'net':>10}")
    print(f"{'entry-notional ağırlıklı ort.':<34}"
          f"{wls_mean(g_r, w)*100:>+10.4f}{-wls_mean(f_r, w)*100:>+10.4f}"
          f"{wls_mean(u_r, w)*100:>+10.4f}{wls_mean(ratios_cap, w)*100:>+10.4f}")
    print(f"{'eşit ağırlıklı tur ort.':<34}"
          f"{sum(g_r)/len(g_r)*100:>+10.4f}{-sum(f_r)/len(f_r)*100:>+10.4f}"
          f"{sum(u_r)/len(u_r)*100:>+10.4f}{sum(ratios_cap)/len(ratios_cap)*100:>+10.4f}")
    print(f"{'tur medyanı':<34}"
          f"{median(g_r)*100:>+10.4f}{-median(f_r)*100:>+10.4f}"
          f"{median(u_r)*100:>+10.4f}{median(ratios_cap)*100:>+10.4f}")
    # BLOK3 A.7 kalıcı mutabakat: ağırlıklı satır BİLEŞENLERİ toplam
    # oranlarına birebir oturmak ZORUNDA (wls_mean(x,en) ≡ Σx/Σen).
    # Sapma varsa tablo GEÇERSİZ — exit 1. Σentry de raporlanır.
    print(f"ağırlıklı-satır mutabakatı (Σx/Σentry, Σentry={tot_en:.2f}):")
    bad = 0
    for nm, xs, tot in (("gross", g_r, G), ("fee", f_r, F), ("funding", u_r, U),
                        ("net", ratios_cap, N)):
        a, b = wls_mean(xs, w) * 100, (0.0 if not tot_en else tot / tot_en * 100)
        d = abs(a - b)
        bad += d > 1e-9
        print(f"  {nm:<8} wls={a:+.4f}%  Σ/toplam={b:+.4f}%  Δ={d:.2e} "
              f"{'MUTABIK' if d <= 1e-9 else 'SAPMA — TABLO GEÇERSİZ'}")
    if bad:
        print("HATA: bileşenler toplamlara oturmuyor"); return 1
    print("== turnover-bazlı (ikinci tablo) ==")
    f_t = [r["fee"] / r["turnover"] for r in rounds]
    u_t = [r["funding"] / r["turnover"] for r in rounds]
    g_t = [r["gross"] / r["turnover"] for r in rounds]
    w2 = [r["turnover"] for r in rounds]
    print(f"{'turnover ağırlıklı ort.':<34}"
          f"{wls_mean(g_t, w2)*100:>+10.4f}{-wls_mean(f_t, w2)*100:>+10.4f}"
          f"{wls_mean(u_t, w2)*100:>+10.4f}{wls_mean(ratios_turn, w2)*100:>+10.4f}")
    print(f"{'eşit ağırlıklı / medyan net':<34}"
          f"{'':>10}{'':>10}{'':>10}{sum(ratios_turn)/len(ratios_turn)*100:>+10.4f}"
          f" / {median(ratios_turn)*100:+.4f}")

    print(f"\nUSDT: gross={G:+.4f} fee={-F:.4f} funding={U:+.4f} net={N:+.4f}")
    dev = abs(N - BASELINE_NET)
    print(f"kabuk mutabakatı: |Σnet − {BASELINE_NET}| = {dev:.6f} "
          f"{'OK (≤0.01)' if dev <= TOL else 'SAPMA — neden aranmalı'}"
          f"  [açık-küme kom. {tail_com:.4f} hariç]")
    cost = F - U            # net = gross − (fee − funding) → maliyet bileşeni
    print(f"maliyet/gross katı: (fee − funding)/|gross| = "
          f"{cost/abs(G):.4f}×  · net/gross = {N/G:.4f}×  "
          f"(gross={G:+.4f}; ücret {F:.4f}; funding {U:+.4f})")

    lo, hi, neff, k = cluster_bootstrap(rounds)
    eqm = sum(ratios_cap) / len(ratios_cap)
    print(f"\n== M-3 SEMBOL-KÜMELİ BOOTSTRAP (tur-başı sermaye-bazlı net) ==")
    print(f"benzersiz sembol {k} · tur {len(rounds)} · seed {BOOT_SEED} · "
          f"B {BOOT_B} · nokta(ort) {eqm*100:+.4f}% · %95 CI "
          f"[{lo*100:+.4f}%, {hi*100:+.4f}%] · Kish n_eff {neff:.1f}")
    print(f"CI sıfır içeriyor mu: {'EVET' if lo <= 0 <= hi else 'HAYIR'}")
    print("date-block duyarlılığı (kapanış tarihine göre 3 blok):")
    for i, n, m, t0, t1 in date_blocks(rounds):
        import datetime as dt
        f0 = dt.datetime.fromtimestamp(t0, dt.timezone.utc).strftime("%m-%d")
        f1 = dt.datetime.fromtimestamp(t1, dt.timezone.utc).strftime("%m-%d")
        print(f"  blok{i} [{f0}..{f1}] n={n} ortalama {m*100:+.4f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
