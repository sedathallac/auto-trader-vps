"""risk_engine.py — Profesyonel risk katmanı
Drawdown, ardışık zarar, devre kesici, dinamik boyutlandırma
"""
import logging
import json
from pathlib import Path

log = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self, config):
        self.cfg = config
        self.peak_equity = 0.0
        self.current_equity = 0.0
        self.current_drawdown = 0.0
        self.consecutive_losses = 0
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.is_halted = False
        self.halt_reason = None
        self.state = {}
        self.state_file = Path.home() / ".risk_engine_state.json"
        self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                d = json.loads(self.state_file.read_text())
                self.peak_equity = d.get("peak_equity", 0.0)
                self.consecutive_losses = d.get("consecutive_losses", 0)
                self.daily_trades = d.get("daily_trades", 0)
                self.daily_pnl = d.get("daily_pnl", 0.0)
                self.state = d
            except Exception as e:
                __import__("logging").getLogger("risk_engine").warning(f"state yüklenemedi: {e}")

    def _save_state(self):
        self.state.update({
            "peak_equity": self.peak_equity,
            "consecutive_losses": self.consecutive_losses,
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
        })
        self.state_file.write_text(json.dumps(self.state, indent=2))

    def update_equity(self, current_equity):
        """Her döngüde çağır, drawdown + devre kesici kontrolü"""
        self.current_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        if self.peak_equity > 0:
            self.current_drawdown = (self.peak_equity - current_equity) / self.peak_equity
        if self.current_drawdown >= self.cfg["max_drawdown_pct"] and not self.is_halted:
            self.is_halted = True
            self.halt_reason = f"Drawdown %{self.current_drawdown*100:.1f} (limit %{self.cfg['max_drawdown_pct']*100:.0f}%)"
            log.critical(f"🚨 DEVRE KESİCİ: {self.halt_reason}")
            self._save_state()
        self._save_state()

    def update_exposure(self, balance, positions):
        """Toplam marjin kullanımını hesapla ve limiti aşarsa durdur"""
        if balance <= 0:
            return
        total_margin = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            # Cross margin'de initial margin yaklaşık notional / leverage
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

    def register_trade_result(self, pnl_quote):
        """Pozisyon kapandığında çağır (TP/SL tetiklendiğinde)"""
        self.daily_pnl += pnl_quote
        if pnl_quote < 0:
            self.consecutive_losses += 1
            log.warning(f"Zarar #{self.consecutive_losses} (toplam günlük: {self.daily_pnl:+.4f})")
            if self.consecutive_losses >= self.cfg["max_consecutive_losses"]:
                log.warning(f"⚠️  Ardışık zarar limitine ({self.cfg['max_consecutive_losses']}) ulaşıldı")
        else:
            if self.consecutive_losses > 0:
                log.info(f"Kazanç! Ardışık zarar sıfırlandı (önceki: {self.consecutive_losses})")
            self.consecutive_losses = 0
        self._save_state()

    def approve_trade(self, balance, stop_distance_pct):
        """Sinyalden gelen işlem talebini denetle"""
        if self.is_halted:
            return None, f"halted: {self.halt_reason}"
        if self.daily_trades >= self.cfg["max_trades_per_day"]:
            return None, f"daily_limit ({self.daily_trades})"
        if self.consecutive_losses >= self.cfg["max_consecutive_losses"]:
            return None, f"loss_limit ({self.consecutive_losses})"

        if stop_distance_pct <= 0:
            return None, "invalid_stop"
        fee_pct = self.cfg.get("fee_pct_round_trip", 0.0008)
        kelly = self.cfg.get("kelly_fraction", 0.25)
        effective_sl = stop_distance_pct + fee_pct
        risk_amount = balance * self.cfg["risk_per_trade_pct"]
        raw_notional = risk_amount / effective_sl
        notional = raw_notional * kelly
        notional = max(self.cfg["min_notional"], min(notional, self.cfg["max_notional"]))
        return notional, "approved"

    def register_open(self):
        self.daily_trades += 1
        self._save_state()

    def reset_daily(self):
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self._save_state()
        log.info("🌅 Günlük sayaçlar sıfırlandı")
