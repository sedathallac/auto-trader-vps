"""mark_price_observer.py — BLOK26 Bölüm 4: gerçek Mark Price verisi.

Binance USDⓈ-M Futures PUBLIC Mark Price — credential YOK, yalnız public
market data. İki taşıma modu:

  WS  (wss://fstream.binance.com, <symbol>@markPrice@1s):
      DOKÜMANTASYONUYLA doğrulandı ANKARA/VPS'den ÇALIŞMIYOR — 101
      handshake kuruluyor ama tek kare bile gelmiyor (aggTrade,
      markPrice, markPrice@1s, !markPrice@arr; hem istasyon hem VPS
      raw-socket testiyle). Spot WS çalışıyor, futures REST çalışıyor →
      bölgesel engel. Ayrıntı: BLOK26_MARK_PRICE_STREAM_PLAN.md.

  REST (GET /fapi/v1/premiumIndex, sembolsüz = tüm semboller, weight 10):
      CANLI DOĞRULANDI (~0.5s/istek, 8 H1 sembolü tek yanıtta). VARSAYILAN
      MOD. Alanlar WS payload'ı ile birebir eşleşir:
      {symbol, markPrice(p), indexPrice(i), lastFundingRate(r),
       nextFundingTime(T), time(E)}.

Kurallar (görev gereği):
  - BAĞIMSIZ observer: aktif paper runner'a BAĞLANMAZ, ona yazmaz
  - veri kaybında son fiyat sonsuza dek geçerli SAYILMAZ — önceden
    belgelenmiş eşikler:
      WS:   STALE_THRESHOLD_S       = 10 s  (1s akışında 10 güncelleme)
      REST: STALE_THRESHOLD_REST_S  = 60 s  (5s poll aralığının 12 katı;
            bir sembol 60s güncellenmediyse STALE olayı yazılır)
  - hata/reconnect: üstel backoff (1s→60s), öncesi/sonrası gap kaydedilir
  - append-only günlük JSONL + günlük SHA256 manifesti (yalnız bugünkü
    manifest güncellenir; eski günler değiştirilmez)
  - emir yok, credential yok, DB'ye paper yazımı yok

Kayıt yerleri:
  ~/futures-research/blok25/forward-monitor/markprice/YYYY-MM-DD.jsonl
  ~/futures-research/blok25/forward-monitor/markprice/YYYY-MM-DD.manifest.json
  ~/futures-research/blok25/forward-monitor/markprice/events.jsonl
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT", "DOGEUSDT",
           "LINKUSDT", "SOLUSDT", "XRPUSDT"]
WS_BASE = "wss://fstream.binance.com/stream?streams="
REST_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
STALE_THRESHOLD_S = 10.0          # DOCUMENTED stale threshold (1s WS akışı)
STALE_THRESHOLD_REST_S = 60.0     # DOCUMENTED stale threshold (REST poll)
POLL_INTERVAL_S = 5.0             # varsayılan REST poll aralığı
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
DEFAULT_DIR = Path("~/futures-research/blok25/forward-monitor/markprice"
                   ).expanduser()


def stream_url(symbols=SYMBOLS, freq="1s"):
    streams = "/".join(f"{s.lower()}@markPrice@{freq}" for s in symbols)
    return WS_BASE + streams


def _day(now_ms):
    return datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime(
        "%Y-%m-%d")


class MarkPriceRecorder:
    """Append-only JSONL + günlük SHA256 manifesti."""

    def __init__(self, directory=DEFAULT_DIR, stream="rest:premiumIndex"):
        self.dir = Path(directory)
        self.stream = stream
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._fh_day = None
        self._count = 0
        self._first_ts = None
        self._last_ts = None

    def _open(self, now_ms):
        day = _day(now_ms)
        if self._fh is None or self._fh_day != day:
            if self._fh is not None:
                self._flush_manifest()
                self._fh.close()
            self._fh = open(self._dir_file(day), "a")
            self._fh_day = day
            self._count = sum(1 for _ in open(self._dir_file(day)))
            self._first_ts = None
            self._last_ts = None
            if self._count:
                # son satırdan devam bilgisi al
                last = self._read_last_line(day)
                if last:
                    self._first_ts = self._read_first_line(day)["t"]
                    self._last_ts = last["t"]

    def _dir_file(self, day):
        return self.dir / f"{day}.jsonl"

    def _read_last_line(self, day):
        with open(self._dir_file(day), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = [l for l in f.read().decode().split("\n") if l.strip()]
        return json.loads(lines[-1]) if lines else None

    def _read_first_line(self, day):
        with open(self._dir_file(day)) as f:
            for line in f:
                if line.strip():
                    return json.loads(line)
        return None

    def record(self, tick):
        """tick: {t, E, s, p, i, r, T} — dosyaya append + flush."""
        self._open(tick["t"])
        self._fh.write(json.dumps(tick) + "\n")
        self._fh.flush()
        self._count += 1
        if self._first_ts is None:
            self._first_ts = tick["t"]
        self._last_ts = tick["t"]

    def _flush_manifest(self):
        if self._fh_day is None:
            return
        day = self._fh_day
        f = self._dir_file(day)
        h = hashlib.sha256()
        n = 0
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        man = {"date": day, "records": self._count or n,
               "first_ts": self._first_ts, "last_ts": self._last_ts,
               "sha256": h.hexdigest(), "stream": self.stream,
               "symbols": SYMBOLS}
        (self.dir / f"{day}.manifest.json").write_text(
            json.dumps(man, indent=1))

    def close(self):
        if self._fh is not None:
            self._flush_manifest()
            self._fh.close()
            self._fh = None

    def event(self, kind, detail, now_ms=None):
        """Reconnect/stale/gap olayları — ayrı append-only dosya."""
        rec = {"t": now_ms or int(time.time() * 1000), "kind": kind,
               "detail": detail}
        with open(self.dir / "events.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


def parse_tick(msg, recv_ms):
    """Combined stream mesajını tick'e çevir; beklenmeyen biçim → None."""
    try:
        d = json.loads(msg)
        data = d.get("data", d)
        if data.get("e") != "markPriceUpdate":
            return None
        return {"t": recv_ms, "E": int(data["E"]), "s": data["s"],
                "p": float(data["p"]), "i": float(data["i"]),
                "r": float(data["r"]), "T": int(data["T"])}
    except (ValueError, KeyError, TypeError):
        return None


