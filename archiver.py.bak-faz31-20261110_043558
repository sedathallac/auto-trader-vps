#!/usr/bin/env python3
"""archiver.py — Kapsamlı ticaret arşivleme beyni.
Binance API'den fill/emir geçmişi çeker, logları parse eder, SQLite'a yazar.
"""
import os, sys, time, json, sqlite3, logging, re
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~"))

from auto_trader import BinanceAPI, HBAPI

LOG = Path.home() / "archiver.log"
DB = Path.home() / ".trading_archive.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("archiver")


def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER UNIQUE,
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
    conn.commit()
    return conn


def sync_trades(binance, conn):
    """Binance userTrades endpoint'inden son 24 saatteki fill'leri çek."""
    try:
        since = int((time.time() - 24 * 3600) * 1000)
        # Tüm açık pozisyon sembollerini topla
        symbols = set()
        positions = binance.position_risk()
        if isinstance(positions, list):
            symbols |= {p["symbol"] for p in positions}
        # Bilinen sembolleri de ekle (tracked)
        tracked_file = Path.home() / ".tracked_symbols.json"
        if tracked_file.exists():
            symbols |= set(json.loads(tracked_file.read_text()))

        c = conn.cursor()
        new_count = 0
        for sym in symbols:
            try:
                trades = binance._get("/fapi/v1/userTrades", {
                    "symbol": sym, "startTime": since, "limit": 500
                }, signed=True)
                if not isinstance(trades, list):
                    continue
                for t in trades:
                    try:
                        c.execute("""INSERT OR IGNORE INTO trades
                            (trade_id, symbol, side, price, qty, quote_qty,
                             commission, commission_asset, realized_pnl, time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                            t.get("id"),
                            t.get("symbol"),
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
                        log.debug(f"Trade insert hatası: {e}")
            except Exception as e:
                log.debug(f"{sym} userTrades: {e}")
        conn.commit()
        if new_count > 0:
            log.info(f"✅ {new_count} yeni fill arşivlendi")
    except Exception as e:
        log.error(f"sync_trades: {e}")


def sync_orders(binance, conn):
    """Binance allOrders endpoint'inden son 24 saatteki emirleri çek."""
    try:
        since = int((time.time() - 24 * 3600) * 1000)
        positions = binance.position_risk()
        symbols = set()
        if isinstance(positions, list):
            symbols |= {p["symbol"] for p in positions}
        tracked_file = Path.home() / ".tracked_symbols.json"
        if tracked_file.exists():
            symbols |= set(json.loads(tracked_file.read_text()))

        c = conn.cursor()
        new_count = 0
        for sym in symbols:
            try:
                orders = binance._get("/fapi/v1/allOrders", {
                    "symbol": sym, "startTime": since, "limit": 500
                }, signed=True)
                if not isinstance(orders, list):
                    continue
                for o in orders:
                    try:
                        c.execute("""INSERT OR IGNORE INTO orders
                            (order_id, symbol, side, type, status, price,
                             orig_qty, executed_qty, stop_price, update_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
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
                        if c.rowcount > 0:
                            new_count += 1
                    except Exception as e:
                        log.debug(f"Order insert hatası: {e}")
            except Exception as e:
                log.debug(f"{sym} allOrders: {e}")
        conn.commit()
        if new_count > 0:
            log.info(f"✅ {new_count} yeni emir arşivlendi")
    except Exception as e:
        log.error(f"sync_orders: {e}")


def parse_logs(conn):
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
            lines = log_path.read_text().splitlines()[-500:]
            for line in lines:
                if any(k in line for k in ["ERROR", "CRITICAL", "Traceback", "Exception"]):
                    # Duplicate kontrolü (aynı mesaj son 60 saniyede varsa atla)
                    msg = line[:500]
                    c.execute("""SELECT COUNT(*) FROM errors
                        WHERE raw_line = ? AND captured_at > datetime('now','-1 minute')""", (msg,))
                    if c.fetchone()[0] == 0:
                        level = "CRITICAL" if "CRITICAL" in line else "ERROR"
                        c.execute("""INSERT INTO errors (source, level, message, raw_line)
                            VALUES (?, ?, ?, ?)""", (source, level, msg, line))
            conn.commit()
        except Exception as e:
            log.debug(f"parse_logs {source}: {e}")


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
