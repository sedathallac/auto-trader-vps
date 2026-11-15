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
import coin_quality
import leverage_policy
import sizing_policy
import preflight
import requests

BINANCE_FAPI = "https://fapi.binance.com"
HB_API = "http://localhost:8000"
HB_ACCOUNT = "master_account"

def _env_float(name, default):
    """Env'den float; geçersiz/boş → default. Tarama eşikleri yeniden derleme gerektirmeden ayarlanabilsin."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


MIN_VOLUME_USDT = _env_float("MIN_VOLUME_USDT", 200_000_000)
MIN_PCT_CHANGE = _env_float("MIN_PCT_CHANGE", 3.0)
MAX_PCT_CHANGE = _env_float("MAX_PCT_CHANGE", 15.0)
_SCAN_FUNNEL = {}           # son scan_top tarama hunisi (ticker → USDT → blacklist → hacim → değişim → picks)
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
# Faz 6: strateji/indikatör teyit katmanı bayrağı. off=canlı davranış değişmez (ek API çağrısı YOK),
# compare=teyit/p_real loglanır + signal_outcomes.db'ye yazılır ama karar ESKİ mantıkla verilir,
# on=yalnız FİLTRE (trend veto / teyit yetersiz / p_real düşük girişi REDDEDER; asla giriş tetiklemez).
STRATEGY_CONFIRMATION = os.getenv("STRATEGY_CONFIRMATION", "off").strip().lower()
# FAZ13-C: ters yön politikası yalnız compare/shadow modunda uygulanır. Bu bayrak kapalıysa
# compare de asıl yönle karar verir (eski davranış). Canlı ('on') bu bayraktan ETKİLENMEZ.
REVERSE_POLICY_ENABLED = os.getenv("REVERSE_POLICY", "on").strip().lower() in ("1", "on", "true", "yes")
# Faz 4: --loop sürecinde run_once çağrıları arasında yaşayan cache + tier zamanlama.
_SCAN_CACHE = None          # scan_engine.MetricCache (tembel init)
_SCAN_LAST_RUN = {}         # tier.name -> son çalışma epoch'u
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.05
TIME_LIMIT_SEC = 14400  # 4 saat
TOP_N = int(_env_float("TOP_N", 2))   # FAZ13-A: env'e bağlı; picks altında kalırsa huniden geçen kadar probe edilir
# Faz 9: WHALE_HUNTING=compare iken bu kadar aday DEĞERLENDİRİLİR (ilk TOP_N open_coin ile; kalanı compare_probe ile
# YALNIZ loglanır — emir/blacklist/rezervasyon yok). 0 veya TOP_N altı → ek aday yok.
try:
    TOP_N_COMPARE = max(0, min(50, int(os.getenv("TOP_N_COMPARE", "20"))))
except ValueError:
    TOP_N_COMPARE = 20
MAX_OPEN_POSITIONS = max(0, int(os.getenv("MAX_OPEN_POSITIONS", "0")))  # 0 = EMERGENCY (yeni giris durdu)
_HB_AUTH_BROKEN = False  # FAZ14f: Hummingbot kimlik hatasi sonrasi surecte yeni emir denenmez


def _effective_max_positions():
    """Pozisyon tavanı: canlıda LIVE_MAX_OPEN_POSITIONS, compare'da MAX_OPEN_POSITIONS.

    Modül düzeyi MAX_OPEN_POSITIONS yalnız compare servisi içindir (env'de taşınır).
    Canlı servis limiti LIVE_MAX_OPEN_POSITIONS ile verir; eski kod kapılarda modül
    düzeyini okuduğu için canlıda limit 0 görünüyor ve `len(active) >= 0` her zaman
    doğru olup tarama hiç başlamıyordu.
    """
    try:
        cfg = _live_cfg()
    except Exception:
        return MAX_OPEN_POSITIONS
    if getattr(cfg, "is_live", False):
        try:
            return max(0, int(cfg.max_open_positions))
        except (TypeError, ValueError):
            return 0
    return MAX_OPEN_POSITIONS

RISK_CONFIG = {
    "max_drawdown_pct": 0.20,
    "max_total_margin_pct": 0.40,
    "max_consecutive_losses": 3,
    "max_trades_per_day": 10000,  # FAZ14: gunluk islem SAYISI sinirsiz; tek sinir zarar limiti
    "risk_per_trade_pct": 0.015,
    "kelly_fraction": 0.25,
    "fee_pct_round_trip": 0.0008,
    "slippage_pct_round_trip": 0.0004,
    "min_net_reward_risk": 1.20,
    "min_notional_policy": "refuse",
    "max_daily_loss_pct": 0.05,
    "min_coin_quality": 0.55,
    "risk_guards_enabled": True,
    "target_risk_units": 3.0,
    "min_leverage": 1,
    "max_leverage": 5,
    "min_notional": 5.5,
    "max_notional": 999999.0,  # FAZ14h: sabit USDT tavani kaldirildi; baglayici tavan max_position_pct (equity %20)
    "max_position_pct": 20.0,  # FAZ14: equity yuzdesi tavani (sabit USDT yerine olceklenen matematik)
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
# Faz 6: teyit eşikleri (muhafazakâr; min_p_real=0.65 yönetici kararı, ⚠️ CALIBRATION-REQUIRED).
from strategy_engine import CONFIRM_DEFAULTS as _CONFIRM_DEFAULTS
RISK_CONFIG.update(_CONFIRM_DEFAULTS)

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
MAX_NOTIONAL_USDT = float(RISK_CONFIG["max_notional"])  # FAZ14h: tek dogruluk kaynagi; sabit 200 kaldirildi
USE_SHORTS = True          # short işlemleri aktif
# FAZ13-A: yön barajı env'e bağlı. Tarama barajı gevşetilip bu baraj sabit kalınca adaylar
# "yön belirsiz" ile düşüyordu (Faz6 çağrısı 2.0 → 0.2/tarama). Varsayılanlar eski davranış.
LONG_ON_DIP_PCT = _env_float("LONG_ON_DIP_PCT", -3.0)     # %3+ düşmüşse LONG (mean reversion)
SHORT_ON_PUMP_PCT = _env_float("SHORT_ON_PUMP_PCT", 3.0)  # %3+ yükselmişse SHORT (mean reversion)

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
    def __init__(self, key, secret, base=None):
        self.key = key
        self.secret = secret
        self._info = None
        self._info_ts = 0
        # FAZ9 live: the base URL is per instance, so the testnet and mainnet instances can never share a client.
        # BINANCE_FUTURES_BASE_URL wins, then the module default (mainnet) — the compare instance is unchanged.
        self.base = (base or os.getenv("BINANCE_FUTURES_BASE_URL") or BINANCE_FAPI).rstrip("/")

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
            return _session.get(f"{self.base}{path}?{self._sign(params)}",
                                headers=self._hdr(), timeout=15).json()
        return _session.get(f"{self.base}{path}", params=params, timeout=15).json()

    def _post(self, path, params, method="POST"):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 60000
        url = f"{self.base}{path}?{self._sign(params)}"
        if method == "DELETE":
            return _session.delete(url, headers=self._hdr(), timeout=15).json()
        return _session.post(url, headers=self._hdr(), timeout=15).json()

    def account(self):
        """GET /fapi/v2/account — the startup check reads balance and canTrade from here."""
        return self._get("/fapi/v2/account", signed=True)

    def tickers(self):
        return self._get("/fapi/v1/ticker/24hr")

    def order_book(self, symbol, limit=20):
        return self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def funding_rate(self, symbol):
        """GET /fapi/v1/premiumIndex — anlık funding oranı (lastFundingRate)."""
        return self._get("/fapi/v1/premiumIndex", {"symbol": symbol})

    def open_interest(self, symbol):
        """GET /fapi/v1/openInterest — açık pozisyon miktarı (base cinsinden)."""
        return self._get("/fapi/v1/openInterest", {"symbol": symbol})

    def taker_klines(self, symbol, interval="5m", limit=1):
        """Son `limit` mum: taker buy/sell oranı için (kline[9]=takerBuyBase)."""
        return self._get("/fapi/v1/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit})

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
    # Hummingbot PositionExecutorConfig yalnız 1 (LONG), 2 (SHORT), 3 (HEDGE) kabul eder.
    # ±1 konvansiyonu sızarsa executor reddedilir; burada fail-closed durdurulur.
    if side not in (1, 2, 3):
        raise ValueError(f"invalid executor side={side!r}: Hummingbot expects 1, 2 or 3")
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
    if HB_OFFLINE:
        # HB_OFFLINE = emir YOK (veri toplama). ONEWAY doğrulaması YALNIZ yeni-giriş kapısında atlanır; geçersiz/IP-kısıtlı
        # anahtarla (-2015) mod UNKNOWN olup compare verisini engellemesin. _verify_oneway'in kendisi DEĞİŞMEDİ:
        # ensure_sl_coverage / SL doğrulaması gerçek modu kullanmaya devam eder (yanlış mod varsayımıyla SL gönderilmez).
        log.info("🔌 HB_OFFLINE: ONEWAY kontrolü yeni-giriş kapısında atlandı (varsayılan: ONEWAY)")
        mode_ok, mode = True, "ONEWAY"
    else:
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
                # FAZ 2.3 + FAZ11 L3: ATR bazli SL (open_coin ile tutarli, sizing_policy)
                atr_pct = calculate_atr(binance, sym) or 0.02
                sl_pct = sizing_policy.suggest_sl_tp(
                    atr_pct, direction=1 if is_long else -1, cfg=RISK_CONFIG).sl_pct
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
    funnel = {"tickers": 0, "usdt": 0, "blacklisted": 0, "volume": 0, "pct": 0, "picks": 0}
    for d in binance.tickers():
        funnel["tickers"] += 1
        try:
            if not d["symbol"].endswith("USDT"):
                continue
            funnel["usdt"] += 1
            if d["symbol"] in blacklist:
                funnel["blacklisted"] += 1
                continue
            vol = float(d["quoteVolume"])
            if vol < MIN_VOLUME_USDT:
                continue
            funnel["volume"] += 1
            chg = abs(float(d["priceChangePercent"]))
            if not (MIN_PCT_CHANGE <= chg <= MAX_PCT_CHANGE):
                continue
            funnel["pct"] += 1
            cands.append(d)
        except (KeyError, ValueError):
            continue
    funnel["picks"] = min(len(cands), max(0, int(n)))
    _SCAN_FUNNEL.clear()
    _SCAN_FUNNEL.update(funnel)
    if not cands:
        return []
    mv = max(float(d["quoteVolume"]) for d in cands)
    mc = max(abs(float(d["priceChangePercent"])) for d in cands)
    for d in cands:
        d["_score"] = 0.6 * (float(d["quoteVolume"]) / mv) + 0.4 * (abs(float(d["priceChangePercent"])) / mc)
    cands.sort(key=lambda x: x["_score"], reverse=True)
    top = cands[:n]
    # FAZ11: legacy yol da kalite taşısın (yalnız seçilen n coin için, tek çağrı seti).
    for d in top:
        d.update(_attach_coin_quality(binance, d["symbol"], d))
    return top


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


def _coin_quality_metrics(binance, symbol, ticker, cache=None, atr_pct=None, ttl=300):
    """FAZ11: order book + funding + OI + taker oranı → coin_quality.score_coin girdisi.

    Her metrik ayrı TTL'li cache anahtarında tutulur; bir metrik hatası diğerlerini
    düşürmez (eksik metrik skorda raporlanır, uydurulmaz).
    """
    def _cached(key, fn):
        if cache is not None:
            hit = cache.get(key, ttl)
            if hit is not None:
                return hit
        try:
            val = fn()
        except Exception:
            return None
        if val is not None and cache is not None:
            cache.put(key, val)
        return val

    ob = _cached(f"ob:{symbol}", lambda: binance.order_book(symbol, limit=20))
    bids = ob.get("bids") if isinstance(ob, dict) else None
    asks = ob.get("asks") if isinstance(ob, dict) else None
    metrics = {
        "spread_bps": coin_quality.spread_bps_from_book(bids, asks) if bids and asks else None,
        "depth_usdt_1pct": coin_quality.depth_within_pct(bids, asks, 0.01) if bids and asks else None,
        "quote_volume_24h": float(ticker.get("quoteVolume") or 0) or None,
        "atr_pct": atr_pct,
        "price_change_24h_pct": (float(ticker["priceChangePercent"]) / 100.0
                                 if ticker.get("priceChangePercent") is not None else None),
    }
    fr = _cached(f"fund:{symbol}", lambda: binance.funding_rate(symbol))
    if isinstance(fr, dict) and fr.get("lastFundingRate") is not None:
        try:
            metrics["funding_rate"] = float(fr["lastFundingRate"])
        except (TypeError, ValueError):
            metrics["funding_rate"] = None
    oi = _cached(f"oi:{symbol}", lambda: binance.open_interest(symbol))
    if isinstance(oi, dict) and oi.get("openInterest") is not None:
        try:
            qty = float(oi["openInterest"])
            px = float(ticker.get("lastPrice") or 0)
            metrics["open_interest_usdt"] = qty * px if px > 0 else None
        except (TypeError, ValueError):
            metrics["open_interest_usdt"] = None
    kl = _cached(f"taker:{symbol}", lambda: binance.taker_klines(symbol, "5m", 1))
    metrics["taker_buy_sell_ratio"] = coin_quality.taker_ratio_from_klines(kl) if kl else None
    return metrics


def _attach_coin_quality(binance, symbol, ticker, cache=None, atr_pct=None, direction=1):
    """Ticker'a CQ / quality_vetoes / funding_cost_bps alanlarını ekler (hata → CQ None)."""
    try:
        metrics = _coin_quality_metrics(binance, symbol, ticker, cache, atr_pct)
        cq = coin_quality.score_coin(symbol, metrics, direction=direction)
        return {"CQ": cq.score, "quality_ok": cq.ok,
                "quality_vetoes": list(cq.vetoes), "quality_missing": list(cq.missing),
                "funding_cost_bps": cq.funding_cost_bps,
                "quality_components": cq.components}
    except Exception as e:
        log.debug(f"  Faz11 kalite hatası {symbol}: {e}")
        return {"CQ": None, "quality_ok": False, "quality_vetoes": ["quality_error"],
                "quality_missing": [], "funding_cost_bps": 0.0, "quality_components": {}}


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
                        "atr_pct": atr, "volume_conf": vc,
                        **_attach_coin_quality(binance, sym, d, cache, atr)})
        except Exception as e:
            log.debug(f"  Faz4 metrik hatası {sym}: {e}")

    for t in due:
        _SCAN_LAST_RUN[t.name] = now
    scored = compute_quality_scores(raw)
    # FAZ11: kalite vetosu olan coinler taranmış havuzdan ÇIKARILIR (fail-closed).
    kept = [c for c in scored if c.get("quality_ok")]
    if len(kept) != len(scored):
        log.info(f"  🧪 Faz11 kalite: {len(scored) - len(kept)} coin veto/eleme "
                 f"({len(kept)} kaldı)")
    return kept


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
    _cap = _effective_max_positions()
    if _cap <= 0:
        log.warning("calculate_position_size: pozisyon tavani <= 0, returning 0")
        return 0.0, 0.0
    margin_per_trade = total_target_margin / _cap
    base_notional = margin_per_trade * LEVERAGE

    # Volatilite ayarı
    notional = base_notional * vol_mult

    # Sınırlar
    notional = min(notional, MAX_NOTIONAL_USDT)
    notional = max(notional, MIN_NOTIONAL_USDT)

    # Bakiye güvenlik kontrolü (toplam marjin bakiyenin %90'ını aşmasın)
    if margin_per_trade * _cap > balance_usdt * 0.9:
        safe_margin = (balance_usdt * 0.9) / _cap
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