# --- REST modu (varsayılan; WS bölgesel engelli — modül docstring'i) ------

def rest_tick(row, recv_ms):
    """premiumIndex satırını tick'e çevir. Alanlar WS payload'ıyla birebir:
    p=markPrice, i=indexPrice, r=lastFundingRate, T=nextFundingTime, E=time.
    Bozuk satır ValueError/KeyError fırlatır — sessizce veri uydurma YOK."""
    return {"t": recv_ms, "E": int(row["time"]), "s": row["symbol"],
            "p": float(row["markPrice"]), "i": float(row["indexPrice"]),
            "r": float(row["lastFundingRate"]),
            "T": int(row["nextFundingTime"])}


def fetch_all(symbols=SYMBOLS, timeout=10.0):
    """Tüm semboller tek istekte (weight 10); yalnız H1 sembollerini süz.
    Public endpoint — credential YOK."""
    import requests
    r = requests.get(REST_URL, timeout=timeout)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise ValueError(f"beklenmedik premiumIndex yanıtı: {type(rows)}")
    return {row["symbol"]: row for row in rows if row["symbol"] in symbols}


def stale_symbols(last_tick, now_ms, threshold_s):
    """Eşiği aşan semboller — önceden belgelenmiş stale kuralının kendisi
    (test edilebilir ayrı fonksiyon)."""
    return sorted(s for s, t in last_tick.items()
                  if (now_ms - t) / 1000 > threshold_s)


