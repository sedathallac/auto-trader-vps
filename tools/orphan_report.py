"""tools/orphan_report.py — P0-6: salt-okur yetim (pozisyon ↔ algo-SL) raporu.

KULLANIM (VPS, FAZ18-B5):
  python3 -m tools.orphan_report [--env-file /etc/audit_api_key] [--db YOL]
      [--base https://fapi.binance.com] [--sapi-base https://api.binance.com]
      [--symbol BTCUSDT] [--max-weight 300] [--skip-preflight]

ÇIKIŞ PROTOKOLÜ (E3): final satır "VERDICT: OK|FINDINGS|INCOMPLETE" →
  exit 0/1/2;  exit 3 = 418/429 (Retry-After loglanır, ANINDA dur, retry YOK);
  exit 4 = anahtar politikası KURAL TABLOSUNDA KIRMIZI satır (E2-B6 ön-
  kontrolü: RET; tablo aşağıda ve PREFLIGHT_RULES'ta).

E2-B6 PREFLIGHT KURAL TABLOSU (BLOK6 İŞ2) — /sapi/v1/account/apiRestrictions
satırları artık tek bir bildirimsel tabloda (flag, tetik, aksiyon, mesaj):

  enableWithdrawals=True          → RED    RET, exit 4 (davranış aynı)
  ipRestrict != True              → RED    RET, exit 4; fail-closed: alan
                                           yoksa/Doğru değilse de KIRMIZI
  enableInternalTransfer=True     → UYARI  bayrak İSİMLİ WARNING loglanır,
                                           araç koşmaya devam eder (exit 4 YOK)
  enableMargin=True               → UYARI  aynı
  enableSpotAndMarginTrading=True → UYARI  aynı
  enableFutures=True              → KABUL  SESSİZ: ne uyarı ne RET.
       GEREKÇE: futures API erişimi (positionRisk/openAlgoOrders) anahtarın
       enableFutures=True olmasını ZORUNLU kılar — KABUL edilmezse araç hiç
       koşamaz. KALAN RİSK (kabul edilmiştir): anahtar emir yerleştirme
       gücü taşır; bu araç salt-GET olmakla birlikte anahtarın kendisi
       yazma kapasitelidir. Azaltımlar: ipRestrict KIRMIZI satırı (IP
       beyaz-listesi zorunlu) + anahtarın yalnız bu aracın env-file'ında
       durması.

Tablonun tek gerçeği kodda: PREFLIGHT_RULES + preflight_verdict().

ANAHTARLAR (D1): botun GERÇEK env adları BINANCE_KEY/BINANCE_SECRET — kanıt:
  auto_trader.py:5240  `bk = os.environ.get("BINANCE_KEY")`.  Anahtar
  --env-file (systemd EnvironmentFile: KEY=VALUE, yorum/boş satır atlanır,
  gereksiz tırnaklar soyulur, SON-değer kazanır) ya da süreç ortamından
  okunur. Anahtar ASLA argv'den alınmaz ve ASLA yazdırılmaz. Canlı süreç
  ortamından anahtar okuma yolu (PID-environ) tamamen KALDIRILDI (C.3/2).

GÜVENCELER:
  * YALNız imzalı GET. Kod yolunda gönderim/iptal yolu YOK
    (test kaynak-taraması + Request(method=) taraması ile kilitli).
  * "SL yok" hükmü YALNIZ algo service'ten (A-1 doktrini): koşullu emirler
    allOrders/openOrders'ta GÖRÜNMEZ; normal emir SL_YOK'u temize çıkaramaz.
  * TP ≠ SL (D2): TAKE_PROFIT* asla SL sayılmaz; STOP_TRAILING ayrı
    TRAILING sınıfıdır — koruyucu-sayılır ama BULGU olarak işaretlenir.
  * D3: hiçbir sorgu sarmalayıcısı hatayı boş listeye çevirmez; hata ya
    YÜKSELTİLİR ya INCOMPLETE işaretlenir. INCOMPLETE varsa sembol bazlı
    SL_YOK hükmü SUPRESE edilir, genel hüküm INCOMPLETE'tir.
  * D4: ledger mode=ro açılır + PRAGMA şema doğrulaması; tablo/kolon
    eksikliği ya da sqlite hatası → INCOMPLETE (asla "temiz" değil).

E1 KAPSAM: hesabi-geneli SEMBOLSÜZ tekil tarama —
  GET /fapi/v1/openAlgoOrders (W=40) + GET /fapi/v1/openOrders (W=40) +
  GET /fapi/v2/positionRisk (W=5). Sembol parametresi YALNIZ --symbol
  derinleşmesinde kullanılır. Ağırlık bütçesi: --max-weight (varsayılan 300)
  W1M deltası aşılırsa kalan sorgular koşmaz → INCOMPLETE (E3).
"""
import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# D2: gerçek SL sınıfı YALNIZ bunlar (STOP_MARKET koşullu-algo, STOP düz).
SL_TYPES = ("STOP_MARKET", "STOP")
# D2: ayrı TRAILING sınıfı — koruyucu sayılır ama tek başına bulgudur.
TRAILING_TYPES = ("STOP_TRAILING", "TRAILING_STOP_MARKET")
# D2: TP sınıfı — ASLA SL'yi karşılamaz.
TP_TYPES = ("TAKE_PROFIT", "TAKE_PROFIT_MARKET")
# D5-e: algo satırında canlılık; FINISHED/CANCELED/REJECTED/EXPIRED ölüdür (F20).
LIVE_ALGO_STATUS = ("NEW", "PARTIALLY_FILLED")
# Normal emir şeması (openOrders) canlılık alanları.
LIVE_ORDER_STATUS = ("NEW", "PARTIALLY_FILLED", "WORKING")
LEDGER_TABLE = "sl_placements"
# _read_ledger'in SELECT ettiği kolonlar (D4 PRAGMA doğrulaması bunları ister).
LEDGER_COLS = ("symbol", "token", "state", "algo_id", "client_algo_id", "gen")