def calculate_dynamic_leverage(atr_pct, *, drawdown=0.0, f_star=None, stage_cap=None):
    """FAZ11 Katman 2: leverage_policy.compute_leverage delege eder.

    Beş tavanın en küçüğü (volatilite, drawdown, Kelly, tasfiye, kademe/borsa) ve
    tam sayıya AŞAĞI yuvarlama. Eski `target_risk/ATR%` formülü yalnız volatilite
    tavanını görüyordu; tasfiye güvenliği ve Kelly hesaba katılmıyordu.
    """
    if atr_pct is None or atr_pct <= 0:
        return RISK_CONFIG["min_leverage"]
    sigma = float(atr_pct)
    sl_pct = min(max(sigma * RISK_CONFIG["atr_sl_multiplier"], RISK_CONFIG["sl_min_pct"]),
                 RISK_CONFIG["sl_max_pct"])
    d = leverage_policy.compute_leverage(
        sigma, sl_pct,
        drawdown=float(drawdown or 0.0),
        f_star=f_star,
        stage_cap=stage_cap if stage_cap is not None else RISK_CONFIG["max_leverage"],
        exchange_max=RISK_CONFIG["max_leverage"],
    )
    return max(RISK_CONFIG["min_leverage"], min(RISK_CONFIG["max_leverage"], d.leverage))


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
    """Risk-bazlı boyutlandırma — FAZ11 Katman 3 politikasına delege eder.

    Eski gövde `max(notional, min_notional)` ile kayıp bütçesini aşabiliyordu;
    `sizing_policy.size_position` bütçe minimumu karşılamıyorsa None döner.
    Dönen üçlü geriye uyumludur: (notional|None, risk_amount, effective_sl).

    FAZ14: canlıda sabit USDT tavanı yerine equity yüzdesi (`LIVE_MAX_POSITION_PCT`)
    uygulanır; kademe override'ları global RISK_CONFIG'i kirletmeden yerel kopyaya girer.
    """
    import sizing_policy
    cfg = dict(RISK_CONFIG)
    try:
        _lc = _live_cfg()
        if getattr(_lc, "is_live", False):
            cfg.update(_lc.risk_overrides())
    except Exception:
        pass
    d = sizing_policy.size_position(balance_usdt, sl_pct + slippage_pct, cfg=cfg)
    return d.notional, d.budget, d.effective_sl


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


