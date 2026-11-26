import sys, os, tempfile, sqlite3, shutil, threading
sys.path.insert(0, '/root')

test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
conn = sqlite3.connect(test_db)
conn.executescript("""
CREATE TABLE risk_state (id INTEGER PRIMARY KEY CHECK (id=1), peak_equity REAL NOT NULL DEFAULT 0, consecutive_losses INTEGER NOT NULL DEFAULT 0, daily_trades INTEGER NOT NULL DEFAULT 0, daily_pnl REAL NOT NULL DEFAULT 0, last_reset_date TEXT NOT NULL, is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE trade_ledger (executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL NOT NULL DEFAULT 0, closed_at TEXT, recorded_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'sync');
CREATE TABLE sync_meta (id INTEGER PRIMARY KEY CHECK (id=1), last_close_ts REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
INSERT INTO risk_state (id, peak_equity, consecutive_losses, daily_trades, daily_pnl, last_reset_date, is_halted, halt_reason, updated_at) VALUES (1, 2524.63, 0, 0, 0.0, '2026-11-11', 0, NULL, '2026-11-11T00:00:00');
INSERT INTO sync_meta (id, last_close_ts, updated_at) VALUES (1, 1794293285.83, '2026-11-11T00:00:00');
""")
conn.commit()
conn.close()

tmp_module = tempfile.NamedTemporaryFile(suffix="_c3_mod.py", delete=False, dir="/root")
tmp_module.close()
shutil.copy("/root/risk_engine.py.c3.new", tmp_module.name)

import importlib.util
spec = importlib.util.spec_from_file_location("risk_engine_c3", tmp_module.name)
re_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(re_mod)
RiskEngine = re_mod.RiskEngine

cfg = {
    "db_file": test_db,
    "max_drawdown_pct": 0.20, "max_total_margin_pct": 0.40,
    "max_consecutive_losses": 3, "max_trades_per_day": 20,
    "risk_per_trade_pct": 0.015, "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008, "min_notional": 5.5, "max_notional": 25.0,
}

print("=" * 60)
print("C3 — SQLite Ledger Testleri (FIX)")
print("=" * 60)

print("\n[TEST 1] risk_state yukleme")
r = RiskEngine(cfg)
print(f"  peak={r.peak_equity}, daily_pnl={r.daily_pnl}, is_halted={r.is_halted}")
assert r.peak_equity == 2524.63
assert r.is_halted == False
print("  PASS")

print("\n[TEST 2] Ilk kayit (insert)")
inserted, msg = r.record_trade("EXEC1", "BTC-USDT", 5.0, "2026-11-11T06:00:00")
print(f"  inserted={inserted}, msg={msg}, daily_pnl={r.daily_pnl}")
assert inserted == True and r.daily_pnl == 5.0
print("  PASS")

print("\n[TEST 3] Idempotency")
inserted, msg = r.record_trade("EXEC1", "BTC-USDT", 5.0, "2026-11-11T06:00:00")
print(f"  inserted={inserted}, msg={msg}, daily_pnl={r.daily_pnl}")
assert inserted == False and r.daily_pnl == 5.0
print("  PASS")

print("\n[TEST 4] Negatif pnl")
inserted, msg = r.record_trade("EXEC2", "ETH-USDT", -2.0, "2026-11-11T06:01:00")
assert inserted == True and r.daily_pnl == 3.0 and r.consecutive_losses == 1
print(f"  daily_pnl={r.daily_pnl}, losses={r.consecutive_losses}")
print("  PASS")

print("\n[TEST 5] is_halted kalici")
r.is_halted = True
r.halt_reason = "TEST_HALT"
r._save_state()
r2 = RiskEngine(cfg)
print(f"  Yeni obje: is_halted={r2.is_halted}, halt_reason={r2.halt_reason}")
assert r2.is_halted == True and r2.halt_reason == "TEST_HALT"
# Reset disk'te
r2.is_halted = False
r2.halt_reason = None
r2._save_state()
print("  PASS")

print("\n[TEST 6] trade_ledger icerigi")
conn = sqlite3.connect(test_db)
rows = conn.execute("SELECT executor_id, trading_pair, net_pnl_quote, source FROM trade_ledger ORDER BY recorded_at").fetchall()
conn.close()
for row in rows:
    print(f"    {row}")
assert len(rows) == 2
print("  PASS")

print("\n[TEST 7] approve_trade 25 USDT cap (FRESH obje)")
r3 = RiskEngine(cfg)  # Disk'ten is_halted=False okur
print(f"  r3.is_halted={r3.is_halted}")
assert r3.is_halted == False
notional, status = r3.approve_trade(balance=100000, stop_distance_pct=0.01)
print(f"  notional={notional}, status={status}")
assert notional == 25.0, f"beklenen 25.0, bulunan {notional}"
print("  PASS")

print("\n[TEST 8] Concurrent insert (5 thread)")
results = []
def concurrent_insert(eid):
    ins, _ = r3.record_trade(eid, "TEST-USDT", 0.5, "2026-11-11T06:02:00")
    results.append((eid, ins))
threads = [threading.Thread(target=concurrent_insert, args=(f"CONC{i}",)) for i in range(5)]
for t in threads: t.start()
for t in threads: t.join()
inserted_count = sum(1 for _, ins in results if ins)
print(f"  5 thread, {inserted_count} basarili insert")
assert inserted_count == 5
print("  PASS")

print("\n[TEST 9] Ayni executor 5 thread ile (idempotency stress)")
results2 = []
def dup_insert(_):
    ins, _msg = r3.record_trade("DUP-EXEC", "STRESS-USDT", 1.0, "2026-11-11T06:03:00")
    results2.append(ins)
threads2 = [threading.Thread(target=dup_insert, args=(i,)) for i in range(5)]
for t in threads2: t.start()
for t in threads2: t.join()
success_count = sum(1 for x in results2 if x)
print(f"  5 thread ayni executor: {success_count} basarili (beklenen 1)")
assert success_count == 1
print("  PASS")

print("\n" + "=" * 60)
print("TUM C3 TESTLERI BASARILI")
print("=" * 60)

os.unlink(test_db)
os.unlink(tmp_module.name)
