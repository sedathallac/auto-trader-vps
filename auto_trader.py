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

    def open_orders(self, symbol):
        """GET /fapi/v1/openOrders — REGULAR (non-algo) open orders for a symbol
        (e.g. a resting take_profit LIMIT, or an in-flight / partially filled
        close). Returns the raw response (list on success, error dict otherwise)
        so the caller can fail closed on an error. Used to detect a pre-sent
        _close_order / _take_profit_limit_order before the close handoff."""
        return self._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def cancel_order(self, symbol, order_id):
        """DELETE /fapi/v1/order — cancel a REGULAR order by exchange orderId."""
        return self._post("/fapi/v1/order", {"symbol": symbol, "orderId": order_id},
                          method="DELETE")

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
    authority; TP and time-limit become CloseCoordinator responsibilities
    (CloseCoordinator.request_take_profit / request_time_limit).
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
        sole SL authority and TP/time-limit route through the CloseCoordinator."""
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


def ensure_sl_coverage(binance):
    try:
        positions = binance.position_risk()
        if not isinstance(positions, list):
            log.warning(f"positionRisk hata: {positions}")
            return
        open_pos = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0}
        if not open_pos:
            log.info("SL kontrolü: açık pozisyon yok")
            return
        covered = set()
        for sym in open_pos:
            covered |= {o["symbol"] for o in binance.open_algo_orders_for(sym)}
        missing = [s for s in open_pos if s not in covered]
        if not missing:
            log.info(f"✅ SL koruması tam ({len(open_pos)} pozisyon)")
            return
        log.warning(f"⚠️  SL eksik: {missing}")
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
                r = binance.place_algo_sl(sym, trig, qty, side)
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


def open_coin(binance, hb, coin, dry_run=False, risk=None, reserver=None):
    # C6-FIX: Fonksiyon başında defansif fail-closed limit kontrolü
    try:
        _cur = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
    except Exception as e:
        log.critical(f"🚨 C6: open_coin executor sayısı alınamadı, fail-closed: {e}")
        return False
    if len(_cur) >= MAX_OPEN_POSITIONS:
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
    if RISK_CONFIG.get("risk_guards_enabled", False):
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
        add_blacklist(symbol, "dolmadı")
        return False
    log.info(f"  ✅ doldu: {filled:.2f} USDT")

    tick = float(filt["PRICE_FILTER"]["tickSize"])
    if side == 1:
        trig = round(round((price * (1 - sl_pct)) / tick) * tick, 8)
    else:
        trig = round(round((price * (1 + sl_pct)) / tick) * tick, 8)
    sr = binance.place_algo_sl(symbol, trig, amount, side)
    algo_id = sr.get("algoId") if isinstance(sr, dict) else None
    
    sl_verified = False
    if algo_id:
        log.info(f"  ⏳ SL emri gonderildi (algoId={algo_id}), dogrulaniyor...")
        # Retry mekanizması: 3 deneme, her biri 3 saniye arayla
        for attempt in range(3):
            time.sleep(3)
            try:
                open_orders = binance.open_algo_orders_for(symbol)
                for o in open_orders:
                    if str(o.get("algoId")) == str(algo_id):
                        # Miktar alanı için birden fazla olası anahtarı kontrol et
                        o_qty = float(o.get("quantity") or o.get("origQty") or o.get("qty") or 0)
                        if o_qty >= float(amount) * 0.95:
                            sl_verified = True
                            break
                if sl_verified:
                    break
                log.warning(f"  SL dogrulama denemesi {attempt+1}/3 basarisiz, tekrar deneniyor...")
            except Exception as e:
                log.error(f"  SL dogrulama hatasi (API): {e}")

    if not sl_verified:
        log.critical(f"  🚨 SL DOGRULANAMADI! (algoId={algo_id}). Pozisyon acil KAPATILIYOR!")
        close_side = "SELL" if side == 1 else "BUY"
        try:
            # Market emri ile pozisyonu kapat (reduceOnly)
            binance._post("/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "MARKET",
                "quantity": str(amount), "reduceOnly": "true"
            })
            log.info("  ✅ Pozisyon piyasa emriyle kapatildi.")
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
                            close_side = "SELL" if side == 1 else "BUY"
                            binance._post("/fapi/v1/order", {
                                "symbol": symbol, "side": close_side, "type": "MARKET",
                                "quantity": str(amount), "reduceOnly": "true"
                            })
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

    try:
        active = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
    except Exception as e:
        log.error(f"aktif executor sayısı alınamadı, pozisyon limiti kontrolü ATLANDI: {e}")
        active = set()
    if len(active) >= MAX_OPEN_POSITIONS:
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
        try:
            _cur_active = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
        except Exception as e:
            log.critical(f"🚨 C6: döngü içi executor sayısı alınamadı, fail-closed: {e}")
            break
        if len(_cur_active) >= MAX_OPEN_POSITIONS:
            log.info(f"🚫 C6: limit doldu ({len(_cur_active)}/{MAX_OPEN_POSITIONS}), döngüden çıkılıyor")
            break
        try:
            if open_coin(binance, hb, c, dry_run=dry_run, risk=risk, reserver=reserver):
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
