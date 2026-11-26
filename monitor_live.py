#!/usr/bin/env python3
"""Faz 9 günlük izleme raporu (SALT-OKUNUR: DB'ler mode=ro açılır, log yalnız okunur).

Kontroller: whale_metrics / signal_outcomes satır sayıları (24s + toplam), OBI-boş (quality_ok=0) ve spoof oranı,
log'da 429/418, Traceback, Faz3-6[compare] satırları, HB_OFFLINE göstergesi, gerçek emir izi, DB boyutu.
Çıkış kodu: 0=OK, 1=UYARI, 2=DURDUR (plan §8). Kullanım: python monitor_live.py [--hours 24] [--json]
"""
import argparse, json, re, sqlite3, sys, time
from pathlib import Path

HOME = Path.home()
WHALE_DB = HOME / "whale_metrics.db"
SIGNAL_DB = HOME / "signal_outcomes.db"
LOG = HOME / "auto_trader.log"
TARGETS = {"whale_rows": 2000, "signal_resolved": 100}      # plan §5
OK, WARN, STOP = 0, 1, 2
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_RATE = re.compile(r"(?<!\d)(429|418)(?!\d)")
_REAL_ORDER = re.compile(r"create_executor|executor (created|başlatıldı)|emir gönderildi|ORDER_PLACED", re.I)


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def whale_stats(path, since, now):
    if not Path(path).exists():
        return {"exists": False}
    try:
        c = _ro(path)
    except sqlite3.Error as e:
        return {"exists": True, "error": str(e)}
    try:
        total = _q(c, "SELECT COUNT(*) FROM whale_metrics")
        w = "WHERE recorded_at>=?"
        n24 = _q(c, f"SELECT COUNT(*) FROM whale_metrics {w}", (since,))
        q_ok24 = _q(c, f"SELECT COUNT(*) FROM whale_metrics {w} AND quality_ok=1", (since,))
        spoof24 = _q(c, f"SELECT COUNT(*) FROM whale_metrics {w} AND spoof=1", (since,))
        modes = dict(c.execute(f"SELECT mode,COUNT(*) FROM whale_metrics {w} GROUP BY mode", (since,)).fetchall())
        days = _q(c, "SELECT COUNT(DISTINCT date(recorded_at,'unixepoch'))FROM whale_metrics")
        last = c.execute("SELECT MAX(recorded_at) FROM whale_metrics").fetchone()[0]
        return {"exists": True, "total": total, "last_24h": n24, "modes_24h": modes, "distinct_days": days,
                "obi_empty_rate": (1 - q_ok24 / n24) if n24 else None,
                "spoof_rate": (spoof24 / n24) if n24 else None,
                "age_min": ((now - last) / 60) if last else None}
    except sqlite3.Error as e:
        return {"exists": True, "error": str(e)}
    finally:
        c.close()


def signal_stats(path, since):
    if not Path(path).exists():
        return {"exists": False}
    try:
        c = _ro(path)
    except sqlite3.Error as e:
        return {"exists": True, "error": str(e)}
    try:
        live = "source='live'"
        per = c.execute(f"SELECT signal,regime,COUNT(*) FROM signal_outcomes WHERE {live} AND resolved=1 "
                        "GROUP BY signal,regime").fetchall()
        return {"exists": True,
                "total": _q(c, "SELECT COUNT(*) FROM signal_outcomes"),
                "live_total": _q(c, f"SELECT COUNT(*) FROM signal_outcomes WHERE {live}"),
                "live_new_24h": _q(c, f"SELECT COUNT(*) FROM signal_outcomes WHERE {live} AND recorded_at>=?", (since,)),
                "live_resolved": _q(c, f"SELECT COUNT(*) FROM signal_outcomes WHERE {live} AND resolved=1"),
                "live_resolved_per_signal_regime": {f"{s}|{r}": n for s, r, n in per},
                "max_per_signal_regime": max([n for *_, n in per], default=0)}
    except sqlite3.Error as e:
        return {"exists": True, "error": str(e)}
    finally:
        c.close()


def log_stats(path, since):
    out = {"exists": Path(path).exists(), "rate_limit_429_418": 0, "tracebacks": 0, "hb_offline_seen": False,
           "real_order_hits": 0, "faz_compare_lines": {"Faz3": 0, "Faz4": 0, "Faz5": 0, "Faz6": 0}, "lines_24h": 0}
    if not out["exists"]:
        return out
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    in_window = False
    with open(path, errors="replace") as f:
        for line in f:
            m = _TS.match(line)
            if m:
                in_window = m.group(1) >= cutoff
            if "HB_OFFLINE" in line and "🔌" in line:
                out["hb_offline_seen"] = True                  # başlangıç satırı pencere dışında olabilir
            if not in_window:
                continue
            out["lines_24h"] += 1
            if "Traceback (most recent call last)" in line:
                out["tracebacks"] += 1
            if _RATE.search(line) and re.search(r"rate|limit|HTTP|status|error|hata", line, re.I):
                out["rate_limit_429_418"] += 1
            if _REAL_ORDER.search(line) and "dry" not in line.lower() and "atlan" not in line.lower():
                out["real_order_hits"] += 1
            for k in out["faz_compare_lines"]:
                if f"{k}[compare]" in line:
                    out["faz_compare_lines"][k] += 1
    return out


