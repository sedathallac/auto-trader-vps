"""faz4_decision_archive.py — BLOK 2 İŞ 4 (M-5): KARAR-FİYATI ARŞİVİ + CostModel.

AMAÇ: giriş VE çıkış order intent'lerini karar anındaki defter durumuyla
(best_bid/ask, mid, spread_bps, update_id) birlikte yakalayan APPEND-ONLY
NDJSON arşivi; implementation-shortfall ölçümü; SÜRÜMLÜ CostModel.
FAZ 5'in ön koşulu budur — arşiv geleceği ölçer, geçmişi değil.

M-5 SAHTE-TRANSPORT DOKTRİNİ: bu modül ve testleri GERÇEK emir transportuna
hiçbir şey göndermez (satır içinde urllib/requests/http TIKLANMAZ — test
kaynak-taramasıyla kilitli). Üretim DB'sine migration/deploy YOK; entegrasyon
kuyruğa yazma kararı İNSAN kararıdır. Mevcut 220 fill için slippage/latency
"ÖLÇÜLEMEDİ" — arşiv öncesi karar-anı defter verisi YOK; uydurulmaz.

ŞEMA (tek satır = bir intent+fill olayı; hamurlaşma yok):
  event_id model_id symbol strategy_id signal_id phase(ENTRY/EXIT — genişletme)
  decision_ts_utc_ms order_intent_ts_utc_ms best_bid best_ask mid_price
  spread_bps orderbook_update_id orderbook_timestamp side intended_qty
  intended_order_type clientOrderId exchange_order_id fill_id fill_qty
  fill_price maker latency_ms
KURALLAR (testli): (1) event_id tektir — tekrar EKLENMEZ (append-only,
overwrite yok); (2) olay-sırası: decision_ts ≤ order_intent_ts ve sembol
içinde order_intent_ts geriye alamaz; (3) mid/spread BİRBİRİNDEN TÜRETİLİR:
bid/ask verildiyse mid=(b+a)/2, spread_bps=(a−b)/mid·1e4 — tutarsızlık RED.
FILL ALANLARI OPSİYONELDİR: emir henüz düşmediyse satır fill'sız da
yazılabilir (kayıp intent görünür kalır); shortfall yalnız fill'lı sayıları
sayar.

COSTMODEL cm-v1-20261119 bileşen durumları [FILL]/BLOK1 ölçümleriyle:
  fee/side = 0.05% (ÖLÇÜLÜ: 6.4635/12.971,92 = 0.0498%) · funding işaretli
  (ölçülmüş toplam −0.6892; tur-içi tahakkuk YAKLAŞIK) · slippage ÖLÇÜLEMEDİ
  (arşiv öncesi karar-anı defteri yok) · latency ÖLÇÜLEMEDİ. Her çıktı
  model_id taşır.
"""
import json
import os

MODEL_ID = "cm-v1-20261119"

# Zorunlu alanlar (M-5 çekirdeği + phase genişletmesi) — değer None olabilir
# (ör. defter okunamadıysa best_bid), ANCAK fill'sız satır fill_id taşımaz.
REQUIRED_FIELDS = (
    "event_id", "model_id", "symbol", "strategy_id", "signal_id", "phase",
    "decision_ts_utc_ms", "order_intent_ts_utc_ms", "side", "intended_qty",
    "intended_order_type", "clientOrderId",
)
OPTIONAL_FIELDS = (
    "best_bid", "best_ask", "mid_price", "spread_bps", "orderbook_update_id",
    "orderbook_timestamp", "exchange_order_id", "fill_id", "fill_qty",
    "fill_price", "maker", "latency_ms",
)
SIDES = ("BUY", "SELL")
PHASES = ("ENTRY", "EXIT")


class ArchiveError(ValueError):
    """Şema/sıra/dedup ihlali — satır REDDEDİLİR, dosyaya yazılmaz."""


