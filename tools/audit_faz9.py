#!/usr/bin/env python3
"""tools/audit_faz9.py — FAZ9 canlı sistem READ-ONLY denetim (tek geçiş).

TASARIM KURALLARI (önceki taslağın 10 kusurunun düzeltilmişi):
  * auto_trader'ı İMPORT ETMEZ (modül düzeyi yan etkileri yok sayılamaz) —
    imzalama burada, bağımsız, stdlib.
  * env canlı servis prosesinden okunur (/proc/<pid>/environ); HİÇBİR sır
    yazdırılmaz (yalnız var/yok + uzunluk).
  * Binance base URL env'den (BINANCE_FUTURES_BASE_URL).
  * Algo SL uç noktası KODDA KANITLI olan: GET /fapi/v1/openAlgoOrders
    (/sapi/v1/algo/futures/openOrders DEĞİL).
  * sqlite: file:...?mode=ro + PRAGMA query_only=ON. immutable= YOK,
    wal_checkpoint YOK (yazma yok).
  * HTTP yalnız GET (Binance). HB /executors/search statik olarak doğrulanmış
    tek uçtur; POST ama salt-okur sorgudur (emir/degisiklik yoktur).
  * Her bölüm VERIFIED / PARTIAL / UNKNOWN etiketiyle basılır; veri yoksa
    uydurulmaz.

KULLANIM (VPS, root):
    python3 tools/audit_faz9.py --db /var/lib/auto_trader_faz9_live/risk_ledger.db \
        [--pid <servis-pid>] [--since-days 30]
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LABELS = []  # (section, label)


def emit(section, label, text):
    LABELS.append((section, label))
    print(f"[{label}] {section}: {text}")


def _find_service_pid():
    """/proc taraması ile auto_trader.py çalıştıran ilk pid."""
    for pid in sorted(os.listdir("/proc")):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace")
            if "auto_trader.py" in cmd:
                return int(pid)
        except OSError:
            continue
    return None


def load_env_from_proc(pid):
    env = {}
    path = f"/proc/{pid}/environ"
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        for item in raw.split(b"\0"):
            if not item or b"=" not in item:
                continue
            k, _, v = item.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    except OSError as e:
        emit("ENV", "UNKNOWN", f"/proc/{pid}/environ okunamadi: {e}")
        sys.exit(2)
    return env


def secret_presence(env, name):
    v = env.get(name, "")
    return f"VAR(len={len(v)})" if v else "YOK"


def mbx_signed_get(base, path, api_key, secret, params=None):
    """HMAC-SHA256 imzalı GET — recvWindow 60000, timeout 15 (bot ile ayni)."""
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 60000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
        if isinstance(data, dict) and "code" in data:
            return None, f"api-error code={data.get('code')} msg={data.get('msg')}"
        return data, None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def open_ro_sqlite(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error as e:
        return None if "no such" in str(e).lower() else (_ for _ in ()).throw(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="risk_ledger.db yolu (salt-okur)")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--hb-url", default=None,
                    help="HB API base (default: env HB_API veya kod sabiti)")
    args = ap.parse_args()

    pid = args.pid or _find_service_pid()
    if not pid:
        emit("ENV", "UNKNOWN", "auto_trader servisi calisan pid bulunamadi (--pid ver)")
        sys.exit(2)
    env = load_env_from_proc(pid)
    print(f"== ENV (pid={pid}) ==")
    print(f"  BINANCE_KEY={secret_presence(env,'BINANCE_KEY')} "
          f"BINANCE_SECRET={secret_presence(env,'BINANCE_SECRET')} "
          f"HB_USERNAME={secret_presence(env,'HB_USERNAME')} "
          f"HB_PASSWORD={secret_presence(env,'HB_PASSWORD')}")
    base = env.get("BINANCE_FUTURES_BASE_URL") or "https://fapi.binance.com"
    key, sec = env.get("BINANCE_KEY", ""), env.get("BINANCE_SECRET", "")
    if not key or not sec:
        emit("BINANCE", "UNKNOWN", "env'de BINANCE_KEY/SECRET yok")

    # ---- Binance: pozisyonlar + algo SL (kodda kanitli uc: /fapi/v1/openAlgoOrders)
    pos, err = mbx_signed_get(base, "/fapi/v2/positionRisk", key, sec) if key else (None, "keysiz")
    if err:
        emit("BINANCE.POSITIONS", "UNKNOWN", err)
        live_syms = {}
    else:
        live = [p for p in pos if float(p.get("positionAmt") or 0) != 0]
        live_syms = {p["symbol"]: float(p["positionAmt"]) for p in live}
        upnl = sum(float(p.get("unRealizedProfit") or 0) for p in live)
        emit("BINANCE.POSITIONS", "VERIFIED",
             f"{len(live)} acik pozisyon, UPNL toplam {upnl:+.8f} "
             f"({', '.join(live_syms) or '-'})")

    algo, err = mbx_signed_get(base, "/fapi/v1/openAlgoOrders", key, sec) if key else (None, "keysiz")
    if err:
        emit("BINANCE.ALGO_SL", "UNKNOWN", err)
        algo_syms = set()
    else:
        rows = algo if isinstance(algo, list) else (algo or {}).get("orders", [])
        algo_syms = {r.get("symbol") for r in rows}
        emit("BINANCE.ALGO_SL", "VERIFIED",
             f"{len(rows)} acik algo SL ({', '.join(sorted(s for s in algo_syms if s)) or '-'})")
    if live_syms:
        unprotected = sorted(set(live_syms) - algo_syms)
        emit("GATE.UNPROTECTED", "VERIFIED" if not unprotected else "PARTIAL",
             f"SL'siz acik sembol: {unprotected or 'YOK'}")

    # ---- Income (komisyon/funding/realized) — tek pencere
    since_ms = int((time.time() - args.since_days * 86400) * 1000)
    inc, err = mbx_signed_get(base, "/fapi/v1/income", key, sec,
                              {"startTime": since_ms, "limit": 1000}) if key else (None, "keysiz")
    income_by_type = {}
    if err or inc is None:
        emit("BINANCE.INCOME", "UNKNOWN", err or "bos")
    else:
        if len(inc) >= 1000:
            emit("BINANCE.INCOME", "PARTIAL", "1000 satir limiti — pencere buyut")
        for row in inc:
            t = row.get("incomeType")
            a, b = income_by_type.setdefault(t, [0, 0.0])
            income_by_type[t] = [a + 1, b + float(row.get("income") or 0)]
        for t, (n, s) in sorted(income_by_type.items()):
            print(f"  {t}: n={n} total={s:+.8f}")
        emit("BINANCE.INCOME", "PARTIAL" if len(inc) >= 1000 else "VERIFIED",
             f"{len(inc)} kayit, son {args.since_days} gun")

    # ---- Ledger (salt-okur)
    if not os.path.exists(args.db):
        emit("LEDGER", "UNKNOWN", f"{args.db} yok")
    else:
        conn = open_ro_sqlite(args.db)
        try:
            ledger = q(conn, "SELECT executor_id, trading_pair, net_pnl_quote, closed_at, source "
                             "FROM trade_ledger") or []
            if not ledger:
                emit("LEDGER", "UNKNOWN", "trade_ledger yok/bos")
            net = sum(float(r["net_pnl_quote"] or 0) for r in ledger)
            zeros = sum(1 for r in ledger if float(r["net_pnl_quote"] or 0) == 0)
            srcs = {}
            for r in ledger:
                srcs[r["source"]] = srcs.get(r["source"], 0) + 1
            emit("LEDGER.NET", "VERIFIED",
                 f"{len(ledger)} satir, net {net:+.8f}, sifir-PnL {zeros}, "
                 f"source dagilimi {srcs}")
            sym = {}
            for r in ledger:
                s = (r["trading_pair"] or "").replace("-", "")
                sym[s] = sym.get(s, 0.0) + float(r["net_pnl_quote"] or 0)
            # Binance realized+commission+funding per symbol vs ledger karsilastirmasi
            if inc:
                bin_sym = {}
                for row in inc:
                    if row.get("incomeType") == "TRANSFER":
                        continue
                    s = row.get("symbol")  # income.symbol zaten 'BTCUSDT' biciminde
                    bin_sym[s] = bin_sym.get(s, 0.0) + float(row.get("income") or 0)
                diffs = {s: round(sym.get(s, 0.0) - v, 8)
                         for s, v in bin_sym.items() if abs(sym.get(s, 0.0) - v) > 1e-8}
                emit("LEDGER.SYMBOL_DELTA", "PARTIAL" if diffs else "VERIFIED",
                     f"ledger-binance farki olan sembol: {diffs or 'YOK'} "
                     f"(farklar UNKNOWN birakildi; ledger'a dokunulmadi)")
            meta = q(conn, "SELECT last_close_ts, updated_at FROM sync_meta WHERE id=1")
            emit("SYNC.WATERMARK", "VERIFIED" if meta else "UNKNOWN",
                 f"{meta[0] if meta else 'yok'}")
            rs = q(conn, "SELECT daily_trades, daily_pnl, consecutive_losses, "
                         "is_halted, last_reset_date, updated_at FROM risk_state WHERE id=1")
            emit("RISK.STATE", "VERIFIED" if rs else "UNKNOWN", f"{rs[0] if rs else 'yok'}")
            sl = q(conn, "SELECT state, COUNT(*) n FROM sl_placements GROUP BY state")
            if sl is None:
                emit("SL.PLACEMENTS", "UNKNOWN", "tablo yok")
            else:
                dead_placed = q(conn, "SELECT symbol FROM sl_placements WHERE state IN ('PENDING','PLACED')") or []
                orphan = sorted(r["symbol"] for r in dead_placed if r["symbol"] not in live_syms)
                emit("SL.PLACEMENTS", "PARTIAL" if orphan else "VERIFIED",
                     f"dagilim={[ (r['state'], r['n']) for r in sl ]}, "
                     f"acik-pozisyonu-olmayan aktif kayitlar: {orphan or 'YOK'}")
        finally:
            conn.close()

    # ---- HB (tek statik-dogrulanmis uc, salt-okur sorgu)
    hb_url = args.hb_url or env.get("HB_API") or "http://127.0.0.1:8000"
    hb_user, hb_pw = env.get("HB_USERNAME", ""), env.get("HB_PASSWORD", "")
    if hb_user:
        import base64
        tok = base64.b64encode(f"{hb_user}:{hb_pw}".encode()).decode()
        req = urllib.request.Request(f"{hb_url}/executors/search", data=b"{}",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Basic {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, list):
                term = sum(1 for e in data if e.get("status") == "TERMINATED")
                stale = sum(1 for e in data if e.get("status") == "RUNNING"
                            and e.get("is_trading") is False)
                emit("HB.EXECUTORS", "VERIFIED",
                     f"{len(data)} exec, {term} TERM, {stale} RUNNING-notrading")
            else:
                emit("HB.EXECUTORS", "UNKNOWN", f"beklenmeyen govde: {list(body)[:5]}")
        except Exception as e:
            emit("HB.EXECUTORS", "UNKNOWN", f"{type(e).__name__}: {e}")
    else:
        emit("HB.EXECUTORS", "UNKNOWN", "HB_USERNAME env'de yok")

    ok = sum(1 for _, l in LABELS if l == "VERIFIED")
    print(f"\n== OZET: {ok} VERIFIED / "
          f"{sum(1 for _, l in LABELS if l=='PARTIAL')} PARTIAL / "
          f"{sum(1 for _, l in LABELS if l=='UNKNOWN')} UNKNOWN ==")


if __name__ == "__main__":
    main()