# --------------------------------------------------------- E2-B6 kural tablosu
# Modül docstring'indeki tablonun kod karşılığı (tek gerçek, satır satır
# incelenabilir). Satır sözleşmesi: (flag, tetik, aksiyon, mesaj)
#   tetik "_TRUE"     → bayrak is True iken ateşler (açık yetki = risk),
#   tetik "_NOT_TRUE" → bayrak is True OLMAYINCA ateşler (fail-closed:
#                       alan yoksa/bozuksa da ateşler — ipRestrict),
#   aksiyon RED → RET + exit 4; UYARI → stderr UYARI satırı, koşulur;
#                 KABUL → hiçbir şey üretmez (sessiz kabul).
RED, UYARI, KABUL = "RED", "UYARI", "KABUL"
_TRUE, _NOT_TRUE = "true", "not_true"

PREFLIGHT_RULES = [
    ("enableWithdrawals", _TRUE, RED,
     "para çekme açık — anahtar salt-okur sayılmaz"),
    ("ipRestrict", _NOT_TRUE, RED,
     "IP beyaz-listesi kapalı ya da doğrulanamadı — C.3 şartı"),
    ("enableInternalTransfer", _TRUE, UYARI,
     "internal transfer açık — bulgu DEĞİL, uyarı; koşulur"),
    ("enableMargin", _TRUE, UYARI,
     "margin transfer açık — bulgu DEĞİL, uyarı; koşulur"),
    ("enableSpotAndMarginTrading", _TRUE, UYARI,
     "spot+margin trading açık — bulgu DEĞİL, uyarı; koşulur"),
    ("enableFutures", _TRUE, KABUL,
     "futures erişimi aracın çalışması için zorunlu — KABUL "
     "(kalan risk docstring'de belgelidir)"),
]


def preflight_verdict(ar):
    """E2-B6: apiRestrictions sözlüğünü PREFLIGHT_RULES'tan geçir.
    Saf fonksiyon (I/O yok): (red_lines, warning_lines) döner.
    red_lines DOLUYSa çağıran RET verip exit 4 vermeli; warning_lines
    stderr'e UYARI olarak yazılır ve koşulur. KABUL satırı (enableFutures)
    hiçbir satır ÜRETMEZ — ne uyarı ne RET."""
    red_lines, warning_lines = [], []
    for flag, when, action, msg in PREFLIGHT_RULES:
        if action == KABUL:
            continue                          # sessiz kabul: asla uyarma/RET
        v = ar.get(flag)
        fires = (v is True) if when == _TRUE else (v is not True)
        if fires:
            line = f"{flag}={v!r}: {msg}"
            (red_lines if action == RED else warning_lines).append(line)
    return red_lines, warning_lines


