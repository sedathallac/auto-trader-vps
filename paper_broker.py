"""paper_broker.py — BLOK25 Bölüm 4: paper/shadow broker. NO REAL ORDERS.

This module has NO network surface at all (enforced by
tests/test_blok25_paper.py::test_paper_broker_has_no_network_surface): it
cannot send an order even by accident. All market data arrives through
caller-fed methods (the paper_runner fetches PUBLIC endpoints only).

What it models (per the Bölüm 4 requirements):
- REALISTIC fills, never optimistic: a market order pays the half-spread,
  slippage AND a square-root market-impact term on top of the bar-open mid —
  a buy ALWAYS fills above the mid, a sell ALWAYS below (tested).
- commissions per side (taker bp on traded notional),
- funding settlements at real 8h boundaries with real rates (runner-fed),
  accrues iff entry_ts < settlement ts (Binance semantics),
- mark-to-market uPnL per bar, realized PnL per trade, drawdown from peak,
- liquidation per an ANALYTIC PARTIAL model (cross margin, <=1x gross ->
  ~95% adverse move; BLOK23 §6: the true MM schedule is unverifiable) —
  a crossing is a CRITICAL event and HALTS new executions,
- SL/TP barriers are MODELED (configurable) but DISABLED for H1 (frozen
  research design: flip-only exit); a live transition would require the
  operator-gated exchange-side SL instead,
- full state in SQLite (paper ledger): restart-safe, restart-recoverable.

Fill model (frozen in the run manifest BEFORE the paper run starts):
  fill_buy  = ref * (1 + (half_spread_bp + slippage_bp + impact_bp)/1e4)
  fill_sell = ref * (1 - (half_spread_bp + slippage_bp + impact_bp)/1e4)
  impact_bp = min(impact_max_bp,
                  impact_coeff * 1e4 * sqrt(notional / bar_quote_volume))
  (volume unavailable -> impact_max_bp, conservative)
"""
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Optional

# ---- frozen execution model (recorded verbatim in the manifest) ----------
FILL_MODEL = {
    "half_spread_bp": 1.0,     # half quoted spread vs the bar-open mid
    "slippage_bp": 3.0,        # base slippage (prereg §17/18)
    "impact_coeff": 0.1,       # sqrt-impact coefficient
    "impact_max_bp": 10.0,     # impact cap; also used when volume unknown
    "commission_bp": 5.0,      # taker commission per side
    "funding_model": "real rate at real 8h boundaries (runner-fed public "
                     "data); accrues iff entry_ts < settlement ts; "
                     "cf = -dir*rate*units*last_close_before_settlement",
    "mark_model": "4h close fed by the runner (public klines)",
    "liquidation_model": "PARTIAL analytic: cross margin, <=1x gross -> "
                         "liquidation at ~95% adverse move from entry; "
                         "crossing is CRITICAL and halts new executions",
    "sl_tp_model": "barrier checking per bar; DISABLED for H1 (flip-only "
                   "research design)",
}
LIQ_FRACTION = 0.95           # analytic adverse-move bound (see FILL_MODEL)


