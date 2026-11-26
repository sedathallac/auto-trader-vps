"""canary_live.py — BLOK28.1: Futures LIVE Canary runtime (RC — EMİR GÖNDERMEZ
bu görevde; canlı başlatma ayrı operatör onayı).

Mimari (görev §0-§7):
  - İKİ bağımsız döngü: strategy_loop (300s, H1 sinyal) + risk_loop (yeni
    Mark Price güncellemesi başına, en fazla 5s kadans; 300s'i BEKLEMEZ).
  - Tekil worker: flock dosya kilidi + DB lease (aynı DB'de ikinci instance
    başlamayı reddeder).
  - Pozisyon bazlı risk çıkışı: -5 USDT → EXIT_PENDING → reduce-only MARKET
    çıkış → positionRisk ile FLAT doğrulaması → ancak sonra CLOSED.
    H1 flip BEKLENMEZ (§2).
  - Toplam -10 USDT → kalıcı HALT state machine:
    CANARY_ACTIVE → CANARY_HALT_INTENT → CANARY_RISK_HALTED →
    CANARY_EXITING_ALL → CANARY_EXIT_RECONCILIATION → CANARY_FLAT_VERIFIED →
    CANARY_HALT_LOCKED (yalnız operatör).
  - UNKNOWN emir (POST timeout): körlemesine tekrar gönderilmez; client id ile
    sorgulanır, positionRisk/açık emirlerle çözülür (§2.3).
  - Borsa tarafı koruma: her pozisyon için STOP_MARKET CONDITIONAL
    (closePosition=true, workingType=MARK_PRICE, triggerPrice=-5 USDT bütçesinden);
    borsada DOĞRULANANA dek pozisyon PROTECTED sayılmaz; koruma kurulamazsa
    giriş yok + reduce-only çıkış (§5).
  - Likidasyon: LiquidationModel (gerçek leverageBracket) doğrulaması olmadan
    giriş yok (§6) → LIQUIDATION_RISK_UNVERIFIED.
  - Restart: önce exchange mutabakatı → sahiplik → risk DB → en son giriş
    değerlendirmesi (§4.2).
  - F-a…F-g eşdeğerleri: çıkış sonrası artık koruma emri çözümü (F-a), flat
    doğrulamadan CLOSED yazılmaz (F-b), doğrulanamayan koruma kapsamı girişi
    bloklar (F-c), kesinti 'pozisyon yok' OKUNMAZ (F-d), -2021 sınıflandırılır
    (F-e), koruma/çıkış taze positionRisk okur (F-g).


BLOK32 (2026-11-25, operatör strateji kararı): giriş politikası
QUALITY_BIDIRECTIONAL — çift yönlü LONG/SHORT quality-based
target-following. Girişler yalnızca momentum flip'ine bağlı DEĞİL: her YENİ
kapalı mumpda mevcut hedef yön (engine target) bağımsız LONG/SHORT kalite
skoruyla doğrulanır (canary_quality.py — kalibre edilmiş eşikler, hysteresis
episode makinesi, fail-closed bileşen hesabı). Kalite eşiği geçilmedikçe
emir YOK; flip tek başına giriş hakkı VERMEZ (flip tüketilir + görünür
QUALITY_INSUFFICIENT_FLIP kaydı). Reversal (One-way): ters sinyalde ÖNCE
reduce-only çıkış → borsada flat doğrulama → ancak sonraki barlarda yeni
yön değerlendirmesi; aynı sembolde ikinci pozisyon ASLA açılmaz.
FLIP_ONLY politikası (kanonik araştırma davranışı) entry_policy bayrağıyla
korunur — legacy testler ve flip-only semantiği değişmez.

Operatör limitleri canary_risk.RiskManager'da merkezî ve değiştirilemez.
"""
import contextlib
import fcntl
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from canary_risk import (RiskManager, OrderRejected, MAX_OPEN_POSITIONS,
                         MAX_POSITION_NOTIONAL, MAX_TOTAL_NOTIONAL,
                         MAX_POSITION_NET_LOSS, MAX_CANARY_NET_LOSS,
                         MARK_STALE_S, STATUS_HALTED)
from canary_exchange import (ExchangeUnavailable, UnknownResult,
                             BinanceApiError)
from canary_liquidation import (LiquidationModel, LIQUIDATION_RISK_UNVERIFIED,
                                round_to_tick)
from h1_adapter import H1SignalEngine
from canary_brain_adapter import (SIGNAL_SOURCE_H1, SIGNAL_SOURCE_BRAIN,
                                  BrainDecisionSource)
import canary_quality as cq

# LEGACY 8 sembollük sabit evren — yalnız FLIP_ONLY/legacy testler ve
# reference kayıt için durur. Üretim (BRAIN_PRIMARY) evreni canary_runner
# beynin eligible contract discovery çıktısından kurar (görev §1: eski
# liste yeni işlem evreninin belirleyicisi DEĞİLDİR).
SYMBOLS = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "BNBUSDT", "DOGEUSDT",
           "LINKUSDT", "SOLUSDT", "XRPUSDT"]
# BLOK29.1 (2026-11-25): hedef notional TEK MERKEZİ KAYNAK'TAN türetilir —
# canary_risk.MAX_POSITION_NOTIONAL (100 USDT). Burada ayrı sayı YOKTUR;
# operatör limiti değişirse yalnızca canary_risk.py güncellenir.
CANARY_NOTIONAL = MAX_POSITION_NOTIONAL

# ---- PROVIZYONEL politika parametreleri (operatör onayı bekliyor — §14) ----
MARK_STALE_AFTER_SECONDS = MARK_STALE_S          # 30s (BLOK28.0'dan devralınan)
STRATEGY_LOOP_S = 300.0                           # H1 sinyal döngüsü
RISK_LOOP_S = 5.0                                 # mark örnekleme temposu

BAR_MS = 4 * 3600 * 1000
WARMUP_BARS = 400

# BLOK31 (2026-11-25): giriş ANI fiyat/boyutlama güvenliği — eski bar
# açılış fiyatıyla saatler sonra MARKET emir gönderme hatasının düzeltmesi.
# NOTIONAL_SLIPPAGE_TOL KALDIRILDI: fill sonrası %1 tolerans, 100 USDT
# operatör hard cap'ini fiilen 101'e çıkarıyordu; artık hard cap = 100
# (ihlal → reduce-only çıkış + açık ihlal kaydı) ve qty, slippage payıyla
# AŞAĞI boyutlanır (ENTRY_SIZE_BUFFER).
PRICE_STALE_MS = 30_000             # bookTicker güncelliği (MARK_STALE_S hizalı)
SIGNAL_PRICE_DEVIATION_MAX = 0.02   # taze icra fiyatı ↔ sinyal kapanışı sapması
ENTRY_SIZE_BUFFER = 0.01            # MARKET slippage payı — qty aşağı boyutlanır

# HALT state machine (§3)
HALT_ACTIVE = "CANARY_ACTIVE"
HALT_INTENT = "CANARY_HALT_INTENT"
HALT_RISK = "CANARY_RISK_HALTED"
HALT_EXITING_ALL = "CANARY_EXITING_ALL"
HALT_RECON = "CANARY_EXIT_RECONCILIATION"
HALT_FLAT_VERIFIED = "CANARY_FLAT_VERIFIED"
HALT_LOCKED = "CANARY_HALT_LOCKED"
HALT_STATES = (HALT_ACTIVE, HALT_INTENT, HALT_RISK, HALT_EXITING_ALL,
               HALT_RECON, HALT_FLAT_VERIFIED, HALT_LOCKED)

CLIENT_PREFIX = "C281"               # sahiplik kanıtı: clientOrderId öneki

# emir durumları (live_orders.status)
O_INTENT = "INTENT"                  # kaydedildi, gönderilmedı
O_SUBMITTED = "SUBMITTED"
O_UNKNOWN = "UNKNOWN"                # POST timeout — §2.3
O_FILLED = "FILLED"
O_PARTIAL = "PARTIALLY_FILLED"
O_CANCELED = "CANCELED"
O_REJECTED = "REJECTED"
O_TRIGGERED = "TRIGGERED"            # algo koruma tetiklendi

# live_positions.protection_status
P_PENDING = "PROTECTION_PENDING"
P_VERIFIED = "PROTECTED"
P_FAILED = "PROTECTION_FAILED_EXIT_PENDING"
P_TRIGGERED = "PROTECTION_TRIGGERED"
P_CANCELED = "PROTECTION_CANCELED"

