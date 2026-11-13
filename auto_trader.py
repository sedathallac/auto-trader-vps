#!/usr/bin/env python3
"""auto_trader.py v4 — otonom kalite tarayıcı + native SL koruma"""
import os, sys, time, hmac, hashlib, logging, argparse, json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = requests.Session()
# F3/F4: only idempotent reads are retried automatically. A POST/DELETE that the
# server processed but answered with 5xx/timeout must NOT be re-sent blindly
# (duplicate executor = duplicate position). Write outcomes are resolved by
# querying state instead (see HBAPI.create_executor).
_retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500,502,503,504], allowed_methods=["GET"])
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))
from risk_engine import RiskEngine
from position_reservation import PositionReserver
import requests

BINANCE_FAPI = "https://fapi.binance.com"
HB_API = "http://localhost:8000"
HB_ACCOUNT = "master_account"

MIN_VOLUME_USDT = 200_000_000
MIN_PCT_CHANGE = 3.0
MAX_PCT_CHANGE = 15.0
MAX_SPREAD_PCT = 0.5
MIN_DEPTH_USDT = 50_000
TARGET_NOTIONAL = 10.0
LEVERAGE = 3
# Faz 3: bilimsel kaldıraç/boyutlandırma bayrağı. off=canlı davranış değişmez,
# compare=eski↔yeni yan yana loglanır (eski kullanılır), on=yeni formül kullanılır
# (önce kalibrasyon + dry-run şart). Varsayılan 'off'.
SCIENTIFIC_SIZING = os.getenv("SCIENTIFIC_SIZING", "off").strip().lower()
# HB_OFFLINE: Hummingbot API (port 8000) olmadan çalıştırma — test/kalibrasyon bypass'ı.
# Aktifken reconcile_positions ve sync_risk_engine HB'ye gitmez; open_coin dry-run'a
# zorlanır (GERÇEK EMİR YOK), böylece Faz3[compare] kalibrasyon logları HB olmadan
# toplanabilir. Varsayılan KAPALI → production davranışı DEĞİŞMEZ.
HB_OFFLINE = os.getenv("HB_OFFLINE", "0").strip().lower() in ("1", "true", "yes", "on")
# Faz 4: hiyerarşik tarama bayrağı. off=mevcut scan_top (production değişmez),
# compare=yeni scan_tiered Q-sıralaması loglanır ama eski scan_top kullanılır,
# on=tiered tarama kullanılır (kalibrasyon sonrası). Varsayılan 'off'.
HIERARCHICAL_SCAN = os.getenv("HIERARCHICAL_SCAN", "off").strip().lower()
# Faz 5: balina avcılığı bayrağı. off=canlı davranış değişmez (ek API çağrısı YOK),
# compare=sinyal+E[kâr] loglanır/whale_metrics'e yazılır ama karar ESKİ mantıkla verilir,
# on=yalnız FİLTRE (sahte/kalitesiz girişi REDDEDER; asla giriş tetiklemez). Varsayılan 'off'.
WHALE_HUNTING = os.getenv("WHALE_HUNTING", "off").strip().lower()
_WHALE_TRACKER = None       # whale_engine.WhaleTracker (tembel init; duvar kalıcılığı/OI Δ)
# Faz 4: --loop sürecinde run_once çağrıları arasında yaşayan cache + tier zamanlama.
_SCAN_CACHE = None          # scan_engine.MetricCache (tembel init)
_SCAN_LAST_RUN = {}         # tier.name -> son çalışma epoch'u
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.05
TIME_LIMIT_SEC = 14400  # 4 saat
TOP_N = 2
MAX_OPEN_POSITIONS = max(0, int(os.getenv("MAX_OPEN_POSITIONS", "0")))  # 0 = EMERGENCY (yeni giris durdu)

RISK_CONFIG = {
    "max_drawdown_pct": 0.20,
    "max_total_margin_pct": 0.40,
    "max_consecutive_losses": 3,
    "max_trades_per_day": 20,
    "risk_per_trade_pct": 0.015,
    "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008,
    "risk_guards_enabled": True,
    "target_risk_units": 3.0,
    "min_leverage": 1,
    "max_leverage": 5,
    "min_notional": 5.5,
    "max_notional": 25.0,   # C1-FIX: Hard cap 25 USDT (önceden 10000 idi ve bypass ediliyordu)
    # ATR-bazlı dinamik SL/TP
    "atr_sl_multiplier": 1.5,
    "atr_tp_multiplier": 2.5,
    "sl_min_pct": 0.010,
    "sl_max_pct": 0.10,
    "tp_min_pct": 0.015,
    "tp_max_pct": 0.15,
}

# Faz 5: balina eşikleri RISK_CONFIG içinde (muhafazakâr başlangıç, ⚠️ CALIBRATION-REQUIRED).
from whale_engine import WHALE_DEFAULTS as _WHALE_DEFAULTS
RISK_CONFIG.update(_WHALE_DEFAULTS)

# --- Korelasyon Limiti ---
MAX_PER_SECTOR = 2
SECTOR_MAP = {
    # L1
    "ETHUSDT": "L1", "SOLUSDT": "L1", "BNBUSDT": "L1", "AVAXUSDT": "L1",
    "ADAUSDT": "L1", "DOTUSDT": "L1", "NEARUSDT": "L1", "APTUSDT": "L1",
    "SUIUSDT": "L1", "TONUSDT": "L1", "ATOMUSDT": "L1", "TRXUSDT": "L1",
    # MEME
    "DOGEUSDT": "MEME", "SHIBUSDT": "MEME", "PEPEUSDT": "MEME",
    "WIFUSDT": "MEME", "BONKUSDT": "MEME", "FLOKIUSDT": "MEME",
    # AI
    "FETUSDT": "AI", "AGIXUSDT": "AI", "RNDRUSDT": "AI", "TAOUSDT": "AI",
    "WLDUSDT": "AI", "OCEANUSDT": "AI",
    # DeFi
    "UNIUSDT": "DEFI", "AAVEUSDT": "DEFI", "MKRUSDT": "DEFI",
    "CRVUSDT": "DEFI", "COMPUSDT": "DEFI", "LDOUSDT": "DEFI",
}

# --- Pozisyon Boyutlandırma Beyni ---
TARGET_MARGIN_USAGE_PCT = 0.30   # Bakiyenin %30'u toplam marjin
MIN_NOTIONAL_USDT = 5.5           # Binance min
MAX_NOTIONAL_USDT = 25.0          # İşlem başına üst sınır
USE_SHORTS = True          # short işlemleri aktif
LONG_ON_DIP_PCT = -3.0     # %3+ düşmüşse LONG (mean reversion)
SHORT_ON_PUMP_PCT = 3.0    # %3+ yükselmişse SHORT (mean reversion)

HEARTBEAT_FILE = Path.home() / ".auto_trader.heartbeat"
BLACKLIST_FILE = Path.home() / ".auto_trader_blacklist.json"
BLACKLIST_TTL_SEC = 86400
PRESEED_BLACKLIST = [
    # Kripto dışı tokenize hisse/ETF türevleri
    "NVDAUSDT", "SKHYNIXUSDT", "CLUSDT", "SOXLUSDT", "SOXSUSDT",
    "MSTRUSDT", "COINUSDT", "HOODUSDT", "MARAUSDT", "RIOTUSDT",
    "AAPLUSDT", "TSLAUSDT", "MSFTUSDT", "GOOGLUSDT", "AMZNUSDT",
    "METAUSDT", "NFLXUSDT", "AMDUSDT", "INTCUSDT", "QCOMUSDT",
    "TQQQUSDT", "SQQQUSDT", "SPYUSDT", "QQQUSDT",
    "IBITUSDT", "FBTCUSDT", "GBTCUSDT", "BITOUSDT",
    # Düşük likidite / sürekli sorun çıkaran
    "KORUUSDT", "JCTUSDT", "HYPEUSDT", "ZECUSDT",
    # FAZ 2.1: Tokenize hisse/emtia (executor acilmaz)
    "BZUSDT", "SKHYUSDT", "SNDKUSDT", "SPCXUSDT", "XAGUSDT", "MUUSDT",
    "SNXXUSDT",
]

log = logging.getLogger(__name__)


def load_blacklist():
    bl = {}
    if BLACKLIST_FILE.exists():
        try:
            bl = json.loads(BLACKLIST_FILE.read_text())
        except Exception as e:
            log.warning(f"blacklist yüklenemedi, boş kabul: {e}")
            bl = {}
    now = time.time()
    # FAZ 2.2: Preseed her zaman taze (TTL'den muaf)
    for s in PRESEED_BLACKLIST:
        bl[s] = now
    return {k: v for k, v in bl.items()
            if k in PRESEED_BLACKLIST or (now - v) < BLACKLIST_TTL_SEC}


def save_blacklist(bl):
    BLACKLIST_FILE.write_text(json.dumps(bl, indent=2))


def add_blacklist(symbol, reason):
    bl = load_blacklist()
    bl[symbol] = time.time()
    save_blacklist(bl)
    log.info(f"  [BLACKLIST] {symbol} ({reason})")