def _is_hb_auth_error(create_result) -> bool:
    """Hummingbot kimlik/konfig hatası mı? (401/403 veya 'Incorrect username or password')

    FAZ14f: bu hata sembolün özelliği değildir. Sembolü karalisteye yazmak, kimlik
    düzeltildiğinde bile o sembolün TTL boyunca işlem görmemesine yol açıyordu.
    """
    if not isinstance(create_result, dict):
        return False
    detail = str(create_result.get("detail") or create_result.get("error") or "").lower()
    if "incorrect username or password" in detail or "unauthorized" in detail:
        return True
    code = create_result.get("status_code") or create_result.get("status")
    try:
        return int(code) in (401, 403)
    except (TypeError, ValueError):
        return False


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


_LAST_CONFIRM = {}          # son Faz6 teyidinin aileleri (live family gate için)


_LIVE_CFG = None            # live_guard.LiveConfig (tembel; HB_OFFLINE=1 iken hiç yüklenmez)
_startup_checked = [False]  # live açılış doğrulaması süreç başına bir kez
_EDGE_CACHE = {"ts": 0.0, "decisions": None}  # FAZ10: promotion_gate TTL cache


def _live_cfg():
    """Live config, or None in compare mode. A bad live config raises: the service must not start."""
    global _LIVE_CFG
    if _LIVE_CFG is None:
        import live_guard
        _LIVE_CFG = live_guard.LiveConfig.from_env().validate()
    return _LIVE_CFG


def _live_family_gate(symbol, side):
    """(allowed, reason). In compare/dry-run the gate is open; in live only the winning family may place an order."""
    try:
        import live_guard
        cfg = _live_cfg()
    except Exception as e:
        log.critical(f"🚨 live config geçersiz, fail-closed: {e}")
        return False, f"live config error: {e}"
    if not cfg.is_live:
        return True, "not live: gate open"
    if not cfg.strategy_family:
        # Regime-only configuration: the regime gate decides, the family gate stays open.
        return True, "no family filter: regime gate only"
    families = list((_LAST_CONFIRM.get("families") or {}).keys())
    if not families:
        return False, "no confirming family for this side"
    for fam in families:
        ok, reason = live_guard.family_gate(fam, cfg)
        if ok:
            log.info(f"  🎯 live family eşleşti: {fam} ({symbol} side={side})")
            return True, reason
    return False, f"confirming families {families} do not include live family {cfg.strategy_family!r}"


def _live_regime_gate():
    """(allowed, reason). Canlıda yalnız kazanan rejimler emir verebilir; compare'da kapı açık."""
    try:
        import live_guard
        cfg = _live_cfg()
    except Exception as e:
        log.critical(f"🚨 live config geçersiz, fail-closed: {e}")
        return False, f"live config error: {e}"
    if not cfg.is_live:
        return True, "not live: gate open"
    regime = _LAST_CONFIRM.get("regime")
    # FAZ14: ters politika uygulanmış bir sinyal varsa rejim kapısı açılır. Ters strateji
    # TAM OLARAK trend rejimi için kanıtlandı (wick_rejection/rsi_divergence, p<0.01);
    # LIVE_WINNING_REGIMES=range,mid bu hücreleri bloke ederdi.
    if _LAST_CONFIRM.get("reversed"):
        return True, f"reverse policy: regime={regime} allowed"
    ok, reason = live_guard.regime_gate(regime, cfg)
    if not ok:
        log.info(f"[LIVE] skipped regime={regime} (not in winning set)")
    return ok, reason


