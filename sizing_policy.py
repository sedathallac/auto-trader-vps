"""sizing_policy.py — FAZ11 Katman 3: pozisyon boyutu ve SL/TP önerisi (SAF).

İki karar noktası:

  `size_position`  → (notional|None, reason). Kayıp bütçesi, Kelly tavanı, kalite
                     skoru, funding maliyeti ve borsa minimumu tek yerde kesişir.
  `suggest_sl_tp`  → (sl_pct, tp_pct, reason). ATR tabanlı, bantlı ve net R/R
                     kapısından geçmiş geometri.

Tasarım kuralları:
  * Bütçe borsa minimumunu karşılamıyorsa emir REDDEDİLİR (upsize yok).
  * Kalite skoru düşükse notional doğrusal küçültülür; sıfıra inerse emir yok.
  * Funding maliyeti stop mesafesine eklenir: SL'de kayıp = notional·(SL+fee+slip+funding).
  * SL/TP bantları ATR'ye bağlıdır; bant dışına taşan öneri kırpılır ve raporlanır.
  * Saf: ağ yok, dosya yok, saat yok.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import edge_engine

SIZING_DEFAULTS = {
    "risk_per_trade_pct": 0.015,
    "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008,
    "slippage_pct_round_trip": 0.0004,
    "min_notional": 5.5,
    "max_notional": 25.0,
    "max_position_pct": 20.0,   # FAZ14: equity yuzdesi tavani (0 = tavan yok)
    "max_leverage": 20,         # FAZ14m: 5 -> 20 (kaldiracli marjin tavani)
    "min_net_reward_risk": 1.20,
    "quality_floor": 0.55,
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 3.0,         # FAZ14m: TP = 2 x SL
    "sl_min_pct": 0.004,
    "sl_max_pct": 0.060,
    "tp_min_pct": 0.008,        # FAZ14m: 2 x sl_min_pct
    "tp_max_pct": 0.120,
}


def _cfg(cfg, key):
    return (cfg or {}).get(key, SIZING_DEFAULTS[key])


@dataclass
class SizeDecision:
    notional: float | None
    reason: str
    budget: float = 0.0
    effective_sl: float = 0.0
    quality_mult: float = 1.0
    caps: list = field(default_factory=list)

    @property
    def ok(self):
        return self.notional is not None

    def as_dict(self):
        return {"notional": None if self.notional is None else round(self.notional, 4),
                "ok": self.ok, "reason": self.reason, "budget": round(self.budget, 4),
                "effective_sl": round(self.effective_sl, 6),
                "quality_mult": round(self.quality_mult, 4), "caps": list(self.caps)}


def quality_multiplier(quality, floor=None):
    """Kalite → boyut çarpanı. floor altı → 0 (emir yok), 1.0 üstü → 1.0.

    Doğrusal: floor'da 0, 1.0'da 1.0. Böylece sınırda kalan coin yarım boyut alır,
    kötü coin hiç almaz.
    """
    f = float(floor if floor is not None else SIZING_DEFAULTS["quality_floor"])
    if quality is None:
        return 0.0
    q = float(quality)
    if q <= f:
        return 0.0
    if q >= 1.0:
        return 1.0
    return (q - f) / (1.0 - f)


def size_position(balance, sl_pct, *, quality=None, funding_bps=0.0, kelly_fraction=None,
                  cfg=None) -> SizeDecision:
    """Kayıp bütçesinden notional. Bütçe minimumu karşılamıyorsa None döner."""
    c = cfg or {}
    if balance is None or balance <= 0 or sl_pct is None or sl_pct <= 0:
        return SizeDecision(None, "invalid_inputs")

    fee = float(_cfg(c, "fee_pct_round_trip"))
    slip = float(_cfg(c, "slippage_pct_round_trip"))
    fund = max(0.0, float(funding_bps or 0.0)) / 10000.0
    effective_sl = float(sl_pct) + fee + slip + fund

    kelly = float(kelly_fraction if kelly_fraction is not None
                  else _cfg(c, "kelly_fraction"))
    budget = float(balance) * float(_cfg(c, "risk_per_trade_pct")) * max(0.0, kelly)
    raw = budget / effective_sl

    qm = quality_multiplier(quality, _cfg(c, "quality_floor")) if quality is not None else 1.0
    if qm <= 0:
        return SizeDecision(None, f"quality={quality}", budget, effective_sl, qm, ["quality"])
    notional = raw * qm
    caps = []
    if qm < 1.0:
        caps.append("quality")

    max_n = float(_cfg(c, "max_notional"))
    if max_n > 0 and notional > max_n:
        notional = max_n
        caps.append("max_notional")

    # FAZ14: equity yüzdesi tavanı. Sabit USDT tavanı yerine ölçeklenen matematik:
    # hesap büyüdükçe boyut da büyür, küçüldükçe küçülür. 0/None = tavan yok.
    pct = _cfg(c, "max_position_pct")
    if pct:
        pct_cap = float(balance) * float(pct) / 100.0
        if pct_cap > 0 and notional > pct_cap:
            notional = pct_cap
            caps.append("max_position_pct")

    # Kaldıraçlı marjin tavanı: notional equity×leverage'ı aşamaz (tasfiye güvenliği).
    lev = float(_cfg(c, "max_leverage"))
    if lev > 0:
        lev_cap = float(balance) * lev
        if notional > lev_cap:
            notional = lev_cap
            caps.append("leverage_cap")

    min_n = float(_cfg(c, "min_notional"))
    if notional < min_n:
        return SizeDecision(None, f"below_min_notional ({notional:.2f} < {min_n:.2f})",
                            budget, effective_sl, qm, caps + ["min_notional"])

    return SizeDecision(float(notional), "approved", budget, effective_sl, qm, caps)


@dataclass
class Geometry:
    sl_pct: float
    tp_pct: float
    reason: str
    net_rr: float = 0.0
    clipped: list = field(default_factory=list)
    ok: bool = False

    def as_dict(self):
        return {"sl_pct": round(self.sl_pct, 6), "tp_pct": round(self.tp_pct, 6),
                "net_rr": round(self.net_rr, 4), "ok": self.ok,
                "reason": self.reason, "clipped": list(self.clipped)}


def suggest_sl_tp(atr_pct, *, direction=1, cfg=None, min_rr=None) -> Geometry:
    """ATR tabanlı SL/TP; bantlara kırpılır ve net R/R kapısından geçirilir.

    SL = mult_sl·ATR, TP = mult_tp·ATR. Bant dışına çıkan değer kırpılır
    (`clipped` listesinde raporlanır) — sessizce kabul edilmez.
    """
    c = cfg or {}
    if atr_pct is None or atr_pct <= 0:
        return Geometry(0.0, 0.0, "invalid_atr")
    if direction not in (-1, 1):
        return Geometry(0.0, 0.0, "invalid_direction")

    clipped = []
    sl = float(_cfg(c, "sl_atr_mult")) * float(atr_pct)
    tp = float(_cfg(c, "tp_atr_mult")) * float(atr_pct)

    sl_lo, sl_hi = float(_cfg(c, "sl_min_pct")), float(_cfg(c, "sl_max_pct"))
    tp_lo, tp_hi = float(_cfg(c, "tp_min_pct")), float(_cfg(c, "tp_max_pct"))
    if sl < sl_lo:
        sl = sl_lo
        clipped.append("sl_min")
    elif sl > sl_hi:
        sl = sl_hi
        clipped.append("sl_max")
    if tp < tp_lo:
        tp = tp_lo
        clipped.append("tp_min")
    elif tp > tp_hi:
        tp = tp_hi
        clipped.append("tp_max")

    fee = float(_cfg(c, "fee_pct_round_trip"))
    slip = float(_cfg(c, "slippage_pct_round_trip"))
    rr, rr_ok, detail = edge_engine.net_reward_risk(tp, sl, fee, slip)
    need = float(min_rr if min_rr is not None else _cfg(c, "min_net_reward_risk"))
    if not rr_ok or rr < need:
        return Geometry(sl, tp, f"net_rr {rr:.2f} < {need:.2f} ({detail})", rr, clipped, False)
    return Geometry(sl, tp, f"net_rr {rr:.2f} ok", rr, clipped, True)
