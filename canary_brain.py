"""canary_brain.py — CANARY AI BRAIN çekirdeği: tam evren tarama motoru.

GÖREV (2026-11-26): mevcut canary yalnızca 8 sabit sembolü analiz ediyor
(SYMBOLS). Bu modül Binance USDⓈ-M uygun evreninin TAMAMINI (519 canlı
uygun sembol, keşif anı itibarıyla; sınır YOK — Top 10/20/50 tavanı YOK)
taranabilir hale getirir. Saf mantık: ağ YOK, durum YOK — tüm veri
parametre olarak enjekte edilir (contract_discovery deseni).

KATMANLAR:
  1. Evren: contract_discovery.discover_contracts → LIVE_ELIGIBLE semboller
     (PERPETUAL + USDT quote + USDT margin + TRADING). Entegrasyon — WIP
     modül KORUNDU, değiştirilmedi.
  2. Piyasa kalitesi kapıları (sembol başına, fail-closed):
       - 24h quoteVolume likidite tabanı (veri yok → RED)
       - anlık spread (bookTicker; veri yok → RED)
       - veri güncelliği (premiumIndex/24hr zaman damgası)
  3. Sinyal: mevcut donmuş H1 motoru (L=180 TSMom 4h) + BLOK32 kalite
     modeli (canary_quality.QualityModel) YENİDEN KULLANILIR — yeni sinyal
     matematiği İCAT EDİLMEZ. LONG ve SHORT BAĞIMSIZ değerlendirilir.
  4. Maliyet modeli: komisyon + slippage (spread'den) + funding — pozitif
     sinyal ancak NET beklenti > 0 ise ilan edilir.
  5. Karar: CANDIDATE / NO_TRADE / REJECTED + makinece okunabilir BR_*
     kodları. Sinyal yoksa NO_TRADE (yapay sinyal ÜRETİLMEZ).

KALİBRASYON DURUMU (dürüst etiket — §3/§4):
  Kalite eşikleri ve beklenti tahminleri 8 sembollük train(≤2023)/val(2024)
  verisinden geldi; OOS (2025-2026) nokta tahminleri pozitif AMA bootstrap
  %95 CI sıfırı içeriyor (LONG [−3.29,+7.26], SHORT [−2.89,+5.01]) →
  EDGE_NOT_PROVEN. 519 sembollük evrende YENİ kalibrasyon YAPILMADI →
  her PredictionRecord probability_is_calibrated=False ve
  calibration_domain='8_SYMBOLS' taşır. Tam evren kalibrasyonu ayrı
  operatör onaylı araştırma görevidir.

EMRİ YOKTUR: bu modül (ve tüm brain yığını) canlı giriş zincirine,
canary_live'a, canlı DB'ye DOKUNMAZ — gölge/prediction katmanıdır.
"""
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

from contract_discovery import (DiscoveryResult, LIVE_ELIGIBLE,
                                VENUE_FAPI)

# ---- model kimliği -----------------------------------------------------------
MODEL_VERSION = "CANARY_BRAIN_v1:H1_TSMOM_4H_L180+BLOK32_QUALITY+CostGate"

# ---- karar etiketleri ---------------------------------------------------------
DECISION_CANDIDATE = "CANDIDATE"     # bütün kapılar + net edge pozitif
DECISION_NO_TRADE = "NO_TRADE"       # değerlendirildi; kenar YOK (sinyal yok)
DECISION_REJECTED = "REJECTED"       # kapı başarısızlığı (veri/likidite/skor)

