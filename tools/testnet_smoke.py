"""testnet_smoke.py — BLOK11 §9: yalnız-Binance-TESTNET devre doğrulaması.

AMAÇ: anahtarların TESTNET anahtarları olduğunu ve imzalı REST zincirinin
canlı YOL AYNI kodla konuştuğunu kanıtlamak. ÜRETİME TEK BİT GİTMEZ: base URL
kodda SABİTTİR (testnet.binancefuture.com); production anahtarları ortamda
VARSA koşu REDDEDİLİR (yanlış-anahtar kontaminasyonu).

VARVARSAYILAN DAVRANIŞ (varsayılan): yalnız ping + signed account read.
Emir YALNIZ --place-order açık bayrağıyla: ETHUSDT değil, sembol verilir;
fiyat son fiyattan %5 uzağa limit, sonra derhal cancel; azaltma YOK (pozisyon
açma riski yok — limit uzakta durur, iptal edilir). Yine de testnet emri test
hesabına işlenir: operatör onayı + risk notu gerektirir.

Bu görevde TESTNET_API_KEY/TESTNET_SECRET bu makinede YOK → gerçek ağ turu
YAPILMADI (görev §13 blokajı). Testler transport enjekte eder; ağa tek istek
yoktur. Kimlikler HİÇ loglanmaz/yazılmaz.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

TESTNET_BASE = "https://testnet.binancefuture.com"   # SABİT — üretime geçiş yok
PROD_KEY_NAMES = ("BINANCE" + "_" + "KEY", "BINANCE" + "_" + "SECRET")


class SmokeRefused(Exception):
    pass


def precheck(env):
    """Sıra önemli: önce üretim anahtarı RED (kontaminasyon), sonra testnet
    anahtarının YOKLUĞU (blokaj). Döner: (key, secret)."""
    for nm in PROD_KEY_NAMES:
        if env.get(nm):
            raise SmokeRefused(
                "ortamda üretim anahtarı %r var — smoke yalnız TESTNET_* "
                "anahtarlarıyla koşar (yanlış hedef koruması)" % nm)
    k = env.get("TESTNET_API_KEY")
    s = env.get("TESTNET_API_SECRET")
    if not k or not s:
        raise SmokeRefused(
            "TESTNET_API_KEY/TESTNET_API_SECRET yok — testnet smoke YAPILAMAZ "
            "(görev §13 blokajı; operatör anahtarı gerekir)")
    return k, s


def order_approval(env):
    """BLOK12 §7 — --place-order AYRI, KALICI operatör onayı bekliyor.

    Operatör 0600 izinli bir işaret dosyası yazar (içeriği hiçbir yere
    basılmaz) ve TESTNET_ORDER_APPROVAL_FILE ile işaretler. Dosya yok /
    izinler gevşek / boş → RED (BLOCKED). KIMLIK SOHBETTE ISTENMEZ:
    anahtarlar yalnız korumalı env veya secret store ile sağlanır."""
    fp = env.get("TESTNET_ORDER_APPROVAL_FILE")
    if not fp:
        raise SmokeRefused(
            "OPERATÖR-ONAYI-BEKLENIYOR: testnet emir testi AYRI onay "
            "ister — TESTNET_ORDER_APPROVAL_FILE (0600 işaret dosyası) "
            "ayarlanmadan --place-order koşmaz")
    try:
        st = os.stat(fp)
    except OSError:
        raise SmokeRefused(
            "OPERATÖR-ONAYI dosyası yok/okunamıyor — emir testi BLOCKED")
    if st.st_mode & 0o077:
        raise SmokeRefused(
            "OPERATÖR-ONAYI dosya izinleri gevşek (chmod 600 gerekir) — RED")
    if st.st_size == 0:
        raise SmokeRefused("OPERATÖR-ONAYI dosyası boş — RED")
    return True


def _signed_params(secret, params):
    q = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig


def rest_get(path, params, key, secret, fetch=None, recv_window=5000):
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = recv_window
    url = TESTNET_BASE + path + "?" + _signed_params(secret, params)
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    if fetch is None:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    return fetch(req)


def rest_post(path, params, key, secret, fetch=None, recv_window=5000):
    q = _signed_params(secret, dict(params,
                                      timestamp=int(time.time() * 1000),
                                      recvWindow=recv_window))
    req = urllib.request.Request(TESTNET_BASE + path, data=q.encode(),
                                 headers={"X-MBX-APIKEY": key}, method="POST")
    if fetch is None:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    return fetch(req)


def run_smoke(env, symbol="BTCUSDT", place_order=False, price=None,
              qty="0.001", fetch=None, sleep=lambda s: None):
    """Tek tur. fetch(req)->dict enjekte edilebilir (test). Döner: adım listesi.
    Adımlar: ping → account(read) → [--place-order: price-get → limit(uzak) →
    query → cancel → confirm]."""
    key, secret = precheck(env)
    steps = []
    if fetch is None:
        with urllib.request.urlopen(TESTNET_BASE + "/fapi/v1/ping",
                                    timeout=10) as r:
            assert r.status == 200
    else:
        fetch({"url": TESTNET_BASE + "/fapi/v1/ping", "method": "GET"})
    steps.append("ping OK (%s)" % TESTNET_BASE)
    acc = rest_get("/fapi/v1/account", {}, key, secret, fetch=fetch)
    if not isinstance(acc, dict) or "totalWalletBalance" not in acc:
        raise SmokeRefused("account beklenmedik yanıt (anahtar testnet mi?)")
    steps.append("account OK: testnet bakiye alanı görüldü (değer loglanmaz)")
    if place_order:
        # BLOK12 §7: ping+account salt-okunur; EMİR ayrı operatör onayına
        # bağlıdır (onaysız bu satırdan sonrası ASLA koşmaz).
        order_approval(env)
        if fetch is None:
            with urllib.request.urlopen(TESTNET_BASE + "/fapi/v1/ticker/price"
                                        "?symbol=" + symbol, timeout=10) as r:
                px = float(json.loads(r.read().decode())["price"])
        else:
            px = float(fetch({"url": TESTNET_BASE + "/fapi/v1/ticker/price"
                              "?symbol=" + symbol, "method": "GET"})["price"])
        far = round((price if price is not None else px * 0.95), 2)
        cid = "fz9s%s%d" % (symbol, int(time.time() * 1000))
        resp = rest_post("/fapi/v1/order",
                         {"symbol": symbol, "side": "BUY", "type": "LIMIT",
                          "timeInForce": "GTC", "quantity": qty,
                          "price": "%g" % far, "newClientOrderId": cid},
                         key, secret, fetch=fetch)
        oid = resp.get("orderId") if isinstance(resp, dict) else None
        if oid is None:
            raise SmokeRefused("testnet limit reddedildi: %r" % (resp,))
        steps.append("limit OK (%s, uzak fiyat, cid=fz9s…)" % symbol)
        sleep(1)
        rest_get("/fapi/v1/order", {"symbol": symbol, "orderId": oid},
                 key, secret, fetch=fetch)
        steps.append("query OK")
        cq = _signed_params(secret, {"symbol": symbol, "orderId": oid,
                                     "timestamp": int(time.time() * 1000)})
        creq = urllib.request.Request(
            TESTNET_BASE + "/fapi/v1/order", data=cq.encode(),
            headers={"X-MBX-APIKEY": key}, method="DELETE")
        if fetch is None:
            with urllib.request.urlopen(creq, timeout=10) as r:
                json.loads(r.read().decode())
        else:
            fetch(creq)
        steps.append("cancel OK — smoke tamam (pozisyon YOK, uzak limit)")
    return steps


def main(argv=None, env=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--place-order", action="store_true",
                    help="uzak-limit koy→sorgula→iptal (testnet; operatör "
                         "onayı + risk notu gerektirir)")
    a = ap.parse_args(argv)
    try:
        for s in run_smoke(os.environ if env is None else env,
                           symbol=a.symbol, place_order=a.place_order):
            print("SMOKE OK:", s)
    except SmokeRefused as e:
        print("RED:", e)
        return 2
    print("NOT: TESTNET SONUCU — ÜRETİM KANITI DEĞİLDİR (base URL kodda "
          "sabit; anahtar kümesi ayrı; üretim davranışını kanıtlamaz)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
