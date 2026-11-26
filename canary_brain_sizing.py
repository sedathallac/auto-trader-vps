"""canary_brain_sizing.py — CANARY AI BRAIN dinamik sermaye/pozisyon beyni.

GÖREV §5 (2026-11-26): hedef SABİT notional veya sabit kaldıraç DEĞİLDİR.
Pozisyon büyüklüğü sırayla hesaplanır:

  1. STOP MESAFESİ önce belirlenir — volatiliteye göre (k × sigma_bar):
     stop_pct = STOP_SIGMA_MULT × sigma_bar(4h). Stop, gidiş-dönüş işlem
     maliyetinden DAR olamaz (maliyetten dar stop =komisyon/funding yüzünden
     mekanik zarar) → SR_STOP_BELOW_COSTS.
  2. İZİN VERİLEN PARASAL RİSK: min(kalan pozisyon bütçesi 5, kalan toplam
     bütçe 10) — operatör onaylı zarar bütçeleri (canary_risk tek kaynak).
  3. KANIT GÜCÜ KESİNTİSİ: risk × confidence. confidence = OOS nokta
     beklenti / bootstrap CI üst sınırı (canary_brain.calibration_confidence
     — veri türevli). OOS CI sıfırı içerdiğinden < 1: kanıtlanmamış kenara
     yüksek sermaye YAPILMAZ. Kelly TEK BAŞINA KULLANILMAZ (belirsizlik
     kesintisi + bütçe tavanı birlikte).
  4. PORTFÖY KORELASYONU: tutulan her pozisyonun riski ile korelasyon
     harcırahı: factor = 1/(1 + Σ|corr_i|×risk_i/10). Korelasyon
     hesaplanamıyorsa KONSERVATİF 1.0 sayılır (uyarı kayda geçer).
  5. NOTIONAL SONRA: qty = risk_final/(stop×fiyat). Notional asla hedef
     DEĞİLDİR — risk bütçesinin stop mesafesine bölümü.
  6. TAVANLAR (guardrail, hedef DEĞİL):
       - canlı politika hard cap (canary_risk.MAX_POSITION_NOTIONAL=100;
         mevcut operatör politikası — boyutlama hedefi değil, güvenlik tavanı)
       - bracket IM ≤ availableBalance (hesap verisi yoksa cap UNVERIFIED)

Kaldıraç/margin türü OTOMATİK DEĞİŞTİRİLMEZ (operatör onayı ayrı).
Gölge/advisory katmanı: emir ÜRETMEZ; canary_live giriş zinciri ve
canary_risk bütçe kapıları OLDUĞU GİBİ çalışır.
"""
import math
from typing import List, Optional, Tuple

from canary_risk import (MAX_POSITION_NET_LOSS, MAX_CANARY_NET_LOSS,
                         MAX_POSITION_NOTIONAL)
from canary_brain import calibration_confidence

# ÖNERİLEN varsayılan (operatör env BRAIN_STOP_SIGMA_MULT ile değiştirilir):
# 2 × sigma_4h — 4h'lik EWMA volatilitesinin 2 katı stop mesafesi. Dayanak:
# normal varyans altında tek-bar 2σ hareket ~%5 olasılık; 5 USDT bütçelik
# pozisyonda stop'un günlük gürültüyle tetiklenmemesi için asgari tampon.
DEFAULT_STOP_SIGMA_MULT = 2.0

# Boyutlama ret kodları (SR_ = sizing reject)
SR_INVALID_INPUT = "SR_INVALID_INPUT"
SR_STOP_BELOW_COSTS = "SR_STOP_BELOW_COSTS"
SR_NO_RISK_BUDGET = "SR_NO_RISK_BUDGET"
SR_ZERO_NOTIONAL = "SR_ZERO_NOTIONAL"


def _bracket_for(brackets: List[dict], notional: float) -> Optional[dict]:
    for b in brackets:
        if b["floor"] <= notional < b["cap"]:
            return b
    return None