# ---- ret kodları (makinece okunabilir; BR_ = brain reject) --------------------
BR_NOT_IN_UNIVERSE = "BR_NOT_IN_UNIVERSE"
BR_DATA_FETCH_ERROR = "BR_DATA_FETCH_ERROR"
BR_STALE_MARKET_DATA = "BR_STALE_MARKET_DATA"
BR_INSUFFICIENT_HISTORY = "BR_INSUFFICIENT_HISTORY"
BR_ILLIQUID_24H = "BR_ILLIQUID_24H"
BR_SPREAD_TOO_WIDE = "BR_SPREAD_TOO_WIDE"
BR_NEUTRAL_MOMENTUM = "BR_NEUTRAL_MOMENTUM"
BR_QUALITY_BELOW_THRESHOLD = "BR_QUALITY_BELOW_THRESHOLD"
BR_COSTS_EXCEED_EXPECTED_EDGE = "BR_COSTS_EXCEED_EXPECTED_EDGE"
BR_INTERNAL_ERROR = "BR_INTERNAL_ERROR"

# ---- maliyet parametreleri (KAYNAK: BLOK32 kalibrasyonu — operatör onaylı) ----
# BİRİM: bütün *_pct değerleri YÜZDE-NOKTADIR (0.05 = %0.05 = notional'in
# 5e-4'ü). Kalibrasyon değerleriyle (result.json %-nokta) tutarlıdır.
TAKER_FEE_PCT = 0.05            # %0.05/yön (BLOK32 kalibrasyon varsayımı)
SLIPPAGE_FLOOR_PCT = 0.03       # %0.03/yön taban (kalibre; spread üstüne eklenir)
FUNDING_FLOOR_PER_BAR = 0.005   # %0.005/4h-bar (konservatif; kalibrasyon)

# ---- likidite/spread önerilen varsayılanları (operatör env ile değiştirilebilir;
#      gölge tarayıcı kapılarıdır — canlı giriş kapısı DEĞİL) -------------------
DEFAULT_MIN_QUOTE_VOLUME_24H = 5_000_000.0    # USDT
DEFAULT_MAX_SPREAD_PCT = 0.10                 # %0.10 anlık spread

# ---- 8 sembollük kalibrasyon alanı (~/blok32-calibration/result.json, OOS) ---
# Değerler 2026-11-25 kalibrasyonundan sabitlendi (dosya repo dışı; provenans
# yorumda). CI'lar bootstrap %95. confidence = exp / CI_upper (veri türevli;
# icat edilmiş sabit DEĞİL — OOS kanıt gücünün nokta tahmine oranı).
CALIBRATION_DOMAIN = "8_SYMBOLS"
CALIBRATION_8SYM = {
    1: {   # LONG
        "oos_expectancy_pct": 1.604,     # işlem başına net % (maliyet dahil)
        "oos_win_rate": 0.341,
        "oos_avg_win_pct": 20.303,
        "oos_avg_loss_pct": -8.068,
        "oos_ci_pct": (-3.29, 7.26),
        "oos_hold_bars": 174.3,
    },
    -1: {  # SHORT
        "oos_expectancy_pct": 0.989,
        "oos_win_rate": 0.480,
        "oos_avg_win_pct": 13.899,
        "oos_avg_loss_pct": -10.928,
        "oos_ci_pct": (-2.89, 5.01),
        "oos_hold_bars": 162.2,
    },
}


def calibration_confidence(direction: int) -> float:
    """OOS kanıt gücü ölçeği [0,1] = nokta beklenti / CI üst sınırı.
    CI sıfırı içerdiğinden < 1 çıkar — OOS kanıtı olmayan sinyale yüksek
    sermaye tahsisini yapısal olarak engeller (§5)."""
    c = CALIBRATION_8SYM[direction]
    upper = c["oos_ci_pct"][1]
    if upper <= 0:
        return 0.0
    return max(0.0, min(1.0, c["oos_expectancy_pct"] / upper))


def calibration_uncertainty_pct(direction: int) -> float:
    """Tahmin belirsizliği = bootstrap %95 CI yarı genişliği (%-puan)."""
    lo, hi = CALIBRATION_8SYM[direction]["oos_ci_pct"]
    return (hi - lo) / 2.0


