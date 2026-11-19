"""faz3_data.py — FAZ3 arşiv indirici (data.binance.vision, USDT-M monthly klines).

ÖN-KAYIT sözleşmesi (PREREGISTRATION_FAZ3.md §2):
  * Her zip, yanındaki .CHECKSUM (SHA256) ile DOĞRULANIR; uyuşmazlık → dosya
    reddedilir (ERROR) ve asla parse edilmez.
  * 404 = ay yok (listing öncesi / veri öncesi) → MISSING olarak manifest'e
    yazılır; delisting de manifest'te görünür kalır (evren-dışı BIRAKILMAZ).
  * Depo Git-DIŞIDIR: /home/reisfurkan/faz9-audit/raw/klines/... (audit alanı).
  * Açık CDN; kimlik yok; nazik sıralı indirme + kısa bekleme. 429/403 →
    Retry-After'a uy, DURDUR (Mission C rate-limit doktrini).

Kullanım: python3 faz3_data.py [--root DİZİN] [--only SYM,...] [--tfs 15m,1h,4h]
"""
import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
DEFAULT_ROOT = "/home/reisfurkan/faz9-audit/raw/klines"

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
          "ADAUSDT", "DOGEUSDT", "LINKUSDT"]


def load_universe_a(path):
    syms = [l.strip().upper() for l in open(path) if l.strip()]
    return syms


def months(frm, to):
    y, m = int(frm[:4]), int(frm[5:7])
    out = []
    while (y, m) <= (int(to[:4]), int(to[5:7])):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "faz3-research/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def fetch_one(sym, tf, ym, root, log):
    """Tek (sym,tf,ay) indirir. DÖNER: manifest kaydı (status: OK|MISSING|ERROR)."""
    name = "%s-%s-%s.zip" % (sym, tf, ym)
    dest_dir = os.path.join(root, sym, tf)
    dest = os.path.join(dest_dir, ym + ".csv")
    if os.path.exists(dest):
        return {"sym": sym, "tf": tf, "ym": ym, "status": "CACHED"}
    for attempt in (1, 2, 3):
        try:
            with _get(BASE + "/%s/%s/%s" % (sym, tf, name)) as r:
                if r.status == 404:
                    break
                blob = r.read()
            try:
                with _get(BASE + "/%s/%s/%s.CHECKSUM" % (sym, tf, name)) as r:
                    ck = r.read().decode().strip()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"sym": sym, "tf": tf, "ym": ym,
                            "status": "ERROR", "why": "checksum-yok"}
                raise
            # Biçim (doğrulanmış): "<sha256>  <ad>" — her iki sıralamaya da
            # toleranslı: 64-hex olan tek token alınır.
            toks = ck.replace(",", " ").split()
            hexes = [t.lower() for t in toks
                     if len(t) == 64 and all(c in "0123456789abcdefABCDEF" for c in t)]
            if len(hexes) != 1:
                return {"sym": sym, "tf": tf, "ym": ym, "status": "ERROR",
                        "why": "checksum-bozuk"}
            want = hexes[0]
            got = sha256(blob)
            if want != got:
                log("CHECKSUM UYUŞMAZ %s (%s != %s) — dosya REDDEDİLDİ" % (name, got, want))
                return {"sym": sym, "tf": tf, "ym": ym,
                        "status": "ERROR", "why": "checksum"}
            zf = zipfile.ZipFile(io.BytesIO(blob))
            inner = zf.namelist()[0]
            csv = zf.read(inner)
            os.makedirs(dest_dir, exist_ok=True)
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(csv)
            os.replace(tmp, dest)
            rows = csv.count(b"\n")
            return {"sym": sym, "tf": tf, "ym": ym, "status": "OK",
                    "bytes": len(csv), "rows": rows}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"sym": sym, "tf": tf, "ym": ym, "status": "MISSING"}
            if e.code in (403, 429):
                wait = int(e.headers.get("Retry-After") or 60)
                log("RATE/BAN %s %s — %dsn DUR" % (e.code, name, wait))
                time.sleep(wait)
                continue
            log("http %s %s (deneme %s)" % (e.code, name, attempt))
        except Exception as e:
            log("hata %s: %s (deneme %s)" % (name, e, attempt))
        time.sleep(2 * attempt)
    return {"sym": sym, "tf": tf, "ym": ym, "status": "ERROR", "why": "fail"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--universe-file", default="/home/reisfurkan/faz9-audit/raw/streams/algo_sembol_evreni.txt")
    ap.add_argument("--only", default=None, help="virgülle sembol filtresi")
    ap.add_argument("--tfs", default="15m,1h,4h")
    ap.add_argument("--a-from", default="2024-01", help="Evren A başlangıç")
    ap.add_argument("--b-from", default="2022-01", help="Evren B başlangıç")
    ap.add_argument("--to", default="2026-10")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    tfs = a.tfs.split(",")
    ev_a = load_universe_a(a.universe_file)
    if a.only:
        keep = {s.strip().upper() for s in a.only.split(",")}
        ev_a = [s for s in ev_a if s in keep]
    plan = [(s, months(a.a_from, a.to)) for s in ev_a] + \
           [(s, months(a.b_from, a.to)) for s in
            (MAJORS if not a.only else [m for m in MAJORS if m in {x.strip().upper() for x in a.only.split(",")}])]
    # majors evren-A'da zaten varsa tekrar yok
    seen = set()
    tasks = []
    for sym, ms in plan:
        for ym in ms:
            for tf in tfs:
                k = (sym, tf, ym)
                if k not in seen:
                    seen.add(k)
                    tasks.append(k)
    log = lambda *x: (print(*x, flush=True))
    from concurrent.futures import ThreadPoolExecutor
    results = []
    t0 = time.time()
    done = [0]
    import threading
    lk = threading.Lock()

    def work(k):
        rec = fetch_one(k[0], k[1], k[2], a.root, log)
        with lk:
            done[0] += 1
            if done[0] % 200 == 0:
                log("[%d/%d] %.0fs" % (done[0], len(tasks), time.time() - t0))
        return rec

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for rec in ex.map(work, tasks):
            results.append(rec)
    mp = os.path.join(a.root, "manifest.json")
    old = []
    if os.path.exists(mp):
        old = json.load(open(mp))
    idx = {(r["sym"], r["tf"], r["ym"]): r for r in old + results}
    os.makedirs(a.root, exist_ok=True)
    with open(mp + ".tmp", "w") as fh:
        json.dump(sorted(idx.values(), key=lambda r: (r["sym"], r["tf"], r["ym"])),
                  fh, indent=1)
    os.replace(mp + ".tmp", mp)
    from collections import Counter
    log("BİTTİ %.0fs" % (time.time() - t0), Counter(r["status"] for r in results))
    errs = [r for r in results if r["status"] == "ERROR"]
    if errs:
        log("ERROR'lar:", errs[:10])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
