"""exchange_ledger.py — FAZ2: fill/income-kaynaklı ledger writer + salt-okur mutabakat.

Mission B kanıtları (kaynak etiketi [FILL]/[INC]):
  * Gerçek gerçekleşen-PnL TEK kaynağı userTrades (220/220 fill, income ile
    Σ-birebir); HB defteri SYSTEM_CLEANUP kapanışlarını 0.00 yazmış (F23-i),
    sync kalıcı-PARTIAL'da ledger'a HİÇ yazmamış (F21 — ADA −4.07).
  * F22: collector dedupe kusuru fromId+startTime/endTime BİRLİKTE
    kullanıldığında sayfaların yutulmasına yol açıyordu — keyset fetcher bu
    dersi SÖZLEŞME olarak taşır: fromId ASLA startTime/endTime ile birlikte
    gönderilmez.

Bu modül üç şey sağlar ve hiçbiri üretim DB'sine/URL'ine DOKUNMAZ:
  1. build_close_events: userTrades fill'lerinden pozisyon-delta yürüyüşüyle
     KAPANIŞ KÜMELERİ üretir; net = ΣrealizedPnl − Σkomisyon + pay edilen
     funding (komisyon MALİYETTİR) — SYSTEM_CLEANUP'ın 0.00'ı yerine GERÇEK sayı.
  2. write_events_to_ledger: kümeleri RiskEngine.record_trade ile
     source="binance_fill_verified" ve affect_daily=False (geçmiş kapanış;
     bugünün risk sayacı YENİDEN YAZILMAZ) olarak STAGING DB'ye yazar;
     record_trade idempotency'si (executor_id PRIMARY KEY) çift yazımı engeller.
  3. reconcile: salt-okur günlük mutabakat — Σrp(fills) ≡ ΣREALIZED_PNL(income),
     Σkomisyon ≡ ΣCOMMISSION, ledger ↔ exchange farkı; tolerans 0.01 USDT;
     eşik aşımı BULGU olarak döner, ZORLA EŞLEŞTİRME YOK, hiçbir mutating
     işlem YOK.

Simge normalliği: ONE-WAY hesap varsayımı (BUY=+, SELL=−); hedge hesapta
positionSide ayrımı gerekir (FAZ2 kapsamı dışı — ONEWAY kanıtı: Mission B).
"""
import json

TOL_USDT = 0.01
SOURCE_FILL_VERIFIED = "binance_fill_verified"
# income akışında funding olayının gerçek etiketi FUNDING_FEE'dir (23 satır,
# −0.6892 [INC]); API dokümanı FUNDING der — ikisi de kabul edilir.
_FUNDING_TYPES = ("FUNDING", "FUNDING_FEE")


# ------------------------------------------------------------------ loader
def load_fill_rows(path):
    """Collector ndjson'ından userTrades_fromid_* satırlarını düzleştirir.

    DÖNER: list[fill dict] (ham Binance alan adlarıyla). Başka dataset'ler
    (ör. allOrders) sessizce ATILIR — kaynak ayrımı disiplin."""
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ds = rec.get("dataset", "")
            if not ds.startswith("userTrades_fromid_"):
                continue
            for r in rec.get("rows") or []:
                if isinstance(r, dict):
                    out.append(r)
    return out