def run_rest(symbols=SYMBOLS, directory=DEFAULT_DIR, duration_s=None,
             interval=POLL_INTERVAL_S):
    """REST polling döngüsü: poll → kaydet → stale izle → hatada backoff."""
    rec = MarkPriceRecorder(directory, stream=f"rest:{REST_URL}")
    last_tick = {}          # symbol -> son başarılı poll ms
    backoff = BACKOFF_START
    deadline = (time.time() + duration_s) if duration_s else None
    rec.event("OBSERVER_START",
              f"REST {REST_URL} interval={interval}s "
              f"stale>{STALE_THRESHOLD_REST_S:.0f}s symbols={len(symbols)}")
    while True:
        t0 = time.time()
        if deadline and t0 > deadline:
            rec.event("OBSERVER_STOP", "duration reached")
            rec.close()
            return
        try:
            rows = fetch_all(symbols)
        except Exception as e:
            rec.event("DISCONNECTED", f"{type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        backoff = BACKOFF_START
        now_ms = int(time.time() * 1000)
        missing = [s for s in symbols if s not in rows]
        if missing:
            rec.event("MISSING_SYMBOLS", f"{missing}")
        for s, row in sorted(rows.items()):
            tick = rest_tick(row, now_ms)
            prev = last_tick.get(s)
            if prev is not None and \
                    (now_ms - prev) / 1000 > STALE_THRESHOLD_REST_S:
                rec.event("STALE_RECOVERED",
                          f"{s} gap {(now_ms - prev) / 1000:.0f}s")
            last_tick[s] = now_ms
            rec.record(tick)
        # son fiyatı sonsuza dek geçerli sayma: eşiği aşan sembolleri yaz
        stale_now = stale_symbols(last_tick, int(time.time() * 1000),
                                  STALE_THRESHOLD_REST_S)
        if stale_now:
            rec.event("STALE", f"no update >{STALE_THRESHOLD_REST_S:.0f}s: "
                               f"{stale_now}")
        # poll süresini hesaba kat: aralık istek süresinden başlar
        elapsed = time.time() - t0
        time.sleep(max(0.0, interval - elapsed))


def run_ws(symbols=SYMBOLS, directory=DEFAULT_DIR, duration_s=None,
           freq="1s"):
    """WS döngüsü — DOKÜMANTASYON amaçlı; fstream.binance.com bu ağdan
    kare göndermiyor (bkz. modül docstring'i ve STREAM_PLAN raporu)."""
    import websocket  # websocket-client; ping'lere otomatik pong
    rec = MarkPriceRecorder(directory, stream=f"ws:markPrice@{freq}")
    url = stream_url(symbols, freq)
    backoff = BACKOFF_START
    last_tick = {}          # symbol -> recv ms
    deadline = (time.time() + duration_s) if duration_s else None
    rec.event("OBSERVER_START", url)
    while True:
        if deadline and time.time() > deadline:
            rec.event("OBSERVER_STOP", "duration reached")
            rec.close()
            return
        try:
            ws = websocket.create_connection(url, timeout=5)
            rec.event("CONNECTED", f"backoff was {backoff:.0f}s")
            backoff = BACKOFF_START
            while True:
                if deadline and time.time() > deadline:
                    ws.close()
                    rec.event("OBSERVER_STOP", "duration reached")
                    rec.close()
                    return
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    msg = None
                now_ms = int(time.time() * 1000)
                if msg:
                    tick = parse_tick(msg, now_ms)
                    if tick:
                        prev = last_tick.get(tick["s"])
                        if prev is not None and \
                                (now_ms - prev) / 1000 > STALE_THRESHOLD_S:
                            rec.event("STALE_RECOVERED",
                                      f"{tick['s']} gap "
                                      f"{(now_ms - prev) / 1000:.0f}s")
                        last_tick[tick["s"]] = now_ms
                        rec.record(tick)
                    continue
                # recv timeout: stale kontrolü
                stale_now = [s for s, t in last_tick.items()
                             if (now_ms - t) / 1000 > STALE_THRESHOLD_S]
                if stale_now:
                    rec.event("STALE", f"no tick >{STALE_THRESHOLD_S:.0f}s: "
                                       f"{sorted(stale_now)}")
        except Exception as e:
            rec.event("DISCONNECTED", f"{type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


def stats(directory=DEFAULT_DIR, day=None):
    """Günlük istatistik: tick sayısı, aralık dağılımı, alan varlığı,
    manifest doğrulaması (eski günler için SHA256)."""
    d = Path(directory)
    if day is None:
        files = sorted(d.glob("*.jsonl"))
        files = [f for f in files if f.name != "events.jsonl"]
        day = files[-1].name[:-6] if files else None
    if day is None:
        return {"error": "no data"}
    f = d / f"{day}.jsonl"
    per_sym = {}
    fields_seen = set()
    prev_t = {}
    gaps = []
    for line in open(f):
        if not line.strip():
            continue
        t = json.loads(line)
        fields_seen.update(t.keys())
        s = t["s"]
        rec = per_sym.setdefault(s, {"ticks": 0, "first_t": t["t"],
                                     "last_t": t["t"]})
        rec["ticks"] += 1
        rec["last_t"] = t["t"]
        if s in prev_t and prev_t[s] != t["t"]:
            gaps.append((t["t"] - prev_t[s]) / 1000)
        prev_t[s] = t["t"]
    gaps.sort()
    man_path = d / f"{day}.manifest.json"
    man = json.loads(man_path.read_text()) if man_path.exists() else None
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return {
        "day": day, "per_symbol": per_sym, "fields": sorted(fields_seen),
        "inter_tick_s": {
            "p50": gaps[len(gaps) // 2] if gaps else None,
            "p95": gaps[int(len(gaps) * 0.95)] if gaps else None,
            "max": gaps[-1] if gaps else None},
        "manifest_match": bool(man and man.get("sha256") == h.hexdigest()),
        "manifest": man,
    }


def compare_proxy(directory=DEFAULT_DIR, db_file=None, day=None,
                  now_ms=None):
    """Gerçek Mark Price (REST/WS tick) vs paper run'ın proxy mark'ı
    (4h bar kapanışı, salt-okunur ledger 'marks' tablosu).

    Her tick için: o an YÜRÜRLÜKTE olan proxy mark (tick E'sinden büyük
    olmayan en son mark_px) ile gerçek mark'ın farkı:
      dev_bp = (gerçek − proxy)/proxy · 1e4   (bp)
    Rapor: sembol başına |dev| ortalama/p95/p99/maks, örnek sayısı,
    timestamp skew (tick t − E) istatistikleri.
    """
    import sqlite3
    d = Path(directory)
    db_file = db_file or Path("~/futures-research/blok25/results/"
                              "paper_ledger.db").expanduser()
    if day is None:
        files = sorted(f for f in d.glob("*.jsonl")
                       if f.name != "events.jsonl")
        if not files:
            return {"error": "no data"}
        day = files[-1].name[:-6]
    # proxy mark'ları yükle (salt-okunur): symbol -> [(ts, mark_px)]
    marks = {}
    try:
        db = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        for sym, ts, px in db.execute("SELECT symbol, ts, mark_px "
                                      "FROM marks ORDER BY ts"):
            marks.setdefault(sym, []).append((ts, px))
        db.close()
    except sqlite3.Error as e:
        return {"error": f"ledger: {type(e).__name__}: {e}"}
    now_ms = now_ms or int(time.time() * 1000)
    per_sym = {}
    skew = []
    import bisect
    for line in open(d / f"{day}.jsonl"):
        if not line.strip():
            continue
        tick = json.loads(line)
        s = tick["s"]
        skew.append((tick["t"] - tick["E"]) / 1000)
        series = marks.get(s)
        if not series:
            continue
        ts_list = [m[0] for m in series]
        # yürürlükteki proxy: E'den büyük olmayan en son mark
        idx = bisect.bisect_right(ts_list, tick["E"]) - 1
        if idx < 0:
            continue
        proxy = series[idx][1]
        if not proxy:
            continue
        dev_bp = abs(tick["p"] - proxy) / proxy * 1e4
        rec = per_sym.setdefault(s, {"n": 0, "dev_bp": [],
                                     "first_t": tick["t"],
                                     "last_t": tick["t"]})
        rec["n"] += 1
        rec["dev_bp"].append(dev_bp)
        rec["last_t"] = tick["t"]
    out = {"day": day, "per_symbol": {}, "skew_s": {}}
    for s, rec in sorted(per_sym.items()):
        devs = sorted(rec["dev_bp"])
        out["per_symbol"][s] = {
            "n": rec["n"],
            "dev_bp_mean": round(sum(devs) / len(devs), 3),
            "dev_bp_p95": round(devs[int(len(devs) * 0.95)], 3),
            "dev_bp_p99": round(devs[min(len(devs) - 1,
                                         int(len(devs) * 0.99))], 3),
            "dev_bp_max": round(devs[-1], 3),
        }
    if skew:
        skew.sort()
        out["skew_s"] = {"mean": round(sum(skew) / len(skew), 3),
                         "p95": round(skew[int(len(skew) * 0.95)], 3),
                         "max": round(skew[-1], 3), "n": len(skew)}
    return out


def main():
    p = argparse.ArgumentParser(description="BLOK26 Mark Price observer "
                                            "(public, no credentials)")
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p.add_argument("--mode", choices=("rest", "ws"), default="rest",
                   help="rest: /fapi/v1/premiumIndex poll (varsayılan, "
                        "çalışıyor); ws: markPrice@1s (bu ağdan engelli)")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL_S,
                   help="REST poll aralığı (s)")
    p.add_argument("--duration", type=float, default=None,
                   help="saniye; yoksa süresiz")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--compare", action="store_true",
                   help="gerçek mark vs paper 4h-close proxy sapması")
    p.add_argument("--db", default=None,
                   help="paper ledger (compare için, salt-okunur)")
    p.add_argument("--day", default=None)
    args = p.parse_args()
    if args.stats:
        print(json.dumps(stats(args.dir, args.day), indent=1, default=str))
        return 0
    if args.compare:
        print(json.dumps(compare_proxy(args.dir, args.db, args.day),
                         indent=1, default=str))
        return 0
    if args.mode == "rest":
        run_rest(directory=args.dir, duration_s=args.duration,
                 interval=args.interval)
    else:
        run_ws(directory=args.dir, duration_s=args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