class BinanceAPI:
    def __init__(self, key, secret):
        self.key = key
        self.secret = secret
        self._info = None
        self._info_ts = 0

    def _sign(self, params):
        qs = urlencode(params)
        return qs + "&signature=" + hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def _hdr(self):
        return {"X-MBX-APIKEY": self.key}

    def _get(self, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 60000
            return _session.get(f"{BINANCE_FAPI}{path}?{self._sign(params)}",
                                headers=self._hdr(), timeout=15).json()
        return _session.get(f"{BINANCE_FAPI}{path}", params=params, timeout=15).json()

    def _post(self, path, params, method="POST"):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 60000
        url = f"{BINANCE_FAPI}{path}?{self._sign(params)}"
        if method == "DELETE":
            return _session.delete(url, headers=self._hdr(), timeout=15).json()
        return _session.post(url, headers=self._hdr(), timeout=15).json()

    def tickers(self):
        return self._get("/fapi/v1/ticker/24hr")

    def order_book(self, symbol, limit=20):
        return self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def position_risk(self):
        return self._get("/fapi/v2/positionRisk", signed=True)

    def position_mode(self):
        """GET /fapi/v1/positionSide/dual -> {"dualSidePosition": bool}.
        true = HEDGE (long+short per symbol), false = ONEWAY (single netted)."""
        return self._get("/fapi/v1/positionSide/dual", signed=True)

    def open_algo_orders_for(self, symbol):
        r = self._get("/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        if isinstance(r, list):
            return r
        if isinstance(r, dict) and "orders" in r:
            return r["orders"]
        return []

    def open_algo_orders_raw(self, symbol):
        """GET /fapi/v1/openAlgoOrders returning the RAW response, never swallowing
        an error into []. A timeout/exception propagates; an API error dict
        ({"code","msg"}) is returned as-is; success is a list (or {"orders":[...]}).
        sl_status uses this to tell a real 'no SL' (ABSENT) apart from an outage
        (UNKNOWN) — open_algo_orders_for() cannot, as it maps both to []."""
        return self._get("/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    def open_orders(self, symbol):
        """GET /fapi/v1/openOrders — REGULAR (non-algo) open orders for a symbol
        (e.g. a resting take_profit LIMIT, or an in-flight / partially filled
        close). Returns the raw response (list on success, error dict otherwise)
        so the caller can fail closed on an error. Used to detect a pre-sent
        _close_order / _take_profit_limit_order before the close handoff."""
        return self._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def cancel_order(self, symbol, order_id=None, orig_client_order_id=None):
        """DELETE /fapi/v1/order — cancel a REGULAR order by exchange orderId or by
        origClientOrderId (the HB client order id)."""
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self._post("/fapi/v1/order", params, method="DELETE")

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        """GET /fapi/v1/order — current status of a single REGULAR order, by
        exchange orderId or by origClientOrderId (the HB client order id). Used to
        confirm a pre-sent close/TP order's real terminal state even when it is no
        longer in openOrders — never inferred from a cancel response or error code."""
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self._get("/fapi/v1/order", params, signed=True)

    def exchange_info(self):
        if not self._info or time.time() - self._info_ts > 3600:
            self._info = self._get("/fapi/v1/exchangeInfo")
            self._info_ts = time.time()
        return self._info

    def filters(self, symbol):
        for s in self.exchange_info()["symbols"]:
            if s["symbol"] == symbol:
                return {f["filterType"]: f for f in s["filters"]}
        return None

    def get_balance(self, asset="USDT"):
        r = self._get("/fapi/v2/balance", signed=True)
        if isinstance(r, list):
            for a in r:
                if a["asset"] == asset:
                    return float(a["balance"])
        return 0.0

    def get_available_balance(self, asset="USDT"):
        r = self._get("/fapi/v2/balance", signed=True)
        if isinstance(r, list):
            for a in r:
                if a["asset"] == asset:
                    return float(a["availableBalance"])
        return 0.0

    def set_leverage(self, symbol, lev):
        return self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})

    def cancel_algo_order(self, algo_id):
        return self._post("/fapi/v1/algoOrder", {"algoId": algo_id}, method="DELETE")

    def place_algo_sl(self, symbol, trigger_price, quantity, side):
        sl_side = "SELL" if side == 1 else "BUY"
        return self._post("/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": sl_side,
            "type": "STOP_MARKET", "triggerPrice": f"{trigger_price:.8f}",
            "quantity": f"{quantity:.8f}", "reduceOnly": "true",
        })


def build_executor_config(symbol, amount, lev, side):
    """K1=B: a position_executor with NO autonomous close barriers.

    stop_loss / take_profit / time_limit / trailing_stop are all disabled so
    Hummingbot never places its own close. In dev-2.18.0 every control_* barrier
    is skipped when its configured value is falsy (control_stop_loss /
    control_take_profit / control_trailing_stop guard on the value; control_time_
    limit depends on time_limit), and the SHUTTING_DOWN retry close path only runs
    on a NON keep_position stop (POSITION_HOLD stops without placing a close). So
    with all four None, no SL / market-TP / limit-TP / trailing / time-limit close
    can be produced. The exchange reduceOnly STOP_MARKET algo order is the sole SL
    authority.

    E-1 (live exit policy — documented, not a strategy change): the ONLY live exits
    are (1) the exchange algo SL filling autonomously, and (2) the coordinator-routed
    emergency close (SL unverified / liquidation-adjacent). There is deliberately NO
    TP or time-limit exit in the live loop: HB's autonomous TP/time-limit stay
    disabled here, and NO production code calls CloseCoordinator.request_take_profit /
    request_time_limit. Those remain available interfaces for a FUTURE increment;
    adding a trigger is a strategy change and is OUT OF SCOPE. The tp / time-limit
    values passed to create_executor are accepted for signature compatibility and
    ignored (never handed to HB as barriers).
    """
    return {
        "type": "position_executor",
        "connector_name": "binance_perpetual",
        "trading_pair": symbol.replace("USDT", "-USDT"),
        "side": side,
        "amount": str(amount),
        "leverage": lev,
        "triple_barrier_config": {
            "stop_loss": None,
            "take_profit": None,
            "time_limit": None,
            "trailing_stop": None,
            "open_order_type": 1,
        },
    }


class HBAPI:
    def __init__(self, base, user, pw):
        self.base = base
        self.auth = (user, pw)

    def _active_ids_for(self, pair):
        return {e.get("executor_id") for e in self.active_executors()
                if e.get("trading_pair") == pair}

    def create_executor(self, symbol, amount, lev, sl, tp, tl, side=1):
        """Single-shot create. On 5xx/timeout the outcome is UNKNOWN: never
        resend; resolve by diffing active executors for the pair.

        K1=B: the executor is created with NO autonomous close barriers (see
        build_executor_config). sl/tp/tl are accepted for signature compatibility
        but are NOT handed to Hummingbot as barriers — the exchange algo SL is the
        sole SL authority. E-1: there is no live TP/time-limit trigger (SL-only exit
        policy); request_take_profit/request_time_limit exist for a future increment
        but are not called in the live loop."""
        pair = symbol.replace("USDT", "-USDT")
        try:
            before = self._active_ids_for(pair)
        except Exception as e:
            return {"error": f"pre-create executor snapshot failed, refusing: {e}"}
        payload = {"account_name": HB_ACCOUNT,
                   "executor_config": build_executor_config(symbol, amount, lev, side)}
        try:
            r = _session.post(f"{self.base}/executors/", json=payload,
                              auth=self.auth, timeout=30)
            if r.status_code < 500:
                return r.json()
            cause = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            cause = f"{type(e).__name__}: {e}"
        # UNKNOWN outcome -> resolve from state, do not resend
        try:
            new = self._active_ids_for(pair) - before
        except Exception as e:
            return {"error": f"UNKNOWN create outcome ({cause}); resolve failed: {e}",
                    "outcome": "UNKNOWN"}
        if len(new) == 1:
            return {"executor_id": next(iter(new)), "resolved_after": cause}
        return {"error": f"UNKNOWN create outcome ({cause}); new executors={sorted(new)}",
                "outcome": "UNKNOWN" if new else "NOT_CREATED"}

    def active_executors(self):
        r = _session.post(f"{self.base}/executors/search",
                          json={"is_active": True}, auth=self.auth, timeout=15)
        return r.json().get("data", [])

    def get_executor(self, eid):
        r = requests.post(f"{self.base}/executors/search",
                          json={}, auth=self.auth, timeout=15)
        for e in r.json().get("data", []):
            if e.get("executor_id") == eid:
                return e
        return None

    def stop_executor(self, eid, keep_position=True):
        """Stop an executor WITHOUT letting it place its own close
        (keep_position=True == POSITION_HOLD, so HB's autonomous close/TP paths
        do not run). Used by the CloseCoordinator handoff. Returns HB response."""
        return _session.post(
            f"{self.base}/executors/{eid}/stop",
            json={"close_type": "EARLY_STOP", "keep_position": bool(keep_position)},
            auth=self.auth, timeout=15).json()


def heartbeat_tick():
    """Her döngüde çağrılır, botun yaşadığını gösteren zaman damgası"""
    try:
        HEARTBEAT_FILE.write_text(datetime.now().isoformat())
    except Exception as e:
        log.error(f"Heartbeat yazılamadı: {e}")


def is_flat_confirmed(positions, symbol):
    """K2: a position is 'flat' ONLY when `positions` is a valid list that
    CONTAINS `symbol` with positionAmt == 0. A non-list (API error), a missing /
    mismatched symbol, a non-zero amount, or an unparseable amount all return
    False — so an outage, a stale/partial snapshot or a partial close is NEVER
    mistaken for a closed position (fail-closed)."""
    if not isinstance(positions, list):
        return False
    for p in positions:
        if p.get("symbol") == symbol:
            try:
                return float(p.get("positionAmt", "nan")) == 0.0
            except (TypeError, ValueError):
                return False
    return False  # symbol absent -> not confirmed flat


def sync_risk_engine(risk, hb, binance=None):
    """C3: Sync closed executor PnLs to SQLite ledger (idempotent via PRIMARY KEY).

    K2: a TERMINATED executor's PnL is recorded ONLY after a current, successful
    Binance positionRisk query confirms that symbol is flat (positionAmt == 0).
    If positionRisk errors/timeouts, is stale/missing, or the position is not
    zero (partial close), the close is NOT recorded — it stays UNKNOWN/open and a
    CRITICAL alarm is raised (fail-closed)."""
    if HB_OFFLINE:
        log.info("🔌 HB_OFFLINE: sync_risk_engine atlandı (HB API'ye gidilmedi)")
        return
    import sqlite3 as _sq
    from datetime import timezone as _tz

    db_path = Path.home() / "risk_ledger.db"

    try:
        with _sq.connect(str(db_path), timeout=10) as conn:
            meta = conn.execute("SELECT last_close_ts FROM sync_meta WHERE id=1").fetchone()
            last_ts = float(meta[0]) if meta else 0.0
            reported_ids = {r[0] for r in conn.execute("SELECT executor_id FROM trade_ledger")}
    except Exception as e:
        log.critical(f"🚨 C3: sync_meta/ledger okunamadi, fail-closed: {e}")
        return

    reported = 0
    max_close_ts = last_ts
    try:
        r = requests.post(f"{HB_API}/executors/search", json={},
                          auth=hb.auth, timeout=15).json()
        all_execs = r.get("data", [])
        terminated = [e for e in all_execs if e.get("status") == "TERMINATED"]
        log.info(f"  RiskEngine sync: {len(all_execs)} exec, {len(terminated)} TERM, {len(reported_ids)} in ledger")
        # K2: single exchange snapshot used to confirm each close is really flat.
        try:
            _positions = binance.position_risk() if binance is not None else None
        except Exception as _pe:
            _positions = None
            log.warning(f"  K2: positionRisk alınamadı (flat teyidi yok): {_pe}")
        for e in terminated:
            eid = e.get("executor_id")
            if not eid:
                continue
            if eid in reported_ids:
                continue
            closed_at = float(e.get("close_timestamp") or 0)
            if closed_at <= last_ts:
                # Pre-watermark: skip (already handled via migration or previous sync)
                continue
            pnl = float(e.get("net_pnl_quote") or 0)
            pair = e.get("trading_pair")
            # K2 gate: do NOT record a close (final PnL) unless the exchange
            # confirms this symbol is flat. Fail-closed on error/stale/partial.
            _symbol = pair.replace("-", "") if pair else None
            if not is_flat_confirmed(_positions, _symbol):
                log.critical(f"🚨 K2: executor {str(eid)[:12]} TERMINATED ama borsa "
                             f"flat DEĞİL/doğrulanamadı ({_symbol}) — PnL YAZILMADI, "
                             f"kapanmış sayılmadı (fail-closed)")
                continue
            closed_at_iso = datetime.fromtimestamp(closed_at).isoformat() if closed_at else None
            inserted, msg = risk.record_trade(eid, pair, pnl, closed_at_iso)
            if inserted:
                reported += 1
                log.info(f"  RiskEngine PnL: {pair} -> {pnl:+.4f} ({msg})")
            else:
                log.debug(f"  skip {eid[:12]}: {msg}")
            if closed_at > max_close_ts:
                max_close_ts = closed_at
        if reported:
            log.info(f"  RiskEngine: {reported} yeni kapanis islendi")
    except Exception as e:
        log.warning(f"  RiskEngine sync HATA: {type(e).__name__}: {e}")
        return

    try:
        now = datetime.now(_tz.utc).isoformat()
        with _sq.connect(str(db_path), timeout=10) as conn:
            conn.execute(
                "UPDATE sync_meta SET last_close_ts=?, updated_at=? WHERE id=1",
                (max_close_ts, now)
            )
            conn.commit()
    except Exception as e:
        log.critical(f"🚨 C3: sync_meta güncellenemedi: {e}")