def evaluate(w, s, lg, db_mb):
    stop, warn = [], []
    if lg.get("real_order_hits"):
        stop.append(f"gerçek emir izi şüphesi: {lg['real_order_hits']} satır")
    if lg.get("rate_limit_429_418"):
        stop.append(f"429/418 rate-limit: {lg['rate_limit_429_418']} (tekrar denenmez, süreç durdurulur)")
    if lg.get("tracebacks", 0) >= 10:
        stop.append(f"Traceback döngüsü: {lg['tracebacks']}")
    for name, st in (("whale_metrics", w), ("signal_outcomes", s)):
        if st.get("error"):
            stop.append(f"{name} DB hatası: {st['error']}")
    if lg.get("exists") and not lg["hb_offline_seen"]:
        stop.append("log'da HB_OFFLINE göstergesi yok — dry-run doğrulanamadı")
    if not lg.get("exists"):
        warn.append("auto_trader.log yok")
    if not w.get("exists"):
        warn.append("whale_metrics.db henüz yok")
    elif w.get("last_24h") == 0:
        warn.append("son 24s whale satırı yok (süreç durmuş olabilir)")
    elif w.get("age_min") is not None and w["age_min"] > 120:
        warn.append(f"son whale satırı {w['age_min']:.0f} dk önce")
    if w.get("obi_empty_rate") is not None and w["obi_empty_rate"] > 0.30:
        warn.append(f"OBI-boş oranı yüksek: {w['obi_empty_rate']:.0%}")
    if w.get("spoof_rate") is not None and w["spoof_rate"] > 0.30:
        warn.append(f"spoof oranı yüksek: {w['spoof_rate']:.0%}")
    if lg.get("exists") and lg["lines_24h"] and not lg["faz_compare_lines"]["Faz5"] and not lg["faz_compare_lines"]["Faz6"]:
        warn.append("log'da Faz5/Faz6[compare] satırı yok (bayraklar açık mı?)")
    if lg.get("tracebacks", 0) and lg["tracebacks"] < 10:
        warn.append(f"Traceback: {lg['tracebacks']}")
    if db_mb > 500:
        warn.append(f"DB boyutu {db_mb:.0f} MB")
    return (STOP if stop else WARN if warn else OK), stop, warn


def build_report(hours=24, now=None, whale_db=WHALE_DB, signal_db=SIGNAL_DB, log=LOG):
    now = time.time() if now is None else now
    since = now - hours * 3600
    w, s, lg = whale_stats(whale_db, since, now), signal_stats(signal_db, since), log_stats(log, since)
    db_mb = sum(Path(p).stat().st_size for p in (whale_db, signal_db) if Path(p).exists()) / 1e6
    code, stop, warn = evaluate(w, s, lg, db_mb)
    prog = {"whale_rows": f"{w.get('total', 0)}/{TARGETS['whale_rows']}",
            "signal_max_per_signal_regime": f"{s.get('max_per_signal_regime', 0)}/{TARGETS['signal_resolved']}"}
    return {"status": ["OK", "UYARI", "DURDUR"][code], "exit_code": code, "window_h": hours, "whale": w,
            "signals": s, "log": lg, "db_mb": round(db_mb, 2), "progress": prog, "stop": stop, "warn": warn}


def render(r):
    w, s, lg = r["whale"], r["signals"], r["log"]
    pct = lambda x: "-" if x is None else f"{x:.1%}"
    L = [f"=== Faz 9 izleme — {r['status']} (son {r['window_h']}s) ===",
         f"whale_metrics : toplam={w.get('total', '-')} son24s={w.get('last_24h', '-')} gün={w.get('distinct_days', '-')} "
         f"OBI-boş={pct(w.get('obi_empty_rate'))} spoof={pct(w.get('spoof_rate'))} modlar={w.get('modes_24h', '-')}",
         f"signal_outcomes: toplam={s.get('total', '-')} canlı={s.get('live_total', '-')} yeni24s={s.get('live_new_24h', '-')} "
         f"çözülmüş={s.get('live_resolved', '-')} en-dolu sinyal×rejim={s.get('max_per_signal_regime', '-')}",
         f"log           : satır24s={lg.get('lines_24h')} 429/418={lg.get('rate_limit_429_418')} traceback={lg.get('tracebacks')} "
         f"HB_OFFLINE={lg.get('hb_offline_seen')} gerçek-emir-izi={lg.get('real_order_hits')} compare={lg.get('faz_compare_lines')}",
         f"ilerleme      : {r['progress']}  DB={r['db_mb']} MB"]
    L += [f"DURDUR: {m}" for m in r["stop"]] + [f"UYARI : {m}" for m in r["warn"]]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Faz 9 günlük izleme (salt-okunur)")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = build_report(a.hours)
    print(json.dumps(r, indent=2, default=str) if a.json else render(r))
    return r["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