class DecisionArchive:
    """NDJSON append-only karar-fiyatı arşivi. Yazma yalnız append();
    güncelleme/silme kasıtlı olarak YOK."""

    def __init__(self, path):
        self.path = path
        self._seen = set()
        self._last_intent_ms = {}      # symbol → en son order_intent_ts
        if os.path.exists(path):
            for ln in open(path).read().splitlines():
                if not ln.strip():
                    continue
                r = json.loads(ln)
                self._seen.add(r["event_id"])
                s = r["symbol"]
                t = r["order_intent_ts_utc_ms"]
                if t is not None:
                    self._last_intent_ms[s] = max(
                        self._last_intent_ms.get(s, 0), t)

    def append(self, rec):
        r = dict(rec)
        missing = [k for k in REQUIRED_FIELDS if r.get(k) is None]
        if missing:
            raise ArchiveError(f"zorunlu alan eksik: {missing}")
        if r["model_id"] != MODEL_ID:
            raise ArchiveError(f"model_id {r['model_id']!r} ≠ {MODEL_ID}")
        if r["phase"] not in PHASES or r["side"] not in SIDES:
            raise ArchiveError("phase ∈ ENTRY/EXIT, side ∈ BUY/SELL olmalı")
        if float(r["intended_qty"]) <= 0:
            raise ArchiveError("intended_qty > 0 olmalı")
        if r["event_id"] in self._seen:
            raise ArchiveError(f"dedup: event_id {r['event_id']} tekrar edemez"
                               " (append-only — overwrite yok)")
        d, i = r["decision_ts_utc_ms"], r["order_intent_ts_utc_ms"]
        if i < d:
            raise ArchiveError(f"olay-sırası: intent({i}) < decision({d})")
        sym, last = r["symbol"], self._last_intent_ms.get(r["symbol"])
        if last is not None and i < last:
            raise ArchiveError(f"olay-sırası: {sym} intent {i} < önceki {last}"
                               " (geriye zaman yok)")
        # mid/spread türetme + tutarlılık (bid/ask ikisi de verildiyse)
        b, a = r.get("best_bid"), r.get("best_ask")
        if b is not None and a is not None:
            if not (0 < b <= a):
                raise ArchiveError("best_bid ≤ best_ask ve > 0 olmalı")
            mid = (b + a) / 2.0
            spr = (a - b) / mid * 1e4
            for k, v in (("mid_price", mid), ("spread_bps", spr)):
                if r.get(k) is not None and abs(r[k] - v) > 1e-6 * max(1.0, abs(v)):
                    raise ArchiveError(f"{k} verilienden türetilenle uyuşmuyor")
            r["mid_price"], r["spread_bps"] = mid, spr
        if r.get("fill_price") is not None and float(r["fill_price"]) <= 0:
            raise ArchiveError("fill_price > 0 olmalı")
        unknown = set(r) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
        if unknown:
            raise ArchiveError(f"şema-dışı alan: {sorted(unknown)}")
        with open(self.path, "a") as fh:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
        self._seen.add(r["event_id"])
        self._last_intent_ms[sym] = max(self._last_intent_ms.get(sym, 0), i)
        return r


def implementation_shortfall(archive_or_rows, phase=None):
    """Karar-anı mid'inden gerçekleşen dolguya KAYAN değer (pozitif = maliyet).
    Yalnız fill_price VE mid_price dolu satırlar sayılır; eksikler GİDERİLMEZ,
    `measured` sayacı raporlanır (M-5: 'ölçülemedi' yazılır, uydurulmaz)."""
    rows = (archive_or_rows.read()
            if hasattr(archive_or_rows, "read") else list(archive_or_rows))
    sel = [r for r in rows if (phase is None or r.get("phase") == phase)]
    ok = []
    for r in sel:
        if r.get("fill_price") is None or r.get("mid_price") is None:
            continue
        sign = 1.0 if r["side"] == "BUY" else -1.0
        bps = sign * (float(r["fill_price"]) - r["mid_price"]) / r["mid_price"] * 1e4
        ok.append((bps, bps / 1e4 * float(r["fill_qty"] or 0) * float(r["fill_price"])))
    if not ok:
        return dict(n=len(sel), measured=0, mean_bps=None, total_usdt=None,
                    status="ÖLÇÜLEMEDİ (fill+mid dolu satır yok)")
    bps = sorted(x[0] for x in ok)
    mean = sum(bps) / len(bps)
    m = len(bps) // 2
    med = bps[m] if len(bps) % 2 else (bps[m - 1] + bps[m]) / 2.0
    return dict(n=len(sel), measured=len(ok), mean_bps=mean, median_bps=med,
                total_usdt=sum(x[1] for x in ok), status="ÖLÇÜLDÜ")


