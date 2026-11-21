"""h5_engine.py — BLOK11 §3 H5 motoru (SAF, ÇEVRİMDIŞI).

TÜM sayısal çekirdek ön-kayıtlı kaynakların BİREBİR kopyasıdır; yeniden
yazım yoktur (tek-kaynak ilkesi, FAZ5_PREREG B.3). Kaynak SHA256'ları:

  faz5_harness.py  cc0f1010c5787bd20e7d4537b41e668ba559402ed32745253f03a98701ded4ef
      → FEE_RATE, SCENARIOS, PESS_MODEL_ID, mirror, _exit_index, run_round,
        run_pair  (birebir)
  faz7_h5_power.py c8877b450c51963236868be44e6cc64f863b1bdef564810bb0a92055c06bd669
      → invnorm, phi, dsr, boot_lo  (birebir; B.5 B=10000 çağrıda verilir)

Tek bilinçli fark: run_round_ex — run_round'un BİREBİR kopyası olup yalnız
dönüş demeti (+n_held, i0, entry_px, exit_px) ile döner; testler ikisinin
(gross, net, why) üçlüsünün her sentetik girdide aynı kaldığını doğrular.
Ek alanlar yalnız yüzdeseleştirme (net/notional) ve funding sürüklemesi
(bars) içindir — bariyer/çıkış mantığına dokunmaz.

Funding: FUNDING_PER_DAY=-0.000418 (faz3_backtest 5acd2adc... ile dondurulmuş
sabit) yüzde alanında HER İKİ bacağa da aynı sürüklenme olarak eklenir
(FAZ3 sözleşmesi). Ağ yok, saat yok, dosya yok, Kimlik okuma yok.
"""
import math

# ---------------- faz5_harness.py birebir kopya (cc0f1010) ----------------
FEE_RATE = 0.0005           # cm-v1: ÖLÇÜLÜ [FILL] 0.05%/side
SCENARIOS = {"optimist": dict(fee_rate=FEE_RATE, slip_bps=0.0),
             "base": dict(fee_rate=FEE_RATE, slip_bps=0.0),
             "pessimist": dict(fee_rate=FEE_RATE, slip_bps=25.0)}   # VARSAYIM
PESS_MODEL_ID = "cm-v1-pess-v1"


def mirror(signal):
    """Bariyer aynalama: yön ters, E aynı, TP/SL yüzdeleri aynı taraflı kalır."""
    t, qty, hold, tp, sl = signal
    return (t, qty, hold, tp, sl)     # yön motor tarafında terslenir; bariyer
    #                                   mesafeleri simetrik tanımlıdır (B.3)


def _exit_index(bars, t):
    import bisect
    ts = [b[0] for b in bars]
    return bisect.bisect_right(ts, t)      # yok-bar indexi


def run_round(bars, signal, side, fee_rate, slip_bps):
    """Tek tur: (gross, net, exit_reason). side=+1 LONG / −1 SHORT."""
    t, qty, hold, tp, sl = signal
    i0 = _exit_index(bars, t)
    if i0 >= len(bars):
        return 0.0, 0.0, "NO-BAR"
    s = 1.0 if side > 0 else -1.0
    adj = 1.0 + slip_bps / 1e4
    e_px = bars[i0][1] * (adj if s > 0 else 1 / adj)       # aleyhe giriş
    tp_px = e_px * (1 + s * tp)
    sl_px = e_px * (1 - s * sl)
    x_px, why, n = None, "HOLD", 0
    for n, b in enumerate(bars[i0:i0 + hold]):
        o, h, l, c = b[1], b[2], b[3], b[4]
        if (s > 0 and l <= sl_px) or (s < 0 and h >= sl_px):
            x_px, why = sl_px, "SL"
            break
        if (s > 0 and h >= tp_px) or (s < 0 and l <= tp_px):
            x_px, why = tp_px, "TP"
            break
    if x_px is None:
        j = min(i0 + hold - 1, len(bars) - 1)
        x_px, why = bars[j][4], "HOLD"
    x_eff = x_px / adj if s > 0 else x_px * adj            # aleyhe çıkış
    gross = s * (x_px - e_px) * qty
    turnover = (e_px + x_px) * qty
    net = gross - fee_rate * turnover
    return gross, net, why


def run_pair(bars, signals, fee_rate, slip_bps):
    """B.3-1 eşleştirilmiş ikili: HER sinyalde LONG ve SHORT birlikte;
    filtre yok. Döner: dict(long=(G,N,n), short=(G,N,n))."""
    out = {"long": [0.0, 0.0, 0], "short": [0.0, 0.0, 0]}
    for sig in signals:
        for side, key in ((1, "long"), (-1, "short")):
            g, net, _ = run_round(bars, sig, side, fee_rate, slip_bps)
            out[key][0] += g
            out[key][1] += net
            out[key][2] += 1
    return {k: tuple(v) for k, v in out.items()}