# ---- rejim etiketi (kalite bileşenlerinden türetilir; icat yok) ---------------
def regime_label(momentum_positive: Optional[bool],
                 vol_reg: Optional[float]) -> str:
    """vol_reg = sigma_bar / son 500 barın sigma medyanı (<1 düşük vol).
    momentum None → veri eksik → '?'. Etiket yalnız İZLEME amaçlıdır;
    sinyal matematiğinde KULLANILMAZ (kalite skoru kendi bileşenini içerir)."""
    vol = ("LOW_VOL" if (vol_reg is not None and vol_reg < 1.0)
           else "HIGH_VOL" if vol_reg is not None else "VOL_UNKNOWN")
    trend = ("UP" if momentum_positive else "DOWN"
             if momentum_positive is not None else "?")
    return f"{vol}_{trend}_TREND"


@dataclass(frozen=True)
class PredictionRecord:
    """§3 karar kaydı — her sembol ve yön için denetlenebilir kayıt.
    (frozen dataclass: kayıt yazıldıktan sonra değiştirilemez.)"""
    symbol: str
    direction: int                      # +1 LONG / -1 SHORT
    ts: int                             # UTC epoch ms (değerlendirme anı)
    cycle_id: int
    decision: str                       # CANDIDATE / NO_TRADE / REJECTED
    regime: str
    signal_score: Optional[float]       # kalite skoru 0-100 (None = hesaplanamadı)
    quality_threshold: Optional[float]
    quality_pass: Optional[bool]
    calibrated_probability: Optional[float]    # P(işlem net pozitif) tahmini
    probability_is_calibrated: bool           # False → alan dışı türev
    expected_gross_return_pct: Optional[float]
    expected_net_return_pct: Optional[float]
    expected_transaction_costs_pct: Optional[float]
    funding_estimate_pct: Optional[float]
    slippage_estimate_pct: Optional[float]
    prediction_uncertainty_pct: Optional[float]
    stop_distance_pct: Optional[float]        # boyutlama beyni (canary_brain_sizing)
    expected_holding_bars: Optional[float]
    expected_holding_hours: Optional[float]
    rejection_reasons: Tuple[str, ...] = ()
    model_version: str = MODEL_VERSION
    calibration_domain: str = CALIBRATION_DOMAIN
    # bağlam (denetim izi)
    price: Optional[float] = None
    spread_pct: Optional[float] = None
    quote_volume_24h: Optional[float] = None
    last_funding_rate: Optional[float] = None
    momentum: Optional[float] = None
    mom_t: Optional[float] = None
    trend_confirmation: Optional[float] = None
    volatility_context: Optional[float] = None
    volume_confirmation: Optional[float] = None
    sigma_annualized: Optional[float] = None
    sigma_bar: Optional[float] = None
    sizing: Optional[dict] = None             # SizingBrain çıktısı (adaylar)
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = {"symbol": self.symbol, "direction": self.direction,
             "ts": self.ts, "cycle_id": self.cycle_id,
             "decision": self.decision, "regime": self.regime,
             "signal_score": self.signal_score,
             "quality_threshold": self.quality_threshold,
             "quality_pass": self.quality_pass,
             "calibrated_probability": self.calibrated_probability,
             "probability_is_calibrated": self.probability_is_calibrated,
             "expected_gross_return_pct": self.expected_gross_return_pct,
             "expected_net_return_pct": self.expected_net_return_pct,
             "expected_transaction_costs_pct":
                 self.expected_transaction_costs_pct,
             "funding_estimate_pct": self.funding_estimate_pct,
             "slippage_estimate_pct": self.slippage_estimate_pct,
             "prediction_uncertainty_pct": self.prediction_uncertainty_pct,
             "stop_distance_pct": self.stop_distance_pct,
             "expected_holding_bars": self.expected_holding_bars,
             "expected_holding_hours": self.expected_holding_hours,
             "rejection_reasons": list(self.rejection_reasons),
             "model_version": self.model_version,
             "calibration_domain": self.calibration_domain,
             "price": self.price, "spread_pct": self.spread_pct,
             "quote_volume_24h": self.quote_volume_24h,
             "last_funding_rate": self.last_funding_rate,
             "momentum": self.momentum, "mom_t": self.mom_t,
             "trend_confirmation": self.trend_confirmation,
             "volatility_context": self.volatility_context,
             "volume_confirmation": self.volume_confirmation,
             "sigma_annualized": self.sigma_annualized,
             "sigma_bar": self.sigma_bar,
             "sizing": self.sizing, "notes": list(self.notes)}
        return d