class QueryError(RuntimeError):
    """Sorgu başarısızlığı (taşıma/HTTP/hata-sözlüğü). Boş listeye DÖNÜŞMEZ."""


class BanError(RuntimeError):
    """418/429 — ban/hız sinyali. Retry YOK; araç ANINDA durmalı (exit 3)."""

    def __init__(self, code, retry_after=None):
        super().__init__(f"HTTP {code} Retry-After={retry_after}")
        self.code = code
        self.retry_after = retry_after


def parse_env_file(path):
    """systemd EnvironmentFile ayrıştırıcısı (D1): KEY=VALUE satırları;
    boş satır ve '#'/';' yorumları atlanır; tek/çift tırnak soyulur; aynı
    anahtar tekrarlanırsa SON değer kazanır. '=' içermeyen satır atlanır."""
    env = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(";"):
                continue
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def classify_order_type(order_type):
    """D2 sınıflandırma: 'SL' | 'TRAILING' | 'TP' | '' (ilgisiz)."""
    ot = str(order_type or "").upper()
    if ot in SL_TYPES:
        return "SL"
    if ot in TRAILING_TYPES:
        return "TRAILING"
    if ot in TP_TYPES:
        return "TP"
    return ""


def is_live(row, algo_side=True):
    """Canlılık (D5-e). Şema gerçeği: koşullu emir alanı `algoStatus`,
    normal emir alanı `status` — ikisini de tolere et. Sadece NEW /
    PARTIALLY_FILLED (normal emirde +WORKING) canlıdır."""
    wanted = LIVE_ALGO_STATUS if algo_side else LIVE_ORDER_STATUS
    st = str(row.get("algoStatus", "") or row.get("status", "") or "").upper()
    return st in wanted


def _f(v):
    """Güvenli float; yoksa/bozuksa None (kontrol 'atla' anlamına gelir)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class MbxTransport:
    """İmzalı GET taşıyıcısı (yalnız GET; Request method= almaz).

    opener parametresi TEST ENJEKSİYON noktasıdır: sahte urlopen ile
    istisna / hata-sözlüğü / liste-değil / 418 HTTPError senaryoları
    bağlanır. Canlı varsayılan: urllib.request.urlopen.
    w1m: son X-MBX-USED-WEIGHT-1M yanıt başlığı (E3 ağırlık logu)."""

    def __init__(self, api_key, secret, base, opener=None):
        self._key = api_key
        self._sec = secret
        self.base = base
        self.opener = opener or urllib.request.urlopen
        self.w1m = None

    def get(self, path, params=None, base=None):
        p = dict(params or {})
        p.setdefault("timestamp", int(time.time() * 1000))
        p.setdefault("recvWindow", 5000)
        q = urllib.parse.urlencode(p)
        sig = hmac.new(self._sec.encode(), q.encode(),
                       hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            f"{base or self.base}{path}?{q}&signature={sig}",
            headers={"X-MBX-APIKEY": self._key})
        try:
            with self.opener(req, timeout=20) as r:   # GET-only
                hdrs = getattr(r, "headers", None)
                w = hdrs.get("X-MBX-USED-WEIGHT-1M") if hdrs is not None \
                    else None
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):            # D3: anında dur, retry YOK
                ra = e.headers.get("Retry-After") if e.headers is not None \
                    else None
                raise BanError(e.code, ra) from None
            raise QueryError(f"HTTP {e.code} {path}") from None
        except (OSError, ValueError) as e:      # taşıma / bozuk JSON
            raise QueryError(f"{type(e).__name__} {path}") from None
        if w is not None:
            try:
                self.w1m = int(w)
            except (TypeError, ValueError):
                self.w1m = None
        if isinstance(body, dict) and "code" in body:   # D3: hata sözlüğü
            raise QueryError(f"API hatası {path}: code={body.get('code')}")
        return body


class Runner:
    """Sorgu yürütücü: şema doğrulaması + W1M logu + bütçe + INCOMPLETE
    defteri (E3/D3). Hata ASLA boş veriye dönüşmez: başarısız sorgu None
    döner ve run.incomplete'e yazılır. BanError burada ASLA yakalanmaz —
    main()'e anında yayılır (no-delay DUR)."""

    def __init__(self, get, w1m=None, max_w1m=None, sleep=time.sleep):
        self.get = get
        self.w1m = w1m          # callable → son X-MBX-USED-WEIGHT-1M (int/None)
        self.max_w1m = max_w1m
        self.sleep = sleep
        self.ok = 0
        self.total = 0
        self.incomplete = {}
        self.over_budget = False
        self._base_w = None

    def query(self, name, path, params=None, expect="list"):
        if self.over_budget:    # E3: bütçe aşıldı → kalan sorgular koşmaz
            self.incomplete[name] = "koşulmadı: ağırlık bütçesi aşıldı"
            return None
        self.total += 1
        try:
            data = self.get(path, params)
        except QueryError as e:
            self.incomplete[name] = f"{type(e).__name__}: {e}"
            return None
        w = self.w1m() if self.w1m is not None else None
        print(f"[GET] {path} W1M={'-' if w is None else w}")
        if expect == "list" and not isinstance(data, list):
            self.incomplete[name] = f"liste değil: {type(data).__name__}"
            return None
        if expect == "dict" and not isinstance(data, dict):
            self.incomplete[name] = f"sözlük değil: {type(data).__name__}"
            return None
        self.ok += 1
        if w is not None:       # bu aracın tükettiği W1M deltası (E3)
            if self._base_w is None:
                self._base_w = w
            delta = w - self._base_w
            if self.max_w1m is not None and delta > self.max_w1m:
                self.over_budget = True
                self.incomplete["weight-budget"] = (
                    f"W1M deltası {delta} > {self.max_w1m}")
        return data


