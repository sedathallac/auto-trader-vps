"""tools/orphan_report.py — P0-6: salt-okur yetim (pozisyon ↔ algo-SL) raporu.

KULLANIM (VPS):  python3 -m tools.orphan_report [--pid PID] [--db YOL]
GÜVENCELER:
  * YALNIZ imzalı GET. Kod yolunda POST/PUT/DELETE/iptal/gönderim YOK
    (test kaynak-taraması + transport-kaydedici ile kilitli).
  * "SL yok" hükmü YALNIZ algo service'ten (A-1 doktrini): allOrders koşullu
    emirleri GÖSTERMEZ — yokluk hükmü oradan çıkarılmaz.
  * Ledger dosyası mode=ro açılır; hiçbir yazım/temizlik yapılmaz.
AÇIK KAPSAM NOTU: openAlgoOrders sembol-bazlıdır; yetim-SL taraması
POZİSYONU OLAN + LEDGER'DA PLACED/PENDING İZİ BULUNAN sembollerle sınırlıdır
(botta izlenmemiş sembolde SL oluşamaz varsayımı). 947-sembol taraması
rate-limit doktrini gereği kasıtlı YOK.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import hashlib
import hmac

STOP_TYPES = ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT")
LIVE_ALGO = ("PENDING", "NEW", "PARTIALLY_FILLED", "WORKING", "ACTIVE", "LIVE")


def mbx_signed_get(base, path, api_key, secret, params=None):
    p = dict(params or {})
    p.setdefault("timestamp", int(time.time() * 1000))
    p.setdefault("recvWindow", 5000)
    q = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{base}{path}?{q}&signature={sig}", headers={"X-MBX-APIKEY": api_key})
    with urllib.request.urlopen(req, timeout=20) as r:      # GET-only (urlopen default)
        return json.loads(r.read().decode())


def analyze(positions, algos_by_sym, ledger_rows):
    """Safer birleştirme. Döner dict: rows + verdict. Hesaplamalar salt-oku."""
    pos = {}
    for p in positions:
        try:
            amt = float(p.get("positionAmt") or 0.0)
        except (TypeError, ValueError):
            continue
        if amt != 0.0:
            pos[p["symbol"]] = {"amt": amt, "entry": p.get("entryPrice")}
    led = {}
    for r in ledger_rows:
        led.setdefault(r["symbol"], []).append(dict(r))
    syms = sorted(set(pos) | set(led))
    rows, no_sl, orphan_sl, drift = [], [], [], []
    for s in syms:
        # ŞEMA (ham kanıt algo_orders_20261119.ndjson): alan ADI algoStatus;
        # "status" normal emir şeması — ikisini de tolere et.
        al = [a for a in algos_by_sym.get(s, [])
              if (str(a.get("algoStatus", "") or a.get("status", ""))
                  .upper() in LIVE_ALGO)]
        stop_algos = [a for a in al if str(a.get("orderType", "")).upper() in STOP_TYPES]
        placed = [r for r in led.get(s, []) if r["state"] == "PLACED"]
        kind = []
        if s in pos and not stop_algos:
            kind.append("SL_YOK")               # hükmün KAYNAĞI: algo service
            no_sl.append(s)
        if s not in pos and stop_algos:
            kind.append("YETIM_SL")
            orphan_sl.append(s)
        for r in placed:
            ids = {str(a.get("algoId")) for a in al}
            if r.get("algo_id") and str(r["algo_id"]) not in ids:
                kind.append("KİMLİK-UYUŞMAZ")
                drift.append(s)
                break
        rows.append({"symbol": s, "pos_amt": pos.get(s, {}).get("amt"),
                     "sl_algo": len(stop_algos), "ledger_placed": len(placed),
                     "bulgu": ",".join(kind) or "-"})
    n = len(no_sl) + len(orphan_sl) + len(drift)
    verdict = ("VERDICT: TEMİZ — 0 yetim/0 SL'siz/0 kimlik-uyuşmaz" if n == 0
               else f"VERDICT: {len(no_sl)} SL'siz-pozisyon, {len(orphan_sl)} "
                    f"yetim-SL, {len(drift)} kimlik-uyuşmaz — İNSAN incele "
                    f"(bu araç HİÇBİR ŞEY DÜZELTMEZ)")
    return {"rows": rows, "verdict": verdict}


def _read_ledger(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT symbol, token, state, algo_id, client_algo_id, gen "
            "FROM sl_placements WHERE state IN ('PENDING','PLACED')")]
    except sqlite3.OperationalError:
        return []          # tablolar yoksa: boş evren (salt-okur, dokunma yok)
    finally:
        con.close()


def collect(get, ledger_rows, sleep=time.sleep):
    """Salt-GET toplayıcı: get callable YALNIZ GET uygular; test sahte-GET
    kaydediciyle doğrular. Döner (positions, algos_by_sym)."""
    positions = get("/fapi/v2/positionRisk")
    if not isinstance(positions, list):
        raise RuntimeError(f"positionRisk liste değil: {positions!r:.120}")
    syms = {p["symbol"] for p in positions if float(p.get("positionAmt") or 0)} \
        | {r["symbol"] for r in ledger_rows}
    algos = {}
    for s in sorted(syms):
        try:
            algos[s] = get("/fapi/v1/openAlgoOrders", {"symbol": s}) or []
        except Exception as e:
            algos[s] = []
            print(f"UYARI: openAlgoOrders {s}: {type(e).__name__}", file=sys.stderr)
        sleep(0.15)                            # nazik sıra (429/418 doktrini)
    return positions, algos


def _env_from_proc(pid):
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            for kv in fh.read().split(b"\0"):
                if b"=" in kv:
                    k, v = kv.split(b"=", 1)
                    env[k.decode()] = v.decode()
    except OSError as e:
        print(f"HATA: /proc/{pid}/environ: {e}", file=sys.stderr)
        sys.exit(2)
    return env


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--db", default="/var/lib/auto_trader_faz9_live/risk_ledger.db")
    ap.add_argument("--base", default="https://fapi.binance.com")
    a = ap.parse_args(argv)
    if a.pid is None:
        print("HATA: --pid gerekli (canlı servis pid; salt-okur /proc env).",
              file=sys.stderr)
        return 2
    env = _env_from_proc(a.pid)
    key, sec = env.get("BINANCE_API_KEY", ""), env.get("BINANCE_API_SECRET", "")
    if not key or not sec:
        print("HATA: env'de anahtar yok (yazdırılmaz).", file=sys.stderr)
        return 2
    get = lambda path, params=None: mbx_signed_get(a.base, path, key, sec, params)
    led = _read_ledger(a.db)
    try:
        positions, algos = collect(get, led)
    except Exception as e:
        print(f"HATA: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    out = analyze(positions, algos, led)
    print(f"{'SEMBOL':<14} {'POZİSYON':>12} {'ALGO-SL':>8} {'LEDGER':>7}  BULGU")
    for r in out["rows"]:
        print(f"{r['symbol']:<14} {str(r['pos_amt']):>12} {r['sl_algo']:>8} "
              f"{r['ledger_placed']:>7}  {r['bulgu']}")
    print(out["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
