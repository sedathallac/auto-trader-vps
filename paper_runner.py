"""paper_runner.py — BLOK25 Bölüm 4: forward paper/shadow runner.

FORWARD-ONLY from the frozen start timestamp: no 2026 holdout access, no OOS
computation, no statistical peeking. Public Binance USDⓈ-M endpoints only
(klines, funding-rate history) — no credentials, no signed endpoints.

NO ORDERS: the executor is PaperBroker, which has no network surface at all
(enforced by tests). The H1 signal layer, the 18-check risk decision and the
reconciliation gate are the Bölüm 2/3 components, unchanged.

Manifest (frozen BEFORE the first live bar, verified on every start):
  test start timestamp, strategy params + param hash, code hash of the
  signal/risk/broker sources, execution model (spread/slippage/impact/
  commission/funding/mark/liquidation) + its hash, acceptance criteria,
  end conditions. Any change stops the runner (fail-closed).

Restart recovery: on every start the engine state is REBUILT by replaying
the lookback window with execute=False (no execution, no dedup consumption);
already-executed bars stay CONSUMED in the persistent SignalDedupStore, so a
restart can never double-execute a decision.
"""
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from h1_adapter import (H1SignalEngine, H1Router, H1Reconciler,
                        STRATEGY_ID, STRATEGY_LABEL, H1_SYMBOLS, param_manifest)
from paper_broker import (PaperBroker, PaperRiskState, FILL_MODEL,
                          fill_model_hash)
from risk_checklist import PretradeChecklist
from signal_dedup import SignalDedupStore

BAR_MS = 4 * 3600 * 1000
WARMUP_BARS = 400          # >= 181 needed for the N=180 momentum window

BASE_URL = "https://fapi.binance.com"

# frozen BEFORE the run starts (Bölüm 4 requirement)
ACCEPTANCE_CRITERIA = {
    "min_duration_days": 90,
    "max_drawdown_halt": 0.20,          # prereg §21e
    "unexplained_recon_drift_allowed": 0,
    "execution_model_breach_allowed": 0,
    "note": "statistical gate evaluated ONLY at the end by the operator "
            "(no peeking); no OOS claim, 2026 holdout stays sealed",
}
END_CONDITIONS = {
    "operator_stop": "explicit operator stop",
    "drawdown_halt": "20% drawdown halts new executions (ledger HALT)",
    "manifest_breach": "any manifest hash change stops the runner",
    "min_duration": "no statistical claim before the minimum duration",
}

# ---- Bölüm 6 resilience thresholds ---------------------------------------
# Fail-closed: her KRİTİK durum HALT eder; 18 kontrolün 'halted_kill_switch'
# maddesi yeni pozisyon açan her kararı bloklar.
DRIFT_WARN_MS = 5_000          # imzalı çağrı recvWindow=60s güvenli aralık
DRIFT_CRITICAL_MS = 60_000     # bar-kapalı hesabı güvenilmez → HALT
DISK_MIN_FREE_MB = 100         # ledger yazılamaz → HALT
RSS_LIMIT_MB = 512             # supervisor bu sınırı aşarsa öldürüp yeniden başlatır
STALE_DATA_MS = 12 * 3600 * 1000   # 3 bar boyunca taze kapalı bar yok → HALT
STALE_BAR_MS = 2 * BAR_MS      # 2 barden eski "yeni" bar → execute=NO (MISSED)
REQUEST_MIN_INTERVAL = 0.25    # public endpoint pacing (client-side rate limit)


class RateLimited(Exception):
    """HTTP 429/418 — Binance rate limit; Retry-After saniye cinsinden."""

    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = float(retry_after)