# ---- maliyet modeli -----------------------------------------------------------
def roundtrip_cost_pct(spread_pct: Optional[float],
                       last_funding_rate: Optional[float],
                       hold_bars: float) -> Optional[float]:
    """Gidiş-dönüş toplam işlem maliyeti (YÜZDE-NOKTA, notional üzerinden).

      komisyon : 2 × TAKER_FEE_PCT (giriş+çıkış taker — kalibrasyon varsayımı)
      slippage : 2 × max(SLIPPAGE_FLOOR_PCT, spread/2)  (gerçek anlık spread
                 tabanın ALTINA düşemez — kalibrasyonun konservatif tabanı)
      funding  : hold_bars × max(FUNDING_FLOOR_PER_BAR, |rate|%×100/2)
                 (lastFundingRate kesir olduğu için %-noktaya çevrilir; 4h bar
                 başına 8h'lik oranın yarısı; konservatif: funding her zaman
                 MALİYET sayılır — BLOK32 kalibrasyon semantiği)

    spread/funding None veya hold_bars <= 0 → None (fail-closed: maliyet
    hesaplanamıyansa pozitif sinyal İLAN EDİLMEZ)."""
    if spread_pct is None or spread_pct < 0 or \
            last_funding_rate is None or not (hold_bars > 0):
        return None
    slip_side = max(SLIPPAGE_FLOOR_PCT, spread_pct / 2.0)
    fund_bar = max(FUNDING_FLOOR_PER_BAR, abs(last_funding_rate) * 100.0 / 2.0)
    return 2.0 * (TAKER_FEE_PCT + slip_side) + fund_bar * hold_bars


def slippage_estimate_pct(spread_pct: Optional[float]) -> Optional[float]:
    """Yön başına slippage tahmini (YÜZDE-NOKTA): taban ile anlık yarı-spread'in
    büyüğü. Spread yok → None."""
    if spread_pct is None or spread_pct < 0:
        return None
    return max(SLIPPAGE_FLOOR_PCT, spread_pct / 2.0)


# ---- evren kapısı --------------------------------------------------------------
@dataclass
class UniverseGateResult:
    """Bir tarama döngüsü için sembol başına piyasa-veri kapısı çıktısı."""
    symbol: str
    eligible: bool
    reasons: Tuple[str, ...] = ()
    price: Optional[float] = None
    spread_pct: Optional[float] = None
    quote_volume_24h: Optional[float] = None
    last_funding_rate: Optional[float] = None
    market_data_age_ms: Optional[int] = None


def eligible_symbols(discovery: DiscoveryResult) -> list:
    """Keşif sonucundan USDT-M LIVE_ELIGIBLE semboller (sıralı, tekrarsız)."""
    seen = []
    for coin in discovery.coins:
        if coin.category != LIVE_ELIGIBLE:
            continue
        for c in coin.contracts:
            if c.venue == VENUE_FAPI and c.category == LIVE_ELIGIBLE:
                if c.symbol not in seen:
                    seen.append(c.symbol)
                break
    return sorted(seen)


