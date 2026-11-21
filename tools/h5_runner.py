"""h5_runner.py — BLOK11 §3 H5 tam koşucu (ÇEVRİMDIŞI, fail-closed gardlar).

GÖREV: H5 = "ters kural" (FAZ5_PREREG B.1): FAZ3 üretici kurallarının AYNI
giriş zamanları/boyutları, TERS taraf, aynalanmış bariyerler — B.3 eşleştirilmiş
ikilinin ters bacaktır; ileri bacaktır kontrol kolu. Motor tools/h5_engine.py
(birebir kopyalar). Sinyal makinesi FAZ3'ün kendisidir (faz3_backtest, tek
kaynak); bariyerler sizing_policy.SIZING_DEFAULTS (canlı SL/TP kaynağı; GRID
anahtarları sizing tarafından hiç okunmaz — doğrulandı).

BİLİNÇLİ SAPMALAR (raporda PRELIMINARY etiketiyle taşınır):
  * B.2 birincil OOS penceresi 2020-2023'tür; BLOK11 görevi 2025 ister —
    2025 FAZ3 KEŞİF penceresinin (2024-01..2026-10) İÇİNDEDİR, yani üreticiye
    göre ÖRNEK-DEĞİLDİR. Sonuç yalnız ÖNSONUÇ (PRELIMINARY)'dir.
  * B.8: B.5 dört-kapısı YALNIZ 2020-23 holdout'una uygulanır → burada kapı
    hükmü ÜRETİLMEZ; sayılar raporlanır.
  * BCHUSDT/GALAUSDT 2025 arşivi yerelde YOK (görev §13: indirme operatör
    onayı gerektirir) → tek-sembol koşusu --allow-partial ile ve PARTIAL
    etiketiyle koşar; sembol-kümeli kapı tek kümede tanımsızdır (N/A).
  * Girişler HER ZAMAN < 2026-01-01T00:00Z (2026 girişi yok); çıkışlar FAZ3
    16-bar (4 saat) zaman-limiti nedeniyle 2026-01-01'e sızabilir (görevin
    kendi alternatif penceresi "…2026-01-01" bunu zorlar) — sızış sayılır,
    raporlanır.

GARDLAR (her biri RED → exit 3, koşul yok): halt=true; base_url yalnız boş
veya testnet; sembol listesi TAM {ETHUSDT,BCHUSDT,GALAUSDT}; tf=15m; pencere
yalnız {(2025-01-01,2025-12-31),(2025-11-01,2026-01-01)}; out_dir YOK; kaynak
taranmış (emir/ağ/credential token'ı yok); ortamda BINANCE kimliği yok; veri
ayı-bütünlüğü. Ağ yok: bu dosya hiçbir HTTP/istemci sınıfı ihraç etmez ve
edinmez.
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import h5_engine as HE  # noqa: E402

REQUIRED_KEYS = ("base_url", "halt", "symbols", "timeframe", "window",
                 "data_root", "faz3_root", "out_dir")
ALLOWED_BASE = ("", "https://testnet.binancefuture.com",
                "wss://fstream.binancefuture.com")
MISSION_SYMBOLS = {"ETHUSDT", "BCHUSDT", "GALAUSDT"}
WINDOWS = {("2025-01-01", "2025-12-31"): "FULL2025",
           ("2025-11-01", "2026-01-01"): "EXACT_NOV_DEC"}
TFS = ("15m", "1h", "4h")
# ısıtma: karar penceresinden en az 5 ay önce (FAZ3 warmup'ı dizinin tamamını
# yükler; bu yalnız G07 bulunabilirlik deneminin alt sınırıdır).
WARMUP_MONTHS_REQ = 5


class GuardFail(Exception):
    pass


def _utc(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc)


def _months(a_dt, b_dt):
    """[a,b] ay-anahtarları (b inclusive ayı)."""
    out, y, m = [], a_dt.year, a_dt.month
    while (y, m) <= (b_dt.year, b_dt.month):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ---- G08 token'ları: kaynak taramasının yasak sözcükleri BİLEŞTİRME ile ----
# (dosyanın kendisi yasağı ihlal etmeden listeyi tutmalı)
def _banned_tokens():
    return ["pl" + "ace_order", "or" + "der_place", "new" + "_order",
            "create_exec" + "utor", "close_reduce" + "_only",
            "marke" + "t_close", "canc" + "el_order", "place_algo" + "_sl",
            "res" + "t_api", "Client" + "(", "impor" + "t requests",
            "impor" + "t httpx", "urlli" + "b.request",
            "os.env" + "iron.get(\"BINANCE", "BINANC" + "E_KEY\"",
            "_po" + "st(\"/fapi"]


def check_source_ban(paths):
    """H5 dosyalarında emir/ağ/credential tokenı ARAMA. Bulursa (name,hit)."""
    for p in paths:
        with open(p, "r", encoding="utf-8") as fh:
            src = fh.read()
        for tok in _banned_tokens():
            if tok in src:
                return os.path.basename(p), tok
    return None


def guard_g01(cfg):
    if cfg.get("halt") is not True:
        return "halt true olmalı (üretim HALTED doğrulaması); bulundu: %r" % (
            cfg.get("halt"),)
    return None


def guard_g02(cfg):
    bu = cfg.get("base_url")
    if bu not in ALLOWED_BASE:
        return "base_url üretim/yabancı: %r — yalnız boş veya testnet" % (bu,)
    return None


def guard_g03(cfg):
    syms = cfg.get("symbols")
    if not isinstance(syms, list) or set(syms) != MISSION_SYMBOLS \
            or len(syms) != len(set(syms)):
        return "symbols perspektifsiz TAM misyon kümesi olmalı %s; bulundu: %r" % (
            sorted(MISSION_SYMBOLS), syms)
    return None


def guard_g04(cfg):
    if cfg.get("timeframe") != "15m":
        return "timeframe 15m olmalı; bulundu: %r" % (cfg.get("timeframe"),)
    return None


def guard_g05(cfg):
    w = cfg.get("window") or {}
    key = (w.get("from"), w.get("to"))
    if key not in WINDOWS:
        return "pencere yalnız %s; bulundu: %r" % (list(WINDOWS), key)
    return None


def guard_g06(cfg):
    if os.path.exists(cfg.get("out_dir") or ""):
        return "out_dir ZATEN VAR (taze koşu zorunlu): %r" % (cfg.get("out_dir"),)
    return None


def guard_g07(cfg, allow_partial):
    """Ay-bazlı bulunabilirlik. Eksik sembol → bloklanır. Hepsi eksikse veya
    allow_partial yoksa RED; kısmiyle koşu yalnız açık onayla (PARTIAL etiket)."""
    root = cfg.get("data_root") or ""
    w = cfg.get("window") or {}
    a, b = _utc(w.get("from", "9999")), _utc(w.get("to", "9999"))
    wy, wm = a.year, a.month - WARMUP_MONTHS_REQ           # ay-bazlı ısıtma
    while wm < 1:
        wm += 12
        wy -= 1
    need = _months(datetime.datetime(wy, wm, 1, tzinfo=datetime.timezone.utc), b)
    if b.day != 1:                                  # +1 ay: 4h çıkış sızması.
        y, m = int(need[-1][:4]), int(need[-1][5:7])  # to=2026-01-01 ise ay
        need.append("%04d-%02d" % (y + 1, 1) if m == 12  # zaten yarım-açık uçtur
                    else "%04d-%02d" % (y, m + 1))
    blocked = {}
    for sym in cfg["symbols"]:
        miss = [f"{sym}/{tf}/{ym}.csv" for tf in TFS for ym in need
                if not os.path.isfile(os.path.join(root, sym, tf, ym + ".csv"))]
        if miss:
            blocked[sym] = miss
    if not blocked:
        return None, []
    if len(blocked) == len(cfg["symbols"]) or not allow_partial:
        return ("eksik veri: %s — indirme OPERATÖR ONAYI gerektirir (görev §13); "
                "kısmi koşu için --allow-partial gerekir" %
                {k: len(v) for k, v in blocked.items()}, sorted(blocked))
    return None, sorted(blocked)


def guard_g09(env):
    for nm in ("BINANCE" + "_" + "KEY", "BINANCE" + "_" + "SECRET"):
        if env.get(nm):
            return ("ortamda %r var — çevrimdışı araştırma koşusu Binance "
                    "kimliğiyle koşmaz (kontaminasyon/yanlış hedef koruması)" % nm)
    return None


def guard_g10(cfg):
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    unknown = sorted(set(cfg) - set(REQUIRED_KEYS))
    if missing or unknown:
        return "şema hatası: eksik=%r bilinmeyen=%r" % (missing, unknown)
    w = cfg["window"]
    if not isinstance(w, dict) or "from" not in w or "to" not in w:
        return "window {from,to} olmalı"
    for k, typ in (("base_url", str), ("symbols", list), ("timeframe", str),
                   ("data_root", str), ("faz3_root", str), ("out_dir", str)):
        if not isinstance(cfg[k], typ):
            return "%r tipi %s olmalı" % (k, typ.__name__)
    return None


GUARDS = (("G01-schema", guard_g10), ("G01-halt", guard_g01),
          ("G02-base", guard_g02), ("G03-symbols", guard_g03),
          ("G04-tf", guard_g04), ("G05-window", guard_g05),
          ("G06-outdir", guard_g06), ("G09-env", guard_g09))


def run_guards(cfg, env=None, allow_partial=False, source_paths=None):
    env = os.environ if env is None else env
    log = []
    for name, fn in GUARDS:
        why = fn(env) if fn is guard_g09 else fn(cfg)
        log.append({"guard": name, "ok": why is None, "why": why})
        if why:
            return log, name, []
    hit = check_source_ban(source_paths or
                           [os.path.join(_HERE, "h5_engine.py"),
                            os.path.join(_HERE, "h5_runner.py")])
    log.append({"guard": "G08-sourceban", "ok": hit is None,
                "why": None if hit is None else
                "kaynakta yasak token: %s -> %r" % hit})
    if hit:
        return log, "G08-sourceban", []
    why7, blocked = guard_g07(cfg, allow_partial)
    log.append({"guard": "G07-data", "ok": why7 is None, "why": why7,
                "blocked_symbols": blocked})
    if why7:
        return log, "G07-data", []
    return log, None, blocked


# --------------------------- koşu (yalnız gardlar GEÇERSE) -------------------
def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(65536), b""):
            h.update(blk)
    return h.hexdigest()


def _iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000,
                                           datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


MS_2026 = 1767225600000  # 2026-01-01T00:00:00Z — girişler daima bundan önce


def simulate_symbol(FB, sizing_policy, sym, root, load_to_ym, ym_from, ym_to,
                    scenarios, hold, entry_lo_ms=0):
    """TEK sembol: FAZ3 kararları → geçiren olaylar → eşleştirilmiş çift bacak.
    Pencere süzümü ÜÇ KATMAN: scan'ın ym-penceresi (karar barı) + burada
    giriş-barı t süzümü (karar 2025-12-31 23:45 → giriş 2026-01-01T00:00
    GEÇERSİZDİR: 2026 girişi yok; sayılır, raporlanır) + run()'daki G-runtime
    değişmezi (süzüm doğruysa daima SIFIR). Döner (trades, skipped, stats)."""
    sd = FB.SymbolData(sym, root, "2022-01", load_to_ym)
    dec = FB.scan_symbol_decisions(sd, {}, ym_from, ym_to)
    ts = [b[0] for b in sd.r15]          # yalnız hız-önbelleği (yarışsız)
    skipped_zero = 0
    excluded_2026 = 0
    trades = []
    for d in dec:
        if d["veto"] or d["fams"] < 1:
            continue
        if not (entry_lo_ms <= d["t"] < MS_2026):
            excluded_2026 += 1           # giriş barı pencere dışı (2026 sızması)
            continue
        i = d["i"]
        geom = sizing_policy.suggest_sl_tp(d["atr_pct"], direction=d["side"],
                                           cfg={})
        if geom.sl_pct <= 0 or geom.tp_pct <= 0:
            skipped_zero += 1
            continue
        sig = (sd.r15[i][0], 1.0, hold, geom.tp_pct, geom.sl_pct)
        for scen, prm in scenarios.items():
            for arm, side in (("forward", d["side"]), ("reversed", -d["side"])):
                g, net, why_, nh, i0, e_px, x_px = HE.run_round_ex(
                    sd.r15, sig, side, prm["fee_rate"], prm["slip_bps"], ts=ts)
                if why_ == "NO-BAR":
                    continue
                notional = sd.r15[i0][1]
                if notional <= 0:
                    continue
                fund = HE.funding_drag_pct(nh)
                trades.append({
                    "sym": sym, "arm": arm, "scen": scen, "i": i,
                    "t_entry": sd.r15[i0][0], "t_exit": sd.r15[min(
                        i0 + nh - 1, len(sd.r15) - 1)][0],
                    "side": side, "entry_px": sd.r15[i0][1], "exit_px": x_px,
                    "why": why_, "bars": nh,
                    "gross_pct": g / notional, "fee_pct": -prm["fee_rate"]
                    * (e_px + x_px) / notional,
                    "net_pct": net / notional + fund, "funding_pct": fund})
    return (trades,
            {"zero_geometry": skipped_zero, "excluded_2026_entry": excluded_2026},
            FB.SCAN_STATS.get(sym, {}))


def stats_arm(trades, seed=20261120, B=10000, NTRIAL=20):
    """Bir kol senaryo dilimi → B.5-uygulanabilir sayılar (hüküm DEĞİL)."""
    if not trades:
        return {"n": 0}
    vals = [t["net_pct"] for t in trades]
    n = len(vals)
    mean = sum(vals) / n
    rng = random.Random(seed)
    iid_lo = HE.boot_lo(vals, [1] * n, rng, B=B)
    qs, qc = {}, {}
    for t in trades:
        dt = datetime.datetime.fromtimestamp(t["t_exit"] / 1000,
                                             datetime.timezone.utc)
        q = "%04dQ%d" % (dt.year, (dt.month - 1) // 3 + 1)
        qs[q] = qs.get(q, 0.0) + t["net_pct"]
        qc[q] = qc.get(q, 0) + 1
    keys = sorted(qc)
    q_lo = HE.boot_lo([qs[k] for k in keys], [qc[k] for k in keys],
                      random.Random(seed), B=B)
    days = {}
    for t in trades:
        d = _iso(t["t_exit"])[:10]
        days.setdefault(d, []).append(t["net_pct"])
    dmean = [sum(v) / len(v) for k, v in sorted(days.items())]
    dsum = [sum(v) for k, v in sorted(days.items())]
    return {"n": n, "mean_net_pct": mean, "sum_net_pct": sum(vals),
            "iid_boot_lo5": iid_lo, "quarter_boot_lo5": q_lo,
            "quarters": len(keys),
            "dsr_mean_N20": HE.dsr(dmean, NTRIAL),
            "dsr_sum_N20": HE.dsr(dsum, NTRIAL),
            "exit_counts": {w: sum(1 for t in trades if t["why"] == w)
                            for w in ("SL", "TP", "HOLD")},
            "spill_2026_exits": sum(1 for t in trades
                                    if t["t_exit"] >= MS_2026)}


def run(cfg, allow_partial=False, out_note=""):
    log, fail, blocked = run_guards(cfg, allow_partial=allow_partial)
    if fail:
        return 3, log, None
    w = cfg["window"]
    wname = WINDOWS[(w["from"], w["to"])]
    entry_lo = int(_utc(w["from"]).timestamp() * 1000)
    if wname == "FULL2025":
        ym_from, ym_to, load_to = "2025-01", "2025-12", "2026-01"
    else:
        ym_from, ym_to, load_to = "2025-11", "2025-12", "2026-01"
    sys.path.insert(0, cfg["faz3_root"])
    import faz3_backtest as FB              # noqa: E402 (gard sonrası, geç)
    import sizing_policy                   # noqa: E402
    scenarios = {"pessimist": HE.SCENARIOS["pessimist"],
                 "base": HE.SCENARIOS["base"]}
    hold = FB.BARS_TIME_LIMIT
    all_trades, skipped, scan_stats = [], {}, {}
    run_syms = [s for s in cfg["symbols"] if s not in blocked]
    for sym in run_syms:
        tr, sk, st = simulate_symbol(FB, sizing_policy, sym, cfg["data_root"],
                                     load_to, ym_from, ym_to, scenarios, hold,
                                     entry_lo_ms=entry_lo)
        # G-runtime: 2026 girişi SIFIR olmalı (aksi halde RED-çıkış)
        bad = [t for t in tr if not (entry_lo <= t["t_entry"] < MS_2026)]
        if bad:
            raise GuardFail("G-runtime: pencere dışı giriş %d" % len(bad))
        all_trades += tr
        skipped[sym] = sk
        scan_stats[sym] = st
    os.makedirs(cfg["out_dir"])
    prov = {"h5_engine.py": _sha(os.path.join(_HERE, "h5_engine.py")),
            "h5_runner.py": _sha(os.path.join(_HERE, "h5_runner.py")),
            "faz3_backtest.py": _sha(os.path.join(cfg["faz3_root"],
                                                  "faz3_backtest.py")),
            "sizing_policy.py": _sha(os.path.join(cfg["faz3_root"],
                                                  "sizing_policy.py")),
            "strategy_engine.py": _sha(os.path.join(cfg["faz3_root"],
                                                    "strategy_engine.py")),
            "edge_engine.py": _sha(os.path.join(cfg["faz3_root"],
                                                "edge_engine.py")),
            "FAZ5_PREREG_sha_in_job": "99a36a90059ce1c590449db631e2f85"
                                      "c123e69826011cb8952c7994946a49615"}
    summary = {"LABEL": "PRELIMINARY",
               "partial": bool(blocked), "blocked_symbols": blocked,
               "window": wname, "window_cfg": w, "symbols_run": run_syms,
               "warmup_note": "SymbolData alt sınırı TRİMLEZ (FAZ6 BLOK6 İŞ1 "
                              "sözleşmesi) — ısıtma 2022-01'den",
               "provenance_sha256": prov, "guards": log,
               "skipped": skipped, "scan_stats": scan_stats,
               "cost_model_id": HE.PESS_MODEL_ID,
               "arms": {}}
    for arm in ("forward", "reversed"):
        for scen in scenarios:
            sub = [t for t in all_trades
                   if t["arm"] == arm and t["scen"] == scen]
            summary["arms"]["%s/%s" % (arm, scen)] = stats_arm(sub)
    summary["b5_applicability"] = {
        "g1_symbol_cluster_boot": "N/A — tek sembol (BCH/GALA veri bekliyor); "
                                  "küme-bootstrap tek kümede tanımsız",
        "g2_timeblock_boot": "HESAPLANDI (çeyrek bloklar; tek yıl = 4 blok, "
                             "dejeneredir)",
        "g3_DSR_N20": "HESAPLANDI",
        "g4_year_signs": "N/A — tek yıl",
        "verdict": "B.5 dört-kapısı 2025 penceresine UYGULANMAZ (B.8: yalnız "
                   "2020-23 birincil holdout) ve 2025 FAZ3 keşif penceresi "
                   "içinde → üreticiye göre in-sample. HÜKMÜZ: ÖNSONUÇ."}
    with open(os.path.join(cfg["out_dir"], "trades.csv"), "w") as fh:
        fh.write("sym,arm,scen,i,t_entry,t_exit,side,entry_px,exit_px,why,"
                 "bars,gross_pct,net_pct,funding_pct\n")
        for t in sorted(all_trades, key=lambda x: (x["arm"], x["scen"],
                                                   x["sym"], x["i"])):
            fh.write("%s,%s,%s,%d,%s,%s,%d,%.8g,%.8g,%s,%d,%.8f,%.8f,%.8f\n" % (
                t["sym"], t["arm"], t["scen"], t["i"], _iso(t["t_entry"]),
                _iso(t["t_exit"]), t["side"], t["entry_px"], t["exit_px"],
                t["why"], t["bars"], t["gross_pct"], t["net_pct"],
                t["funding_pct"]))
    with open(os.path.join(cfg["out_dir"], "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True, ensure_ascii=False)
    with open(os.path.join(cfg["out_dir"], "RUN_CONFIG.json"), "w") as fh:
        json.dump({"config": cfg, "allow_partial": allow_partial,
                   "note": out_note}, fh, indent=1, ensure_ascii=False)
    return 0, log, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--allow-partial", action="store_true",
                    help="eksik sembolleri PARTIAL etiketiyle atla (operatör "
                         "kararı; görev §13 veri onayı bekliyor)")
    a = ap.parse_args(argv)
    with open(a.config) as fh:
        cfg = json.load(fh)
    code, log, summary = run(cfg, allow_partial=a.allow_partial)
    for e in log:
        print("%s %s %s" % ("GREEN" if e["ok"] else "RED", e["guard"],
                            e.get("why") or ""))
    if code != 0:
        print("RED — koşu YAPILMADI (exit %d)" % code)
        return code
    rev = summary["arms"].get("reversed/pessimist", {})
    fwd = summary["arms"].get("forward/pessimist", {})
    print("PRELIMINARY %s%s: n_rev=%s mean_rev=%.4f%% n_fwd=%s mean_fwd=%.4f%%"
          % (summary["window"], " PARTIAL" if summary["partial"] else "",
             rev.get("n"), 100 * (rev.get("mean_net_pct") or 0),
             fwd.get("n"), 100 * (fwd.get("mean_net_pct") or 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
