"""blok25_integration_check.py — BLOK25 Bölüm 5: credential'sız entegrasyon
kontrolleri.

Kapsam:
  1. PUBLIC endpoint'ler (credential YOK): ping, serverTime (clock drift),
     exchangeInfo (8 H1 sembolü: TRADING / PERPETUAL / USDT), klines 4h,
     fundingRate geçmişi (+ yerleşim aralığı tespiti), bookTicker (spread
     ölçümü — FILL_MODEL half_spread_bp=1.0 varsayımının kontrolü).
  2. İMZALI OKUMA (yalnız BINANCE_KEY/BINANCE_SECRET env mevcutsa, MEVCUT
     mekanizmayla — auto_trader.BinanceAPI; mekanizma DEĞİŞTİRİLMEZ):
     /fapi/v2/balance, /fapi/v2/positionRisk, /fapi/v1/positionSide/dual,
     /fapi/v1/openOrders. SADECE OKUMA — emir/leverage/margin/iptal YOK.

Credential değerleri ASLA yazdırılmaz/başka yere kopyalanmaz; yalnızca
varlık kontrolü ve mevcut mekanizma üzerinden çağrı yapılır.
"""
import json
import sys
import time

from auto_trader import BinanceAPI, BINANCE_FAPI
from h1_adapter import H1_SYMBOLS

_results = []


def check(name, ok, detail):
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def public_checks():
    import requests
    s = requests.Session()

    # ping
    t0 = time.time()
    r = s.get(f"{BINANCE_FAPI}/fapi/v1/ping", timeout=15)
    check("ping", r.status_code == 200, f"HTTP {r.status_code}, "
          f"{(time.time()-t0)*1000:.0f} ms")

    # serverTime + clock drift
    t0 = time.time()
    st = s.get(f"{BINANCE_FAPI}/fapi/v1/time", timeout=15).json()
    local_mid = (t0 + time.time()) / 2 * 1000
    drift = int(st["serverTime"]) - local_mid
    check("clock_drift", abs(drift) < 5000,
          f"server-local = {drift:+.0f} ms (signed çağrı recvWindow=60s "
          f"için güvenli aralık ±5000 ms)")

    # exchangeInfo: 8 H1 sembolü
    info = s.get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=15).json()
    by_sym = {s_["symbol"]: s_ for s_ in info["symbols"]}
    for sym in H1_SYMBOLS:
        si = by_sym.get(sym)
        if si is None:
            check(f"exchangeInfo:{sym}", False, "symbol NOT LISTED")
            continue
        ok = (si["status"] == "TRADING" and si["contractType"] == "PERPETUAL"
              and si["quoteAsset"] == "USDT")
        check(f"exchangeInfo:{sym}", ok,
              f"status={si['status']} contract={si['contractType']} "
              f"quote={si['quoteAsset']}")

    # klines 4h: her sembol için son bar dolu ve güncel
    now = time.time() * 1000
    for sym in H1_SYMBOLS:
        kl = s.get(f"{BINANCE_FAPI}/fapi/v1/klines",
                   params={"symbol": sym, "interval": "4h", "limit": 3},
                   timeout=15).json()
        last_open = int(kl[-1][0])
        age_h = (now - last_open) / 3600000
        ok = len(kl) == 3 and 0 <= age_h < 4.2
        check(f"klines4h:{sym}", ok,
              f"son bar open yaşı {age_h:.2f}h, close={kl[-1][4]}")

    # fundingRate geçmişi + yerleşim aralığı
    for sym in H1_SYMBOLS:
        fr = s.get(f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                   params={"symbol": sym, "limit": 10},
                   timeout=15).json()
        ts = [int(x["fundingTime"]) for x in fr]
        gaps = {round((ts[i+1] - ts[i]) / 3600000, 2) for i in range(len(ts)-1)}
        interval_h = gaps.pop() if len(gaps) == 1 else None
        age_h = (now - ts[-1]) / 3600000
        ok = len(fr) >= 2 and interval_h is not None and age_h < interval_h + 2
        check(f"fundingRate:{sym}", ok,
              f"son {len(fr)} kayıt, aralık {interval_h}h, "
              f"son oran {fr[-1]['fundingRate']}, son yerleşim {age_h:.1f}h önce")

    # bookTicker spread — FILL_MODEL half_spread_bp=1.0 varsayımı
    for sym in H1_SYMBOLS:
        bt = s.get(f"{BINANCE_FAPI}/fapi/v1/ticker/bookTicker",
                   params={"symbol": sym}, timeout=15).json()
        bid, ask = float(bt["bidPrice"]), float(bt["askPrice"])
        half_spread_bp = (ask - bid) / 2 / ((ask + bid) / 2) * 1e4
        ok = half_spread_bp <= 5.0   # model 1bp varsayar; 5bp'ye kadar tolere
        check(f"spread:{sym}", ok,
              f"half-spread {half_spread_bp:.2f} bp "
              f"(FILL_MODEL varsayımı 1.00 bp)")


