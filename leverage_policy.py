"""leverage_policy.py — FAZ11 Katman 2: kaldıraç politikası (SAF).

Tek karar noktası: `compute_leverage`. Kaldıraç, aşağıdaki beş tavanın EN
KÜÇÜĞÜdür ve hiçbir zaman tavandan yukarı yuvarlanmaz:

  1. volatilite tavanı      L ≤ c / σ                (σ = ATR% / fiyat)
  2. drawdown cezası        L ≤ L_vol / (1 + d)
  3. Kelly tavanı           L ≤ lo + (hi−lo)·clip(f_used/f_ref, 0, 1)
  4. tasfiye güvenliği      L ≤ 1 / (buffer·SL + bakım marjı)
  5. kademe/borsa tavanı    L ≤ min(stage_cap, exchange_max)

Tasarım kuralları:
  * Tam sayıya AŞAĞI yuvarlanır (`floor`): yukarı yuvarlama tasfiye tavanını
    delebilir ve tek bir tick'te hesabı bozabilir.
  * σ ≤ 0, bakiye ≤ 0, SL ≤ 0 gibi geçersiz girdilerde en DÜŞÜK kaldıraç
    döner (fail-safe), asla en yüksek.
  * Saf: ağ yok, dosya yok, saat yok.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

LEVERAGE_DEFAULTS = {
    "c": 0.02,                 # volatilite ölçeği: σ=2% → L≈1
    "lo": 1.0,
    "hi": 20.0,
    "kelly_fraction": 0.25,
    "kelly_full_ref": 0.25,    # f_used bu değere ulaşınca Kelly tavanı hi'ye çıkar
    "liquidation_buffer": 2.0,  # SL, tasfiye mesafesinin en az 2 katı içinde
    "maintenance_margin": 0.005,
    "max_drawdown_pct": 0.20,
}


def _cfg(cfg, key):
    return (cfg or {}).get(key, LEVERAGE_DEFAULTS[key])


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


@dataclass
class LeverageDecision:
    leverage: int
    raw: float
    caps: list = field(default_factory=list)
    reason: str = ""
    sigma: float = 0.0
    drawdown: float = 0.0

    def as_dict(self):
        return {"leverage": self.leverage, "raw": round(self.raw, 4),
                "caps": list(self.caps), "reason": self.reason,
                "sigma": round(self.sigma, 6), "drawdown": round(self.drawdown, 4)}


def volatility_leverage(sigma, cfg=None):
    """L_vol = c/σ. σ ≤ 0 → lo (fail-safe: bilinmeyen volatilite yüksek kaldıraç almaz)."""
    lo, hi = float(_cfg(cfg, "lo")), float(_cfg(cfg, "hi"))
    if sigma is None or sigma <= 0:
        return lo
    return _clip(float(_cfg(cfg, "c")) / float(sigma), lo, hi)


def drawdown_penalty(leverage, drawdown):
    """L/(1+d): drawdown arttıkça kaldıraç doğrusal olarak azalır."""
    d = max(float(drawdown or 0.0), 0.0)
    return float(leverage) / (1.0 + d)


def kelly_leverage_cap(f_star, cfg=None):
    """Fractional-Kelly tabanlı tavan. Kenar yoksa lo, tam kenarda hi."""
    lo, hi = float(_cfg(cfg, "lo")), float(_cfg(cfg, "hi"))
    f_used = float(_cfg(cfg, "kelly_fraction")) * max(float(f_star or 0.0), 0.0)
    ref = float(_cfg(cfg, "kelly_full_ref")) or 1.0
    edge = _clip(f_used / ref, 0.0, 1.0)
    return lo + (hi - lo) * edge


def liquidation_cap(sl_pct, cfg=None):
    """L ≤ 1/(buffer·SL + bakım). SL tasfiyeden ÖNCE tetiklenmeli."""
    lo = float(_cfg(cfg, "lo"))
    if sl_pct is None or sl_pct <= 0:
        return lo
    buf = float(_cfg(cfg, "liquidation_buffer"))
    maint = float(_cfg(cfg, "maintenance_margin"))
    return max(lo, 1.0 / (buf * float(sl_pct) + maint))


def compute_leverage(sigma, sl_pct, drawdown=0.0, f_star=None, stage_cap=None,
                     exchange_max=None, cfg=None) -> LeverageDecision:
    """Beş tavanın en küçüğü; tam sayıya aşağı yuvarlanır.

    Dönen `caps` hangi tavanın bağlayıcı olduğunu söyler (denetim izi).
    """
    lo = float(_cfg(cfg, "lo"))
    hi = float(_cfg(cfg, "hi"))
    caps = []

    if sigma is None or sigma <= 0 or sl_pct is None or sl_pct <= 0:
        return LeverageDecision(max(1, int(math.floor(lo))), lo,
                                ["invalid_inputs"], "invalid_inputs: fail-safe lo",
                                float(sigma or 0.0), float(drawdown or 0.0))

    l_vol = volatility_leverage(sigma, cfg)
    l_dd = drawdown_penalty(l_vol, drawdown)
    if l_dd < l_vol:
        caps.append("drawdown")
    l_kelly = kelly_leverage_cap(f_star, cfg) if f_star is not None else hi
    if l_kelly < l_dd:
        caps.append("kelly")
    l_liq = liquidation_cap(sl_pct, cfg)
    if l_liq < min(l_dd, l_kelly):
        caps.append("liquidation")

    raw = min(l_dd, l_kelly, l_liq, hi)
    if stage_cap is not None and float(stage_cap) < raw:
        raw = float(stage_cap)
        caps.append("stage")
    if exchange_max is not None and float(exchange_max) < raw:
        raw = float(exchange_max)
        caps.append("exchange")

    lev = int(math.floor(raw))
    if lev < 1:
        lev = 1
        caps.append("floor")
    reason = "binding=" + (caps[0] if caps else "none")
    return LeverageDecision(lev, raw, caps, reason, float(sigma), float(drawdown or 0.0))


def leverage_ok(leverage, sl_pct, cfg=None):
    """Emir öncesi son kontrol: verilen kaldıraç tasfiye tavanının altında mı?"""
    if leverage is None or leverage < 1:
        return False, "leverage<1"
    cap = liquidation_cap(sl_pct, cfg)
    if float(leverage) > cap:
        return False, f"leverage={leverage}>{cap:.2f}"
    return True, f"leverage={leverage}<=cap={cap:.2f}"