def collect(get, ledger_rows, sleep=time.sleep, symbol=None, run=None):
    """Salt-GET toplayıcı (E1: SEMBOLSÜZ tekil çağrılar). get(path, params)
    YALNIZ GET uygular; hata Runner üzerinden INCOMPLETE olur — burada
    hiçbir istisna yutulmaz (D3). Döner: (positions, algos_by_sym,
    orders_by_sym). --symbol verildiyse ek olarak sembol-parametreli
    derinleşme çağrıları yapılır ve evren o sembole daraltılır."""
    run = run or Runner(get, sleep=sleep)
    positions = run.query("positionRisk", "/fapi/v2/positionRisk")
    algos = run.query("openAlgoOrders", "/fapi/v1/openAlgoOrders")
    orders = run.query("openOrders", "/fapi/v1/openOrders")
    if symbol:                                    # isteğe bağlı derinleşme
        a2 = run.query("openAlgoOrders:drill", "/fapi/v1/openAlgoOrders",
                       {"symbol": symbol})
        o2 = run.query("openOrders:drill", "/fapi/v1/openOrders",
                       {"symbol": symbol})
        if isinstance(algos, list) and a2:
            seen = {str(a.get("algoId")) for a in algos}
            algos = algos + [a for a in a2
                             if str(a.get("algoId")) not in seen]
        if isinstance(orders, list) and o2:
            seen = {str(o.get("orderId")) for o in orders}
            orders = orders + [o for o in o2 if str(o.get("orderId"))
                               not in seen]
    algos_by_sym, orders_by_sym = {}, {}
    for a in (algos if isinstance(algos, list) else []):
        s = str(a.get("symbol") or "")
        if s:
            algos_by_sym.setdefault(s, []).append(a)
    for o in (orders if isinstance(orders, list) else []):
        s = str(o.get("symbol") or "")
        if s:
            orders_by_sym.setdefault(s, []).append(o)
    if symbol:
        algos_by_sym = {k: v for k, v in algos_by_sym.items() if k == symbol}
        orders_by_sym = {k: v for k, v in orders_by_sym.items() if k == symbol}
    return (positions if isinstance(positions, list)
            else []), algos_by_sym, orders_by_sym


