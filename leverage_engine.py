"""leverage_engine.py — Faz 3: bilimsel kaldıraç + pozisyon boyutlandırma.

Master Plan Bölüm 5 (kaldıraç) ve Bölüm 9 (boyutlandırma) formüllerini TEK ve
uyumlu bir birim altında toplar. İki lever artık AYNI (q, σ) anlık görüntüsünü
kullanır ve `margin = S / L` ilişkisiyle tutarlıdır.

FORMÜLLER
---------
Kaldıraç (Bölüm 5):
    L_raw = c · σ⁻¹ · q / (1 + d)
    L     = clip( L_raw , lo , min(hi, kelly_ceiling) )
Boyutlandırma (Bölüm 9):
    S_raw = bakiye · r_eff · q / σ
    S     = clip( S_raw , min_notional , max_notional )

  σ : volatilite (ATR%, kesir — ör. 0.02 = %2)
  q : sinyal güven skoru = trade_ledger'dan rolling WIN-RATE (0..1)
  d : mevcut drawdown (kesir — risk_engine.current_drawdown)
  c : kalibrasyon sabiti  (⚠️ CALIBRATION-REQUIRED — canlıda açmadan önce kalibre et)
  r : risk oranı (RISK_CONFIG.risk_per_trade_pct), Kelly ile tavanlanır

KELLY TAVANI (Bölüm 5)
----------------------
Kelly fraksiyonu geçmişten türetilir (q=p=win-rate, b=ort_kazanç/ort_kayıp):
    f* = (b·p − (1−p)) / b
Kaldıraç ve risk, fractional Kelly'nin GÜVENLİ bir kesriyle (ör. 1/4 Kelly)
tavanlanır:
    f_used        = kelly_fraction · max(f*, 0)       # kenar yoksa 0 → muhafazakâr
    edge          = clip( f_used / kelly_full_ref , 0, 1 )   # 0=kenar yok, 1=tam
    kelly_ceiling = lo + (hi − lo) · edge              # kenar yoksa tavan = lo
    r_eff         = min( r , f_used )                  # sizing riski Kelly ile sınırlı

COLD-START (reliable=False, n<min_sample): Kelly yerine cold_start bantları geçerli
(S: 12–15 USDT, L: 2x–3x) → veri yokken eski sisteme yakın başlangıç.

Kenar yok (f*≤0, reliable=True) → L tabana (lo) iner, r_eff=0 → S minimuma clip'lenir. Fail-safe.

GÜVENLİK: Bu modül SAF hesaplamadır; borsaya hiçbir şey göndermez. auto_trader'daki
üç-durumlu bayrak (off | compare | on) VARSAYILAN 'off' olduğundan, entegrasyon canlı
davranışı değiştirmez; 'compare' eski↔yeni değerleri yan yana loglar.
"""
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ⚠️ CALIBRATION-REQUIRED varsayılanlar — canlıda ('on') açmadan önce backtest/dry-run
# verisiyle kalibre edilmeli. 'off'/'compare' modunda canlı davranışı etkilemez.
LEV_ENGINE_CONFIG = {
    "c": 0.12,              # kalibrasyon sabiti (σ=0.02,q=0.5,d=0 → L≈3, mevcut merkeze yakın)
    "lo": 1.0,             # L alt sınırı
    "hi": 5.0,             # L üst sınırı (RISK_CONFIG.max_leverage ile hizalı)
    "kelly_fraction": 0.25,  # fractional Kelly (1/4 Kelly)
    "kelly_full_ref": 0.25,  # "tam kenar" sayılan fractional-Kelly değeri (edge normalizasyonu)
    "lookback": 50,        # q/p/b için son N kapanmış işlem
    "min_sample": 20,      # altında güvenilmez → fallback
    "b_cap": 10.0,         # payoff oranı üst sınırı (kayıpsız örnekte patlamayı önler)
    "q_fallback": 0.5,     # yetersiz geçmişte nötr güven
    # Cold-start (reliable=False): Kelly tavanı/r_eff geçmiş yokken tabana çöker (f*=0).
    # Eski sisteme yakın başlangıç için S ve L bu bantlara clip'lenir. reliable=True
    # olunca bu bantlar DEVRE DIŞI, formül Kelly'ye göre kendini ayarlar.
    "cold_start_min_notional": 12.0,   # S tabanı (USDT)
    "cold_start_max_notional": 15.0,   # S tavanı (USDT)
    "cold_start_lev_lo": 2.0,          # L tabanı
    "cold_start_lev_hi": 3.0,          # L tavanı
}