def reconcile_positions(binance, hb):
    """Borsa pozisyonları ile Hummingbot executor'larını karşılaştır"""
    log.info("🔍 State Reconciliation...")
    if HB_OFFLINE:
        log.info("🔌 HB_OFFLINE: reconciliation atlandı (HB'siz; drift yok sayıldı)")
        return True
    try:
        exchange_positions = binance.position_risk()
        if not isinstance(exchange_positions, list):
            log.error(f"positionRisk alınamadı: {exchange_positions}")
            return False
        open_exchange_pos = {
            p["symbol"]: float(p["positionAmt"])
            for p in exchange_positions
            if float(p["positionAmt"]) != 0
        }

        hb_executors = hb.active_executors()
        if not isinstance(hb_executors, list):
            log.error(f"hb executors alınamadı: {hb_executors}")
            return False
        open_hb_pairs = {
            e["trading_pair"]: e
            for e in hb_executors
            if e.get("status") == "RUNNING" and e.get("filled_amount_quote", 0) > 0
        }

        # HB pair -> symbol (BTC-USDT -> BTCUSDT)
        hb_symbols = {pair.replace("-", "") for pair in open_hb_pairs.keys()}
        exchange_symbols = set(open_exchange_pos.keys())

        orphaned_on_exchange = exchange_symbols - hb_symbols
        orphaned_on_hb = hb_symbols - exchange_symbols

        drift = False
        for sym in orphaned_on_exchange:
            log.critical(f"🚨 DRIFT: Borsada {sym} var ({open_exchange_pos[sym]}), HB'de executor yok")
            drift = True
        for sym in orphaned_on_hb:
            log.critical(f"🚨 DRIFT: HB'de {sym} executor var, borsada pozisyon yok")
            drift = True
            # AUTO-HEAL with per-symbol cooldown (avoid hammering HB)
            cooldown_sec = 300
            heal_state_file = Path.home() / ".auto_heal_cooldown.json"
            heal_state = {}
            if heal_state_file.exists():
                try:
                    heal_state = json.loads(heal_state_file.read_text())
                except Exception as e:
                    log.warning(f"auto-heal state yüklenemedi: {e}")
                    heal_state = {}
            last_heal = heal_state.get(sym, 0)
            now_ts = time.time()
            if now_ts - last_heal < cooldown_sec:
                log.info(f"  AUTO-HEAL {sym}: cooldown ({int(now_ts - last_heal)}s/{cooldown_sec}s)")
                continue
            for e in hb_executors:
                if e["trading_pair"].replace("-", "") == sym and e.get("status") == "RUNNING":
                    eid = e.get("executor_id")
                    try:
                        r = requests.post(f"{HB_API}/executors/{eid}/stop", json={"close_type": "EARLY_STOP", "keep_position": True}, auth=hb.auth, timeout=15).json()
                        log.warning("  AUTO-HEAL " + sym + " -> " + str(r.get("status")))
                        heal_state[sym] = now_ts
                        heal_state_file.write_text(json.dumps(heal_state, indent=2))
                    except Exception as ex:
                        log.error("  AUTO-HEAL HATA " + sym + ": " + str(ex))
                    break

        if not drift:
            log.info(f"✅ Reconciliation OK — {len(open_exchange_pos)} pozisyon senkron")
            return True

        log.critical("🚨 DRIFT! Bu döngüde yeni işlem açılmayacak")
        return False
    except Exception as e:
        log.exception(f"reconcile_positions hata: {e}")
        return False


def cleanup_orphan_sl(binance):
    try:
        positions = binance.position_risk()
        if not isinstance(positions, list):
            return
        open_syms = {p["symbol"] for p in positions if float(p["positionAmt"]) != 0}
        info = binance.exchange_info()
        orphans = []
        for s in info["symbols"]:
            sym = s["symbol"]
            if not sym.endswith("USDT") or sym in open_syms:
                continue
            for o in binance.open_algo_orders_for(sym):
                orphans.append((sym, o["algoId"]))
        if not orphans:
            log.info("🧹 Yetim SL: yok")
            return
        log.warning(f"🧹 {len(orphans)} yetim SL temizleniyor...")
        for sym, aid in orphans:
            try:
                r = binance.cancel_algo_order(aid)
                if r.get("code") == "200":
                    log.info(f"  ✅ {sym} iptal (algoId={aid})")
            except Exception as e:
                log.warning(f"  ❌ {sym}: {e}")
    except Exception as e:
        log.exception(f"cleanup_orphan_sl: {e}")


# OPEN_UNPROTECTED (increment 9): symbols whose exchange SL could not be placed
# (especially Binance -2021 ORDER_WOULD_IMMEDIATELY_TRIGGER). While this map is
# non-empty the bot is OPEN_UNPROTECTED: new entries are blocked and a CRITICAL
# alarm stays up until the real exchange state is resolved. We NEVER auto-change
# the stop price or close the position here — those are separate security
# procedures.
_OPEN_UNPROTECTED = {}  # symbol -> Binance error code (or None)


def sl_placement_outcome(result):
    """Classify a place_algo_sl result. ("PROTECTED", None) when accepted (has
    algoId); else ("UNPROTECTED", code) with the Binance error code when present
    (e.g. -2021)."""
    if isinstance(result, dict) and result.get("algoId"):
        return ("PROTECTED", None)
    code = result.get("code") if isinstance(result, dict) else None
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return ("UNPROTECTED", code)


def note_sl_outcome(unprotected, symbol, result):
    """Pure transition for the OPEN_UNPROTECTED map. Returns (new_map, action);
    action in {PROTECTED, UNPROTECTED_2021, UNPROTECTED_OTHER}."""
    state, code = sl_placement_outcome(result)
    m = dict(unprotected)
    if state == "PROTECTED":
        m.pop(symbol, None)
        return (m, "PROTECTED")
    m[symbol] = code
    return (m, "UNPROTECTED_2021" if code == -2021 else "UNPROTECTED_OTHER")


def should_block_new_entries(unprotected=None):
    """True while any position is OPEN_UNPROTECTED (SL not confirmed on exchange)."""
    u = _OPEN_UNPROTECTED if unprotected is None else unprotected
    return len(u) > 0


def new_entry_allowed(drift, stale, mode_ok, unprotected):
    """Pure new-entry gate (K6): allowed only when NONE of the blockers hold.
    Independent of the protection check, which runs regardless of drift/stale."""
    return (not drift) and (not stale) and bool(mode_ok) and (not unprotected)


def protect_then_gate(binance, reserver, drift, stale):
    """K6: run the protection check (SL coverage + CRITICAL alarms) and the
    reservation reconcile REGARDLESS of drift/stale, THEN decide whether new
    entries are allowed. A reconciliation drift blocks new entries but never
    disables protection for open positions. Returns (allow_new_entry, reasons)."""
    ensure_sl_coverage(binance)  # <- always runs; drift/stale never skip it
    try:
        _eos = _exchange_open_symbols(binance)
        if _eos is not None and reserver is not None:
            _rc = reserver.reconcile(_eos)
            if _rc.promoted or _rc.released:
                log.info(f"  🔁 rezervasyon reconcile: promoted={len(_rc.promoted)} "
                         f"released={len(_rc.released)} kept={len(_rc.kept)}")
    except Exception as e:
        log.warning(f"rezervasyon reconcile hatası: {e}")
    mode_ok, mode = _verify_oneway(binance)
    unprotected = should_block_new_entries()
    allow = new_entry_allowed(drift=drift, stale=stale, mode_ok=mode_ok,
                              unprotected=unprotected)
    return allow, {"drift": drift, "stale": stale, "mode": mode,
                   "mode_ok": mode_ok, "unprotected": unprotected}


# K-3: shared ledger path for the cross-process SL-placement intent (same db as the
# reservation / close-intent). Overridable in tests.
SL_LEDGER_DB = str(Path.home() / "risk_ledger.db")


def _dedupe_algo_sl(binance, symbol):
    """K-3 deterministic dedupe backstop: if `symbol` has MORE THAN ONE live STOP algo
    SL, KEEP the one with the MAX algoId and cancel the rest. The keep rule is
    deterministic, so two processes running this concurrently can never cancel BOTH
    (each cancels the same non-max set; the max always survives -> never unprotected).
    Returns the list of cancelled algoIds."""
    cancelled = []
    try:
        orders = binance.open_algo_orders_for(symbol)
        if not isinstance(orders, list):
            return cancelled
        stops = [o for o in orders
                 if o.get("algoId") is not None
                 and "STOP" in str(o.get("orderType") or o.get("type")
                                   or o.get("algoType") or "").upper()]
        if len(stops) <= 1:
            return cancelled
        keep = max(stops, key=lambda o: int(o["algoId"]))
        for o in stops:
            if o["algoId"] != keep["algoId"]:
                try:
                    binance.cancel_algo_order(o["algoId"])
                    cancelled.append(o["algoId"])
                    log.info(f"  🧹 K-3 dedupe {symbol}: fazla SL iptal "
                             f"(algoId={o['algoId']}, tutulan={keep['algoId']})")
                except Exception as e:
                    log.warning(f"  K-3 dedupe iptal hatası {symbol}/{o['algoId']}: {e}")
    except Exception as e:
        log.warning(f"  K-3 dedupe hatası {symbol}: {e}")
    return cancelled


