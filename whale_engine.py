"""whale_engine.py — Faz 5: balina avcılığı (SAF sinyal/edge hesabı, FİLTRE olarak kullanılır).

Bu modül borsaya EMİR GÖNDERMEZ ve işlem TETİKLEMEZ. Yalnız mevcut bir adayın
(yön zaten decide_side ile belirlenmiş) kalitesiz/sahte (fakeout) olup olmadığına karar
verir. 'on' modunda yalnız REDDEDEBİLİR; giriş ekleyemez, yön/boyut/SL'i değiştirmez.

KARAR KURALI
    E_net = E[kâr] − (C_işlem + RP)          (hepsi notional S'in kesri)
    E[kâr] = p_win·G − (1−p_win)·L
    C_işlem = fee_rt + slip_in + 1.5·slip_out + funding_hold
    RP      = λσ·σ·(1+d) + belirsizlik
    Reddet  ⇔ veri yok (fail-closed) | balina yönü ters ∧ güçlü | E_net < eşik

Tüm eşikler RISK_CONFIG["whale_*"] altında (⚠️ CALIBRATION-REQUIRED, muhafazakâr başlangıç).
"""
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

WHALE_DEFAULTS = {
    "whale_obi_band_bps": 25,          # OBI bandı (mid ± bps)
    "whale_wall_mult": 8.0,            # duvar: kademe ≥ k × medyan kademe
    "whale_wall_min_notional": 250_000.0,
    "whale_wall_persist_s": 30.0,      # duvar kalıcılığı (spoof filtresi)
    "whale_gap_z": 3.0,                # likidite boşluğu z-skoru eşiği
    "whale_big_trade_notional": 100_000.0,   # "balina işlemi" mutlak eşiği
    "whale_dir_min_score": 0.15,       # altında yön = 0
    "whale_block_strength": 0.40,      # ters yönde bu güçte balina → RED
    "whale_min_net_edge_pct": 0.0015,  # min net kenar (S kesri) = %0.15
    "whale_edge_cap": 0.08,            # p_win en çok 0.5 ± 0.08 (cold-start)
    "whale_lambda_sigma": 0.10,        # risk primi: volatilite katsayısı
    "whale_uncertainty_pct": 0.0010,   # kalibre değilken sabit ek prim (10 bps)
    "whale_funding_extreme": 0.001,    # |funding| ≥ bu → kalabalık pozisyon, ek prim
    "whale_hold_hours": 4.0,           # beklenen tutma süresi (funding maliyeti)
    "whale_oi_norm": 0.02,             # OI Δ% normalizasyonu (%2 → tam skor)
    "whale_tp_wall_frac": 0.8,         # TP, karşı duvara mesafenin bu kesrinde
    "whale_w_obi": 0.30, "whale_w_cvd": 0.30, "whale_w_wall": 0.25, "whale_w_oi": 0.15,
}


def _cfg(cfg, key):
    return (cfg or {}).get(key, WHALE_DEFAULTS[key])


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


@dataclass
class WhaleSignal:
    direction: int = 0              # -1 satış baskısı, 0 yok, +1 alış baskısı
    strength: float = 0.0           # [0,1]
    quality_ok: bool = False        # veri tam mı (False → fail-closed)
    components: dict = field(default_factory=dict)
    spoof_suspect: bool = False


@dataclass
class EdgeResult:
    p_win: float
    gain: float
    loss: float
    e_profit: float
    cost: float
    risk_premium: float
    e_net: float
    detail: dict = field(default_factory=dict)


# ----------------------------------------------------------------- saf bileşenler
def parse_levels(raw):
    return [(float(p), float(q)) for p, q in raw]


def order_book_imbalance(bids, asks, band_bps):
    """OBI = (V_bid − V_ask)/(V_bid + V_ask), mid ± band_bps içinde notional. [-1,1]; veri yok → None."""
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2.0
    band = mid * band_bps / 10000.0
    vb = sum(p * q for p, q in bids if p >= mid - band)
    va = sum(p * q for p, q in asks if p <= mid + band)
    tot = vb + va
    return None if tot <= 0 else (vb - va) / tot


def find_walls(levels, mult, min_notional):
    """Medyan kademe notional'ının ≥ mult katı VE ≥ min_notional olan kademeler: [(price, notional)]."""
    if not levels:
        return []
    notionals = [p * q for p, q in levels]
    med = statistics.median(notionals)
    thr = max(mult * med, min_notional)
    return [(p, p * q) for p, q in levels if p * q >= thr]


