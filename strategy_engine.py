"""strategy_engine.py — Faz 6: indikatör/strateji TEYİT katmanı (SAF hesap, yalnız FİLTRE).

Strateji (decide_side, mean-reversion) DEĞİŞMEZ. Bu modül emir göndermez, yön üretmez:
mevcut bir adayın sahte (fake) olma olasılığını tahmin edip yalnız REDDEDEBİLİR.

ROLLER
  TREND  (1h, 4h)            → VETO: güçlü trende karşı mean-reversion (düşen bıçak)
  TÜKENME / YAPI / HACİM (15m)→ TEYİT: ≥ min_families FARKLI aileden uyumlu sinyal
  p_real = sigmoid( logit(prior) + Σ_aile ln((1−p_fake)/p_fake) )  — aile başına TEK terim
  Reddet ⇔ veri eksik (fail-closed) | trend veto | teyit yetersiz | p_real < min_p_real

Yalnız KAPANMIŞ mumlar kullanılır (look-ahead yok): fetch son (oluşmakta olan) mumu atar.
p_fake: signal_outcomes.db'deki (ayrı SQLite) sinyal×rejim Beta posterior'u; veri yoksa nötr 0.5.
"""
import math
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

CONFIRM_DEFAULTS = {
    "confirm_min_families": 2,
    "confirm_min_p_real": 0.65,          # yönetici kararı (0.55 çok düşük bulundu)
    "confirm_veto_trend_strength": 0.6,
    "confirm_adx_trend": 25.0,
    "confirm_adx_range": 20.0,
    "confirm_min_strength": 0.3,         # bir sinyalin oy sayılması için
    "confirm_prior_real": 0.5,
    "confirm_min_events": 100,           # posterior'un güvenilir sayılması için (declustered)
    "confirm_beta_a0": 1.0, "confirm_beta_b0": 1.0,
    "confirm_horizon": 8,                # 15m mum (=2 saat)
    "confirm_tf_main": "15m",
    "confirm_tf_veto": ("1h", "4h"),
    "confirm_kline_limit": 250,
    "confirm_fdr": 0.10,
}

FAM_TREND, FAM_EXHAUSTION, FAM_STRUCTURE, FAM_VOLUME = "trend", "exhaustion", "structure", "volume"
CONFIRM_FAMILIES = (FAM_EXHAUSTION, FAM_STRUCTURE, FAM_VOLUME)


def _cfg(cfg, key):
    return (cfg or {}).get(key, CONFIRM_DEFAULTS[key])


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


# ===================================================================== göstergeler (saf)
def ema(values, n):
    """Seed = ilk n değerin SMA'sı, sonra α=2/(n+1). Yetersiz veri → []. Dönen liste values[n-1:] hizalı."""
    if n <= 0 or len(values) < n:
        return []
    k = 2.0 / (n + 1)
    cur = sum(values[:n]) / n
    out = [cur]
    for v in values[n:]:
        cur = v * k + cur * (1 - k)
        out.append(cur)
    return out


def _wilder(values, n):
    """Wilder düzleştirme: ilk = ilk n'in ortalaması; sonra (prev*(n-1)+x)/n."""
    if len(values) < n:
        return []
    cur = sum(values[:n]) / n
    out = [cur]
    for v in values[n:]:
        cur = (cur * (n - 1) + v) / n
        out.append(cur)
    return out


def rsi_series(closes, n=14):
    """Wilder RSI serisi (closes[n:] ile hizalı). Yetersiz veri → []."""
    if len(closes) < n + 1:
        return []
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in ch]
    losses = [max(-c, 0.0) for c in ch]
    ag, al = _wilder(gains, n), _wilder(losses, n)
    out = []
    for g, l in zip(ag, al):
        out.append(100.0 if l == 0 and g > 0 else (50.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)))
    return out