def _sl_problems(a, p):
    """D5: bir SL adayı için geçersizlik nedenleri (boşsa geçerli).
    (a) yön/positionSide tutarlılığı, (b) kapsam (closePosition ya da
    quantity ≥ |amt|), (c) trigger↔mark sağlığı (boş tarafta tetik),
    (d) likidasyon sağlığı (liqPrice varsa — yoksa ATLANIR).
    Eksik alan → o kontrol atlanır (uydurma hüküm yok)."""
    bad = []
    amt = p["amt"]
    side = str(a.get("side") or "").upper()
    if side:
        need = "SELL" if amt > 0 else "BUY"
        if side != need:
            bad.append(f"yan={side}≠{need}")
    ps = str(a.get("positionSide") or "BOTH").upper()
    if ps not in ("", "BOTH"):
        need_ps = "LONG" if amt > 0 else "SHORT"
        if ps != need_ps:
            bad.append(f"pozTaraf={ps}≠{need_ps}")
    close = a.get("closePosition")
    if close is True or str(close).upper() == "TRUE":
        pass                                     # tam kapsam — yeterli
    else:
        qty = _f(a.get("quantity"))
        if qty is None:
            qty = _f(a.get("origQty"))
        if qty is not None and qty + 1e-12 < abs(amt):
            bad.append(f"kapsam qty={qty} < |amt|={abs(amt)}")
    trig = _f(a.get("triggerPrice"))
    if trig is None:
        trig = _f(a.get("stopPrice"))
    if trig is not None:
        mark, liq = p.get("mark"), p.get("liq")
        if mark is not None:
            if amt > 0 and trig >= mark:         # LONG: stop mark'ın üstünde
                bad.append(f"trig={trig} ≥ mark={mark} (boş taraf)")
            if amt < 0 and trig <= mark:         # SHORT: stop mark'ın altında
                bad.append(f"trig={trig} ≤ mark={mark} (boş taraf)")
        if liq is not None and liq > 0:
            if amt > 0 and trig <= liq:
                bad.append(f"trig={trig} ≤ liq={liq} (likidasyon önce gelir)")
            if amt < 0 and trig >= liq:
                bad.append(f"trig={trig} ≥ liq={liq} (likidasyon önce gelir)")
    return bad