def ensure_sl_coverage(binance):
    try:
        positions = binance.position_risk()
        if not isinstance(positions, list):
            log.warning(f"positionRisk hata: {positions}")
            return
        open_pos = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0}
        # K-3 deterministic dedupe backstop: clean any duplicate SL on every open
        # position (both processes run this; deterministic keep-max never cancels both).
        for _sym in open_pos:
            _dedupe_algo_sl(binance, _sym)
        # RC-B: a reliable positionRisk read (a list) is authoritative flat
        # confirmation. Clear any stale OPEN_UNPROTECTED alarm for a symbol that is
        # no longer open, so an old (e.g. -2021) entry can never block new entries
        # forever. Cleanup happens ONLY on this reliable read (an unreliable read
        # returned above without touching the map).
        for _sym in list(_OPEN_UNPROTECTED):
            if _sym not in open_pos:
                _OPEN_UNPROTECTED.pop(_sym, None)
        if not open_pos:
            log.info("SL kontrolü: açık pozisyon yok")
            return
        # RC-2 fix: validate REAL protective coverage mode-aware via the inc10.1
        # sl_status (NOT "any algo order exists"). For each open position the real
        # positionRisk amount, ONEWAY/HEDGE mode, side, symbol, order type, side,
        # positionSide, reduceOnly|closePosition, quantity (full coverage) and order
        # status are evaluated together -> PRESENT / ABSENT / UNKNOWN:
        #   PRESENT -> fully protected; do NOT send a second SL.
        #   ABSENT (ONEWAY) -> place via the EXISTING place_algo_sl mechanism below.
        #   UNKNOWN -> cannot verify; never place a blind SL; CRITICAL + block entries.
        #   HEDGE without a valid existing SL -> fail-closed (no unsupported combo).
        from close_adapter import (BinanceHBCloseAdapter,
                                    SL_PRESENT, SL_UNKNOWN)
        adapter = BinanceHBCloseAdapter(binance, None)  # sl_status uses only binance
        _ok, mode = _verify_oneway(binance)             # "ONEWAY" | "HEDGE" | "UNKNOWN"
        missing = []
        for sym, p in open_pos.items():
            amt = float(p["positionAmt"])
            side = "LONG" if amt > 0 else "SHORT"
            if mode == "UNKNOWN":
                status = SL_UNKNOWN                      # mode unverifiable -> fail-closed
            else:
                status = adapter.sl_status(sym, side, mode=mode, position_amt=abs(amt))
            if status == SL_PRESENT:
                _OPEN_UNPROTECTED.pop(sym, None)         # fully protected -> clear alarm
                continue
            if status == SL_UNKNOWN:
                _OPEN_UNPROTECTED[sym] = _OPEN_UNPROTECTED.get(sym)  # keep -2021 if any
                log.critical(f"🚨 OPEN_UNPROTECTED {sym}: SL durumu DOĞRULANAMADI "
                             f"(UNKNOWN) — korumasız sayıldı; yeni giriş durduruldu; "
                             f"kör SL GÖNDERİLMEDİ")
                continue
            # SL_ABSENT: no valid full-coverage SL exists for this position.
            if mode == "HEDGE":
                _OPEN_UNPROTECTED[sym] = _OPEN_UNPROTECTED.get(sym)
                log.critical(f"🚨 OPEN_UNPROTECTED {sym}: HEDGE modda geçerli SL yok; "
                             f"HEDGE SL gönderme yolu YOK — fail-closed; yeni giriş "
                             f"durduruldu; desteklenmeyen emir GÖNDERİLMEDİ")
                continue
            missing.append(sym)                         # ONEWAY + ABSENT -> place SL
        if not missing:
            if _OPEN_UNPROTECTED:
                log.warning(f"⚠️  SL doğrulanamayan/eksik (fail-closed): "
                            f"{sorted(_OPEN_UNPROTECTED)}")
            else:
                log.info(f"✅ SL koruması tam ({len(open_pos)} pozisyon)")
            return
        log.warning(f"⚠️  SL eksik (ONEWAY, yerleştirilecek): {missing}")
        # K-3 cross-process SL-placement intent (same shared ledger as reservation).
        try:
            from sl_placement import SlPlacementStore, ACQUIRED as _SL_ACQ
            _sl_store = SlPlacementStore(SL_LEDGER_DB)
            _sl_store.init_schema()
        except Exception as _e:
            log.warning(f"K-3 SlPlacementStore kurulamadı: {_e}")
            _sl_store = None
            _SL_ACQ = "ACQUIRED"
        for sym in missing:
            try:
                # Stop uncontrolled retry: a symbol already flagged -2021 is NOT
                # re-sent (resending the same trigger just fails again). Keep the
                # CRITICAL alarm up until the real exchange state is resolved.
                if _OPEN_UNPROTECTED.get(sym) == -2021:
                    log.critical(f"🚨 OPEN_UNPROTECTED {sym}: SL -2021 sürüyor — "
                                 f"korumasız; yeni giriş durduruldu; otomatik SL "
                                 f"yeniden gönderimi DURDURULDU (kontrolsüz retry yok)")
                    continue
                p = open_pos[sym]
                qty = abs(float(p["positionAmt"]))
                entry = float(p["entryPrice"])
                is_long = float(p["positionAmt"]) > 0
                side = 1 if is_long else 2
                filt = binance.filters(sym)
                tick = float(filt["PRICE_FILTER"]["tickSize"]) if filt else 0.01
                # FAZ 2.3: ATR bazli SL (open_coin ile tutarli)
                atr_pct = calculate_atr(binance, sym) or 0.02
                sl_pct = min(max(atr_pct * RISK_CONFIG["atr_sl_multiplier"],
                                 RISK_CONFIG["sl_min_pct"]),
                             RISK_CONFIG["sl_max_pct"])
                if is_long:
                    trig = round(round((entry * (1 - sl_pct)) / tick) * tick, 8)
                else:
                    trig = round(round((entry * (1 + sl_pct)) / tick) * tick, 8)
                # K-3: only ONE process places this (symbol, position-epoch) SL; a
                # concurrent ensure_sl_coverage (orphan_killer) sees BUSY and skips.
                _tok = ("LONG" if is_long else "SHORT") + ":" + str(p.get("entryPrice"))
                if _sl_store is not None and _sl_store.acquire(sym, _tok) != _SL_ACQ:
                    log.info(f"  ⏸️ {sym} SL yerleştirme başka process'te (BUSY) — atlandı")
                    continue
                r = binance.place_algo_sl(sym, trig, qty, side)
                if _sl_store is not None:
                    if isinstance(r, dict) and r.get("algoId"):
                        _sl_store.mark_placed(sym, _tok)
                    else:
                        _sl_store.release(sym, _tok)   # failure -> allow reality/retry
                _new, _action = note_sl_outcome(_OPEN_UNPROTECTED, sym, r)
                _OPEN_UNPROTECTED.clear()
                _OPEN_UNPROTECTED.update(_new)
                if _action == "PROTECTED":
                    log.info(f"  ✅ {sym} SL gönderildi: trigger={trig} qty={qty} "
                             f"algoId={r['algoId']} → OPEN_PROTECTED")
                elif _action == "UNPROTECTED_2021":
                    # -2021: OPEN_UNPROTECTED. Do NOT change the stop price and do
                    # NOT close the position (separate security procedures).
                    log.critical(f"🚨 OPEN_UNPROTECTED {sym}: SL -2021 (would "
                                 f"immediately trigger) — korumasız, yeni giriş "
                                 f"durduruldu; stop fiyatı DEĞİŞTİRİLMEDİ, pozisyon "
                                 f"KAPATILMADI; borsa durumu çözülene dek alarm açık")
                else:
                    log.critical(f"🚨 OPEN_UNPROTECTED {sym}: SL konulamadı "
                                 f"code={_OPEN_UNPROTECTED.get(sym)} ({r}) — yeni "
                                 f"giriş durduruldu")
            except Exception as e:
                log.exception(f"  {sym} SL istisna: {e}")
    except Exception as e:
        log.exception(f"ensure_sl_coverage: {e}")


def check_correlation_limit(symbol, binance):
    """Aynı sektörden MAX_PER_SECTOR'dan fazla pozisyon açılmasını engelle"""
    target_sector = SECTOR_MAP.get(symbol, symbol)  # bilinmeyen coin kendi sektöründe
    try:
        positions = binance.position_risk()
        if not isinstance(positions, list):
            log.critical("🚨 C6: positionRisk alınamadı, fail-closed")
            return False
        open_symbols = [p["symbol"] for p in positions if float(p["positionAmt"]) != 0]
        same_sector = [s for s in open_symbols if SECTOR_MAP.get(s, s) == target_sector]
        if len(same_sector) >= MAX_PER_SECTOR:
            log.warning(f"  🚫 Korelasyon: {target_sector} sektöründe zaten {len(same_sector)} pozisyon ({same_sector})")
            return False
        return True
    except Exception as e:
        log.critical(f"🚨 C6: korelasyon kontrol hatası, fail-closed: {e}")
        return False


def check_stale_data(binance, max_age_sec=30):
    """Binance sunucu saati ile yerel saat arasındaki farkı kontrol et"""
    try:
        resp = binance._get("/fapi/v1/time")
        server_ms = int(resp.get("serverTime", 0))
        local_ms = int(time.time() * 1000)
        diff_sec = abs(server_ms - local_ms) / 1000
        if diff_sec > max_age_sec:
            log.critical(f"🚨 STALE DATA: Sunucu-yerel fark {diff_sec:.1}sn (limit: {max_age_sec}sn)")
            return False
        log.debug(f"✅ Data taze (fark: {diff_sec:.2f}sn)")
        return True
    except Exception as e:
        log.error(f"Stale data check hatası: {e}")
        return False


def calculate_atr(binance, symbol, interval="15m", period=14):
    """ATR'yi yüzde olarak döndür (örn. 0.025 = %2.5)"""
    try:
        klines = binance._get("/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": period + 1
        })
        if not isinstance(klines, list) or len(klines) < period + 1:
            return None
        trs = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i-1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        atr_abs = sum(trs) / len(trs)
        current_price = float(klines[-1][4])
        return atr_abs / current_price if current_price > 0 else None
    except Exception as e:
        log.warning(f"ATR hesaplanamadı {symbol}: {e}")
        return None


def check_liquidity(binance, symbol):
    try:
        ob = binance.order_book(symbol, limit=20)
        bids = [(float(p), float(q)) for p, q in ob.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in ob.get("asks", [])]
        if not bids or not asks:
            return False, "order book boş"
        bid, ask = bids[0][0], asks[0][0]
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        depth = sum(p * q for p, q in bids[:10]) + sum(p * q for p, q in asks[:10])
        if spread_pct > MAX_SPREAD_PCT:
            return False, f"spread %{spread_pct:.2f}"
        if depth < MIN_DEPTH_USDT:
            return False, f"depth {depth:,.0f}"
        return True, f"spread={spread_pct:.2f}% depth={depth:,.0f}"
    except Exception as e:
        return False, f"orderbook: {e}"


