"""risk_engine.py — SQLite risk ledger (C3 + FAZ16 integrity).

FAZ16 düzeltmeleri (kök neden → davranış):
  * DB yolu tek: `db_file` çağırandan gelir (auto_trader STATE_DIR/risk_ledger.db).
  * Sessiz DB oluşturma YOK: `mode=rw` URI; dosya yoksa fail-closed. Yeni kurulum
    ayrı ve açık bir bootstrap komutuna aittir (`db_create=True` yalnız test/bootstrap).
  * `_ensure_schema()` kaldırıldı: startup'ta otomatik migration/DDL yok. Şema
    doğrulanır; eksikse yeni giriş fail-closed, açık pozisyon yönetimi sürer.
  * `_save_state()` başarıyı bool döner; hata sessizce yutulmaz, entry health kapanır.
  * `record_trade()` tek `BEGIN IMMEDIATE` transaction: duplicate kontrolü, ledger
    INSERT, risk_state UPDATE (rowcount kontrollü), commit. Hata → rollback.
  * `daily_pnl` ledger'dan türetilir (aynı transaction içinde), böylece restart veya
    tekrar kayıt PnL'yi iki kez artırmaz.
  * `consecutive_losses` doğrulanmış UTC kapanış zamanına göre kronolojik hesaplanır.
  * Günlük baseline (day_start_equity/_date/_at) DB'de kalıcı; gün içi restart'ta
    yeniden üretilmez, uydurulmaz.
  * Kalıcı HALTED ile geçici entry-health ayrıldı: günlük reset veya başarılı sync
    HALTED'ı kaldırmaz.
"""
import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB_FILE = Path.home() / "risk_ledger.db"

REQUIRED_RISK_STATE_COLS = {
    "id", "peak_equity", "consecutive_losses", "daily_trades", "daily_pnl",
    "last_reset_date", "is_halted", "halt_reason", "updated_at",
}
REQUIRED_LEDGER_COLS = {
    "executor_id", "trading_pair", "net_pnl_quote", "closed_at", "recorded_at", "source",
}
REQUIRED_SYNC_META_COLS = {"id", "last_close_ts", "updated_at"}


class RiskStoreError(RuntimeError):
    """DB yok / açılamıyor / şema doğrulanamıyor — fail-closed."""


def utc_now():
    return datetime.now(timezone.utc)


def utc_day(ts=None):
    """UTC gün anahtarı (YYYY-MM-DD). Makinenin yerel timezone'una güvenilmez."""
    if ts is None:
        return utc_now().date().isoformat()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()