def analyze(positions, algos_by_sym, ledger_rows, orders_by_sym=None,
            incomplete=None):
    """Safer birleştirme — SAF HESAP, ağ yok. Döner dict: rows + verdict.

    Girdi sözleşmesi (A-1): SL-yokluk hükmü yalnız ALGO listesinden çıkar;
    orders_by_sym (normal emirler) SL_YOK'u temize ÇIKARAMAZ, yalnız yetim
    stop tespiti ve ek kanıt içindir. D2: TP asla SL değildir; TRAILING
    koruyucu-sayılır ama SL_TRAILING bulgusu üretir. D5: SL adayları
    geçerlilik denetiminden geçer (SL_GEÇERSİZ / ÇİFT_SL). incomplete
    doluysa SL_YOK hükümleri SUPRESE edilir ve hüküm INCOMPLETE'tir (D3)."""
    orders_by_sym = orders_by_sym or {}
    incomplete = incomplete or {}
    pos = {}
    for p in positions:
        try:
            amt = float(p.get("positionAmt") or 0.0)
        except (TypeError, ValueError):
            continue
        if amt != 0.0:
            pos[p["symbol"]] = {"amt": amt, "entry": _f(p.get("entryPrice")),
                                "mark": _f(p.get("markPrice")),
                                "liq": _f(p.get("liqPrice"))}
    led = {}
    for r in ledger_rows:
        led.setdefault(r["symbol"], []).append(dict(r))
    live_algos, live_orders = {}, {}
    for s, lst in algos_by_sym.items():
        live_algos[s] = [a for a in lst if is_live(a, algo_side=True)]
    for s, lst in orders_by_sym.items():
        live_orders[s] = [o for o in lst if is_live(o, algo_side=False)]
    syms = sorted(set(pos) | set(led)
                  | {s for s, a in live_algos.items() if a}
                  | {s for s, o in live_orders.items() if o})
    rows = []
    n = dict(no_sl=0, orphan=0, drift=0, invalid=0, double=0, trail=0,
             orphan_tp=0)
    for s in syms:
        kind = []
        p = pos.get(s)
        la = live_algos.get(s, [])
        sl = [a for a in la if classify_order_type(a.get("orderType")) == "SL"]
        tr = [a for a in la
              if classify_order_type(a.get("orderType")) == "TRAILING"]
        tp = [a for a in la if classify_order_type(a.get("orderType")) == "TP"]
        o_sl = [o for o in live_orders.get(s, [])
                if classify_order_type(o.get("type")) == "SL"]
        placed = [r for r in led.get(s, []) if r["state"] == "PLACED"]
        if p is None and (sl or tr or o_sl):
            kind.append("YETIM_SL")               # koruyucu-emir yetim
            n["orphan"] += 1
        elif p is None and tp:
            kind.append("YETIM_TP")               # D2: TP yetimi SL değildir
            n["orphan_tp"] += 1
        if p is not None:
            if not sl:
                if tr:
                    kind.append("SL_TRAILING")    # D2: koruyucu-fakat-işaretli
                    n["trail"] += 1
                elif not incomplete:              # hükmün KAYNAĞI: algo
                    kind.append("SL_YOK")         # service (A-1); D3 supresyon
                    n["no_sl"] += 1
            else:
                if any(_sl_problems(a, p) for a in sl):
                    kind.append("SL_GEÇERSİZ")    # D5-a..d
                    n["invalid"] += 1
                if len(sl) > 1:                   # D5-f: aynı sembol/yön
                    kind.append("ÇİFT_SL")        # üstüne birden çok canlı stop
                    n["double"] += 1
        ids = {str(a.get("algoId")) for a in la}
        for r in placed:
            if r.get("algo_id") and str(r["algo_id"]) not in ids:
                kind.append("KİMLİK-UYUŞMAZ")
                n["drift"] += 1
                break
        rows.append({"symbol": s, "pos_amt": p.get("amt") if p else None,
                     "sl_algo": len(sl), "sl_order": len(o_sl),
                     "ledger_placed": len(placed),
                     "bulgu": ",".join(dict.fromkeys(kind)) or "-"})
    found = sum(n.values())
    if incomplete:
        verdict = ("VERDICT: INCOMPLETE — eksik veriyle HÜKÜM YOK ("
                   + ", ".join(sorted(incomplete)) + ") — bu araç HİÇBİR ŞEY "
                   "DÜZELTMEZ; sorguları onar ve yeniden koş")
    elif found == 0:
        verdict = ("VERDICT: OK — 0 SL'siz / 0 yetim-SL / 0 geçersiz-SL / "
                   "0 çift-SL / 0 kimlik-uyuşmaz")
    else:
        verdict = (f"VERDICT: FINDINGS — {n['no_sl']} SL'siz-pozisyon, "
                   f"{n['orphan']} yetim-SL, {n['invalid']} geçersiz-SL, "
                   f"{n['double']} çift-SL, {n['trail']} trailing-only, "
                   f"{n['orphan_tp']} yetim-TP, {n['drift']} kimlik-uyuşmaz "
                   f"— İNSAN incele (bu araç HİÇBİR ŞEY DÜZELTMEZ)")
    return {"rows": rows, "verdict": verdict}