def find_gaps(levels, mid, z_thr):
    """Kademeler arası aralık z-skoru ≥ z_thr olan boşluklar: [(price_from, price_to, gap_bps)].
    levels mid'den uzağa doğru sıralı olmalı."""
    if len(levels) < 4:
        return []
    gaps = [abs(levels[i + 1][0] - levels[i][0]) for i in range(len(levels) - 1)]
    med = statistics.median(gaps)
    mad = statistics.median([abs(g - med) for g in gaps]) or (med * 0.5) or 1e-12
    out = []
    for i, g in enumerate(gaps):
        z = 0.6745 * (g - med) / mad
        if z >= z_thr:
            out.append((levels[i][0], levels[i + 1][0], g / mid * 10000.0))
    return out


def taker_flow(trades, big_notional):
    """aggTrades → (cvd_ratio [-1,1], big_buy_n, big_sell_n). m=True → satıcı agresör.
    Veri yok → (None,0,0)."""
    buy = sell = 0.0
    bb = bs = 0
    for t in trades or []:
        try:
            notional = float(t["p"]) * float(t["q"])
            is_sell = bool(t["m"])
        except (KeyError, ValueError, TypeError):
            continue
        if is_sell:
            sell += notional
            bs += notional >= big_notional
        else:
            buy += notional
            bb += notional >= big_notional
    tot = buy + sell
    return (None if tot <= 0 else (buy - sell) / tot), bb, bs


def vwap_slippage(levels, notional):
    """Notional'ı kademelerde yürüyerek |VWAP−en iyi fiyat|/en iyi fiyat. Derinlik yetmezse None."""
    if not levels or notional <= 0:
        return None
    best = levels[0][0]
    need, cost, qty = notional, 0.0, 0.0
    for p, q in levels:
        take = min(need, p * q)
        cost += take
        qty += take / p
        need -= take
        if need <= 1e-9:
            break
    if need > 1e-9 or qty <= 0:
        return None
    return abs(cost / qty - best) / best


