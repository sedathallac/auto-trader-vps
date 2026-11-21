"""BLOK13 — Risk motoru kapanışı ve giriş kilidi (kabul kapıları §9).

Kanıtlanmış kök nedenler (VPS 2026-11-21, salt-okunur inceleme):
  1. trade_ledger'daki 27 satır naive (saatsiz) closed_at + NULL
     closed_at_epoch → parse_close_epoch None → health_ok=False (fail-closed
    , DOĞRU davranış).
  2. Kurulu hummingbot-api `offset` alanını tanımıyor (cursor sözleşmesi);
     eski yürüyüş kalıcı PARTIAL → sync erken döner → watermark İLERLEMEZ.
  3. `GÜNLÜK EQUITY KESİCİ (limit %5% = 5.00 USDT)` + `equity geçersiz (0.0)`
     CRITICAL satırları F25 öz-testinin SCRATCH motorundandı (day_start=100);
     üretim config 2% iken öz-test 5% varsayılanla koşuyordu ve scratch
     logları üretim günlüğüne sızıyordu. Artık öz-test etkili config ile
     koşar ve üretim günlüğüne YAZMAZ.

Bu dosyadaki hiçbir test gerçek Binance/Hummingbot/emir yoluna dokunmaz;
create_executor ve requests.post sahtelenir ve 0 çağrı doğrulanır.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import risk_engine as RE

FULL_DDL = """
CREATE TABLE risk_state (
    id INTEGER PRIMARY KEY CHECK (id=1), peak_equity REAL NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0, daily_trades INTEGER NOT NULL DEFAULT 0,
    daily_pnl REAL NOT NULL DEFAULT 0, last_reset_date TEXT NOT NULL,
    is_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT, updated_at TEXT NOT NULL,
    day_start_equity REAL DEFAULT 0, day_start_equity_date TEXT,
    day_start_equity_at TEXT, open_keys TEXT);
CREATE TABLE trade_ledger (
    executor_id TEXT PRIMARY KEY, trading_pair TEXT, net_pnl_quote REAL NOT NULL DEFAULT 0,
    closed_at TEXT, recorded_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'sync',
    closed_at_epoch REAL);
CREATE TABLE sync_meta (
    id INTEGER PRIMARY KEY CHECK (id=1), last_close_ts REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL);