def scan_top(binance, n, blacklist=None):
    blacklist = blacklist or set()
    cands = []
    for d in binance.tickers():
        try:
            if not d["symbol"].endswith("USDT"):
                continue
            if d["symbol"] in blacklist:
                continue
            vol = float(d["quoteVolume"])
            chg = abs(float(d["priceChangePercent"]))
            if vol >= MIN_VOLUME_USDT and MIN_PCT_CHANGE <= chg <= MAX_PCT_CHANGE:
                cands.append(d)
        except (KeyError, ValueError):
            continue
    if not cands:
        return []
    mv = max(float(d["quoteVolume"]) for d in cands)
    mc = max(abs(float(d["priceChangePercent"])) for d in cands)
    for d in cands:
        d["_score"] = 0.6 * (float(d["quoteVolume"]) / mv) + 0.4 * (abs(float(d["priceChangePercent"])) / mc)
    cands.sort(key=lambda x: x["_score"], reverse=True)
    return cands[:n]


def _depth_liquidity(binance, symbol, bps=10):
    """Order-book derinliği: mid ± bps bandındaki toplam notional (bid+ask). None=hata."""
    try:
        ob = binance.order_book(symbol, limit=20)
        bids = [(float(p), float(q)) for p, q in ob.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in ob.get("asks", [])]
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2.0
        band = mid * bps / 10000.0
        liq = (sum(p * q for p, q in bids if p >= mid - band)
               + sum(p * q for p, q in asks if p <= mid + band))
        return liq
    except Exception:
        return None


def _volume_confirmation(binance, symbol, interval="15m", lookback=20):
    """Hacim-teyidi: son mum hacmi / önceki `lookback` mum ortalaması (spike oranı).
    >1 = artan ilgi (hareketi teyit). None=veri yok/hata (çağıran nötr'e düşer)."""
    try:
        kl = binance._get("/fapi/v1/klines",
                          {"symbol": symbol, "interval": interval, "limit": lookback + 1})
        if not isinstance(kl, list) or len(kl) < lookback + 1:
            return None
        vols = [float(k[5]) for k in kl]      # klines index 5 = base volume
        prev = vols[:-1]
        avg = sum(prev) / len(prev) if prev else 0.0
        return (vols[-1] / avg) if avg > 0 else None
    except Exception:
        return None


def run_tiered_scan(binance, blacklist=None):
    """Faz 4: vadesi gelen katmanları çalıştırıp birleşik havuzu Q'ya göre döndürür.

    Tek paylaşılan ticker snapshot + per-symbol ATR/depth/hacim cache'i (TTL'li) ile
    rate-limit korunur; metrik toplama katman evreniyle sınırlıdır. Saf skor/zamanlama
    scan_engine'de. Bu tick'te vadesi gelen katman yoksa [] döner. Dönen öğeler orijinal
    ticker alanlarını + 'Q'/'vol_score' içerir (open_coin ile drop-in uyumlu)."""
    from scan_engine import MetricCache, due_tiers, compute_quality_scores
    global _SCAN_CACHE
    if _SCAN_CACHE is None:
        _SCAN_CACHE = MetricCache()
    cache = _SCAN_CACHE
    blacklist = blacklist or set()
    now = time.time()

    due = due_tiers(now, _SCAN_LAST_RUN)
    if not due:
        return []

    # Paylaşılan ticker snapshot (tek ağır çağrı, TTL 60s)
    tickers = cache.get_or_fetch("tickers", 60, binance.tickers)
    if not isinstance(tickers, list):
        return []
    base = []
    for d in tickers:
        try:
            s = d["symbol"]
            if not s.endswith("USDT") or s in blacklist:
                continue
            vol = float(d["quoteVolume"])
            chg = abs(float(d["priceChangePercent"]))
            if vol >= MIN_VOLUME_USDT and MIN_PCT_CHANGE <= chg <= MAX_PCT_CHANGE:
                base.append(d)
        except (KeyError, ValueError):
            continue
    base.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)

    # Vadesi gelenlerin en büyük evrenini kullan; hot vadeyse gerçek depth kullan.
    tier = max(due, key=lambda t: t.universe)
    use_depth = any(t.liquidity_mode == "depth" for t in due)
    universe = base[:tier.universe]

    raw = []
    for d in universe:
        sym = d["symbol"]
        try:
            qv = float(d["quoteVolume"])
            mom = abs(float(d["priceChangePercent"]))
            atr = cache.get(f"atr:{sym}", 300)
            if atr is None:
                atr = calculate_atr(binance, sym)
                if atr is not None:
                    cache.put(f"atr:{sym}", atr)
            if use_depth:
                liq = cache.get(f"liq:{sym}", 300)
                if liq is None:
                    liq = _depth_liquidity(binance, sym)
                    if liq is not None:
                        cache.put(f"liq:{sym}", liq)
                if liq is None:
                    liq = qv  # depth hatası → proxy'ye düş
            else:
                liq = qv
            vc = cache.get(f"vc:{sym}", 300)
            if vc is None:
                vc = _volume_confirmation(binance, sym)
                if vc is not None:
                    cache.put(f"vc:{sym}", vc)
            if vc is None:
                vc = 1.0  # teyit yok → nötr
            raw.append({**d, "liquidity": liq, "momentum": mom,
                        "atr_pct": atr, "volume_conf": vc})
        except Exception as e:
            log.debug(f"  Faz4 metrik hatası {sym}: {e}")

    for t in due:
        _SCAN_LAST_RUN[t.name] = now
    return compute_quality_scores(raw)


def calculate_position_size(balance_usdt, coin):
    """
    Risk-bazlı dinamik pozisyon boyutu.
    Returns: (notional_usdt, vol_mult)
    """
    chg = abs(float(coin["priceChangePercent"]))

    # Volatilite çarpanı: hareketli coin → küçük pozisyon
    if chg >= 12:   vol_mult = 0.50
    elif chg >= 8:  vol_mult = 0.70
    elif chg >= 5:  vol_mult = 0.85
    else:           vol_mult = 1.00

    # Hedef marjin (tüm işlemler için)
    total_target_margin = balance_usdt * TARGET_MARGIN_USAGE_PCT
    if MAX_OPEN_POSITIONS <= 0:
        log.warning("calculate_position_size: MAX_OPEN_POSITIONS <= 0, returning 0")
        return 0.0, 0.0
    margin_per_trade = total_target_margin / MAX_OPEN_POSITIONS
    base_notional = margin_per_trade * LEVERAGE

    # Volatilite ayarı
    notional = base_notional * vol_mult

    # Sınırlar
    notional = min(notional, MAX_NOTIONAL_USDT)
    notional = max(notional, MIN_NOTIONAL_USDT)

    # Bakiye güvenlik kontrolü (toplam marjin bakiyenin %90'ını aşmasın)
    if margin_per_trade * MAX_OPEN_POSITIONS > balance_usdt * 0.9:
        safe_margin = (balance_usdt * 0.9) / MAX_OPEN_POSITIONS
        notional = safe_margin * LEVERAGE

    return notional, vol_mult


def decide_side(coin):
    """Mean reversion: düşenden LONG, yükselenden SHORT"""
    chg = float(coin["priceChangePercent"])
    if chg <= LONG_ON_DIP_PCT:
        return 1, "LONG"
    if USE_SHORTS and chg >= SHORT_ON_PUMP_PCT:
        return 2, "SHORT"
    return None, None


def calc_amount(price, filters):
    if not filters:
        return None
    step = float(filters["LOT_SIZE"]["stepSize"])
    min_q = float(filters["LOT_SIZE"]["minQty"])
    min_n = float(filters.get("MIN_NOTIONAL", {}).get("notional", 5))
    steps = max(1, int((TARGET_NOTIONAL / price) / step))
    amt = steps * step
    while amt * price < min_n:
        amt += step
    if amt < min_q:
        amt = min_q
    return round(amt, 8)


def calculate_dynamic_leverage(atr_pct):
    """
    Dinamik kaldıraç: leverage = target_risk / ATR%
    Referans: Moreira & Muir (2017), Alexander & Dakos (2022)
    """
    if atr_pct <= 0:
        return RISK_CONFIG["min_leverage"]
    target = RISK_CONFIG["target_risk_units"]
    raw = target / (atr_pct * 100.0)
    lev = int(round(raw))
    lev = max(RISK_CONFIG["min_leverage"], min(RISK_CONFIG["max_leverage"], lev))
    return lev


def get_real_slippage(binance, symbol):
    """Spread + baz slipaj (orderbook'tan)"""
    try:
        ob = binance.order_book(symbol, limit=5)
        bid = float(ob["bids"][0][0])
        ask = float(ob["asks"][0][0])
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid
        base = {"BTCUSDT": 0.0002, "ETHUSDT": 0.0002, "SOLUSDT": 0.0003}.get(symbol, 0.0005)
        return (spread_pct / 2) + base
    except Exception as e:
        log.warning(f"slippage hatası {symbol}: {e}")
        return 0.001


def calculate_position_size_v2(balance_usdt, sl_pct, slippage_pct):
    """Risk-bazlı boyutlandırma (Van Tharp)."""
    risk_amount = balance_usdt * RISK_CONFIG["risk_per_trade_pct"]
    effective_sl = sl_pct + slippage_pct
    if effective_sl <= 0:
        return None
    notional = risk_amount / effective_sl
    notional = min(notional, RISK_CONFIG["max_notional"])
    notional = max(notional, RISK_CONFIG["min_notional"])
    return notional, risk_amount, effective_sl


def _verify_oneway(binance):
    """Verify the account is in ONEWAY position mode (A9/F7).

    Returns (ok, mode). ok is True ONLY when the exchange confirms ONEWAY
    (dualSidePosition == false). HEDGE, or any unverifiable result, returns
    (False, ...) so the caller fails closed and blocks new entries — the
    symbol-keyed reservation and netting assumptions only hold under ONEWAY.
    """
    try:
        r = binance.position_mode()
    except Exception:
        return (False, "UNKNOWN")
    if not isinstance(r, dict) or "dualSidePosition" not in r:
        return (False, "UNKNOWN")
    v = r["dualSidePosition"]
    hedge = (v.strip().lower() == "true") if isinstance(v, str) else bool(v)
    return ((not hedge), "HEDGE" if hedge else "ONEWAY")


