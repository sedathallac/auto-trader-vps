#!/usr/bin/env python3
"""archiver.py — Kapsamlı ticaret arşivleme beyni.
Binance API'den fill/emir geçmişi çeker, logları parse eder, SQLite'a yazar.
"""
import os, sys, time, json, sqlite3, logging
from datetime import datetime
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~"))

from auto_trader import BinanceAPI, HBAPI

LOG = Path.home() / "archiver.log"
DB = Path.home() / ".trading_archive.db"

log = logging.getLogger("archiver")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    _fh = logging.FileHandler(LOG)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_sh)


def init_db(db_path=None):
    _target = str(db_path) if db_path else str(DB)
    conn = sqlite3.connect(_target, timeout=30.0, isolation_level=None)
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA journal_mode = WAL')
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER UNIQUE,
        order_id INTEGER,
        symbol TEXT,
        side TEXT,
        price REAL,
        qty REAL,
        quote_qty REAL,
        commission REAL,
        commission_asset TEXT,
        realized_pnl REAL,
        time INTEGER,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER UNIQUE,
        symbol TEXT,
        side TEXT,
        type TEXT,
        status TEXT,
        price REAL,
        orig_qty REAL,
        executed_qty REAL,
        stop_price REAL,
        update_time INTEGER,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        level TEXT,
        message TEXT,
        raw_line TEXT,
        captured_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS heartbeats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        archiver TEXT,
        trader TEXT,
        captured_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS archive_symbols (
        symbol TEXT PRIMARY KEY,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        source TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_trade_sync TEXT,
        last_order_sync TEXT
    )""")
    conn.commit()
    return conn


def register_archive_symbol(conn, symbol, source):
    """Upsert into archive_symbols. Never deletes; symbol kept forever."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute("""
        INSERT INTO archive_symbols (symbol, first_seen_at, last_seen_at, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET last_seen_at=excluded.last_seen_at
    """, (symbol, now, now, source))
    conn.commit()


def get_archive_symbols(conn, binance):
    """Union of archive_symbols (permanent) + currently open positions."""
    symbols = set()
    for row in conn.execute("SELECT symbol FROM archive_symbols WHERE enabled=1"):
        symbols.add(row[0])
    try:
        positions = binance.position_risk()
        if isinstance(positions, list):
            symbols |= {p["symbol"] for p in positions if float(p.get("positionAmt", 0)) != 0}
    except Exception as e:
        log.warning(f"get_archive_symbols: position_risk failed: {e}")
    return symbols


def sync_trades(binance, conn):
    """Binance userTrades endpoint'inden son 24 saatteki fill'leri çek."""
    try:
        since = int((time.time() - 24 * 3600) * 1000)
        # Tüm açık pozisyon sembollerini topla
        symbols = get_archive_symbols(conn, binance)

        c = conn.cursor()
        new_count = 0
        for sym in symbols:
            register_archive_symbol(conn, sym, "sync_trades")
            # P1-1: per-symbol watermark, fallback to 24h
            _row = c.execute(
                "SELECT last_trade_sync FROM archive_symbols WHERE symbol=?", (sym,)
            ).fetchone()
            if _row and _row[0]:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    _last = _dt.fromisoformat(_row[0].replace("Z", "+00:00"))
                    _since = int(_last.timestamp() * 1000) - 3600 * 1000  # 1h buffer
                except Exception:
                    _since = int((time.time() - 24 * 3600) * 1000)
            else:
                _since = int((time.time() - 24 * 3600) * 1000)
            since = _since
            try:
                all_trades = []
                from_id = None
                for _page in range(100):
                    _params = {"symbol": sym, "startTime": since, "limit": 500}
                    if from_id is not None:
                        _params = {"symbol": sym, "limit": 500, "fromId": from_id}
                    _page_data = binance._get("/fapi/v1/userTrades", _params, signed=True)
                    if not isinstance(_page_data, list) or not _page_data:
                        break
                    all_trades.extend(_page_data)
                    if len(_page_data) < 500:
                        break
                    from_id = int(_page_data[-1].get("id", 0)) + 1
                trades = all_trades
                if not isinstance(trades, list):
                    continue
                for t in trades:
                    try:
                        c.execute("""INSERT OR IGNORE INTO trades
                            (trade_id, symbol, order_id, side, price, qty, quote_qty,
                             commission, commission_asset, realized_pnl, time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                            t.get("id"),
                            t.get("symbol"),
                            int(t.get("orderId") or 0),
                            "BUY" if t.get("side") == "BUY" else "SELL",
                            float(t.get("price", 0)),
                            float(t.get("qty", 0)),
                            float(t.get("quoteQty", 0)),
                            float(t.get("commission", 0)),
                            t.get("commissionAsset", ""),
                            float(t.get("realizedPnl", 0)),
                            int(t.get("time", 0)),
                        ))
                        if c.rowcount > 0:
                            new_count += 1
                    except Exception as e:
                        log.warning(f"Trade insert hatası: {e}")
            except Exception as e:
                log.warning(f"{sym} userTrades: {e}")
            # P1-1: update per-symbol watermark after processing
            try:
                from datetime import datetime as _dt, timezone as _tz
                _now_iso = _dt.now(_tz.utc).isoformat().replace("+00:00", "Z")
                c.execute("UPDATE archive_symbols SET last_trade_sync=? WHERE symbol=?",
                          (_now_iso, sym))
            except Exception as _e:
                log.warning(f"watermark update failed {sym}: {_e}")
        conn.commit()
        if new_count > 0:
            log.info(f"✅ {new_count} yeni fill arşivlendi")
    except Exception as e:
        log.error(f"sync_trades: {e}")


