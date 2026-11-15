"""coin_quality.py — FAZ11 Katman 1: çok kriterli coin kalite motoru (SAF).

FAZ4'ün Q skoru yalnız hacim + momentum + volatilite + hacim-teyidi kullanır.
Bu modül sekiz piyasa-mikroyapı metriğini [0,1] aralığına normalize eder,
ağırlıklı birleşik skor üretir ve kritik metriklerde SERT VETO uygular.

Tasarım kuralları:
  * Saf: ağ yok, dosya yok, saat yok. Aynı girdi → aynı çıktı.
  * Fail-closed: kritik metrik eksikse (spread/depth) coin REDDEDİLİR; skor
    uydurulmaz. Kritik olmayan metrik eksikse ağırlığı yeniden dağıtılır ve
    `missing` listesinde raporlanır.
  * Ölçek-bağımsız: her metrik kendi bandına göre puanlanır; set-max
    normalizasyonu YOK (tek bir dev coin tüm ölçeği ezmesin).

Bileşenler ve varsayılan ağırlıklar (toplam 1.0):
  spread 0.15 · depth 0.15 · volume 0.15 · atr 0.15
  funding 0.10 · open_interest 0.10 · taker 0.10 · change 0.10
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

QUALITY_WEIGHTS = {
    "spread": 0.15,
    "depth": 0.15,
    "volume": 0.15,
    "atr": 0.15,
    "funding": 0.10,
    "open_interest": 0.10,
    "taker": 0.10,
    "change": 0.10,
}

# Bantlar — hepsi tek yerde, kalibrasyon için tek noktadan değiştirilebilir.
BANDS = {
    "spread_bps_good": 1.0,        # ≤1 bps → 1.0
    "spread_bps_bad": 20.0,        # ≥20 bps → 0.0
    "depth_scale_usdt": 50_000.0,  # doyum ölçeği (±%1 bandındaki notional)
    "volume_scale_usdt": 50_000_000.0,
    "atr_lo": 0.003,               # %0.3 altı: hareketsiz
    "atr_hi": 0.020,               # %2.0 üstü: tehlikeli
    "funding_good": 0.0001,        # %0.01 / 8h
    "funding_bad": 0.0005,         # %0.05 / 8h
    "oi_scale_usdt": 20_000_000.0,
    "taker_lo": 0.80,              # [0.80, 1.25] sağlıklı
    "taker_hi": 1.25,
    "taker_extreme_lo": 0.67,
    "taker_extreme_hi": 1.50,
    "change_good": 0.10,           # |24s| ≤ %10 → 1.0
    "change_bad": 0.20,            # ≥ %20 → 0.0
}

# Sert veto eşikleri: bu değerler aşılırsa skor ne olursa olsun emir yok.
VETO = {
    "spread_bps_max": 25.0,
    "depth_usdt_min": 5_000.0,
    "open_interest_usdt_min": 500_000.0,
    "abs_change_max": 0.25,
    "atr_pct_max": 0.06,
}

MIN_QUALITY = 0.55


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _linear(value, good, bad):
    """good → 1.0, bad → 0.0, arada lineer. good==bad → 0/1 sıçraması."""
    if value is None:
        return None
    if good == bad:
        return 1.0 if value == good else 0.0
    return _clip01((bad - value) / (bad - good))


def _saturating(value, scale):
    """0 → 0, scale → 0.5, ∞ → 1. Log-benzeri doyum; büyüklük farkını yumuşatır."""
    if value is None or value <= 0 or scale <= 0:
        return 0.0 if value is not None else None
    return _clip01(value / (value + scale))


def spread_score(spread_bps, bands=None):
    b = bands or BANDS
    return _linear(spread_bps, b["spread_bps_good"], b["spread_bps_bad"])


def depth_score(depth_usdt, bands=None):
    b = bands or BANDS
    return _saturating(depth_usdt, b["depth_scale_usdt"])


def volume_score(quote_volume_24h, bands=None):
    b = bands or BANDS
    return _saturating(quote_volume_24h, b["volume_scale_usdt"])


def atr_band_score(atr_pct, bands=None):
    """Bant puanlama: [lo,hi] → 1.0; altında lineer yüksel, üstünde lineer düş.

    Çok hareketsiz coin (kâr üretemez) ve çok oynak coin (stop avı) aynı şekilde
    cezalandırılır; tepe düz bir banttır, tek nokta değil.
    """
    b = bands or BANDS
    if atr_pct is None:
        return None
    if atr_pct <= 0:
        return 0.0
    lo, hi = b["atr_lo"], b["atr_hi"]
    if lo <= atr_pct <= hi:
        return 1.0
    if atr_pct < lo:
        return _clip01(atr_pct / lo)
    return _clip01(1.0 - (atr_pct - hi) / hi)


def funding_score(funding_rate, bands=None):
    """|rate| büyüklüğü cezalandırılır; yön ayrıca `funding_cost_bps` ile hesaplanır."""
    b = bands or BANDS
    if funding_rate is None:
        return None
    return _linear(abs(funding_rate), b["funding_good"], b["funding_bad"])


def funding_cost_bps(funding_rate, direction, intervals=1, bands=None):
    """Pozisyon yönüne göre funding maliyeti (bps). Long pozitif oranda öder.

    Dönen değer ≥0: her zaman bir MALİYETTİR (negatifse kazanç → 0'a kırpılır,
    çünkü kazanç funding'e güvenilerek planlanmaz).
    """
    if funding_rate is None or direction not in (-1, 1):
        return 0.0
    b = bands or BANDS
    paid = direction * float(funding_rate) * max(1, int(intervals))
    if paid <= 0:
        return 0.0
    return min(paid * 10000.0, b["funding_bad"] * 10000.0)


def oi_score(open_interest_usdt, bands=None):
    b = bands or BANDS
    return _saturating(open_interest_usdt, b["oi_scale_usdt"])


def taker_score(taker_ratio, bands=None):
    """[lo,hi] → 1.0; uç değerler (tek taraflı agresyon / spoof) → 0."""
    b = bands or BANDS
    if taker_ratio is None:
        return None
    r = float(taker_ratio)
    if r <= 0:
        return 0.0
    if b["taker_lo"] <= r <= b["taker_hi"]:
        return 1.0
    if r < b["taker_lo"]:
        return _clip01((r - b["taker_extreme_lo"]) / (b["taker_lo"] - b["taker_extreme_lo"]))
    return _clip01((b["taker_extreme_hi"] - r) / (b["taker_extreme_hi"] - b["taker_hi"]))


def change_score(price_change_24h_pct, bands=None):
    b = bands or BANDS
    if price_change_24h_pct is None:
        return None
    return _linear(abs(price_change_24h_pct), b["change_good"], b["change_bad"])


@dataclass
class CoinQuality:
    symbol: str
    score: float
    ok: bool
    vetoes: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    components: dict = field(default_factory=dict)
    funding_cost_bps: float = 0.0

    def as_dict(self):
        return {
            "symbol": self.symbol, "score": round(self.score, 4), "ok": self.ok,
            "vetoes": list(self.vetoes), "missing": list(self.missing),
            "components": {k: (None if v is None else round(v, 4))
                           for k, v in self.components.items()},
            "funding_cost_bps": round(self.funding_cost_bps, 3),
        }


CRITICAL = ("spread", "depth")


def score_coin(symbol, metrics, direction=1, weights=None, bands=None, veto=None,
               min_quality=MIN_QUALITY):
    """Metrik sözlüğü → CoinQuality.

    metrics anahtarları: spread_bps, depth_usdt_1pct, quote_volume_24h, atr_pct,
    funding_rate, open_interest_usdt, taker_buy_sell_ratio, price_change_24h_pct.
    Eksik kritik metrik → ok=False (fail-closed). Eksik kritik olmayan metriğin
    ağırlığı kalanlara oransal dağıtılır.
    """
    w = dict(weights or QUALITY_WEIGHTS)
    b = bands or BANDS
    v = veto or VETO
    m = metrics or {}

    comp = {
        "spread": spread_score(m.get("spread_bps"), b),
        "depth": depth_score(m.get("depth_usdt_1pct"), b),
        "volume": volume_score(m.get("quote_volume_24h"), b),
        "atr": atr_band_score(m.get("atr_pct"), b),
        "funding": funding_score(m.get("funding_rate"), b),
        "open_interest": oi_score(m.get("open_interest_usdt"), b),
        "taker": taker_score(m.get("taker_buy_sell_ratio"), b),
        "change": change_score(m.get("price_change_24h_pct"), b),
    }
    missing = [k for k, val in comp.items() if val is None]
    vetoes = []

    if any(k in missing for k in CRITICAL):
        return CoinQuality(symbol, 0.0, False,
                           vetoes=[f"missing_critical:{','.join(k for k in CRITICAL if k in missing)}"],
                           missing=missing, components=comp)

    if m.get("spread_bps") is not None and m["spread_bps"] > v["spread_bps_max"]:
        vetoes.append(f"spread_bps={m['spread_bps']:.1f}>{v['spread_bps_max']:.0f}")
    if m.get("depth_usdt_1pct") is not None and m["depth_usdt_1pct"] < v["depth_usdt_min"]:
        vetoes.append(f"depth={m['depth_usdt_1pct']:.0f}<{v['depth_usdt_min']:.0f}")
    if m.get("open_interest_usdt") is not None and m["open_interest_usdt"] < v["open_interest_usdt_min"]:
        vetoes.append(f"oi={m['open_interest_usdt']:.0f}<{v['open_interest_usdt_min']:.0f}")
    if m.get("price_change_24h_pct") is not None and abs(m["price_change_24h_pct"]) > v["abs_change_max"]:
        vetoes.append(f"change={m['price_change_24h_pct']:+.3f}>{v['abs_change_max']:.2f}")
    if m.get("atr_pct") is not None and m["atr_pct"] > v["atr_pct_max"]:
        vetoes.append(f"atr={m['atr_pct']:.3f}>{v['atr_pct_max']:.2f}")

    total_w = sum(w[k] for k in comp if comp[k] is not None)
    if total_w <= 0:
        return CoinQuality(symbol, 0.0, False, vetoes=vetoes + ["no_usable_metrics"],
                           missing=missing, components=comp)
    score = sum(w[k] * comp[k] for k in comp if comp[k] is not None) / total_w

    cost = funding_cost_bps(m.get("funding_rate"), direction, 1, b)
    ok = (not vetoes) and score >= float(min_quality)
    return CoinQuality(symbol, score, ok, vetoes, missing, comp, cost)


def rank(candidates, key="CQ"):
    """Kalite skoruna göre azalan sıralama; skoru olmayanlar sona (fail-closed)."""
    return sorted(candidates, key=lambda c: (c.get(key) is not None, c.get(key) or 0.0),
                  reverse=True)


def quality_gate(coin, min_quality=MIN_QUALITY):
    """open_coin için tek satır kapı: (allow, reason). Kalite bilgisi yoksa İZİN VERME."""
    if not isinstance(coin, dict):
        return False, "no_quality_data"
    cq = coin.get("CQ")
    if cq is None:
        return False, "no_quality_data"
    if coin.get("quality_vetoes"):
        return False, "veto:" + ",".join(coin["quality_vetoes"])
    if cq < float(min_quality):
        return False, f"cq={cq:.3f}<{min_quality:.2f}"
    return True, f"cq={cq:.3f}"


def spread_bps_from_book(bids, asks):
    """En iyi bid/ask'ten spread (bps). Boş/bozuk defter → None (fail-closed)."""
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (IndexError, TypeError, ValueError):
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / 2.0
    return (best_ask - best_bid) / mid * 10000.0


def depth_within_pct(bids, asks, pct=0.01):
    """Mid'in ±pct bandındaki toplam notional (bid+ask). Bozuk defter → None."""
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (IndexError, TypeError, ValueError):
        return None
    if best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2.0
    band = mid * pct
    total = 0.0
    for p, q in bids:
        try:
            price, qty = float(p), float(q)
        except (TypeError, ValueError):
            continue
        if price >= mid - band:
            total += price * qty
    for p, q in asks:
        try:
            price, qty = float(p), float(q)
        except (TypeError, ValueError):
            continue
        if price <= mid + band:
            total += price * qty
    return total


def taker_ratio_from_klines(klines):
    """Son mum(lar)dan taker buy/sell oranı. Binance kline[9]=takerBuyBase, [5]=volume."""
    if not klines:
        return None
    buy = sell = 0.0
    for k in klines:
        try:
            vol = float(k[5])
            taker_buy = float(k[9])
        except (IndexError, TypeError, ValueError):
            continue
        buy += taker_buy
        sell += max(0.0, vol - taker_buy)
    if sell <= 0:
        return None
    return buy / sell