def _live_edge_gate():
    """(allowed, reason). Canlıda yalnız istatistiksel olarak KANITLANMIŞ aile×rejim hücresi emir verir.

    Kanıt yoksa (hiç çözülmüş hücre yok) kapı "shadow" modunda açık kalır: veri
    toplanmadan kanıt üretilemez. Kanıt varsa ve bu hücre geçmiyorsa emir YOK.
    """
    try:
        cfg = _live_cfg()
    except Exception as e:
        log.critical(f"🚨 live config geçersiz, fail-closed: {e}")
        return False, f"live config error: {e}"
    if not cfg.is_live:
        return True, "not live: gate open"
    mode = os.getenv("EDGE_GATE", "auto").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return True, "edge gate off"
    # FAZ14: ters politika hücresi backtest ile kanıtlı (wick_rejection/trend p<0.0001,
    # rsi_divergence/trend p=0.0011). Kanıt ters yönde olduğu için asıl-yön hücre kaydı
    # bu emri bloke etmemeli.
    if _LAST_CONFIRM.get("reversed"):
        return True, "reverse policy: cell proven by backtest"
    regime = _LAST_CONFIRM.get("regime")
    families = list((_LAST_CONFIRM.get("families") or {}).keys())
    if not families:
        return False, "no confirming family"
    try:
        import edge_engine
        import strategy_engine as _se
        decisions = _live_edge_decisions(_se.SignalStore(_se.SIGNAL_DB), RISK_CONFIG)
    except Exception as e:
        log.critical(f"🚨 edge gate hesabı başarısız, fail-closed: {e}")
        return False, f"edge gate error: {e}"
    if not decisions:
        log.info("[EDGE] shadow: henüz çözülmüş hücre yok, kapı veri toplama için açık")
        return True, "shadow: no resolved cells yet"
    for fam in families:
        ok, reason = edge_engine.gate_lookup(decisions, fam, regime)
        if ok:
            log.info(f"  🎓 edge cell kanıtlı: {fam}:{regime} ({reason})")
            return True, reason
    return False, f"no proven edge cell for {families} regime={regime}"


def _live_edge_decisions(store, cfg, ttl=300):
    """promotion_gate sonuçlarını süreç içinde TTL ile cache'ler (her tick'te DB taraması yok)."""
    now = time.time()
    if _EDGE_CACHE["decisions"] is not None and now - _EDGE_CACHE["ts"] < ttl:
        return _EDGE_CACHE["decisions"]
    import edge_engine
    cost = float(cfg.get("fee_pct_round_trip", 0.0008)) + \
        float(cfg.get("slippage_pct_round_trip", 0.0004))
    cells = edge_engine.cells_from_rows(store.resolved_rows(), cost=cost)
    decisions = edge_engine.promotion_gate(cells, cfg)
    _EDGE_CACHE["ts"] = now
    _EDGE_CACHE["decisions"] = decisions
    return decisions


_CALIB_LOGGED = {"done": False}


def _log_calibration_status():
    """Süreç başına bir kez: posterior hangi DB'den okunuyor, kaç hücre kalibre."""
    if _CALIB_LOGGED["done"]:
        return
    _CALIB_LOGGED["done"] = True
    try:
        import strategy_engine as _se
        cov = _se.SignalStore(_se.SIGNAL_DB, calib_db=_se.calib_db_path()).calibration_coverage(RISK_CONFIG)
        if cov.get("error"):
            log.warning(f"🧮 Kalibrasyon DB okunamadı ({cov['path']}): {cov['error']} — p_fake nötr 0.5 kalacak")
            return
        log.info(f"🧮 Kalibrasyon: db={cov['path']} hücre={cov['cells']} kalibre_hücre={cov['calibrated_cells']} "
                 f"(n≥{cov['min_events']}) skorlanabilir_satır={cov['scorable_rows']}")
        if not cov["calibrated_cells"]:
            log.warning("⚠️ CALIBRATION-REQUIRED: hiçbir (sinyal,rejim) hücresi min_events'i geçmiyor — "
                        "p_fake nötr 0.5, p_real prior'a eşit kalır")
    except Exception as e:
        log.warning(f"🧮 Kalibrasyon durumu okunamadı: {e}")


def _faz6_confirm(binance, symbol, side, sl_pct, mode):
    """Faz 6 teyit. compare: logla + signal_outcomes'a yaz, ASLA reddetmez. on: reddedebilir / hata → fail-closed.
    Döner: True = devam, False = reddet (yalnız mode=='on')."""
    try:
        import strategy_engine as _se
        _sd6 = 1 if side == 1 else -1
        _lim = RISK_CONFIG["confirm_kline_limit"]
        _main6 = _se.fetch_candles(binance, symbol, RISK_CONFIG["confirm_tf_main"], _lim)
        _tr6 = {tf: _se.fetch_candles(binance, symbol, tf, _lim)
                for tf in RISK_CONFIG["confirm_tf_veto"]}
        _store6 = _se.SignalStore(_se.SIGNAL_DB, calib_db=_se.calib_db_path())
        if _main6 is not None:
            _store6.resolve_pending(symbol, _main6, RISK_CONFIG["fee_pct_round_trip"], sl_pct)
        # FAZ14: ters politika canlıda da uygulanır (REVERSE_POLICY=on). Politika evaluate()
        # içinde trend vetosundan ÖNCE çalışır; nihai yön ters çevrilmiş sinyallerden gelir.
        _policy6 = (_se.REVERSE_POLICY
                    if (mode in ("compare", "on") and REVERSE_POLICY_ENABLED) else None)
        _v6 = _se.confirm(_sd6, _main6, _tr6, _store6, symbol, RISK_CONFIG, policy=_policy6)
        # FAZ9 live: the family gate needs to know which families actually confirmed for THIS side. Kept module-level so
        # _faz6_confirm keeps its bool signature and every existing caller is untouched.
        _LAST_CONFIRM.clear()
        _LAST_CONFIRM.update({"families": dict(_v6.families or {}), "regime": _v6.regime,
                              "p_real": _v6.p_real, "allow": bool(_v6.allow),
                              "effective_side": _v6.effective_side,
                              "reversed": tuple(_v6.reversed_signals)})
        log.info(f"  🧪 Faz6[{mode}]: rejim={_v6.regime} trend_karşı={_v6.trend_against:.2f} "
                 f"aileler={_v6.families} p_real={_v6.p_real:.3f} kalibre={_v6.calibrated} "
                 f"adx15={'-' if _v6.regime_adx is None else f'{_v6.regime_adx:.0f}'} "
                 f"adx_trend={'-' if _v6.trend_adx is None else f'{_v6.trend_adx:.0f}'} "
                 f"ters={list(_v6.reversed_signals) or '-'} | "
                 f"{'GEÇER' if _v6.allow else 'RED'}: {_v6.reason}")
        if _main6 is not None:
            for _r6 in _v6.readings:
                # Shadow: politika uygulanmış yön kaydedilir ki çözücü ters yönün sonucunu hesaplasın.
                # Shadow'da yön filtresi YOK (her iki yönün verisi gerekir); canlıda eski filtre korunur.
                _eff6 = _se.effective_direction(_r6, _v6.regime, _policy6)
                if _eff6 == 0 or _r6.strength < RISK_CONFIG["confirm_min_strength"]:
                    continue
                if _policy6 is None and _eff6 != _sd6:
                    continue
                _rec6 = _r6 if _eff6 == _r6.direction else \
                    _se.SignalReading(_r6.family, _r6.name, _eff6, _r6.strength)
                _store6.record_event(
                    symbol, _rec6, _v6.regime, _main6.t[-1], _main6.c[-1],
                    RISK_CONFIG["confirm_horizon"],
                    source="shadow" if _policy6 is not None else "live",
                    sl_pct=sl_pct
                )
        if mode == "on" and not _v6.allow:
            log.info(f"  atlandı: strateji teyidi ({_v6.reason})")
            return False
    except Exception as _e:
        if mode == "on":
            log.warning(f"  Faz6 teyit hatası, fail-closed (giriş yok): {_e}")
            return False
        log.warning(f"  Faz6 strategy_engine hatası (compare, karar etkilenmedi): {_e}")
    return True