def _exchange_open_symbols(binance):
    """Set of symbols with a non-zero exchange position, or None on API failure.
    None means the caller MUST fail-closed (do not admit a new position)."""
    positions = binance.position_risk()
    if not isinstance(positions, list):
        return None
    out = set()
    for p in positions:
        try:
            if float(p.get("positionAmt", 0)) != 0:
                out.add(p["symbol"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _reservation_should_keep(create_result) -> bool:
    """Fate of a slot reservation after create_executor returns.

    KEEP (do not release) when the executor exists OR the outcome is UNKNOWN (a
    position may have been created). RELEASE only on a CONFIRMED failure: no
    executor id and outcome != UNKNOWN (NOT_CREATED, pre-create refusal, or a
    <500 rejection). This preserves the reserved slot across UNKNOWN outcomes so
    the cap can never be under-counted; reconcile() resolves it against the
    exchange later.
    """
    if not isinstance(create_result, dict):
        return True  # unparseable -> treat as UNKNOWN, keep (fail-safe for cap)
    if create_result.get("executor_id"):
        return True
    return create_result.get("outcome") == "UNKNOWN"


def coordinate_close(coordinator, binance, symbol, reason, side=None, amount=None):
    """Route EVERY position close through the single CloseCoordinator authority
    (inc13). In the live loop a coordinator is always wired, so there is no direct
    market-close bypass: the close becomes one idempotent, cross-process-guarded,
    flat-confirmed reduce-only close (HB executor stopped keep_position, pre-sent
    orders resolved, UNKNOWN -> no blind re-POST). The direct reduce-only fallback
    is used ONLY when no coordinator is wired (a legacy/dry caller) so a close is
    never silently skipped."""
    if coordinator is not None:
        r = coordinator.request_close(symbol, reason)
        if r.ok:
            log.info(f"  ✅ koordineli kapanış [{reason}]: {r.action}")
        else:
            log.critical(f"  🚨 koordineli kapanış güvenli durdu [{reason}]: "
                         f"{r.action} — {r.reason}")
        return r
    # B-1: NO direct MARKET close bypass. Without the central coordinator we cannot
    # guarantee idempotency / flat-confirmation / cross-process intent, so a raw
    # reduce-only MARKET POST could double-close or race another process. Send
    # NOTHING and fail closed; run_once blocks new entries when the coordinator is
    # unavailable, and the open position keeps its exchange SL (K1=B).
    from close_coordinator import CloseResult, CloseState
    log.critical(f"  🚨 B-1: CloseCoordinator YOK — merkezi kapanış gönderilemiyor "
                 f"[{reason}]; doğrudan MARKET kapanış YASAK (fail-closed).")
    return CloseResult(False, CloseState.FAILED_SAFE, "NO_COORDINATOR",
                       reason="no central close authority; refusing direct MARKET close",
                       critical=True)


def open_coin(binance, hb, coin, dry_run=False, risk=None, reserver=None, coordinator=None):
    # HB_OFFLINE: gerçek emir YOK — dry-run'a zorla (create_executor asla çalışmaz).
    # Böylece 🔬 Faz3[compare] logu üretilir ama borsaya/HB'ye emir gönderilmez.
    if HB_OFFLINE:
        dry_run = True
    # C6-FIX: Fonksiyon başında defansif fail-closed limit kontrolü (HB_OFFLINE'da HB'siz;
    # yoksa bu ilk hb.active_executors() çağrısı offline'da patlar ve compare log'a ulaşılamaz).
    if HB_OFFLINE:
        _cur = set()
    else:
        try:
            _cur = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
        except Exception as e:
            log.critical(f"🚨 C6: open_coin executor sayısı alınamadı, fail-closed: {e}")
            return False
    if not HB_OFFLINE and len(_cur) >= MAX_OPEN_POSITIONS:
        log.info(f"  C6: limit dolu ({len(_cur)}/{MAX_OPEN_POSITIONS}), atlanıyor")
        return False
    symbol = coin["symbol"]
    pair = symbol.replace("USDT", "-USDT")
    price = float(coin["lastPrice"])
    log.info(f"→ {pair} fiyat={price} hacim={float(coin['quoteVolume']):,.0f} skor={coin['_score']:.3f}")

    ok, msg = check_liquidity(binance, symbol)
    if not ok:
        log.warning(f"  atlandı: {msg}")
        add_blacklist(symbol, msg)
        return False
    log.info(f"  likidite OK: {msg}")

    # Korelasyon limiti kontrolü
    if not check_correlation_limit(symbol, binance):
        log.info("  atlandı: korelasyon limiti")
        return False

    filt = binance.filters(symbol)
    if not filt:
        add_blacklist(symbol, "filters yok")
        return False
    # Yön kararı
    side, side_name = decide_side(coin)
    if side is None:
        log.info(f"  atlandı: yön belirsiz (%{float(coin['priceChangePercent']):+.2f})")
        return False
    log.info(f"  yön={side_name}")

    # ATR-bazlı dinamik SL/TP
    atr_pct = calculate_atr(binance, symbol, interval="15m", period=14)
    if atr_pct is None:
        atr_pct = 0.02
        log.warning("  ATR alınamadı, fallback %2.0 kullanılıyor")

    sl_pct = min(max(atr_pct * RISK_CONFIG["atr_sl_multiplier"], RISK_CONFIG["sl_min_pct"]), RISK_CONFIG["sl_max_pct"])
    tp_pct = min(max(atr_pct * RISK_CONFIG["atr_tp_multiplier"], RISK_CONFIG["tp_min_pct"]), RISK_CONFIG["tp_max_pct"])
    log.info(f"  📊 ATR=%{atr_pct*100:.2} → SL=%{sl_pct*100:.2f} TP=%{tp_pct*100:.2f}")

    # Slipaj (sadece flag açıksa)
    if RISK_CONFIG.get("risk_guards_enabled", False):
        slippage_pct = get_real_slippage(binance, symbol)
        log.info(f"  📏 Slipaj=%{slippage_pct*100:.2}")
    else:
        slippage_pct = 0.0

    # === Faz 5: balina FİLTRESİ ('off' → atlanır, ek API çağrısı yok) ===
    # compare: logla + whale_metrics'e yaz, karar değişmez. on: yalnız REDDEDEBİLİR.
    if WHALE_HUNTING in ("compare", "on"):
        try:
            import whale_engine as _we
            global _WHALE_TRACKER
            if _WHALE_TRACKER is None:
                _WHALE_TRACKER = _we.WhaleTracker()
            _sd = 1 if side == 1 else -1
            _wi = _we.fetch_whale_inputs(binance, symbol)
            if _wi is None:
                _sig, _edge = _we.WhaleSignal(), None
            else:
                _sig = _we.compute_signal(_wi, _WHALE_TRACKER, RISK_CONFIG)
                _edge = _we.expected_edge(
                    _sd, _sig, sl_pct=sl_pct, tp_pct=tp_pct, atr_pct=atr_pct,
                    drawdown=float(getattr(risk, "current_drawdown", 0.0) or 0.0),
                    notional=RISK_CONFIG["max_notional"], bids=_wi["bids"], asks=_wi["asks"],
                    fee_rt=RISK_CONFIG["fee_pct_round_trip"], funding_rate=_wi.get("funding"),
                    cfg=RISK_CONFIG) if _sig.quality_ok else None
            _allow, _why = _we.evaluate_filter(_sd, _sig, _edge, RISK_CONFIG)
            log.info(f"  🐋 Faz5[{WHALE_HUNTING}]: dir={_sig.direction:+d} güç={_sig.strength:.2f} "
                     f"spoof={_sig.spoof_suspect} | "
                     + (f"p_win={_edge.p_win:.3f} E[kâr]={_edge.e_profit*100:.3f}% "
                        f"maliyet={_edge.cost*100:.3f}% RP={_edge.risk_premium*100:.3f}% "
                        f"E_net={_edge.e_net*100:.3f}%" if _edge else "edge=yok")
                     + f" | {'GEÇER' if _allow else 'RED'}: {_why}")
            _we.record_metrics(_we.WHALE_DB, symbol, WHALE_HUNTING, _sd, _sig, _edge, _allow, _why)
            if WHALE_HUNTING == "on" and not _allow:
                log.info(f"  atlandı: balina filtresi ({_why})")
                return False
        except Exception as _e:
            if WHALE_HUNTING == "on":
                log.warning(f"  Faz5 balina filtresi hatası, fail-closed (giriş yok): {_e}")
                return False
            log.warning(f"  Faz5 whale_engine hatası (compare, karar etkilenmedi): {_e}")

    # Bakiye
    balance = binance.get_available_balance("USDT")
    if balance < 10:
        log.warning(f"  yetersiz bakiye: {balance:.2f} USDT")
        return False

    # Boyutlandırma (FAZ 1.2: RiskEngine onayı)
    if RISK_CONFIG.get("risk_guards_enabled", False) and risk is not None:
        target_notional, status = risk.approve_trade(balance, sl_pct + slippage_pct)
        if target_notional is None:
            log.warning(f"  RiskEngine reddetti: {status}")
            return False
        log.info(f"  RiskEngine onayladı: {target_notional:.2f} USDT (günlük: {risk.daily_trades}, ardışık zarar: {risk.consecutive_losses})")
    else:
        target_notional, vol_mult = calculate_position_size(balance, coin)
        log.info(f"  bakiye={balance:.2f} → hedef_notional={target_notional:.2f} USDT (vol×{vol_mult:.2})")

    # === Faz 3: bilimsel kaldıraç + boyutlandırma (birleşik L/S, margin=S/L) ===
    # Bayrak 'off' iken bu blok atlanır -> canlı davranış DEĞİŞMEZ. 'compare' eski↔yeni
    # değerleri loglar ama ESKİyi kullanır; 'on' yeni formülü kullanır (kalibrasyon şart).
    _new_lev = None  # 'on' modunda aşağıdaki kaldıraç bloğunda kullanılır
    if SCIENTIFIC_SIZING in ("compare", "on"):
        try:
            from leverage_engine import (signal_stats_from_ledger, scientific_leverage,
                                         scientific_position_size, margin_for)
            _dd = float(getattr(risk, "current_drawdown", 0.0) or 0.0)
            _stats = signal_stats_from_ledger(SL_LEDGER_DB)
            _new_s = scientific_position_size(
                balance, RISK_CONFIG["risk_per_trade_pct"], atr_pct, _stats,
                MIN_NOTIONAL_USDT, MAX_NOTIONAL_USDT)
            _new_lev = scientific_leverage(atr_pct, _stats, _dd)
            _old_lev = (calculate_dynamic_leverage(atr_pct)
                        if RISK_CONFIG.get("risk_guards_enabled", False) else LEVERAGE)
            log.info(
                f"  🔬 Faz3[{SCIENTIFIC_SIZING}]: q={_stats.q:.2f} "
                f"(n={_stats.n}, f*={_stats.f_star:.3f}, reliable={_stats.reliable}) | "
                f"S: eski={target_notional:.2f} → yeni={_new_s:.2f} | "
                f"L: eski={_old_lev} → yeni={_new_lev:.2f} | "
                f"margin≈{margin_for(_new_s, _new_lev):.2f} USDT")
            if SCIENTIFIC_SIZING == "on":
                target_notional = _new_s
        except Exception as _e:
            log.warning(f"  Faz3 leverage_engine hatası (eski formüle düşülüyor): {_e}")
            _new_lev = None

    # Target notional → amount
    step = float(filt["LOT_SIZE"]["stepSize"])
    raw_amount = target_notional / price
    steps = max(1, int(raw_amount / step))
    amount = round(steps * step, 8)

    min_n = float(filt.get("MIN_NOTIONAL", {}).get("notional", 5))
    if amount * price < min_n:
        amount = round((min_n / price / step + 1) * step, 8)

    log.info(f"  amount={amount} (≈{amount * price:.2f} USDT)")

    if dry_run:
        log.info("  [DRY] emir yok")
        return True

    # Dinamik kaldıraç (flag açıksa ATR-bazlı, kapalıysa sabit 3x)
    if SCIENTIFIC_SIZING == "on" and _new_lev is not None:
        dyn_lev = max(1, int(round(_new_lev)))
        log.info(f"  🔬 Faz3 kaldıraç uygulandı: {dyn_lev}x (bilimsel formül)")
    elif RISK_CONFIG.get("risk_guards_enabled", False):
        dyn_lev = calculate_dynamic_leverage(atr_pct)
        log.info(f"  🧠 Dinamik kaldıraç: ATR=%{atr_pct*100:.2f} → {dyn_lev}x")
    else:
        dyn_lev = LEVERAGE

    lr = binance.set_leverage(symbol, dyn_lev)
    if isinstance(lr, dict) and "leverage" in lr:
        log.info(f"  leverage={lr['leverage']}x")
    else:
        log.warning(f"  leverage yanıtı: {lr}")

    # TOCTOU FIX: atomically reserve a position slot BEFORE the create POST.
    # exchange_open_symbols comes from positionRisk (single source of truth); the
    # reservation counts exchange positions UNION outstanding reservations against
    # MAX_OPEN_POSITIONS in one BEGIN IMMEDIATE tx, so concurrent candidates or a
    # second process can never breach the cap. Released on confirmed failure;
    # KEPT on UNKNOWN (reconcile resolves it against the exchange later).
    _res_id = None
    if reserver is not None:
        _eos = _exchange_open_symbols(binance)
        if _eos is None:
            log.critical("🚨 rezervasyon: positionRisk alınamadı, fail-closed (yeni pozisyon yok)")
            return False
        _rr = reserver.reserve(symbol, _eos)
        if not _rr.ok:
            log.info(f"  🔒 rezervasyon reddetti: {_rr.reason} (kullanım={_rr.used}/{MAX_OPEN_POSITIONS})")
            return False
        _res_id = _rr.reservation_id
        log.info(f"  🔒 slot rezerve (id={_res_id}, kullanım={_rr.used}/{MAX_OPEN_POSITIONS})")

    er = hb.create_executor(symbol, amount, dyn_lev, sl_pct, tp_pct, TIME_LIMIT_SEC, side=side)
    eid = er.get("executor_id")
    if not eid:
        if reserver is not None and _res_id is not None:
            if _reservation_should_keep(er):
                log.warning(f"  🔒 rezervasyon KORUNUYOR (id={_res_id}): UNKNOWN sonuç, reconcile çözecek")
            else:
                reserver.release(_res_id, reason="executor_not_created")
                log.info(f"  🔓 rezervasyon serbest (id={_res_id}): executor oluşmadı")
        add_blacklist(symbol, f"executor: {er}")
        log.error(f"  executor açılamadı: {er}")
        return False
    log.info(f"  executor={eid}")

    time.sleep(8)
    ex = hb.get_executor(eid)
    filled = ex.get("filled_amount_quote", 0) if ex else 0
    err = ex.get("error_count", 0) if ex else 999
    if filled < 1 or err > 0:
        log.warning(f"  ⚠️  dolmadı: filled={filled:.2f} err={err}")
        # Faz2 execution robustness: stop the unfilled/errored executor
        # (keep_position=True so any partial fill becomes a position that
        # ensure_sl_coverage/orphan_killer will protect) and RELEASE the reserved
        # slot. Exchange positions are always counted in the cap (used = exchange ∪
        # reservations), so releasing never under-counts; this prevents a leaked
        # PENDING reservation from starving MAX_OPEN_POSITIONS until reconcile's TTL.
        try:
            hb.stop_executor(eid, keep_position=True)
        except Exception as e:
            log.warning(f"  executor durdurma hatası (dolmadı): {e}")
        if reserver is not None and _res_id is not None:
            reserver.release(_res_id, reason="not_filled")
            log.info(f"  🔓 rezervasyon serbest (id={_res_id}): dolmadı")
        add_blacklist(symbol, "dolmadı")
        return False
    log.info(f"  ✅ doldu: {filled:.2f} USDT")

    tick = float(filt["PRICE_FILTER"]["tickSize"])
    if side == 1:
        trig = round(round((price * (1 - sl_pct)) / tick) * tick, 8)
    else:
        trig = round(round((price * (1 + sl_pct)) / tick) * tick, 8)
    # === K-4: open_coin SL-placement intent (full prevention of the open_coin <->
    # ensure_sl_coverage double-SL race) ===
    # Acquire the SAME cross-process SlPlacementStore intent (K-3) that
    # ensure_sl_coverage uses, keyed on (symbol, '<side>:<entryPrice>') from exchange
    # truth, so the two paths can never both place an SL for this position epoch --
    # without waiting for the <=60s deterministic dedupe. Done BEFORE the pre-open
    # reconciliation: if another process owns this placement we must not cancel its
    # SL nor place a duplicate; we skip and let the sl_status verification below
    # confirm it (or fail-closed emergency close, exactly as today).
    try:
        from sl_placement import SlPlacementStore, ACQUIRED as _SL_ACQ
        _oc_store = SlPlacementStore(SL_LEDGER_DB)
        _oc_store.init_schema()
    except Exception as _e:
        log.warning(f"K-4 SlPlacementStore kurulamadi: {_e}")
        _oc_store = None
        _SL_ACQ = "ACQUIRED"
    _oc_tok = None
    if _oc_store is not None:
        try:
            _oc_pr = binance.position_risk()
            _oc_p = next((q for q in _oc_pr if q.get("symbol") == symbol
                          and float(q["positionAmt"]) != 0), None) \
                if isinstance(_oc_pr, list) else None
            if _oc_p is not None:
                _oc_tok = ("LONG" if side == 1 else "SHORT") + ":" + str(_oc_p.get("entryPrice"))
        except Exception as _e:
            log.warning(f"K-4 intent token alinamadi (positionRisk): {_e}")
            _oc_tok = None
    _oc_owned = (_oc_store is None or _oc_tok is None
                 or _oc_store.acquire(symbol, _oc_tok) == _SL_ACQ)

    if not _oc_owned:
        log.info(f"  BUSY: {symbol} SL yerlestirme baska process'te - open_coin atladi, dogrulamaya birakildi")
        sr = None
        algo_id = None
    else:
        # Faz2.1 (K-1/K-2) pre-open reconciliation: cancel any leftover algo SL for this
        # symbol (orphan from a prior/closed position) BEFORE placing the new one, so a
        # freshly opened position can never end up with a DUPLICATE SL.
        try:
            for _o in binance.open_algo_orders_for(symbol):
                _aid = _o.get("algoId")
                if _aid:
                    binance.cancel_algo_order(_aid)
                    log.info(f"  pre-open: eski algo SL iptal (algoId={_aid}) - tek SL garantisi")
        except Exception as e:
            log.warning(f"  pre-open SL reconciliation hatasi: {e}")

        sr = binance.place_algo_sl(symbol, trig, amount, side)
        algo_id = sr.get("algoId") if isinstance(sr, dict) else None
        if _oc_store is not None and _oc_tok is not None:
            if isinstance(sr, dict) and sr.get("algoId"):
                _oc_store.mark_placed(symbol, _oc_tok)
            else:
                _oc_store.release(symbol, _oc_tok)   # failure -> reflect reality/retry
    # === /K-4 ===

    # F-1: verify the SL with the inc10.1 mode-aware tri-state sl_status, NOT a bare
    # algoId+qty>=95% check. PRESENT requires a LIVE STOP_MARKET that FULLY protects
    # THIS position under its mode (side / positionSide / reduceOnly|closePosition /
    # quantity / status). ABSENT or UNKNOWN -> not verified -> emergency close.
    # Placement is ONEWAY only; HEDGE never reaches here (run_once gates it) and
    # sl_status would reject a ONEWAY-style SL in HEDGE -> fail-closed.
    from close_adapter import BinanceHBCloseAdapter, SL_PRESENT, SL_UNKNOWN
    _sl_adapter = BinanceHBCloseAdapter(binance, None)
    _mode_ok, _mode = _verify_oneway(binance)
    _side_str = "LONG" if side == 1 else "SHORT"
    sl_verified = False
    sl_state = None
    log.info(f"  ⏳ SL emri gonderildi (algoId={algo_id}), sl_status ile dogrulaniyor...")
    for attempt in range(3):
        time.sleep(3)
        try:
            sl_state = _sl_adapter.sl_status(symbol, _side_str, mode=_mode,
                                             position_amt=amount)
        except Exception as e:
            log.error(f"  SL durum sorgu hatasi (API): {e}")
            sl_state = SL_UNKNOWN
        if sl_state == SL_PRESENT:
            sl_verified = True
            break
        log.warning(f"  SL dogrulama {attempt+1}/3: durum={sl_state} (algoId={algo_id})")

    if not sl_verified:
        log.critical(f"  🚨 SL DOGRULANAMADI! (durum={sl_state}, algoId={algo_id}). "
                     f"Pozisyon acil KAPATILIYOR!")
        try:
            # Single close authority (inc13): coordinated reduce-only close, not a
            # raw market POST — idempotent + flat-confirmed + cross-process guarded.
            coordinate_close(coordinator, binance, symbol, "SL_UNVERIFIED",
                             side=side, amount=amount)
        except Exception as e:
            log.critical(f"  🚨🚨 ACIL KAPATMA BASARISIZ: {e} !!! MANUEL MUDAHALE GEREKLI !!!")
        
        if risk is not None:
            risk.is_halted = True
            risk.halt_reason = f"SL dogrulanamadi: {symbol}"
            risk._save_state()
        return False
    else:
        log.info(f"  ✅ SL dogrulandi (algoId={algo_id}, qty={amount})")

    if RISK_CONFIG.get("risk_guards_enabled", False):
        try:
            pc = binance.position_risk()
            for pos in pc:
                if pos["symbol"] == symbol and float(pos["positionAmt"]) != 0:
                    liq = float(pos.get("liquidationPrice", 0))
                    entry_pos = float(pos["entryPrice"])
                    if liq > 0:
                        liq_dist = abs(entry_pos - liq) / entry_pos
                        sl_dist = abs(entry_pos - trig) / entry_pos
                        log.info(f"  🔍 Likidasyon={liq} (%{liq_dist*100:.2}) vs SL %{sl_dist*100:.2}")
                        if sl_dist >= liq_dist * 0.7:
                            log.critical("  🚨 SL likidasyona çok yakın — ACİL KAPAT")
                            coordinate_close(coordinator, binance, symbol,
                                             "LIQ_TOO_CLOSE", side=side, amount=amount)
                            add_blacklist(symbol, "liq_too_close")
                            return False
                    break
        except Exception as e:
            log.warning(f"  Likidasyon kontrol hatası: {e}")

    if risk is not None:
        risk.register_open()
        log.info("  📊 RiskEngine: günlük sayaç arttı")

    # Tracked list (orphan_killer cleanup only) - atomic write
    try:
        TRACKED_JSON = Path.home() / ".tracked_symbols.json"
        tracked = set()
        if TRACKED_JSON.exists():
            tracked = set(json.loads(TRACKED_JSON.read_text()))
        tracked.add(symbol)
        tmp = TRACKED_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(tracked), indent=2))
        tmp.replace(TRACKED_JSON)
    except Exception as e:
        log.warning(f"tracked update: {e}")

    # Permanent archive_symbols registration (independent of orphan_killer)
    try:
        import sqlite3
        _adb = sqlite3.connect("/root/.trading_archive.db", timeout=10)
        _adb.execute("""CREATE TABLE IF NOT EXISTS archive_symbols (
            symbol TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            source TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_trade_sync TEXT,
            last_order_sync TEXT
        )""")
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc).isoformat().replace("+00:00", "Z")
        _adb.execute("""
            INSERT INTO archive_symbols (symbol, first_seen_at, last_seen_at, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET last_seen_at=excluded.last_seen_at
        """, (symbol, _now, _now, "auto_trader_open"))
        _adb.commit()
        _adb.close()
    except Exception as e:
        log.warning(f"archive_symbols register failed: {e}")

    return True