def _read_ledger(db_path):
    """D4: ledger'ı file:...?mode=ro ile aç ve PRAGMA table_info ile kullanılan
    kolonları doğrula. Döner (rows, err): err DOLUYSA evren BİLİNMİYORDUR —
    hüküm INCOMPLETE olur, asla 'temiz' okunmaz. Salt-okunur; yazım yok."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return [], f"ledger açılamadı: {type(e).__name__}: {e}"
    try:
        con.row_factory = sqlite3.Row
        cols = [r[1] for r in con.execute(
            f"PRAGMA table_info({LEDGER_TABLE})")]
        if not cols:
            return [], f"tablo yok: {LEDGER_TABLE}"
        missing = [c for c in LEDGER_COLS if c not in cols]
        if missing:
            return [], f"eksik kolon: {','.join(missing)}"
        rows = [dict(r) for r in con.execute(
            f"SELECT {', '.join(LEDGER_COLS)} FROM {LEDGER_TABLE} "
            "WHERE state IN ('PENDING','PLACED')")]
        return rows, None
    except sqlite3.Error as e:
        return [], f"ledger okunamadı: {type(e).__name__}: {e}"
    finally:
        con.close()


def main(argv=None, transport=None):
    ap = argparse.ArgumentParser(
        description="Salt-okur pozisyon↔algo-SL yetim raporu (yalnız GET).")
    ap.add_argument("--env-file", default=None,
                    help="systemd EnvironmentFile yolu (BINANCE_KEY/"
                         "BINANCE_SECRET; anahtar argv'den ALINMAZ)")
    ap.add_argument("--db", default="/var/lib/auto_trader_faz9_live/risk_ledger.db")
    ap.add_argument("--base", default="https://fapi.binance.com")
    ap.add_argument("--sapi-base", default="https://api.binance.com",
                    help="E2: SAPI host'u fapi host'undan ayrıdır")
    ap.add_argument("--symbol", default=None,
                    help="isteğe bağlı derinleşme (sembol-parametreli tek yol)")
    ap.add_argument("--max-weight", type=float, default=300.0,
                    help="E3: bu aracın W1M deltası bütçesi")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="apiRestrictions ön-kontrolünü atla — YALNIZ mock test")
    a = ap.parse_args(argv)

    if transport is None:                          # D1: env-file + env fallback
        envd = parse_env_file(a.env_file) if a.env_file else {}
        key = envd.get("BINANCE_KEY") or os.environ.get("BINANCE_KEY", "")
        sec = envd.get("BINANCE_SECRET") or os.environ.get("BINANCE_SECRET", "")
        if not key or not sec:
            print("HATA: BINANCE_KEY/BINANCE_SECRET yok (env-file ya da "
                  "ortam; anahtar yazdırılmaz).", file=sys.stderr)
            return 2
        transport = MbxTransport(key, sec, a.base)

    def get(path, params=None):
        # E2: SAPI çağrıları farklı host'ta (--sapi-base).
        base = a.sapi_base if str(path).startswith("/sapi/") else a.base
        return transport.get(path, params=params, base=base)

    run = Runner(get, w1m=lambda: getattr(transport, "w1m", None),
                 max_w1m=a.max_weight)
    try:
        if a.skip_preflight:
            print("UYARI: ön-kontrol atlandı (--skip-preflight — yalnız mock "
                  "test; canlıda KULLANMA).", file=sys.stderr)
        else:
            ar = run.query("apiRestrictions", "/sapi/v1/account/apiRestrictions",
                           expect="dict")
            if ar is None:
                run.incomplete.setdefault(
                    "apiRestrictions", "salt-okur anahtar politikası "
                                       "doğrulanamadı")
            else:
                # E2-B6: RET/UYARI kararı artık YALNIZ kural tablosundan.
                red_lines, warning_lines = preflight_verdict(ar)
                for w in warning_lines:
                    print(f"UYARI: E2 politika bayrağı — {w}",
                          file=sys.stderr)
                if red_lines:
                    print("RET: anahtar politikası salt-okur DEĞİL — "
                          "KIRMIZI satır(lar): " + "; ".join(red_lines)
                          + " — C.3 şartı; DURUYORUM.", file=sys.stderr)
                    return 4
        led, err = _read_ledger(a.db)              # D4: hata → INCOMPLETE
        if err:
            run.incomplete["ledger"] = err
        positions, algos_by_sym, orders_by_sym = collect(
            get, led, sleep=run.sleep, symbol=a.symbol, run=run)
    except BanError as b:                          # D3: anında dur (exit 3)
        print(f"418/429 Retry-After={b.retry_after}; DURUYORUM")
        return 3

    if a.symbol:                                   # derinleşme: evren daralt
        positions = [p for p in positions if p.get("symbol") == a.symbol]
        led = [r for r in led if r["symbol"] == a.symbol]

    out = analyze(positions, algos_by_sym, led, orders_by_sym,
                  incomplete=run.incomplete)
    print(f"{'SEMBOL':<14} {'POZİSYON':>12} {'ALGO-SL':>8} {'EMİR-SL':>8} "
          f"{'LEDGER':>7}  BULGU")
    for r in out["rows"]:
        print(f"{r['symbol']:<14} {str(r['pos_amt']):>12} {r['sl_algo']:>8} "
              f"{r['sl_order']:>8} {r['ledger_placed']:>7}  {r['bulgu']}")
    print(f"{run.ok}/{run.total} sorgu başarılı")   # E3 sayaç satırı
    print(out["verdict"])                           # E3 FINAL satırı
    kind = out["verdict"].split()[1]
    return 0 if kind == "OK" else (1 if kind == "FINDINGS" else 2)


if __name__ == "__main__":
    sys.exit(main())
