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
import os
from dataclasses import dataclass, field


def _env_float(name, default):
    """Env'den float oku; bozuk/boş değerde varsayılana dön (fail-safe)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


LEVERAGE_DEFAULTS = {
    # FAZ14k: c env'e bağlandı. c=0.02 → σ=%2'de 1x (~%3 risk/işlem),
    # c=0.06 → σ=%2'de 3x (~%9 risk/işlem). Ölçek keyfi sabit değil.
    "c": _env_float("LEVERAGE_C", 0.02),   # volatilite ölçeği: L_vol = c/σ
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
    caps_detail: dict = field(default_factory=dict)
    risk_per_trade: float = 0.0

    def as_dict(self):
        return {"leverage": self.leverage, "raw": round(self.raw, 4),
                "caps": list(self.caps), "reason": self.reason,
                "sigma": round(self.sigma, 6), "drawdown": round(self.drawdown, 4),
                "caps_detail": dict(self.caps_detail),
                "risk_per_trade": round(self.risk_per_trade, 6)}


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
    """Beş tavanın en küçüğü; tam sayıya round-half-to-even ile yuvarlanır.

    FAZ14k: `floor` yerine `round` (banker's rounding). floor, ham değer 1.85 iken 1x
    verip 2.00 iken 2x veriyordu; %8 ATR farkı %100 kaldıraç farkı üretiyordu. round
    sürekli davranır (1.85→2, 2.00→2, 2.597→3). Yuvarlama sonrası değer tasfiye
    tavanını aşarsa L_liq'e clamp edilir — güvenlik korunur.

    Dönen `caps` hangi tavanın bağlayıcı olduğunu söyler (denetim izi);
    `caps_detail` tüm tavanların ham değerlerini taşır (şeffaflık logu).
    """
    lo = float(_cfg(cfg, "lo"))
    hi = float(_cfg(cfg, "hi"))
    caps = []

    if sigma is None or sigma <= 0 or sl_pct is None or sl_pct <= 0:
        return LeverageDecision(max(1, int(math.floor(lo))), lo,
                                ["invalid_inputs"], "invalid_inputs: fail-safe lo",
                                float(sigma or 0.0), float(drawdown or 0.0),
                                {"invalid_inputs": lo}, 0.0)

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
    # Şeffaflık: hiçbir ceza/kademe tavanı bağlamadıysa bağlayan volatilitedir.
    if not caps:
        caps.append("volatility")

    # round-half-to-even: sürekli, sıçramasız. Python'un round()'u banker's rounding yapar.
    lev = int(round(raw))
    if lev < 1:
        lev = 1
        caps.append("floor")
    # Güvenlik: yuvarlama tasfiye tavanını delemez.
    if float(lev) > l_liq:
        lev = max(1, int(math.floor(l_liq)))
        caps.append("liq_clamp")
    if float(lev) > hi:
        lev = int(math.floor(hi))
        caps.append("hi_clamp")

    detail = {"volatility": round(l_vol, 4), "drawdown": round(l_dd, 4),
              "kelly": round(l_kelly, 4), "liquidation": round(l_liq, 4),
              "stage": None if stage_cap is None else float(stage_cap),
              "exchange": None if exchange_max is None else float(exchange_max)}
    binding = caps[0] if caps else "none"
    reason = f"binding={binding}"
    # risk_per_trade: SL mesafesi × kaldıraç = notional'ın yüzdesi olarak risk.
    risk = float(sl_pct) * float(lev)
    return LeverageDecision(lev, raw, caps, reason, float(sigma),
                            float(drawdown or 0.0), detail, risk)


def leverage_ok(leverage, sl_pct, cfg=None):
    """Emir öncesi son kontrol: verilen kaldıraç tasfiye tavanının altında mı?"""
    if leverage is None or leverage < 1:
        return False, "leverage<1"
    cap = liquidation_cap(sl_pct, cfg)
    if float(leverage) > cap:
        return False, f"leverage={leverage}>{cap:.2f}"
    return True, f"leverage={leverage}<=cap={cap:.2f}"
