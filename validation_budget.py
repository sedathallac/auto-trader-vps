#!/usr/bin/env python3
"""BLOK10 §3 — KONTROLLÜ CANLI DOĞRULAMA BÜTÇESİ.

Amaç: Operatörün ayrı onaylayacağı kontrollü canlı testin SONLU üst sınırlarını
bağımsız, kalıcı ve atomik bir kontrol olarak uygulamak:

    en fazla 2 işlem / işlem başına ≤ 50 USDT / toplam ≤ 100 USDT notional

Bu kontrol RİSK MOTORUNDAN BAĞIMSIZDIR: RiskEngine limitleri değişse bile bu
tavanlar ayrıca uygulanır (savunma-hattı). FIRST-USE'TE KURULAN SQLite tablosu
üzerinde BEGIN IMMEDIATE ile atomik rezervasyon; restart'ta kalıcı; retry aynı
idempotency-anahtarıyla HAK YENİDEN TÜKETMEZ; ACK-belirsiz durumda rezervasyon
iade edilmez (korunur).

Ortam değişkenleri:
    CONTROLLED_LIVE_VALIDATION   "false" (varsayılan) | true/1/yes/on
    MAX_VALIDATION_TRADES        varsayılan 2   (sert tavan 2; yalnız sıkılaşır)
    MAX_VALIDATION_POSITION_NOTIONAL_USDT  varsayılan 50.0 (sert tavan 50.0)
    MAX_VALIDATION_TOTAL_NOTIONAL_USDT     varsayılan 100.0 (sert tavan 100.0)

FAIL-CLOSED DOKTRİNİ:
  * Bayrak AÇIK iken limitlerden biri geçersiz/eksik-çözümlenemez (boş, 0,
    negatif, sayısal olmayan, sert tavanı aşan) → Config.valid=False → çağıran
    YENİ GİRİŞİ BLOKLAR. Sessiz varsayılana düşülmez.
  * Herhangi bir SQLite/veri hatası → BudgetError (çağıran bloklar).
  * Notional > işlem-başı tavan → rezervasyon reddi (POST'A GİDİLMEZ).
  * RELEASED satırla aynı anahtar → YENİDEN tüketir (iade edilmiş hakkı görmezden
    gelip POST'a izin vermek EMNİYETSİZ taraf olurdu).
Bayrak KAPALI iken bu modül giriş yoluna HİÇ dokunmaz (normal akış değişmez).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

# Sert tavanlar — ortam override'ları bunları AŞAMAZ, yalnız sıkılaştırabilir.
HARD_MAX_TRADES = 2
HARD_MAX_PER_TRADE_USDT = 50.0
HARD_MAX_TOTAL_USDT = 100.0

_TRUTHY = ("1", "true", "yes", "on")
_EPS = 1e-9


class BudgetError(RuntimeError):
    """Bütçe deposuna erişilemiyor/veri bozuk → çağıran fail-closed bloklemeli."""


@dataclass(frozen=True)
class Limits:
    max_trades: int
    max_per_trade_usdt: float
    max_total_usdt: float


@dataclass(frozen=True)
class Config:
    enabled: bool
    valid: bool
    limits: Optional[Limits]
    error: Optional[str] = None

    @staticmethod
    def from_env() -> "Config":
        flag = os.getenv("CONTROLLED_LIVE_VALIDATION", "false").strip().lower()
        enabled = flag in _TRUTHY
        if not enabled:
            return Config(enabled=False, valid=True, limits=None)
        try:
            raw_t = os.getenv("MAX_VALIDATION_TRADES")
            if raw_t is None:
                trades = HARD_MAX_TRADES
            else:
                t = float(raw_t.strip())
                if not t.is_integer() or not (0 < t <= HARD_MAX_TRADES):
                    return Config(True, False, None,
                                  f"MAX_VALIDATION_TRADES={raw_t!r} geçersiz "
                                  f"(1..{HARD_MAX_TRADES} tam sayı olmalı)")
                trades = int(t)
            raw_p = os.getenv("MAX_VALIDATION_POSITION_NOTIONAL_USDT")
            if raw_p is None:
                per = HARD_MAX_PER_TRADE_USDT
            else:
                p = float(raw_p.strip())
                if not (0.0 < p <= HARD_MAX_PER_TRADE_USDT + _EPS):
                    return Config(True, False, None,
                                  f"MAX_VALIDATION_POSITION_NOTIONAL_USDT="
                                  f"{raw_p!r} geçersiz (0< v ≤ {HARD_MAX_PER_TRADE_USDT})")
                per = p
            raw_tot = os.getenv("MAX_VALIDATION_TOTAL_NOTIONAL_USDT")
            if raw_tot is None:
                total = HARD_MAX_TOTAL_USDT
            else:
                tot = float(raw_tot.strip())
                if not (0.0 < tot <= HARD_MAX_TOTAL_USDT + _EPS):
                    return Config(True, False, None,
                                  f"MAX_VALIDATION_TOTAL_NOTIONAL_USDT="
                                  f"{raw_tot!r} geçersiz (0< v ≤ {HARD_MAX_TOTAL_USDT})")
                total = tot
        except ValueError:
            return Config(True, False, None, "limit ortam değişkeni sayısal değil")
        return Config(True, True, Limits(trades, per, total))


@dataclass(frozen=True)
class Reservation:
    ok: bool
    rid: Optional[str]
    reason: str
    trades_used: int = 0
    notional_used: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS validation_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    trades_used INTEGER NOT NULL DEFAULT 0,
    notional_used REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO validation_state (id, trades_used, notional_used, updated_at)
VALUES (1, 0, 0.0, 'init');
CREATE TABLE IF NOT EXISTS validation_reservations (
    rid TEXT PRIMARY KEY,
    idem_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    notional REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RESERVED','CONSUMED','RELEASED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT
);
"""


