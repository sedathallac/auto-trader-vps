"""canary_quality.py — BLOK32: sinyal kalite modeli (operatör politikası
2026-11-25: çift yönlü LONG/SHORT quality-based target-following giriş).

OPERATÖR KARARI: canary artık yalnızca momentum flip'inde değil; hedef yön
(engine target) her YENİ kapalı mumpda kalite eşiğini geçtiğinde de giriş
yapabilir. LONG ve SHORT kalitesi BAĞIMSIZ ölçülür ve ayrı eşiklere tabidir.
Kalitesi yetersiz sinyalde işlem AÇILMAZ; yapay sinyal üretilMEZ; eşikler
işlem sayısını artırmak için GEVŞETİLMEZ.

KALİBRASYON (~/blok32-calibration, 2026-11-25):
  Veri        : 8 sembol 4h klines 2022-01→2026-11-25 (ETH 2020'den).
                Chronological split: train ≤2023 / val 2024 / OOS 2025-2026.
  Maliyetler  : taker komisyon %0.05 + slippage %0.03 (yön başına) +
                funding %0.005/4h-bar (pozisyona karşı — konservatif).
  Eşik seçimi : YALNIZ train+val üzerinde, yön başına net expectancy maksimize
                (≥25 işlem kısıtıyla). OOS tek seferlik değerlendirildi.
  Sonuç (OOS) : LONG 44 işlem exp +1.60 PF 1.30 | SHORT 50 işlem exp +0.99
                PF 1.17 | flip-only baseline aynı dönemde net −211.9 (LONG
                negatif). Bootstrap %95 CI her iki yönde sıfırı içerir
                (LONG [−3.29,+7.26], SHORT [−2.89,+5.01]) → OOS performansı
                PARTIAL olarak raporlanır; nokta tahminler pozitif ve
                2025/2026 yılları tutarlıdır.

SKOR TANIMI (0-100, yön d için):
  mom_t    = m / (sigma_bar * sqrt(180))           — momentumun volatiliteye
             göre normalize gücü (t-istatistiği benzeri); |mom_t| TRAIN
             yüzdelineğine göre [0,1]'e ölçeklenir, YÖN UYUMU (sign(m)==d)
             ile çarpılır.
  trend    = son 45/90/135 barlık alt-momentumların sign(m) ile uyum oranı
             ([0,1], TRAIN yüzdeliği), yön uyumu ile çarpılır.
  vol_reg  = sigma_bar / son 500 barın sigma medyanı — düşük volatilite
             rejimi iyidir: (1 − yüzdelik) katkısı.
  vol_conf = bar hacmi / son 20 bar ortalama hacim (≥3'te kırpılır), TRAIN
             yüzdeliği.
  score_d  = 100 * (0.40*mom + 0.25*trend + 0.15*volreg + 0.20*volconf)
  Ağırlıklar: momentum ve trend yön belirleyici ana bileşenler; volatilite
  rejimi ve hacim doğrulayıcı. Skor ≤ 0.40+0.25 = %65 üst bileşen olmadan
  eşik (≥66) GEÇİLEMEZ → momentumu ters yönde olan sembol o yönde ASLA
  kalite geçemez (yanlış yön/skor uyumsuzluğu yapısal olarak engelli).

HYSTERESIS (gir-çık engeli): episode ARMED→ENGAGED (giriş) sonrası yeniden
ARMED olabilmesi için skor θ−h altına DÜŞMELİ. h = train'te ardışık barlar
arası |Δskor| medyanı (LONG 4.0 / SHORT 4.1) — normal bar-bar skor
dalgalanması episode'u yeniden silahlandıramaz.

FAIL-CLOSED: herhangi bir bileşen hesaplanamıyorsa (yetersiz geçmiş, veri
eksik) skor None döner → giriş KAPALI, nedeni loglanır. Hiçbir bileşen
"hesaplanıyormuş gibi" gösterilmez.
"""
import bisect
import json
import math
import os
from collections import deque
from typing import Optional

# ---- kalibre edilmiş parametreler (train+val; OOS'de DEĞİŞTİRİLMEDİ) -------
THRESHOLD_LONG = 66.5
THRESHOLD_SHORT = 66.0
HYST_LONG = 4.0
HYST_SHORT = 4.1

W_MOM, W_TREND, W_VOLREG, W_VOLCONF = 0.40, 0.25, 0.15, 0.20

ENTRY_POLICY_QUALITY = "QUALITY_BIDIRECTIONAL"   # BLOK32 üretim politikası
ENTRY_POLICY_FLIP_ONLY = "FLIP_ONLY"             # araştırma/kanonik davranış

_VOL_WIN = 20          # hacim ortalaması penceresi (bar)
_SIG_WIN = 500         # volatilite rejimi medyan penceresi (bar)
_SUBWINDOWS = (45, 90, 135)   # trend doğrulama alt-momentumları