def load_income_rows(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("dataset", "") != "income_90d":
                continue
            for r in rec.get("rows") or []:
                if isinstance(r, dict):
                    out.append(r)
    return out


# ------------------------------------------------------- close-event builder
def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def norm_fill(row):
    """Ham userTrades satırını normalize eder; id-sıra anahtarı (ts, id)."""
    return {
        "id": int(row["id"]),
        "order_id": row.get("orderId"),
        "symbol": str(row.get("symbol", "")),
        "side": str(row.get("side", "")).upper(),
        "qty": _f(row.get("qty")),
        "price": _f(row.get("price")),
        "rp": _f(row.get("realizedPnl")),
        "fee": _f(row.get("commission")),
        "ts": _f(row.get("time")) / 1000.0,
    }


_EPS_QTY = 1e-9


def build_close_events(fills):
    """Pozisyon-delta yürüyüşü ile kapanış kümeleri.

    Kural: pozisyon 0'dan uzaklaşan ilk fill küme AÇAR (open_ts); mutlak
    pozisyon ~0'a döndüğünde küme KAPANIR. Küme içi TÜM fill'lerin (giriş
    dâhil) komisyonu round-trip nete dahildir — Mission B finansal tanımı.
    Yürüyüş sonunda kapalı olmayan küme EVENT ÜRETMEZ; open_tails döner
    (hâlâ açık pozisyon — uydurma kapanış YOK).
    SIFIR-GEÇİŞİ (tek fill pozisyonu sıfırlayıp ters tarhte açar): kapanan
    kısmın komisyonu miktar-oranlı pay edilir; realizedPnl TÜYÜYLE kapanan
    kümeye yazılır (rp yalnız azaltan kısımdan doğar — açan kısım realize
    etmez). Geçen fill iki kümenin de fill_ids'inde görünür (denetim izi).

    DÖNER: (events, open_tails)
      event = {symbol, open_ts, closed_at_epoch, rp, commission, funding=0.0,
               fill_ids, close_order_ids, executor_id,
               net (rp+commission; funding atanınca güncellenir)}
    """
    by_sym = {}
    for raw in fills:
        n = raw if "rp" in raw and "fee" in raw and "id" in raw else norm_fill(raw)
        by_sym.setdefault(n["symbol"], []).append(n)
    def _new_cluster(sym, n):
        return {"symbol": sym, "open_ts": n["ts"], "rp": 0.0,
                "commission": n["fee"], "funding": 0.0, "fill_ids": [n["id"]],
                "close_order_ids": []}

    def _emit(cur):
        # komisyon bir MALİYETTİR (userTrades'te pozitif büyülüklü gelir):
        # net = Σrp − Σkomisyon + funding (Mission B finansal tanımı).
        cur["net"] = round(cur["rp"] - cur["commission"] + cur["funding"], 10)
        cur["executor_id"] = "fill-%s-%d" % (cur["symbol"], cur["fill_ids"][0])
        events.append(cur)

    events, tails = [], []
    for sym in sorted(by_sym):
        rows = sorted(by_sym[sym], key=lambda x: (x["ts"], x["id"]))
        pos = 0.0
        cur = None
        for n in rows:
            delta = n["qty"] if n["side"] == "BUY" else -n["qty"]
            if cur is None:
                if delta == 0:
                    continue
                cur = _new_cluster(sym, n)
                pos = delta
                continue
            reducing = pos != 0 and (pos > 0) != (delta > 0)
            pos_new = pos + delta
            if reducing and pos_new != 0 and (pos_new > 0) != (pos > 0):
                # SIFIR-GEÇİŞİ: kapanan kısım kümeyi kapatır, açan kısım yenisini açar
                frac = abs(pos) / abs(delta)
                cur["fill_ids"].append(n["id"])
                cur["commission"] += n["fee"] * frac
                cur["rp"] += n["rp"]              # rp yalnız kapanan kısmındır
                cur["close_order_ids"].append(n["order_id"])
                cur["closed_at_epoch"] = n["ts"]
                _emit(cur)
                cur = _new_cluster(sym, n)
                cur["commission"] = n["fee"] * (1.0 - frac)
                pos = pos_new
                continue
            cur["fill_ids"].append(n["id"])
            cur["commission"] += n["fee"]
            cur["rp"] += n["rp"]
            if reducing:
                cur["close_order_ids"].append(n["order_id"])
            pos = pos_new
            if abs(pos) < _EPS_QTY:
                pos = 0.0
                cur["closed_at_epoch"] = n["ts"]
                _emit(cur)
                cur = None
        if cur is not None:
            tails.append({"symbol": sym, "open_ts": cur["open_ts"],
                          "unclosed_fill_ids": cur["fill_ids"]})
    events.sort(key=lambda e: (e["closed_at_epoch"], e["symbol"]))
    return events, tails


def assign_funding(events, income_rows):
    """FUNDING income olaylarını zaman-penceresiyle kapanış kümelerine pay eder.

    Atanabilir: funding.time ∈ [open_ts, closed_at_epoch] VE sembol eşleşmesi
    (income sembolü boşsa o funding HİÇBİR kümeye atanmaz — uydurma atama YOK).
    DÖNER: atanmamış funding satırları listesi (raporlama için)."""
    unassigned = []
    funding = [r for r in income_rows if r.get("incomeType") in _FUNDING_TYPES]
    for r in sorted(funding, key=lambda x: _f(x.get("time"))):
        ts = _f(r.get("time")) / 1000.0
        sym = str(r.get("symbol") or "")
        target = None
        if sym:
            for e in events:
                if e["symbol"] == sym and e["open_ts"] <= ts <= e["closed_at_epoch"]:
                    target = e
                    break
        if target is None:
            unassigned.append(r)
            continue
        amt = _f(r.get("income"))
        target["funding"] = round(target["funding"] + amt, 10)
        target["net"] = round(target["rp"] - target["commission"] +
                              target["funding"], 10)
    return unassigned


# ------------------------------------------------------------- staging write
def write_events_to_ledger(engine, events, source=SOURCE_FILL_VERIFIED):
    """Kümeleri STAGING RiskEngine'ine yazar (geçmiş kapanış semantiği:
    affect_daily=False → bugünün risk_state sayacı DOKUNULMAZ).

    record_trade idempotent: aynı executor_id ikinci kez 'already-recorded'.
    DÖNER: {"inserted": n, "duplicate": n, "error": [executor_id,...]}"""
    out = {"inserted": 0, "duplicate": 0, "error": []}
    for e in events:
        ok, msg = engine.record_trade(
            executor_id=e["executor_id"], trading_pair=e["symbol"],
            net_pnl_quote=e["net"], closed_at_epoch=e["closed_at_epoch"],
            source=source, affect_daily=False)
        if ok:
            out["inserted"] += 1
        elif msg == "already-recorded":
            out["duplicate"] += 1
        else:
            out["error"].append(e["executor_id"])
    return out


# ------------------------------------------------------------------ reconcile
def norm_symbol(p):
    """Ledger 'BTC-USDT' ↔ exchange 'BTCUSDT' tek biçimi."""
    return str(p or "").replace("-", "").upper()


def reconcile(fills, income_rows, ledger_rows, tol=TOL_USDT):
    """SALT-OKUR mutabakat. Hiçbir girdiyi DEĞİŞTİRMEZ; dönüş rapordur.

    Kontroller (hepsi kaynak-etiketli, zorla eşleştirme YOK):
      R1 [FILL↔INC] ΣrealizedPnl(fills) ≡ ΣREALIZED_PNL(income)   |Δ|≤tol
      R2 [FILL↔INC] Σkomisyon(fills)     ≡ −ΣCOMMISSION(income)   |Δ|≤tol
      R3 [LED↔FILL] ledger net toplamı (kapsanan semboller) ile
          fill-net (rp+fee) arasındaki GAP listelenir (ADA-tipo ledger
          eksiklikleri görünür kalır; düzeltme YAPILMAZ).
    """
    fills_n = [f if "rp" in f and "fee" in f else norm_fill(f) for f in fills]
    rp_fill = sum(f["rp"] for f in fills_n)
    fee_fill = sum(f["fee"] for f in fills_n)
    rp_inc = sum(_f(r.get("income")) for r in income_rows
                 if r.get("incomeType") == "REALIZED_PNL")
    com_inc = -sum(_f(r.get("income")) for r in income_rows
                   if r.get("incomeType") == "COMMISSION")
    findings = []
    d1 = rp_fill - rp_inc
    if abs(d1) > tol:
        findings.append({"check": "R1 rp fill≡income", "delta": round(d1, 8),
                         "sources": ["EXCHANGE-FILL", "EXCHANGE-INCOME"]})
    d2 = fee_fill - com_inc
    if abs(d2) > tol:
        findings.append({"check": "R2 commission fill≡income",
                         "delta": round(d2, 8),
                         "sources": ["EXCHANGE-FILL", "EXCHANGE-INCOME"]})
    led = {}
    for r in ledger_rows:
        led[norm_symbol(r.get("trading_pair"))] = \
            led.get(norm_symbol(r.get("trading_pair")), 0.0) + \
            _f(r.get("net_pnl_quote"))
    fill_net = {}
    for f in fills_n:
        fill_net[f["symbol"]] = fill_net.get(f["symbol"], 0.0) + f["rp"] - f["fee"]
    gaps = []
    for sym in sorted(set(led) | set(fill_net)):
        lv, fv = led.get(sym, 0.0), fill_net.get(sym, 0.0)
        if abs(lv - fv) > tol:
            gaps.append({"symbol": sym, "ledger": round(lv, 8),
                         "exchange": round(fv, 8),
                         "delta": round(fv - lv, 8)})
    return {
        "totals": {
            "fill_rp": round(rp_fill, 8), "income_rp": round(rp_inc, 8),
            "fill_commission": round(fee_fill, 8),
            "income_commission": round(com_inc, 8),
            "funding_income": round(sum(_f(r.get("income")) for r in income_rows
                                        if r.get("incomeType") in _FUNDING_TYPES), 8),
            "ledger_net": round(sum(led.values()), 8),
        },
        "tolerance": tol,
        "findings": findings,
        "ledger_vs_exchange_gaps": gaps,
    }


# ------------------------------------------------- exchange-derived gate input
def exchange_day_pnl(day_str, fills, income_rows):
    """2.3 — risk-gate güncel GÜN netini YALNIZ exchange verisinden üretir.

    Gün (UTC) içinde KAPANAN kümelerin net toplamı (rp − komisyon + pay edilen
    funding). Gate bu değerle ledger-türevli daily_pnl'yi ÇAPRAZ-KONTROL
    edebilir; fark > tol ise defekt sinyali (F21-tipo ledger boşluğu).
    Kapsam dışı günlerin funding'i SEMBOL-GÜN eşleşmesiyle eklenir (funding
    kapanışa atanmış olsa da gün-temsilİ funding'in kendi UTC günüdür).
    DÖNER: dict {rp, commission, funding, net, closed_events} — kaynak: FILL/INC.
    """
    tot = {"rp": 0.0, "commission": 0.0, "funding": 0.0, "net": 0.0,
           "closed_events": 0}
    for e in fills:                      # burada fills = CLOSE EVENT listesi
        if _utc_day(e.get("closed_at_epoch")) == day_str:
            tot["rp"] += e.get("rp", 0.0)
            tot["commission"] += e.get("commission", 0.0)
            tot["closed_events"] += 1
    for r in income_rows:
        if r.get("incomeType") in _FUNDING_TYPES:
            if _utc_day(_f(r.get("time")) / 1000.0) == day_str:
                tot["funding"] += _f(r.get("income"))
    tot["net"] = round(tot["rp"] - tot["commission"] + tot["funding"], 8)
    for k in ("rp", "commission", "funding"):
        tot[k] = round(tot[k], 8)
    return tot


def _utc_day(epoch):
    from datetime import datetime, timezone
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%d")


# ----------------------------------------------------- keyset fetch (F22 ders)
class FillKeysetFetcher:
    """userTrades keyset sayfalama SÖZLEŞMESİ (F22 kök-nedeni):
    fromId ASLA startTime/endTime ile BİRLİKTE gönderilmez — birlikte
    kullanımda Binance sayfaları yutuyor (kanıt: 220→119 fill kaybı).

    get_fn(path, params) enjekte edilir (canlıda imzalı GET; testte sahte) —
    modül ağa KENDİLİĞİNDEN çıkmaz. Salt-okur; üretim boundary'sine yazmaz."""

    def __init__(self, get_fn):
        self._get = get_fn
        self.requests = []          # denetim kaydı: gönderilen PARAM SETLERİ

    def fetch_symbol(self, symbol, start_id=0, limit=1000, max_pages=10000):
        out, from_id, pages = [], int(start_id), 0
        while pages < max_pages:
            params = {"symbol": symbol, "fromId": from_id, "limit": int(limit)}
            # SÖZLEŞME: startTime/endTime BURAYA ASLA EKLENMEZ (F22).
            self.requests.append(dict(params))
            page = self._get("/fapi/v1/userTrades", params)
            if not isinstance(page, list):
                raise RuntimeError(
                    "userTrades sayfası liste değil (fail-closed): %r" % (page,))
            out.extend(page)
            pages += 1
            if len(page) < limit:
                break
            from_id = int(page[-1]["id"]) + 1
        seen, ded = set(), []
        for r in sorted(out, key=lambda x: int(x["id"])):
            if int(r["id"]) not in seen:
                seen.add(int(r["id"]))
                ded.append(r)
        return ded


# --------------------------------------------------------------- CLI (salt-okur)
def _load_ledger_rows(path):
    out = []
    with open(path) as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("dataset") == "ledger_trade_rows":
                out.extend(r for r in (rec.get("rows") or []) if isinstance(r, dict))
    return out


def main(argv=None):
    """Salt-okur günlük mutabakat CLI'ı. ÜÇ collector ndjson'ı okur, rapor
    basar, DIFFER bulunursa EXIT=1. HİÇBİR DB/AĞ YAZMASI YOK."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fills", required=True, help="trades_fromid_* ndjson")
    ap.add_argument("--income", required=True, help="income ndjson")
    ap.add_argument("--ledger", required=True, help="ledger ndjson (trade rows)")
    ap.add_argument("--tol", type=float, default=TOL_USDT)
    a = ap.parse_args(argv)
    rep = reconcile(load_fill_rows(a.fills), load_income_rows(a.income),
                    _load_ledger_rows(a.ledger), tol=a.tol)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 1 if rep["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