def run_once(dry_run=False):
    log.info("=" * 60)
    log.info(f"Tarama: {datetime.now().isoformat()}")
    heartbeat_tick()
    risk = RiskEngine(RISK_CONFIG)
    reserver = PositionReserver(risk.db_file, MAX_OPEN_POSITIONS)
    try:
        reserver.init_schema()
    except Exception as e:
        log.error(f"rezervasyon şeması init hatası: {e}")
    bk = os.environ.get("BINANCE_KEY")
    bs = os.environ.get("BINANCE_SECRET")
    hu = os.environ.get("HB_USERNAME", "FURKAN")
    hp = os.environ.get("HB_PASSWORD", "1973")
    if not bk or not bs:
        log.error("BINANCE_KEY/SECRET yok")
        return

    binance = BinanceAPI(bk, bs)
    hb = HBAPI(HB_API, hu, hp)
    if HB_OFFLINE:
        log.warning("🔌 HB_OFFLINE aktif — Hummingbot API atlanıyor; open_coin dry-run'a "
                    "zorlanacak (gerçek emir YOK). Kalibrasyon/log toplama modu.")

    # inc13: wire the SINGLE close authority into the live loop. Every position
    # close routes through this CloseCoordinator (idempotent, flat-confirmed, HB
    # autonomous closes disabled). Cross-process idempotency uses CloseIntentStore in
    # the SAME risk ledger db as PositionReserver. On failure -> None, and
    # coordinate_close falls back to a direct reduce-only close (never skips a close).
    coordinator = None
    try:
        from close_adapter import BinanceHBCloseAdapter
        from close_coordinator import CloseCoordinator
        from close_intent import CloseIntentStore
        _intent = CloseIntentStore(risk.db_file)
        _intent.init_schema()
        coordinator = CloseCoordinator(BinanceHBCloseAdapter(binance, hb),
                                       intent_store=_intent)
    except Exception as e:
        log.critical(f"🚨 CloseCoordinator kurulamadı, fallback doğrudan kapanış: {e}")

    # FAZ 1.2: Günlük reset kontrolü
    today = datetime.now().date().isoformat()
    if risk.state.get("last_reset_date") != today:
        risk.reset_daily()
        risk.state["last_reset_date"] = today
        risk._save_state()
        log.info(f"  Günlük sayaçlar sıfırlandı ({today})")

    # FAZ 1.2: Son taramadan bu yana kapanan executor PnL'lerini bildir
    sync_risk_engine(risk, hb, binance=binance)

    # cleanup_orphan_sl artık orphan_killer.py tarafından yapılıyor.
    # K6: compute drift/stale as FLAGS (do not early-return on them) so the
    # protection check still runs for open positions; then gate new entries.
    drift = not reconcile_positions(binance, hb)
    stale = not check_stale_data(binance, max_age_sec=30)

    allow_new, _reasons = protect_then_gate(binance, reserver, drift, stale)
    if not allow_new:
        if drift:
            log.critical("🚨 Reconciliation drift — yeni giriş engellendi (koruma denetimi yine de çalıştı)")
        if stale:
            log.critical("🚨 Stale data — yeni giriş engellendi")
        if not _reasons["mode_ok"]:
            log.critical(f"🚨 Pozisyon modu {_reasons['mode']} (ONEWAY değil) — yeni giriş fail-closed engellendi")
        if _reasons["unprotected"]:
            log.critical(f"🚨 OPEN_UNPROTECTED {sorted(_OPEN_UNPROTECTED)} — yeni giriş "
                         f"engellendi (borsa SL doğrulanana dek alarm açık)")
        return
    log.info("  ✅ Koruma denetimi çalıştı; mod ONEWAY; yeni giriş izni var")

    # B-1: the central close authority is mandatory for new entries. If the
    # CloseCoordinator/CloseIntentStore could not be built, block NEW entries
    # (fail-closed) — the SL protection check above already ran for open positions.
    if coordinator is None:
        log.critical("🚨 B-1: CloseCoordinator/CloseIntentStore kurulamadı — merkezi "
                     "kapanış otoritesi YOK; yeni girişler fail-closed engellendi "
                     "(açık pozisyonların borsa SL koruma denetimi çalıştı)")
        return

    if HB_OFFLINE:
        active = set()  # HB_OFFLINE: HB'siz; aktif executor yok sayılır (kalibrasyon modu)
    else:
        try:
            active = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
        except Exception as e:
            log.error(f"aktif executor sayısı alınamadı, pozisyon limiti kontrolü ATLANDI: {e}")
            active = set()
    if not HB_OFFLINE and len(active) >= MAX_OPEN_POSITIONS:
        log.info(f"Maks pozisyon limiti ({MAX_OPEN_POSITIONS}) dolu → yeni açmıyorum")
        return

    try:
        eq = binance.get_balance("USDT")
        risk.update_equity(eq)
        log.info(f"💰 Equity={eq:.2} | DD=%{risk.current_drawdown*100:.2} | Peak={risk.peak_equity:.2}")

        # Exposure cap kontrolü
        positions_for_exposure = binance.position_risk()
        if isinstance(positions_for_exposure, list):
            risk.update_exposure(eq, positions_for_exposure)

        if risk.is_halted:
            log.critical(f"🚨 Sistem DURDU: {risk.halt_reason}")
            return
    except Exception as e:
        log.warning(f"Risk kontrol hatası: {e}")
    bl = load_blacklist()
    log.info(f"Aktif: {sorted(active) if active else 'yok'}")
    log.info(f"Blacklist: {sorted(bl.keys())}")

    picks = scan_top(binance, TOP_N * 3, blacklist=set(bl.keys()))

    # Faz 4: hiyerarşik tarama (flag arkasında; 'off' → bu blok atlanır, davranış değişmez).
    if HIERARCHICAL_SCAN in ("compare", "on"):
        try:
            tiered = run_tiered_scan(binance, blacklist=set(bl.keys()))
            if tiered:
                log.info(
                    f"  🧭 Faz4[{HIERARCHICAL_SCAN}]: tiered(top5 Q)="
                    f"{[(t['symbol'], round(t['Q'], 3)) for t in tiered[:5]]} | "
                    f"scan_top={[p['symbol'] for p in picks[:5]]}")
                if HIERARCHICAL_SCAN == "on":
                    picks = tiered
            else:
                log.info(f"  🧭 Faz4[{HIERARCHICAL_SCAN}]: bu tick'te vadesi gelen katman yok")
        except Exception as e:
            log.warning(f"  Faz4 tiered scan hatası (scan_top'a düşülüyor): {e}")

    if not picks:
        log.warning("hiç aday yok")
        return

    candidates = [c for c in picks
                  if c["symbol"].replace("USDT", "-USDT") not in active
                  and c["symbol"] not in bl][:TOP_N]
    if not candidates:
        log.info("yeni aday yok")
        return

    ok = 0
    for c in candidates:
        # C6-FIX: Her çağrıdan hemen önce pozisyon sayısını yeniden kontrol et
        if HB_OFFLINE:
            _cur_active = set()  # HB_OFFLINE: kalibrasyon modu, limit kontrolü HB'siz
        else:
            try:
                _cur_active = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
            except Exception as e:
                log.critical(f"🚨 C6: döngü içi executor sayısı alınamadı, fail-closed: {e}")
                break
        if not HB_OFFLINE and len(_cur_active) >= MAX_OPEN_POSITIONS:
            log.info(f"🚫 C6: limit doldu ({len(_cur_active)}/{MAX_OPEN_POSITIONS}), döngüden çıkılıyor")
            break
        try:
            # HB_OFFLINE: open_coin dry-run'a zorlanır — compare log üretilir ama GERÇEK
            # EMİR GÖNDERİLMEZ (HB'siz güvenli kalibrasyon/log toplama).
            if open_coin(binance, hb, c, dry_run=(dry_run or HB_OFFLINE), risk=risk,
                         reserver=reserver, coordinator=coordinator):
                ok += 1
        except Exception as e:
            log.exception(f"{c['symbol']} istisna: {e}")
            add_blacklist(c["symbol"], "istisna")
    log.info(f"Bitti: {ok}/{len(candidates)}")


def main():
    log_file = Path.home() / "auto_trader.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)]
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.loop:
        log.info(f"Döngü, {a.interval}s (heartbeat her 60s)")
        while True:
            try:
                run_once(a.dry)
            except Exception as e:
                log.exception(f"döngü: {e}")
            # Uyku 60s'lik dilimlerde, her dilimde heartbeat yaz
            chunks = max(1, a.interval // 60)
            for _ in range(chunks):
                heartbeat_tick()
                time.sleep(60)
    else:
        run_once(a.dry)


if __name__ == "__main__":
    main()