def run_round_ex(bars, signal, side, fee_rate, slip_bps, ts=None):
    """run_round'un birebir kopyası; dönüş +n_held, i0, entry_px, exit_px.
    (gross, net, why) üçlüsü run_round ile her girdide aynıdır (test edilir).
    ts: [b[0] for b in bars] önbelleği — YALNIZ hızlandırma; verilmezse
    run_round ile aynı yol (her çağrıda yeniden kurulur)."""
    import bisect
    t, qty, hold, tp, sl = signal
    i0 = bisect.bisect_right(ts, t) if ts is not None else _exit_index(bars, t)
    if i0 >= len(bars):
        return 0.0, 0.0, "NO-BAR", 0, i0, None, None
    s = 1.0 if side > 0 else -1.0
    adj = 1.0 + slip_bps / 1e4
    e_px = bars[i0][1] * (adj if s > 0 else 1 / adj)       # aleyhe giriş
    tp_px = e_px * (1 + s * tp)
    sl_px = e_px * (1 - s * sl)
    x_px, why, n = None, "HOLD", 0
    for n, b in enumerate(bars[i0:i0 + hold]):
        o, h, l, c = b[1], b[2], b[3], b[4]
        if (s > 0 and l <= sl_px) or (s < 0 and h >= sl_px):
            x_px, why = sl_px, "SL"
            break
        if (s > 0 and h >= tp_px) or (s < 0 and l <= tp_px):
            x_px, why = tp_px, "TP"
            break
    if x_px is None:
        j = min(i0 + hold - 1, len(bars) - 1)
        x_px, why = bars[j][4], "HOLD"
    x_eff = x_px / adj if s > 0 else x_px * adj            # aleyhe çıkış
    gross = s * (x_px - e_px) * qty
    turnover = (e_px + x_px) * qty
    net = gross - fee_rate * turnover
    return gross, net, why, n + 1, i0, e_px, x_px


# ---------------- faz7_h5_power.py birebir kopya (c8877b45) ----------------
GAMMA = 0.5772156649


def invnorm(p):
    # Acklam ters normal CDF (|hata|<1.15e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= 0.97575:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def dsr(series, ntrial):
    T = len(series)
    m = sum(series) / T
    v = sum((x - m) ** 2 for x in series) / (T - 1)
    if v <= 0:
        return -1.0
    sd = math.sqrt(v)
    g3 = sum((x - m) ** 3 for x in series) / T / sd ** 3
    g4 = sum((x - m) ** 4 for x in series) / T / sd ** 4
    sr = m / sd
    den = 1 - g3 * sr + (g4 - 1) / 4.0 * sr * sr
    if den <= 0:
        return -1.0
    sqrtT = math.sqrt(T - 1)
    z = invnorm
    # E[max SR] under ntrial iid trials: sqrt(V)·((1−γ)z(1−1/N)+γz(1−1/(Ne)))
    vmax = den / (T - 1)
    sr0 = math.sqrt(vmax) * ((1 - GAMMA) * z(1 - 1.0 / ntrial)
                             + GAMMA * z(1 - 1.0 / (ntrial * math.e)))
    return phi((sr - sr0) * sqrtT / math.sqrt(den))


def boot_lo(vals_s, vals_c, rng, B=10000):
    """küme-bootstrap: ağırlıklı ortalamanın %5 alt-ucu (0 kütleli eleme yok;
    seçim ağırlıkları eşit — B.5 küme-örnekleme tarifi)."""
    idx = [i for i, c in enumerate(vals_c) if c > 0]
    if len(idx) < 3:
        return -1.0
    means = []
    ch = rng.choices
    K = len(idx)
    for _ in range(B):
        s = c = 0
        for j in ch(idx, k=K):
            s += vals_s[j]
            c += vals_c[j]
        means.append(s / c if c else 0.0)
    means.sort()
    return means[int(0.05 * B)]


# ---------------- dondurulmuş funding (faz3_backtest 5acd2adc) ----------------
FUNDING_PER_DAY = -0.000418


def funding_drag_pct(bars_held, bars_minutes=15):
    """FAZ3 sözleşmesi: net_ret yüzdelerine günlük −0.000418 sürüklenmesi.
    Her iki bacağa aynı işaret (maliyet) olarak uygulanır."""
    return FUNDING_PER_DAY * (bars_held * bars_minutes / 1440.0)
