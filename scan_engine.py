"""scan_engine.py — Faz 4: hiyerarşik çok-katmanlı tarama + Kalite Skoru (Q).

SAF hesaplama modülü (Binance'e dokunmaz) — test edilebilir. Ağ/çağrı tarafı
auto_trader.run_tiered_scan adaptöründe; burası yalnız cache, zamanlama ve skor.

Q = α·Likidite + β·Momentum + γ·Volatilite + δ·Hacim-Teyidi
  Likidite / Momentum / Hacim-Teyidi : set-max'a göre [0,1] normalize.
  Volatilite : orta-bandı ödüllendiren eğri (mean-reversion) — ATR% [lo,hi] → 1.0,
               altında lineer yüksel, üstünde lineer azal (~2·hi'de 0).

Katsayılar ⚠️ CALIBRATION-REQUIRED (compare modu loglarıyla ince ayar).
"""
import time
from dataclasses import dataclass

# Q katsayıları (yönetici kararı; kalibrasyon gerektirir)
Q_WEIGHTS = {"liquidity": 0.35, "momentum": 0.30, "volatility": 0.15, "volume_conf": 0.20}

# Volatilite orta-bandı (ATR%, kesir): bu bantta tam puan.
VOL_BAND_LO = 0.015   # %1.5
VOL_BAND_HI = 0.04    # %4.0


@dataclass(frozen=True)
class Tier:
    name: str
    universe: int       # kaç coin taranacak (hacme göre ilk N)
    interval: int       # saniye — vade periyodu
    liquidity_mode: str  # "depth" (gerçek order-book) | "quote_proxy" (quoteVolume)


# Pahalıdan ucuza değil — sabit tanım; çağıran sırayı/evreni kendi seçer.
TIERS = (
    Tier("hot", 100, 300, "depth"),
    Tier("warm", 250, 900, "quote_proxy"),
    Tier("cold", 500, 3600, "quote_proxy"),
)


class MetricCache:
    """TTL'li basit metrik önbelleği. `clock` enjekte edilebilir (test için)."""

    def __init__(self, clock=time.time):
        self._d = {}
        self._clock = clock

    def get(self, key, ttl):
        """TTL içindeyse değeri, değilse None döndürür (miss)."""
        entry = self._d.get(key)
        if entry is None:
            return None
        val, ts = entry
        if self._clock() - ts > ttl:
            return None
        return val

    def put(self, key, val):
        self._d[key] = (val, self._clock())

    def get_or_fetch(self, key, ttl, fn):
        hit = self.get(key, ttl)
        if hit is not None:
            return hit
        val = fn()
        self.put(key, val)
        return val


def volatility_score(atr_pct, lo=VOL_BAND_LO, hi=VOL_BAND_HI):
    """Orta-bant eğrisi: [lo,hi] → 1.0; atr<lo → lineer (atr/lo); atr>hi → lineer azalış
    (hi genişliğinde, ~2·hi'de 0). ATR yok/≤0 → 0 (fail-safe, düşük puan)."""
    if atr_pct is None or atr_pct <= 0:
        return 0.0
    if lo <= atr_pct <= hi:
        return 1.0
    if atr_pct < lo:
        return max(0.0, atr_pct / lo)
    return max(0.0, 1.0 - (atr_pct - hi) / hi)


def due_tiers(now, last_run, tiers=TIERS):
    """Vadesi gelen (elapsed ≥ interval, veya hiç çalışmamış) katmanları döndürür."""
    out = []
    for t in tiers:
        if now - last_run.get(t.name, float("-inf")) >= t.interval:
            out.append(t)
    return out


def compute_quality_scores(raw, weights=Q_WEIGHTS):
    """raw: [{symbol, liquidity, momentum, atr_pct, volume_conf, ...}] (diğer alanlar korunur).

    liquidity/momentum/volume_conf set-max'a normalize; volatilite shaped(0..1).
    Her öğeye 'vol_score' ve 'Q' eklenir; Q'ya göre AZALAN sıralı yeni liste döner.
    Boş giriş → []."""
    if not raw:
        return []
    maxliq = max((r["liquidity"] for r in raw), default=0.0) or 1.0
    maxmom = max((r["momentum"] for r in raw), default=0.0) or 1.0
    maxvc = max((r["volume_conf"] for r in raw), default=0.0) or 1.0
    out = []
    for r in raw:
        vs = volatility_score(r.get("atr_pct"))
        q = (weights["liquidity"] * (r["liquidity"] / maxliq)
             + weights["momentum"] * (r["momentum"] / maxmom)
             + weights["volatility"] * vs
             + weights["volume_conf"] * (r["volume_conf"] / maxvc))
        item = dict(r)
        item["vol_score"] = vs
        item["Q"] = q
        out.append(item)
    out.sort(key=lambda r: r["Q"], reverse=True)
    return out