class ValidationBudget:
    """Kalıcı, atomik doğrulama bütçesi. Her işlem YALNIZ SQLite üzerinden sayılır."""

    def __init__(self, db_file: str, limits: Limits):
        self._db_file = str(db_file)
        self._limits = limits
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ infra
    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_file, timeout=10, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            try:
                con.close()
            except Exception:
                pass
            raise
        return con

    def _ensure_init(self, con: sqlite3.Connection) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            try:
                con.executescript(_SCHEMA)
            except sqlite3.Error as e:
                raise BudgetError(f"bütçe deposu başlatılamadı: {e}") from e
            self._initialized = True

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ------------------------------------------------------------------ read
    def status(self) -> dict:
        try:
            con = self._conn()
        except sqlite3.Error as e:
            raise BudgetError(f"bütçe deposuna erişilemedi: {e}") from e
        try:
            self._ensure_init(con)
            row = con.execute(
                "SELECT trades_used, notional_used FROM validation_state "
                "WHERE id=1").fetchone()
            if row is None:
                raise BudgetError("validation_state satırı yok (bozuk depo)")
            return {"trades_used": int(row[0]), "notional_used": float(row[1]),
                    "max_trades": self._limits.max_trades,
                    "max_per_trade_usdt": self._limits.max_per_trade_usdt,
                    "max_total_usdt": self._limits.max_total_usdt}
        except sqlite3.Error as e:
            raise BudgetError(f"bütçe durumu okunamadı: {e}") from e
        finally:
            con.close()

    # ---------------------------------------------------------------- reserve
    def reserve(self, symbol: str, notional: float, idem_key: str) -> Reservation:
        """Atomik hak ayırma. ok=False → POST'A GİDİLMEZ; exception → fail-closed."""
        try:
            notional = float(notional)
        except (TypeError, ValueError):
            return Reservation(False, None, "BAD_NOTIONAL")
        if not (notional == notional) or notional <= 0.0 or notional > 1e12:
            return Reservation(False, None, "BAD_NOTIONAL")
        if not idem_key:
            return Reservation(False, None, "BAD_IDEM_KEY")
        lim = self._limits
        try:
            con = self._conn()
        except sqlite3.Error as e:
            raise BudgetError(f"bütçe deposuna erişilemedi: {e}") from e
        try:
            self._ensure_init(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                # 1) İdempotency: aynı anahtar varsa YENİ tüketim yok.
                existing = con.execute(
                    "SELECT rid, status FROM validation_reservations "
                    "WHERE idem_key=?", (idem_key,)).fetchone()
                if existing is not None and existing[1] != "RELEASED":
                    used = con.execute(
                        "SELECT trades_used, notional_used FROM validation_state "
                        "WHERE id=1").fetchone()
                    con.execute("COMMIT")
                    return Reservation(True, existing[0], "IDEMPOTENT",
                                       int(used[0]), float(used[1]))
                # 2) Tavan kontrolleri (sıra: başına → adet → toplam).
                if notional > lim.max_per_trade_usdt + _EPS:
                    con.execute("ROLLBACK")
                    return Reservation(False, None, "PER_TRADE_CAP")
                row = con.execute(
                    "SELECT trades_used, notional_used FROM validation_state "
                    "WHERE id=1").fetchone()
                if row is None:
                    raise BudgetError("validation_state satırı yok")
                t_used, n_used = int(row[0]), float(row[1])
                if t_used + 1 > lim.max_trades:
                    con.execute("ROLLBACK")
                    return Reservation(False, None, "TRADE_COUNT_CAP",
                                       t_used, n_used)
                if n_used + notional > lim.max_total_usdt + _EPS:
                    con.execute("ROLLBACK")
                    return Reservation(False, None, "TOTAL_NOTIONAL_CAP",
                                       t_used, n_used)
                # 3) Tüket: yeni satır VEYA RELEASED satırın yeniden tüketimi.
                now = self._now()
                if existing is not None:        # RELEASED → yeniden tüket (güvenli)
                    rid = existing[0]
                    con.execute(
                        "UPDATE validation_reservations SET status='RESERVED', "
                        "symbol=?, notional=?, updated_at=?, reason=NULL "
                        "WHERE rid=?", (symbol, notional, now, rid))
                else:
                    rid = uuid.uuid4().hex
                    con.execute(
                        "INSERT INTO validation_reservations (rid, idem_key, "
                        "symbol, notional, status, created_at, updated_at) "
                        "VALUES (?,?,?,?, 'RESERVED', ?, ?)",
                        (rid, idem_key, symbol, notional, now, now))
                con.execute(
                    "UPDATE validation_state SET trades_used=trades_used+1, "
                    "notional_used=notional_used+?, updated_at=? WHERE id=1",
                    (notional, now))
                used = con.execute(
                    "SELECT trades_used, notional_used FROM validation_state "
                    "WHERE id=1").fetchone()
                con.execute("COMMIT")
                return Reservation(True, rid, "RESERVED",
                                   int(used[0]), float(used[1]))
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise BudgetError(f"rezervasyon başarısız (fail-closed): {e}") from e
        finally:
            con.close()

    # ---------------------------------------------------------------- confirm
    def confirm(self, rid: str) -> bool:
        """POST başarılı (executor oluştu) → RESERVED→CONSUMED. İade hakkı kalmaz."""
        return self._transition(rid, "RESERVED", "CONSUMED", None,
                                refund=False)

    # ---------------------------------------------------------------- release
    def release(self, rid: str, reason: str = "") -> bool:
        """Yalnız RESERVED iade edilir (teyitli başarısızlık). CONSUMED ASLA."""
        return self._transition(rid, "RESERVED", "RELEASED", reason, refund=True)

    def _transition(self, rid, frm, to, reason, refund) -> bool:
        try:
            con = self._conn()
        except sqlite3.Error as e:
            raise BudgetError(f"bütçe deposuna erişilemedi: {e}") from e
        try:
            self._ensure_init(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute(
                    "SELECT status, notional FROM validation_reservations "
                    "WHERE rid=?", (rid,)).fetchone()
                if row is None or row[0] != frm:
                    con.execute("ROLLBACK")
                    return False
                now = self._now()
                con.execute(
                    "UPDATE validation_reservations SET status=?, updated_at=?, "
                    "reason=COALESCE(?, reason) WHERE rid=?", (to, now, reason, rid))
                if refund:
                    con.execute(
                        "UPDATE validation_state SET trades_used="
                        "MAX(0, trades_used-1), notional_used=notional_used-?, "
                        "updated_at=? WHERE id=1", (float(row[1]), now))
                con.execute("COMMIT")
                return True
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise BudgetError(f"durum geçişi başarısız (fail-closed): {e}") from e
        finally:
            con.close()