def _rss_mb():
    """This process's resident memory (Linux /proc; None if unavailable)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def setup_logging(results_dir, name="paper"):
    """Rotated log file (5 MB x 5) — disk dolmasını log'un kendisi
    tetiklemesin."""
    log = logging.getLogger(f"blok25.{name}")
    if log.handlers:
        return log
    h = logging.handlers.RotatingFileHandler(
        str(Path(results_dir) / f"{name}.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes():
    """Hash of every source that determines the paper run's behavior."""
    here = os.path.dirname(os.path.abspath(__file__))
    return {name: file_sha256(os.path.join(here, name))
            for name in ("h1_adapter.py", "risk_checklist.py",
                         "paper_broker.py", "signal_dedup.py",
                         "paper_runner.py")}


def build_manifest(start_ts):
    m = {
        "test_start_ts": start_ts,
        "strategy": param_manifest(),        # includes param_hash
        "label": STRATEGY_LABEL,
        "code_hashes": code_hashes(),
        "execution_model": FILL_MODEL,
        "execution_model_hash": fill_model_hash(),
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "end_conditions": END_CONDITIONS,
        "orders": "NONE — paper only; PaperBroker has no network surface",
    }
    m["manifest_hash"] = hashlib.sha256(
        json.dumps(m, sort_keys=True).encode()).hexdigest()
    return m


class PublicBinanceFetcher:
    """PUBLIC endpoints only (klines + funding-rate history). No credentials,
    no signed endpoints, no order paths."""

    def __init__(self, base_url=BASE_URL, timeout=15,
                 min_interval=REQUEST_MIN_INTERVAL):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval = float(min_interval)
        self._last_req = 0.0

    def _pace(self):
        """Client-side pacing between public requests (rate-limit hygiene)."""
        wait = self._last_req + self.min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()

    def _get(self, path):
        url = self.base + path
        self._pace()
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 418):
                raise RateLimited(
                    float(e.headers.get("Retry-After") or 5.0))
            raise

    def server_time(self):
        """Public /fapi/v1/time — clock-drift ölçümü (credential'sız)."""
        return int(self._get("/fapi/v1/time")["serverTime"])

    def klines(self, symbol, interval="4h", limit=500):
        rows = self._get(f"/fapi/v1/klines?symbol={symbol}"
                         f"&interval={interval}&limit={limit}")
        # [openTime, open, high, low, close, volume, closeTime,
        #  quoteVolume, trades, ...]
        return [(int(k[0]), float(k[1]), float(k[4]), float(k[7]))
                for k in rows]

    def funding_history(self, symbol, limit=100):
        rows = self._get(f"/fapi/v1/fundingRate?symbol={symbol}"
                         f"&limit={limit}")
        return [(int(r["fundingTime"]), float(r["fundingRate"]))
                for r in rows]


