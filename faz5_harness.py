"""faz5_harness.py — B.3/B.4: FAZ 5 eşleştirilmiş-ikili simülatörü (SENTETİK).

BU MODÜL GERÇEK VERİYE DOKUNMAZ (B.6): yalnız saf fonksiyonlar + sentetik
testler. Kabul kapısı docs/FAZ5_PREREGISTRATION.md'dedir; insan onayı olmadan
koşu YOK. Maliyetler cm-v1-20261119 üzerine senaryo sarmalayıcısıdır;
kötümser senaryo DEĞERLERİ varsayımdır ve model_id "cm-v1-pess-v1" taşır.

Sözleşme:
  bars  = [(t, open, high, low, close), ...] kronolojik.
  signal= (t_sig, qty, hold_bars, tp_pct, sl_pct) — YOK-BAR SONRASI AÇILIŞTAN
          yürür (bakış-yönlü kazanç yok).
  Bariyer aynalama (B.3): LONG(E, +k TP, −m SL) ↔ SHORT(E, −k TP, +m SL);
  aynı çubukta önce SL kontrol edilir (muhafazakâr).
  Slippage KÖTÜMSEN'de her iki bacakta aleyhe uygulanır (alım ↑, satım ↓).
"""

FEE_RATE = 0.0005           # cm-v1: ÖLÇÜLÜ [FILL] 0.05%/side
SCENARIOS = {"optimist": dict(fee_rate=FEE_RATE, slip_bps=0.0),
             "base": dict(fee_rate=FEE_RATE, slip_bps=0.0),
             "pessimist": dict(fee_rate=FEE_RATE, slip_bps=25.0)}   # VARSAYIM
PESS_MODEL_ID = "cm-v1-pess-v1"


def mirror(signal):
    """Bariyer aynalama: yön ters, E aynı, TP/SL yüzdeleri aynı taraflı kalır."""
    t, qty, hold, tp, sl = signal
    return (t, qty, hold, tp, sl)     # yön motor tarafında terslenir; bariyer
                                      # mesafeleri simetrik tanımlıdır (B.3)


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