def fill_model_hash() -> str:
    import hashlib
    return hashlib.sha256(json.dumps(FILL_MODEL, sort_keys=True)
                          .encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperBroker:
    """World + Executor for the H1Router. SQLite-backed paper ledger."""

    def __init__(self, db_file, symbols, equity_start=1000.0,
                 fill_model=None, barrier_config=None):
        self.db_file = str(db_file)
        self.symbols = list(symbols)
        self.equity_start = float(equity_start)
        self.fill_model = dict(fill_model or FILL_MODEL)
        # SL/TP barriers: None = flip-only (H1 frozen design). A config like
        # {"sl_pct": 0.05} would model an exchange-side STOP per bar.
        self.barrier_config = barrier_config
        self._last_mark = {}          # symbol -> latest close (mark proxy)
        self._quote_volume = {}       # symbol -> latest bar quote volume
        self._cur_ts = {}             # symbol -> current bar ts (note_bar)
        self._init_schema()
        self._reload_marks()         # restart: restore last mark per symbol

    def _reload_marks(self):
        """On restart, restore the last mark per symbol from the marks
        table so unrealized PnL survives a process bounce."""
        conn = self._connect()
        try:
            for s in self.symbols:
                row = conn.execute(
                    "SELECT mark_px FROM marks WHERE symbol=? "
                    "ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
                if row:
                    self._last_mark[s] = row["mark_px"]
        finally:
            conn.close()

    # ---- sqlite ----------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(self.db_file, timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self):
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL, peak_equity REAL NOT NULL,
                    halted INTEGER NOT NULL DEFAULT 0,
                    halt_reason TEXT);
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY, direction INTEGER NOT NULL,
                    units REAL NOT NULL, entry_px REAL NOT NULL,
                    entry_ts INTEGER NOT NULL, entry_notional REAL NOT NULL,
                    e_comm REAL NOT NULL, liq_px REAL,
                    signal_bar_ts INTEGER, signal_price REAL);
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, direction INTEGER NOT NULL,
                    entry_ts INTEGER, exit_ts INTEGER,
                    entry_px REAL, exit_px REAL, units REAL,
                    e_comm REAL, x_comm REAL, pnl REAL,
                    signal_bar_ts INTEGER, execution_ts INTEGER,
                    ref_price REAL, signal_price REAL,
                    entry_signal_price REAL,
                    spread_bp REAL, slippage_bp REAL, impact_bp REAL,
                    commission REAL, reason TEXT);
                CREATE TABLE IF NOT EXISTS funding_rates (
                    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                    rate REAL NOT NULL, PRIMARY KEY (symbol, ts));
                CREATE TABLE IF NOT EXISTS funding_cursor (
                    symbol TEXT PRIMARY KEY, last_ts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS funding (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                    rate REAL NOT NULL, units REAL NOT NULL,
                    mark_px REAL NOT NULL, cf REAL NOT NULL,
                    direction INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL, symbol TEXT NOT NULL,
                    mark_px REAL NOT NULL, upnl REAL NOT NULL,
                    equity REAL NOT NULL, drawdown REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS recon (
                    ts INTEGER NOT NULL, items TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                    open REAL, close REAL, quote_volume REAL,
                    PRIMARY KEY (symbol, ts));
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL, entry TEXT NOT NULL);
            """)
            if conn.execute("SELECT 1 FROM account WHERE id=1").fetchone() \
                    is None:
                conn.execute("INSERT INTO account (id, cash, peak_equity) "
                             "VALUES (1, ?, ?)",
                             (self.equity_start, self.equity_start))
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('started_at', ?)",
                    (_now(),))
        finally:
            conn.close()

    def _account(self):
        conn = self._connect()
        try:
            return dict(conn.execute("SELECT * FROM account WHERE id=1")
                        .fetchone())
        finally:
            conn.close()

    def _set_account(self, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        conn = self._connect()
        try:
            conn.execute(f"UPDATE account SET {cols} WHERE id=1",
                         tuple(kw.values()))
        finally:
            conn.close()

    def _position(self, symbol):
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM positions WHERE symbol=?",
                             (symbol,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def _positions(self):
        conn = self._connect()
        try:
            return [dict(r) for r in
                    conn.execute("SELECT * FROM positions "
                                 "WHERE direction != 0")]
        finally:
            conn.close()

    def event(self, kind, detail):
        conn = self._connect()
        try:
            conn.execute("INSERT INTO events (ts, kind, detail) "
                         "VALUES (?, ?, ?)", (_now(), kind, detail))
        finally:
            conn.close()

    def record_journal(self, entry):
        """Durable copy of every router journal entry (measurement items
        survive restarts)."""
        conn = self._connect()
        try:
            conn.execute("INSERT INTO journal (recorded_at, entry) "
                         "VALUES (?, ?)", (_now(), json.dumps(entry,
                                                              default=str)))
        finally:
            conn.close()

    def journal_rows(self):
        conn = self._connect()
        try:
            return [json.loads(r["entry"]) for r in conn.execute(
                "SELECT entry FROM journal ORDER BY id")]
        finally:
            conn.close()

    def halt(self, reason):
        self._set_account(halted=1, halt_reason=reason)
        self.event("HALT", reason)

    # ---- runner-fed market data (public, caller-side) ---------------------
    def note_bar(self, symbol, ts, open_px, close_px, quote_volume):
        """Record the bar (volume feeds the impact model). Called by the
        runner BEFORE router.run_bar for the same bar. The MARK is written
        at the bar close (on_bar_close), after the open-time flip — never
        before it."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bars (symbol, ts, open, close, "
                "quote_volume) VALUES (?, ?, ?, ?, ?)",
                (symbol, ts, open_px, close_px, quote_volume))
        finally:
            conn.close()
        self._quote_volume[symbol] = quote_volume
        self._cur_ts[symbol] = ts

    def note_funding_rate(self, symbol, ts, rate):
        conn = self._connect()
        try:
            conn.execute("INSERT OR REPLACE INTO funding_rates "
                         "(symbol, ts, rate) VALUES (?, ?, ?)",
                         (symbol, ts, rate))
        finally:
            conn.close()

    # ---- World surface ----------------------------------------------------
    def equity(self):
        """Cash (realized) + unrealized at the latest marks."""
        acc = self._account()
        return acc["cash"] + self.unrealized()

    def available_balance(self):
        return max(0.0, self.equity())

    def unrealized(self):
        total = 0.0
        for p in self._positions():
            mark = self._last_mark.get(p["symbol"], p["entry_px"])
            total += p["direction"] * p["units"] * (mark - p["entry_px"])
        return total

    def positions(self):
        """positionRisk-shaped rows (the checklist/reconciler contract)."""
        return [{"symbol": p["symbol"],
                 "positionAmt": str(p["direction"] * p["units"]),
                 "entryPrice": str(p["entry_px"]),
                 "markPrice": str(self._last_mark.get(p["symbol"],
                                                      p["entry_px"]))}
                for p in self._positions()]

    def open_symbols(self):
        return {p["symbol"] for p in self._positions()}

    def active_executor_pairs(self):
        return set()

    def reserved_symbols(self):
        return set()

    def leverage_brackets(self):
        return None                     # liquidation stays PARTIAL (§6)

    # ---- broker hooks (router call order) ----------------------------------
    def settle_funding(self, symbol, upto_ts):
        """Real settlements in (last settled, this bar open]; a position open
        at the settlement snapshot pays even if it exits at this same open
        (Binance semantics, h1_engine step (a) transcription)."""
        conn = self._connect()
        try:
            cur = conn.execute("SELECT last_ts FROM funding_cursor "
                               "WHERE symbol=?", (symbol,)).fetchone()
            last = cur["last_ts"] if cur else 0
            rates = [dict(r) for r in conn.execute(
                "SELECT ts, rate FROM funding_rates WHERE symbol=? AND "
                "ts > ? AND ts <= ? ORDER BY ts", (symbol, last, upto_ts))]
        finally:
            conn.close()
        if not rates:
            return
        pos = self._position(symbol)
        mark = self._last_mark.get(symbol)
        cash = self._account()["cash"]
        for r in rates:
            cf = 0.0
            if pos is not None and pos["direction"] != 0 and \
                    pos["entry_ts"] is not None and pos["entry_ts"] < r["ts"]:
                base_px = mark if mark else pos["entry_px"]
                cf = (-pos["direction"] * r["rate"] * pos["units"] * base_px)
                cash += cf
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT INTO funding (symbol, ts, rate, units, "
                        "mark_px, cf, direction) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (symbol, r["ts"], r["rate"], pos["units"], base_px,
                         cf, pos["direction"]))
                finally:
                    conn.close()
            # a settlement with no open position still advances the cursor
            conn = self._connect()
            try:
                conn.execute("INSERT OR REPLACE INTO funding_cursor "
                             "(symbol, last_ts) VALUES (?, ?)",
                             (symbol, r["ts"]))
            finally:
                conn.close()
        self._set_account(cash=cash)

    def on_bar_close(self, symbol, close_px, ts=None):
        """Update the mark; write the per-bar mark/uPnL/equity/drawdown row;
        check barriers (SL/TP if configured) and the liquidation model.
        ts defaults to the current noted bar (the router calls this at the
        END of each bar, after the open-time flip — the mark therefore
        reflects the post-flip book)."""
        self._last_mark[symbol] = close_px
        ts = ts if ts is not None else self._cur_ts.get(symbol)
        if ts is not None:
            self._write_mark(ts, symbol)
        pos = self._position(symbol)
        if pos is None or pos["direction"] == 0:
            return
        # SL/TP barriers: modeled but DISABLED for H1 (flip-only design)
        if self.barrier_config:
            self._check_barriers(pos, close_px, ts)
        # liquidation (PARTIAL analytic model): ~95% adverse move
        liq = pos.get("liq_px")
        if liq is not None and \
                ((pos["direction"] > 0 and close_px <= liq) or
                 (pos["direction"] < 0 and close_px >= liq)):
            self._liquidate(pos, close_px, ts)

    def _write_mark(self, ts, symbol):
        pos = self._position(symbol)
        upnl = 0.0
        if pos is not None and pos["direction"] != 0:
            upnl = pos["direction"] * pos["units"] * \
                (self._last_mark[symbol] - pos["entry_px"])
        acc = self._account()
        equity = acc["cash"] + self.unrealized()
        peak = max(acc["peak_equity"], equity)
        dd = 1.0 - equity / peak if peak > 0 else 0.0
        conn = self._connect()
        try:
            conn.execute("INSERT INTO marks (ts, symbol, mark_px, upnl, "
                         "equity, drawdown) VALUES (?, ?, ?, ?, ?, ?)",
                         (ts, symbol, self._last_mark[symbol], upnl, equity,
                          dd))
            conn.execute("UPDATE account SET peak_equity=? WHERE id=1", (peak,))
        finally:
            conn.close()

    def _check_barriers(self, pos, close_px, ts):
        cfg = self.barrier_config
        sl = cfg.get("sl_pct")
        if sl is None:
            return
        trig = pos["entry_px"] * (1 - pos["direction"] * sl)
        hit = ((pos["direction"] > 0 and close_px <= trig) or
               (pos["direction"] < 0 and close_px >= trig))
        if hit:
            self._close_position(pos, close_px, "SL_BARRIER", ts)

    def _liquidate(self, pos, close_px, ts):
        """CRITICAL: the analytic liquidation bound was crossed. Close at the
        modeled liquidation price and HALT new executions (fail-closed)."""
        liq = pos["liq_px"]
        self._close_position(pos, liq, "LIQUIDATION", ts)
        self.halt(f"LIQUIDATION model crossed for {pos['symbol']} at "
                  f"{liq} (PARTIAL analytic model — CRITICAL)")
        self.event("CRITICAL", f"liquidation model crossed: {pos['symbol']}")

    # ---- fills -------------------------------------------------------------
    def _impact_bp(self, notional, symbol):
        qv = self._quote_volume.get(symbol)
        if not qv or qv <= 0:
            return self.fill_model["impact_max_bp"]   # conservative
        return min(self.fill_model["impact_max_bp"],
                   self.fill_model["impact_coeff"] * 1e4
                   * math.sqrt(notional / qv))

    def _fill(self, symbol, direction, notional, ref_price):
        """Realistic market fill: pay half-spread + slippage + impact vs the
        bar-open mid. direction +1 buys (fills ABOVE mid), -1 sells (BELOW)."""
        impact = self._impact_bp(notional, symbol)
        bp = (self.fill_model["half_spread_bp"]
              + self.fill_model["slippage_bp"] + impact)
        fill = ref_price * (1.0 + direction * bp / 1e4)
        return fill, bp, impact

    # ---- Executor ------------------------------------------------------------
    def apply_target(self, decision, ref_price):
        """Flip-only executor: close the existing position (if any) and open
        the target, both with realistic fills and commissions. NO real
        orders — this only writes the paper ledger."""
        symbol = decision.symbol
        pos = self._position(symbol)
        fill, bp, impact = self._fill(symbol, decision.direction,
                                      decision.notional, ref_price)
        comm_bp = self.fill_model["commission_bp"]
        e_comm = comm_bp / 1e4 * decision.notional
        cash = self._account()["cash"]
        if pos is not None and pos["direction"] != 0:
            # close leg: sell what we hold -> fill in the ADVERSE direction
            # for the held side
            xfill, xbp, _ = self._fill(symbol, -pos["direction"],
                                       pos["units"] * ref_price, ref_price)
            x_comm = comm_bp / 1e4 * pos["units"] * xfill
            gross = pos["direction"] * pos["units"] * \
                (xfill - pos["entry_px"])
            cash += gross - x_comm
            self._record_trade(pos, xfill, x_comm, gross, decision, xbp)
        units = decision.notional / fill
        cash -= e_comm
        liq = fill * (1.0 - decision.direction * LIQ_FRACTION)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO positions (symbol, direction, units,"
                " entry_px, entry_ts, entry_notional, e_comm, liq_px, "
                "signal_bar_ts, signal_price) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?)",
                (symbol, decision.direction, units, fill,
                 decision.execution_ts, decision.notional, e_comm, liq,
                 decision.bar_ts, decision.signal_price))
        finally:
            conn.close()
        self._set_account(cash=cash)
        return {"executed": True, "mode": "PAPER", "fill": fill,
                "fill_bp": bp, "impact_bp": impact, "units": units,
                "e_comm": e_comm}

    def _record_trade(self, pos, xfill, x_comm, gross, decision, xbp):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO trades (symbol, direction, entry_ts, exit_ts, "
                "entry_px, exit_px, units, e_comm, x_comm, pnl, "
                "signal_bar_ts, execution_ts, ref_price, signal_price, "
                "entry_signal_price, spread_bp, slippage_bp, impact_bp, "
                "commission, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (pos["symbol"], pos["direction"], pos["entry_ts"],
                 decision.execution_ts, pos["entry_px"], xfill,
                 pos["units"], pos["e_comm"], x_comm, gross - x_comm,
                 pos["signal_bar_ts"], decision.execution_ts,
                 decision.ref_price, decision.signal_price,
                 pos.get("signal_price"),
                 self.fill_model["half_spread_bp"],
                 self.fill_model["slippage_bp"], xbp,
                 pos["e_comm"] + x_comm, "FLIP"))
        finally:
            conn.close()

    def _close_position(self, pos, px, reason, ts_):
        """Close at the given price (barrier/liquidation) — no flip open."""
        comm_bp = self.fill_model["commission_bp"]
        x_comm = comm_bp / 1e4 * pos["units"] * px
        gross = pos["direction"] * pos["units"] * (px - pos["entry_px"])
        cash = self._account()["cash"] + gross - x_comm
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO trades (symbol, direction, entry_ts, exit_ts, "
                "entry_px, exit_px, units, e_comm, x_comm, pnl, "
                "signal_bar_ts, execution_ts, ref_price, signal_price, "
                "entry_signal_price, spread_bp, slippage_bp, impact_bp, "
                "commission, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (pos["symbol"], pos["direction"], pos["entry_ts"], ts_,
                 pos["entry_px"], px, pos["units"], pos["e_comm"], x_comm,
                 gross - x_comm, pos["signal_bar_ts"], ts_,
                 self._last_mark.get(pos["symbol"]), None,
                 pos.get("signal_price"),
                 self.fill_model["half_spread_bp"],
                 self.fill_model["slippage_bp"], 0.0,
                 pos["e_comm"] + x_comm, reason))
            conn.execute("DELETE FROM positions WHERE symbol=?",
                         (pos["symbol"],))
            conn.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
        finally:
            conn.close()

    # ---- reconciliation reporting -------------------------------------------
    def record_recon(self, ts, items):
        conn = self._connect()
        try:
            conn.execute("INSERT INTO recon (ts, items) VALUES (?, ?)",
                         (ts, json.dumps(items)))
        finally:
            conn.close()

    # ---- reads for the risk state / reporting --------------------------------
    def account_status(self):
        acc = self._account()
        equity = acc["cash"] + self.unrealized()
        peak = max(acc["peak_equity"], equity)
        return {"cash": acc["cash"], "equity": equity,
                "peak_equity": peak,
                "drawdown": 1.0 - equity / peak if peak > 0 else 0.0,
                "unrealized": self.unrealized(),
                "halted": bool(acc["halted"]),
                "halt_reason": acc["halt_reason"]}

    def closed_trades(self):
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trades ORDER BY id")]
        finally:
            conn.close()

    def funding_rows(self):
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM funding ORDER BY id")]
        finally:
            conn.close()

    def marks_rows(self, symbol=None):
        conn = self._connect()
        try:
            if symbol:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM marks WHERE symbol=? ORDER BY id",
                    (symbol,))]
            return [dict(r) for r in conn.execute(
                "SELECT * FROM marks ORDER BY id")]
        finally:
            conn.close()

    def events_rows(self):
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY id")]
        finally:
            conn.close()

    def meta_get(self, key):
        conn = self._connect()
        try:
            r = conn.execute("SELECT value FROM meta WHERE key=?",
                             (key,)).fetchone()
            return r["value"] if r else None
        finally:
            conn.close()

    def meta_set(self, key, value):
        conn = self._connect()
        try:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) "
                         "VALUES (?, ?)", (key, value))
        finally:
            conn.close()


