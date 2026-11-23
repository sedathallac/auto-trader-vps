"""paper_supervisor.py — BLOK25 Bölüm 6: paper runner için supervisor +
watchdog.

Görevleri:
  - runner'ı subprocess olarak başlatır; ölürse yeniden başlatır
    (backoff 30s → 2x → 10 min cap)
  - HEARTBEAT watchdog: runner kalbi atmayı bırakırsa (döngü asılı)
    KILL + restart — sessizce oturamaz
  - RSS watchdog: bellek sınırını aşarsa KILL + restart (OOM koruması)
  - saatlik TUTARLI SQLite yedekliği (sqlite3 backup API — WAL-güvenli),
    son 24 yedek tutulur
  - tüm eylemler döndürülmüş (rotated) log'a yazılır

Supervisor ASLA emir göndermez, pozisyon açmaz, piyasa erişimi YOKTUR —
tüm piyasa erişimi runner'ın içinde ve yalnız PUBLIC endpoint'lerdir.
Runner'ın flock'u bizim altımızda da geçerlidir: aynı results dizinine
ikinci runner başlatılamaz (duplicate worker protection).

Runner'ın İÇİNDE tespit edilen kritik durumlar (disk, clock drift, ölü
feed, manifest ihlali, liquidation) ledger'ı HALT eder; 18 kontrolün
'halted_kill_switch' maddesi yeni pozisyon açan her kararı bloklar
(fail-closed). Supervisor yeniden başlatsa bile HALT kalıcıdır —
yalnız operatör kaldırabilir.
"""
import argparse
import json
import logging
import logging.handlers
import sqlite3
import subprocess
import time
from pathlib import Path

DEFAULTS = {
    "poll_sec": 10,
    "heartbeat_stale_sec": 900,   # 3x 300s interval
    "rss_limit_mb": 512,
    "backoff_start": 30,
    "backoff_max": 600,
    "backup_every_sec": 3600,
    "backup_keep": 24,
}


def setup_logging(results_dir):
    log = logging.getLogger("blok25.supervisor")
    if log.handlers:
        return log
    h = logging.handlers.RotatingFileHandler(
        str(Path(results_dir) / "paper_supervisor.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


def rss_mb(pid):
    """Child process resident memory (Linux /proc; None if gone)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def read_heartbeat(results_dir):
    p = Path(results_dir) / "paper_heartbeat.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def backup_db(db_file, backup_dir, keep=DEFAULTS["backup_keep"]):
    """Consistent hot backup via the sqlite3 backup API (WAL-safe)."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    dst = backup_dir / f"paper_ledger_{stamp}.db"
    src = sqlite3.connect(db_file)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    backups = sorted(backup_dir.glob("paper_ledger_*.db"))
    for old in backups[:-keep] if keep else []:
        old.unlink()
    return dst


def supervise(cmd, results_dir, db_file=None, poll_sec=DEFAULTS["poll_sec"],
              heartbeat_stale_sec=DEFAULTS["heartbeat_stale_sec"],
              rss_limit_mb=DEFAULTS["rss_limit_mb"],
              backoff_start=DEFAULTS["backoff_start"],
              backoff_max=DEFAULTS["backoff_max"],
              max_restarts=None, log=None):
    """Run `cmd` under supervision. Returns the restart count once
    max_restarts is exceeded (None = supervise forever)."""
    log = log or setup_logging(results_dir)
    results_dir = Path(results_dir)
    backoff = backoff_start
    restarts = 0
    last_backup = 0.0
    proc = None
    started_at = None
    log.info("supervisor start: %s", cmd)
    while True:
        if proc is None or proc.poll() is not None:
            if proc is not None:
                restarts += 1
                log.warning("runner EXITED rc=%s — restart #%d in %.0fs",
                            proc.returncode, restarts, backoff)
                if max_restarts is not None and restarts > max_restarts:
                    log.info("max_restarts=%s reached — supervisor exits",
                             max_restarts)
                    return restarts
                time.sleep(backoff)
                backoff = min(backoff * 2, backoff_max)
            proc = subprocess.Popen(cmd)
            started_at = time.time()
            log.info("runner started pid=%d", proc.pid)
            continue
        # ---- child alive: watchdog checks ------------------------------
        now = time.time()
        hb = read_heartbeat(results_dir)
        kill_reason = None
        if hb and hb.get("ts"):
            hb_age = now - hb["ts"] / 1000
            if hb_age > heartbeat_stale_sec:
                kill_reason = (f"STALE HEARTBEAT {hb_age:.0f}s > "
                               f"{heartbeat_stale_sec}s (cycle={hb.get('cycle')})")
        elif now - started_at > heartbeat_stale_sec:
            kill_reason = "NO HEARTBEAT since start"
        if kill_reason is None:
            rss = rss_mb(proc.pid)
            if rss is not None and rss > rss_limit_mb:
                kill_reason = f"RSS {rss:.0f} MB > {rss_limit_mb} MB"
        if kill_reason:
            log.warning("KILL pid=%d: %s", proc.pid, kill_reason)
            proc.kill()
            proc.wait()
            proc = None            # restart path on next poll
            restarts += 1          # a kill+respawn IS a restart
            if max_restarts is not None and restarts > max_restarts:
                log.info("max_restarts=%s reached — supervisor exits",
                         max_restarts)
                return restarts
            continue
        # ---- periodic consistent backup --------------------------------
        if db_file and now - last_backup > DEFAULTS["backup_every_sec"]:
            try:
                dst = backup_db(db_file, results_dir / "backups")
                log.info("backup -> %s", dst.name)
            except Exception as e:
                log.error("backup failed: %s", e)
            last_backup = now
        time.sleep(poll_sec)


def main():
    import sys
    here = str(Path(__file__).resolve().parent)
    p = argparse.ArgumentParser(description="BLOK25 paper supervisor "
                                            "(restart/watchdog/backup; NO orders)")
    p.add_argument("--db", default=Path("~/futures-research/blok25/results/"
                                        "paper_ledger.db").expanduser())
    p.add_argument("--results", default=Path("~/futures-research/blok25/"
                                             "results").expanduser())
    p.add_argument("--interval", type=float, default=300.0)
    p.add_argument("--poll-sec", type=float, default=DEFAULTS["poll_sec"])
    p.add_argument("--heartbeat-stale-sec", type=float,
                   default=DEFAULTS["heartbeat_stale_sec"])
    p.add_argument("--rss-limit-mb", type=float,
                   default=DEFAULTS["rss_limit_mb"])
    args = p.parse_args()
    cmd = [sys.executable, f"{here}/paper_runner.py",
           "--db", str(args.db), "--results", str(args.results),
           "--interval", str(args.interval)]
    supervise(cmd, args.results, db_file=str(args.db),
              poll_sec=args.poll_sec,
              heartbeat_stale_sec=args.heartbeat_stale_sec,
              rss_limit_mb=args.rss_limit_mb)


if __name__ == "__main__":
    main()
