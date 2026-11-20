#!/usr/bin/env python3
"""BLOK 4 İŞ 1 — PARİTE: harness sinyal akışı × 60 canlı giriş (2026-11-08→11-18 UTC).

Soru: FAZ3'ün "sinyal rastgeleden kötü" hükmünü ürettiği harness, CANLI botun
temsilcisi mi? İki varyant koşulur:
  V1 "yayınlandığı gibi"  : FAZ3'ün ölçtüğü hâli — p_fake nötr (0.5,False),
                            policy=YOK, D-4 sıfır-ATR barı atlanır.
  V2 "canlı-eşdeğeri"     : @108002c karar kodu + canlı env gerçeği —
                            REVERSE_POLICY (REVERSE_POLICY=on varsayılan),
                            as-of p_fake (SIGNAL_CALIBRATION_DB = compare DB,
                            min_events=100), aynı D-4 atlaması.
Eşleştirme: sembol + |Δt|≤15dk + yön. recall = canlı girişlerin harness'te
karşılığı; precision = harness allow yoğunluğu (canlı girişler ek risk/marfj/
tetik kapılarından geçer; precision'ın düşük olması BEKLENİR, sınıflandırılır).
Kanıt etiketi: harness=BACKTEST kararı × FILL girişi; birleştirme YOK —
yalnız ZAMAN/SEMBOL/YON eşleşmesi raporlanır.
"""
import bisect
import datetime as dt
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/reisfurkan/faz9-faz3")
sys.path.insert(0, "/home/reisfurkan/faz9-faz2")
import strategy_engine as se          # == üretim @108002c (diff ile doğrulandı)
import exchange_ledger as EL
import faz3b_universe_recheck as U
from faz3_backtest import SymbolData, _cd

AUD = "/home/reisfurkan/faz9-audit/"
KL = AUD + "raw/klines"
FILLS = AUD + "raw/streams/trades_fromid_fix_20261119.ndjson"
CALIB_DB = AUD + "raw/streams/calib/sigcal_compare_20261119.db"   # SIGNAL_CALIBRATION_DB kopyası
W0 = dt.datetime(2026, 11, 8, 0, 0, tzinfo=dt.UTC).timestamp() * 1000
W1 = dt.datetime(2026, 11, 18, 23, 59, tzinfo=dt.UTC).timestamp() * 1000
TOL = 15 * 60 * 1000
ME = se.CONFIRM_DEFAULTS["confirm_min_events"]


class AsOfStore:
    """compare-DB satırlarıyla anlık p_fake: görünürlük = recorded_at≤t ve
    resolved_at≤t (canlının o an DB'de gördüğü durumun yaklaşık yeniden-inşası;
    saptama D-2). n<min_events → nötr (0.5,False) — canlının kendi yolu."""

    def __init__(self, db):
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        self.cells = defaultdict(list)
        for r in c.execute("SELECT signal,regime,real,resolved_at,recorded_at,sl_pct"
                           " FROM signal_outcomes WHERE resolved=1"):
            if r["sl_pct"] is None or r["real"] is None:
                continue
            res = r["resolved_at"]
            if res is not None:   # DB'de ISO-Z text; recorded_at epoch-real
                res = dt.datetime.fromisoformat(res.replace("Z", "+00:00")).timestamp()
            self.cells[(r["signal"], r["regime"])].append(
                (r["recorded_at"], res, 1 if r["real"] else 0))
        for k in self.cells:
            self.cells[k].sort()
        self.now = None
        self.miss = 0   # p_fake sorgulanan ama DB'de hücresi hiç olmayan sinyal

    def set_now(self, t_s):
        self.now = t_s

    def p_fake(self, name, regime, cfg=None):
        rows = self.cells.get((name, regime))
        if not rows:
            self.miss += 1
            return 0.5, False
        t = self.now
        n = w = 0
        for rec, res, real in rows:
            if rec > t:
                break
            if res is not None and res <= t:
                n += 1
                w += real
        if n < ME:
            return 0.5, False
        a0 = se.CONFIRM_DEFAULTS["confirm_beta_a0"]
        b0 = se.CONFIRM_DEFAULTS["confirm_beta_b0"]
        return 1.0 - (a0 + w) / (a0 + b0 + n), True