class SizingBrain:
    """Saf boyutlama hesabı (ağ/durum YOK — bütün girdiler parametre).

    size() girdileri:
      price               : güncel fiyat (gate'den)
      sigma_bar           : motorun 4h bar EWMA volatilitesi (KESİR —
                            sqrt(var); 0.02 = %2/bar)
      roundtrip_cost_pct  : canary_brain.roundtrip_cost_pct çıktısı (%-nokta)
      position_budget_remaining : o sembol/yön için kalan 5'lik bütçe (USDT)
      total_budget_remaining    : kalan 10'luk toplam bütçe (USDT)
      confidence          : kanıt gücü [0,1] (verilmezse kalibrasyondan)
      held_positions      : [(corr veya None, risk_usdt)] — açık canary
                            pozisyonlerinin adayla korelasyonu ve riski
      available_balance   : borsa availableBalance (USDT; None → cap UNVERIFIED)
      brackets            : aday sembolin leverage bracket listesi
      notional_hard_cap   : canlı politika tavanı (varsayılan
                            MAX_POSITION_NOTIONAL — 100 USDT)
    """

    def __init__(self, stop_sigma_mult: float = DEFAULT_STOP_SIGMA_MULT):
        if not (stop_sigma_mult > 0):
            raise ValueError("stop_sigma_mult > 0 olmalı")
        self.stop_sigma_mult = stop_sigma_mult

    def size(self, *, price: float, sigma_bar: Optional[float],
             roundtrip_cost_pct: Optional[float],
             position_budget_remaining: float,
             total_budget_remaining: float,
             confidence: Optional[float] = None,
             held_positions: Optional[List[Tuple[Optional[float], float]]] = None,
             available_balance: Optional[float] = None,
             brackets: Optional[List[dict]] = None,
             notional_hard_cap: Optional[float] = MAX_POSITION_NOTIONAL
             ) -> dict:
        reasons = []
        warnings = []
        capped_by = []

        if not (price > 0) or sigma_bar is None or not (sigma_bar > 0):
            return self._out(False, reasons + [f"{SR_INVALID_INPUT}:price/"
                                               "sigma"], warnings, capped_by)
        if roundtrip_cost_pct is None or not (roundtrip_cost_pct > 0):
            return self._out(False, reasons + [f"{SR_INVALID_INPUT}:costs"],
                             warnings, capped_by)

        # 1) stop mesafesi (volatilite tabanlı) — maliyet kontrolü ile
        sigma_bar_pct = sigma_bar * 100.0
        stop_pct = self.stop_sigma_mult * sigma_bar_pct
        if stop_pct <= roundtrip_cost_pct:
            reasons.append(f"{SR_STOP_BELOW_COSTS}:stop={stop_pct:.3f}%"
                           f"<=costs={roundtrip_cost_pct:.3f}%")

        # 2) izin verilen parasal risk (operatör bütçeleri)
        risk_allowed = min(position_budget_remaining,
                           total_budget_remaining,
                           MAX_POSITION_NET_LOSS, MAX_CANARY_NET_LOSS)
        if risk_allowed <= 0:
            reasons.append(f"{SR_NO_RISK_BUDGET}:pos="
                           f"{position_budget_remaining:.2f} total="
                           f"{total_budget_remaining:.2f}")

        # 3) kanıt gücü kesintisi (Kelly TEK BAŞINA kullanılmaz)
        if confidence is None:
            confidence = calibration_confidence(1)  # çağıran yön vermelidir;
            warnings.append("confidence verilmedi — LONG varsayılanı")  # nadir
        confidence = max(0.0, min(1.0, confidence))
        risk_scaled = risk_allowed * confidence

        # 4) portföy korelasyon harcırahı
        held_positions = held_positions or []
        corr_sum = 0.0
        for corr, held_risk in held_positions:
            if corr is None:
                corr = 1.0        # hesaplanamadı → konservatif
                warnings.append("korelasyon hesaplanamadı — 1.0 sayıldı")
            corr_sum += abs(corr) * max(0.0, held_risk)
        portfolio_factor = 1.0 / (1.0 + corr_sum / MAX_CANARY_NET_LOSS)
        risk_final = risk_scaled * portfolio_factor

        if reasons:
            return self._out(False, reasons, warnings, capped_by,
                             stop_distance_pct=stop_pct,
                             confidence=confidence,
                             portfolio_factor=portfolio_factor)

        # 5) notional = risk / stop (hedef DEĞİL — türev)
        stop_frac = stop_pct / 100.0
        notional = risk_final / stop_frac
        qty = notional / price

        # 6) tavanlar (guardrail)
        if notional_hard_cap is not None and notional > notional_hard_cap:
            notional = notional_hard_cap
            qty = notional / price
            capped_by.append(f"POLICY_NOTIONAL_CAP:{notional_hard_cap}")
        if available_balance is not None and brackets:
            br = _bracket_for(brackets, notional)
            if br is None:
                reasons.append("SR_BRACKET_MISSING:notional_bracket_disinda")
            else:
                max_lev = max(1, br.get("max_leverage") or 1)
                im = notional / max_lev
                if im > available_balance:
                    notional = available_balance * max_lev
                    if notional_hard_cap is not None:
                        notional = min(notional, notional_hard_cap)
                    qty = notional / price
                    capped_by.append(
                        f"AVAILABLE_BALANCE_CAP:im={im:.2f}>avail="
                        f"{available_balance:.2f}")
        elif available_balance is None:
            warnings.append("hesap verisi yok — marjin tavanı UNVERIFIED")

        if notional <= 0 or qty <= 0:
            reasons.append(SR_ZERO_NOTIONAL)

        # etkin risk (tavan kesiildiyse gerçekte riske gilen para)
        effective_risk = notional * stop_frac
        im_usdt = None
        if brackets:
            br2 = _bracket_for(brackets, notional)
            if br2 is not None:
                im_usdt = notional / max(1, br2.get("max_leverage") or 1)
        return self._out(not reasons and notional > 0, reasons, warnings,
                         capped_by, stop_distance_pct=stop_pct,
                         confidence=confidence,
                         portfolio_factor=portfolio_factor,
                         notional=notional, qty=qty,
                         risk_usdt=effective_risk,
                         risk_before_scaling=risk_allowed,
                         im_usdt=im_usdt)

    @staticmethod
    def _out(ok, reasons, warnings, capped_by, **extra) -> dict:
        d = {"ok": ok, "reasons": list(reasons), "warnings": list(warnings),
             "capped_by": list(capped_by)}
        d.update({k: v for k, v in extra.items() if v is not None or k in
                  ("stop_distance_pct", "confidence", "portfolio_factor")})
        return d
