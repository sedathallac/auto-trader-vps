#!/usr/bin/env python3
"""C0 Grid Emergency Kill Switch
Only affects the passed executor_id + cancels BTCUSDT open orders.
Does NOT touch auto_trader, MAX_OPEN_POSITIONS, or any other service.
"""
import sys, os, hmac, hashlib, time, json
import requests

HB_URL = "http://localhost:8000"
HB_AUTH = ("furkan_admin", "HmB0t_S3cure_2026x")
SYMBOL = "BTCUSDT"

def hb_stop(eid):
    print(f"=== 1) HB API: STOP executor {eid} ===")
    try:
        r = requests.post(f"{HB_URL}/executors/{eid}/stop",
                          json={"close_type": "EARLY_STOP", "keep_position": False},
                          auth=HB_AUTH, timeout=15)
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  HB stop failed: {e}")

def binance_signed(method, path, params=None):
    key = os.environ["BINANCE_KEY"]
    secret = os.environ["BINANCE_SECRET"]
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    q = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com{path}?{q}&signature={sig}"
    r = requests.request(method, url, headers={"X-MBX-APIKEY": key}, timeout=15)
    return r.json()

def cancel_orders():
    print(f"=== 2) Cancel all {SYMBOL} open orders ===")
    try:
        r = binance_signed("DELETE", "/api/v3/openOrders", {"symbol": SYMBOL})
        if isinstance(r, list):
            print(f"  Cancelled: {len(r)} orders")
        else:
            print(f"  Cancel result: {r}")
    except Exception as e:
        print(f"  Cancel failed: {e}")

def show_balance():
    print(f"=== 3) Balance BTC + USDT ===")
    try:
        r = binance_signed("GET", "/api/v3/account")
        for b in r.get("balances", []):
            if b["asset"] in ("BTC", "USDT"):
                print(f"  {b['asset']}: free={b['free']} locked={b['locked']}")
    except Exception as e:
        print(f"  Balance failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python kill_spot_grid.py <executor_id>")
        sys.exit(1)
    eid = sys.argv[1]
    hb_stop(eid)
    print()
    cancel_orders()
    print()
    show_balance()
    print("\n=== KILL SWITCH COMPLETE ===")