def scan_symbol(sd, store):
    """Bar-başı iki varyantın kararı. DÖNER: list[(bar_kapanis_ms, varyant, side_eff)] allow
    + atlandı/skip sayaçları + bar-bazı kanıt sözlüğü (recall sınıflaması için)."""
    allows = {"V1": [], "V2": []}
    evid = {}     # (sym, bar_close_ms) -> {skip|reason per variant}
    skipped = 0
    r15 = sd.r15
    if len(r15) < 260:
        return allows, skipped, evid
    trend_cache = {}
    for i in range(250, len(r15)):
        t_ms = r15[i][0]
        if t_ms < W0:
            continue
        if t_ms > W1:
            break
        main = _cd(r15, i)
        if main is None or len(main) < 60:
            continue
        n1h = bisect.bisect_right(sd.ct1h, t_ms)
        n4h = bisect.bisect_right(sd.ct4h, t_ms)
        key = (n1h, n4h)
        if key not in trend_cache:
            cd1 = _cd(sd.r1h, n1h - 1) if n1h > 60 else None
            cd4 = _cd(sd.r4h, n4h - 1) if n4h > 60 else None
            t1 = se.trend_reading(cd1, "1h") if cd1 else None
            t4 = se.trend_reading(cd4, "4h") if cd4 else None
            trend_cache.clear()
            trend_cache[key] = (t1, t4)
        t1, t4 = trend_cache[key]
        if t1 is None or t4 is None:
            skipped += 1
            evid[(sd.sym, t_ms)] = {"why": "trend-yetersiz"}
            continue
        try:
            readings = (se.exhaustion_readings(main) + se.structure_readings(main)
                        + se.volume_readings(main))
            atr_arr = se.atr_series(main.h, main.l, main.c, 14)
            atr_pct = (atr_arr[-1] / main.c[-1]) if atr_arr and main.c[-1] else None
            if atr_pct is None or atr_pct <= 0:
                skipped += 1
                evid[(sd.sym, t_ms)] = {"why": "D-4-skip-atr0"}
                continue
            regime_adx = se.adx_last(main.h, main.l, main.c)
        except Exception:
            skipped += 1
            evid[(sd.sym, t_ms)] = {"why": "hata-bari"}
            continue
        regime = se.regime_from_adx(regime_adx, None)
        trends = [t1, t4]
        store.set_now(t_ms / 1000.0)
        info = {"why": "allow-yok", "V1": {}, "V2": {}}
        for side in (1, -1):
            v1 = se.evaluate(side, readings, trends, regime,
                             lambda n, r: (0.5, False), None, regime_adx=regime_adx)
            v2 = se.evaluate(side, readings, trends, regime, store.p_fake, None,
                             regime_adx=regime_adx, policy=se.REVERSE_POLICY)
            for tag, v, acc in (("V1", v1, allows["V1"]), ("V2", v2, allows["V2"])):
                if v.allow and v.effective_side in (1, -1):
                    acc.append((t_ms, v.effective_side))
                    info[tag][v.effective_side] = "allow"
                else:
                    info[tag][side] = (v.reason or "?")[:60]
        evid[(sd.sym, t_ms)] = info
    return allows, skipped, evid