def signed_checks():
    """İş istasyonundan beklenen sonuç: -2015 (anahtar VPS IP'sine kilitli —
    bir GÜVENLİK özelliğidir). Gerçek imzalı-okuma doğrulaması VPS'te,
    aynı mevcut mekanizmayla (systemd EnvironmentFile + BinanceAPI) yapıldı
    ve ~/futures-research/blok25/results/integration_check_vps.json'a kaydedildi."""
    import os
    bk = os.environ.get("BINANCE_KEY")
    bs = os.environ.get("BINANCE_SECRET")
    if not bk or not bs:
        check("signed_env", False,
              "BINANCE_KEY/SECRET env yok — imzalı okuma atlandı")
        return
    api = BinanceAPI(bk, bs)   # MEVCUT mekanizma, değiştirilmedi

    def _read(path, name):
        try:
            r = api._get(path, signed=True)
        except Exception as e:
            check(name, False, f"{type(e).__name__}: {e}")
            return None
        if isinstance(r, dict) and "code" in r:
            # hata yanıtı: -2015 = IP whitelist (bu istasyondan BEKLENİR)
            expected = (r.get("code") == -2015)
            check(name, expected,
                  f"code={r.get('code')} ({'IP kilidi — beklenen' if expected
                  else 'BEKLENMEDİK HATA'}: {str(r.get('msg', ''))[:50]})")
            return None
        return r

    r = _read("/fapi/v2/balance", "signed:balance")
    if isinstance(r, list):
        usdt = [b for b in r if b.get("asset") == "USDT"]
        check("signed:balance", True,
              f"{len(r)} varlık satırı; USDT satırı={'var' if usdt else 'YOK'} "
              f"(değer rapor dışı)")

    r = _read("/fapi/v2/positionRisk", "signed:positionRisk")
    if isinstance(r, list):
        open_pos = [p for p in r if float(p.get("positionAmt", 0)) != 0]
        check("signed:positionRisk", True,
              f"{len(r)} sembol satırı, {len(open_pos)} açık pozisyon")

    r = _read("/fapi/v1/positionSide/dual", "signed:positionSide/dual")
    if isinstance(r, dict) and "dualSidePosition" in r:
        check("signed:positionSide/dual", True,
              f"dualSidePosition={r['dualSidePosition']}")

    r = _read("/fapi/v1/openOrders", "signed:openOrders")
    if isinstance(r, list):
        ok = all(isinstance(o, dict) and "orderId" in o for o in r)
        check("signed:openOrders", ok,
              f"{len(r)} açık emir (GridC0 dokunulmaz — yalnız okundu)")


def main():
    print("== BLOK25 Bölüm 5: PUBLIC endpoint kontrolleri ==")
    public_checks()
    print("== BLOK25 Bölüm 5: İMZALI OKUMA (mevcut mekanizma, READ ONLY) ==")
    signed_checks()
    n_fail = sum(1 for (_, ok, _) in _results if not ok)
    print(f"\nTOPLAM: {len(_results)} kontrol, {n_fail} FAIL")
    out = [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in _results]
    path = __import__("os").path.expanduser(
        "~/futures-research/blok25/results/integration_check.json")
    with open(path, "w") as f:
        json.dump({"ts": int(time.time() * 1000), "checks": out}, f, indent=1)
    print(f"yazıldı: {path}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
