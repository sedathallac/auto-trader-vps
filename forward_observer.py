"""forward_observer.py — BLOK26 Bölüm 2: 90 günlük forward gözlem sistemi.

ÇALIŞAN PAPER BOT'A TAMAMEN SALT-OKUNUR. Yazdığı tek yer kendi
append-only gözlem dizinidir (~/futures-research/blok25/forward-monitor/).

Kurallar (görev gereği):
  - paper ledger'a YAZMAZ (SQLite `mode=ro` URI ile açılır)
  - emir üretmez, credential kullanmaz, pozisyon açmaz
  - H1 parametresine dokunmaz
  - gözlenen süreçlere SİNYAL GÖNDERMEZ (kill/restart yok)
  - eski kayıtların üzerine yazmaz: her gözlem bugünün JSONL dosyasına
    APPEND edilir; her kayıt zincirleme SHA256 ile imzalanır
    (kaynak: seq + ts + prev_hash + veri; sonradan değişen tek bir
    kayıt zincir doğrulamasında kırılır)

Kayıt düzeni:
  forward-monitor/daily/YYYY-MM-DD.json        # JSONL, append-only
  forward-monitor/daily/YYYY-MM-DD.manifest.json  # günün özeti (yalnız
      bugünkü dosya güncellenir; geçmiş günlerin manifesti yeniden yazılmaz)
"""
import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RESULTS = Path("~/futures-research/blok25/results").expanduser()
DEFAULT_MONITOR = Path("~/futures-research/blok25/forward-monitor").expanduser()
DEFAULT_LOGDIR = Path("~/futures-research/blok26/logs").expanduser()
EQUITY_START = 1000.0
MARK_PRICE_SOURCE = "close_proxy_4h"   # Bölüm 4'e kadar bilinen gerçek


# --------------------------------------------------------------------------
def _proc_info(pid):
    """Salt-okunur /proc bilgisi; süreç yoksa None."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode().strip()
        cwd = os.readlink(f"/proc/{pid}/cwd")
        stat = open(f"/proc/{pid}/stat").read()
        stat = stat[stat.rindex(")") + 2:].split()
        rss_kb = int(stat[21])
        starttime_ticks = int(stat[19])
        return {"pid": pid, "cmdline": cmdline, "cwd": cwd, "rss_mb":
                round(rss_kb / 1024, 1),
                "uptime_s": round(time.time() - _btime() -
                                  starttime_ticks / _hz(), 0)}
    except (OSError, ValueError, IndexError):
        return None


def _btime():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])


def _hz():
    return os.sysconf("SC_CLK_TCK")


def find_processes():
    """Supervisor/runner PID'lerini pgrep ile bul (sinyal YOK, yalnız oku)."""
    out = {"supervisor": None, "runner": None}
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmd = (pid_dir / "cmdline").read_bytes().replace(
                b"\0", b" ").decode()
        except OSError:
            continue
        if "paper_runner.py" in cmd and "python" in cmd:
            out["runner"] = _proc_info(int(pid_dir.name))
        elif "paper_supervisor.py" in cmd and "python" in cmd:
            out["supervisor"] = _proc_info(int(pid_dir.name))
    return out


# --------------------------------------------------------------------------
def _ro_ledger(db_file):
    """Ledger'i kesinlikle salt-okunur aç (mode=ro URI)."""
    return sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)