class PaperRunner:
    def __init__(self, db_file, results_dir, symbols=None,
                 equity_start=1000.0, fetcher=None, clock=None,
                 dedup_db=None, stale_bar_ms=STALE_BAR_MS):
        self.symbols = list(symbols or H1_SYMBOLS)
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher or PublicBinanceFetcher()
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.broker = PaperBroker(db_file, self.symbols,
                                  equity_start=equity_start)
        self.risk = PaperRiskState(self.broker)
        store = SignalDedupStore(dedup_db or db_file)
        store.init_schema()
        self.checklist = PretradeChecklist(self.risk, signal_dedup=store)
        self.engine = H1SignalEngine(self.symbols)
        self.router = H1Router(self.engine, self.checklist, self.broker,
                               self.broker, reconciler=H1Reconciler(),
                               journal_sink=self.broker.record_journal)
        self.stale_bar_ms = stale_bar_ms
        self._lock_file = None
        self._cycle_n = 0
        self.log = setup_logging(self.results_dir)

    # ---- resilience (Bölüm 6) -------------------------------------------
    def acquire_lock(self):
        """Duplicate-worker protection: results dizini başına TEK runner.
        flock — process çökünce kernel otomatik bırakır (restart güvenli)."""
        if self._lock_file is not None:
            return
        path = self.results_dir / "paper.lock"
        f = open(path, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise RuntimeError(
                f"another paper runner holds {path} — REFUSING to start "
                f"(duplicate worker protection)")
        self._lock_file = f

    def _critical(self, reason):
        """Critical failure → HALT (fail-closed): 18 kontrolün
        'halted_kill_switch' maddesi yeni pozisyon açan her kararı bloklar."""
        self.broker.event("CRITICAL", reason)
        self.broker.halt(reason)
        self.log.error("CRITICAL: %s", reason)

    def _check_clock(self):
        try:
            server = self.fetcher.server_time()
        except AttributeError:
            self.broker.event("CLOCK_CHECK_UNAVAILABLE",
                              "fetcher has no server_time — drift unchecked")
            return
        except Exception as e:
            self.broker.event("FETCH_ERROR", f"serverTime: {e}")
            return
        drift = server - self.clock()
        if abs(drift) > DRIFT_CRITICAL_MS:
            self._critical(f"CLOCK_DRIFT {drift:+.0f} ms — bar-kapalı "
                           f"hesabı güvenilmez; yeni pozisyon açılmaz")
        elif abs(drift) > DRIFT_WARN_MS:
            self.broker.event("CLOCK_DRIFT_WARN", f"{drift:+.0f} ms")

    def _check_disk(self):
        free_mb = shutil.disk_usage(str(self.results_dir)).free / 1e6
        if free_mb < DISK_MIN_FREE_MB:
            self._critical(f"DISK_LOW {free_mb:.0f} MB boş — ledger yazımı "
                           f"risk altında; yeni pozisyon açılmaz")

    def _check_fresh_data(self, klines, now):
        """Market-data gap / stale feed: 12h (3 bar) taze kapalı bar yoksa
        sinyal güvenilmez → HALT."""
        for s in self.symbols:
            closed = self._closed_bars(klines[s], now)
            if not closed:
                self._critical(f"STALE_DATA {s}: kapalı bar YOK")
            elif now - closed[-1][0] > STALE_DATA_MS:
                age_h = (now - closed[-1][0]) / 3600000
                self._critical(f"STALE_DATA {s}: son kapalı bar "
                               f"{age_h:.1f}h önce — feed ölü; yeni pozisyon "
                               f"açılmaz")

    def _note_data_gaps(self, klines):
        """Eksik bar JOURNAL'lanır (ölçüm); donmuş motorun gap kuralı (pending
        → sonraki MEVCUT open) bunları işler. Tasarım gereği ölümcül DEĞİL."""
        for s in self.symbols:
            kl = klines[s]
            for a, b in zip(kl, kl[1:]):
                if b[0] - a[0] != BAR_MS:
                    self.broker.event(
                        "DATA_GAP", f"{s}: {a[0]} -> {b[0]} "
                                    f"({(b[0] - a[0]) / 3600000:.1f}h)")

    def _fetch_with_retry(self):
        """429/418 → Retry-After kadar bekle, BİR kez tekrar dene; yine
        olursa CYCLE_ERROR (ölümcül değil ama kayıtlı)."""
        try:
            return self._fetch_klines()
        except RateLimited as e:
            wait = min(max(e.retry_after, 1.0), 60.0)
            self.broker.event("RATE_LIMIT",
                              f"HTTP 429/418 — {wait:.0f}s backoff")
            time.sleep(wait)
            return self._fetch_klines()

    def _heartbeat(self, ok=True, err=None, dur_s=0.0):
        """Watchbeat for the supervisor: ts / cycle / rss / duration."""
        hb = {"ts": int(time.time() * 1000), "cycle": self._cycle_n,
              "ok": ok, "error": err, "duration_s": round(dur_s, 3),
              "rss_mb": _rss_mb(), "halted": self.risk.is_halted}
        try:
            (self.results_dir / "paper_heartbeat.json").write_text(
                json.dumps(hb, indent=1))
        except OSError as e:
            self.log.error("heartbeat write failed: %s", e)

    # ---- manifest -------------------------------------------------------
    def freeze_manifest(self):
        """First call FREEZES the manifest (before any live bar); every later
        call VERIFIES it — code/model/param drift stops the runner."""
        stored = self.broker.meta_get("manifest_hash")
        if stored is None:
            m = build_manifest(self.clock())
            self.broker.meta_set("manifest", json.dumps(m))
            self.broker.meta_set("manifest_hash", m["manifest_hash"])
            (self.results_dir / "paper_manifest.json").write_text(
                json.dumps(m, indent=1))
            self.broker.event("MANIFEST_FROZEN", m["manifest_hash"])
            return m
        stored_manifest = json.loads(self.broker.meta_get("manifest"))
        rebuilt = build_manifest(stored_manifest["test_start_ts"])
        if rebuilt["manifest_hash"] != stored:
            self.broker.event("MANIFEST_BREACH", stored)
            raise RuntimeError("manifest mismatch — paper run REFUSES to "
                               "continue (fail-closed)")
        return stored_manifest

    # ---- bar bookkeeping ---------------------------------------------------
    def _last_ts(self, symbol):
        v = self.broker.meta_get(f"last_bar:{symbol}")
        return int(v) if v else None

    def _set_last_ts(self, symbol, ts):
        self.broker.meta_set(f"last_bar:{symbol}", str(ts))

    def _fetch_klines(self):
        out = {}
        for s in self.symbols:
            out[s] = self.fetcher.klines(s, "4h", WARMUP_BARS + 10)
        return out

    def _refresh_funding(self):
        for s in self.symbols:
            try:
                hist = self.fetcher.funding_history(s)
            except Exception as e:
                self.broker.event("FETCH_ERROR", f"funding {s}: {e}")
                continue
            for (ts, rate) in hist:
                self.broker.note_funding_rate(s, ts, rate)

    def _closed_bars(self, klines, now):
        """Only CLOSED bars: open + 4h <= now (an in-progress bar is never
        a decision input)."""
        return [k for k in klines if k[0] + BAR_MS <= now]

    def _warmup(self, klines_by_sym):
        """Rebuild engine state from history WITHOUT executing. Runs on the
        first start AND on every restart (the in-memory engine state is
        rebuilt; already-executed bars stay CONSUMED in the persistent dedup
        store, so a restart can never double-execute a decision). On restart
        the replay is capped at the stored last-processed bar — bars beyond
        it are NEW and processed live."""
        now = self.clock()
        events = []
        for s, kl in klines_by_sym.items():
            cutoff = self._last_ts(s)
            for (ot, o, c, qv) in self._closed_bars(kl, now):
                if cutoff is None or ot <= cutoff:
                    events.append((ot, s, o, c, qv))
        events.sort(key=lambda e: (e[0], e[1]))
        last = {}
        for (ot, s, o, c, qv) in events:
            last[s] = (ot, o, c, qv)
            self.router.run_bar(s, ot, o, c, execute=False)
        for s, (ot, o, c, qv) in last.items():
            if self._last_ts(s) is None or ot >= self._last_ts(s):
                self.broker.note_bar(s, ot, o, c, qv)
                self._set_last_ts(s, ot)
        self.broker.event("WARMUP_DONE",
                          f"{len(events)} bars replayed (no execution)")

    # ---- one cycle -----------------------------------------------------------
    def cycle(self):
        """One poll: fetch public data, process new CLOSED bars through the
        router (signal -> 18 checks -> paper executor), record recon and
        status. Returns the status dict."""
        self._cycle_n += 1
        now = self.clock()
        klines = self._fetch_with_retry()
        # resilience: clock drift / disk / feed freshness / data gaps
        self._check_clock()
        self._check_disk()
        self._check_fresh_data(klines, now)
        self._note_data_gaps(klines)
        # first start: freeze the manifest BEFORE any bar is processed;
        # restart: verify it (a breach raises, fail-closed)
        self.freeze_manifest()
        # always rebuild the engine from history (first start AND restart)
        self._warmup(klines)
        # gather new closed bars across symbols, merged-timeline order
        events = []
        for s in self.symbols:
            last = self._last_ts(s)
            for (ot, o, c, qv) in self._closed_bars(klines[s], now):
                if last is None or ot > last:
                    events.append((ot, s, o, c, qv))
        events.sort(key=lambda e: (e[0], e[1]))
        for (ot, s, o, c, qv) in events:
            self.broker.note_bar(s, ot, o, c, qv)
            if now - ot > self.stale_bar_ms:
                # STALE SIGNAL (fail-closed): bu bar 2 barden eski kapandı —
                # sinyali ŞİMDİ, eski open fiyatından icra etmek ölçümü
                # bozar. Karar journal'lanır (MISSED/WARMUP_SKIPPED → drift
                # AÇIKLANMIŞ sayılır), icra YOK.
                self.broker.event(
                    "STALE_BAR", f"{s} bar {ot} {(now - ot) / 3600000:.1f}h "
                                 f"eski — execute=NO (fail-closed)")
                self.router.run_bar(s, ot, o, c, execute=False)
            else:
                self.router.run_bar(s, ot, o, c)
            self._set_last_ts(s, ot)
        self._refresh_funding()
        # reconciliation record + status
        items = H1Reconciler().reconcile(self.engine, self.broker.positions(),
                                         self.broker.equity())
        ts = events[-1][0] if events else now
        self.broker.record_recon(ts, [{"symbol": i.symbol,
                                       "status": i.status,
                                       "detail": i.detail} for i in items])
        status = self._status(items)
        (self.results_dir / "paper_status.json").write_text(
            json.dumps(status, indent=1, default=str))
        self._heartbeat()
        return status

    def _status(self, items):
        outcomes = {}
        for j in self.broker.journal_rows():
            outcomes[j["outcome"]] = outcomes.get(j["outcome"], 0) + 1
        return {
            "ts": self.clock(),
            "strategy": STRATEGY_ID,
            "label": STRATEGY_LABEL,
            "account": self.broker.account_status(),
            "positions": self.broker.positions(),
            "target_book": {s: self.engine.direction(s)
                            for s in self.symbols},
            "recon": [{"symbol": i.symbol, "status": i.status}
                      for i in items],
            "journal_outcomes": outcomes,
            "manifest_hash": self.broker.meta_get("manifest_hash"),
        }

    # ---- long-running mode ----------------------------------------------------
    def run(self, interval_sec=300):
        self.acquire_lock()          # duplicate worker protection
        self.broker.event("RUNNER_START", f"interval={interval_sec}s "
                          f"pid={os.getpid()}")
        while True:
            t0 = time.time()
            try:
                self.cycle()
                ok, err = True, None
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
                self.broker.event("CYCLE_ERROR", err)
                self._heartbeat(ok=False, err=err, dur_s=time.time() - t0)
            self.log.info("cycle %d ok=%s dur=%.1fs rss=%s",
                          self._cycle_n, ok, time.time() - t0,
                          _rss_mb())
            time.sleep(interval_sec)


def main():
    import argparse
    import sys
    p = argparse.ArgumentParser(description="BLOK25 paper/shadow runner "
                                            "(NO real orders)")
    p.add_argument("--db", default=os.path.expanduser(
        "~/futures-research/blok25/results/paper_ledger.db"))
    p.add_argument("--results", default=os.path.expanduser(
        "~/futures-research/blok25/results"))
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--interval", type=float, default=300.0)
    p.add_argument("--once", action="store_true",
                   help="run one cycle and exit")
    args = p.parse_args()
    runner = PaperRunner(args.db, args.results, equity_start=args.equity)
    if not args.once:
        try:
            runner.acquire_lock()
        except RuntimeError as e:
            print(f"REFUSING TO START: {e}")
            sys.exit(1)
    if args.once:
        print(json.dumps(runner.cycle(), indent=1, default=str))
        return
    runner.run(interval_sec=args.interval)


if __name__ == "__main__":
    main()