def sync_orders(binance, conn):
    """Binance allOrders endpoint'inden son 24 saatteki emirleri çek."""
    try:
        symbols = get_archive_symbols(conn, binance)

        c = conn.cursor()
        new_count = 0
        for sym in symbols:
            register_archive_symbol(conn, sym, "sync_orders")
            # P1-1: per-symbol watermark, fallback to 24h
            _row = c.execute(
                "SELECT last_order_sync FROM archive_symbols WHERE symbol=?", (sym,)
            ).fetchone()
            if _row and _row[0]:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    _last = _dt.fromisoformat(_row[0].replace("Z", "+00:00"))
                    _since = int(_last.timestamp() * 1000) - 3600 * 1000
                except Exception:
                    _since = int((time.time() - 24 * 3600) * 1000)
            else:
                _since = int((time.time() - 24 * 3600) * 1000)
            since = _since
            try:
                all_orders = []
                order_from_id = None
                for _page in range(100):
                    _params = {"symbol": sym, "startTime": since, "limit": 500}
                    if order_from_id is not None:
                        _params = {"symbol": sym, "limit": 500, "orderId": order_from_id}
                    _page_data = binance._get("/fapi/v1/allOrders", _params, signed=True)
                    if not isinstance(_page_data, list) or not _page_data:
                        break
                    all_orders.extend(_page_data)
                    if len(_page_data) < 500:
                        break
                    order_from_id = int(_page_data[-1].get("orderId", 0)) + 1
                orders = all_orders
                if not isinstance(orders, list):
                    continue
                for o in orders:
                    try:
                        _pre_exists = c.execute(
                            "SELECT 1 FROM orders WHERE order_id=?", (o.get("orderId"),)
                        ).fetchone() is not None
                        c.execute("""INSERT INTO orders
                            (order_id, symbol, side, type, status, price,
                             orig_qty, executed_qty, stop_price, update_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(order_id) DO UPDATE SET
                                status=excluded.status,
                                executed_qty=excluded.executed_qty,
                                stop_price=excluded.stop_price,
                                update_time=excluded.update_time""", (
                            o.get("orderId"),
                            o.get("symbol"),
                            o.get("side"),
                            o.get("type"),
                            o.get("status"),
                            float(o.get("price", 0)),
                            float(o.get("origQty", 0)),
                            float(o.get("executedQty", 0)),
                            float(o.get("stopPrice", 0)),
                            int(o.get("updateTime", 0)),
                        ))
                        if c.rowcount > 0 and not _pre_exists:
                            new_count += 1
                    except Exception as e:
                        log.warning(f"Order insert hatası: {e}")
            except Exception as e:
                log.warning(f"{sym} allOrders: {e}")
            # P1-1: update per-symbol watermark after processing
            try:
                from datetime import datetime as _dt, timezone as _tz
                _now_iso = _dt.now(_tz.utc).isoformat().replace("+00:00", "Z")
                c.execute("UPDATE archive_symbols SET last_order_sync=? WHERE symbol=?",
                          (_now_iso, sym))
            except Exception as _e:
                log.warning(f"watermark update failed {sym}: {_e}")
        conn.commit()
        if new_count > 0:
            log.info(f"✅ {new_count} yeni emir arşivlendi")
    except Exception as e:
        log.error(f"sync_orders: {e}")


