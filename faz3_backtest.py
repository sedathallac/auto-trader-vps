"""faz3_backtest.py — FAZ3 replay: arşiv klines → strategy_engine(KOD-OLDUĞU-SPEC)
→ olaylar → portföy simülasyonu → config grid'i.

Ön-kayıt sözleşmeleri (PREREGISTRATION_FAZ3.md):
  * Sinyal = strategy_engine.confirm()/evaluate() @1971059; yeniden-yazım yok.
    Ağır okumalar (readings/regime/trend) bar başına BİR kez hesaplanır ve
    iki yön × iki min_fam kararında yeniden kullanılır — matematik değişmez,
    yalnız ÖNEMLİ ölçüde hız.
  * p_fake kalibrasyonu: store=None (nötr 0.5). signal_outcomes kalibrasyonu
    yalnız 13-19 Kas canlı verisi içeriyor — tarihsel replay'de olması
    LOOKAHEAD olurdu; nötr prior TEK dürüst seçimdir (DEVIATIONS §D-1).
  * Giriş = sinyali izleyen 15m barının AÇILIŞI; exit = SL/TP ilk temas
    (edge_engine.first_touch_outcome) ya da 16. bar kapanışı (TIME_LIMIT_SEC
    14400 = 4 saat, canlı ile aynı).
  * Maliyet: komisyon 0.001 round-trip (ölçülen %0.05/taraf [FILL]) + slippage
    round-trip = iki taraflı amprik |actual−trig|/trig örneği (20 algo
    tetiklemesi, [ALGO] 2026-11-19; deterministik tohum).
  * Funding: −0.6892 USDT / 11 gün / ~150 USDT ort. notional [INC] ⇒ günde
    −4.18bp pozisyon-notionalı; tutulan-bar'a oransız.
  * Portföy: ≤3 eşzamanlı, sembol başına 1; notional=min(0.20, 0.015/sl_pct)×
    equity; bileşik.
"""
import bisect
import csv
import json
import math
import os
import random

import edge_engine
import strategy_engine as se

TFS = ("15m", "1h", "4h")
MONTHS_A = ("2024-01", "2026-10")
BARS_TIME_LIMIT = 16          # 4h / 15m
FEE_ROUND_TRIP = 0.001        # ölçülen %0.05/taraf × 2  [FILL]
FUNDING_PER_DAY = -0.000418   # [INC] −0.6892/11g/$150  (sabit, raporda belirsizlik)
# [ALGO] algo_orders_20261119.ndjson FINISHED 20 tetiklemenin |actual−trig|/trig
# değerleri (ölçülmüş; medyan 9.9e-5):
SLIPPAGE_EMPIRICAL = [
    2.4e-05, 3.1e-05, 4.0e-05, 4.4e-05, 5.1e-05, 5.5e-05, 6.2e-05, 7.0e-05,
    7.7e-05, 8.6e-05, 9.9e-05, 1.05e-04, 1.18e-04, 1.31e-04, 1.47e-04,
    1.66e-04, 1.88e-04, 2.21e-04, 2.75e-04, 8.67e-04,
]


def _ym(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000,
                                           datetime.timezone.utc).strftime("%Y-%m")


def load_csv(path, ym_from, ym_to):
    """Binance arşiv CSV'si → klines-row listesi [t_ms,o,h,l,c,v,...].
    Header'lı/docsız iki biçimi de kabul eder; ay-penceresi süzülür (kapsam
    dışı komşu aylar ısıtma için OK — süzme KARAR pencerine uygulanır)."""
    rows = []
    with open(path) as fh:
        rd = csv.reader(fh)
        for r in rd:
            if not r or not r[0].isdigit() or len(r) < 12:
                continue                       # header / bozuk satır
            try:
                vals = [float(x) for x in r[1:12]]
            except ValueError:
                continue
            t = int(r[0])
            if t > 1e14:                 # mikro-sn aylar (2025-01 öncesi bazı setler)
                t //= 1000
            rows.append([t] + vals + [r[11]])
    return rows


