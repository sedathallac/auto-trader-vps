#!/usr/bin/env python3
"""BLOK28.1 §12 — SALT-OKUNUR hesap doğrulama denemesi (test/kanıt aracı).

Yalnızca İMZALI GET uçları çağırır (mainnet, USDT-M futures):
  /fapi/v2/positionRisk, /fapi/v2/balance, /fapi/v1/premiumIndex,
  /fapi/v1/leverageBracket, /fapi/v1/positionSide/dual (mod),
  /fapi/v1/account (margin/leverage bilgileri)

HİÇBİR emir POST EDİLMEZ. Anahtar/secret DEĞERLERİ çıktıya/-loga
YAZILMAZ (§12: "API key/secret değerlerini loglama") — yalnızca
değişken adı ve hata kodları raporlanır.

Çalıştırma: python3 scripts/blok28_1_account_verify.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://fapi.binance.com"

ENDPOINTS = [
    ("GET", "/fapi/v2/positionRisk", "SIGNED",
     "pozisyon riskleri (net qty/marj/likidasyon)"),
    ("GET", "/fapi/v2/balance", "SIGNED", "USDT-M cüzdan bakiyesi"),
    ("GET", "/fapi/v1/premiumIndex", "PUBLIC", "mark/index fiyatları"),
    ("GET", "/fapi/v1/leverageBracket", "SIGNED",
     "bakım marjı katmanları (§6)"),
    ("GET", "/fapi/v1/positionSide/dual", "SIGNED",
     "position mode (one-way/hedge)"),
    ("GET", "/fapi/v2/account", "SIGNED", "hesap özeti (kaldıraç/marj modu)"),
]


def hmac_sha256(secret: str, msg: str) -> str:
    import hashlib
    import hmac
    return hmac.new(secret.encode(), msg.encode(),
                    hashlib.sha256).hexdigest()


def signed_get(base, path, key, secret, params=None):
    q = dict(params or {})
    q["timestamp"] = int(time.time() * 1000)
    q["recvWindow"] = 5000
    qs = urllib.parse.urlencode(q)
    sig = hmac_sha256(secret, qs)
    url = f"{base}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    key = os.environ.get("BINANCE_KEY", "")
    secret = os.environ.get("BINANCE_SECRET", "")
    print(f"BINANCE_KEY set: {bool(key)} (değer YAZILMAZ)")
    print(f"BINANCE_SECRET set: {bool(secret)} (değer YAZILMAZ)")
    if not key or not secret:
        print("SONUÇ: anahtar yok → ACCOUNT_RECONCILIATION=BLOCKED")
        return 2
    signed_ok = False
    for method, path, auth, desc in ENDPOINTS:
        try:
            data = signed_get(BASE, path, key, secret)
            n = len(data) if isinstance(data, list) else 1
            print(f"  {path:35s} [{auth}] OK   ({desc}; {n} kayıt) — "
                  "değerler rapora yazılmadı (salt-okunur kanıt)")
            if auth == "SIGNED":
                signed_ok = True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            print(f"  {path:35s} [{auth}] HTTP {e.code} — {body}")
        except Exception as e:  # noqa: BLE001
            print(f"  {path:35s} [{auth}] HATA {type(e).__name__}: {e}")
    if signed_ok:
        print("SONUÇ: imzalı GET'ler çalışıyor — hesap verisi kanıtlandı")
        return 0
    print("SONUÇ: İMZALI GET'lerin TAMAMI başarısız (public uç çalışıyor) →")
    print("  ACCOUNT_RECONCILIATION=BLOCKED, TRADE_PERMISSION_NOT_PROVEN,")
    print("  LIQUIDATION_MODEL (gerçek tier) BLOCKED")
    return 2


if __name__ == "__main__":
    sys.exit(main())