class PaperRiskState:
    """The RiskEngine surface PretradeChecklist composes, derived from the
    paper ledger (drawdown, consecutive losses, daily PnL, halt state).
    Every value is measured — nothing invented."""

    def __init__(self, broker, cfg=None):
        self.broker = broker
        self.cfg = cfg or {
            "max_drawdown_pct": 0.20,       # prereg §21e / RISK_CONFIG
            "max_consecutive_losses": 3,    # RISK_CONFIG
            "max_trades_per_day": 20,       # RISK_CONFIG
            "max_total_margin_pct": 0.40,   # RISK_CONFIG
            "max_open_positions": 8,        # the frozen universe size
        }

    @property
    def is_halted(self):
        return self.broker.account_status()["halted"]

    @property
    def halt_reason(self):
        return self.broker.account_status()["halt_reason"]

    @property
    def current_drawdown(self):
        return self.broker.account_status()["drawdown"]

    @property
    def peak_equity(self):
        return self.broker.account_status()["peak_equity"]

    @property
    def current_equity(self):
        return self.broker.account_status()["equity"]

    @property
    def consecutive_losses(self):
        streak = 0
        for t in reversed(self.broker.closed_trades()):
            net = t["pnl"] - t["e_comm"]
            if net < 0:
                streak += 1
            else:
                break
        return streak

    @property
    def daily_pnl(self):
        # realized net (pnl - entry comm) of trades exited today + funding
        day0 = _today_start_ms()
        tot = sum(t["pnl"] - t["e_comm"] for t in self.broker.closed_trades()
                  if (t["exit_ts"] or 0) >= day0)
        tot += sum(f["cf"] for f in self.broker.funding_rows()
                   if f["ts"] >= day0)
        return tot

    @property
    def state(self):
        st = self.broker.account_status()
        return {"peak_equity": st["peak_equity"], "source": "paper_ledger"}


def _today_start_ms():
    now = datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, now.day,
                        tzinfo=timezone.utc).timestamp() * 1000)