def _resolve_pending_signal_outcomes(binance):
    """Aday listesine bağlı olmadan DB'deki tüm bekleyen Faz6 sembollerini sonuçlandır."""
    summary = {
        "symbols": 0,
        "resolved": 0,
        "scored": 0,
        "unscored": 0,
        "waiting": 0,
        "unavailable": 0,
        "errors": 0,
    }

    try:
        import strategy_engine as _se
        store = _se.SignalStore(_se.SIGNAL_DB)
        pending = store.pending_symbol_ranges()
    except Exception as exc:
        log.warning(f"Faz6 bekleyen sonuç kuyruğu okunamadı: {exc}")
        summary["errors"] += 1
        return summary

    if not pending:
        return summary

    summary["symbols"] = len(pending)
    tf = RISK_CONFIG.get(
        "confirm_tf_main",
        _se.CONFIRM_DEFAULTS["confirm_tf_main"],
    )
    cost = float(RISK_CONFIG.get("fee_pct_round_trip", 0.0008))

    for symbol, oldest_entry_ts in pending:
        try:
            candles = _se.fetch_candles_from(
                binance, symbol, tf, oldest_entry_ts
            )
            if candles is None:
                summary["unavailable"] += 1
                log.warning(
                    f"Faz6 sonuç kuyruğu: {symbol} mum verisi alınamadı; "
                    "kayıtlar beklemede bırakıldı."
                )
                continue

            result = store.resolve_pending_detailed(symbol, candles, cost)
            summary["resolved"] += result["resolved"]
            summary["scored"] += result["scored"]
            summary["unscored"] += result["unscored"]
            summary["waiting"] += result["waiting"]

            if result["error"]:
                summary["errors"] += 1
                log.error(
                    f"Faz6 sonuç kuyruğu DB hatası ({symbol}): {result['error']}"
                )
        except Exception as exc:
            summary["errors"] += 1
            log.exception(f"Faz6 bekleyen sonuç işlenemedi ({symbol}): {exc}")

    log.info(
        "Faz6 bekleyen sonuç kuyruğu: "
        f"sembol={summary['symbols']} "
        f"çözülen={summary['resolved']} "
        f"skorlanabilir={summary['scored']} "
        f"eski_stop_bilgisi_yok={summary['unscored']} "
        f"bekleyen={summary['waiting']} "
        f"veri_yok={summary['unavailable']} "
        f"hata={summary['errors']}"
    )
    return summary


def _faz5_whale(binance, symbol, side, sl_pct, tp_pct, atr_pct, risk, mode):
    """Faz 5 balina. compare: logla + whale_metrics'e yaz, ASLA reddetmez. on: reddedebilir / hata → fail-closed.
    Döner: True = devam, False = reddet (yalnız mode=='on')."""
    try:
        import whale_engine as _we
        global _WHALE_TRACKER
        if _WHALE_TRACKER is None:
            _WHALE_TRACKER = _we.WhaleTracker()
        _sd = 1 if side == 1 else -1
        _wi = _we.fetch_whale_inputs(binance, symbol, cfg=RISK_CONFIG)
        if _wi is None:
            _sig, _edge = _we.WhaleSignal(), None
        else:
            _sig = _we.compute_signal(_wi, _WHALE_TRACKER, RISK_CONFIG)
            # Offline backtest kalibrasyonu (OOS skill>0 ise) → p_win; yoksa cold-start formülü.
            _pw = (_we.pwin_from_calibration(_sd * _sig.components.get("cvd", 0.0),
                                             _we.load_pwin_calibration(_we.WHALE_PWIN_CALIBRATION))
                   if _sig.quality_ok else None)
            _edge = _we.expected_edge(
                _sd, _sig, sl_pct=sl_pct, tp_pct=tp_pct, atr_pct=atr_pct,
                drawdown=float(getattr(risk, "current_drawdown", 0.0) or 0.0),
                notional=RISK_CONFIG["max_notional"], bids=_wi["bids"], asks=_wi["asks"],
                fee_rt=RISK_CONFIG["fee_pct_round_trip"], funding_rate=_wi.get("funding"),
                p_win_calibrated=_pw, calibrated=_pw is not None,
                cfg=RISK_CONFIG) if _sig.quality_ok else None
        _allow, _why = _we.evaluate_filter(_sd, _sig, _edge, RISK_CONFIG)
        log.info(f"  🐋 Faz5[{mode}]: dir={_sig.direction:+d} güç={_sig.strength:.2f} "
                 f"spoof={_sig.spoof_suspect} | "
                 + (f"p_win={_edge.p_win:.3f} E[kâr]={_edge.e_profit*100:.3f}% "
                    f"maliyet={_edge.cost*100:.3f}% RP={_edge.risk_premium*100:.3f}% "
                    f"E_net={_edge.e_net*100:.3f}%" if _edge else "edge=yok")
                 + f" | {'GEÇER' if _allow else 'RED'}: {_why}")
        _we.record_metrics(_we.WHALE_DB, symbol, mode, _sd, _sig, _edge, _allow, _why)
        if mode == "on" and not _allow:
            log.info(f"  atlandı: balina filtresi ({_why})")
            return False
    except Exception as _e:
        if mode == "on":
            log.warning(f"  Faz5 balina filtresi hatası, fail-closed (giriş yok): {_e}")
            return False
        log.warning(f"  Faz5 whale_engine hatası (compare, karar etkilenmedi): {_e}")
    return True


def compare_probe(binance, coin, risk=None):
    """YALNIZ-LOG aday değerlendirmesi (WHALE_HUNTING=compare): Faz 6 + Faz 5 compare verisini toplar. Emir yok,
    blacklist yazmaz, rezervasyon/HB yok, hiçbir şeyi reddetmez. Döner: değerlendirildi mi."""
    symbol = coin["symbol"]
    side, side_name = decide_side(coin)
    if side is None:
        log.info(f"  🔎 probe {symbol}: yön belirsiz (%{float(coin['priceChangePercent']):+.2f}) — atlandı")
        return False
    atr_pct = calculate_atr(binance, symbol, interval="15m", period=14)
    if atr_pct is None:
        atr_pct = 0.02
    _geo = sizing_policy.suggest_sl_tp(atr_pct, direction=1 if side == 1 else -1,
                                       cfg=RISK_CONFIG)
    sl_pct, tp_pct = _geo.sl_pct, _geo.tp_pct
    log.info(f"→ 🔎 probe {symbol} yön={side_name} (yalnız compare verisi; emir YOK)")
    if STRATEGY_CONFIRMATION in ("compare", "on"):
        _faz6_confirm(binance, symbol, side, sl_pct, "compare")
    if WHALE_HUNTING in ("compare", "on"):
        _faz5_whale(binance, symbol, side, sl_pct, tp_pct, atr_pct, risk, "compare")
    return True