# ----------------------------------------------------------------- kalıcılık izleyici
class WhaleTracker:
    """Duvar kalıcılığı ve OI deltası için sembol-bazlı hafıza (çağrılar arası yaşar)."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._walls = {}   # (sym, side, price) -> first_seen
        self._seen_persistent = {}  # (sym, side, price) -> True (kalıcı görülmüş)
        self._oi = {}      # sym -> (oi, ts)

    def update_walls(self, symbol, side, walls, persist_s):
        """Kalıcı (≥persist_s) duvarları ve kaybolan kalıcı duvar (spoof) bayrağını döndürür."""
        now = self._clock()
        cur = {(symbol, side, round(p, 8)) for p, _ in walls}
        persistent, spoof = [], False
        for k in cur:
            first = self._walls.setdefault(k, now)
            if now - first >= persist_s:
                persistent.append(k[2])
                self._seen_persistent[k] = True
        for k in [k for k in self._walls if k[0] == symbol and k[1] == side and k not in cur]:
            if self._seen_persistent.pop(k, False) or (now - self._walls[k]) >= persist_s:
                spoof = True
            del self._walls[k]
        return persistent, spoof

    def oi_delta(self, symbol, oi_now):
        """Önceki örneğe göre (oi_now−prev)/prev; ilk örnek → None."""
        prev = self._oi.get(symbol)
        self._oi[symbol] = (oi_now, self._clock())
        if not prev or prev[0] <= 0 or oi_now is None:
            return None
        return (oi_now - prev[0]) / prev[0]


# ----------------------------------------------------------------- sinyal
def compute_signal(inputs, tracker, cfg=None) -> WhaleSignal:
    """inputs: {symbol, bids, asks, trades, oi (float|None)}. Veri eksik → quality_ok=False, direction=0."""
    try:
        sym = inputs["symbol"]
        bids, asks = inputs["bids"], inputs["asks"]
        if not bids or not asks or inputs.get("trades") is None:
            return WhaleSignal()
        mid = (bids[0][0] + asks[0][0]) / 2.0
        obi = order_book_imbalance(bids, asks, _cfg(cfg, "whale_obi_band_bps"))
        cvd, big_b, big_s = taker_flow(inputs["trades"], _cfg(cfg, "whale_big_trade_notional"))
        if obi is None or cvd is None:
            return WhaleSignal()

        persist = _cfg(cfg, "whale_wall_persist_s")
        bw = find_walls(bids, _cfg(cfg, "whale_wall_mult"), _cfg(cfg, "whale_wall_min_notional"))
        aw = find_walls(asks, _cfg(cfg, "whale_wall_mult"), _cfg(cfg, "whale_wall_min_notional"))
        bw_p, bw_spoof = tracker.update_walls(sym, "bid", bw, persist)
        aw_p, aw_spoof = tracker.update_walls(sym, "ask", aw, persist)
        spoof = bw_spoof or aw_spoof
        wall_score = (1.0 if bw_p else 0.0) - (1.0 if aw_p else 0.0)

        oi_d = tracker.oi_delta(sym, inputs.get("oi"))
        oi_score = 0.0
        if oi_d is not None and cvd != 0:
            oi_score = _clip(oi_d / _cfg(cfg, "whale_oi_norm"), -1.0, 1.0) * (1 if cvd > 0 else -1)

        score = (_cfg(cfg, "whale_w_obi") * obi + _cfg(cfg, "whale_w_cvd") * cvd
                 + _cfg(cfg, "whale_w_wall") * wall_score + _cfg(cfg, "whale_w_oi") * oi_score)
        wsum = (_cfg(cfg, "whale_w_obi") + _cfg(cfg, "whale_w_cvd")
                + _cfg(cfg, "whale_w_wall") + _cfg(cfg, "whale_w_oi"))
        score = _clip(score / wsum, -1.0, 1.0)
        strength = abs(score) * (0.5 if spoof else 1.0)
        direction = 0 if abs(score) < _cfg(cfg, "whale_dir_min_score") else (1 if score > 0 else -1)
        gaps_up = find_gaps(asks, mid, _cfg(cfg, "whale_gap_z"))
        gaps_dn = find_gaps(bids, mid, _cfg(cfg, "whale_gap_z"))
        comps = {"obi": obi, "cvd": cvd, "wall": wall_score, "oi_delta": oi_d, "oi_score": oi_score,
                 "big_buy_n": big_b, "big_sell_n": big_s,
                 "ask_wall_px": (min(aw_p) if aw_p else None), "bid_wall_px": (max(bw_p) if bw_p else None),
                 "gap_up_bps": (gaps_up[0][2] if gaps_up else None),
                 "gap_dn_bps": (gaps_dn[0][2] if gaps_dn else None),
                 "mid": mid}
        return WhaleSignal(direction, strength if direction else 0.0, True, comps, spoof)
    except Exception:
        return WhaleSignal()


# ----------------------------------------------------------------- E[kâr] > maliyet + risk primi
def expected_edge(side_dir, signal, *, sl_pct, tp_pct, atr_pct, drawdown, notional,
                  bids, asks, fee_rt, funding_rate=None, calibrated=False,
                  p_win_calibrated=None, cfg=None) -> EdgeResult:
    """side_dir: +1 LONG, −1 SHORT. Tüm çıktılar notional kesri."""
    mid = signal.components.get("mid") or ((bids[0][0] + asks[0][0]) / 2.0)

    # G: TP, karşı yöndeki ilk duvara mesafenin bir kesrinde sınırlanır (duvara sürmeyiz).
    gain = tp_pct
    wall_px = signal.components.get("ask_wall_px" if side_dir > 0 else "bid_wall_px")
    if wall_px:
        dist = abs(wall_px - mid) / mid
        gain = min(gain, dist * _cfg(cfg, "whale_tp_wall_frac"))
    loss = sl_pct

    # p_win: kalibre değilken sinyal gücü büyük kenar vaat edemez (cap).
    align = signal.direction * side_dir * signal.strength if signal.quality_ok else 0.0
    if p_win_calibrated is not None:
        p_win = _clip(p_win_calibrated, 0.0, 1.0)
    else:
        p_win = 0.5 + _cfg(cfg, "whale_edge_cap") * align
    e_profit = p_win * gain - (1.0 - p_win) * loss

    # Maliyet: komisyon + gerçek (VWAP) slipaj giriş/çıkış + funding.
    entry_book = asks if side_dir > 0 else bids
    exit_book = bids if side_dir > 0 else asks
    s_in = vwap_slippage(entry_book, notional)
    s_out = vwap_slippage(exit_book, notional)
    s_in = 0.001 if s_in is None else s_in      # derinlik yetmezse muhafazakâr
    s_out = 0.001 if s_out is None else s_out
    f = funding_rate or 0.0
    funding_cost = f * side_dir * (_cfg(cfg, "whale_hold_hours") / 8.0)   # long öder (f>0)
    cost = fee_rt + s_in + 1.5 * s_out + funding_cost

    # Risk primi: volatilite·(1+d) + belirsizlik (kalibre değil / aşırı funding / spoof).
    unc = 0.0 if calibrated else _cfg(cfg, "whale_uncertainty_pct")
    if abs(f) >= _cfg(cfg, "whale_funding_extreme"):
        unc += _cfg(cfg, "whale_uncertainty_pct")
    if signal.spoof_suspect:
        unc += _cfg(cfg, "whale_uncertainty_pct")
    rp = _cfg(cfg, "whale_lambda_sigma") * atr_pct * (1.0 + max(drawdown or 0.0, 0.0)) + unc

    e_net = e_profit - (cost + rp)
    return EdgeResult(p_win, gain, loss, e_profit, cost, rp, e_net,
                      {"s_in": s_in, "s_out": s_out, "funding_cost": funding_cost,
                       "align": align, "wall_px": wall_px})


# ----------------------------------------------------------------- FİLTRE kararı
def evaluate_filter(side_dir, signal, edge, cfg=None):
    """(allow, reason). YALNIZ reddedebilir. Veri yok → RED (fail-closed)."""
    if not signal.quality_ok or edge is None:
        return False, "balina verisi eksik (fail-closed)"
    if (signal.direction == -side_dir
            and signal.strength >= _cfg(cfg, "whale_block_strength")):
        return False, (f"ters balina baskısı (dir={signal.direction:+d} "
                       f"güç={signal.strength:.2f}) — fakeout riski")
    if edge.e_net < _cfg(cfg, "whale_min_net_edge_pct"):
        return False, (f"E_net={edge.e_net*100:.3f}% < eşik "
                       f"{_cfg(cfg, 'whale_min_net_edge_pct')*100:.3f}%")
    return True, f"E_net={edge.e_net*100:.3f}%"


# ----------------------------------------------------------------- veri toplama (public GET)
def fetch_whale_inputs(binance, symbol):
    """Public REST (rate-limit dostu: depth(100)=5 + aggTrades=20 + OI=1 + premiumIndex=1).
    Herhangi bir hata → None (fail-closed). Yalnız Faz 4 'Hot' adayları için çağrılmalı."""
    try:
        ob = binance.order_book(symbol, limit=100)
        if not isinstance(ob, dict) or "bids" not in ob:
            return None
        trades = binance._get("/fapi/v1/aggTrades", {"symbol": symbol, "limit": 500})
        if not isinstance(trades, list):
            return None
        oi = binance._get("/fapi/v1/openInterest", {"symbol": symbol})
        oi_val = float(oi["openInterest"]) if isinstance(oi, dict) and "openInterest" in oi else None
        pi = binance._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        funding = float(pi["lastFundingRate"]) if isinstance(pi, dict) and "lastFundingRate" in pi else None
        return {"symbol": symbol, "bids": parse_levels(ob["bids"]), "asks": parse_levels(ob["asks"]),
                "trades": trades, "oi": oi_val, "funding": funding}
    except Exception:
        return None


# ----------------------------------------------------------------- whale_metrics (AYRI sqlite)
WHALE_DB = str(Path.home() / "whale_metrics.db")

_SCHEMA = """CREATE TABLE IF NOT EXISTS whale_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at REAL NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    side INTEGER,
    direction INTEGER, strength REAL, quality_ok INTEGER, spoof INTEGER,
    p_win REAL, gain REAL, loss REAL, e_profit REAL, cost REAL, risk_premium REAL, e_net REAL,
    allow INTEGER, reason TEXT,
    components_json TEXT
)"""


def record_metrics(db_file, symbol, mode, side_dir, signal, edge, allow, reason):
    """Kalibrasyon verisi. risk_ledger.db'ye DOKUNMAZ; ayrı dosya. Hata sessizce yutulur."""
    import json
    try:
        conn = sqlite3.connect(str(db_file), timeout=5)
        try:
            conn.execute(_SCHEMA)
            e = edge
            conn.execute(
                "INSERT INTO whale_metrics (recorded_at,symbol,mode,side,direction,strength,"
                "quality_ok,spoof,p_win,gain,loss,e_profit,cost,risk_premium,e_net,allow,reason,"
                "components_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), symbol, mode, side_dir, signal.direction, signal.strength,
                 int(signal.quality_ok), int(signal.spoof_suspect),
                 e.p_win if e else None, e.gain if e else None, e.loss if e else None,
                 e.e_profit if e else None, e.cost if e else None,
                 e.risk_premium if e else None, e.e_net if e else None,
                 int(allow), reason, json.dumps(signal.components, default=str)))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False