def parse_logs(conn):
    STATE = Path.home() / ".parse_state.json"
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    """auto_trader.log ve orphan_killer.log'daki hataları arşivle."""
    c = conn.cursor()
    for log_path, source in [
        (Path.home() / "auto_trader.log", "trader"),
        (Path.home() / "orphan_killer.log", "orphan_killer"),
    ]:
        if not log_path.exists():
            continue
        try:
            # Son 500 satırı oku
            size = log_path.stat().st_size
            key = str(log_path)
            if key not in state:
                state[key] = size
                continue
            last = state[key]
            if size < last:
                last = 0
            if size == last:
                continue
            with open(log_path, "r") as f:
                f.seek(last)
                lines = f.read().splitlines()
                new_offset = f.tell()
            for line in lines:
                if any(k in line for k in ["ERROR", "CRITICAL", "Traceback", "Exception"]):
                    # Duplicate kontrolü (aynı mesaj son 60 saniyede varsa atla)
                    msg = line[:500]
                    # FIX: dedup tam satir ile (raw_line = line, msg degil)
                    c.execute("""SELECT COUNT(*) FROM errors
                        WHERE raw_line = ? AND captured_at > datetime('now','-30 days')""", (line,))
                    if c.fetchone()[0] == 0:
                        level = "CRITICAL" if "CRITICAL" in line else "ERROR"
                        c.execute("""INSERT INTO errors (source, level, message, raw_line)
                            VALUES (?, ?, ?, ?)""", (source, level, msg, line))
            conn.commit()
            state[key] = new_offset
        except Exception as e:
            log.warning(f"parse_logs {source}: {e}")


    try:
        STATE.write_text(json.dumps(state, indent=2))
    except Exception as _e:
        log.warning(f"parse_logs outer: {_e}")


def capture_heartbeat(conn):
    c = conn.cursor()
    hb = Path.home() / ".auto_trader.heartbeat"
    trader_ts = ""
    if hb.exists():
        trader_ts = hb.read_text().strip()
    c.execute("INSERT INTO heartbeats (archiver, trader) VALUES (?, ?)",
              (datetime.now().isoformat(), trader_ts))
    conn.commit()


def archive_once():
    bk = os.environ.get("BINANCE_KEY")
    bs = os.environ.get("BINANCE_SECRET")
    if not bk or not bs:
        log.error("BINANCE_KEY/SECRET yok")
        return
    binance = BinanceAPI(bk, bs)
    conn = init_db()
    try:
        capture_heartbeat(conn)
        sync_trades(binance, conn)
        sync_orders(binance, conn)
        parse_logs(conn)
        # Özet
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trades")
        t = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM orders")
        o = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM errors")
        e = c.fetchone()[0]
        log.info(f"📊 Arşiv: {t} trade, {o} emir, {e} hata")
    finally:
        conn.close()


def main():
    if "--once" in sys.argv:
        archive_once()
        return
    log.info("🚀 Archiver başladı (60sn döngü)")
    while True:
        try:
            archive_once()
        except Exception as e:
            log.exception(f"loop: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
