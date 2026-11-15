"""risk_engine.py — SQLite-based risk ledger (C3)
Drawdown, ardışık zarar, devre kesici, dinamik boyutlandırma.
JSON state dosyaları yerine transactional SQLite ledger kullanır.
"""
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_DB_FILE = Path.home() / "risk_ledger.db"


class RiskEngine:
    def __init__(self, config):
        self.cfg = config
        self.db_file = Path(config.get("db_file", str(DEFAULT_DB_FILE)))
        self.peak_equity = 0.0
        self.current_equity = 0.0
        self.day_start_equity = 0.0
        self.current_drawdown = 0.0
        self.consecutive_losses = 0
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.is_halted = False
        self.halt_reason = None
        self.margin_usage_pct = 0.0
        self.last_reset_date = None
        self.state = {}
        self._ensure_schema()
        self._load_state()

    def _ensure_schema(self):
        """risk_state tablosunu garanti eder.

        Eski kurulumlarda tablo başka bir araçla oluşturulmuştu; yeni bir state dizini
        (shadow/live) açıldığında tablo yoktu ve `_load_state` FAIL-CLOSED halt'a düşüyordu.
        Şema burada tanımlıdır: hiçbir kurulum adımı elle tablo yaratmak zorunda değil.
        """
        try:
            with self._db() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS risk_state (
                        id INTEGER PRIMARY KEY,
                        peak_equity REAL DEFAULT 0,
                        consecutive_losses INTEGER DEFAULT 0,
                        daily_trades INTEGER DEFAULT 0,
                        daily_pnl REAL DEFAULT 0,
                        last_reset_date TEXT,
                        is_halted INTEGER DEFAULT 0,
                        halt_reason TEXT,
                        updated_at TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            log.critical(f"🚨 C3: risk_state şeması oluşturulamadı, FAIL-CLOSED: {e}")
            self.is_halted = True
            self.halt_reason = f"DB schema error: {e}"

    @contextmanager
    def _db(self):
        """Context manager: connection + PRAGMA + close."""
        conn = sqlite3.connect(str(self.db_file), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def _load_state(self):
        try:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT peak_equity, consecutive_losses, daily_trades, daily_pnl, "
                    "is_halted, halt_reason, last_reset_date FROM risk_state WHERE id=1"
                ).fetchone()
                if row:
                    self.peak_equity = float(row[0] or 0)
                    self.consecutive_losses = int(row[1] or 0)
                    self.daily_trades = int(row[2] or 0)
                    self.daily_pnl = float(row[3] or 0)
                    self.is_halted = bool(row[4])
                    self.halt_reason = row[5]
                    self.last_reset_date = row[6]
                    self.state = {
                        "peak_equity": self.peak_equity,
                        "consecutive_losses": self.consecutive_losses,
                        "daily_trades": self.daily_trades,
                        "daily_pnl": self.daily_pnl,
                        "is_halted": self.is_halted,
                        "halt_reason": self.halt_reason,
                        "last_reset_date": self.last_reset_date,
                    }
                else:
                    log.warning("risk_state tablosu bos, defaults kullaniliyor")
        except Exception as e:
            log.critical(f"🚨 C3: risk state yuklenemedi, FAIL-CLOSED: {e}")
            self.is_halted = True
            self.halt_reason = f"DB load error: {e}"

    def _save_state(self):
        """UPSERT risk_state. Caller state['last_reset_date'] override'ini de okur."""
        if "last_reset_date" in self.state:
            self.last_reset_date = self.state["last_reset_date"]
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._db() as conn:
                conn.execute("""
                    INSERT INTO risk_state
                        (id, peak_equity, consecutive_losses, daily_trades, daily_pnl,
                         last_reset_date, is_halted, halt_reason, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        peak_equity=excluded.peak_equity,
                        consecutive_losses=excluded.consecutive_losses,
                        daily_trades=excluded.daily_trades,
                        daily_pnl=excluded.daily_pnl,
                        is_halted=excluded.is_halted,
                        halt_reason=excluded.halt_reason,
                        updated_at=excluded.updated_at
                """, (
                    self.peak_equity, self.consecutive_losses, self.daily_trades,
                    self.daily_pnl, self.last_reset_date or now[:10],
                    1 if self.is_halted else 0, self.halt_reason, now
                ))
                conn.commit()
        except Exception as e:
            log.critical(f"🚨 C3: risk state KAYDEDILEMEDI: {e}")

    def record_trade(self, executor_id, trading_pair, net_pnl_quote, closed_at_iso=None):
        """C3: Atomic, idempotent PnL record.
        Returns (inserted: bool, message: str).
        Duplicate executor_id -> (False, 'already-recorded')."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._db() as conn:
                try:
                    conn.execute("""
                        INSERT INTO trade_ledger
                            (executor_id, trading_pair, net_pnl_quote, closed_at, recorded_at, source)
                        VALUES (?, ?, ?, ?, ?, 'sync')
                    """, (executor_id, trading_pair, net_pnl_quote, closed_at_iso, now))
                except sqlite3.IntegrityError:
                    conn.rollback()
                    return False, "already-recorded"

                self.daily_pnl += net_pnl_quote
                if net_pnl_quote < 0:
                    self.consecutive_losses += 1
                    if self.consecutive_losses >= self.cfg["max_consecutive_losses"]:
                        log.warning(f"⚠️  Ardışık zarar limitine ({self.cfg['max_consecutive_losses']}) ulaşıldı")
                else:
                    if self.consecutive_losses > 0:
                        log.info(f"Kazanç! Ardışık zarar sıfırlandı (önceki: {self.consecutive_losses})")
                    self.consecutive_losses = 0

                conn.execute("""
                    UPDATE risk_state SET daily_pnl=?, consecutive_losses=?, updated_at=?
                    WHERE id=1
                """, (self.daily_pnl, self.consecutive_losses, now))
                conn.commit()
                return True, f"pnl={net_pnl_quote:+.4f}"
        except Exception as e:
            log.critical(f"🚨 C3: record_trade HATA: {e}")
            return False, f"error: {e}"

    def register_trade_result(self, pnl_quote, executor_id=None, trading_pair=None, closed_at_iso=None):
        """Backward-compat. C3: use record_trade() when executor_id available."""
        if executor_id:
            return self.record_trade(executor_id, trading_pair, pnl_quote, closed_at_iso)
        # Legacy path (no idempotency - avoid in production)
        log.warning("register_trade_result without executor_id — no idempotency")
        self.daily_pnl += pnl_quote
        if pnl_quote < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self._save_state()

    def update_equity(self, current_equity):
        self.current_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        if self.peak_equity > 0:
            self.current_drawdown = (self.peak_equity - current_equity) / self.peak_equity
        # FAZ10: gerçekleşmemiş zarar da günlük limiti tetikler. Gün başı equity
        # baz alınır; realized-only muhasebe açık pozisyon zararını gizliyordu.
        today = datetime.now().date().isoformat()
        if self.state.get("day_start_equity_date") != today or not self.day_start_equity:
            self.day_start_equity = current_equity
            self.state["day_start_equity"] = current_equity
            self.state["day_start_equity_date"] = today
        if self.day_start_equity > 0:
            day_loss = (self.day_start_equity - current_equity) / self.day_start_equity
            limit = float(self.cfg.get("max_daily_loss_pct", 0.05))
            # FAZ14: limit equity yuzdesidir; USDT karsiligi gun basi equity'den turetilir.
            _limit_usdt = self.day_start_equity * limit
            if day_loss >= limit and not self.is_halted:
                self.is_halted = True
                self.halt_reason = (f"daily_loss_pct %{day_loss*100:.2f} "
                                    f"(limit %{limit*100:.0f}% = {_limit_usdt:.2f} USDT)")
                log.critical(f"🚨 GÜNLÜK EQUITY KESİCİ: {self.halt_reason}")
        if self.current_drawdown >= self.cfg["max_drawdown_pct"] and not self.is_halted:
            self.is_halted = True
            self.halt_reason = f"Drawdown %{self.current_drawdown*100:.1f} (limit %{self.cfg['max_drawdown_pct']*100:.0f}%)"
            log.critical(f"🚨 DEVRE KESİCİ: {self.halt_reason}")
        self._save_state()

    def update_exposure(self, balance, positions):
        if balance <= 0:
            return
        total_margin = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            notional = abs(amt) * float(p.get("markPrice", p.get("entryPrice", 0)))
            lev = float(p.get("leverage", 1))
            total_margin += notional / lev
        usage = total_margin / balance if balance > 0 else 0
        self.margin_usage_pct = usage
        log.info(f"📊 Marjin kullanımı: %{usage*100:.1f} (limit: %{self.cfg.get('max_total_margin_pct', 0.4)*100:.0f})")
        if usage > self.cfg.get("max_total_margin_pct", 0.4):
            self.is_halted = True
            self.halt_reason = f"Exposure limit aşıldı (%{usage*100:.1})"
            log.critical(f"🚨 {self.halt_reason} — yeni pozisyon açılmayacak")

    def approve_trade(self, balance, stop_distance_pct, take_profit_pct=None):
        """Risk bütçesi onayı. Döner (notional|None, reason).

        FAZ10: maliyetler (komisyon + slipaj) stop mesafesine eklenir; net R/R
        kapısı ve bütçe taşması kapısı edge_engine'dedir. `take_profit_pct`
        verilmezse R/R kapısı atlanır (geriye uyumlu çağrılar).
        """
        if self.is_halted:
            return None, f"halted: {self.halt_reason}"
        if self.daily_trades >= self.cfg["max_trades_per_day"]:
            return None, f"daily_limit ({self.daily_trades})"
        if self.consecutive_losses >= self.cfg["max_consecutive_losses"]:
            return None, f"loss_limit ({self.consecutive_losses})"
        # FAZ9 live: günlük zarar tavanı USDT cinsinden (kademe limiti). Anahtarsız çağrıda davranış değişmez.
        _dl = self.cfg.get("max_daily_loss_usdt")
        if _dl is not None and self.daily_pnl <= -abs(float(_dl)):
            self.is_halted = True
            self.halt_reason = f"daily_loss {self.daily_pnl:.2f} <= -{abs(float(_dl)):.2f} USDT"
            self._save_state()
            log.critical(f"🚨 Günlük zarar limiti: {self.halt_reason}")
            return None, f"daily_loss_limit ({self.daily_pnl:.2f})"
        if stop_distance_pct <= 0:
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
        return notional, "approved"

    def register_open(self):
        self.daily_trades += 1
        self._save_state()

    def reset_daily(self):
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.day_start_equity = 0.0
        self.state.pop("day_start_equity", None)
        self.state.pop("day_start_equity_date", None)
        self._save_state()
        log.info("🌅 Günlük sayaçlar sıfırlandı")