_REF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "canary_quality_refs.json")
with open(_REF_PATH) as _f:
    _REFS = json.load(_f)     # train (≤2023) yüzdelinek tabloları, 1001 nokta


def _pct(ref_name, val) -> float:
    """TRAIN dağılımında val'in yüzdeliği [0,1] (1001 noktalı quantile
    tablosu; maksimum içpolasyon hatası 0.1 percentil → skorda ≤0.04)."""
    r = _REFS[ref_name]
    return bisect.bisect_left(r, val) / len(r)


class QualityModel:
    """Per-sembol kalite hesabı. Engine'in (H1SignalEngine) closes/var
    durumunu OKUR (momentum/trend/volatilite) + kendi tuttuğu hacim ve
    sigma geçmişinden (canlı ve warmup replay barlarıyla beslenir) hacim
    doğrulaması ve volatilite rejimi çıkarır. Engine durumunu DEĞİŞTİRMEZ.

    feed() her end_bar sonrası çağrılır (warmup replay dahil — restart
    sonrası geçmiş pencere kendini yeniden kurar; ek kalıcı durum YOK)."""

    def __init__(self, engine):
        self.engine = engine
        self._vols = {}      # sym -> deque[son _VOL_WIN bar hacmi]
        self._sigmas = {}    # sym -> deque[son _SIG_WIN bar sigma]

    def feed(self, symbol, volume: float, sigma_bar: Optional[float]):
        v = self._vols.setdefault(symbol, deque(maxlen=_VOL_WIN))
        v.append(volume if volume is not None else 0.0)
        s = self._sigmas.setdefault(symbol, deque(maxlen=_SIG_WIN))
        if sigma_bar is not None and sigma_bar > 0:
            s.append(sigma_bar)

    def compute(self, symbol: str, direction: int) -> Optional[dict]:
        """Yön d için kalite skoru. Bileşenlerden herhangi biri hesaplan-
        amıyorsa None (fail-closed). Dönen dict §3 sinyal nesnesi alanlarını
        taşır."""
        S = self.engine._state(symbol)
        closes = S["closes"]
        var = S["var"]
        n = S["n"]
        if n < self.engine.lookback + 1 or not closes or var is None or var <= 0:
            return None
        c = closes[-1]
        m = c / closes[n - 1 - self.engine.lookback] - 1.0
        if m == 0:
            return None                       # nötr momentum → kalite yok
        sigma_bar = math.sqrt(var)
        mom_t = m / (sigma_bar * math.sqrt(self.engine.lookback))
        # trend: alt-momentumların ana momentumla işaret uyumu
        agree = 0
        for k in _SUBWINDOWS:
            if n > k:
                mk = c / closes[n - 1 - k] - 1.0
                if (mk > 0) == (m > 0):
                    agree += 1
        trend = agree / len(_SUBWINDOWS)
        # volatilite rejimi: sigma / son 500 barın medyanı
        sigs = sorted(self._sigmas.get(symbol) or [])
        vol_reg = None
        if sigs:
            med = sigs[len(sigs) // 2]
            if med > 0:
                vol_reg = sigma_bar / med
        # hacim doğrulaması
        vols = self._vols.get(symbol) or []
        vol_conf = None
        if len(vols) >= _VOL_WIN and sum(vols) > 0:
            vol_conf = vols[-1] / (sum(vols) / len(vols))
        if vol_reg is None or vol_conf is None:
            return None                       # pencere henüz dolmadı
        align = 1.0 if (m > 0) == (direction > 0) else 0.0
        c_mom = _pct("mom_t", abs(mom_t))
        c_tr = _pct("trend", trend)
        c_vr = 1.0 - _pct("vol_reg", vol_reg)
        c_vc = _pct("vol_conf", min(vol_conf, 3.0))
        score = 100.0 * (W_MOM * c_mom * align + W_TREND * c_tr * align
                         + W_VOLREG * c_vr + W_VOLCONF * c_vc)
        return {
            "symbol": symbol,
            "direction": direction,
            "quality_score": round(score, 2),
            "quality_threshold": (THRESHOLD_LONG if direction > 0
                                  else THRESHOLD_SHORT),
            "quality_pass": score >= (THRESHOLD_LONG if direction > 0
                                      else THRESHOLD_SHORT),
            "momentum_strength": round(mom_t, 4),
            "trend_confirmation": round(trend, 3),
            "volatility_context": round(vol_reg, 4),
            "volume_confirmation": round(vol_conf, 4),
            "signal_reason": (
                f"m={m:+.4f} mom_t={mom_t:+.2f} trend={trend:.2f} "
                f"vol_reg={vol_reg:.2f} vol_conf={vol_conf:.2f} "
                f"score={score:.1f}/"
                f"{THRESHOLD_LONG if direction > 0 else THRESHOLD_SHORT}"),
        }


def threshold(direction: int) -> float:
    return THRESHOLD_LONG if direction > 0 else THRESHOLD_SHORT


def hysteresis(direction: int) -> float:
    return HYST_LONG if direction > 0 else HYST_SHORT