def true_ranges(h, l, c):
    return [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def atr_series(h, l, c, n=14):
    return _wilder(true_ranges(h, l, c), n)


def adx_last(h, l, c, n=14):
    """Wilder ADX son değeri; yetersiz veri (< 2n+1 mum) → None."""
    if len(c) < 2 * n + 1:
        return None
    pdm, mdm = [], []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    tr = true_ranges(h, l, c)

    def smooth(x):   # Wilder toplamı
        cur = sum(x[:n])
        out = [cur]
        for v in x[n:]:
            cur = cur - cur / n + v
            out.append(cur)
        return out
    s_tr, s_p, s_m = smooth(tr), smooth(pdm), smooth(mdm)
    dx = []
    for a, p, m in zip(s_tr, s_p, s_m):
        if a <= 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / a, 100 * m / a
        dx.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    adx = _wilder(dx, n)
    return adx[-1] if adx else None


def supertrend_dir(h, l, c, atr_n=10, mult=3.0):
    """Son Supertrend yönü: +1 (yukarı/boğa), −1 (aşağı/ayı); yetersiz veri → 0."""
    atr = atr_series(h, l, c, atr_n)
    if not atr:
        return 0
    off = len(c) - len(atr)             # atr[i] ↔ c[i+off]
    direction, f_up, f_dn = 1, None, None
    for i, a in enumerate(atr):
        idx = i + off
        mid = (h[idx] + l[idx]) / 2.0
        up, dn = mid + mult * a, mid - mult * a
        if f_up is None:
            f_up, f_dn = up, dn
            direction = 1 if c[idx] >= mid else -1
            continue
        prev_c = c[idx - 1]
        f_up = up if (up < f_up or prev_c > f_up) else f_up
        f_dn = dn if (dn > f_dn or prev_c < f_dn) else f_dn
        if direction == 1 and c[idx] < f_dn:
            direction = -1
        elif direction == -1 and c[idx] > f_up:
            direction = 1
    return direction


def bollinger_z(closes, n=20):
    """Son kapanışın n-periyot ortalamasından z-skoru; yetersiz/sabit → None."""
    if len(closes) < n:
        return None
    w = closes[-n:]
    sd = statistics.pstdev(w)
    return None if sd == 0 else (closes[-1] - sum(w) / n) / sd


def swing_levels(h, l, left=3, right=3, tol=0.0):
    """Pivot tepe/dipler kümelenir (|Δ|≤tol aynı seviye): [(price, touches, kind)] kind='support'|'resistance'."""
    raw = []
    for i in range(left, len(h) - right):
        if l[i] == min(l[i - left:i + right + 1]):
            raw.append((l[i], "support"))
        if h[i] == max(h[i - left:i + right + 1]):
            raw.append((h[i], "resistance"))
    out = []
    for kind in ("support", "resistance"):
        pts = sorted(p for p, k in raw if k == kind)
        cluster = []
        for p in pts:
            if cluster and p - cluster[-1] > tol:
                out.append((sum(cluster) / len(cluster), len(cluster), kind))
                cluster = []
            cluster.append(p)
        if cluster:
            out.append((sum(cluster) / len(cluster), len(cluster), kind))
    return out


def rsi_divergence(lows, highs, rsi, width=2, window=40):
    """Boğa: fiyat LL + RSI HL → +1. Ayı: fiyat HH + RSI LH → −1. Yok → 0. rsi, lows/highs son kısmıyla hizalı."""
    n = min(len(rsi), len(lows), len(highs), window)
    if n < 2 * width + 3:
        return 0
    lw, hg, rs = lows[-n:], highs[-n:], rsi[-n:]
    piv_lo = [i for i in range(width, n - width) if lw[i] == min(lw[i - width:i + width + 1])]
    piv_hi = [i for i in range(width, n - width) if hg[i] == max(hg[i - width:i + width + 1])]
    if len(piv_lo) >= 2 and lw[piv_lo[-1]] < lw[piv_lo[-2]] and rs[piv_lo[-1]] > rs[piv_lo[-2]]:
        return 1
    if len(piv_hi) >= 2 and hg[piv_hi[-1]] > hg[piv_hi[-2]] and rs[piv_hi[-1]] < rs[piv_hi[-2]]:
        return -1
    return 0


# ===================================================================== mum verisi
@dataclass
class Candles:
    o: list
    h: list
    l: list
    c: list
    v: list
    t: list      # açılış zamanı (ms)
    tb: list = field(default_factory=list)   # taker-buy base hacmi (klines[9]); yoksa boş

    def __len__(self):
        return len(self.c)


def parse_klines(rows, drop_unclosed=True):
    """Binance klines → Candles. Son (oluşmakta olan) mum ATILIR → look-ahead/repainting yok."""
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    if drop_unclosed:
        rows = rows[:-1]
    try:
        tb = [float(r[9]) for r in rows] if all(len(r) > 9 for r in rows) else []
        return Candles([float(r[1]) for r in rows], [float(r[2]) for r in rows],
                       [float(r[3]) for r in rows], [float(r[4]) for r in rows],
                       [float(r[5]) for r in rows], [int(r[0]) for r in rows], tb)
    except (ValueError, TypeError, IndexError):
        return None


def fetch_candles(binance, symbol, tf, limit=250):
    """Public klines (anahtarsız GET). Hata → None (fail-closed)."""
    try:
        return parse_klines(binance._get("/fapi/v1/klines",
                                         {"symbol": symbol, "interval": tf, "limit": limit}))
    except Exception:
        return None


# ===================================================================== sinyal okumaları
@dataclass
class SignalReading:
    family: str
    name: str
    direction: int          # -1 / 0 / +1 (fiyatın beklenen dönüş yönü; TREND'de trend yönü)
    strength: float         # [0,1]
    tf: str = ""
    ok: bool = True


def trend_reading(cd: Candles, tf):
    """TREND: Supertrend + EMA50/200 + ADX → (yön skoru [-1,1], adx_gücü [0,1], adx). Yetersiz veri → None."""
    if cd is None or len(cd) < 210:
        return None
    st = supertrend_dir(cd.h, cd.l, cd.c)
    e50, e200 = ema(cd.c, 50), ema(cd.c, 200)
    if not e50 or not e200:
        return None
    ema_dir = 1 if e50[-1] > e200[-1] else -1
    adx = adx_last(cd.h, cd.l, cd.c)
    if adx is None:
        return None
    return {"tf": tf, "score": 0.5 * st + 0.5 * ema_dir, "st": st, "ema_dir": ema_dir,
            "adx": adx, "adx_strength": _clip((adx - 15.0) / 25.0, 0.0, 1.0)}


def exhaustion_readings(cd: Candles):
    out = []
    rsi = rsi_series(cd.c, 14)
    if rsi:
        r = rsi[-1]
        if r < 35:
            out.append(SignalReading(FAM_EXHAUSTION, "rsi_extreme", 1, _clip((35 - r) / 20.0, 0, 1), "15m"))
        elif r > 65:
            out.append(SignalReading(FAM_EXHAUSTION, "rsi_extreme", -1, _clip((r - 65) / 20.0, 0, 1), "15m"))
        div = rsi_divergence(cd.l, cd.h, rsi)
        if div:
            out.append(SignalReading(FAM_EXHAUSTION, "rsi_divergence", div, 0.7, "15m"))
    z = bollinger_z(cd.c, 20)
    if z is not None and abs(z) >= 1.5:
        out.append(SignalReading(FAM_EXHAUSTION, "bollinger_z", 1 if z < 0 else -1,
                                 _clip((abs(z) - 1.5) / 1.0, 0, 1), "15m"))
    return out


def structure_readings(cd: Candles):
    out = []
    atr = atr_series(cd.h, cd.l, cd.c, 14)
    if atr:
        a = atr[-1]
        lv = swing_levels(cd.h[-120:], cd.l[-120:], 3, 3, tol=0.5 * a)
        px = cd.c[-1]
        for price, touches, kind in lv:
            d = abs(px - price)
            if d <= a:
                direction = 1 if kind == "support" else -1
                strength = _clip(min(touches, 3) / 3.0 * (1.0 - d / a), 0, 1)
                out.append(SignalReading(FAM_STRUCTURE, "sr_proximity", direction, strength, "15m"))
                break
    rng = cd.h[-1] - cd.l[-1]
    if rng > 0:
        lower = (min(cd.o[-1], cd.c[-1]) - cd.l[-1]) / rng
        upper = (cd.h[-1] - max(cd.o[-1], cd.c[-1])) / rng
        if lower >= 0.5 and cd.c[-1] >= (cd.h[-1] + cd.l[-1]) / 2:
            out.append(SignalReading(FAM_STRUCTURE, "wick_rejection", 1, _clip(lower, 0, 1), "15m"))
        elif upper >= 0.5 and cd.c[-1] <= (cd.h[-1] + cd.l[-1]) / 2:
            out.append(SignalReading(FAM_STRUCTURE, "wick_rejection", -1, _clip(upper, 0, 1), "15m"))
    return out


def volume_readings(cd: Candles):
    """Hacim klimaksı: ucuzlayan mumda 3× medyan spike, sonraki mumda sönme → tükenme (karşı yön)."""
    if len(cd) < 23:
        return []
    med = statistics.median(cd.v[-22:-2])
    if med <= 0:
        return []
    spike, nxt = cd.v[-2], cd.v[-1]
    if spike > 3.0 * med and nxt < spike:
        selling = cd.c[-2] < cd.o[-2]
        return [SignalReading(FAM_VOLUME, "volume_climax", 1 if selling else -1,
                              _clip(spike / med / 6.0, 0, 1), "15m")]
    return []


def regime_from_adx(adx, cfg=None):
    if adx is None:
        return "unknown"
    if adx >= _cfg(cfg, "confirm_adx_trend"):
        return "trend"
    if adx < _cfg(cfg, "confirm_adx_range"):
        return "range"
    return "mid"


# ===================================================================== birleştirme
@dataclass
class Verdict:
    allow: bool
    reason: str
    p_real: float = 0.0
    regime: str = "unknown"
    veto: bool = False
    families: dict = field(default_factory=dict)
    readings: list = field(default_factory=list)
    trend_against: float = 0.0
    calibrated: bool = False


def trend_against_strength(trends, side_dir, weights=None):
    """side_dir'e KARŞI trend gücü [0,1]: ağırlıklı ort. max(0, −side·skor)·adx_gücü. 4h ağır basar."""
    weights = weights or {"1h": 0.4, "4h": 0.6}
    tot = w_sum = 0.0
    for t in trends:
        w = weights.get(t["tf"], 0.5)
        tot += w * max(0.0, -side_dir * t["score"]) * t["adx_strength"]
        w_sum += w
    return tot / w_sum if w_sum else 0.0


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def compute_readings(main: Candles):
    """15m Candles → tüm teyit sinyalleri (saf)."""
    return exhaustion_readings(main) + structure_readings(main) + volume_readings(main)


def evaluate(side_dir, readings, trends, regime, p_fake_fn, cfg=None) -> Verdict:
    """KARAR mantığı (veri toplamadan bağımsız; canlı confirm() ve offline backtest AYNI kodu kullanır).
    p_fake_fn(signal_name, regime) → (p_fake, calibrated)."""
    # 1) REJİM VETO — güçlü trende karşı mean-reversion
    against = trend_against_strength(trends, side_dir)
    adx_max = max(t["adx"] for t in trends)
    if (against >= _cfg(cfg, "confirm_veto_trend_strength")
            and adx_max >= _cfg(cfg, "confirm_adx_trend")):
        return Verdict(False, f"güçlü trende karşı mean-reversion (against={against:.2f}, ADX={adx_max:.0f})",
                       regime=regime, veto=True, readings=readings, trend_against=against)

    # 2) TEYİT — aile başına en iyi uyumlu sinyal
    best, calibrated_all = {}, True
    for r in readings:
        if r.direction != side_dir or r.strength < _cfg(cfg, "confirm_min_strength"):
            continue
        p_fake, cal = p_fake_fn(r.name, regime)
        score = (1.0 - p_fake) * r.strength
        if r.family not in best or score > best[r.family][0]:
            best[r.family] = (score, r, p_fake, cal)
    if len(best) < _cfg(cfg, "confirm_min_families"):
        return Verdict(False, f"teyit yetersiz ({len(best)}/{_cfg(cfg, 'confirm_min_families')} aile)",
                       regime=regime, families={f: b[1].name for f, b in best.items()},
                       readings=readings, trend_against=against)

    # 3) Birleşik güven — aile başına TEK LLR terimi (aile içi çift sayım yok)
    prior = _clip(_cfg(cfg, "confirm_prior_real"), 0.01, 0.99)
    logit = math.log(prior / (1 - prior))
    for _, r, pf, cal in best.values():
        pf = _clip(pf, 0.05, 0.95)
        logit += math.log((1 - pf) / pf)
        calibrated_all = calibrated_all and cal
    p_real = _sigmoid(logit)
    allow = p_real >= _cfg(cfg, "confirm_min_p_real")
    return Verdict(allow, (f"p_real={p_real:.3f} " + ("≥" if allow else "<")
                           + f" {_cfg(cfg, 'confirm_min_p_real'):.2f}"),
                   p_real, regime, False, {f: b[1].name for f, b in best.items()},
                   readings, against, calibrated_all)


def confirm(side_dir, main: Candles, trend_cds: dict, store=None, symbol="", cfg=None) -> Verdict:
    """side_dir: +1 LONG / −1 SHORT. main=15m Candles; trend_cds={'1h':Candles,'4h':Candles}.
    Eksik veri → fail-closed. YALNIZ allow/deny üretir."""
    if main is None or len(main) < 60 or not trend_cds or any(v is None for v in trend_cds.values()):
        return Verdict(False, "veri eksik (fail-closed)")
    trends = []
    for tf, cd in trend_cds.items():
        t = trend_reading(cd, tf)
        if t is None:
            return Verdict(False, f"{tf} trend verisi yetersiz (fail-closed)")
        trends.append(t)
    regime = regime_from_adx(adx_last(main.h, main.l, main.c), cfg)
    readings = exhaustion_readings(main) + structure_readings(main) + volume_readings(main)
    p_fake_fn = (lambda name, reg: store.p_fake(name, reg, cfg)) if store is not None \
        else (lambda name, reg: (0.5, False))
    return evaluate(side_dir, readings, trends, regime, p_fake_fn, cfg)


# ===================================================================== istatistik araçları
def wilson_interval(wins, n, z=1.96):
    """Wilson skor aralığı (lo, hi); n=0 → (0,1)."""
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def permutation_pvalue(sig_flags, base_flags, n_perm=1000, seed=0):
    """H0: sinyal oranı ≤ baseline. Tek yönlü; (sinyal_ort − baseline_ort) permütasyon p-değeri."""
    if not sig_flags or not base_flags:
        return 1.0
    rng = random.Random(seed)
    obs = sum(sig_flags) / len(sig_flags) - sum(base_flags) / len(base_flags)
    pool = list(sig_flags) + list(base_flags)
    ns, hits = len(sig_flags), 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        d = sum(pool[:ns]) / ns - sum(pool[ns:]) / (len(pool) - ns)
        hits += d >= obs
    return (hits + 1) / (n_perm + 1)


def benjamini_hochberg(pvals, q=0.10):
    """BH-FDR: her p için reddet(True)/etme(False) listesi (giriş sırasında)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    k_max = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            k_max = rank
    rej = [False] * m
    for rank, i in enumerate(order, 1):
        if rank <= k_max:
            rej[i] = True
    return rej


def kaplan_meier(durations, observed):
    """Yönü koruma süresi sağkalım eğrisi: [(t, S(t))]. observed=False → sansürlü (ufuk sonuna dek korundu)."""
    pairs = sorted(zip(durations, observed))
    at_risk, s, curve, i = len(pairs), 1.0, [], 0
    while i < len(pairs):
        t = pairs[i][0]
        d = c = 0
        while i < len(pairs) and pairs[i][0] == t:
            d += 1 if pairs[i][1] else 0
            c += 1
            i += 1
        if d:
            s *= 1 - d / at_risk
            curve.append((t, s))
        at_risk -= c
    return curve


def outcome_from_bars(direction, entry, fut_h, fut_l, fut_c, horizon, cost, sl_pct):
    """real ⇔ H sonunda getiri > maliyet VE ufukta SL aşılmadı. (real, fwd_ret, persist_bars) döner.
    persist_bars: yönde kapanışın ilk kez giriş fiyatının KARŞI tarafına geçtiği mum (yoksa horizon)."""
    n = min(horizon, len(fut_c))
    if n < horizon:
        return None
    fwd = direction * (fut_c[horizon - 1] - entry) / entry
    mae = min(direction * ((fut_l[i] if direction > 0 else fut_h[i]) - entry) / entry for i in range(n))
    persist = next((i + 1 for i in range(n) if direction * (fut_c[i] - entry) < 0), horizon)
    return (fwd > cost and mae > -sl_pct), fwd, persist


# ===================================================================== signal_outcomes (AYRI sqlite)
SIGNAL_DB = str(Path.home() / "signal_outcomes.db")

_SCHEMA = """CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at REAL NOT NULL,
    symbol TEXT NOT NULL,
    signal TEXT NOT NULL,
    family TEXT NOT NULL,
    regime TEXT NOT NULL,
    direction INTEGER NOT NULL,
    strength REAL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    horizon INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    resolved INTEGER NOT NULL DEFAULT 0,
    real INTEGER,
    fwd_ret REAL,
    persist_bars INTEGER
)"""


class SignalStore:
    """signal_outcomes.db — risk_ledger.db'ye DOKUNMAZ. Tüm hatalar sessizce yutulur (filtre kritik yol değil)."""

    def __init__(self, path=SIGNAL_DB):
        self.path = str(path)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.execute(_SCHEMA)
        return c

    def record_event(self, symbol, reading, regime, entry_ts, entry_price, horizon, source="live"):
        try:
            c = self._conn()
            try:
                dup = c.execute("SELECT 1 FROM signal_outcomes WHERE symbol=? AND signal=? AND direction=? "
                                "AND entry_ts=?", (symbol, reading.name, reading.direction, entry_ts)).fetchone()
                if dup:
                    return False
                c.execute("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,"
                          "strength,entry_ts,entry_price,horizon,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (time.time(), symbol, reading.name, reading.family, regime, reading.direction,
                           reading.strength, int(entry_ts), float(entry_price), int(horizon), source))
                c.commit()
                return True
            finally:
                c.close()
        except Exception:
            return False

    def record_resolved(self, symbol, reading, regime, entry_ts, entry_price, horizon,
                        real, fwd_ret, persist_bars, source="offline"):
        """Offline backtest satırı (zaten çözülmüş). Aynı (symbol,signal,direction,entry_ts) tekrar yazılmaz."""
        try:
            c = self._conn()
            try:
                dup = c.execute("SELECT 1 FROM signal_outcomes WHERE symbol=? AND signal=? AND direction=? "
                                "AND entry_ts=? AND source=?", (symbol, reading.name, reading.direction,
                                                                int(entry_ts), source)).fetchone()
                if dup:
                    return False
                c.execute("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,"
                          "strength,entry_ts,entry_price,horizon,source,resolved,real,fwd_ret,persist_bars) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                          (time.time(), symbol, reading.name, reading.family, regime, reading.direction,
                           reading.strength, int(entry_ts), float(entry_price), int(horizon), source,
                           int(bool(real)), float(fwd_ret), int(persist_bars)))
                c.commit()
                return True
            finally:
                c.close()
        except Exception:
            return False

    def resolve_pending(self, symbol, cd: Candles, cost, sl_pct):
        """Bu sembolün çözülmemiş olaylarını (entry_ts mumundan sonra horizon kadar KAPANMIŞ mum varsa) çözer."""
        done = 0
        try:
            c = self._conn()
            try:
                rows = c.execute("SELECT id,direction,entry_ts,entry_price,horizon FROM signal_outcomes "
                                 "WHERE symbol=? AND resolved=0", (symbol,)).fetchall()
                for id_, d, ts, px, hz in rows:
                    idx = [i for i, t in enumerate(cd.t) if t > ts]
                    if len(idx) < hz:
                        continue
                    sl = idx[:hz]
                    res = outcome_from_bars(d, px, [cd.h[i] for i in sl], [cd.l[i] for i in sl],
                                            [cd.c[i] for i in sl], hz, cost, sl_pct)
                    if res is None:
                        continue
                    real, fwd, persist = res
                    c.execute("UPDATE signal_outcomes SET resolved=1, real=?, fwd_ret=?, persist_bars=? "
                              "WHERE id=?", (int(real), fwd, persist, id_))
                    done += 1
                c.commit()
            finally:
                c.close()
        except Exception:
            pass
        return done

    def p_fake(self, signal, regime, cfg=None):
        """Sinyal×rejim Beta posterior → (p_fake, calibrated). n<min_events → nötr (0.5, False)."""
        try:
            c = self._conn()
            try:
                wins, n = c.execute("SELECT COALESCE(SUM(real),0), COUNT(*) FROM signal_outcomes "
                                    "WHERE signal=? AND regime=? AND resolved=1",
                                    (signal, regime)).fetchone()
            finally:
                c.close()
        except Exception:
            return 0.5, False
        if n < _cfg(cfg, "confirm_min_events"):
            return 0.5, False
        a = _cfg(cfg, "confirm_beta_a0") + wins
        b = _cfg(cfg, "confirm_beta_b0") + (n - wins)
        return 1.0 - a / (a + b), True