class CostModel:
    """Sürümlü maliyet modeli — her çıktı model_id taşır. Bileşen bileşen
    ÖLÇÜLÜ/ÖLÇÜLEMEDİ statüsü açık; tahmin ile ölçüm karışmaz."""

    model_id = MODEL_ID
    FEE_RATE_PER_SIDE = 0.0005          # [FILL] 6.4635/12971.92 = 0.0498%
    FUNDING_MEASURED_TOTAL = -0.68915844  # işaretli gelir/gider (BLOK1 [FILL])

    # Arşiv öncesi bunlar BİLİNEMEZ — 0 uydurmak yerine None.
    SLIPPAGE_BPS_PER_SIDE = None        # ÖLÇÜLEMEDİ: karar-anı defteri yok
    LATENCY_MS = None                   # ÖLÇÜLEMEDİ

    def cost_per_round_usdt(self, entry_notional):
        """Kesin bilinen bileşen: komisyon (giriş+çıkış). Funding/slippage
        bilinmiyor → toplama DAHİL EDİLMEZ, ayrıca raporlanır."""
        return 2.0 * self.FEE_RATE_PER_SIDE * entry_notional

    def measurement_status(self):
        return dict(model_id=self.model_id,
                    fee_per_side="ÖLÇÜLÜ 0.05% [FILL]",
                    funding=("ÖLÇÜLÜ toplam −0.6892 USDT; tur-içi tahakkuk "
                             "YAKLAŞIK (pencere-atama)"),
                    slippage=("ÖLÇÜLEMEDİ (karar-anı defteri yok; ölçüm bu "
                              "model_id'nin arşiviyle başlar)"),
                    latency="ÖLÇÜLEMEDİ (arşiv öncesi kayıt yok)")

    def describe(self):
        s = self.measurement_status()
        return " · ".join(f"{k}: {v}" for k, v in s.items())


class FakeTransport:
    """SAHTE emir transportu (test/simülasyon): imzalı/HTTP HİÇBİR ŞEY
    göndermez; niyeti arşive yazar, senaryodaki dolguyu döner. Gerçek
    transport'a geçiş İNSAN kararı — bu sınıf o yolu temsil etmez."""

    def __init__(self, archive, fills_by_client=None, clock_ms=None):
        self.archive = archive
        self.fills = dict(fills_by_client or {})
        self._clock = clock_ms or (lambda: 0)
        self.sent = []                  # görsel denetim: hangi niyetler geçti

    def submit(self, intent):
        """intent: DecisionArchive.append alan sözlüğü (fill alanları hariç).
        Döner: arşiv satırı (fill senaryoda varsa fill'lı)."""
        r = dict(intent)
        cid = r["clientOrderId"]
        self.sent.append(cid)
        f = self.fills.get(cid)
        if f:
            r.update(exchange_order_id=f.get("exchange_order_id"),
                     fill_id=f.get("fill_id"), fill_qty=f.get("qty"),
                     fill_price=f.get("price"), maker=f.get("maker"),
                     latency_ms=f.get("latency_ms"))
        return self.archive.append(r)


def main(argv=None):
    """GEÇMİŞ (220 fill) için dürüst çıktı: karar-anı defteri kaydı YOK →
    shortfall ÖLÇÜLEMEDİ. Arşiv yalnız GELECEK için ölçer."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fills", default="/home/reisfurkan/faz9-audit/raw/"
                    "streams/trades_fromid_fix_20261119.ndjson")
    a = ap.parse_args(argv)
    n = 0
    for ln in open(a.fills):
        ln = ln.strip()
        if not ln:
            continue
        rec = json.loads(ln)
        if rec.get("dataset", "").startswith("userTrades_fromid"):
            n += len(rec.get("rows", []))
    print(f"[FILL] {n} geçmiş fill — decision_ts/best_bid/orderbook kaydı "
          "YOK (arşiv bu blokla yeni başlıyor)")
    print(f"implementation shortfall (giriş+çıkış slippage/latency): "
          f"ÖLÇÜLEMEDİ — M-5 gereği uydurulmaz")
    cm = CostModel()
    print(f"CostModel {cm.model_id}: {cm.describe()}")
    print(f"kesin bilinen tek bileşen — komisyon: tur-başı giriş-notionalı "
          f"107.86 → {cm.cost_per_round_usdt(107.86):.4f} USDT "
          f"(ölçülenle uyumlu: 6.4635/60 tur = 0.10773)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
