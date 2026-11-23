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
import hashlib
import json
import os
import time
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


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes():
    """Hash of every source that determines the paper run's behavior."""
    here = os.path.dirname(os.path.abspath(__file__))
    return {name: file_sha256(os.path.join(here, name))
            for name in ("h1_adapter.py", "risk_checklist.py",
                         "paper_broker.py", "signal_dedup.py")}


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

    def __init__(self, base_url=BASE_URL, timeout=15):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path):
        url = self.base + path
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

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
                 dedup_db=None):
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
        now = self.clock()
        klines = self._fetch_klines()
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
        self.broker.event("RUNNER_START", f"interval={interval_sec}s")
        while True:
            try:
                self.cycle()
            except Exception as e:
                self.broker.event("CYCLE_ERROR", f"{type(e).__name__}: {e}")
            time.sleep(interval_sec)


def main():
    import argparse
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
    if args.once:
        print(json.dumps(runner.cycle(), indent=1, default=str))
        return
    runner.run(interval_sec=args.interval)


if __name__ == "__main__":
    main()