def main():
    fills = EL.load_fill_rows(FILLS)
    rounds = U.walk_rounds_priced(fills)
    syms = sorted({r["symbol"] for r in rounds})
    print(f"P.0 round={len(rounds)} sembol={len(syms)} pencere=11-08→11-18 UTC")
    print(f"P.0 canlı config: CONFIRM_DEFAULTS aynen (env'de CONFIRM_* override YOK), "
          f"STRATEGY_CONFIRMATION=on, REVERSE_POLICY=on(varsayılan), "
          f"p_fake=compare-DB as-of min_events={ME}")
    store = AsOfStore(CALIB_DB)
    all_allows = {"V1": [], "V2": []}
    tot_skip = 0
    evid = {}
    for s in syms:
        sd = SymbolData(s, KL, "2026-11", "2026-11")
        allows, skipped, ev = scan_symbol(sd, store)
        tot_skip += skipped
        for k, v in ev.items():
            evid[k] = v
        for tag in ("V1", "V2"):
            for t_ms, sd_ in allows[tag]:
                all_allows[tag].append((s, t_ms, sd_))
        print(f"  {s:12s} V1-allow={len(all_allows['V1']) and len(allows['V1']):4d} "
              f"V2-allow={len(allows['V2']):4d} skip={skipped:3d}")

    print("\nP.1 HARNESS ALLOW YOĞUNLUĞU (28 sembol, 11 gün):")
    for tag in ("V1", "V2"):
        L = all_allows[tag]
        print(f"  {tag}: {len(L)} allow olayı vs 60 canlı giriş → oran {len(L)/60:.1f}×")

    print("\nP.2 RECALL — canlı 60 girişin harness karşılığı (±15dk, yön dahil):")
    idx = {tag: defaultdict(list) for tag in ("V1", "V2")}
    for tag in ("V1", "V2"):
        for s, t, d in all_allows[tag]:
            idx[tag][s].append((t, d))
    stats = {tag: {"hit": 0, "wrong_dir": 0, "miss": 0} for tag in ("V1", "V2")}
    miss_why = defaultdict(int)
    miss_detail, wrong_detail = [], []
    fmt = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%m-%d %H:%M")
    for r in sorted(rounds, key=lambda r: r["open_ts"]):
        # walk_rounds_priced open_ts=EPOCH-SANİYE; kline ts=MİLİSANİYE — normalize.
        r["open_ms"] = r["open_ts"] * 1000 if r["open_ts"] < 1e12 else r["open_ts"]
        d_live = 1 if r["direction"] == "LONG" else -1
        near_t, near_ev = None, None
        for k, v in evid.items():
            if k[0] != r["symbol"]:
                continue
            if abs(k[1] - r["open_ms"]) <= max(TOL, 16 * 60 * 1000):
                if near_t is None or abs(k[1] - r["open_ms"]) < abs(near_t - r["open_ms"]):
                    near_t, near_ev = k[1], v
        for tag in ("V1", "V2"):
            cands = [x for x in idx[tag][r["symbol"]] if abs(x[0] - r["open_ms"]) <= TOL]
            if any(d == d_live for _, d in cands):
                stats[tag]["hit"] += 1
            elif cands:
                stats[tag]["wrong_dir"] += 1
                if tag == "V2":
                    wrong_detail.append((r["symbol"], fmt(r["open_ms"]),
                                         d_live, sorted({d for _, d in cands})))
            else:
                stats[tag]["miss"] += 1
                if tag == "V2":
                    if near_ev is None:
                        miss_why["taranmis-bar-yok"] += 1
                        miss_detail.append((r["symbol"], fmt(r["open_ms"]), d_live,
                                            "taranmış bar yok"))
                    else:
                        rs = (near_ev.get("V2") or {}).get(d_live)
                        full = rs if rs and rs != "allow" else near_ev.get("why", "?")
                        miss_why[full.split(" ")[0]] += 1
                        miss_detail.append((r["symbol"], fmt(r["open_ms"]), d_live, full))
    for tag in ("V1", "V2"):
        s_ = stats[tag]
        print(f"  {tag}: isabet {s_['hit']}/60 ({100*s_['hit']/60:.0f}%) | "
              f"zaman-var-yön-farklı {s_['wrong_dir']} | hiç-yok {s_['miss']}")
    print("  V2 kaçağı en-yakın-bar sınıflaması:", dict(miss_why),
          f"(skip-edilen bar toplamı pencerede: {tot_skip})")
    for sym, t, d, why in miss_detail:
        print(f"    KAÇIRDI {sym:10s} {t} dir={d:+d} — {why}")
    for sym, t, d, sides in wrong_detail:
        print(f"    YÖN-FARK {sym:10s} {t} canlı={d:+d} harness-allow={sides}")

    nf = defaultdict(int)
    for r in rounds:
        nf[min(r["n_fills"], 9)] += 1
    print("\nP.3 ÇOK-FILL (scale-in) — canlı tur başına fill sayısı:",
          dict(sorted(nf.items())),
          f"→ n_fills>2: {sum(1 for r in rounds if r['n_fills'] > 2)}/60 tur.")
    print("   Harness: KARAR başına TEK giriş (i+1 açılışı); scale-in MODELSİZ (kod: "
          "faz3_backtest.simulate_trades).")
    print(f"\nP.not: as-of p_fake hücre-eksik-sorgu={store.miss}; DB penceresi "
          "11-13 11:59→ (öncesi canlıda da nötr — D-1 etiketi).")


if __name__ == "__main__":
    main()