@dataclass
class SignalStats:
    """trade_ledger geçmişinden türetilmiş sinyal istatistikleri."""
    n: int            # örneklem (kapanmış işlem sayısı)
    p: float          # win-rate (0..1)
    b: float          # payoff oranı = ort_kazanç / ort_kayıp
    f_star: float     # Kelly fraksiyonu f* = (b·p−(1−p))/b, >=0 clamp
    q: float          # sinyal güven skoru (= p, veya yetersiz örnekte q_fallback)
    reliable: bool    # n >= min_sample mı


def signal_stats_from_ledger(db_file, cfg=LEV_ENGINE_CONFIG) -> SignalStats:
    """Son `lookback` kapanmış işlemden p (win-rate), b (payoff), f* ve q türetir.

    Yetersiz geçmiş (n < min_sample), veri yok veya DB hatası → muhafazakâr fallback:
    q=q_fallback, f*=0 (Kelly tavanı L'yi tabana indirir), reliable=False."""
    lookback = int(cfg["lookback"])
    try:
        conn = sqlite3.connect(str(db_file), timeout=10)
        try:
            rows = conn.execute(
                "SELECT net_pnl_quote FROM trade_ledger "
                "ORDER BY recorded_at DESC LIMIT ?", (lookback,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return SignalStats(0, 0.0, 0.0, 0.0, float(cfg["q_fallback"]), False)

    pnls = [float(r[0]) for r in rows if r[0] is not None]
    n = len(pnls)
    if n < int(cfg["min_sample"]):
        return SignalStats(n, 0.0, 0.0, 0.0, float(cfg["q_fallback"]), False)

    wins = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x < 0]   # pozitif büyüklük
    p = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    if avg_loss <= 0:
        # Kayıp yok: payoff'u b_cap ile sınırla (sonsuz kaldıraç yok).
        b = float(cfg["b_cap"]) if wins else 0.0
    else:
        b = min(avg_win / avg_loss, float(cfg["b_cap"]))
    f_star = ((b * p - (1.0 - p)) / b) if b > 0 else 0.0
    f_star = max(f_star, 0.0)   # negatif kenar → 0 (muhafazakâr)
    return SignalStats(n, p, b, f_star, p, True)


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def kelly_ceiling(stats: SignalStats, cfg=LEV_ENGINE_CONFIG) -> float:
    """Fractional-Kelly tabanlı kaldıraç tavanı: kenar yoksa lo, tam kenarda hi."""
    lo, hi = float(cfg["lo"]), float(cfg["hi"])
    f_used = float(cfg["kelly_fraction"]) * max(stats.f_star, 0.0)
    ref = float(cfg["kelly_full_ref"]) or 1.0
    edge = _clip(f_used / ref, 0.0, 1.0)
    return lo + (hi - lo) * edge


def scientific_leverage(sigma: float, stats: SignalStats, drawdown: float,
                        cfg=LEV_ENGINE_CONFIG) -> float:
    """L = clip( c·σ⁻¹·q/(1+d) , lo , min(hi, kelly_ceiling) ). σ≤0 → lo (fail-safe)."""
    lo, hi = float(cfg["lo"]), float(cfg["hi"])
    if sigma is None or sigma <= 0:
        return lo
    d = max(float(drawdown or 0.0), 0.0)
    l_raw = float(cfg["c"]) * (1.0 / sigma) * stats.q / (1.0 + d)
    if not stats.reliable:
        return _clip(l_raw, float(cfg["cold_start_lev_lo"]), min(hi, float(cfg["cold_start_lev_hi"])))
    hi_eff = min(hi, kelly_ceiling(stats, cfg))
    return _clip(l_raw, lo, hi_eff)


def scientific_position_size(balance: float, r: float, sigma: float,
                             stats: SignalStats, min_notional: float,
                             max_notional: float, cfg=LEV_ENGINE_CONFIG) -> float:
    """S = clip( bakiye·r_eff·q/σ , min_notional , max_notional ).
    r_eff = min(r, fractional-Kelly f_used): sizing riski Kelly ile tavanlanır.
    σ≤0 veya bakiye≤0 → min_notional (fail-safe)."""
    if sigma is None or sigma <= 0 or balance <= 0:
        return float(min_notional)
    if not stats.reliable:
        s_raw = balance * float(r) * stats.q / sigma
        s_hi = min(float(max_notional), float(cfg["cold_start_max_notional"]))
        s_lo = min(max(float(min_notional), float(cfg["cold_start_min_notional"])), s_hi)
        return _clip(s_raw, s_lo, s_hi)
    f_used = float(cfg["kelly_fraction"]) * max(stats.f_star, 0.0)
    r_eff = min(float(r), f_used)
    s_raw = balance * r_eff * stats.q / sigma
    return _clip(s_raw, float(min_notional), float(max_notional))


def margin_for(notional: float, leverage: float) -> float:
    """Birleştirme tutarlılığı: margin = S / L (L>0)."""
    return notional / leverage if leverage > 0 else float("inf")


# ----------------------------------------------------------------------------
# Faz 7 — birleşik boyutlandırma: Faz 3 (S, L) × RiskEngine (sabit riskler)
# ----------------------------------------------------------------------------
def liquidation_safe_leverage_cap(sl_pct, buffer=2.0, maint=0.005, lo=1.0):
    """Tasfiye mesafesi (≈ 1/L − bakım marjı) SL mesafesinin en az `buffer` katı olacak şekilde
    kaldıraç tavanı: L_max = 1 / (buffer·sl + maint). SL tasfiyeden ÖNCE tetiklenmeli."""
    if sl_pct is None or sl_pct <= 0:
        return float(lo)
    return max(float(lo), 1.0 / (buffer * float(sl_pct) + maint))


@dataclass
class UnifiedSizing:
    notional: float       # nihai S (USDT)
    leverage: int         # nihai L (tam sayı; borsaya bu gider)
    margin: float         # S / L
    s_faz3: float         # sınırlar uygulanmadan önce Faz 3 çıktısı
    l_faz3: float
    caps: list            # hangi sınırlar devreye girdi: risk_engine | liquidation | margin


def unified_sizing(balance, sigma, sl_pct, drawdown, stats, risk_cfg,
                   min_notional, max_notional, risk_notional=None, cfg=LEV_ENGINE_CONFIG):
    """Faz 3 formülünü RiskEngine değişmezleriyle birleştirir (risk her zaman önceliklidir):

      1) S  ≤ risk_notional  (RiskEngine.approve_trade: risk_per_trade·Kelly/(SL+fee) üst sınırı)
         → Faz 3 yalnız KÜÇÜLTEBİLİR, RiskEngine'in kayıp-bütçesini aşamaz.
      2) L  ≤ 1/(2·SL + bakım)  (SL, tasfiyeden önce tetiklenir); tam sayıya AŞAĞI yuvarlanır
         (yukarı yuvarlama tasfiye tavanını delebilirdi).
      3) margin = S/L ≤ max_total_margin_pct · bakiye  (tek işlem tüm marjin bütçesini yemez);
         aşılırsa S küçültülür.
    risk_notional=None (risk_guards kapalı) → yalnız sert tavanlar (min/max_notional) geçerli."""
    s3 = scientific_position_size(balance, risk_cfg["risk_per_trade_pct"], sigma, stats,
                                  min_notional, max_notional, cfg)
    l3 = scientific_leverage(sigma, stats, drawdown, cfg)
    caps = []
    s = s3
    if risk_notional is not None and risk_notional < s:
        s = float(risk_notional)
        caps.append("risk_engine")
    l_cap = liquidation_safe_leverage_cap(sl_pct)
    lev = max(1, int(round(l3)))
    lev_ceiling = max(1, int(math.floor(l_cap)))
    if lev > lev_ceiling:
        lev = lev_ceiling
        caps.append("liquidation")
    max_margin = float(risk_cfg.get("max_total_margin_pct", 0.4)) * max(float(balance), 0.0)
    if s / lev > max_margin:
        s = lev * max_margin
        caps.append("margin")
    return UnifiedSizing(s, lev, s / lev, s3, l3, caps)