def gate_market_data(symbol: str, *, ticker: Optional[dict],
                     book: Optional[dict], premium: Optional[dict],
                     now_ms: int, max_age_ms: int,
                     min_quote_volume_24h: float,
                     max_spread_pct: float) -> UniverseGateResult:
    """Piyasa kalitesi kapısı (fail-closed: veri yok/eski → RED).

    ticker  : /fapi/v1/ticker/24hr satırı (quoteVolume, lastPrice)
    book    : /fapi/v1/ticker/bookTicker satırı (bidPrice, askPrice)
    premium : /fapi/v1/premiumIndex satırı (markPrice, lastFundingRate, time)
    """
    reasons = []
    if ticker is None or book is None or premium is None:
        # hangi bileşen eksikse görünür; hiçbiri sıfır VARSAYILMAZ
        missing = [n for n, v in (("24hr", ticker), ("bookTicker", book),
                                  ("premiumIndex", premium)) if v is None]
        reasons.append(f"{BR_STALE_MARKET_DATA}:missing={','.join(missing)}")
        return UniverseGateResult(symbol, False, tuple(reasons))

    try:
        last = float(ticker["lastPrice"])
        bid = float(book["bidPrice"])
        ask = float(book["askPrice"])
        qv = float(ticker["quoteVolume"])
        rate = float(premium["lastFundingRate"])
        data_t = int(premium.get("time") or 0)
    except (KeyError, TypeError, ValueError) as e:
        return UniverseGateResult(
            symbol, False, (f"{BR_STALE_MARKET_DATA}:parse={type(e).__name__}",))

    if not (last > 0 and bid > 0 and ask > 0 and ask >= bid):
        return UniverseGateResult(
            symbol, False, (f"{BR_STALE_MARKET_DATA}:invalid_prices",))
    age = now_ms - data_t if data_t > 0 else None
    if age is not None and age > max_age_ms:
        reasons.append(f"{BR_STALE_MARKET_DATA}:age={age}ms>{max_age_ms}ms")

    spread_pct = (ask - bid) / last * 100.0 if last > 0 else None
    if qv < min_quote_volume_24h:
        reasons.append(f"{BR_ILLIQUID_24H}:qv={qv:.0f}<"
                       f"{min_quote_volume_24h:.0f}")
    if spread_pct is not None and spread_pct > max_spread_pct:
        reasons.append(f"{BR_SPREAD_TOO_WIDE}:spread={spread_pct:.5f}>"
                       f"{max_spread_pct:.5f}")

    return UniverseGateResult(
        symbol, not reasons, tuple(reasons), price=last,
        spread_pct=spread_pct, quote_volume_24h=qv,
        last_funding_rate=rate, market_data_age_ms=age)