def iso_utc(dt):
    """Timezone bilgisi içeren UTC ISO-8601."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_close_epoch(closed_at_epoch, closed_at):
    """Doğrulanmış kapanış zamanı (Unix saniye) veya None.

    Naive `closed_at` değerleri UTC varsayılmaz (eski satırlar yerel saatle
    yazılmış olabilir) — çözümlenemezse None döner ve çağıran UNRESOLVED işaretler.
    """
    if closed_at_epoch is not None:
        try:
            v = float(closed_at_epoch)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and v > 0 else None
    if not closed_at:
        return None
    try:
        dt = datetime.fromisoformat(str(closed_at))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def connect_rw(db_file, create=False):
    """Mevcut SQLite dosyasını read-write aç. `create=False` iken dosya YOKSA hata."""
    p = Path(db_file)
    if not p.exists():
        if not create:
            raise RiskStoreError(f"risk DB yok: {p} (sessiz oluşturma yasak)")
        p.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{p}?mode={'rwc' if create else 'rw'}"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    except sqlite3.Error as e:
        raise RiskStoreError(f"risk DB açılamadı ({p}): {e}") from e
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _columns(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return None
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def validate_store(conn):
    """Şema + singleton bütünlüğü. Sorun listesi döner (boş = sağlıklı)."""
    problems = []
    for table, required in (("risk_state", REQUIRED_RISK_STATE_COLS),):
        cols = _columns(conn, table)
        if cols is None:
            problems.append(f"tablo YOK: {table}")
            continue
        missing = required - cols
        if missing:
            problems.append(f"{table}: eksik sütun {', '.join(sorted(missing))}")
    if problems:
        return problems
    _led = _columns(conn, "trade_ledger")
    if _led is None:
        return []          # ledger yok → migration_required (entry kapısı kapatır)
    missing = REQUIRED_LEDGER_COLS - _led
    if missing:
        problems.append(f"trade_ledger: eksik sütun {', '.join(sorted(missing))}")
    if conn.execute("SELECT 1 FROM risk_state WHERE id=1").fetchone() is None:
        problems.append("risk_state id=1 satırı YOK")
    dup = conn.execute(
        "SELECT executor_id FROM trade_ledger GROUP BY executor_id HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        problems.append(f"trade_ledger duplicate executor_id: {dup[0]}")
    return problems


def bootstrap_sync_meta(conn):
    """sync_meta id=1 yoksa idempotent oluştur (şema migration'ı DEĞİL).

    Tablo hiç yoksa burada DDL çalıştırılmaz (startup otomatik migration yasak);
    False döner ve sync katmanı giriş sağlığını kapatır.
    """
    if _columns(conn, "sync_meta") is None:
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sync_meta (id, last_close_ts, updated_at) VALUES (1, 0, ?)",
            (iso_utc(utc_now()),),
        )
        if conn.execute("SELECT 1 FROM sync_meta WHERE id=1").fetchone() is None:
            raise RiskStoreError("sync_meta id=1 oluşturulamadı")
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


class RiskEngine:
    def __init__(self, config):
        self.cfg = config
        self.db_file = Path(config.get("db_file", str(DEFAULT_DB_FILE)))
        self.peak_equity = 0.0
        self.current_equity = 0.0
        self.day_start_equity = 0.0
        self.day_start_equity_date = None
        self.day_start_equity_at = None
        self.current_drawdown = 0.0
        self.consecutive_losses = 0
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.is_halted = False
        self.halt_reason = None
        self.margin_usage_pct = 0.0
        self.last_reset_date = None
        self.state = {}
        self.health_ok = True
        self.health_reason = None
        self.migration_required = False
        self._open_keys = set()
        self._load_state()

    # ---------------------------------------------------------------- health
    def _block(self, reason):
        self.health_ok = False
        self.health_reason = reason
        log.critical(f"🚨 C3: risk store sağlıksız — yeni giriş kapalı: {reason}")

    @contextmanager
    def _db(self):
        conn = connect_rw(self.db_file, create=bool(self.cfg.get("db_create", False)))
        try:
            yield conn
        finally:
            conn.close()

    def _load_state(self):
        try:
            with self._db() as conn:
                problems = validate_store(conn)
                if problems:
                    self._block("; ".join(problems))
                    return
                bootstrap_sync_meta(conn)
                _cols = _columns(conn, "risk_state") or set()
                _opt = ("day_start_equity", "day_start_equity_date",
                        "day_start_equity_at", "open_keys")
                _sel = ", ".join(c if c in _cols else "NULL" for c in _opt)
                self.migration_required = any(c not in _cols for c in _opt) or \
                    _columns(conn, "trade_ledger") is None
                row = conn.execute(
                    "SELECT peak_equity, consecutive_losses, daily_trades, daily_pnl, "
                    "is_halted, halt_reason, last_reset_date, " + _sel +
                    " FROM risk_state WHERE id=1"
                ).fetchone()
        except Exception as e:
            self._block(f"DB load error: {e}")
            return
        if row is None:
            # risk_state id=1 yok: satır UYDURULMAZ. Bellekte varsayılanlarla devam
            # edilir; merkezi kapı migration_required ile yeni girişi kapatır.
            self.migration_required = True
            log.critical("🚨 C3: risk_state id=1 satırı YOK — yeni giriş kapalı "
                         "(risk_migration.py ile onarılmalı)")
            return
        self.peak_equity = float(row[0] or 0)
        self.consecutive_losses = int(row[1] or 0)
        self.daily_trades = int(row[2] or 0)
        self.daily_pnl = float(row[3] or 0)
        self.is_halted = bool(row[4])
        self.halt_reason = row[5]
        self.last_reset_date = row[6]
        self.day_start_equity = float(row[7] or 0)
        self.day_start_equity_date = row[8]
        self.day_start_equity_at = row[9]
        try:
            self._open_keys = set(json.loads(row[10] or "[]"))
        except (TypeError, ValueError):
            self._open_keys = set()
        self.state = {
            "peak_equity": self.peak_equity,
            "consecutive_losses": self.consecutive_losses,
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "last_reset_date": self.last_reset_date,
            "day_start_equity": self.day_start_equity,
            "day_start_equity_date": self.day_start_equity_date,
            "day_start_equity_at": self.day_start_equity_at,
        }

    def _save_state(self):
        """UPSERT risk_state. Dönüş: True yalnız gerçekten yazıldıysa.

        FAZ16: migration gerektiren eski şemada (day_start_equity/open_keys yok)
        yalnız TEMEL kolonlar yazılır; kayıt sessizce düşmez, `migration_required`
        bayrağı merkezi kapıyı kapatır.

        FAZ14x→FAZ16 entegrasyon: öznitelik (self.last_reset_date) TEK otoritedir;
        state sözlüğü yalnız AYNADIR (FAZ16 startup-migration-yasak sözleşmesi ve
        TestBaseline bu yönü sabitler). FAZ14x-çağı state-override çağrı yolu
        entegre ağaçta KALDIRILDI — kanonik yazma yolu reset_daily(reset_date=).
        """
        self.state["last_reset_date"] = self.last_reset_date
        now = iso_utc(utc_now())
        base = (self.peak_equity, self.consecutive_losses, self.daily_trades,
                self.daily_pnl, self.last_reset_date or utc_day(),
                1 if self.is_halted else 0, self.halt_reason, now)
        full_sql = """
            INSERT INTO risk_state
                (id, peak_equity, consecutive_losses, daily_trades, daily_pnl,
                 last_reset_date, is_halted, halt_reason, updated_at,
                 day_start_equity, day_start_equity_date, day_start_equity_at, open_keys)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                peak_equity=excluded.peak_equity,
                consecutive_losses=excluded.consecutive_losses,
                daily_trades=excluded.daily_trades,
                daily_pnl=excluded.daily_pnl,
                last_reset_date=excluded.last_reset_date,
                is_halted=excluded.is_halted,
                halt_reason=excluded.halt_reason,
                updated_at=excluded.updated_at,
                day_start_equity=excluded.day_start_equity,
                day_start_equity_date=excluded.day_start_equity_date,
                day_start_equity_at=excluded.day_start_equity_at,
                open_keys=excluded.open_keys
        """
        full_args = base + (self.day_start_equity, self.day_start_equity_date,
                            self.day_start_equity_at, json.dumps(sorted(self._open_keys)))
        base_sql = """
            INSERT INTO risk_state
                (id, peak_equity, consecutive_losses, daily_trades, daily_pnl,
                 last_reset_date, is_halted, halt_reason, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                peak_equity=excluded.peak_equity,
                consecutive_losses=excluded.consecutive_losses,
                daily_trades=excluded.daily_trades,
                daily_pnl=excluded.daily_pnl,
                last_reset_date=excluded.last_reset_date,
                is_halted=excluded.is_halted,
                halt_reason=excluded.halt_reason,
                updated_at=excluded.updated_at
        """
        try:
            with self._db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    try:
                        cur = conn.execute(full_sql, full_args)
                    except sqlite3.OperationalError as oe:
                        if "no such column" not in str(oe):
                            raise
                        self.migration_required = True
                        cur = conn.execute(base_sql, base)
                    if conn.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RiskStoreError("risk_state yazılmadı (changes!=1)")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            return True
        except Exception as e:
            self._block(f"risk state KAYDEDILEMEDI: {e}")
            return False

    # ------------------------------------------------------------- ledger
    def _ledger_day_sum(self, conn, day):
        """Bugünkü (UTC) doğrulanmış kapanış toplamı + çözümlenemeyen satır sayısı."""
        rows = conn.execute(
            "SELECT net_pnl_quote, closed_at_epoch, closed_at FROM trade_ledger"
        ).fetchall()
        total = 0.0
        unresolved = 0
        for pnl, epoch, iso in rows:
            ts = parse_close_epoch(epoch, iso)
            if ts is None:
                unresolved += 1
                continue
            if utc_day(ts) == day:
                total += float(pnl or 0)
        return total, unresolved

    def _consecutive_from_ledger(self, conn):
        """Kronolojik ardışık zarar sayacı. Zamanı çözülemeyen satır varsa None."""
        rows = conn.execute(
            "SELECT net_pnl_quote, closed_at_epoch, closed_at, executor_id FROM trade_ledger"
        ).fetchall()
        timed = []
        for pnl, epoch, iso, eid in rows:
            ts = parse_close_epoch(epoch, iso)
            if ts is None:
                return None
            timed.append((ts, str(eid), float(pnl or 0)))
        timed.sort(key=lambda r: (r[0], r[1]))
        streak = 0
        for _, _, pnl in timed:
            streak = streak + 1 if pnl < 0 else 0
        return streak

    def record_trade(self, executor_id, trading_pair, net_pnl_quote,
                     closed_at_iso=None, closed_at_epoch=None,
                     affect_daily=True, source="sync"):
        """Atomik + idempotent PnL kaydı. Dönüş: (inserted, message).

        Tek `BEGIN IMMEDIATE` içinde: duplicate kontrolü → ledger INSERT →
        risk_state UPDATE (rowcount kontrollü) → commit. Herhangi bir hata tüm
        transaction'ı geri alır; bellek DB'den gelen son state'e eşitlenir.

        FAZ14x entegrasyon (korunan üretim sözleşmesi):
          * `source` provenance etiketi (sync | hb_terminated |
            binance_fill_verified | binance_stale_recovery); varsayılan 'sync'.
          * `affect_daily=False` → geçmiş tarihli kapanış YALNIZ ledger'a
            yazılır; bugünün risk_state sayacı yeniden yazILMAZ (FAZ16'nın
            ledger-türevli standardında günlük toplam zaten gün-koşullu,
            ancak streak/yayın yan etkileri de tetiklenmez — üretim testi
            test_faz14x_risk_daily bu ayrımı bekler).
        """
        if not executor_id:
            return False, "error: executor_id required"
        try:
            pnl = float(net_pnl_quote)
        except (TypeError, ValueError):
            return False, "error: invalid pnl"
        if not math.isfinite(pnl):
            return False, "error: non-finite pnl"
        ts = parse_close_epoch(closed_at_epoch, closed_at_iso)
        epoch = ts if ts is not None else None
        iso = iso_utc(datetime.fromtimestamp(ts, tz=timezone.utc)) if ts is not None else None
        now = iso_utc(utc_now())
        try:
            with self._db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if conn.execute("SELECT 1 FROM trade_ledger WHERE executor_id=?",
                                    (executor_id,)).fetchone() is not None:
                        conn.execute("ROLLBACK")
                        return False, "already-recorded"
                    conn.execute("""
                        INSERT INTO trade_ledger
                            (executor_id, trading_pair, net_pnl_quote, closed_at,
                             recorded_at, source, closed_at_epoch)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (executor_id, trading_pair, pnl, iso, now,
                          source or "sync", epoch))
                    if not affect_daily:
                        # Geçmiş kapanış: ledger yeter; bugünün risk_state
                        # sayacı ve bellek state'i YENİDEN YAZILMAZ.
                        conn.execute("COMMIT")
                        return True, "ledger-only (historical close)"
                    day = utc_day()
                    daily, unresolved = self._ledger_day_sum(conn, day)
                    streak = self._consecutive_from_ledger(conn)
                    if streak is None:
                        streak = self.consecutive_losses
                    cur = conn.execute("""
                        UPDATE risk_state SET daily_pnl=?, consecutive_losses=?, updated_at=?
                        WHERE id=1
                    """, (daily, streak, now))
                    if conn.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RiskStoreError("risk_state UPDATE yazılmadı (changes!=1)")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                self.daily_pnl = daily
                self.consecutive_losses = streak
                self.state["daily_pnl"] = daily
                self.state["consecutive_losses"] = streak
            if unresolved:
                self._block(f"ledger: {unresolved} satırın kapanış zamanı çözümlenemedi")
            if streak >= int(self.cfg["max_consecutive_losses"]):
                log.warning(f"⚠️  Ardışık zarar limitine ({self.cfg['max_consecutive_losses']}) ulaşıldı")
            return True, f"pnl={pnl:+.4f}"
        except Exception as e:
            log.critical(f"🚨 C3: record_trade HATA (rollback): {e}")
            return False, f"error: {e}"

    def register_trade_result(self, pnl_quote, executor_id=None, trading_pair=None,
                              closed_at_iso=None):
        """Backward-compat. executor_id yoksa idempotency YOK — üretimde kullanma."""
        if executor_id:
            return self.record_trade(executor_id, trading_pair, pnl_quote, closed_at_iso)
        log.warning("register_trade_result without executor_id — no idempotency")
        self.daily_pnl += pnl_quote
        self.consecutive_losses = self.consecutive_losses + 1 if pnl_quote < 0 else 0
        self._save_state()

    # -------------------------------------------------------------- equity
    def update_equity(self, current_equity):
        """Equity güncelle; günlük baseline'ı KALICI ve tek seferlik kur."""
        try:
            eq = float(current_equity)
        except (TypeError, ValueError):
            self._block("equity geçersiz (sayısal değil)")
            return False
        if not math.isfinite(eq) or eq <= 0:
            self._block(f"equity geçersiz ({current_equity!r})")
            return False
        self.current_equity = eq
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.peak_equity > 0:
            self.current_drawdown = (self.peak_equity - eq) / self.peak_equity

        today = utc_day()
        if self.day_start_equity_date == today:
            if self.day_start_equity > 0:
                pass  # aynı gün: kayıtlı baseline korunur
            else:
                self.day_start_equity = eq
                self.day_start_equity_at = iso_utc(utc_now())
        elif self.last_reset_date == today:
            # Reset bugün yapıldı ama baseline kayboldu: bugünkü equity'yi geçmiş
            # başlangıç sermayesi gibi YAZMA. Yeni giriş fail-closed.
            self._block("daily_baseline_missing (reset bugün, baseline yok)")
            return False
        else:
            self.day_start_equity = eq
            self.day_start_equity_date = today
            self.day_start_equity_at = iso_utc(utc_now())
        self.state["day_start_equity"] = self.day_start_equity
        self.state["day_start_equity_date"] = self.day_start_equity_date
        self.state["day_start_equity_at"] = self.day_start_equity_at

        if self.day_start_equity > 0:
            day_loss = (self.day_start_equity - eq) / self.day_start_equity
            limit = float(self.cfg.get("max_daily_loss_pct", 0.05))
            _limit_usdt = self.day_start_equity * limit
            if day_loss >= limit and not self.is_halted:
                self.is_halted = True
                self.halt_reason = (f"daily_loss_pct %{day_loss*100:.2f} "
                                    f"(limit %{limit*100:.0f}% = {_limit_usdt:.2f} USDT)")
                log.critical(f"🚨 GÜNLÜK EQUITY KESİCİ: {self.halt_reason}")
        if self.current_drawdown >= self.cfg["max_drawdown_pct"] and not self.is_halted:
            self.is_halted = True
            self.halt_reason = (f"Drawdown %{self.current_drawdown*100:.1f} "
                                f"(limit %{self.cfg['max_drawdown_pct']*100:.0f}%)")
            log.critical(f"🚨 DEVRE KESİCİ: {self.halt_reason}")
        return self._save_state()

    def update_exposure(self, balance, positions):
        """Marjin kullanımı. `balance` = Futures EQUITY (totalMarginBalance)."""
        try:
            bal = float(balance)
        except (TypeError, ValueError):
            self._block("exposure: balance geçersiz")
            return False
        if not math.isfinite(bal) or bal <= 0:
            return False
        total_margin = 0.0
        for p in positions or []:
            try:
                amt = float(p.get("positionAmt", 0) or 0)
                if amt == 0:
                    continue
                notional = abs(amt) * float(p.get("markPrice", p.get("entryPrice", 0)) or 0)
                lev = float(p.get("leverage", 1) or 1)
                total_margin += notional / lev if lev > 0 else notional
            except (TypeError, ValueError):
                self._block("exposure: pozisyon alanı geçersiz")
                return False
        usage = total_margin / bal
        self.margin_usage_pct = usage
        log.info(f"📊 Marjin kullanımı: %{usage*100:.1f} "
                 f"(limit: %{self.cfg.get('max_total_margin_pct', 0.4)*100:.0f})")
        if usage > self.cfg.get("max_total_margin_pct", 0.4):
            self.is_halted = True
            self.halt_reason = f"Exposure limit aşıldı (%{usage*100:.1})"
            log.critical(f"🚨 {self.halt_reason} — yeni pozisyon açılmayacak")
            return self._save_state()
        return True

    # -------------------------------------------------------------- approve
    def approve_trade(self, balance, stop_distance_pct, take_profit_pct=None,
                      equity=None, max_position_pct=None):
        """Risk bütçesi onayı. Döner (notional|None, reason).

        FAZ16: `equity` verilirse pozisyon başına `max_position_pct` tavanı burada
        da uygulanır — `approve_trade` yüzde cap'ini atlayamaz. `balance` yalnız
        risk bütçesi tabanıdır; availableBalance equity yerine geçmez.
        """
        if not getattr(self, "health_ok", True):
            return None, f"entry_health: {getattr(self, 'health_reason', 'unhealthy')}"
        if self.is_halted:
            return None, f"halted: {self.halt_reason}"
        if self.daily_trades >= self.cfg["max_trades_per_day"]:
            return None, f"daily_limit ({self.daily_trades})"
        if self.consecutive_losses >= self.cfg["max_consecutive_losses"]:
            return None, f"loss_limit ({self.consecutive_losses})"
        _dl = self.cfg.get("max_daily_loss_usdt")
        _dpnl = float(getattr(self, "daily_pnl", 0.0) or 0.0)
        if _dl is not None and _dpnl <= -abs(float(_dl)):
            self.is_halted = True
            self.halt_reason = f"daily_loss {_dpnl:.2f} <= -{abs(float(_dl)):.2f} USDT"
            if hasattr(self, "_save_state"):
                self._save_state()
            log.critical(f"🚨 Günlük zarar limiti: {self.halt_reason}")
            return None, f"daily_loss_limit ({_dpnl:.2f})"
        if stop_distance_pct is None or stop_distance_pct <= 0:
            return None, "invalid_stop"

        import edge_engine

        fee_pct = self.cfg.get("fee_pct_round_trip", 0.0008)
        slip_pct = self.cfg.get("slippage_pct_round_trip", 0.0004)
        if take_profit_pct is not None:
            rr, rr_ok, detail = edge_engine.net_reward_risk(
                take_profit_pct, stop_distance_pct, fee_pct, slip_pct)
            min_rr = float(self.cfg.get("min_net_reward_risk",
                                        edge_engine.EDGE_DEFAULTS["min_net_reward_risk"]))
            if not rr_ok or rr < min_rr:
                log.warning(f"  RiskEngine net R/R reddi: rr={rr:.2f} < {min_rr:.2f} ({detail})")
                return None, f"net_rr ({rr:.2f} < {min_rr:.2f})"

        notional, reason = edge_engine.risk_notional(
            balance,
            float(self.cfg["risk_per_trade_pct"]),
            stop_distance_pct,
            fee_pct,
            slip_pct,
            float(self.cfg.get("kelly_fraction", 0.25)),
            float(self.cfg["min_notional"]),
            float(self.cfg["max_notional"]),
            policy=str(self.cfg.get("min_notional_policy", "refuse")),
        )
        if notional is None:
            log.warning(f"  RiskEngine boyut reddi: {reason}")
            return None, reason

        pct = max_position_pct if max_position_pct is not None \
            else self.cfg.get("max_position_pct")
        if pct and equity:
            try:
                cap = float(equity) * float(pct) / 100.0
            except (TypeError, ValueError):
                return None, "invalid_equity_cap"
            if cap > 0 and notional > cap:
                log.info(f"  RiskEngine yüzde tavanı: {notional:.2f} → {cap:.2f} "
                         f"(equity={float(equity):.2f} × %{float(pct):.1f})")
                notional = cap
                if notional < float(self.cfg["min_notional"]):
                    return None, f"below_min_notional_after_pct_cap ({notional:.2f})"
        return notional, "approved"

    # --------------------------------------------------------------- counters
    def register_open(self, key=None):
        """Günlük açılış sayacını ATOMİK artır; `key` ile idempotent."""
        if key is not None and key in self._open_keys:
            log.info(f"  register_open: {key} zaten sayıldı (idempotent)")
            return True
        now = iso_utc(utc_now())
        keys = set(self._open_keys)
        if key is not None:
            keys.add(key)
        try:
            with self._db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    try:
                        cur = conn.execute(
                            "UPDATE risk_state SET daily_trades=daily_trades+1, updated_at=?, "
                            "open_keys=? WHERE id=1", (now, json.dumps(sorted(keys))))
                    except sqlite3.OperationalError as oe:
                        if "no such column" not in str(oe):
                            raise
                        self.migration_required = True
                        cur = conn.execute(
                            "UPDATE risk_state SET daily_trades=daily_trades+1, updated_at=? "
                            "WHERE id=1", (now,))
                    if conn.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RiskStoreError("register_open yazılmadı (changes!=1)")
                    row = conn.execute("SELECT daily_trades FROM risk_state WHERE id=1").fetchone()
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            self.daily_trades = int(row[0])
            self._open_keys = keys
            self.state["daily_trades"] = self.daily_trades
            return True
        except Exception as e:
            self._block(f"register_open KAYDEDILEMEDI: {e}")
            return False

    def reset_daily(self, reset_date=None):
        """Günlük sayaç reseti. HALTED ve consecutive_losses KORUNUR.

        FAZ14x entegrasyon: `reset_date` (UTC gün anahtarı) verilebilir —
        sync'in hesapladığı gün kalıcılaştırılır; verilmezse utc_day().
        Her iki durumda tarih _save_state ile UPSERT'e girer (donmuş-tarih
        restart bug'ı kapalı)."""
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.day_start_equity = 0.0
        self.day_start_equity_date = utc_day()
        self.day_start_equity_at = None
        self._open_keys = set()
        self.last_reset_date = reset_date or utc_day()
        self.state["last_reset_date"] = self.last_reset_date
        self.state["day_start_equity"] = 0.0
        self.state["day_start_equity_date"] = self.day_start_equity_date
        self.state["day_start_equity_at"] = None
        ok = self._save_state()
        log.info(f"🌅 Günlük sayaçlar sıfırlandı ({self.last_reset_date}) "
                 f"halted={self.is_halted} streak={self.consecutive_losses}")
        return ok