INSERT INTO risk_state (id, last_reset_date, updated_at) VALUES (1, '2020-01-01', 'x');
INSERT INTO sync_meta (id, last_close_ts, updated_at) VALUES (1, 12345, 'x');
"""

CFG = {
    "max_trades_per_day": 20, "max_consecutive_losses": 3,
    "risk_per_trade_pct": 0.015, "kelly_fraction": 0.25,
    "max_drawdown_pct": 0.20, "max_daily_loss_pct": 0.02,
    "min_notional": 5.5, "max_notional": 999999.0,
    "fee_pct_round_trip": 0.0008, "slippage_pct_round_trip": 0.0004,
    "min_net_reward_risk": 1.20, "min_notional_policy": "refuse",
}


def _db(tmp_path):
    db = tmp_path / "risk.db"
    con = sqlite3.connect(db)
    con.executescript(FULL_DDL)
    con.commit()
    con.close()
    return db


def _add_naive_rows(db, n=27):
    """Canlı örneğin birebir deseni: naive closed_at + NULL closed_at_epoch."""
    con = sqlite3.connect(db)
    for i in range(n):
        con.execute(
            "INSERT INTO trade_ledger (executor_id, trading_pair, net_pnl_quote,"
            " closed_at, recorded_at, source, closed_at_epoch)"
            " VALUES (?, 'BTC-USDT', ?, '2026-11-13T14:28:55.952301',"
            " '2026-11-15T19:21:28.187732+00:00', 'sync', NULL)",
            (f"U{i:02d}", -0.01 * i))
    con.commit()
    con.close()


def _eng(db, **extra):
    cfg = dict(CFG)
    cfg.update({"db_file": str(db), "db_create": True})
    cfg.update(extra)
    return RE.RiskEngine(cfg)


class _NoOrderHB:
    """create_executor ÇAĞRILIRSA patlayan sahte HB istemcisi."""
    auth = ("u", "p")
    create_calls = []

    def create_executor(self, *a, **k):
        _NoOrderHB.create_calls.append((a, k))
        raise AssertionError("create_executor ASLA çağrılmamalı")


@pytest.fixture
def clean_health():
    saved = auto_trader._entry_health_reasons()
    with auto_trader._ENTRY_HEALTH_LOCK:
        auto_trader._ENTRY_HEALTH.clear()
    yield
    with auto_trader._ENTRY_HEALTH_LOCK:
        auto_trader._ENTRY_HEALTH.clear()
        auto_trader._ENTRY_HEALTH.update(saved)


class TestGirisKilitleri:
    def setup_method(self):
        _NoOrderHB.create_calls.clear()
        self._orig_post = auto_trader.requests.post
        self.net_calls = []

        def _spy_post(*a, **k):
            self.net_calls.append(a)
            raise AssertionError(f"testte HTTP POST yasak: {a[:2]}")

        auto_trader.requests.post = _spy_post

    def teardown_method(self):
        auto_trader.requests.post = self._orig_post

    # §9.1 — 27 unresolved closure → health_ok=False → giriş yok
    def test_27_unresolved_closures_fail_closed(self, tmp_path):
        db = _db(tmp_path)
        _add_naive_rows(db, 27)
        risk = _eng(db)
        assert risk.health_ok is False
        assert "ledger_unresolved_timestamps: 27" in risk.health_reason
        notional, why = risk.approve_trade(1000.0, 0.02, equity=3500.0)
        assert notional is None
        assert why.startswith("entry_health:")
        ok, gate_why = auto_trader._risk_entry_gate(risk)
        assert ok is False and "risk_store:" in gate_why
        assert not _NoOrderHB.create_calls
        assert not self.net_calls

    # §9.2 — HALTED=True → giriş yok (kaynak: DB'deki gerçek halt_reason)
    def test_halted_denies_entry(self, tmp_path):
        db = _db(tmp_path)
        con = sqlite3.connect(db)
        con.execute("UPDATE risk_state SET is_halted=1,"
                    " halt_reason='Drawdown %100.0 (limit %20%)' WHERE id=1")
        con.commit()
        con.close()
        risk = _eng(db)
        assert risk.is_halted is True
        assert risk.health_ok is True          # DB sağlıklı — tek engel HALTED
        notional, why = risk.approve_trade(1000.0, 0.02, equity=3500.0)
        assert notional is None and why.startswith("halted:")
        ok, gate_why = auto_trader._risk_entry_gate(risk)
        assert ok is False and "halted:" in gate_why
        # HALTED onay yolundan ASLA kalkmaz
        risk.approve_trade(1000.0, 0.02)
        assert risk.is_halted is True
        assert not self.net_calls

    # §9.3 — Equity None/0.0 → update_equity redder; zincirleme giriş kapalı
    def test_invalid_equity_blocks_entry(self, tmp_path):
        db = _db(tmp_path)
        risk = _eng(db)
        assert risk.update_equity(None) is False
        assert risk.update_equity(0.0) is False
        assert risk.health_ok is False          # fail-closed: şüphede kapalı
        notional, why = risk.approve_trade(1000.0, 0.02, equity=1000.0)
        assert notional is None and why.startswith("entry_health:")
        ok, _ = auto_trader._risk_entry_gate(risk)
        assert ok is False
        # DD sentinel'e sızamaz (F25): dd korunur, 3. sentinelde halt
        risk.update_equity(3491.0)
        dd = risk.dd_ratio
        risk.update_equity(0.0)
        assert risk.dd_ratio == dd
        risk.update_equity(0.0)
        risk.update_equity(0.0)
        assert risk.is_halted is True
        assert risk.halt_reason.startswith("EQUITY_UNAVAILABLE")
        assert not self.net_calls

    # §9.5 — pozitif kontrol: sağlıklı + HALTED yoksa onay VERİLEBİLİR
    def test_healthy_engine_can_approve(self, tmp_path):
        db = _db(tmp_path)
        risk = _eng(db)
        notional, why = risk.approve_trade(1000.0, 0.02, equity=1000.0)
        assert why == "approved" and notional is not None
        ok, _ = auto_trader._risk_entry_gate(risk)
        assert ok is True


class TestSyncPaginationWatermark:
    """§9.4 — pagination incomplete → watermark ilerlemez + giriş kapalı."""

    def test_incomplete_pagination_freezes_watermark(self, tmp_path, clean_health):
        db = _db(tmp_path)
        orig_post = auto_trader.requests.post

        def _server(url, json=None, auth=None, timeout=None):
            class _R:
                status_code = 200

                def json(self):
                    # Kurulu API hatasının taklidi: cursor/offset ne olursa
                    # AYNI sayfa döner, has_more daima True, cursor tekrar eder.
                    return {"data": [{"executor_id": "E1",
                                      "status": "TERMINATED",
                                      "close_timestamp": 9999999999,
                                      "net_pnl_quote": -0.5,
                                      "trading_pair": "SOL-USDT"}],
                            "pagination": {"limit": 1, "has_more": True,
                                           "next_cursor": "c1",
                                           "total_count": 2}}
            return _R()

        auto_trader.requests.post = _server
        try:
            risk = _eng(db)
            auto_trader.sync_risk_engine(risk, _NoOrderHB(), binance=None)
        finally:
            auto_trader.requests.post = orig_post
        # watermark fixture-değeri 12345 — İLERLEMEMELİ
        con = sqlite3.connect(db)
        ts = con.execute("SELECT last_close_ts FROM sync_meta WHERE id=1").fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0]
        con.close()
        assert ts == 12345
        assert n == 0, "eksik geçmişten kayıt YAZILMAMALI"
        assert "sync" in auto_trader._entry_health_reasons()
        ok, why = auto_trader._risk_entry_gate(risk)
        assert ok is False and "ENTRY_HEALTH:sync" in why
        assert not _NoOrderHB.create_calls


class TestLogHonesty:
    """§9.6 — koruma denetimi logu tek başına ENTRY_ALLOWED değildir."""

    SRC = open(auto_trader.__file__, encoding="utf-8").read()

    def test_misleading_message_removed(self):
        assert "yeni giriş izni var" not in self.SRC

    def test_honest_intermediate_message_present(self):
        honest = ("Koruma denetimleri geçti; nihai giriş uygunluğu "
                  "henüz doğrulanmadı.")
        assert honest in self.SRC
        # dürüst satır izin İDDİA etmez
        line = [l for l in self.SRC.splitlines() if honest in l][0]
        assert "ENTRY_ALLOWED" not in line
        assert "izni var" not in line

    def test_entry_allowed_only_after_all_gates(self):
        # run_once içinde: dürüst koruma mesajı protect_then_gate çağrısından
        # SONRA ve CloseCoordinator/equity/HALTED bloklarından ÖNCE gelmeli.
        i_ptg = self.SRC.index("allow_new, _reasons = protect_then_gate")
        i_msg = self.SRC.index("Koruma denetimleri geçti")
        i_coord = self.SRC.index("B-1: CloseCoordinator/CloseIntentStore kurulamadı")
        i_halt = self.SRC.index("Sistem DURDU")
        assert i_ptg < i_msg < i_coord < i_halt
        # Gerçek ENTRY_ALLOWED: create_executor çağrısından hemen önce, aynı
        # blokta (arada başka POST/emir çağrısı yok).
        lines = self.SRC.splitlines()
        i_allowed_line = next(i for i, l in enumerate(lines)
                              if "ENTRY_ALLOWED: tüm kapılar geçti" in l)
        i_create_line = next(i for i, l in enumerate(lines)
                             if "er = hb.create_executor(" in l)
        assert i_allowed_line < i_create_line
        between = lines[i_allowed_line + 1:i_create_line]
        assert not any("create_executor" in l or "requests.post" in l
                       for l in between)

    def test_saved_halt_reason_not_shown_as_current(self):
        # Sistem DURDU satırı kayıtlı nedeni "geçmiş olay" diye etiketler
        assert "KAYITLI halt" in self.SRC and "(geçmiş olay)" in self.SRC


class TestSelftestNoiseAndLimits:
    """§9 kök-3 — öz-test üretimin etkili limitiyle koşar, log sızmaz."""

    def test_selftest_uses_effective_daily_limit_and_is_silent(self, caplog):
        import logging
        with caplog.at_level(logging.CRITICAL, logger="risk_engine"):
            ok, detail = RE.dd_selftest({"max_daily_loss_pct": 0.02,
                                         "max_drawdown_pct": 0.20})
        assert ok is True
        # scratch serisinin KESİCİ/sentinel satırları loglanmamalı
        recs = [r.getMessage() for r in caplog.records
                if r.name.startswith("risk_engine")]
        joined = " | ".join(recs)
        assert "GÜNLÜK EQUITY KESİCİ" not in joined
        assert "equity geçersiz" not in joined

    def test_selftest_rejects_corrupted_expectations(self, monkeypatch):
        monkeypatch.setitem(RE._DD_SELFTEST_EXPECTED, "dd_final", 0.999)
        ok, detail = RE.dd_selftest({"max_daily_loss_pct": 0.02})
        assert ok is False and "dd_final" in detail

    def test_run_once_passes_effective_cfg_to_selftest(self):
        src = open(auto_trader.__file__, encoding="utf-8").read()
        assert "dd_selftest(_risk_cfg)" in src
        # etkili limit her turda görünür (gizli 2%/5% ayrımı yok)
        assert "etkili max_daily_loss_pct" in src


class TestCreateExecutorNeverCalled:
    """§9.7 — yukarıdaki tüm senaryolarda create_executor 0 çağrı."""

    def test_zero_calls(self):
        assert _NoOrderHB.create_calls == []