def ledger_snapshot(db_file, now_ms):
    """Durum/recon/market-yaşı/metrikler — hepsi salt-okunur."""
    snap = {}
    try:
        db = _ro_ledger(db_file)
        db.row_factory = sqlite3.Row
        acc = db.execute("SELECT cash, peak_equity, halted, halt_reason "
                         "FROM account WHERE id=1").fetchone()
        if acc:
            snap["account"] = dict(acc)
            snap["account"]["equity_proxy"] = acc["cash"]
        # pozisyon uPnL'i mark'larla (salt-okunur) hesapla
        pos = []
        for r in db.execute("SELECT symbol, direction, units, entry_px "
                            "FROM positions WHERE direction != 0"):
            d = dict(r)
            m = db.execute("SELECT mark_px FROM marks WHERE symbol=? "
                           "ORDER BY ts DESC LIMIT 1",
                           (d["symbol"],)).fetchone()
            mark = m["mark_px"] if m else d["entry_px"]
            d["upnl"] = d["direction"] * d["units"] * (mark - d["entry_px"])
            pos.append(d)
        snap["positions_n"] = len(pos)
        snap["unrealized"] = round(sum(p["upnl"] for p in pos), 6)
        # kapalı işlem metrikleri (Bölüm 8)
        row = db.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, "
                         "COALESCE(SUM(commission),0) comm FROM trades"
                         ).fetchone()
        snap["closed_trades"] = row["n"]
        snap["realized_pnl"] = round(row["pnl"], 6)
        snap["commission_paid"] = round(row["comm"], 6)
        row = db.execute("SELECT COUNT(*) n, COALESCE(SUM(cf),0) cf "
                         "FROM funding").fetchone()
        snap["funding_events"] = row["n"]
        snap["funding_pnl"] = round(row["cf"], 6)
        # recon + journal
        last_recon = db.execute("SELECT ts, items FROM recon "
                                "ORDER BY rowid DESC LIMIT 1").fetchone()
        if last_recon:
            items = json.loads(last_recon["items"])
            snap["recon"] = {s: sum(1 for i in items if i["status"] == s)
                             for s in {i["status"] for i in items}}
            snap["recon_ts"] = last_recon["ts"]
        outcomes = {}
        for r in db.execute("SELECT entry FROM journal"):
            o = json.loads(r["entry"]).get("outcome")
            outcomes[o] = outcomes.get(o, 0) + 1
        snap["journal_outcomes"] = outcomes
        # market data yaşı
        ages = {}
        for r in db.execute("SELECT symbol, MAX(ts) t FROM bars "
                            "GROUP BY symbol"):
            ages[r["symbol"]] = round((now_ms - r["t"]) / 3600000, 2)
        snap["market_data_age_h"] = ages
        # son hata
        err = db.execute("SELECT ts, kind, detail FROM events WHERE kind IN "
                         "('CYCLE_ERROR','FETCH_ERROR','CRITICAL','HALT',"
                         "'RATE_LIMIT','STALE_BAR') ORDER BY rowid DESC "
                         "LIMIT 1").fetchone()
        snap["last_error"] = (dict(err) if err else None)
        snap["kill_switch"] = bool(acc and acc["halted"])
        db.close()
    except sqlite3.Error as e:
        snap["ledger_error"] = f"{type(e).__name__}: {e}"
    return snap


def supervisor_stats(results_dir):
    """Supervisor logundan restart/KILL/backup sayıları (salt-okunur)."""
    stats = {"restarts": 0, "kills": 0, "backups": 0, "last_lines": []}
    log = Path(results_dir) / "paper_supervisor.log"
    try:
        lines = log.read_text().strip().split("\n")
    except OSError:
        return stats
    for line in lines:
        if "runner started" in line:
            stats["restarts"] += 1
        if "KILL" in line:
            stats["kills"] += 1
        if "backup ->" in line:
            stats["backups"] += 1
    stats["last_lines"] = lines[-3:]
    # ilk 'runner started' başlangıçtır: restart = started - 1 (+kill'ler)
    stats["restarts"] = max(0, stats["restarts"] - 1)
    return stats


# --------------------------------------------------------------------------
def observe(results_dir=DEFAULT_RESULTS, now_ms=None, runner_cycle=None):
    """Tek gözlem anlık görüntüsü (yazma YOK — saf veri toplama)."""
    now_ms = now_ms or int(time.time() * 1000)
    hb_path = Path(results_dir) / "paper_heartbeat.json"
    hb = None
    try:
        hb = json.loads(hb_path.read_text())
    except (OSError, ValueError):
        pass
    procs = find_processes()
    sup = supervisor_stats(results_dir)
    import shutil
    du = shutil.disk_usage(str(results_dir))
    db_file = Path(results_dir) / "paper_ledger.db"
    data = {
        "utc": datetime.fromtimestamp(now_ms / 1000,
                                      timezone.utc).isoformat(),
        "processes": procs,
        "runner_heartbeat": hb,
        "heartbeat_age_s": (round((now_ms - hb["ts"]) / 1000, 1)
                            if hb and hb.get("ts") else None),
        "cycle": (hb or {}).get("cycle"),
        "cycle_ok": (hb or {}).get("ok"),
        "restarts": sup["restarts"],
        "kills": sup["kills"],
        "backups": sup["backups"],
        "uptime_s": (procs["runner"] or {}).get("uptime_s"),
        "rss_mb": (procs["runner"] or {}).get("rss_mb"),
        "disk_free_gb": round(du.free / 1e9, 2),
        "db_size_mb": round(db_file.stat().st_size / 1e6, 3)
                      if db_file.exists() else None,
        "mark_price_source": MARK_PRICE_SOURCE,
        "ledger": ledger_snapshot(db_file, now_ms),
        "supervisor_log_tail": sup["last_lines"],
    }
    return data


# --------------------------------------------------------------------------
def _hash(record_core):
    return hashlib.sha256(json.dumps(record_core, sort_keys=True,
                                     default=str).encode()).hexdigest()


