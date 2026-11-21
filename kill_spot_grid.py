#!/usr/bin/env python3
"""C0 Grid Emergency Kill Switch — BLOK12 §5: GÖMÜLÜ KİMLİK YOK.

Only affects the passed executor_id + cancels BTCUSDT open orders.
Does NOT touch auto_trader, MAX_OPEN_POSITIONS, or any other service.

BLOK12 §5 değişikliği (kök neden): dosyada düz metin heartbeat yönetici
kimliği gömülüydü ve geçmiş commit'lerde yaşıyor (git geçmişi temizlenmez —
rotasyon operatör işidir, bkz. docs/OPERATOR_ROTATION_HEARTBEAT_20261121.md).
Yeni sözleşme:
  * Kimlik yalnız ortamdan gelir: HB_ADMIN_USER / HB_ADMIN_PASS
    (veya HB_ADMIN_ENV_FILE ile işaretlenen 0600 env dosyası; KEY=VALUE).
  * Eksik/boş kimlik → FAIL-CLOSED: HB adımına hiç gidilmez, exit 2.
  * Kimlik değeri HİÇBİR çıktıya yazılmaz (log/baskı yok).
Not: bu script acil durum aracıdır; çalıştırılması operatör işlemidir.
"""
import sys, os, hmac, hashlib, time
import requests

HB_URL = os.getenv("HB_URL", "http://localhost:8000")
SYMBOL = "BTCUSDT"


def load_hb_auth(env=None):
    """(user, pass) — yoksa SystemExit(2). Değerleri asla loglamaz/yazdırmaz."""
    env = os.environ if env is None else env
    env_file = env.get("HB_ADMIN_ENV_FILE", "")
    if env_file:
        try:
            st = os.stat(env_file)
            if st.st_mode & 0o077:
                print("HB_ADMIN_ENV_FILE izinleri sıkı olmalı (chmod 600) — RED")
                raise SystemExit(2)
            with open(env_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError as e:
            print(f"HB_ADMIN_ENV_FILE okunamadı ({e.__class__.__name__}) — RED")
            raise SystemExit(2)
    user = env.get("HB_ADMIN_USER", "")
    pw = env.get("HB_ADMIN_PASS", "")
    if not user or not pw:
        print("HB_ADMIN_USER/HB_ADMIN_PASS tanımsız — fail-closed, HB adımı ATLANDI"
              " (kimlik gömülü değildir; bkz. docs/OPERATOR_ROTATION_HEARTBEAT_20261121.md)")
        raise SystemExit(2)
    return user, pw


def hb_stop(eid, auth):
    print(f"=== 1) HB API: STOP executor {eid} ===")
    try:
        r = requests.post(f"{HB_URL}/executors/{eid}/stop",
                          json={"close_type": "EARLY_STOP", "keep_position": False},
                          auth=auth, timeout=15)
        print(f"  HTTP {r.status_code}")
    except Exception as e:
        print(f"  HB stop failed: {e.__class__.__name__}")


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
        print(f"  Cancel failed: {e.__class__.__name__}")


def show_balance():
    print("=== 3) Balance BTC + USDT ===")
    try:
        r = binance_signed("GET", "/api/v3/account")
        for b in r.get("balances", []):
            if b["asset"] in ("BTC", "USDT"):
                print(f"  {b['asset']}: free={b['free']} locked={b['locked']}")
    except Exception as e:
        print(f"  Balance failed: {e.__class__.__name__}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python kill_spot_grid.py <executor_id>")
        sys.exit(1)
    eid = sys.argv[1]
    hb_stop(eid, load_hb_auth())
    print()
    cancel_orders()
    print()
    show_balance()
    print("\n=== KILL SWITCH COMPLETE ===")