class SymbolData:
    def __init__(self, sym, root, ym_from, ym_to):
        self.sym = sym
        self.r15 = self._load_tf(root, sym, "15m")
        self.r1h = self._load_tf(root, sym, "1h")
        self.r4h = self._load_tf(root, sym, "4h")
        self.ct1h = [r[6] for r in self.r1h]
        self.ct4h = [r[6] for r in self.r4h]

    @staticmethod
    def _load_tf(root, sym, tf):
        d = os.path.join(root, sym, tf)
        out = []
        if not os.path.isdir(d):
            return out
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".csv"):
                out.extend(load_csv(os.path.join(d, fn), "0000", "9999"))
        return out

    @staticmethod
    def _win(rows, ym_from, ym_to):
        return rows  # pencerleme karar döngüsünde (ısıtma önceki aylardan gelir)

    @staticmethod
    def window(rows, upto, limit=250):
        """(stub) ile canlı _get(limit) semantiği: son kapalı bar `upto` +
        oluşmakta-bar stub'ı; parse_klines stub'ı atar → canlı ile BİREBİR."""
        a = max(0, upto + 1 - limit)
        seg = rows[a:upto + 1]
        if not seg:
            return None
        return seg + [seg[-1]]          # stub = son kapalı kopya (parse'ta atılır)


def _cd(rows, upto, limit=250):
    seg = SymbolData.window(rows, upto, limit)
    return se.parse_klines(seg) if seg else None


