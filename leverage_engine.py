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

Kenar yok (f*≤0) → L tabana (lo) iner, r_eff=0 → S minimuma clip'lenir. Fail-safe.

GÜVENLİK: Bu modül SAF hesaplamadır; borsaya hiçbir şey göndermez. auto_trader'daki
üç-durumlu bayrak (off | compare | on) VARSAYILAN 'off' olduğundan, entegrasyon canlı
davranışı değiştirmez; 'compare' eski↔yeni değerleri yan yana loglar.
"""
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
    f_used = float(cfg["kelly_fraction"]) * max(stats.f_star, 0.0)
    r_eff = min(float(r), f_used)
    s_raw = balance * r_eff * stats.q / sigma
    return _clip(s_raw, float(min_notional), float(max_notional))


def margin_for(notional: float, leverage: float) -> float:
    """Birleştirme tutarlılığı: margin = S / L (L>0)."""
    return notional / leverage if leverage > 0 else float("inf")