def append_record(monitor_dir, data, now_ms=None):
    """Bugünün JSONL dosyasına zincirleme hash'li kayıt APPEND et."""
    now_ms = now_ms or int(time.time() * 1000)
    day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime(
        "%Y-%m-%d")
    daily = Path(monitor_dir) / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    day_file = daily / f"{day}.json"
    # son hash'i oku (bugünün son satırı; yoksa dünden devral — global zincir)
    prev = "GENESIS"
    seq = 0
    if day_file.exists():
        lines = [l for l in day_file.read_text().split("\n") if l.strip()]
        if lines:
            last = json.loads(lines[-1])
            prev, seq = last["hash"], last["seq"]
    core = {"seq": seq + 1, "ts": now_ms, "prev_hash": prev, "data": data}
    rec = dict(core)
    rec["hash"] = _hash(core)
    with open(day_file, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)      # observer çoklu çalışmaya karşı
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    # günün manifesti (yalnız BUGÜNÜNKÜ güncellenir)
    lines = [l for l in day_file.read_text().split("\n") if l.strip()]
    man = {"date": day, "records": len(lines),
           "first_seq": json.loads(lines[0])["seq"],
           "last_seq": json.loads(lines[-1])["seq"],
           "first_hash": json.loads(lines[0])["hash"],
           "last_hash": json.loads(lines[-1])["hash"],
           "chain_sha256": _hash([json.loads(l)["hash"] for l in lines])}
    man_file = daily / f"{day}.manifest.json"
    man_file.write_text(json.dumps(man, indent=1))
    return rec


def verify_chain(monitor_dir):
    """Zincir doğrulaması: bir kayıt sonradan değiştiyse kırılır.

    Tasarım: her gün BAĞIMSIZ zincirdir (GENESIS'ten başlar) ve günün
    manifesti (kayıt sayısı + ilk/son hash + hash'ler hash'i) o günü
    mühürler. Gün devrinde zincir bilinçli olarak yeniden başlar; bir günün
    tamamının silinmesi/eklenmesi manifest uyuşmazlığı olarak yakalanır."""
    daily = Path(monitor_dir) / "daily"
    problems = []
    files = sorted(daily.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
                              ".json"))
    total = 0
    for f in files:
        prev = "GENESIS"
        hashes = []
        for i, line in enumerate(f.read_text().split("\n")):
            if not line.strip():
                continue
            total += 1
            try:
                rec = json.loads(line)
            except ValueError:
                problems.append(f"{f.name} satır {i+1}: bozuk JSON")
                continue
            core = {k: rec[k] for k in ("seq", "ts", "prev_hash", "data")}
            if _hash(core) != rec.get("hash"):
                problems.append(f"{f.name} #{rec.get('seq')}: hash uyuşmaz")
            if rec.get("prev_hash") != prev:
                problems.append(f"{f.name} #{rec.get('seq')}: zincır kopuk")
            prev = rec.get("hash", prev)
            hashes.append(rec.get("hash", "?"))
        # günün manifesti ile karşılaştır
        man_file = f.with_name(f.name.replace(".json", ".manifest.json"))
        if man_file.exists():
            try:
                man = json.loads(man_file.read_text())
            except ValueError:
                problems.append(f"{man_file.name}: bozuk manifest JSON")
                man = None
            if man:
                if man.get("records") != len(hashes):
                    problems.append(f"{man_file.name}: kayıt sayısı uyuşmaz "
                                    f"(manifest {man.get('records')}, "
                                    f"dosya {len(hashes)})")
                if hashes and man.get("last_hash") != hashes[-1]:
                    problems.append(f"{man_file.name}: son hash uyuşmaz")
                if hashes and man.get("first_hash") != hashes[0]:
                    problems.append(f"{man_file.name}: ilk hash uyuşmaz")
        else:
            problems.append(f"{man_file.name}: manifest YOK")
    return {"records": total, "days": len(files), "ok": not problems,
            "problems": problems[:20]}


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="BLOK26 forward observer "
                                            "(salt-okunur, append-only)")
    p.add_argument("--results", default=str(DEFAULT_RESULTS))
    p.add_argument("--monitor", default=str(DEFAULT_MONITOR))
    p.add_argument("--interval", type=float, default=300.0)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.once:
        rec = append_record(args.monitor, observe(args.results))
        print(json.dumps({"seq": rec["seq"], "hash": rec["hash"][:16]},
                         indent=1))
        return
    DEFAULT_LOGDIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            rec = append_record(args.monitor, observe(args.results))
        except Exception as e:
            with open(DEFAULT_LOGDIR / "observer_errors.log", "a") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} "
                        f"{type(e).__name__}: {e}\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