# ---- tarayıcı ------------------------------------------------------------------
class BrainScanner:
    """Sembol+yön başına PredictionRecord üretir (saf — ağ/durum YOK).

    evaluate() girdileri:
      gate       : UniverseGateResult (piyasa kapısı geçilmiş olmalı)
      quality    : canary_quality.QualityModel.compute() çıktısı (None olabilir
                   — warmup/veri eksikliği; None → BR_INSUFFICIENT_HISTORY/
                   BR_NEUTRAL_MOMENTUM yoluna düşer)
      sigma_bar  : motorun EWMA bar volatilitesi (None olabilir)
      momentum   : motor momentum'u m (None olabilir)
      warm       : motor ısındı mı (n >= lookback ve var > 0)
    """

    def __init__(self, *,
                 min_quote_volume_24h: float = DEFAULT_MIN_QUOTE_VOLUME_24H,
                 max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT):
        self.min_quote_volume_24h = min_quote_volume_24h
        self.max_spread_pct = max_spread_pct

    def evaluate(self, symbol: str, direction: int, *, cycle_id: int,
                 now_ms: int, gate: UniverseGateResult,
                 quality: Optional[dict], sigma_bar: Optional[float],
                 momentum: Optional[float], warm: bool,
                 sizing: Optional[dict] = None) -> PredictionRecord:
        assert direction in (1, -1)
        reasons = []
        cal = CALIBRATION_8SYM[direction]

        # bağlam alanları (kayıtta her zaman görünür)
        price = gate.price
        spread_pct = gate.spread_pct
        qv = gate.quote_volume_24h
        rate = gate.last_funding_rate
        score = quality.get("quality_score") if quality else None
        theta = quality.get("quality_threshold") if quality else None
        qpass = quality.get("quality_pass") if quality else None
        mom = momentum
        mom_t = quality.get("momentum_strength") if quality else None
        trend_c = quality.get("trend_confirmation") if quality else None
        vol_ctx = quality.get("volatility_context") if quality else None
        vol_conf = quality.get("volume_confirmation") if quality else None
        regime = regime_label(
            None if mom is None else (mom > 0), vol_ctx)

        hold_bars = cal["oos_hold_bars"]
        costs = roundtrip_cost_pct(spread_pct, rate, hold_bars)
        slip = slippage_estimate_pct(spread_pct)
        gross = cal["oos_expectancy_pct"]             # %-nokta (alan tahmini)
        net = (gross - costs) if costs is not None else None
        uncertainty = calibration_uncertainty_pct(direction)
        prob = cal["oos_win_rate"]

        # --- kapılar (sıra: veri → kalite → maliyet) ---
        if not gate.eligible:
            reasons.extend(gate.reasons)
        if not warm:
            reasons.append(BR_INSUFFICIENT_HISTORY)
        if quality is None:
            # warm ama skor None → nötr momentum (m == 0) veya pencere eksik
            if warm and mom == 0:
                reasons.append(BR_NEUTRAL_MOMENTUM)
            elif warm:
                reasons.append(f"{BR_INSUFFICIENT_HISTORY}:quality_windows")
        elif score is not None and theta is not None and not qpass:
            # yapısal olarak: ters yön momentumu skoru %65 üst bileşensiz
            # bırakır (canary_quality dokümantasyonu) — ayrı kodla görünür
            if mom is not None and (mom > 0) != (direction > 0):
                reasons.append(BR_QUALITY_BELOW_THRESHOLD + ":momentum_opposite")
            else:
                reasons.append(f"{BR_QUALITY_BELOW_THRESHOLD}:"
                               f"score={score:.1f}<{theta:.1f}")
        if costs is None:
            reasons.append(f"{BR_COSTS_EXCEED_EXPECTED_EDGE}:costs_uncomputable")
        elif net is not None and net <= 0:
            reasons.append(f"{BR_COSTS_EXCEED_EXPECTED_EDGE}:"
                           f"net={net:+.3f}%<=0")

        if reasons:
            decision = DECISION_REJECTED
        else:
            decision = DECISION_CANDIDATE

        return PredictionRecord(
            symbol=symbol, direction=direction, ts=now_ms, cycle_id=cycle_id,
            decision=decision, regime=regime, signal_score=score,
            quality_threshold=theta, quality_pass=qpass,
            calibrated_probability=prob, probability_is_calibrated=False,
            expected_gross_return_pct=round(gross, 4),
            expected_net_return_pct=(round(net, 4)
                                     if net is not None else None),
            expected_transaction_costs_pct=(round(costs, 4)
                                            if costs is not None else None),
            funding_estimate_pct=(round(
                hold_bars * max(FUNDING_FLOOR_PER_BAR,
                                abs(rate) * 100.0 / 2.0), 4)
                if rate is not None else None),
            slippage_estimate_pct=(round(slip, 4)
                                   if slip is not None else None),
            prediction_uncertainty_pct=uncertainty,
            stop_distance_pct=(sizing or {}).get("stop_distance_pct"),
            expected_holding_bars=hold_bars,
            expected_holding_hours=round(hold_bars * 4.0, 1),
            rejection_reasons=tuple(reasons),
            price=price, spread_pct=spread_pct, quote_volume_24h=qv,
            last_funding_rate=rate, momentum=mom, mom_t=mom_t,
            trend_confirmation=trend_c, volatility_context=vol_ctx,
            volume_confirmation=vol_conf, sigma_bar=sigma_bar,
            sigma_annualized=(sigma_bar * math.sqrt(6.0 * 365.0)
                              if sigma_bar else None),
            sizing=sizing)


def cycle_decision(records) -> str:
    """Döngü kararı: aday YOK → NO_TRADE (yapay sinyal ÜRETİLMEZ)."""
    for r in records:
        if r.decision == DECISION_CANDIDATE:
            return DECISION_CANDIDATE
    return DECISION_NO_TRADE