def select_candidates(picks, active, bl, top_n, total_n):
    """(ana adaylar, probe adayları). Ana = ilk top_n uygun aday (open_coin); probe = sonraki (total_n - top_n)."""
    eligible = [c for c in picks
                if c["symbol"].replace("USDT", "-USDT") not in active and c["symbol"] not in bl]
    main = eligible[:top_n]
    probes = eligible[top_n:max(top_n, total_n)]
    return main, probes


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
    if not HB_OFFLINE and len(_cur) >= _effective_max_positions():
        log.info(f"  C6: limit dolu ({len(_cur)}/{_effective_max_positions()}), atlanıyor")
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

    # FAZ11 Katman 1: çok kriterli coin kalite kapısı (veto + min skor).
    # Tarama zaten CQ ekliyorsa yeniden hesaplanmaz; yoksa burada tek sefer hesaplanır.
    if coin.get("CQ") is None:
        coin.update(_attach_coin_quality(binance, symbol, coin))
    _q_ok, _q_reason = coin_quality.quality_gate(coin, RISK_CONFIG.get("min_coin_quality",
                                                                     coin_quality.MIN_QUALITY))
    if not _q_ok:
        log.info(f"  atlandı: coin kalite ({_q_reason})")
        return False
    log.info(f"  kalite OK: {_q_reason}")

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

    # ATR-bazlı dinamik SL/TP — FAZ11 Katman 3: sizing_policy.suggest_sl_tp
    atr_pct = calculate_atr(binance, symbol, interval="15m", period=14)
    if atr_pct is None:
        atr_pct = 0.02
        log.warning("  ATR alınamadı, fallback %2.0 kullanılıyor")

    _geo = sizing_policy.suggest_sl_tp(atr_pct, direction=1 if side == 1 else -1,
                                       cfg=RISK_CONFIG)
    sl_pct, tp_pct = _geo.sl_pct, _geo.tp_pct
    log.info(f"  📊 ATR=%{atr_pct*100:.2} → SL=%{sl_pct*100:.2f} TP=%{tp_pct*100:.2f} "
             f"(net_rr={_geo.net_rr:.2f})")
    # Canlıda net R/R kapısı serttir; compare veri toplamaya devam eder (dokunulmaz).
    if not HB_OFFLINE and not _geo.ok:
        log.warning(f"  atlandı: SL/TP geometrisi ({_geo.reason})")
        return False

    # === Faz 6: strateji teyit FİLTRESİ ('off' → atlanır, ek API çağrısı yok) ===
    if STRATEGY_CONFIRMATION in ("compare", "on"):
        if not _faz6_confirm(binance, symbol, side, sl_pct, STRATEGY_CONFIRMATION):
            return False
        # FAZ14: ters politika nihai yönü çevirdiyse emir de o yönde açılır.
        # strategy_engine ±1 konvansiyonu kullanır (1=LONG, -1=SHORT); auto_trader ve
        # Hummingbot ise 1=LONG, 2=SHORT bekler. Dönüştürmeden geçirilirse SHORT
        # sinyaller HB'ye side=-1 olarak gider ve executor reddedilir
        # ("Input should be 1, 2 or 3").
        _eff_side = _LAST_CONFIRM.get("effective_side")
        if _eff_side in (1, -1):
            _hb_side = 1 if _eff_side == 1 else 2
            if _hb_side != side:
                side = _hb_side
                side_name = "LONG" if side == 1 else "SHORT"
                log.info(f"  🔄 ters politika: yön {side_name} olarak uygulandı "
                         f"(ters={list(_LAST_CONFIRM.get('reversed') or [])})")

    # FAZ9 live: yalnız kazanan aile emir verebilir. Compare/dry-run'da kapı açık; canlıda kapalı = sinyal
    # signal_outcomes'a yazıldı, emir yok.
    _fam_ok, _fam_reason = _live_family_gate(symbol, side)
    if not _fam_ok:
        log.info(f"  atlandı: live family gate ({_fam_reason})")
        return False

    # FAZ9 live: rejim kapısı. trend rejimi veri toplamaya devam eder, emir vermez.
    _reg_ok, _reg_reason = _live_regime_gate()
    if not _reg_ok:
        log.info(f"  atlandı: live regime gate ({_reg_reason})")
        return False

    # FAZ10: kanıtlanmış edge kapısı. Yalnız istatistiksel olarak pozitif hücre emir verir.
    _edge_ok, _edge_reason = _live_edge_gate()
    if not _edge_ok:
        log.info(f"  atlandı: live edge gate ({_edge_reason})")
        return False

    # FAZ10: net R/R kapısı — komisyon+slipaj düşüldükten sonra kazanç/kayıp oranı.
    if RISK_CONFIG.get("risk_guards_enabled", False):
        import edge_engine as _ee
        _rr, _rr_ok, _rr_detail = _ee.net_reward_risk(
            tp_pct, sl_pct,
            RISK_CONFIG.get("fee_pct_round_trip", 0.0008),
            RISK_CONFIG.get("slippage_pct_round_trip", 0.0004))
        _min_rr = float(RISK_CONFIG.get("min_net_reward_risk", 1.20))
        if not _rr_ok or _rr < _min_rr:
            log.warning(f"  atlandı: net R/R {_rr:.2f} < {_min_rr:.2f} ({_rr_detail})")
            return False
        log.info(f"  ⚖️  net R/R={_rr:.2f} ({_rr_detail})")

    # Slipaj (sadece flag açıksa)
    if RISK_CONFIG.get("risk_guards_enabled", False):
        slippage_pct = get_real_slippage(binance, symbol)
        log.info(f"  📏 Slipaj=%{slippage_pct*100:.2}")
    else:
        slippage_pct = 0.0

    # === Faz 5: balina FİLTRESİ ('off' → atlanır, ek API çağrısı yok) ===
    if WHALE_HUNTING in ("compare", "on"):
        if not _faz5_whale(binance, symbol, side, sl_pct, tp_pct, atr_pct, risk, WHALE_HUNTING):
            return False

    # Bakiye
    balance = binance.get_available_balance("USDT")
    if balance < 10:
        log.warning(f"  yetersiz bakiye: {balance:.2f} USDT")
        return False

    # Boyutlandırma (FAZ 1.2: RiskEngine onayı)
    if RISK_CONFIG.get("risk_guards_enabled", False) and risk is not None:
        target_notional, status = risk.approve_trade(balance, sl_pct + slippage_pct,
                                                     take_profit_pct=tp_pct)
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
            from leverage_engine import signal_stats_from_ledger, unified_sizing
            _dd = float(getattr(risk, "current_drawdown", 0.0) or 0.0)
            _stats = signal_stats_from_ledger(SL_LEDGER_DB)
            # Faz 7: Faz 3 çıktısı RiskEngine değişmezleriyle birleştirilir (S≤risk_notional,
            # L≤tasfiye-güvenli tavan, margin≤bütçe). risk_guards kapalıysa yalnız sert tavanlar.
            _risk_n = (target_notional
                       if (RISK_CONFIG.get("risk_guards_enabled", False) and risk is not None) else None)
            _u = unified_sizing(balance, atr_pct, sl_pct + slippage_pct, _dd, _stats, RISK_CONFIG,
                                MIN_NOTIONAL_USDT, MAX_NOTIONAL_USDT, risk_notional=_risk_n)
            _new_s, _new_lev = _u.notional, float(_u.leverage)
            _old_lev = (calculate_dynamic_leverage(atr_pct)
                        if RISK_CONFIG.get("risk_guards_enabled", False) else LEVERAGE)
            log.info(
                f"  🔬 Faz3[{SCIENTIFIC_SIZING}]: q={_stats.q:.2f} "
                f"(n={_stats.n}, f*={_stats.f_star:.3f}, reliable={_stats.reliable}) | "
                f"S: eski={target_notional:.2f} → yeni={_new_s:.2f} (faz3={_u.s_faz3:.2f}) | "
                f"L: eski={_old_lev} → yeni={_u.leverage} (faz3={_u.l_faz3:.2f}) | "
                f"margin≈{_u.margin:.2f} USDT | sınırlar={_u.caps or '-'}")
            if SCIENTIFIC_SIZING == "on":
                target_notional = _new_s
        except Exception as _e:
            log.warning(f"  Faz3 leverage_engine hatası (eski formüle düşülüyor): {_e}")
            _new_lev = None

    # Target notional → amount (FAZ10: Decimal adım yuvarlama; min notional altı → emir YOK)
    import edge_engine as _ee_amt
    step = float(filt["LOT_SIZE"]["stepSize"])
    min_q = float(filt["LOT_SIZE"].get("minQty", 0) or 0)
    min_n = float(filt.get("MIN_NOTIONAL", {}).get("notional", 5))
    amount, _amt_ok = _ee_amt.amount_for_notional(target_notional, price, step, min_q, min_n)
    if not _amt_ok:
        if dry_run:
            log.warning(f"  [DRY] miktar min notional'ı karşılamıyor "
                        f"(amount={amount}, ≈{amount * price:.2f} < {min_n:.2f} USDT) — emir yok")
        else:
            log.warning(f"  atlandı: miktar min notional'ı karşılamıyor "
                        f"(amount={amount}, ≈{amount * price:.2f} < {min_n:.2f} USDT)")
            return False

    log.info(f"  amount={amount} (≈{amount * price:.2f} USDT)")

    if dry_run:
        log.info("  [DRY] emir yok")
        return True

    # Dinamik kaldıraç (flag açıksa ATR-bazlı, kapalıysa sabit 3x)
    _dd_now = float(getattr(risk, "current_drawdown", 0.0) or 0.0)
    # FAZ14: kademe tavanı sabit LIVE_LEVERAGE değil, LIVE_MAX_LEVERAGE'dir. compute_leverage
    # çıktısı kırpılmaz; yalnız borsa/kademe tavanına clamp edilir.
    _lc_lev = _live_cfg()
    _stage_cap = _lc_lev.max_leverage if _lc_lev.is_live else None
    if SCIENTIFIC_SIZING == "on" and _new_lev is not None:
        dyn_lev = max(1, int(round(_new_lev)))
        log.info(f"  🔬 Faz3 kaldıraç uygulandı: {dyn_lev}x (bilimsel formül)")
    elif RISK_CONFIG.get("risk_guards_enabled", False):
        dyn_lev = calculate_dynamic_leverage(atr_pct, drawdown=_dd_now,
                                             stage_cap=_stage_cap)
        _levd = leverage_policy.compute_leverage(
            float(atr_pct), float(sl_pct),
            drawdown=float(_dd_now or 0.0), f_star=None,
            stage_cap=_stage_cap if _stage_cap is not None else RISK_CONFIG["max_leverage"],
            exchange_max=RISK_CONFIG["max_leverage"])
        _cd = _levd.caps_detail
        log.info(f"  🧠 Dinamik kaldıraç: ATR=%{atr_pct*100:.2f} σ={atr_pct:.4f} "
                 f"c={leverage_policy.LEVERAGE_DEFAULTS['c']:.3f} → L_vol={_cd.get('volatility')} "
                 f"→ round={dyn_lev}x → risk_per_trade={_levd.risk_per_trade*100:.2f}% "
                 f"(leverage_policy)")
        log.info(f"  [LIVE] caps: volatility={_cd.get('volatility')} liq={_cd.get('liquidation')} "
                 f"kelly={_cd.get('kelly')} stage={_cd.get('stage')} "
                 f"exchange={_cd.get('exchange')} → binding={_levd.reason}")
    else:
        dyn_lev = LEVERAGE
    if not HB_OFFLINE:
        log.info(f"[LIVE] leverage: {symbol} ATR=%{atr_pct*100:.2f} → {dyn_lev}x "
                 f"(cap={_stage_cap if _stage_cap is not None else '-'})")

    # FAZ11 Katman 2: kaldıraç politikası — tasfiye tavanını aşan kaldıraçla emir açılmaz.
    _lev_ok, _lev_detail = leverage_policy.leverage_ok(dyn_lev, sl_pct)
    if not _lev_ok:
        log.warning(f"  atlandı: kaldıraç güvenli değil ({_lev_detail})")
        return False

    # FAZ11 Katman 4: 12 adımlı uçuş öncesi kontrol (yalnız canlı; compare dokunulmaz).
    if not HB_OFFLINE and _live_cfg().is_live:
        from types import SimpleNamespace as _NS
        _pf = preflight.pre_flight_check(
            cfg=_live_cfg(),
            hb_offline=0,
            account=lambda: binance.account(),
            exchange_info=lambda: binance.exchange_info(),
            symbol=symbol,
            filters=filt,
            quality=coin,
            leverage=dyn_lev,
            sl_pct=sl_pct,
            size=_NS(ok=target_notional is not None,
                     reason=f"notional={target_notional}"),
            geometry=_geo,
            risk=(target_notional, "approved"),
            exposure={"open_positions": len(_cur),
                      "max_positions": _effective_max_positions(),
                      "margin_usage": float(getattr(risk, "margin_usage", 0.0) or 0.0),
                      "margin_limit": 0.4},
            edge=(_edge_ok, _edge_reason),
        )
        if not _pf.ok:
            log.warning(f"  atlandı: preflight {_pf.failed_at} ({_pf.reason})")
            return False
        log.info(f"  ✅ preflight 12/12 adım geçti")
        _pf_passed = len(_pf.steps) if getattr(_pf, "steps", None) else 12
    else:
        _pf_passed = 0

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
            log.info(f"  🔒 rezervasyon reddetti: {_rr.reason} (kullanım={_rr.used}/{_effective_max_positions()})")
            return False
        _res_id = _rr.reservation_id
        log.info(f"  🔒 slot rezerve (id={_res_id}, kullanım={_rr.used}/{_effective_max_positions()})")

    # FAZ14: her emirde beş beynin kararı loglanır (denetim izi).
    if not HB_OFFLINE:
        _cq = coin.get("CQ")
        log.info(f"[LIVE] coin_quality: {symbol} cq={'n/a' if _cq is None else f'{float(_cq):.3f}'} "
                 f"vetoes={coin.get('quality_vetoes') or '-'}")
        log.info(f"[LIVE] sizing: risk_budget={target_notional * (sl_pct + slippage_pct):.2f} "
                 f"size_usdt={target_notional:.2f} "
                 f"cap_pct={RISK_CONFIG.get('max_position_pct')}% equity={balance:.2f}")
        log.info(f"[LIVE] preflight: {_pf_passed} adım geçti")
        log.info(f"[LIVE] open {symbol} {'LONG' if side == 1 else 'SHORT'} qty={amount} "
                 f"notional={target_notional:.2f} entry={coin.get('lastPrice')} "
                 f"sl={sl_pct:.4f} tp={tp_pct:.4f} lev={dyn_lev}")
    er = hb.create_executor(symbol, amount, dyn_lev, sl_pct, tp_pct, TIME_LIMIT_SEC, side=side)
    eid = er.get("executor_id")
    if not eid:
        if reserver is not None and _res_id is not None:
            if _reservation_should_keep(er):
                log.warning(f"  🔒 rezervasyon KORUNUYOR (id={_res_id}): UNKNOWN sonuç, reconcile çözecek")
            else:
                reserver.release(_res_id, reason="executor_not_created")
                log.info(f"  🔓 rezervasyon serbest (id={_res_id}): executor oluşmadı")
        # FAZ14f: kimlik/konfig hatası sembolün suçu değil — karalisteye YAZMA.
        if _is_hb_auth_error(er):
            global _HB_AUTH_BROKEN
            _HB_AUTH_BROKEN = True
            log.critical(f"  🚨 Hummingbot kimlik doğrulaması başarısız: {er} — sembol karalisteye "
                         f"YAZILMADI; bu süreçte yeni emir denemesi durduruldu")
            return False
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
    # FAZ9 live: kademe limitleri RiskEngine'e enjekte edilir. Compare'da cfg.is_live False → RISK_CONFIG aynen kalır.
    _cfg = _live_cfg()
    _risk_cfg = dict(RISK_CONFIG)
    _max_positions = MAX_OPEN_POSITIONS
    if _cfg.is_live:
        _risk_cfg.update(_cfg.risk_overrides())
        _max_positions = _cfg.max_open_positions
        log.info(f"🔴 LIVE stage={_cfg.stage} testnet={_cfg.testnet} family={_cfg.strategy_family} "
                 f"max_position_pct={_cfg.max_position_pct}% daily_loss_pct={_cfg.daily_loss_pct}% "
                 f"max_lev={_cfg.max_leverage} max_pos={_max_positions} base={_cfg.base_url}")
        log.info(f"[LIVE] daily_loss_limit={_cfg.daily_loss_pct:.2f}% of day-start equity "
                 f"(equity {_cfg.max_position_pct:.0f}% per position, no daily trade count limit)")
    risk = RiskEngine(_risk_cfg)
    reserver = PositionReserver(risk.db_file, _max_positions)
    try:
        reserver.init_schema()
    except Exception as e:
        log.error(f"rezervasyon şeması init hatası: {e}")
    bk = os.environ.get("BINANCE_KEY")
    bs = os.environ.get("BINANCE_SECRET")
    # FAZ14f: kimlik bilgileri env'den gelir; gömülü varsayılan YOK. Canlıda eksikse
    # fail-closed (exit 1) — yanlış varsayılanla Hummingbot'a gidip 401 almak yerine
    # hiç başlamamak doğrudur. HB_OFFLINE=1 iken HB API hiç çağrılmaz, boş olabilir.
    hu = (os.environ.get("HB_USERNAME") or "").strip()
    hp = (os.environ.get("HB_PASSWORD") or "").strip()
    if not bk or not bs:
        log.error("BINANCE_KEY/SECRET yok")
        return
    if not HB_OFFLINE and (not hu or not hp):
        global _HB_AUTH_BROKEN
        _HB_AUTH_BROKEN = True
        log.critical("[LIVE] HB_USERNAME/HB_PASSWORD yok — Hummingbot API kimlik dogrulamasi "
                     "yapilamaz, emir yolu kapatildi (fail-closed). Servis ayakta kalir, emir gitmez.")
        return

    binance = BinanceAPI(bk, bs, base=_cfg.base_url)
    hb = HBAPI(HB_API, hu, hp)
    # FAZ9 live: tek seferlik açılış doğrulaması (bakiye + exchangeInfo). Başarısızsa süreç başlamaz.
    if _cfg.is_live and not _startup_checked[0]:
        try:
            import live_guard
            _sc = live_guard.startup_check(binance, _cfg)
            _startup_checked[0] = True
            log.info(f"[LIVE] startup check ok equity={_sc['balance_usdt']:.2f} symbols={_sc['symbols']} "
                     f"canTrade={_sc['can_trade']} testnet={_sc['testnet']}")
        except Exception as e:
            log.critical(f"[LIVE] startup check FAILED reason={e}")
            risk.is_halted = True
            risk.halt_reason = f"live startup check failed: {e}"
            risk._save_state()
            raise SystemExit(1)
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

    # Risk/pozisyon koruma kontrolleri tamamlandı.
    # Bekleyen araştırma sinyallerini aday listesinden bağımsız çöz.
    # Bu işlem yalnızca public kline verisi okur.
    _resolve_pending_signal_outcomes(binance)

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

    if _HB_AUTH_BROKEN:
        log.warning("Hummingbot kimlik doğrulaması bozuk — bu süreçte yeni emir denenmiyor "
                    "(live.env HB_USERNAME/HB_PASSWORD düzeltilip servis yeniden başlatılmalı)")
        return
    if HB_OFFLINE:
        active = set()  # HB_OFFLINE: HB'siz; aktif executor yok sayılır (kalibrasyon modu)
    else:
        try:
            active = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
        except Exception as e:
            log.error(f"aktif executor sayısı alınamadı, pozisyon limiti kontrolü ATLANDI: {e}")
            active = set()
    if not HB_OFFLINE and len(active) >= _effective_max_positions():
        log.info(f"Maks pozisyon limiti ({_effective_max_positions()}) dolu → yeni açmıyorum")
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
    _log_calibration_status()

    picks = scan_top(binance, max(TOP_N * 3, (TOP_N_COMPARE * 2) if WHALE_HUNTING == "compare" else 0),
                     blacklist=set(bl.keys()))
    if _SCAN_FUNNEL:
        log.info(f"  🔎 Tarama hunisi: ticker={_SCAN_FUNNEL['tickers']} usdt={_SCAN_FUNNEL['usdt']} "
                 f"blacklist={_SCAN_FUNNEL['blacklisted']} hacim≥{MIN_VOLUME_USDT:,.0f}={_SCAN_FUNNEL['volume']} "
                 f"değişim {MIN_PCT_CHANGE:.1f}-{MAX_PCT_CHANGE:.1f}%={_SCAN_FUNNEL['pct']} "
                 f"picks={_SCAN_FUNNEL['picks']}")

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

    ok, n_main, n_probe = process_candidates(binance, hb, picks, active, bl, dry_run, risk, reserver, coordinator)
    if n_main == 0:
        return
    log.info(f"Bitti: {ok}/{n_main}" + (f" (+{n_probe} compare probe)" if n_probe else ""))


def process_candidates(binance, hb, picks, active, bl, dry_run, risk, reserver, coordinator):
    """Ana adaylar open_coin ile (HB_OFFLINE'da dry-run), WHALE_HUNTING=compare iken ek adaylar compare_probe ile
    (yalnız log). Döner (başarılı_open_coin, ana_sayısı, probe_sayısı)."""
    total_n = TOP_N_COMPARE if WHALE_HUNTING == "compare" else TOP_N
    candidates, probes = select_candidates(picks, active, bl, TOP_N, total_n)
    if not candidates:
        log.info("yeni aday yok")
        return 0, 0, 0
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
        if not HB_OFFLINE and len(_cur_active) >= _effective_max_positions():
            log.info(f"🚫 C6: limit doldu ({len(_cur_active)}/{_effective_max_positions()}), döngüden çıkılıyor")
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
    n_probe = 0
    for c in probes:
        try:
            if compare_probe(binance, c, risk=risk):
                n_probe += 1
        except Exception as e:                       # probe asla taramayı/emir akışını etkilemez
            log.warning(f"  🔎 probe {c.get('symbol')} hatası (yok sayıldı): {e}")
    return ok, len(candidates), n_probe


def main():
    log_file = Path.home() / "auto_trader.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)]
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
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
