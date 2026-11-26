#!/usr/bin/env python3
"""archive_report.py — Arşiv veritabanı özet raporu"""
import sqlite3
from datetime import datetime, timezone, timedelta

from pathlib import Path as _P
import sys as _s
_DB = _P.home() / '.trading_archive.db'
if not _DB.exists():
    print('HATA: DB bulunamadi: ' + str(_DB))
    _s.exit(1)
conn = sqlite3.connect(str(_DB))
c = conn.cursor()

print("\n" + "═" * 70)
print(f"  📊 TİCARET ARŞİVİ — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("═" * 70)

c.execute("SELECT COUNT(*) FROM trades")
total_trades = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM orders")
total_orders = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM errors WHERE captured_at > datetime('now','-24 hours')")
recent_errors = c.fetchone()[0]
print(f"\n📈 Toplam: {total_trades} fill, {total_orders} emir, {recent_errors} hata (24s)")

print("\n💰 SEMBOL BAZINDA NET PNL")
c.execute("""SELECT symbol,
    COUNT(*), SUM(realized_pnl), SUM(commission)
    FROM trades
    GROUP BY symbol ORDER BY SUM(realized_pnl) - SUM(commission) DESC""")
for sym, cnt, pnl, fee in c.fetchall():
    net = pnl - fee
    icon = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
    print(f"  {icon} {sym:12s} | {cnt:2d} fill | PnL={pnl:+.4f} | fee={fee:.4f} | net={net:+.4f}")

print("\n" + "─" * 70)
c.execute("""SELECT SUM(realized_pnl), SUM(commission) FROM trades""")
res = c.fetchone()
if res[0] is not None:
    gross, fee = res
    print(f"  Brüt PnL:   {gross:+.4f}")
    print(f"  Komisyon:   {fee:.4f}")
    print(f"  NET PnL:    {gross - fee:+.4f}  {'🟢' if gross - fee > 0 else '🔴'}")

print("\n⚠️  SON 5 HATA")
c.execute("""SELECT source, level, message, captured_at
    FROM errors WHERE captured_at > datetime('now','-24 hours')
    ORDER BY captured_at DESC LIMIT 5""")
for src, lvl, msg, ts in c.fetchall():
    print(f"  [{src}] {ts[:19]} {msg[:80]}")

print("\n📋 SON 5 EMİR")
c.execute("""SELECT symbol, side, type, status, executed_qty, orig_qty
    FROM orders ORDER BY update_time DESC LIMIT 5""")
for sym, side, typ, st, eq, oq in c.fetchall():
    icon = "✅" if st == "FILLED" else ("❌" if st in ("CANCELED","REJECTED") else "⏳")
    print(f"  {icon} {sym} {side} {typ} [{st}] {eq}/{oq}")

print("\n" + "═" * 70 + "\n")
conn.close()