def scan_symbol_decisions(sd, cfg_base, ym_from, ym_to):
    """Bar-başına bir kez: readings+regime+trends → her yön için
    (veto?, n_families) karar çiftleri (min_fam 1 ve 2 ayrılmış olarak).
    DÖNER: list[dict(i, t_open_next, atr_pct, side, veto, fams)]."""
    out = []
    if len(sd.r15) < 260:
        return out
    trend_cache = {}
    r15 = sd.r15
    for i in range(250, len(r15)):
        t_open_next = r15[i + 1][0] if i + 1 < len(r15) else None
        if t_open_next is None:
            break
        ym = _ym(r15[i][0])
        if not (ym_from <= ym <= ym_to):
            continue
        main = _cd(r15, i)
        if main is None or len(main) < 60:
            continue
        n1h = bisect.bisect_right(sd.ct1h, r15[i][0])   # closed-by-time
        n4h = bisect.bisect_right(sd.ct4h, r15[i][0])
        key = (n1h // 1, n4h // 1)
        if key not in trend_cache:
            cd1 = _cd(sd.r1h, n1h - 1) if n1h > 60 else None
            cd4 = _cd(sd.r4h, n4h - 1) if n4h > 60 else None
            t1 = se.trend_reading(cd1, "1h") if cd1 else None
            t4 = se.trend_reading(cd4, "4h") if cd4 else None
            trend_cache.clear()
            trend_cache[key] = (t1, t4, cd1, cd4)
        t1, t4, cd1, cd4 = trend_cache[key]
        if t1 is None or t4 is None:
            continue
        readings = (se.exhaustion_readings(main) + se.structure_readings(main)
                    + se.volume_readings(main))
        atr_arr = se.atr_series(main.h, main.l, main.c, 14)
        atr_pct = (atr_arr[-1] / main.c[-1]) if atr_arr and main.c[-1] else None
        if atr_pct is None or atr_pct <= 0:
            continue
        regime_adx = se.adx_last(main.h, main.l, main.c)
        regime = se.regime_from_adx(regime_adx, cfg_base)
        trends = [t1, t4]
        for side in (1, -1):
            fams = {}
            veto = False
            for min_fam in (1, 2):
                cfg = dict(cfg_base)
                cfg["confirm_min_families"] = min_fam
                v = se.evaluate(side, readings, trends, regime,
                                lambda n, r: (0.5, False), cfg,
                                regime_adx=regime_adx)
                if min_fam == 1:
                    # evaluate veto'yu verdict.veto ile bildirir; aile sayısı
                    # reason yerine fams sözlüğünden okunur.
                    veto = bool(v.veto)
                    fams = len(v.families or {})
            out.append({"sym": sd.sym, "i": i, "t": t_open_next,
                        "side": side, "veto": veto, "fams": fams,
                        "atr_pct": atr_pct})
    return out


def simulate_trades(sd, decisions_for_sym, cfg, rng_seed):
    """min_fam geçiren olaylar → trade'ler (first_touch_outcome / time-limit).
    DÖNER list[trade]. Karar barı i → giriş barı i+1 AÇILIŞI."""
    import sizing_policy
    cfg_sl = dict(cfg)
    trades = []
    # Determinizm: hash() PYTHONHASHSEED'e bağlı — bayt-toplamı kullanılır.
    rng = random.Random(rng_seed + (sum(sd.sym.encode()) % 10007))
    min_fam = cfg.get("confirm_min_families", 1)
    for d in decisions_for_sym:
        if d["veto"] or d["fams"] < min_fam:
            continue
        i = d["i"]
        if i + 1 >= len(sd.r15):
            continue
        entry = sd.r15[i + 1][1]
        sltp = sizing_policy.suggest_sl_tp(d["atr_pct"],
                                           direction=d["side"], cfg=cfg_sl)
        sl_pct, tp_pct = sltp.sl_pct, sltp.tp_pct
        fut_h = [r[2] for r in sd.r15[i + 1:i + 1 + BARS_TIME_LIMIT]]
        fut_l = [r[3] for r in sd.r15[i + 1:i + 1 + BARS_TIME_LIMIT]]
        fut_c = [r[4] for r in sd.r15[i + 1:i + 1 + BARS_TIME_LIMIT]]
        if len(fut_c) < BARS_TIME_LIMIT:
            continue
        slip = rng.choice(SLIPPAGE_EMPIRICAL) * 2      # round-trip karşılığı
        to = edge_engine.first_touch_outcome(
            d["side"], entry, fut_h, fut_l, fut_c, sl_pct, tp_pct,
            fee_pct=FEE_ROUND_TRIP, slippage_pct=slip)
        if to is not None and to.outcome in ("sl", "tp"):
            net, bars, reason = to.net_ret, to.bars_held, to.outcome
        else:
            exitp = fut_c[-1]
            net = edge_engine.net_return_pct(d["side"], entry, exitp,
                                             FEE_ROUND_TRIP, slip)
            bars, reason = BARS_TIME_LIMIT, "time"
        net += FUNDING_PER_DAY * (bars * 15.0 / 1440.0)
        trades.append({"sym": sd.sym, "side": d["side"],
                       "t_entry": sd.r15[i + 1][0],
                       "t_exit": sd.r15[min(i + 1 + bars - 1, len(sd.r15) - 1)][0],
                       "bars": bars, "reason": reason, "net_ret": net,
                       "sl_pct": sl_pct, "tp_pct": tp_pct})
    return trades


def simulate_portfolio(all_trades, equity0=1000.0, max_conc=3):
    """Zaman-sıralı portföy: ≤3 eşzamanlı, sembol başına 1, bileşik sizing,
    günlük equity → günlük getiriler + trade listesi (kaynak: BACKTEST)."""
    trades = sorted(all_trades, key=lambda t: (t["t_entry"], t["sym"]))
    open_pos, closed = [], []
    equity = equity0
    day_eq = {}
    for t in trades:
        # HÂLÂ AÇIK olanları tut (test yakaladı: ≤.predicate açıkları siliyordu)
        open_pos = [p for p in open_pos if p["t_exit"] > t["t_entry"]]
        if len(open_pos) >= max_conc or any(p["sym"] == t["sym"] for p in open_pos):
            continue
        notional = min(0.20, 0.015 / max(1e-9, t["sl_pct"])) * equity
        pnl = t["net_ret"] * notional
        t2 = dict(t)
        t2["notional"] = notional
        t2["quote_pnl"] = pnl
        equity += pnl
        t2["equity_after"] = equity
        open_pos.append(t2)
        closed.append(t2)
        import datetime
        day = datetime.datetime.fromtimestamp(t["t_exit"] / 1000,
                                              datetime.timezone.utc).strftime("%Y-%m-%d")
        day_eq[day] = equity
    days = sorted(day_eq)
    daily = []
    prev = equity0
    for d in days:
        daily.append(day_eq[d] / prev - 1.0)
        prev = day_eq[d]
    return closed, daily


GRID = [{"atr_sl_multiplier": a, "atr_tp_multiplier": t,
         "confirm_min_families": f}
        for a in (1.0, 1.5, 2.0) for t in (2.0, 3.0, 4.0) for f in (1, 2)]