# live_positions.exit_status
X_NONE = "NONE"
X_PENDING = "EXIT_PENDING"
X_UNKNOWN = "EXIT_UNKNOWN"
X_PARTIAL = "EXIT_PARTIAL"
X_UNCONFIRMED = "EXIT_PENDING_UNCONFIRMED"   # kesinti — flat iddia EDİLMEZ
X_CONFIRMED = "EXIT_CONFIRMED"

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_orders (
    client_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- ENTRY / EXIT / PROTECTION
    symbol TEXT NOT NULL,
    pos_id INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    exchange_id TEXT,
    status TEXT NOT NULL,
    params TEXT,
    result TEXT,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS live_positions (
    pos_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    protection_client_id TEXT,
    protection_status TEXT NOT NULL DEFAULT 'PROTECTION_PENDING',
    exit_client_id TEXT,
    exit_status TEXT NOT NULL DEFAULT 'NONE',
    exit_reason TEXT,
    updated_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS live_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class WorkerLockHeld(Exception):
    """Tekil risk worker kilidi başkasında (§4.2)."""


class EntryGateBusy(Exception):
    """Ortak giriş-kapısı kilidi (flock) timeout içinde alınamadı —
    fail-closed: emir GÖNDERİLMEZ (BLOK30 §4)."""


# ============================================================================
# BLOK30 — KALICI ENTRY PAUSE (operatör kontrolü; HALT'tan TAMAMEN BAĞIMSIZ)
#
#   entry_paused=0 → yeni girişler mevcut TÜM güvenlik kontrollerine tabidir
#   entry_paused=1 → yeni giriş emirleri engellenir; risk döngüsü, SAFE_EXIT,
#                    -5/-10 limitleri, HALT state machine, koruma doğrulama,
#                    strateji ÇIKIŞLARI ve mevcut pozisyon yönetimi ÇALIŞIR
#
# Durum live_state tablosunda kalıcıdır (restart sıfırlayamaz); yalnızca
# operatör aracı (canary_control.py resume) kaldırabilir. HALT mekanizmasına
# (halt_state / rm.status) DOKUNULMAZ — PAUSE global HALT'ı bastırmaz.
#
# Eşzamanlılık (§4): uygulama süreci ile CLI süreci arasındaki TEK
# senkronizasyon noktası, DB ile aynı dizinde tutulan ortak OS kilidi
# `<db>.entry-gate.lock` (fcntl.flock)'tir. Giriş zinciri bu kilidi INTENT
# kaydı + new_order() + sonuç yazımı boyunca TUTAR; operatör pause/resume
# yazımı da AYNI kilidi almadan durum değiştirMEZ. Böylece "kontrol sonrası
# emir öncesi PAUSE açıldı" yarış penceresi kapanır. Python threading
# kilidi ayrı süreci serileştiremeyeceği için flock ZORUNLUDUR.
# Kilit sıralaması: entry chain → _entry_lock (thread) → entry-gate (flock)
# → sqlite yazımı; pause/resume → entry-gate (flock) → sqlite yazımı.
# Deadlock yok: flock hiçbir zaman thread kilitleri bekleyen bir yolda
# tutulmaz; risk döngüsü ve çıkışlar bu kilide DOKUNMAZ.
# ============================================================================
ENTRY_PAUSE_KEY = "entry_paused"

# BLOK32 görev-2: yön başına (LONG/SHORT) bağımsız canlı giriş izni.
# Kalıcı live_state anahtarları; değer TAM OLARAK "1" ise o yönün canlı
# girişi AÇIK. Eksik, "0", hatalı veya bilinmeyen her değer KAPALI
# (fail-closed — hiçbir ayar hatası girişi AÇAMAZ). Varsayılan: KAPALI.
# Yalnız QUALITY_BIDIRECTIONAL politikasına uygulanır; FLIP_ONLY kanonik
# araştırma davranışı ve legacy regresyonları DEĞİŞMEZ.
DIR_ENABLED_KEY_LONG = "dir_enabled_long"
DIR_ENABLED_KEY_SHORT = "dir_enabled_short"
DIRECTION_DISABLED_LONG = "DIRECTION_DISABLED_LONG"
DIRECTION_DISABLED_SHORT = "DIRECTION_DISABLED_SHORT"


def _dir_enabled_key(direction):
    return DIR_ENABLED_KEY_LONG if direction > 0 else DIR_ENABLED_KEY_SHORT


def _direction_disabled_label(direction):
    return DIRECTION_DISABLED_LONG if direction > 0 else DIRECTION_DISABLED_SHORT


def set_direction_enabled(db_path, direction, enabled, reason, by,
                          timeout_s=120.0):
    """Operatör yön-izni yazımı — giriş kapısı kilidi ALTINDA
    (set_entry_pause deseni). enabled=True yalnız "1", False yalnız "0"
    yazar; başka değer ASLA yazılmaz. Yalnız dir_enabled_* anahtarlarını
    ve audit kaydını yazar; halt_state, rm durumu, kalite eşikleri,
    pozisyonlar ve koruma emirlerine DOKUNMAZ."""
    key = _dir_enabled_key(direction)
    action = "DIRECTION_ENABLED" if enabled else "DIRECTION_DISABLED"
    with entry_gate(db_path, timeout_s=timeout_s):
        db = sqlite3.connect(str(db_path), timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS live_state ("
                       "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS risk_events ("
                       "ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT)")
            now = int(time.time() * 1000)
            row = db.execute("SELECT value FROM live_state WHERE key=?",
                             (key,)).fetchone()
            prev = row[0] if row else None
            db.execute("INSERT INTO live_state VALUES (?,?) ON CONFLICT"
                        "(key) DO UPDATE SET value=excluded.value",
                        (key, "1" if enabled else "0"))
            db.execute("INSERT INTO risk_events VALUES (?,?,?)",
                       (now, action,
                        f"dir={direction} by={by} reason={reason}"))
            db.commit()
        finally:
            db.close()
    return {"action": action, "direction": direction, "key": key,
            "enabled": bool(enabled), "previous": prev, "ts": now,
            "by": str(by), "reason": str(reason or "")}


def entry_gate_lock_path(db_path):
    """Ortak giriş-kapısı kilit dosyası (DB ile aynı dizin)."""
    return str(db_path) + ".entry-gate.lock"


@contextlib.contextmanager
def entry_gate(db_path, timeout_s=None):
    """Giriş kapısı kilidi (flock). timeout_s=None → süresiz bekleme;
    aksi halde deadline dolarsa EntryGateBusy (çağıran fail-closed davranır).
    Aynı süreçte farklı fd'lerden alınan flock'ler birbirini ENGELLER —
    süreç içi pause/entry yarışı da aynı kapıdan geçer."""
    path = entry_gate_lock_path(db_path)
    f = open(path, "w")
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise EntryGateBusy(
                        f"entry-gate kilidi {timeout_s}s içinde alınamadı: "
                        f"{path}")
                time.sleep(0.02)
        try:
            yield f
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def set_entry_pause(db_path, paused, reason, by, timeout_s=120.0):
    """Operatör ENTRY PAUSE/RESUME yazımı — giriş kapısı kilidi ALTINDA.

    Devam eden bir giriş zinciri varsa (INTENT + new_order + sonuç yazımı)
    sonucunu BEKLER; kilit `timeout_s` içinde boşalmazsa EntryGateBusy
    verir ve durum DEĞİŞMEZ ( erken 'etkinleşti' bildirimi YOK).
    Sadece entry_paused* anahtarlarını yazar; halt_state, rm durumu,
    koruma emirleri ve pozisyonlara DOKUNMAZ."""
    action = "ENTRY_PAUSED" if paused else "ENTRY_RESUMED"
    with entry_gate(db_path, timeout_s=timeout_s):
        db = sqlite3.connect(str(db_path), timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS live_state ("
                       "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS risk_events ("
                       "ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT)")
            now = int(time.time() * 1000)
            row = db.execute("SELECT value FROM live_state WHERE key=?",
                             (ENTRY_PAUSE_KEY,)).fetchone()
            prev = row[0] if row else "0"
            for k, v in ((ENTRY_PAUSE_KEY, "1" if paused else "0"),
                         ("entry_pause_ts", str(now)),
                         ("entry_pause_by", str(by)),
                         ("entry_pause_reason", str(reason or "")),
                         ("entry_pause_last_action", action)):
                db.execute("INSERT INTO live_state VALUES (?,?) ON CONFLICT"
                            "(key) DO UPDATE SET value=excluded.value", (k, v))
            db.execute("INSERT INTO risk_events VALUES (?,?,?)",
                       (now, action, f"by={by} reason={reason}"))
            db.commit()
        finally:
            db.close()
    return {"action": action, "paused": bool(paused), "previous": prev,
            "ts": now, "by": str(by), "reason": str(reason or "")}


def read_entry_pause(db_path):
    """Salt-okunur PAUSE/HALT/pending-entry görünümü (status aracı).
    mode=ro ile açılır — hiçbir şey YAZMAZ, borsa API'si ÇAĞIRMAZ."""
    out = {"db": str(db_path), "exists": os.path.exists(str(db_path)),
           "entry_paused": False}
    if not out["exists"]:
        return out
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def get(k, table="live_state"):
            try:
                row = db.execute(f"SELECT value FROM {table} WHERE key=?",
                                 (k,)).fetchone()
                return row[0] if row else None
            except sqlite3.Error:
                return None
        out["entry_paused"] = (get(ENTRY_PAUSE_KEY) or "0") == "1"
        # BLOK32 görev-2: yön canlı giriş izinleri (salt-okunur görünüm).
        # Ham değer de gösterilir — hatalı ayar görünür/kayda geçer.
        out["dir_enabled_long_raw"] = get(DIR_ENABLED_KEY_LONG)
        out["dir_enabled_short_raw"] = get(DIR_ENABLED_KEY_SHORT)
        out["dir_enabled_long"] = (get(DIR_ENABLED_KEY_LONG) == "1")
        out["dir_enabled_short"] = (get(DIR_ENABLED_KEY_SHORT) == "1")
        out["entry_pause_ts"] = get("entry_pause_ts")
        out["entry_pause_by"] = get("entry_pause_by")
        out["entry_pause_reason"] = get("entry_pause_reason")
        out["entry_pause_last_action"] = get("entry_pause_last_action")
        out["halt_state"] = get("halt_state") or "CANARY_ACTIVE"
        out["rm_status"] = get("status", table="canary_state")
        out["worker_pid"] = get("worker_pid")
        try:
            rows = db.execute(
                "SELECT client_id, symbol, status, updated_ts FROM "
                "live_orders WHERE kind='ENTRY' AND status IN "
                "('INTENT','SUBMITTED','UNKNOWN') ORDER BY updated_ts"
            ).fetchall()
            out["pending_entries"] = [dict(zip(
                ("client_id", "symbol", "status", "updated_ts"), r))
                for r in rows]
        except sqlite3.Error:
            out["pending_entries"] = []
        return out
    finally:
        db.close()


class LiveCanary:
    """Mainnet canary release candidate. Tüm borsa erişimi `exchange`
    soyutlamasından (BinanceFuturesClient | FakeExchange) geçer."""

    def __init__(self, db_path, exchange, symbols=SYMBOLS, t0_ms=None,
                 filters=None, liq_model: Optional[LiquidationModel] = None,
                 clock_ms=None, log_dir=None, entry_policy=None,
                 signal_source=SIGNAL_SOURCE_H1,
                 brain_source: Optional[BrainDecisionSource] = None):
        self.db_path = str(db_path)
        self.dir = Path(log_dir or os.path.dirname(self.db_path))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.exchange = exchange
        self.symbols = list(symbols)
        self.t0_ms = int(t0_ms) if t0_ms is not None else 0
        self.filters = filters or {}       # {symbol: {tickSize, stepSize,...}}
        self.liq = liq_model or LiquidationModel()   # boş → UNVERIFIED
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.rm = RiskManager(self.db_path)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False,
                                  timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(LIVE_SCHEMA)
        self.db.commit()
        self.engine = H1SignalEngine(self.symbols)
        # BLOK32: giriş politikası. Üretim varsayılanı QUALITY_BIDIRECTIONAL
        # (operatör kararı 2026-11-25); FLIP_ONLY kanonik araştırma
        # davranışıdır (legacy testler bu bayrakla çalışır — davranışları
        # BLOK30/31/31.1 ile birebir aynı kalır).
        self.entry_policy = entry_policy or cq.ENTRY_POLICY_QUALITY
        if self.entry_policy not in (cq.ENTRY_POLICY_QUALITY,
                                     cq.ENTRY_POLICY_FLIP_ONLY):
            raise ValueError(f"bilinmeyen entry_policy: {entry_policy!r}")
        # GÖREV §1/§2 (2026-11-26): sinyal kaynağı. BRAIN_PRIMARY modunda
        # giriş adayları YALNIZ CANARY AI BRAIN'den (futures-brain.service
        # BRAIN_DB) gelir; eski 8 sembollük yerel H1 motoru giriş ÜRETEMEZ
        # (yalnız savunma-derinliği olarak kilitlenir — çıkış/risk/koruma
        # altyapısı aynen çalışır). H1_LOCAL: legacy davranış (testler).
        self.signal_source = signal_source
        if self.signal_source not in (SIGNAL_SOURCE_H1,
                                      SIGNAL_SOURCE_BRAIN):
            raise ValueError(f"bilinmeyen signal_source: {signal_source!r}")
        if self.signal_source == SIGNAL_SOURCE_BRAIN and \
                brain_source is None:
            raise ValueError("BRAIN_PRIMARY modu brain_source ister "
                             "(BrainDecisionSource)")
        self.brain_source = brain_source
        # kalite modeli: engine durumunu OKUR (closes/var), kendi hacim/
        # sigma penceresini canlı + warmup replay barlarıyla besler
        self.quality = cq.QualityModel(self.engine)
        self._lock = threading.RLock()          # canaryLive tabloları
        self._entry_lock = threading.RLock()    # giriş zinciri serileştirme
        self._lockfile = None
        self._stop = threading.Event()
        self._last_bar = {}
        for s in self.symbols:
            v = self._get_state(f"last_bar:{s}")
            if v is not None:
                self._last_bar[s] = int(v)
        self._warmed = set()
        # BRAIN_PRIMARY: son beyin döngüsü değerlendirme özeti (izleme;
        # STRATEGY_CYCLE kaydına girer — iki servisin aynı kararı
        # kullandığının kanıt kaynağı)
        self._last_brain_tick = None
        # restart güvenliği (§4.2): mutabakat BU SÜREÇ tarafından yeniden
        # kanıtlanmalı — önceki sürecin reconciled_ok değeri miras ALINMAZ
        self._set_state("reconciled_ok", 0)
        self._set_state("feed_recovered_pending_reconcile", 0)
        if self._get_state("halt_state") is None:
            self._set_state("halt_state", HALT_ACTIVE)

    # ---- düşük seviye durum ------------------------------------------------
    def _get_state(self, key):
        with self._lock:
            row = self._db_execute("SELECT value FROM live_state WHERE key=?",
                                  (key,)).fetchone()
            return row[0] if row else None

    def _set_state(self, key, value):
        with self._lock:
            self._db_execute("INSERT INTO live_state VALUES (?,?) ON CONFLICT"
                            "(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))
            self.db.commit()

    def _now(self):
        return self._clock_ms()

    def _log(self, name, obj):
        try:
            with open(self.dir / name, "a") as f:
                f.write(json.dumps(obj, default=str) + "\n")
        except OSError:
            pass

    @property
    def halt_state(self):
        return self._get_state("halt_state") or HALT_ACTIVE

    @property
    def entry_paused(self):
        """BLOK30: kalıcı operatör ENTRY PAUSE bayrağı. Anahtar yoksa
        varsayılan 0'dır; constructor/startup/restart ASLA sıfırlamaz —
        yalnızca operatör aracı (resume) kaldırır."""
        return str(self._get_state(ENTRY_PAUSE_KEY) or "0") == "1"

    def _dir_enabled(self, direction):
        """BLOK32 görev-2: yön canlı giriş izni (fail-closed). Kalıcı
        dir_enabled_* anahtarının değeri TAM OLARAK "1" ise izin VAR;
        eksik, "0", hatalı veya bilinmeyen her değer KAPALI'dır — hiçbir
        durumda girişi AÇMAZ. Varsayılan (anahtar yok): KAPALI.
        FLIP_ONLY politikasına uygulanmaz (kanonik araştırma davranışı ve
        legacy regresyonlar değişmez); üretim politikası
        QUALITY_BIDIRECTIONAL'da zorunludur."""
        if self.entry_policy != cq.ENTRY_POLICY_QUALITY:
            return True
        return self._get_state(_dir_enabled_key(direction)) == "1"

    # ---- BLOK32: kalıcı kalite episode durum makinesi -----------------------
    # qep:{symbol}:{direction} = ARMED | ENGAGED (live_state tablosunda —
    # restart/süreç ölümü durumunda DA kaybolmaz; §6 "Process belleğinde
    # kaybolan state canlı girişin tekrarına yol açmamalı").
    # ARMED   → skor θ_d'yi geçince giriş adayı doğabilir
    # ENGAGED → bu episode'da emir ÇIKTI; yeniden silahlanma YALNIZ skor
    #           θ_d − h_d altına düştüğünde (hysteresis — eşik kenarı
    #           gir-çık davranışı engellenir). Çıkış episode'u yeniden
    #           silahlamaz: aynı sinyalde otomatik yeniden giriş YOK.
    def _episode_engaged(self, symbol, direction):
        return self._get_state(f"qep:{symbol}:{direction}") == "ENGAGED"

    def _episode_set(self, symbol, direction, engaged):
        self._set_state(f"qep:{symbol}:{direction}",
                        "ENGAGED" if engaged else "ARMED")

    def _transition_halt(self, new_state, detail=""):
        """Atomik (BEGIN IMMEDIATE) ve kalıcı HALT geçişi (§3 adım 1)."""
        with self._lock:
            self.db.commit()
            self._db_execute("BEGIN IMMEDIATE")
            try:
                cur = self._db_execute(
                    "SELECT value FROM live_state WHERE key='halt_state'"
                ).fetchone()
                old = cur[0] if cur else HALT_ACTIVE
                self._db_execute(
                    "INSERT INTO live_state VALUES ('halt_state',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (new_state,))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.rm._event("HALT_TRANSITION", f"{old} -> {new_state} {detail}")

    # ---- tekil worker kilidi (§4.2) ------------------------------------------
    def acquire_worker_lock(self):
        lockpath = self.db_path + ".worker.lock"
        f = open(lockpath, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise WorkerLockHeld(
                f"tekil risk worker kilidi tutuluyor: {lockpath}")
        f.write(f"{os.getpid()}\n")
        f.flush()
        self._lockfile = f
        self._set_state("worker_pid", os.getpid())
        self._set_state("worker_lease_ts", self._now())

    def release_worker_lock(self):
        """İdempotent + thread-safe (run() finally ve dışarıdan stop yarışı)."""
        with self._lock:
            f = self._lockfile
            self._lockfile = None
            if f is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                finally:
                    f.close()

    # ---- emir kayıtları -------------------------------------------------------
    def _db_execute(self, sql, params=()):
        """Tüm live_orders/live_positions/live_state erişimi bu kilitten
        geçer (risk thread + strategy thread aynı sqlite bağlantısını
        paylaşır — eşzamanlı kullanım InterfaceError üretir)."""
        with self._lock:
            return self.db.execute(sql, params)

    def _order_row(self, client_id):
        row = self._db_execute(
            "SELECT * FROM live_orders WHERE client_id=?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        keys = ["client_id", "kind", "symbol", "pos_id", "attempt",
                "exchange_id", "status", "params", "result", "created_ts",
                "updated_ts"]
        return dict(zip(keys, row))

    def _upsert_order(self, client_id, kind, symbol, status, pos_id=None,
                      attempt=1, params=None, result=None, exchange_id=None):
        now = self._now()
        self._db_execute(
            "INSERT INTO live_orders (client_id, kind, symbol, pos_id, "
            "attempt, exchange_id, status, params, result, created_ts, "
            "updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET status=excluded.status, "
            "exchange_id=COALESCE(excluded.exchange_id, exchange_id), "
            "result=COALESCE(excluded.result, result), "
            "pos_id=COALESCE(excluded.pos_id, pos_id), "
            "updated_ts=excluded.updated_ts",
            (client_id, kind, symbol, pos_id, attempt, exchange_id, status,
             json.dumps(params or {}), json.dumps(result or {}), now, now))
        self.db.commit()

    def _live_pos_row(self, pos_id):
        row = self._db_execute(
            "SELECT * FROM live_positions WHERE pos_id=?",
            (pos_id,)).fetchone()
        if row is None:
            return None
        keys = ["pos_id", "symbol", "protection_client_id",
                "protection_status", "exit_client_id", "exit_status",
                "exit_reason", "updated_ts"]
        return dict(zip(keys, row))

    def _update_live_pos(self, pos_id, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._db_execute(f"UPDATE live_positions SET {sets}, "
                         f"updated_ts=? WHERE pos_id=?",
                         (*fields.values(), self._now(), pos_id))
        self.db.commit()

    # ---- borsa okuma yardımcıları ----------------------------------------------
    def _position_risk_map(self):
        """positionRisk → {symbol: row}. Hata → ExchangeUnavailable yükselir
        (çağıran fail-closed davranır — kesinti ASLA 'pozisyon yok' olmaz)."""
        rows = self.exchange.position_risk()
        out = {}
        for r in rows:
            sym = r.get("symbol")
            if sym in self.symbols:
                out[sym] = r
        return out

    def _exchange_amt(self, symbol, pr_map=None):
        """Borsadaki gerçek pozisyon miktarı (positionRisk). pr_map verilmediyse
        kendisi çeker (hata → ExchangeUnavailable yükselir)."""
        if pr_map is None:
            pr_map = self._position_risk_map()
        pr = pr_map.get(symbol)
        if pr is None:
            return None
        return float(pr.get("positionAmt", 0) or 0)

    # =========================================================================
    # STARTUP MUTABAKATI (§4.2: restart → önce mutabakat, en son girişler)
    # =========================================================================
    def startup_reconcile(self):
        now = self._now()
        rec = {"t": now, "kind": "STARTUP_RECONCILE"}
        try:
            pr_map = self._position_risk_map()
            open_orders = self.exchange.open_orders()
            try:
                open_algos = self.exchange.open_algo_orders()
            except ExchangeUnavailable:
                open_algos = []
                rec["algo_orders_unreadable"] = True
        except ExchangeUnavailable as e:
            rec["result"] = "FAIL_CLOSED"
            rec["error"] = str(e)
            self._set_state("reconciled_ok", 0)
            self._log("live_cycles.jsonl", rec)
            self.rm._event("RECONCILE_FAILED", str(e))
            return rec

        our_cids = {o.get("clientOrderId") for o in open_orders
                    if (o.get("clientOrderId") or "").startswith(CLIENT_PREFIX)}
        our_algos = {o.get("clientAlgoId") for o in open_algos
                     if (o.get("clientAlgoId") or "").startswith(CLIENT_PREFIX)}
        unowned_pos, unowned_ord = [], []

        # 1) borsa pozisyonları — sahiplik (§3: kanıt yoksa DOKUNMA)
        for sym, row in pr_map.items():
            if abs(float(row.get("positionAmt", 0) or 0)) < 1e-12:
                continue
            ours = [p for p in self.rm.open_positions() if p["symbol"] == sym]
            if not ours:
                unowned_pos.append(sym)          # canary dışı — dokunma
        # 2) bizim defterdeki açık pozisyonlar borsada var mı
        state_unknown = []
        for pos in self.rm.open_positions():
            sym = pos["symbol"]
            amt = self._exchange_amt(sym, pr_map)
            if amt is None:
                state_unknown.append(sym)
            elif abs(amt) < 1e-12:
                # borsa flat, defter açık → çöz (koruma tetiklenmiş olabilir)
                resolved = self._resolve_unexpected_flat(pos, pr_map)
                if not resolved:
                    state_unknown.append(sym)
            elif pos["direction"] * pos["units"] != amt:
                rec.setdefault("drift", []).append(
                    {"symbol": sym, "ledger": pos["direction"] * pos["units"],
                     "exchange": amt})
        # 3) bizim INTENT/UNKNOWN emirler → çöz
        for row in self._db_execute(
                "SELECT client_id, kind, symbol, status FROM live_orders "
                "WHERE status IN ('INTENT','SUBMITTED','UNKNOWN',"
                "'PARTIALLY_FILLED')").fetchall():
            cid, kind, sym, status = row
            if kind == "ENTRY" and status in ("UNKNOWN", "SUBMITTED",
                                              "PARTIALLY_FILLED"):
                self._resolve_entry_order(cid, sym)
        # 4) sahipliği kanıtlanamayan açık emirler
        for o in open_orders:
            cid = o.get("clientOrderId") or ""
            if not cid.startswith(CLIENT_PREFIX):
                unowned_ord.append({o.get("symbol"): cid})
        for o in open_algos:
            cid = o.get("clientAlgoId") or ""
            if not cid.startswith(CLIENT_PREFIX):
                unowned_ord.append({o.get("symbol"): f"algo:{cid}"})

        ok = not state_unknown and not unowned_pos
        self._set_state("reconciled_ok", 1 if ok else 0)
        self._set_state("unowned_position_seen", 1 if unowned_pos else 0)
        self._set_state("position_state_unknown", 1 if state_unknown else 0)
        self._set_state("reconciled_ts", now)
        rec.update({"result": "OK" if ok else "FLAGS",
                    "unowned_positions": unowned_pos,
                    "unowned_orders": unowned_ord,
                    "position_state_unknown": state_unknown,
                    "our_open_orders": sorted(our_cids | our_algos)})
        self._log("live_cycles.jsonl", rec)
        self.rm._event("STARTUP_RECONCILE", json.dumps(rec, default=str)[:400])
        return rec

    def _resolve_unexpected_flat(self, pos, pr_map):
        """Defter açık, borsa flat: nihai durumu emirlerden/trade'lerden çöz.
        Çözülemezse False (POSITION_STATE_UNKNOWN → giriş bloğu)."""
        lp = self._live_pos_row(pos["pos_id"]) or {}
        exit_cid = lp.get("exit_client_id")
        prot_cid = lp.get("protection_client_id")
        exit_px, exit_comm = None, 0.0
        # çıkış emirimiz dolduysa fill'inden
        if exit_cid:
            o = self._order_row(exit_cid)
            if o and o["status"] == O_FILLED and o.get("result"):
                r = json.loads(o["result"])
                exit_px = float(r.get("avgPrice") or 0) or None
                exit_comm = float(r.get("commission") or
                                  sum(float(f.get("commission", 0))
                                      for f in r.get("fills", [])) or 0)
        # koruma tetiklendiyse (algo TRIGGERED) userTrades'ten bul
        if exit_px is None and prot_cid:
            try:
                trades = self.exchange.user_trades(pos["symbol"],
                                                   start_ms=pos["entry_ts"])
                closes = [t for t in trades
                          if int(t.get("time", 0)) > pos["entry_ts"]
                          and t.get("buyer", False) != (pos["direction"] > 0)]
                if closes:
                    exit_px = float(closes[-1]["price"])
                    exit_comm = sum(float(t.get("commission", 0))
                                    for t in closes)
            except (ExchangeUnavailable, BinanceApiError):
                return False
        if exit_px is None:
            return False
        self._finalize_close(pos, exit_px, exit_comm, self._now(),
                             source="startup_reconcile")
        return True

    # =========================================================================
    # GİRİŞ ZİNCİRİ (§5, §7)
    # =========================================================================
    def _executable_price(self, symbol, direction, now_ms=None):
        """Giriş ANINDA icra edilebilir fiyat (BLOK31 §2.3): BUY→ask,
        SELL→bid (bookTicker, public). Geçersiz/stale/çapraz fiyat →
        (None, neden) — fail-closed; emir ASLA eski bar fiyatıyla
        gönderilmez."""
        now_ms = now_ms or self._now()
        try:
            t = self.exchange.book_ticker(symbol)
        except ExchangeUnavailable as e:
            return None, f"bookTicker okunamadı ({type(e).__name__}: {e})"
        except Exception as e:
            return None, f"bookTicker beklenmeyen hata ({type(e).__name__}: {e})"
        try:
            bid = float(t["bidPrice"])
            ask = float(t["askPrice"])
            ts = int(t.get("time") or 0)
        except (KeyError, TypeError, ValueError):
            return None, "bookTicker alanları okunamadı"
        if bid <= 0 or ask <= 0 or ask < bid:
            return None, f"bookTicker geçersiz (bid={bid} ask={ask})"
        if ts <= 0 or now_ms - ts > PRICE_STALE_MS:
            return None, (f"bookTicker stale ({(now_ms - ts) / 1000:.0f}s"
                          f" > {PRICE_STALE_MS // 1000}s)")
        px = ask if direction > 0 else bid
        return px, {"bid": bid, "ask": ask, "ts": ts}

    def _attempt_due_flip(self, symbol, now_ms, states=None):
        """Engine'deki due flip'i KARAR ANINDA (bar kapanışı = sonraki bar
        açılışı) taze icra fiyatıyla değerlendirir (BLOK31 — eski bar
        açılış fiyatıyla ~4 saat sonra MARKET gönderme hatasının düzeltmesi):
          - karar penceresi (karar barı kapanışı + 1 bar) KAPANDIYSA → flip
            tüketilir + MISSED_FLIP kaydı (fail-closed; eski sinyal icra
            edilmez, oynatılmaz)
          - taze fiyat alınamıyorsa → flip TUTULUR, sonraki tike retry
            (FLIP_PRICE_RETRY kaydı; sinyal kaybolmaz)
          - sapma kapısı (taze fiyat ↔ sinyal kapanışı) aşılıyorsa → flip
            tüketilir + MISSED_FLIP kaydı
        Başarıda (symbol, direction, px, karar barı ts, qv, sinyal kapanışı)
        olayı döner; emir zinciri (try_entry) strategy_tick'te çağrılır."""
        # GÖREV §1: BRAIN_PRIMARY modunda eski H1 motoru giriş ÜRETEMEZ
        # (savunma-derinliği — strategy_tick bu yolu zaten çağırmaz)
        if self.signal_source == SIGNAL_SOURCE_BRAIN:
            return None
        S = self.engine._state(symbol)
        if not S["pend"] or S["pend"] == S["dir"]:
            return None
        direction = S["pend"]
        bar_ts = S["pend_bar_ts"]
        sig_px = S["pend_price"]
        decision_ts = (bar_ts or 0) + BAR_MS
        st = states.get(symbol) if states else None

        def _note(decision, reason):
            if st is not None:
                st["decision"] = decision
                st["decision_reason"] = reason
                st["flip_detected"] = True

        # 1) karar penceresi: flip, karar barından SONRAKİ barın açılışında
        #    icra edilir; o bar kapandıysa pencere geçmiştir → MISSED
        if now_ms - decision_ts > BAR_MS:
            self.engine.begin_bar(symbol, sig_px or 0.0)      # tüket
            reason = (f"karar penceresi kapandı (karar yaşı "
                      f"{(now_ms - decision_ts) / 3600000:.1f}h > "
                      f"{BAR_MS / 3600000:.0f}h)")
            self._log("live_orders.jsonl",
                      {"t": now_ms, "kind": "MISSED_FLIP", "symbol": symbol,
                       "bar_ts": bar_ts, "direction": direction,
                       "signal_px": sig_px, "reason": reason,
                       "execute": False})
            _note("SIGNAL_STALE", reason)
            return None
        # 2) taze icra fiyatı (BUY→ask, SELL→bid; freshness kapılı)
        px, pmeta = self._executable_price(symbol, direction, now_ms)
        if px is None:
            self._log("live_orders.jsonl",
                      {"t": now_ms, "kind": "FLIP_PRICE_RETRY",
                       "symbol": symbol, "bar_ts": bar_ts,
                       "reason": pmeta})
            _note("PRICE_UNAVAILABLE", pmeta)
            return None
        # 3) sapma kapısı: taze fiyat sinyal kapanışından çok uçtuysa girme
        if sig_px and abs(px - sig_px) / sig_px > SIGNAL_PRICE_DEVIATION_MAX:
            dev = abs(px - sig_px) / sig_px
            self.engine.begin_bar(symbol, px)                 # tüket
            reason = (f"fiyat sapma kapısı: taze {px:.6f} vs sinyal kapanışı "
                      f"{sig_px:.6f} (%{dev * 100:.2f} > "
                      f"{SIGNAL_PRICE_DEVIATION_MAX * 100:.0f})")
            self._log("live_orders.jsonl",
                      {"t": now_ms, "kind": "MISSED_FLIP", "symbol": symbol,
                       "bar_ts": bar_ts, "direction": direction,
                       "signal_px": sig_px, "executable_px": px,
                       "reason": reason, "execute": False})
            _note("PRICE_DEVIATION", reason)
            return None
        # 3.5) BLOK32 kalite kapısı (yalnız QUALITY_BIDIRECTIONAL): flip tek
        #    başına giriş hakkı VERMEZ — yeni hedef yönün kalite skoru
        #    eşiği geçmeli VE episode silahlanmış olmalı. Kalibrasyon
        #    semantiği (~/blok32-calibration simulate()): hedef defteri
        #    (engine dir) flip'te KOŞULSUZ ilerler; EMİR yalnız kalite
        #    geçince çıkar. Kalitesi yetersiz flip TÜKETİLİR + görünür
        #    QUALITY_INSUFFICIENT_FLIP kaydı (fail-closed; eski sinyal
        #    oynatılmaz, sonraki barlar hedef-yön takibiyle tekrar
        #    değerlendirir).
        qinfo = None
        if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
            # BLOK32 görev-2: yön canlı giriş kilidi (fail-closed). Flip
            # yolu da bu kilide tabidir — kilitli yön için emir ÇIKMAZ;
            # flip YİNE tüketilir (hedef ilerler, kalitesi yetersiz flip
            # semantiğiyle aynı) ve görünür DIRECTION_DISABLED_FLIP kaydı
            # yazılır. Ters yönlük AÇIK pozisyonun çıkışı (reduce-only)
            # kilidden ETKİLENMEZ — kilit yalnız GİRİŞLERİ kapatır.
            if not self._dir_enabled(direction):
                self.engine.begin_bar(symbol, px)      # tüket (hedef ilerler)
                reason = (f"yön kilidi: {direction} yönü canlı girişe "
                          "KAPALI (dir_enabled_* anahtarı '1' değil — "
                          "fail-closed; varsayılan KAPALI)")
                self._log("live_orders.jsonl",
                          {"t": now_ms, "kind": "DIRECTION_DISABLED_FLIP",
                           "symbol": symbol, "bar_ts": bar_ts,
                           "direction": direction, "signal_px": sig_px,
                           "executable_px": px, "reason": reason,
                           "execute": False})
                _note(_direction_disabled_label(direction), reason)
                return None
            qinfo = self.quality.compute(symbol, direction)
            engaged = self._episode_engaged(symbol, direction)
            if qinfo is None or engaged or not qinfo["quality_pass"]:
                self.engine.begin_bar(symbol, px)      # tüket (hedef ilerler)
                if qinfo is None:
                    reason = ("kalite hesaplanamadı (fail-closed: bileşen "
                              "penceresi dolmadı / veri eksik)")
                elif engaged:
                    reason = (f"episode ENGAGED (hysteresis) — yeniden giriş "
                              f"için skor {qinfo['quality_threshold'] - cq.hysteresis(direction):.1f}"
                              f" altına inmeli (skor {qinfo['quality_score']})")
                else:
                    reason = (f"kalite skoru {qinfo['quality_score']} < eşik "
                              f"{qinfo['quality_threshold']} — "
                              f"{qinfo['signal_reason']}")
                rec = {"t": now_ms, "kind": "QUALITY_INSUFFICIENT_FLIP",
                       "symbol": symbol, "bar_ts": bar_ts,
                       "direction": direction, "signal_px": sig_px,
                       "executable_px": px, "reason": reason,
                       "episode": "ENGAGED" if engaged else "ARMED",
                       "execute": False}
                if qinfo is not None:
                    rec.update({k: qinfo[k] for k in
                                ("quality_score", "quality_threshold",
                                 "momentum_strength", "trend_confirmation",
                                 "volatility_context", "volume_confirmation")})
                self._log("live_orders.jsonl", rec)
                _note("QUALITY_INSUFFICIENT", reason)
                return None
        # 4) ICRA: flip karar anında (= icra barı açılışı) taze fiyatla
        #    tüketilir; dir ilerler. Sonuç (executed/blocked) try_entry'de.
        d = self.engine.begin_bar(symbol, px)
        d.execution_ts = decision_ts
        d.ref_price = px
        if st is not None:
            st["decision"] = ("QUALITY_VALID_LONG" if direction > 0 else
                              "QUALITY_VALID_SHORT") \
                if self.entry_policy == cq.ENTRY_POLICY_QUALITY \
                else "SIGNAL_VALID"
            st["current_executable_price"] = px
            st["price_source"] = "bookTicker"
            st["price_age_ms"] = now_ms - pmeta.get("ts", now_ms)
            st["flip_detected"] = True
            if qinfo is not None:
                for k in ("quality_score", "quality_threshold",
                          "quality_pass", "momentum_strength",
                          "trend_confirmation", "volatility_context",
                          "volume_confirmation", "signal_reason"):
                    st[k] = qinfo[k]
                st["signal_timestamp"] = now_ms
                st["signal_bar_timestamp"] = bar_ts
                st["quality_episode"] = "ENGAGED" if \
                    self._episode_engaged(symbol, direction) else "ARMED"
        return (symbol, direction, px, bar_ts, None, sig_px)

    def _entry_blockers(self, symbol, direction, bar_ts, now_ms):
        """Giriş öncesi tüm kapılar — ret nedenleri listesi (boş = geçer)."""
        reasons = []
        if self.rm.status == STATUS_HALTED or \
                self.halt_state != HALT_ACTIVE:
            reasons.append(f"HALT ({self.rm.status}/{self.halt_state})")
        # BLOK30: operatör ENTRY PAUSE — HALT'tan BAĞIMSIZ kontrol; yalnızca
        # yeni GİRİŞLERİ engeller (risk/çıkış/koruma döngüleri çalışır)
        if self.entry_paused:
            reasons.append("ENTRY_PAUSED (operatör giriş duraklatması — "
                           "mevcut pozisyon yönetimi ve çıkışlar çalışır)")
        # BLOK32 görev-2: yön canlı giriş kilidi — GERÇEK EMİR GÖNDERİLMEDEN
        # ÖNCE ortak kontrol. Flip ve quality target-following giriş
        # yollarının İKİSİ de try_entry → buraya ulaşır (sinyal katmanındaki
        # kontrollerin tamamı savunma-derinliği; burası ortak zorlayıcı).
        # Fail-closed: dir_enabled_* anahtarı TAM OLARAK "1" değilse KAPALI.
        if self.entry_policy == cq.ENTRY_POLICY_QUALITY and \
                not self._dir_enabled(direction):
            reasons.append(
                f"{_direction_disabled_label(direction)} "
                f"(yön kilidi: {direction} yönü canlı girişe KAPALI — "
                "dir_enabled_* anahtarı '1' değil, fail-closed; varsayılan "
                "KAPALI, yalnız açık operatör izni açar)")

        # BLOK31: T0 kapısı KARAR ZAMANINA göre — bar_ts karar barının
        # AÇILIŞI; karar, kapanışta (bar_ts + BAR_MS) verilir. T0'dan önce
        # AÇILIP T0'dan sonra KAPANAN barın sinyali T0 sonrası ilk GEÇERLİ
        # sinyaldir (eski kod ot <= t0 kontrolüyle bunu yanlışlıkla
        # engelliyor ve dedup nedeniyle kalıcı kaybediyordu).
        if bar_ts + BAR_MS <= self.t0_ms:
            reasons.append(f"T0 öncesi/eski sinyal (karar {bar_ts + BAR_MS} "
                           f"<= t0 {self.t0_ms}) — eski -1x8 hedefleri emir "
                           "oluşturamaz")
        if self._get_state("feed_down_ts"):
            reasons.append("Mark Price feed kesintide (fail-closed)")
        if str(self._get_state("reconciled_ok") or "0") != "1":
            reasons.append("startup mutabakatı tamam değil")
        if str(self._get_state("unowned_position_seen") or "0") == "1":
            reasons.append("sahipliği kanıtlanamayan pozisyon/emir var "
                           "(yeni giriş engelli)")
        if str(self._get_state("position_state_unknown") or "0") == "1":
            reasons.append("pozisyon durumu bilinmiyor (fail-closed)")
        if self._get_state("feed_recovered_pending_reconcile") == "1":
            reasons.append("feed geri döndü — mutabakat bekleniyor")
        if str(self._get_state("protection_unverifiable") or "0") == "1":
            reasons.append("koruma emri doğrulanamıyor (F-c fail-closed)")
        # likidasyon kapısı (§6)
        mark = self.rm._mark(symbol, now_ms)
        ok, reason = self.liq.verify(symbol)
        if not ok:
            reasons.append(reason)
        # ilk pozisyon kuralı (§7): ilk fill+koruma doğrulanmadan ikinci yok
        opens = self.rm.open_positions()
        pending_entries = self._db_execute(
            "SELECT COUNT(*) FROM live_orders WHERE kind='ENTRY' AND status "
            "IN ('INTENT','SUBMITTED','UNKNOWN','PARTIALLY_FILLED')"
        ).fetchone()[0]
        first_verified = str(self._get_state("first_position_verified")
                             or "0") == "1"
        if (opens or pending_entries) and not first_verified:
            reasons.append("ilk pozisyonun fill+koruma doğrulaması bekleniyor")
        if pending_entries:
            reasons.append("bekleyen giriş emri var (tek giriş zinciri)")
        # §5: doğrulanmış borsa-koruması olmayan açık pozisyon varken
        # yeni pozisyon AÇILMAZ
        for p in opens:
            lp = self._live_pos_row(p["pos_id"])
            if lp is None or lp["protection_status"] != P_VERIFIED:
                reasons.append(f"{p['symbol']} pozisyonu korumasız/"
                               "koruma doğrulanmamış (PROTECTED değil)")
                break
        # BLOK32 (yalnız QUALITY_BIDIRECTIONAL): aynı sembolde ikinci pozisyon
        # YOK — One-way reversal protokolü (§7): ters sinyalde ÖNCE reduce-only
        # çıkış → borsada flat doğrulama → koruma/defter mutabakatı → ancak
        # sonraki barlarda yeni yön değerlendirmesi. Çıkışı süren veya durumu
        # belirsiz pozisyon üzerine ters emir ASLA gönderilmez. (FLIP_ONLY
        # kanonik davranış değişmez.)
        if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
            for p in opens:
                if p["symbol"] == symbol:
                    lp = self._live_pos_row(p["pos_id"]) or {}
                    reasons.append(
                        f"{symbol} pozisyonu açık (çıkış durumu "
                        f"{lp.get('exit_status', 'bilinmiyor')}) — quality "
                        "politika: aynı sembolde ikinci pozisyon yok, reversal "
                        "önce çıkışın flat doğrulamasını ister")
                    break
        return reasons

    def _sizing(self, symbol, ref_px, notional=CANARY_NOTIONAL):
        """Qty hesabı: stepSize'a aşağı yuvarla; minQty/minNotional ve
        MAX_POSITION_NOTIONAL operatör limiti içinde değilse None + neden
        (§7: keyfi seçim YOK — borsa filtresi izin vermiyorsa giriş
        engellenir)."""
        f = self.filters.get(symbol)
        if not f or not ref_px:
            return None, f"{symbol} filtre bilgisi yok"
        step = float(f["stepSize"])
        min_qty = float(f["minQty"])
        min_notional = float(f["minNotional"] or 0)
        qty = math.floor((notional / ref_px) / step) * step
        qty = round(qty, 12)
        if qty < min_qty:
            return None, (f"{symbol} minQty {min_qty} > hesaplanan {qty} "
                          f"({MAX_POSITION_NOTIONAL} USDT limiti içinde "
                          "boyutlanamıyor)")
        actual = qty * ref_px
        if actual < min_notional:
            return None, (f"{symbol} minNotional {min_notional} > {actual:.2f}")
        if actual > MAX_POSITION_NOTIONAL + 1e-9:
            return None, (f"{symbol} yuvarlama sonrası notional "
                          f"{actual:.2f} > {MAX_POSITION_NOTIONAL}")
        prec = int(f.get("quantityPrecision", 8))
        return f"{qty:.{prec}f}", None

    def try_entry(self, symbol, direction, ref_px, bar_ts, now_ms=None,
                  qv=None, signal_px=None, decision_id=None, notional=None):
        """Tam giriş zinciri (§5 adım 1-9). ref_px = İCRA ANI fiyatı
        (BLOK31: strateji yolu bookTicker ask/bid getirir; eski bar açılışı
        ASLA kullanılmaz); signal_px = sinyal barı kapanışı (sapma kaydı).
        Ret/engel her durumda kayda geçer.

        GÖREV §2 (2026-11-26): decision_id verilirse (BRAIN_PRIMARY yolu)
        idempotency çapası budur — brainsig:{decision_id} kalıcı anahtarı
        aynı beyin kararının ikinci kez emir üretmesini engeller; karar
        kimliği emir params'ına ve denetim kaydına yazılır (beyin kaydından
        execution değerlendirmesine izlenebilirlik). notional: beyin önerisi
        (suggested_notional) — SADECE politika tavanına kadar kabul edilir;
        nihai boyut _sizing (borsa filtreleri) + rm.check_order (CROSS risk
        kapısı) tarafından belirlenir (AI önerisi doğrudan emir DEĞİLDİR)."""
        now_ms = now_ms or self._now()
        rec = {"t": now_ms, "kind": "LIVE_ENTRY_ATTEMPT", "symbol": symbol,
               "direction": direction, "bar_ts": bar_ts, "ref_px": ref_px,
               "signal_px": signal_px, "signal_source": self.signal_source}
        if decision_id:
            rec["decision_id"] = decision_id
        out = [rec]
        if direction == 0:
            rec["result"] = "FLAT_TARGET"
            return out
        # sinyal dedup (aynı sinyal ikinci kez işlem oluşturamaz — §7).
        # BRAIN_PRIMARY: dedup kararı decision_id üzerinden (beyin kaydına
        # birebir izlenebilir); H1 yolu kanonik sig:{symbol}:{bar_ts}.
        sig_key = (f"brainsig:{decision_id}" if decision_id
                   else f"sig:{symbol}:{bar_ts}")
        with self._lock:
            if self._get_state(sig_key) is not None:
                rec["result"] = ("DUPLICATE_BRAIN_DECISION" if decision_id
                                 else "DUPLICATE_SIGNAL")
                return out
            self._set_state(sig_key, str(direction))
        blockers = self._entry_blockers(symbol, direction, bar_ts, now_ms)
        if blockers:
            rec["result"] = "BLOCKED"
            rec["blockers"] = blockers
            self._log("live_orders.jsonl", rec)
            self.rm._event("ENTRY_BLOCKED",
                           f"{symbol} {' | '.join(blockers)[:300]}")
            return out
        # BRAIN_PRIMARY: beyin önerilen notional yalnız politika tavanına
        # KADAR kabul edilir (AI önerisi doğrudan emir haline GETİRİLMEZ —
        # görev §2); hiçbir durumda operatör 100 USDT hard cap'ini aşamaz.
        # H1 yolu ve önerisiz beyin kaydı kanonik politika boyutunu kullanır.
        target_notional = CANARY_NOTIONAL * (1 - ENTRY_SIZE_BUFFER)
        if notional is not None:
            if notional <= 0:
                rec["result"] = "BLOCKED"
                rec["blockers"] = [f"BRAIN_SIZING_INVALID:notional="
                                   f"{notional}"]
                self._log("live_orders.jsonl", rec)
                self.rm._event("ENTRY_BLOCKED",
                               f"{symbol} beyin notional gecersiz ({notional})")
                return out
            target_notional = min(float(notional), target_notional)
        qty_str, err = self._sizing(symbol, ref_px, target_notional)
        if err:
            rec["result"] = "BLOCKED"
            rec["blockers"] = [err]
            self._log("live_orders.jsonl", rec)
            self.rm._event("ENTRY_BLOCKED", f"{symbol} {err}")
            return out
        qty = float(qty_str)
        rec["qty"] = qty_str
        rec["notional"] = round(qty * ref_px, 6)
        with self._entry_lock:
            # BLOK30 §4: ortak giriş kapısı (flock) — operatör CLI'si ile
            # aynı kilidi paylaşır. INTENT kaydı + new_order() + sonuç
            # yazımı bu kritik bölümün İÇİNDE tamamlanır; kapı ALTINDA
            # PAUSE/HALT durumu SON KEZ yeniden okunur (kontrol ile emir
            # arasındaki yarış penceresi kapalı). Kilit alınamazsa emir
            # GÖNDERİLMEZ (fail-closed).
            try:
                with entry_gate(self.db_path, timeout_s=30.0):
                    gate_blockers = self._final_entry_gate(direction)
                    if gate_blockers:
                        rec["result"] = "BLOCKED"
                        rec["blockers"] = gate_blockers
                        self._log("live_orders.jsonl", rec)
                        self.rm._event(
                            "ENTRY_BLOCKED",
                            f"{symbol} {' | '.join(gate_blockers)[:300]}")
                        return out
                    try:
                        # atomik risk kapısı (canary_risk kural 10)
                        self.rm.check_order(symbol, direction,
                                            qty * ref_px, now_ms)
                    except OrderRejected as e:
                        rec["result"] = f"REJECTED: {e}"
                        self._log("live_orders.jsonl", rec)
                        return out
                    # intent KAYIT ÖNCESİ (idempotency anchor — §2.3)
                    seq = self._db_execute(
                        "SELECT COUNT(*)+1 FROM live_orders "
                        "WHERE kind='ENTRY'").fetchone()[0]
                    cid = f"{CLIENT_PREFIX}E{seq}"
                    self._upsert_order(cid, "ENTRY", symbol, O_INTENT,
                                       params={"side": "BUY" if direction > 0
                                               else "SELL",
                                               "quantity": qty_str,
                                               "type": "MARKET",
                                               "decision_id": decision_id,
                                               "signal_source":
                                                   self.signal_source})
                    try:
                        resp = self.exchange.new_order(
                            symbol, "BUY" if direction > 0 else "SELL",
                            "MARKET", quantity=qty_str, client_order_id=cid,
                            new_order_resp_type="RESULT")
                    except UnknownResult as e:
                        self._upsert_order(cid, "ENTRY", symbol, O_UNKNOWN,
                                           result={"error": str(e)})
                        rec["result"] = "ENTRY_UNKNOWN (client id ile " \
                                        "çözülecek)"
                        self._log("live_orders.jsonl", rec)
                        return out
                    except BinanceApiError as e:
                        self._upsert_order(cid, "ENTRY", symbol, O_REJECTED,
                                           result={"code": e.code,
                                                   "msg": e.msg})
                        rec["result"] = f"REJECTED: {e}"
                        self._log("live_orders.jsonl", rec)
                        return out
                    self._upsert_order(cid, "ENTRY", symbol,
                                       resp.get("status", O_SUBMITTED),
                                       exchange_id=str(resp.get("orderId")),
                                       result=resp)
                    rec["entry_client_id"] = cid
                    if resp.get("status") == O_FILLED:
                        rec.update(self._on_entry_filled(cid, resp, symbol,
                                                         direction, now_ms))
                    else:
                        rec["result"] = f"entry status {resp.get('status')} " \
                                        "— risk döngüsü izleyecek"
            except EntryGateBusy as e:
                rec["result"] = "BLOCKED"
                rec["blockers"] = [f"entry-gate kilidi meşgul ({e})"]
                self._log("live_orders.jsonl", rec)
                return out
        self._log("live_orders.jsonl", rec)
        return out

    def _final_entry_gate(self, direction=0):
        """Kapı kilidi ALTINDA son kabul kontrolü (BLOK30 §4). PAUSE durumu
        bu sürecin DB bağlantısından YENİDEN okunur — erken _entry_blockers
        kontrolünden sonra değişmiş olabilir. HALT da yeniden doğrulanır.
        BLOK32 görev-2: yön canlı giriş izni de burada YENİDEN okunur
        (sinyal değerlendirmesi ile emir arasındaki yarış penceresi kapalı).
        Boş liste = geçer."""
        reasons = []
        if self.entry_paused:
            reasons.append("ENTRY_PAUSED (operatör giriş duraklatması)")
        if self.rm.status == STATUS_HALTED or \
                self.halt_state != HALT_ACTIVE:
            reasons.append(f"HALT ({self.rm.status}/{self.halt_state})")
        if direction != 0 and not self._dir_enabled(direction):
            reasons.append(
                f"{_direction_disabled_label(direction)} "
                f"(yön kilidi: {direction} canlı girişe KAPALI — "
                "dir_enabled_* anahtarı '1' değil, fail-closed)")
        return reasons

    def _on_entry_filled(self, cid, resp, symbol, direction, now_ms):
        """Gerçek fill → defter + koruma zinciri (§5 adım 2-9)."""
        qty = float(resp.get("executedQty") or 0)
        avg = float(resp.get("avgPrice") or 0)
        if qty <= 0 or avg <= 0:
            return {"result": "FILL_DOĞRULANAMADI (executedQty/avgPrice yok)"}
        comm = sum(float(f.get("commission", 0) or 0)
                   for f in resp.get("fills", []))
        notional = qty * avg
        # fill sonrası notional kontrolü (§8) — BLOK31: HARD cap (100).
        # Eski %1 tolerans (NOTIONAL_SLIPPAGE_TOL) sınırın fiilen 101'e
        # çıkmasına izin veriyordu; ihlal artık açık kayıt + reduce-only
        # çıkış üretir ve "sınır içinde" gibi RAPORLANMAZ.
        if notional > MAX_POSITION_NOTIONAL + 1e-9:
            pos_id = self.rm.open_position(symbol, direction, notional, avg,
                                           now_ms, comm)
            self._db_execute("INSERT OR REPLACE INTO live_positions "
                            "(pos_id, symbol, protection_status, exit_status,"
                            " updated_ts) VALUES (?,?,?,?,?)",
                            (pos_id, symbol, P_FAILED, X_NONE, now_ms))
            self.db.commit()
            # result=resp: fill yanıtı (executedQty/avgPrice/komisyon) sonraki
            # pos_id güncellemesiyle EZİLMEZ — denetim izi kalıcı (BLOK31 §6)
            self._upsert_order(cid, "ENTRY", symbol, O_FILLED, pos_id=pos_id,
                               result=resp)
            self.rm._event("OVER_LIMIT_NOTIONAL",
                           f"{symbol} fill notional {notional:.4f} > "
                           f"{MAX_POSITION_NOTIONAL} (HARD cap) — reduce-only "
                           "çıkış başlatıldı")
            self.initiate_risk_exit(pos_id, "OVER_LIMIT_NOTIONAL")
            return {"pos_id": pos_id, "result":
                    "OVER_LIMIT_EXIT: fill notional limiti aştı"}
        pos_id = self.rm.open_position(symbol, direction, notional, avg,
                                       now_ms, comm)
        self._upsert_order(cid, "ENTRY", symbol, O_FILLED, pos_id=pos_id,
                           result=resp)
        total_n = self.rm.total_notional(now_ms)
        if total_n is not None and total_n > MAX_TOTAL_NOTIONAL + 1e-9:
            self._db_execute("INSERT OR REPLACE INTO live_positions "
                            "(pos_id, symbol, protection_status, exit_status,"
                            " updated_ts) VALUES (?,?,?,?,?)",
                            (pos_id, symbol, P_FAILED, X_NONE, now_ms))
            self.db.commit()
            self.rm._event("OVER_LIMIT_TOTAL_NOTIONAL",
                           f"toplam fill notional {total_n:.4f} > "
                           f"{MAX_TOTAL_NOTIONAL} (HARD cap) — reduce-only "
                           "çıkış başlatıldı")
            self.initiate_risk_exit(pos_id, "OVER_LIMIT_TOTAL_NOTIONAL")
            return {"pos_id": pos_id, "result":
                    "OVER_LIMIT_EXIT: toplam notional limiti aştı"}
        self._db_execute("INSERT OR REPLACE INTO live_positions "
                        "(pos_id, symbol, protection_status, exit_status, "
                        "updated_ts) VALUES (?,?,?,?,?)",
                        (pos_id, symbol, P_PENDING, X_NONE, now_ms))
        self.db.commit()
        self.rm._event("ENTRY_FILLED", f"{symbol} pos={pos_id} qty={qty} "
                       f"avg={avg} comm={comm}")
        prot = self._place_protection(pos_id, now_ms)
        return {"pos_id": pos_id, "result": "FILLED", "protection": prot}

    def _place_protection(self, pos_id, now_ms):
        """STOP_MARKET CONDITIONAL koruma (§5). -5 USDT bütçesinden
        triggerPrice; closePosition=true; workingType=MARK_PRICE."""
        pos = self.rm._pos(pos_id)
        symbol, direction = pos["symbol"], pos["direction"]
        entry, qty = pos["entry_px"], pos["units"]
        # F-g: koruma yerleşiminden ÖNCE gerçek pozisyonu taze oku
        try:
            amt = self._exchange_amt(symbol)
        except ExchangeUnavailable:
            self._update_live_pos(pos_id, protection_status=P_PENDING)
            return {"status": P_PENDING, "note": "positionRisk okunamadı"}
        if amt is None or abs(amt) < 1e-12 or direction * amt < 0:
            self._update_live_pos(pos_id, protection_status=P_FAILED)
            self.initiate_risk_exit(pos_id, "PROTECTION_NO_POSITION")
            return {"status": P_FAILED}
        qty = abs(amt)                        # gerçek miktar (§5 adım 2-3)
        budget = MAX_POSITION_NET_LOSS
        funding = self._db_execute(  # rm tablosundan (aynı DB dosyası)
            "SELECT COALESCE(SUM(amount),0) FROM funding WHERE pos_id=?",
            (pos_id,)).fetchone()[0]
        fee_rate = (pos["entry_comm"] / (qty * entry)) if entry else 0
        est_exit_comm = fee_rate * qty * entry
        usable = budget - pos["entry_comm"] - est_exit_comm + funding
        # funding: cf negatif = ödenmiş → zarar; pozitif = alınmış
        if usable <= 0:
            self._update_live_pos(pos_id, protection_status=P_FAILED)
            self.initiate_risk_exit(pos_id, "STOP_BUDGET_YOK")
            return {"status": P_FAILED, "note": "bütçe komisyon/funding'e "
                    "gitti — anında çıkış"}
        dist = usable / qty
        raw = entry - dist if direction > 0 else entry + dist
        f = self.filters.get(symbol) or {}
        tick = float(f.get("tickSize") or 0)
        stop = round_to_tick(raw, tick, toward_entry=True,
                             is_long=direction > 0)
        if stop is None or stop <= 0:
            self._update_live_pos(pos_id, protection_status=P_FAILED)
            self.initiate_risk_exit(pos_id, "STOP_FIYATI_HESAPLANAMADI")
            return {"status": P_FAILED}
        # stop, güncel mark'ın doğru tarafında mı? (değilse -2021 gelir)
        mark = self.rm._mark(symbol, now_ms)
        if mark is not None:
            if direction > 0 and stop >= mark:
                self._update_live_pos(pos_id, protection_status=P_FAILED)
                self.initiate_risk_exit(pos_id, "STOP_MARK_OTESINDE")
                return {"status": P_FAILED,
                        "note": "stop mevcut mark'ı geçmiş — anında çıkış"}
            if direction < 0 and stop <= mark:
                self._update_live_pos(pos_id, protection_status=P_FAILED)
                self.initiate_risk_exit(pos_id, "STOP_MARK_OTESINDE")
                return {"status": P_FAILED}
        side = "SELL" if direction > 0 else "BUY"
        cid = f"{CLIENT_PREFIX}P{pos_id}"
        self._upsert_order(cid, "PROTECTION", symbol, O_INTENT, pos_id=pos_id,
                           params={"type": "STOP_MARKET", "side": side,
                                   "triggerPrice": stop,
                                   "workingType": "MARK_PRICE",
                                   "closePosition": True})
        self._update_live_pos(pos_id, protection_client_id=cid,
                              protection_status=P_PENDING)
        try:
            resp = self.exchange.new_algo_order(
                symbol, side, "STOP_MARKET", trigger_price=stop,
                working_type="MARK_PRICE", close_position=True,
                client_algo_id=cid)
        except UnknownResult as e:
            self._upsert_order(cid, "PROTECTION", symbol, O_UNKNOWN,
                               pos_id=pos_id, result={"error": str(e)})
            return {"status": P_PENDING, "note": "UNKNOWN — sorgu ile çözülecek"}
        except BinanceApiError as e:
            # F-e: -2021 sınıflandırılır → PROTECTION_FAILED + çıkış,
            # kontrolsüz retry YOK
            self._upsert_order(cid, "PROTECTION", symbol, O_REJECTED,
                               pos_id=pos_id,
                               result={"code": e.code, "msg": e.msg})
            if e.is_immediately_trigger() or e.is_unknown_order():
                self._update_live_pos(pos_id, protection_status=P_FAILED)
                self.initiate_risk_exit(pos_id, f"PROTECTION_{e.code}")
                return {"status": P_FAILED, "code": e.code}
            self._update_live_pos(pos_id, protection_status=P_FAILED)
            self.initiate_risk_exit(pos_id, f"PROTECTION_{e.code}")
            return {"status": P_FAILED, "code": e.code}
        self._upsert_order(cid, "PROTECTION", symbol,
                           resp.get("algoStatus", O_SUBMITTED), pos_id=pos_id,
                           exchange_id=str(resp.get("algoId")), result=resp)
        verified = self._verify_protection_order(pos_id, cid)
        return {"status": P_VERIFIED if verified else P_PENDING,
                "trigger_price": stop, "client_id": cid,
                "verified": verified}

    def _verify_protection_order(self, pos_id, cid):
        """Koruma emrinin borsada GERÇEKTEN açık ve doğru olduğunu doğrula
        (§5 adım 7-9). Doğrulanmadan pozisyon PROTECTED sayılmaz."""
        pos = self.rm._pos(pos_id)
        try:
            found = None
            for o in self.exchange.open_algo_orders(symbol=pos["symbol"]):
                if o.get("clientAlgoId") == cid:
                    found = o
                    break
            if found is None:
                try:
                    q = self.exchange.query_algo_order(
                        symbol=pos["symbol"], client_algo_id=cid)
                    if isinstance(q, dict) and q.get("clientAlgoId") == cid:
                        found = q
                except (ExchangeUnavailable, BinanceApiError):
                    found = None
        except ExchangeUnavailable:
            return False                    # F-c: doğrulanamıyor → PROTECTED değil
        if found is None:
            return False
        o = self._order_row(cid)
        want = json.loads(o["params"]) if o and o.get("params") else {}
        trig = found.get("triggerPrice") or found.get("stopPrice")
        try:
            trig_f = float(trig)
        except (TypeError, ValueError):
            return False
        ok = (str(found.get("symbol")) == pos["symbol"]
              and str(found.get("side")) == str(want.get("side"))
              and abs(trig_f - float(want["triggerPrice"])) < 1e-12)
        if ok:
            self._update_live_pos(pos_id, protection_status=P_VERIFIED)
            self._set_state("first_position_verified", 1)
            self.rm._event("PROTECTION_VERIFIED",
                           f"pos {pos_id} {pos['symbol']} algo {cid} "
                           f"trigger {trig_f}")
        return ok

    # =========================================================================
    # POZİSYON BAZLI RİSK ÇIKIŞI (§2.1) — H1 flip BEKLEMEZ
    # =========================================================================
    def initiate_risk_exit(self, pos_id, reason):
        """-5 USDT (veya koruma/limit ihlali) → EXIT_PENDING → reduce-only
        çıkış. Gerçek miktar positionRisk'ten okunur (F-g)."""
        now = self._now()
        lp = self._live_pos_row(pos_id)
        if lp is None:
            pos = self.rm._pos(pos_id)
            self._db_execute("INSERT OR REPLACE INTO live_positions "
                            "(pos_id, symbol, protection_status, exit_status,"
                            " updated_ts) VALUES (?,?,?,?,?)",
                            (pos_id, pos["symbol"], P_PENDING, X_NONE, now))
            self.db.commit()
            lp = self._live_pos_row(pos_id)
        if lp["exit_status"] in (X_PENDING, X_UNKNOWN, X_PARTIAL, X_UNCONFIRMED,
                                 X_CONFIRMED):
            return {"pos_id": pos_id, "note": f"çıkış akışı zaten "
                    f"{lp['exit_status']}"}
        pos = self.rm._pos(pos_id)
        try:
            amt = self._exchange_amt(pos["symbol"])
        except ExchangeUnavailable as e:
            # kesinti: flat İDDİA EDİLMEZ — bekle (§3 bağlantı kesilirse)
            self._update_live_pos(pos_id, exit_status=X_UNCONFIRMED,
                                  exit_reason=f"{reason} (feed down: {e})")
            self.rm._event("EXIT_PENDING_UNCONFIRMED",
                           f"pos {pos_id} {reason} — positionRisk okunamadı")
            return {"pos_id": pos_id, "status": X_UNCONFIRMED}
        if amt is None:
            self._update_live_pos(pos_id, exit_status=X_UNCONFIRMED,
                                  exit_reason=reason)
            return {"pos_id": pos_id, "status": X_UNCONFIRMED}
        if abs(amt) < 1e-12:
            # zaten flat (koruma tetiklenmiş olabilir) → çöz
            resolved = self._resolve_unexpected_flat(pos, {})
            return {"pos_id": pos_id, "note": "borsa zaten flat",
                    "resolved": resolved}
        self._submit_exit(pos_id, abs(amt), reason)
        lp = self._live_pos_row(pos_id)
        return {"pos_id": pos_id, "status": lp["exit_status"],
                "reason": reason}

    def _submit_exit(self, pos_id, qty, reason):
        """Reduce-only MARKET çıkış — yeni idempotent intent (yalnızca
        DOĞRULANMIŞ kalan miktar için). Körlemesine tekrar POST edilmez."""
        now = self._now()
        pos = self.rm._pos(pos_id)
        symbol, direction = pos["symbol"], pos["direction"]
        side = "SELL" if direction > 0 else "BUY"
        f = self.filters.get(symbol) or {}
        step = float(f.get("stepSize") or 0) or None
        if step:
            qty = math.floor(qty / step) * step
            prec = int(f.get("quantityPrecision", 8))
            qty_str = f"{qty:.{prec}f}"
        else:
            qty_str = f"{qty:.8f}"
        attempt = (self._db_execute(
            "SELECT COALESCE(MAX(attempt),0)+1 FROM live_orders WHERE "
            "kind='EXIT' AND pos_id=?", (pos_id,)).fetchone()[0])
        cid = f"{CLIENT_PREFIX}X{pos_id}n{attempt}"
        self._upsert_order(cid, "EXIT", symbol, O_INTENT, pos_id=pos_id,
                           attempt=attempt,
                           params={"side": side, "quantity": qty_str,
                                   "reduceOnly": True, "type": "MARKET"})
        self._update_live_pos(pos_id, exit_client_id=cid, exit_status=X_PENDING,
                              exit_reason=reason)
        self.rm._event("EXIT_INTENT",
                       f"pos {pos_id} {symbol} qty {qty_str} reason {reason}")
        try:
            resp = self.exchange.new_order(
                symbol, side, "MARKET", quantity=qty_str, reduce_only=True,
                client_order_id=cid, new_order_resp_type="RESULT")
        except UnknownResult as e:
            self._upsert_order(cid, "EXIT", symbol, O_UNKNOWN, pos_id=pos_id,
                               attempt=attempt, result={"error": str(e)})
            self._update_live_pos(pos_id, exit_status=X_UNKNOWN)
            self.rm._event("EXIT_UNKNOWN",
                           f"pos {pos_id} {cid} — client id ile çözülecek")
            return cid
        except BinanceApiError as e:
            self._upsert_order(cid, "EXIT", symbol, O_REJECTED, pos_id=pos_id,
                               attempt=attempt,
                               result={"code": e.code, "msg": e.msg})
            # -2022 reduceOnly reddi: pozisyon zaten flat olabilir → yeniden
            # sorgula (körlemesine tekrar gönderme)
            self._update_live_pos(pos_id, exit_status=X_UNCONFIRMED)
            self.rm._event("EXIT_REJECTED", f"pos {pos_id} {e}")
            return cid
        self._upsert_order(cid, "EXIT", symbol, resp.get("status", O_SUBMITTED),
                           pos_id=pos_id, attempt=attempt,
                           exchange_id=str(resp.get("orderId")), result=resp)
        if resp.get("status") == O_FILLED:
            self._check_flat_and_finalize(pos_id)
        elif resp.get("status") == O_PARTIAL:
            self._update_live_pos(pos_id, exit_status=X_PARTIAL)
        else:
            self._update_live_pos(pos_id, exit_status=X_PENDING)
        return cid

    def _check_flat_and_finalize(self, pos_id):
        """FLAT doğrulaması positionRisk ile (§2.1 adım 12-13; F-b).
        Flat DOĞRULANMADAN CLOSED yazılmaz."""
        pos = self.rm._pos(pos_id)
        if pos["status"] == "CLOSED":
            return True                # zaten kapatıldı — üzerine yazma
        try:
            amt = self._exchange_amt(pos["symbol"])
        except ExchangeUnavailable:
            self._update_live_pos(pos_id, exit_status=X_UNCONFIRMED)
            return False
        if amt is None or abs(amt) >= 1e-12:
            return False
        lp = self._live_pos_row(pos_id)
        exit_px, exit_comm = None, 0.0
        cid = lp.get("exit_client_id")
        if cid:
            o = self._order_row(cid)
            if o and o.get("result"):
                r = json.loads(o["result"])
                exit_px = float(r.get("avgPrice") or 0) or None
                exit_comm = float(r.get("commission") or
                                  sum(float(f.get("commission", 0))
                                      for f in r.get("fills", [])) or 0)
        if exit_px is None:
            try:
                trades = self.exchange.user_trades(pos["symbol"],
                                                   start_ms=pos["entry_ts"])
                closes = [t for t in trades
                          if int(t.get("time", 0)) > pos["entry_ts"]
                          and t.get("buyer", False) != (pos["direction"] > 0)]
                if closes:
                    exit_px = float(closes[-1]["price"])
                    exit_comm = sum(float(t.get("commission", 0))
                                    for t in closes)
            except (ExchangeUnavailable, BinanceApiError):
                pass
        if exit_px is None:
            exit_px = pos["entry_px"]      # kanıt yok: nötr fiyatla kapat,
            exit_comm = exit_comm          # durum yine kayda geçer
        self._finalize_close(pos, exit_px, exit_comm, self._now(),
                             source="flat_verified")
        return True

    def _finalize_close(self, pos, exit_px, exit_comm, now_ms, source=""):
        realized = self.rm.close_position(pos["pos_id"], exit_px, now_ms,
                                          exit_comm)
        self._update_live_pos(pos["pos_id"], exit_status=X_CONFIRMED)
        # F-a: flat DOĞRULANDIKTAN SONRA artık koruma emrini güvenle temizle
        self._cleanup_protection_after_flat(pos["pos_id"])
        self.rm._event("POSITION_CLOSED",
                       f"pos {pos['pos_id']} {pos['symbol']} realized "
                       f"{realized:.6f} exit_px {exit_px} ({source})")
        return realized

    def _cleanup_protection_after_flat(self, pos_id):
        """Flat doğrulanan pozisyonun artık koruma emrini iptal et ve sonucu
        DOĞRULA (§3 adım 10; F-a: çift kapanış/ters pozisyon önlenir)."""
        lp = self._live_pos_row(pos_id)
        cid = lp.get("protection_client_id") if lp else None
        if not cid:
            return
        o = self._order_row(cid)
        if o and o["status"] in (O_TRIGGERED, O_CANCELED, O_REJECTED,
                                 O_FILLED):
            # P_FAILED durumunun ÜZERİNE YAZMA (neden bilgisi kaybolur);
            # tetiklenmişse TRIGGERED, iptalse CANCELED
            if lp["protection_status"] != P_FAILED:
                self._update_live_pos(pos_id, protection_status=P_TRIGGERED
                                      if o["status"] == O_TRIGGERED
                                      else P_CANCELED)
            return
        pos = self.rm._pos(pos_id)
        try:
            self.exchange.cancel_algo_order(symbol=pos["symbol"],
                                            client_algo_id=cid)
            self._upsert_order(cid, "PROTECTION", pos["symbol"], O_CANCELED,
                               pos_id=pos_id)
            self._update_live_pos(pos_id, protection_status=P_CANCELED)
        except BinanceApiError as e:
            if e.is_unknown_order():
                # cancel/fill yarışı: emir zaten yok → nihai durumu sorgula
                st = None
                try:
                    q = self.exchange.query_algo_order(
                        symbol=pos["symbol"], client_algo_id=cid)
                    if isinstance(q, dict):
                        st = q.get("algoStatus") or q.get("status")
                except (ExchangeUnavailable, BinanceApiError):
                    pass
                st = st or O_TRIGGERED     # kanıt yok: tetiklenmiş kabul
                self._upsert_order(cid, "PROTECTION", pos["symbol"], st,
                                   pos_id=pos_id)
                self._update_live_pos(pos_id, protection_status=P_TRIGGERED
                                      if st == O_TRIGGERED else P_CANCELED)
            else:
                self.rm._event("PROTECTION_CANCEL_ERROR",
                               f"pos {pos_id} {cid} {e}")
        except UnknownResult as e:
            self._upsert_order(cid, "PROTECTION", pos["symbol"], O_UNKNOWN,
                               pos_id=pos_id, result={"error": str(e)})

    # ---- UNKNOWN / partial çözümleme (§2.3) ---------------------------------
    def _resolve_entry_order(self, cid, symbol):
        """ENTRY UNKNOWN/SUBMITTED → client id ile sorgu; körlemesine tekrar
        POST edilmez."""
        o = self._order_row(cid)
        if o is None or o["status"] not in (O_UNKNOWN, O_SUBMITTED, O_INTENT,
                                            O_PARTIAL):
            return
        try:
            resp = self.exchange.query_order(symbol, client_order_id=cid)
        except (ExchangeUnavailable, BinanceApiError) as e:
            if isinstance(e, BinanceApiError) and e.is_unknown_order():
                # emir hiç var olmadı (INTENT aşamasında kaldıysa) → temizle
                self._upsert_order(cid, "ENTRY", symbol, O_CANCELED,
                                   result={"code": e.code, "msg": e.msg})
            return
        status = resp.get("status")
        self._upsert_order(cid, "ENTRY", symbol, status,
                           exchange_id=str(resp.get("orderId")), result=resp)
        # terminal duruma ulaşmış ama KISMEN dolmuş giriş → gerçek fill'i
        # deftere işle (executedQty > 0); borsada sahipsiz pozisyon kalmasın
        if not o.get("pos_id") and status in (O_FILLED, O_PARTIAL, "EXPIRED",
                                              O_CANCELED) and \
                float(resp.get("executedQty") or 0) > 0:
            self._process_late_entry_fill(cid, resp, symbol)

    def _process_late_entry_fill(self, cid, resp, symbol):
        """Restart/UNKNOWN sonrası geç bulunan giriş fill'i — deftere işle ve
        koruma zincirini başlat."""
        params = json.loads(self._order_row(cid)["params"] or "{}")
        direction = 1 if params.get("side") == "BUY" else -1
        rec = self._on_entry_filled(cid, resp, symbol, direction, self._now())
        self._log("live_orders.jsonl", {"t": self._now(),
                                        "kind": "LATE_ENTRY_FILL",
                                        "client_id": cid, **rec})

    def _progress_exit(self, pos_id):
        """Bir pozisyonun çıkış akışını ilerlet (risk loop her tick)."""
        lp = self._live_pos_row(pos_id)
        if lp is None or lp["exit_status"] in (X_NONE, X_CONFIRMED):
            return
        pos = self.rm._pos(pos_id)
        cid = lp.get("exit_client_id")
        if lp["exit_status"] == X_UNCONFIRMED:
            # kesinti sonrası: taze positionRisk ile devam
            try:
                amt = self._exchange_amt(pos["symbol"])
            except ExchangeUnavailable:
                return
            if amt is None:
                return
            if abs(amt) < 1e-12:
                self._check_flat_and_finalize(pos_id)
            else:
                self._submit_exit(pos_id, abs(amt),
                                  lp.get("exit_reason") or "unconfirmed")
            return
        if lp["exit_status"] == X_UNKNOWN and cid:
            try:
                resp = self.exchange.query_order(pos["symbol"],
                                                 client_order_id=cid)
            except ExchangeUnavailable:
                return
            except BinanceApiError as e:
                if e.is_unknown_order():
                    # emir borsada HİÇ YOK (istek ulaşmamış) → intent'i kapat,
                    # positionRisk'e bak (körlemesine tekrar gönderme)
                    self._upsert_order(cid, "EXIT", pos["symbol"], O_CANCELED,
                                       pos_id=pos_id,
                                       result={"code": e.code,
                                               "msg": e.msg,
                                               "note": "never applied"})
                    try:
                        amt = self._exchange_amt(pos["symbol"])
                    except ExchangeUnavailable:
                        return
                    if amt is not None and abs(amt) < 1e-12:
                        self._check_flat_and_finalize(pos_id)
                    else:
                        self._update_live_pos(pos_id, exit_status=X_UNCONFIRMED)
                return
            status = resp.get("status")
            self._upsert_order(cid, "EXIT", pos["symbol"], status,
                               pos_id=pos_id, result=resp)
            if status == O_FILLED:
                if not self._check_flat_and_finalize(pos_id):
                    # doldu ama hâlâ açık → kalan için YENİ intent (doğrulanmış)
                    try:
                        amt = self._exchange_amt(pos["symbol"])
                    except ExchangeUnavailable:
                        return
                    if amt and abs(amt) >= 1e-12:
                        self._submit_exit(pos_id, abs(amt), "partial_remainder")
            elif status == O_CANCELED:
                self._update_live_pos(pos_id, exit_status=X_UNCONFIRMED)
            return
        # PENDING/PARTIAL: pozisyon hâlâ açık mı → kalan miktar takibi
        try:
            amt = self._exchange_amt(pos["symbol"])
        except ExchangeUnavailable:
            return
        if amt is None:
            return
        if abs(amt) < 1e-12:
            self._check_flat_and_finalize(pos_id)
            return
        # kalan açık: yalnızca mevcut çıkış emri TERMİNAL durumdaysa VE
        # DOĞRULANMIŞ kalan için yeni idempotent intent aç (§2.3 — körlemesine
        # yeniden gönderme; emir yoldaysa bekle)
        o = self._order_row(cid) if cid else None
        if o is not None and o["status"] == O_PARTIAL:
            # kısmi dolmuş emrin kalanı iptal oldu mu → sorgula
            try:
                resp = self.exchange.query_order(pos["symbol"],
                                                 client_order_id=cid)
                self._upsert_order(cid, "EXIT", pos["symbol"],
                                   resp.get("status"), pos_id=pos_id,
                                   result=resp)
                o = self._order_row(cid)
            except (ExchangeUnavailable, BinanceApiError):
                return
        in_flight = o is not None and o["status"] in (
            "INTENT", "SUBMITTED", "UNKNOWN", "NEW", "PARTIALLY_FILLED")
        if not in_flight:
            self._submit_exit(pos_id, abs(amt), "partial_remainder")

    # =========================================================================
    # TOPLAM HALT — BÜTÜN POZİSYONLARI KAPATMA (§3)
    # =========================================================================
    def run_halt_sequence(self):
        """-10 USDT HALT zinciri. Her risk tick ilerler; restart güvenli
        (durum kalıcı). Bilinmeyen emir/pozisyon varsa HALT DEVAM EDER."""
        now = self._now()
        state = self.halt_state
        halted = self.rm.status == STATUS_HALTED
        if state == HALT_ACTIVE:
            if halted:
                self._transition_halt(HALT_INTENT, "toplam net zarar -10")
            return
        if state == HALT_INTENT:
            # yalnız CANARY'YE AİT bekleyen giriş emirlerini iptal et (§3 adım
            # 4-5); sahiplik kanıtı: C281 öneki
            for row in self._db_execute(
                    "SELECT client_id, symbol, status, pos_id FROM "
                    "live_orders WHERE kind='ENTRY' AND status IN "
                    "('INTENT','SUBMITTED','UNKNOWN','PARTIALLY_FILLED')"
                    ).fetchall():
                cid, sym, st, pos_id = row
                if st in (O_UNKNOWN, O_SUBMITTED):
                    try:
                        resp = self.exchange.query_order(sym,
                                                         client_order_id=cid)
                        if resp.get("status") == O_FILLED:
                            self._upsert_order(cid, "ENTRY", sym, O_FILLED,
                                               result=resp)
                            if not pos_id:
                                # UNKNOWN girişin geç fill'i → DEFTERE İŞLE
                                # (pozisyon EXITING_ALL'da çıkışa girecek)
                                self._process_late_entry_fill(cid, resp, sym)
                            continue
                    except (ExchangeUnavailable, BinanceApiError):
                        pass                   # çözülemedi → HALT devam eder
                try:
                    self.exchange.cancel_order(sym, client_order_id=cid)
                    self._upsert_order(cid, "ENTRY", sym, O_CANCELED)
                except BinanceApiError as e:
                    if e.is_unknown_order():
                        # cancel/fill yarışı → nihai durum sorgula
                        try:
                            resp = self.exchange.query_order(
                                sym, client_order_id=cid)
                            self._upsert_order(cid, "ENTRY", sym,
                                               resp.get("status"),
                                               result=resp)
                            if resp.get("status") == O_FILLED and not pos_id:
                                self._process_late_entry_fill(cid, resp, sym)
                        except (ExchangeUnavailable, BinanceApiError):
                            return            # UNKNOWN → HALT devam eder
                    else:
                        return                # iptal başarısız → HALT devam
                except (UnknownResult, ExchangeUnavailable):
                    return                    # sonuç bilinmiyor → HALT devam
            self._transition_halt(HALT_RISK, "giriş emirleri temizlendi")
            return
        if state == HALT_RISK:
            self._transition_halt(HALT_EXITING_ALL, "tüm pozisyonlar çıkışa")
            return
        if state == HALT_EXITING_ALL:
            pending = False
            for pos in self.rm.open_positions():
                lp = self._live_pos_row(pos["pos_id"])
                st = lp["exit_status"] if lp else X_NONE
                if st == X_NONE:
                    self.initiate_risk_exit(pos["pos_id"], "TOTAL_HALT")
                    pending = True
                elif st != X_CONFIRMED:
                    self._progress_exit(pos["pos_id"])
                    pending = True
            if not pending:
                self._transition_halt(HALT_RECON, "tüm çıkışlar doğrulandı")
            return
        if state == HALT_RECON:
            # tüm semboller flat + kanca emir yok (yeniden sorgu — §3 adım 11)
            try:
                pr_map = self._position_risk_map()
                for pos in self.rm.open_positions():
                    amt = self._exchange_amt(pos["symbol"], pr_map)
                    if amt and abs(amt) >= 1e-12:
                        self._transition_halt(HALT_EXITING_ALL,
                                              "recon: pozisyon hâlâ açık")
                        return
                # defterde olmayan ama C281 girişinden doğan pozisyon var mı?
                # (late fill işlenmemiş → EXITING_ALL'a dön)
                for sym, row in pr_map.items():
                    if abs(float(row.get("positionAmt", 0) or 0)) < 1e-12:
                        continue
                    if any(p["symbol"] == sym
                           for p in self.rm.open_positions()):
                        continue
                    unledgered = self._db_execute(
                        "SELECT client_id FROM live_orders WHERE kind="
                        "'ENTRY' AND symbol=? AND status IN ('FILLED',"
                        "'PARTIALLY_FILLED') AND pos_id IS NULL",
                        (sym,)).fetchone()
                    if unledgered:
                        self._transition_halt(HALT_EXITING_ALL,
                                              f"recon: {sym} late-fill "
                                              "pozisyonu defterde yok")
                        return
                    # sahiplik kanıtı YOK → canary dışı: DOKUNMA (yalnız not)
                    self.rm._event("HALT_UNOWNED_POSITION_NOTED",
                                   f"{sym} canary dışı pozisyon — dokunulmadı")
                algos = self.exchange.open_algo_orders()
                ours_open = [o for o in algos if (o.get("clientAlgoId")
                             or "").startswith(CLIENT_PREFIX)]
                # flat pozisyonların koruma artıkları → güvenli temizle
                for o in ours_open:
                    cid = o["clientAlgoId"]
                    prow = self._db_execute(
                        "SELECT pos_id FROM live_orders WHERE client_id=?",
                        (cid,)).fetchone()
                    if prow:
                        pos_id = prow[0]
                        if not self.rm.open_positions() or not any(
                                p["pos_id"] == pos_id
                                for p in self.rm.open_positions()):
                            try:
                                self.exchange.cancel_algo_order(
                                    symbol=o.get("symbol"),
                                    client_algo_id=cid)
                            except (BinanceApiError, UnknownResult,
                                    ExchangeUnavailable):
                                pass
                unknown_orders = self._db_execute(
                    "SELECT COUNT(*) FROM live_orders WHERE status IN "
                    "('UNKNOWN','INTENT','SUBMITTED')").fetchone()[0]
                if unknown_orders:
                    return                  # UNKNOWN var → HALT devam eder
                self._transition_halt(HALT_FLAT_VERIFIED,
                                      "tüm semboller flat doğrulandı")
            except ExchangeUnavailable:
                pass                          # kesinti → beklemeye devam
            return
        if state == HALT_FLAT_VERIFIED:
            self._transition_halt(HALT_LOCKED,
                                  "HALT kilitli — yalnız operatör açar")
            return
        # HALT_LOCKED: son durum — otomatik hiçbir şey sıfırlamaz

    def operator_release(self):
        """HALT serbest bırakma — YALNIZ operatör aracı (kod otomatik
        ÇAĞIRMAZ; rm.operator_reset ile birlikte kullanılır)."""
        with self._lock:
            self._set_state("halt_state", HALT_ACTIVE)
        self.rm._event("OPERATOR_RELEASE", "HALT state machine operatör "
                                       "onayıyla sıfırlandı")

    # =========================================================================
    # RISK LOOP (§4.2 — strategy loop'tan BAĞIMSIZ)
    # =========================================================================
    def risk_tick(self, now_ms=None):
        now = now_ms or self._now()
        cyc = {"t": now, "kind": "RISK_TICK",
               "entry_paused": self.entry_paused}   # BLOK30 izleme
        # 1) Mark Price (REST_SAMPLED) — feed sağlık durumu kalıcı
        try:
            rows = self.exchange.premium_index()
            marks_n = 0
            for r in rows:
                if r.get("symbol") in self.symbols:
                    self.rm.update_mark(r["symbol"],
                                        float(r["markPrice"]),
                                        int(r["time"]))
                    marks_n += 1
            if self._get_state("feed_down_ts"):
                self._set_state("feed_down_ts", "")          # temizle
                self._set_state("feed_recovered_pending_reconcile", 1)
                self.rm._event("FEED_RECOVERED", "mark akışı geri döndü — "
                               "mutabakat gerekli")
            cyc["marks_n"] = marks_n
        except ExchangeUnavailable as e:
            if not self._get_state("feed_down_ts"):
                self._set_state("feed_down_ts", now)
                self.rm._event("FEED_OUTAGE", str(e))   # kalıcı olay (§9)
            cyc["feed_down"] = True
            self._log("live_cycles.jsonl", cyc)
            return cyc                     # fail-closed: hesap yok, iptal yok
        # 2) feed geri döndüyse ÖNCE mutabakat (§5: normalleşmeden risk yok)
        if self._get_state("feed_recovered_pending_reconcile") == "1":
            try:
                pr_map = self._position_risk_map()
                self._set_state("feed_recovered_pending_reconcile", 0)
                self._set_state("reconciled_ok", 1)
                self.rm._event("FEED_RECONCILED", "kesinti sonrası mutabakat "
                               "tamam")
            except ExchangeUnavailable:
                cyc["reconcile_pending"] = True
                self._log("live_cycles.jsonl", cyc)
                return cyc
        # 3) funding mutabakatı (gerçek income kayıtları — ~60s'de bir)
        if now - int(self._get_state("last_funding_sync") or 0) > 60_000:
            self._sync_funding(now)
        # 3.5) askıda kalan giriş emirlerini çöz (§2.3: client id ile sorgu;
        # çözülmeden yeni giriş zinciri ilerleyemez)
        for row in self._db_execute(
                "SELECT client_id, symbol FROM live_orders WHERE kind="
                "'ENTRY' AND status IN ('UNKNOWN','SUBMITTED',"
                "'PARTIALLY_FILLED')").fetchall():
            self._resolve_entry_order(row[0], row[1])
        # 4) limit değerlendirme (SAFE_EXIT / HALT tetikleri)
        try:
            actions = self.rm.enforce(now)
        except Exception as e:
            actions = [{"action": "ENFORCE_ERROR", "error": str(e)}]
        cyc["risk_actions"] = actions
        for a in actions:
            if isinstance(a, dict) and a.get("action") == "SAFE_EXIT":
                self.initiate_risk_exit(a["pos_id"], "POSITION_LOSS_LIMIT")
        # 5) HALT zinciri (varsa ilerlet; tetik enforce'tan gelmiş olabilir)
        if self.rm.status == STATUS_HALTED and self.halt_state == HALT_ACTIVE:
            self._transition_halt(HALT_INTENT,
                                  f"toplam net zarar -10 ({self.rm.status})")
        if self.halt_state != HALT_ACTIVE:
            self.run_halt_sequence()
            cyc["halt_state"] = self.halt_state
        # 6) çıkış akışları + koruma doğrulama + drift
        for pos in self.rm.open_positions():
            self._progress_exit(pos["pos_id"])
        self._verify_protections(now)
        try:
            self._drift_check(now)
        except ExchangeUnavailable:
            cyc["drift_unverifiable"] = True   # F-c: okunamadı → blok nedeni
        cyc["open_positions"] = len(self.rm.open_positions())
        cyc["rm_status"] = self.rm.status
        self._log("live_cycles.jsonl", cyc)
        return cyc

    def _sync_funding(self, now):
        """Gerçek FUNDING_FEE income kayıtlarını rm defterine yaz
        (INSERT OR REPLACE — çift sayım yok)."""
        for pos in self.rm.open_positions():
            try:
                rows = self.exchange.income(symbol=pos["symbol"],
                                            income_type="FUNDING_FEE",
                                            start_ms=pos["entry_ts"],
                                            limit=100)
            except (ExchangeUnavailable, BinanceApiError):
                continue
            for r in rows:
                ts = int(r.get("time", 0))
                if ts <= pos["entry_ts"]:
                    continue
                # Binance income: pozitif = alınmış, negatif = ödenmiş.
                # rm funding amount semantiği AYNI yönde (pozitif = alınmış,
                # negatif = ödenmiş/zarar) → doğrudan yaz
                income = float(r.get("income", 0) or 0)
                self.rm.note_funding(pos["pos_id"], ts, income)
        self._set_state("last_funding_sync", now)

    def _verify_protections(self, now):
        """Açık pozisyonların koruma emirleri borsada hâlâ açık mı?
        (F-c: doğrulanamıyorsa YENİ GİRİŞ bloke olur; hiçbir şey iptal
        edilmez.) PENDING/UNKNOWN koruma → çöz ve doğrula; kaybolduysa →
        yeniden yerleştir (taze positionRisk okuyarak — F-g); başarısızsa
        çıkış."""
        if now - int(self._get_state("last_protection_check") or 0) < 30_000:
            return
        self._set_state("last_protection_check", now)
        for pos in self.rm.open_positions():
            lp = self._live_pos_row(pos["pos_id"])
            if lp is None:
                continue
            cid = lp.get("protection_client_id")
            if lp["protection_status"] == P_PENDING and cid:
                o = self._order_row(cid)
                if o and o["status"] == O_REJECTED:
                    self._update_live_pos(pos["pos_id"],
                                          protection_status=P_FAILED)
                    self.initiate_risk_exit(pos["pos_id"],
                                            "PROTECTION_REJECTED")
                    continue
                if o and o["status"] in (O_INTENT, O_UNKNOWN, O_SUBMITTED):
                    self._verify_protection_order(pos["pos_id"], cid)
                    continue
            if lp["protection_status"] != P_VERIFIED:
                continue
            try:
                still = any(o.get("clientAlgoId") == cid
                            for o in self.exchange.open_algo_orders(
                                symbol=pos["symbol"]))
                self._set_state("protection_unverifiable", 0)  # okundu → çöz
            except ExchangeUnavailable:
                self._set_state("protection_unverifiable", 1)  # F-c
                continue
            if not still:
                # tetiklenmiş olabilir → pozisyon durumu
                try:
                    amt = self._exchange_amt(pos["symbol"])
                except ExchangeUnavailable:
                    continue
                if amt is None or abs(amt) < 1e-12:
                    self._update_live_pos(pos["pos_id"],
                                          protection_status=P_TRIGGERED)
                    self._check_flat_and_finalize(pos["pos_id"])
                else:
                    # koruma kaybolmuş ama pozisyon açık → YENİDEN yerleştir
                    self._update_live_pos(pos["pos_id"],
                                          protection_status=P_PENDING)
                    res = self._place_protection(pos["pos_id"], now)
                    if res.get("status") == P_FAILED:
                        self.initiate_risk_exit(pos["pos_id"],
                                                "PROTECTION_LOST")

    def _drift_check(self, now):
        """Defter ↔ borsa pozisyon tutarlılığı (F-c/F-d: okunamayan durum
        ASLA 'pozisyon yok' okunmaz; drift → giriş bloğu)."""
        pr_map = self._position_risk_map()
        drift = []
        for pos in self.rm.open_positions():
            amt = self._exchange_amt(pos["symbol"], pr_map)
            if amt is None:
                continue
            if abs(amt) < 1e-12:
                resolved = self._resolve_unexpected_flat(pos, pr_map)
                if not resolved:
                    drift.append({"symbol": pos["symbol"],
                                  "kind": "FLAT_UNRESOLVED"})
            elif abs(pos["direction"] * pos["units"] - amt) > \
                    abs(amt) * 1e-6 + 1e-12:
                drift.append({"symbol": pos["symbol"], "kind": "SIZE_MISMATCH",
                              "ledger": pos["direction"] * pos["units"],
                              "exchange": amt})
        for sym, row in pr_map.items():
            if abs(float(row.get("positionAmt", 0) or 0)) >= 1e-12 and \
                    not any(p["symbol"] == sym
                            for p in self.rm.open_positions()):
                drift.append({"symbol": sym, "kind": "UNOWNED_POSITION"})
        if drift:
            self._set_state("unowned_position_seen", 1)
            self.rm._event("POSITION_DRIFT", json.dumps(drift)[:300])
        else:
            # temiz durumda latch'i çöz (aksi halde giriş sonsuza dek bloke
            # kalırdı); yalnız bu tick'in kanıtıyla sıfırlanır
            self._set_state("unowned_position_seen", 0)
        return drift

    # =========================================================================
    # STRATEGY LOOP (§4.1 — 300s H1; risk loop'u BEKLEMEZ)
    # =========================================================================
    # ========================================================================
    # GÖREV §2 (2026-11-26): CANARY AI BRAIN → EXECUTION yolu (BRAIN_PRIMARY)
    #
    # Beynin BRAIN_DB'deki son döngüsündeki CANDIDATE kayıtları (LONG ve
    # SHORT ayrı) giriş zincirine taşınır. Her aday kanonik try_entry
    # zincirinden geçer: ENTRY PAUSE, HALT, yön kilidi, T0, mutabakat,
    # sahiplik, likidasyon, ilk-pozisyon, boyutlama (borsa filtreleri),
    # CROSS risk kapısı (AccountRiskGate + canary_risk bütçeleri), flock
    # giriş kapısı, idempotency (brainsig:{decision_id}).
    # Beyin önerisi emir DEĞİLDİR: suggested_notional yalnız politika
    # tavanına kadar kabul edilir; son kararı bağımsız risk kapısı verir.
    # ========================================================================
    def _brain_tick(self, now_ms):
        """Bir strateji döngüsünde beyin adaylarını değerlendirir."""
        fresh = self.brain_source.fresh_candidates(now_ms)
        tick = {"t": now_ms, "kind": "BRAIN_TICK",
                "signal_source": SIGNAL_SOURCE_BRAIN,
                "brain_cycle_id": fresh.get("cycle_id"),
                "brain_cycle_ts": fresh.get("cycle_ts"),
                "brain_age_ms": fresh.get("age_ms"),
                "brain_ok": fresh.get("ok"),
                "brain_reasons": fresh.get("reasons"),
                "candidates_seen": len(fresh.get("candidates") or []),
                "evaluated": []}
        self._last_brain_tick = tick
        out = []
        if not fresh.get("ok"):
            # fail-closed: taze/doğrulanmış aday YOK → emir YOK (NO_TRADE
            # beyin kararındadır; burada sinyal İCAT EDİLMEZ)
            self._log("live_orders.jsonl", tick)
            return out
        # deterministik sıra: decision_id (sembol+yön) — sabit Top-N YOK,
        # bütün adaylar değerlendirilir; sınırı risk kapısı koyar
        for dec in sorted(fresh["candidates"],
                          key=lambda d: d.decision_id):
            recs = self._apply_brain_decision(dec, now_ms)
            out.extend(recs)
            tick["evaluated"].append({
                "decision_id": dec.decision_id, "symbol": dec.symbol,
                "direction": dec.direction,
                "result": recs[0].get("result") if recs else None,
                "blockers": recs[0].get("blockers") if recs else None})
        self._log("live_orders.jsonl", tick)
        return out

    def _apply_brain_decision(self, dec, now_ms):
        """Tek beyin adayını giriş zincirine taşır (dedup → boyut doğrulama
        → taze fiyat → sapma kapısı → try_entry)."""
        rec = {"t": now_ms, "kind": "BRAIN_DECISION", "execute": True,
               "decision_id": dec.decision_id, "symbol": dec.symbol,
               "direction": dec.direction, "brain_ts": dec.ts,
               "cycle_id": dec.cycle_id, "model_version": dec.model_version,
               "signal_source": SIGNAL_SOURCE_BRAIN,
               "expected_net_return_pct": dec.expected_net_return_pct,
               "expected_transaction_costs_pct":
                   dec.expected_transaction_costs_pct,
               "calibrated_probability": dec.calibrated_probability,
               "probability_is_calibrated": dec.probability_is_calibrated,
               "prediction_uncertainty_pct": dec.prediction_uncertainty_pct,
               "stop_distance_pct": dec.stop_distance_pct,
               "suggested_notional": dec.suggested_notional,
               "suggested_quantity": dec.suggested_quantity,
               "rejection_reasons": list(dec.rejection_reasons),
               "brain_price": dec.price}
        out = [rec]
        # idempotency (erken kontrol): bu karar daha önce DEĞERLENDİRİLDİ
        # mi? (try_entry dedup'ıyla AYNI anahtar — bir kez değerlendirilen
        # karar — ister emir çıksın ister bloklansın ister tüketilsin —
        # bir daha emir zincirine GİRMEZ; yeni sinyal yeni döngüdedir)
        with self._lock:
            if self._get_state(f"brainsig:{dec.decision_id}") is not None:
                rec["result"] = "DUPLICATE_BRAIN_DECISION"
                self._log("live_orders.jsonl", rec)
                return out
        # beyin kaydındaki ret nedenleri varsa aday zaten geçersiz
        if dec.rejection_reasons:
            rec["result"] = "BLOCKED"
            rec["blockers"] = [f"BRAIN_REJECTION:{r}"
                               for r in dec.rejection_reasons]
            self._log("live_orders.jsonl", rec)
            return out
        # boyutlama önerisi yok/geçersiz → fail-closed (AI önerisi olmadan
        # politika boyutu icat EDİLMEZ — öneri ile emir ayrımı §2)
        sizing = dec.sizing or {}
        if not sizing.get("ok") or not (dec.suggested_notional or 0) > 0:
            rec["result"] = "BLOCKED"
            rec["blockers"] = ["BRAIN_SIZING_NOT_OK:"
                               + ",".join(sizing.get("reasons") or
                                          ["sizing_yok"])]
            self._log("live_orders.jsonl", rec)
            self.rm._event("ENTRY_BLOCKED",
                           f"{dec.symbol} beyin boyutlamasi gecersiz "
                           f"({dec.decision_id})")
            return out
        # taze icra fiyatı (BUY→ask, SELL→bid; freshness kapılı)
        px, pmeta = self._executable_price(dec.symbol, dec.direction, now_ms)
        if px is None:
            rec["result"] = "PRICE_UNAVAILABLE"
            rec["blockers"] = [f"bookTicker: {pmeta}"]
            self._log("live_orders.jsonl", rec)
            return out
        # sapma kapısı: taze fiyat beyin tarama fiyatından çok uçtuysa
        # karar ömrü dolmuş sayılır → KALICI olarak tüketilir (eski sinyal
        # oynatılmaz — MISSED_FLIP semantiği; sonraki döngü taze karar verir)
        if dec.price and \
                abs(px - dec.price) / dec.price > SIGNAL_PRICE_DEVIATION_MAX:
            dev = abs(px - dec.price) / dec.price
            self._set_state(f"brainsig:{dec.decision_id}",
                            str(dec.direction))
            rec["result"] = "BRAIN_SIGNAL_STALE"
            rec["executable_px"] = px
            rec["blockers"] = [f"fiyat sapması: taze {px:.6f} vs beyin "
                               f"{dec.price:.6f} (%{dev * 100:.2f} > "
                               f"{SIGNAL_PRICE_DEVIATION_MAX * 100:.0f}) — "
                               "karar kalıcı olarak tüketildi"]
            self._log("live_orders.jsonl", rec)
            return out
        rec["executable_px"] = px
        recs = self.try_entry(dec.symbol, dec.direction, px, dec.ts,
                              now_ms, signal_px=dec.price,
                              decision_id=dec.decision_id,
                              notional=dec.suggested_notional)
        # try_entry sonucu BRAIN_DECISION kaydına da bağlanır (denetim
        # izi: beyin kaydı → execution değerlendirmesi tek kayıtta görünür)
        rec["result"] = recs[0].get("result")
        rec["blockers"] = recs[0].get("blockers")
        rec["qty"] = recs[0].get("qty")
        out.extend(recs)
        return out

    def strategy_tick(self, now_ms=None, klines_by_sym=None):
        now = now_ms or self._now()
        # GÖREV §1/§4 (2026-11-26): BRAIN_PRIMARY modunda giriş adayları
        # YALNIZ yeni beyinden gelir — eski 8 sembollük H1 strateji döngüsü
        # (kline çekimi + flip/quality değerlendirmesi) ÇALIŞMAZ, dolayısıyla
        # eski strateji bağımsız emir AÇAMAZ. Risk döngüsü, çıkışlar, koruma
        # doğrulaması, HALT/PAUSE ve mutabakat aynen sürer (risk_tick).
        if self.signal_source == SIGNAL_SOURCE_BRAIN:
            out = self._brain_tick(now)
            seq = int(self._get_state("strategy_cycle_seq") or 0) + 1
            self._set_state("strategy_cycle_seq", seq)
            self._log("strategy_cycles.jsonl",
                      {"t": now, "kind": "STRATEGY_CYCLE", "cycle_id": seq,
                       "written_by": "canary-strategy",
                       "signal_source": SIGNAL_SOURCE_BRAIN,
                       "entry_policy": self.entry_policy,
                       "entry_paused": self.entry_paused,
                       "halt_state": self.halt_state,
                       "rm_status": self.rm.status,
                       "universe_symbols": len(self.symbols),
                       "brain": self._last_brain_tick})
            return out
        fetch_errors = {}
        if klines_by_sym is None:
            klines_by_sym = {}
            for s in self.symbols:
                try:
                    rows = self.exchange.klines(s)
                    klines_by_sym[s] = [(int(k[0]), float(k[1]),
                                         float(k[4]), float(k[7]))
                                        for k in rows]
                except ExchangeUnavailable as e:
                    fetch_errors[s] = f"{type(e).__name__}: {e}"
                except Exception as e:
                    # parse/beklenmeyen hata da sembol bazında fail-closed —
                    # eskiden sessizce yutuluyordu (BLOK31 §5)
                    fetch_errors[s] = f"{type(e).__name__}: {e}"
        events, states = self._evaluate(now, klines_by_sym, fetch_errors)
        # BLOK32: hedef yönü değişmiş açık pozisyonları çıkışa al (QUALITY
        # politikası — flip tüketildiğinde koşulsuz; kalibrasyon semantiği).
        # Olay döngüsünden ÖNCE çalışır: reversal protokolünün çıkış bacağı.
        if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
            self._quality_target_exits(now, states)
        out = []
        for (symbol, direction, ref_px, bar_ts, qv, signal_px) in events:
            # strateji çıkışı (flip'in kapanış bacağı — risk AZALTAN,
            # HALT'ta da serbest; reduce-only)
            for pos in self.rm.open_positions():
                if pos["symbol"] == symbol and \
                        pos["direction"] == -direction and direction != 0:
                    lp = self._live_pos_row(pos["pos_id"])
                    if lp and lp["exit_status"] in (X_NONE, X_CONFIRMED):
                        self.initiate_risk_exit(pos["pos_id"],
                                                "H1_FLIP_STRATEGY_EXIT")
            if direction != 0:
                # BLOK32 reversal protokolü (yalnız QUALITY): aynı sembolde
                # açık pozisyon varken ters yönde emir ASLA gönderilmez —
                # önce reduce-only çıkış, borsada flat doğrulama, koruma/
                # defter mutabakatı; yeni yön SONRAKİ barlarda değerlendirilir
                # (§7 "Korumasız veya çıkış durumu belirsiz pozisyon üzerine
                # yeni ters yönlü emir gönderme").
                open_on_sym = [p for p in self.rm.open_positions()
                               if p["symbol"] == symbol]
                if self.entry_policy == cq.ENTRY_POLICY_QUALITY and open_on_sym:
                    if states.get(symbol) is not None:
                        states[symbol]["entry_result"] = "REVERSAL_EXIT_FIRST"
                        states[symbol]["final_decision"] = \
                            "REVERSAL_EXIT_FIRST"
                        states[symbol]["decision_reason"] = (
                            "quality politika: aynı sembolde açık pozisyon — "
                            "önce çıkış + flat doğrulaması, sonra yeni yön")
                    continue
                recs = self.try_entry(symbol, direction, ref_px, bar_ts,
                                      now, qv, signal_px=signal_px)
                out.extend(recs)
                if states.get(symbol) is not None:
                    res = recs[0].get("result")
                    states[symbol]["entry_result"] = res
                    states[symbol]["entry_blockers"] = recs[0].get("blockers")
                    states[symbol]["sizing_result"] = recs[0].get("qty")
                    prot = recs[0].get("protection")
                    if isinstance(prot, dict):
                        states[symbol]["protection_status"] = prot.get(
                            "status")
                    # §8 final_decision: giriş zinciri sonucu etikete bağlanır
                    if res == "FILLED":
                        states[symbol]["final_decision"] = "FILL_CONFIRMED"
                    elif isinstance(res, str) and res.startswith("REJECTED"):
                        states[symbol]["final_decision"] = "ORDER_REJECTED"
                    elif res == "BLOCKED":
                        # BLOK32 (yalnız QUALITY): blok nedeni ayrıştırılır —
                        # operatör PAUSE'u ve global HALT kendi karar
                        # durumlarıyla görünür (§5 karar durumları listesi)
                        if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
                            bl = " ".join(recs[0].get("blockers") or [])
                            if "ENTRY_PAUSED" in bl:
                                states[symbol]["final_decision"] = \
                                    "ENTRY_PAUSED"
                            elif "HALT" in bl:
                                states[symbol]["final_decision"] = \
                                    "GLOBAL_HALT"
                            elif "DIRECTION_DISABLED" in bl:
                                # BLOK32 görev-2: yön kilidi — emir
                                # gönderilmedi, karar yönsel etiketle görünür
                                states[symbol]["final_decision"] = \
                                    _direction_disabled_label(direction)
                            else:
                                states[symbol]["final_decision"] = \
                                    "ENTRY_BLOCKED"
                        else:
                            states[symbol]["final_decision"] = "ENTRY_BLOCKED"
                    elif isinstance(res, str) and "UNKNOWN" in res:
                        states[symbol]["final_decision"] = "ENTRY_UNKNOWN"
                    else:
                        states[symbol]["final_decision"] = (
                            "ORDER_SUBMITTED" if self.entry_policy ==
                            cq.ENTRY_POLICY_QUALITY else "ENTRY_ATTEMPTED")
                    # BLOK32: emir ÇIKTI ise episode ENGAGED (kalıcı — restart
                    # sonrası da aynı sinyal episode'u tekrar emir ÜRETEMEZ).
                    # BLOCKED/DUPLICATE kalırsa ARMED kalır: sonraki barlar
                    # engel kalkınca tekrar değerlendirir (hedef-yön takibi).
                    if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
                        order_out = (res == "FILLED"
                                     or (isinstance(res, str) and (
                                         res.startswith("REJECTED")
                                         or "UNKNOWN" in res
                                         or res.startswith("entry status"))))
                        if order_out:
                            self._episode_set(symbol, direction, True)
        # BLOK31 §5: her strateji döngüsü GÖRÜNÜR — başarı/başarısızlık ve
        # sembol bazlı durum ayrık kayda geçer (thread'in bu kaydı yazması
        # döngünün çalıştığının kendisidir). BLOK31.1 §8: döngü-düzeyi
        # kapı durumları (PAUSE/HALT/risk/mutabakat/feed) da kayıtta.
        seq = int(self._get_state("strategy_cycle_seq") or 0) + 1
        self._set_state("strategy_cycle_seq", seq)
        self._log("strategy_cycles.jsonl",
                  {"t": now, "kind": "STRATEGY_CYCLE", "cycle_id": seq,
                   "written_by": "canary-strategy",
                   "entry_policy": self.entry_policy,
                   "entry_paused": self.entry_paused,
                   "halt_state": self.halt_state,
                   "rm_status": self.rm.status,
                   "reconciliation_status":
                       str(self._get_state("reconciled_ok") or "0"),
                   "feed_status": ("DOWN" if self._get_state("feed_down_ts")
                                   else "OK"),
                   "fetch_errors": fetch_errors,
                   "symbols": [states[s] for s in self.symbols
                               if states.get(s) is not None]})
        return out

    def _evaluate(self, now_ms, klines_by_sym, fetch_errors=None):
        """Shadow/paper semantiği: warmup replay execute'siz; YENİ barların
        sinyali KARAR ANINDA (bar kapanışı = sonraki bar açılışı) taze icra
        fiyatıyla değerlendirilir (BLOK31); kalıcı last_bar dedup;
        penceresi geçen flip MISSED (fail-closed). (symbol, direction,
        px, karar barı ts, qv, sinyal kapanışı) olayları + sembol bazlı
        durum haritası döner.

        BLOK31.1 düzeltmeleri:
          - `warmup` log alanının semantiği: true = warmup HENÜZ tamamlan-
            mamış (eskiden `s in _warmed` yazılıyordu — tamamlanmış semboller
            için de true okunuyordu). Tamamlanma `warmup_complete` /
            `warmup_state` alanlarından açıkça görünür; eksikse `warmup_missing_bars`
            + `warmup_reason` söyler. Engine durumu DEĞİŞTİRİLMEDİ — yalnızca
            doğru etiketlendirme.
          - `last_processed_bar_before` / `_after`: döngü kaydı artık işleme
            öncesi VE sonrası durumu taşır (eskiden yalnızca öncesi yazılıp
            sonrası sanılıyordu — cycle35 tutarsızlığının kök nedeni).
          - kanonik §7 bar sırası (BLOK31.1): canlı barda end_bar'dan ÖNCE,
            o barın açılışında due olan flip pop edilir. İcra penceresi
            (karar barından sonraki bar) kapanmışsa flip MISSED tüketilir —
            eskiden end_bar önce çalıştığı için momentum geri dönerse due
            flip sessizce iptal oluyor, engine dir'i kanonik akıştan
            sapabiliyordu.
        """
        decisions = []
        states = {}
        warmup_events, live_events = [], []
        attempted = set()
        newly_warmed = set()
        for s in self.symbols:
            kl = klines_by_sym.get(s)
            if kl is None:
                states[s] = {"symbol": s, "interval": "4h",
                             "engine_direction_before":
                                 self.engine.direction(s)}
                continue
            if not kl:
                # boş kline yanıtı veri hatasıdır — NO_NEW_CLOSED_BAR olarak
                # sessizce geçilmez (§8: veri hatası ayrıştırılır, fail-closed)
                states[s] = {"symbol": s, "interval": "4h",
                             "engine_direction_before":
                                 self.engine.direction(s),
                             "decision": "DATA_FETCH_ERROR",
                             "decision_reason":
                                 "kline yanıtı boş (veri hatası — fail-closed)"}
                continue
            last = self._last_bar.get(s)
            cold = s not in self._warmed
            n_new = 0
            latest_closed = None
            for (ot, o, c, qv) in kl:
                if ot + BAR_MS > now_ms:
                    continue                      # açık mum ASLA girdi değil
                latest_closed = ot if latest_closed is None \
                    else max(latest_closed, ot)
                if last is not None and ot <= last and not cold:
                    continue
                if cold and (last is None or ot <= last):
                    warmup_events.append((ot, s, o, c, qv))
                else:
                    live_events.append((ot, s, o, c, qv))
                    n_new += 1
            states[s] = {"symbol": s, "interval": "4h",
                         "bar_count": len(kl), "new_bar_count": n_new,
                         "latest_closed_bar": latest_closed,
                         "last_processed_bar_before": last,
                         "engine_direction_before":
                             self.engine.direction(s)}
        warmup_events.sort(key=lambda e: (e[0], e[1]))
        live_events.sort(key=lambda e: (e[0], e[1]))   # deterministik seçim:
        # kronolojik (bar_ts, symbol) sıra + limit kapısı — var olan,
        # test edilmiş shadow politikası (§7: yeni kural icat edilmedi)
        warm_last = {}
        for (ot, s, o, c, qv) in warmup_events:
            self.engine.begin_bar(s, o)
            self.engine.end_bar(s, ot, c)
            # BLOK32: kalite modelinin hacim/sigma penceresi warmup replay
            # ile kurulur (restart sonrası geçmiş pencere kendini yeniden
            # kurar — ek kalıcı durum YOK; episode/sig anahtarları DB'de)
            if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
                S = self.engine._state(s)
                self.quality.feed(s, qv, math.sqrt(S["var"])
                                  if S["var"] is not None and S["var"] > 0
                                  else None)
            warm_last[s] = ot
        for s, ot in warm_last.items():
            if s not in self._warmed:
                newly_warmed.add(s)
            self._warmed.add(s)
            self._set_last(s, ot)
        qv_by = {}
        for (ot, s, o, c, qv) in live_events:
            # BLOK32: bu yinelemede bu sembol için flip olayı ÜRETİLDİ mi?
            # (bar açılışındaki kanonik deneme VEYA end_bar sonrası deneme
            # — hangisinden gelirse gelsin, o sembolün bu tikteki giriş
            # kararı flip yolunun tekelindedir; kalite değerlendirmesi
            # yinelenmez)
            sym_flip_fired = False
            # KANONİK §7 SIRASI (BLOK31.1): bu barın AÇILIŞINDA (ot) due
            # olan flip, end_bar'dan ÖNCE nihai olarak çözülür — ya taze
            # fiyatla İCRA ya da MISSED (fail-closed). end_bar'ın sig!=dir
            # karşılaştırması ilerlememiş dir üzerinden yapıldığı için bu
            # koruma olmadan momentum geri dönerse due flip SESSİZCE iptal
            # oluyor (ya da sig!=dir ise üzerine yazılıyordu) — engine
            # target book'i kanonik akıştan sapıyordu. Penceresi kesinlikle
            # geçmiş flip doğrudan MISSED; sınır anında (karar yaşı == 1
            # bar) hâlâ geçerli sayılır ve taze fiyatla denenir; fiyat
            # alınamazsa icra barı kapanmak üzere olduğundan flip MISSED
            # tüketilir (bir sonraki tikte sessiz iptal yerine görünür kayıt).
            S = self.engine._state(s)
            if S["pend"] and (S["pend_bar_ts"] or 0) + BAR_MS <= ot:
                if now_ms > (S["pend_bar_ts"] or 0) + 2 * BAR_MS:
                    self._consume_overdue_flip(s, ot, now_ms, states)
                else:
                    ev = self._attempt_due_flip(s, now_ms, states)
                    if ev is not None:
                        decisions.append(ev)
                        sym_flip_fired = True
                    S = self.engine._state(s)
                    if S["pend"] and \
                            (S["pend_bar_ts"] or 0) + BAR_MS <= ot:
                        # attempt flip'i tüketemedi (fiyat alınamadı) —
                        # pencere bu barın kapanışıyla kapanır → MISSED
                        self._consume_overdue_flip(s, ot, now_ms, states)
            self._set_last(s, ot)
            qv_by[s] = qv
            # (c)+(e): EWMA + BU kapanışta sinyal → pend hemen değerlendirme
            # alır (karar anı = bu barın kapanışı = sonraki barın açılışı)
            self.engine.end_bar(s, ot, c)
            if self.entry_policy == cq.ENTRY_POLICY_QUALITY:
                S = self.engine._state(s)
                self.quality.feed(s, qv, math.sqrt(S["var"])
                                  if S["var"] is not None and S["var"] > 0
                                  else None)
            if s not in self._warmed:
                newly_warmed.add(s)
            self._warmed.add(s)
            attempted.add(s)
            ev = self._attempt_due_flip(s, now_ms, states)
            if ev is not None:
                decisions.append(ev)
                sym_flip_fired = True
            # BLOK32: hedef-yön takibi — bu tikte bu sembol için flip olayı
            # ÜRETİLMEDİYSE mevcut hedef yön yeni kapalı mumpdan kalite
            # skoruna bağlanır (flip GEREKMEZ — operatör kararı §5)
            if self.entry_policy == cq.ENTRY_POLICY_QUALITY and not sym_flip_fired:
                self._quality_evaluate(s, ot, c, now_ms, states, decisions)
        # önceki tiklerden kalan due flip'ler (fiyat retry / restart sonrası
        # warmup sınırı): (karar barı, symbol) kronolojik deterministik sıra
        leftover = [s for s in self.symbols
                    if self.engine.pending(s) and s not in attempted]
        leftover.sort(key=lambda s: (self.engine._state(s)["pend_bar_ts"]
                                     or 0, s))
        for s in leftover:
            ev = self._attempt_due_flip(s, now_ms, states)
            if ev is not None:
                decisions.append(ev)
        # sembol bazlı durum etiketleri (gözlemlenebilirlik — BLOK31 §5,
        # BLOK31.1 §7/§8: before/after, warmup semantiği, hedef değişimi)
        for s, row in states.items():
            row["signal_direction"] = self.engine.direction(s)
            row["signal_pending"] = self.engine.pending(s)
            row["engine_direction_after"] = self.engine.direction(s)
            row["target_changed"] = (row.get("engine_direction_before")
                                     != row["engine_direction_after"])
            row["last_processed_bar_after"] = self._last_bar.get(s)
            S = self.engine._state(s)
            if S["pend"]:
                row["signal_timestamp"] = S["pend_bar_ts"]
                row["signal_price_reference"] = S["pend_price"]
                row["signal_age_ms"] = now_ms - (S["pend_bar_ts"] or 0) \
                    - BAR_MS
            warmed = s in self._warmed and self.engine.warm(s)
            row["warmup_complete"] = warmed
            row["warmup"] = not warmed     # BLOK31.1: true = warmup SÜRÜYOR
            row["warmup_state"] = ("WARMUP_COMPLETE" if warmed
                                   else "WARMUP_INCOMPLETE")
            if not warmed:
                missing = max(0, self.engine.lookback - S["n"])
                row["warmup_missing_bars"] = missing
                row["warmup_reason"] = (
                    f"n={S['n']} < lookback {self.engine.lookback}"
                    if missing else
                    "EWMA varyans henüz hesaplanamadı (var None/0)")
            if row.get("decision"):
                row["final_decision"] = row["decision"]
                continue
            if s in (fetch_errors or {}):
                row["decision"] = "DATA_FETCH_ERROR"
                row["decision_reason"] = fetch_errors[s]
            elif "bar_count" not in row:
                row["decision"] = "NO_KLINES"
                row["decision_reason"] = "bu tikte kline verisi sağlanmadı"
            elif not warmed:
                row["decision"] = "WARMUP_INCOMPLETE"
                row["decision_reason"] = row.get("warmup_reason")
            elif s in newly_warmed:
                row["decision"] = "WARMUP_COMPLETE"
                row["decision_reason"] = "warmup replay bu döngüde tamamlandı"
            elif self.engine.pending(s):
                row["decision"] = "PRICE_UNAVAILABLE"
                row["decision_reason"] = "due flip taze fiyat bekliyor (retry)"
            elif row.get("new_bar_count"):
                row["decision"] = "TARGET_UNCHANGED"
                row["decision_reason"] = ("yeni kapalı bar işlendi, hedef "
                                          "yön değişmedi (flip yok)")
            else:
                row["decision"] = "NO_NEW_CLOSED_BAR"
                row["decision_reason"] = "işlenecek yeni kapalı bar yok"
            row["final_decision"] = row["decision"]
        if warmup_events:
            # BLOK31.1: kayıt sembol bazlı tamamlanma durumu da taşır —
            # `bars_replayed` yalnızca bu süreçte replay edilen bar sayısıdır
            # (restart başına bir kez; 409 kapalı bar × 8 sembol = 3272),
            # engine warmup tamamlanması ayrıca doğrulanır
            per_sym = {s: (s in self._warmed and self.engine.warm(s))
                       for s in self.symbols}
            self._log("live_orders.jsonl",
                      {"t": now_ms, "kind": "WARMUP_DONE",
                       "bars_replayed": len(warmup_events), "execute": False,
                       "symbols_warmed": per_sym,
                       "all_warm": all(per_sym.values())})
        return decisions, states

    def _consume_overdue_flip(self, symbol, bar_ot, now_ms, states=None):
        """BLOK31.1 (§7 kanonik sıra): end_bar'dan ÖNCE, karar penceresi
        KESİNLİKLE kapanmış (> 1 bar) pending flip'i tüketir → MISSED
        (fail-closed; eski sinyal asla oynatılmaz). Bu tüketim olmadan
        end_bar'ın sig!=dir karşılaştırması ilerlememiş dir üzerinden
        yapılıyor ve momentum geri dönerse due flip SESSİZCE iptal
        oluyordu (engine target book'i kanonik akıştan sapıyordu)."""
        S = self.engine._state(symbol)
        direction = S["pend"]
        bar_ts = S["pend_bar_ts"]
        sig_px = S["pend_price"]
        self.engine.begin_bar(symbol, sig_px or 0.0)      # pop + dir ilerler
        reason = (f"karar penceresi kapandı (kanonik sıra koruması — karar "
                  f"yaşı {(now_ms - (bar_ts or 0) - BAR_MS) / 3600000:.1f}h "
                  f">= {BAR_MS / 3600000:.0f}h; end_bar öncesi tüketildi)")
        self._log("live_orders.jsonl",
                  {"t": now_ms, "kind": "MISSED_FLIP", "symbol": symbol,
                   "bar_ts": bar_ts, "direction": direction,
                   "signal_px": sig_px, "reason": reason, "execute": False,
                   "source": "canonical_order_guard"})
        st = states.get(symbol) if states else None
        if st is not None:
            st["decision"] = "SIGNAL_STALE"
            st["decision_reason"] = reason
            st["flip_detected"] = True

    # =========================================================================
    # BLOK32 — QUALITY-BASED TARGET-FOLLOWING (operatör kararı 2026-11-25)
    # =========================================================================
    def _quality_evaluate(self, symbol, bar_ot, close_px, now_ms, states,
                          decisions):
        """Her YENİ kapalı mumpda mevcut hedef yönü (engine target — flip
        GEREKMEZ) bağımsız kalite skorundan geçer. Kalibrasyon semantiği
        (~/blok32-calibration simulate(), OOS'e DEĞİŞMEDEN taşıdı):
          - episode ARMED + skor >= θ_d  → giriş adayı (taze icra fiyatıyla)
          - episode ENGAGED + skor < θ_d − h_d → yeniden silahlanma (hysteresis)
          - çıkış episode'u yeniden silahlamaz (aynı sinyalde yeniden giriş yok)
        Fail-closed: bileşen hesaplanamıyorsa skor yok → giriş yok + neden.
        Bekleyen (çözülmemiş) flip varken değerlendirme YOK — o anki geçiş
        flip yolunun tekelindedir. Kalitesi yetersizse NOTHING (emir yok)."""
        # GÖREV §1: BRAIN_PRIMARY modunda eski H1 kalite yolu giriş ÜRETEMEZ
        # (savunma-derinliği — strategy_tick bu yolu zaten çağırmaz)
        if self.signal_source == SIGNAL_SOURCE_BRAIN:
            return
        S = self.engine._state(symbol)
        if S["pend"]:
            return                      # çözülmemiş flip — flip yolu sahiplikte
        d = S["dir"]
        if d == 0 or not self.engine.warm(symbol):
            return
        st = states.get(symbol)
        if st is not None:
            st["target"] = "TARGET_LONG" if d > 0 else "TARGET_SHORT"
            st["target_direction"] = d
        q = self.quality.compute(symbol, d)
        th = cq.threshold(d)
        hy = cq.hysteresis(d)
        engaged = self._episode_engaged(symbol, d)
        # BLOK32 görev-2: yön canlı giriş kilidi (fail-closed). Kalite
        # skoru yine hesaplanır ve döngü kaydında GÖRÜNÜR kalır (operatör
        # izin kararına veri sağlar), ancak kilitli yön için emir zinciri
        # ASLA tetiklenmez ve karar açıkça DIRECTION_DISABLED_* olur.
        if not self._dir_enabled(d):
            if st is not None:
                if q is not None:
                    for k in ("quality_score", "quality_threshold",
                              "quality_pass", "momentum_strength",
                              "trend_confirmation", "volatility_context",
                              "volume_confirmation"):
                        st[k] = q[k]
                    st["signal_reason"] = q["signal_reason"]
                    st["signal_timestamp"] = now_ms
                    st["signal_bar_timestamp"] = bar_ot
                st["quality_episode"] = "ENGAGED" if engaged else "ARMED"
                st["decision"] = _direction_disabled_label(d)
                st["decision_reason"] = (
                    f"yön kilidi: {d} hedefi canlı girişe KAPALI "
                    "(dir_enabled_* anahtarı '1' değil — fail-closed; "
                    "varsayılan KAPALI)")
            return
        if q is None:
            if st is not None and st.get("decision") is None:
                st["decision"] = "QUALITY_INSUFFICIENT"
                st["decision_reason"] = ("kalite hesaplanamadı (fail-closed: "
                                         "bileşen penceresi dolmadı / veri "
                                         "eksik) — {0} hedefi doğrulanamadı"
                                         .format("TARGET_LONG" if d > 0
                                                 else "TARGET_SHORT"))
            return
        # §3 sinyal nesnesi alanları — yalnız GERÇEK hesaplanan bileşenler
        if st is not None:
            for k in ("quality_score", "quality_threshold", "quality_pass",
                      "momentum_strength", "trend_confirmation",
                      "volatility_context", "volume_confirmation"):
                st[k] = q[k]
            st["signal_reason"] = q["signal_reason"]
            st["signal_timestamp"] = now_ms
            st["signal_bar_timestamp"] = bar_ot
            st["quality_episode"] = "ENGAGED" if engaged else "ARMED"
        if engaged:
            # hysteresis: yeniden silahlanma YALNIZ skor θ−h altına inince
            if q["quality_score"] < th - hy:
                self._episode_set(symbol, d, False)
                if st is not None:
                    st["quality_episode"] = "ARMED"
                    st["quality_rearmed"] = True
                    st["quality_rearm_reason"] = (
                        f"skor {q['quality_score']} < θ−h "
                        f"({th:.1f}−{hy:.1f}={th - hy:.1f}) — episode yeniden "
                        "silahlandı (hysteresis)")
            elif st is not None and st.get("decision") is None:
                st["decision"] = "QUALITY_INSUFFICIENT"
                st["decision_reason"] = (f"episode ENGAGED (hysteresis) — "
                                         f"skor {q['quality_score']}")
            return
        if not q["quality_pass"]:
            if st is not None and st.get("decision") is None:
                st["decision"] = "QUALITY_INSUFFICIENT"
                st["decision_reason"] = (f"kalite skoru {q['quality_score']} < "
                                         f"eşik {th} — {q['signal_reason']}")
            return
        # kalite GEÇTİ → giriş adayı. Aynı sembolde açık pozisyon varsa giriş
        # YOK (reversal protokolü §7: önce çıkış + borsada flat doğrulama;
        # ters yön kalitesi pozisyonu çıkışa süren hedef değişiminde zaten
        # _quality_target_exits ile başlatılır).
        if any(p["symbol"] == symbol for p in self.rm.open_positions()):
            if st is not None and st.get("decision") is None:
                st["decision"] = ("QUALITY_VALID_LONG" if d > 0 else
                                  "QUALITY_VALID_SHORT")
                st["decision_reason"] = (f"kalite geçti ({q['quality_score']} "
                                         f">= {th}) ama sembolde pozisyon "
                                         "açık — reversal: önce çıkış")
            return
        # aynı bar için sinyal anahtarı zaten tükendiyse ikinci deneme YOK
        # (§6: aynı kalite olayında ikinci MARKET yok)
        if self._get_state(f"sig:{symbol}:{bar_ot}") is not None:
            return
        px, pmeta = self._executable_price(symbol, d, now_ms)
        if px is None:
            if st is not None and st.get("decision") is None:
                st["decision"] = "PRICE_UNAVAILABLE"
                st["decision_reason"] = (f"kalite geçti ({q['quality_score']}) "
                                         f"ama icra fiyatı yok: {pmeta}")
            return
        if abs(px - close_px) / close_px > SIGNAL_PRICE_DEVIATION_MAX:
            dev = abs(px - close_px) / close_px
            if st is not None and st.get("decision") is None:
                st["decision"] = "PRICE_DEVIATION"
                st["decision_reason"] = (f"taze {px:.6f} vs bar kapanışı "
                                         f"{close_px:.6f} (%{dev * 100:.2f} > "
                                         f"{SIGNAL_PRICE_DEVIATION_MAX * 100:.0f})")
            return
        self._log("live_orders.jsonl",
                  {"t": now_ms, "kind": "QUALITY_SIGNAL", "symbol": symbol,
                   "bar_ts": bar_ot, "direction": d,
                   "quality_score": q["quality_score"],
                   "quality_threshold": th,
                   "momentum_strength": q["momentum_strength"],
                   "trend_confirmation": q["trend_confirmation"],
                   "volatility_context": q["volatility_context"],
                   "volume_confirmation": q["volume_confirmation"],
                   "signal_reason": q["signal_reason"],
                   "signal_timestamp": now_ms,
                   "signal_bar_timestamp": bar_ot,
                   "executable_px": px, "signal_px": close_px,
                   "execute": True})
        if st is not None:
            st["decision"] = ("QUALITY_VALID_LONG" if d > 0 else
                              "QUALITY_VALID_SHORT")
            st["decision_reason"] = (f"kalite geçti: {q['quality_score']} >= "
                                     f"{th} — giriş zinciri tetiklendi")
            st["current_executable_price"] = px
            st["price_source"] = "bookTicker"
            st["price_age_ms"] = now_ms - pmeta.get("ts", now_ms)
        decisions.append((symbol, d, px, bar_ot, None, close_px))

    def _quality_target_exits(self, now_ms, states=None):
        """BLOK32 (yalnız QUALITY politika): hedef yön değiştiğinde (flip
        TÜKETİLDİ — kalite geçsin geçmesin; kalibrasyon semantiği: çıkış
        flip'te koşulsuz) açık ters pozisyonu reduce-only çıkışa al.
        One-way reversal protokolünün ÇIKIŞ bacağı (§7); yeni giriş yalnız
        borsada flat doğrulaması + koruma/defter mutabakatından SONRAki
        barlarda değerlendirilir (same-symbol kapısı bunu zorlar —
        rm pozisyonu yalnız flat-doğrulanmış kapanışta defterden düşer).
        FLIP_ONLY'da çıkışlar olay tabanlı kalır (değişiklik YOK)."""
        for pos in self.rm.open_positions():
            sym = pos["symbol"]
            d = self.engine.direction(sym)
            if d == 0 or pos["direction"] == d:
                continue
            lp = self._live_pos_row(pos["pos_id"])
            if lp and lp["exit_status"] not in (X_NONE, X_CONFIRMED):
                continue          # çıkış akışı zaten sürüyor — dokunma
            res = self.initiate_risk_exit(pos["pos_id"],
                                          "QUALITY_TARGET_FLIP_EXIT")
            self._log("live_orders.jsonl",
                      {"t": now_ms, "kind": "QUALITY_REVERSAL_EXIT",
                       "symbol": sym, "pos_id": pos["pos_id"],
                       "position_direction": pos["direction"],
                       "target_direction": d, "result": res,
                       "reason": "hedef yön değişti (flip tüketildi) — "
                                 "reduce-only çıkış; flat doğrulama sonrası "
                                 "yeni yön değerlendirmesi"})
            if states.get(sym) is not None:
                states[sym]["reversal_exit_initiated"] = True

    def _set_last(self, symbol, ot):
        self._last_bar[symbol] = ot
        self._set_state(f"last_bar:{symbol}", str(ot))

    # =========================================================================
    # ÇALIŞTIRMA — iki bağımsız döngü (§4)
    # =========================================================================
    def run(self, strategy_interval=STRATEGY_LOOP_S,
            risk_interval=RISK_LOOP_S, on_started=None):
        """İki thread: risk loop strategy loop'u BEKLEMEZ. Başlangıçta
        önce risk + mutabakat (§4.2 restart sırası)."""
        self.acquire_worker_lock()
        self.startup_reconcile()
        # ilk risk tick'ini beklemeden at (process start: ÖNCE risk)
        try:
            self.risk_tick()
        except Exception as e:
            self._log("live_cycles.jsonl", {"t": self._now(),
                                            "kind": "RISK_TICK_ERROR",
                                            "error": str(e)})

        def _risk_loop():
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    self.risk_tick()
                except Exception as e:
                    self._log("live_cycles.jsonl",
                              {"t": self._now(), "kind": "RISK_TICK_ERROR",
                               "error": f"{type(e).__name__}: {e}"})
                elapsed = time.time() - t0
                self._stop.wait(max(0.0, risk_interval - elapsed))

        def _strategy_loop():
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    self.strategy_tick()
                except Exception as e:
                    self._log("live_cycles.jsonl",
                              {"t": self._now(),
                               "kind": "STRATEGY_TICK_ERROR",
                               "error": f"{type(e).__name__}: {e}"})
                elapsed = time.time() - t0
                self._stop.wait(max(0.0, strategy_interval - elapsed))

        tr = threading.Thread(target=_risk_loop, name="canary-risk",
                              daemon=True)
        ts = threading.Thread(target=_strategy_loop, name="canary-strategy",
                              daemon=True)
        tr.start()
        ts.start()
        if on_started:
            on_started(self)
        try:
            while tr.is_alive() and ts.is_alive():
                time.sleep(0.5)
        finally:
            self.stop()
        return 0

    def stop(self):
        self._stop.set()
        self.release_worker_lock()
