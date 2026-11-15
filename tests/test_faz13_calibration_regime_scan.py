"""FAZ13 — FAZ6 kalibrasyon bağlama + aile tespiti + range/trend ayrımı + tarama hunisi.

Kanıtlanan davranışlar:
  * posterior OKUMASI ayrı (salt-okunur) kalibrasyon DB'sinden yapılır; yazma kendi DB'sinde kalır
  * kalibre hücrede p_real min_p_real'i geçebilir; kalibre olmayan hücre nötr 0.5'te kalır
  * rejim 15m ADX, veto 1h/4h ADX ile raporlanır (log'daki "range + ADX=43" çelişkisi görünür olur)
  * tarama hunisi sayıları loglanır ve scan_top en az n aday döndürür
"""
import inspect
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import strategy_engine as se


def _calib_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE signal_outcomes (signal TEXT, regime TEXT, resolved INTEGER, "
              "real INTEGER, sl_pct REAL)")
    c.executemany("INSERT INTO signal_outcomes VALUES (?,?,1,?,0.01)", rows)
    c.commit()
    c.close()


def _rows(signal, regime, n, wins):
    return [(signal, regime, 1 if i < wins else 0) for i in range(n)]


# ------------------------------------------------------------------ B) kalibrasyon
def test_calibration_loaded_on_startup(tmp_path):
    db = tmp_path / "calib.db"
    _calib_db(str(db), _rows("wick_rejection", "trend", 120, 100))
    store = se.SignalStore(str(tmp_path / "own.db"), calib_db=str(db))
    cov = store.calibration_coverage(se.CONFIRM_DEFAULTS)
    assert cov["cells"] == 1 and cov["calibrated_cells"] == 1
    assert cov["scorable_rows"] == 120 and cov["min_events"] == 100
    p_fake, calibrated = store.p_fake("wick_rejection", "trend", se.CONFIRM_DEFAULTS)
    assert calibrated is True and p_fake < 0.5


def test_p_real_above_min_when_calibrated():
    readings = [
        se.SignalReading(se.FAM_EXHAUSTION, "rsi_extreme", 1, 0.9),
        se.SignalReading(se.FAM_STRUCTURE, "sr_proximity", 1, 0.9),
    ]
    trends = [{"tf": "1h", "score": 1.0, "adx": 18.0, "adx_strength": 0.12},
              {"tf": "4h", "score": 1.0, "adx": 16.0, "adx_strength": 0.04}]
    v = se.evaluate(1, readings, trends, "range", lambda n, g: (0.2, True), se.CONFIRM_DEFAULTS, regime_adx=12.0)
    assert v.allow is True and v.p_real >= se.CONFIRM_DEFAULTS["confirm_min_p_real"]
    assert v.calibrated is True and set(v.families) == {se.FAM_EXHAUSTION, se.FAM_STRUCTURE}


def test_uncalibrated_cell_stays_neutral(tmp_path):
    db = tmp_path / "calib.db"
    _calib_db(str(db), _rows("wick_rejection", "trend", 99, 90))
    store = se.SignalStore(str(tmp_path / "own.db"), calib_db=str(db))
    assert store.p_fake("wick_rejection", "trend", se.CONFIRM_DEFAULTS) == (0.5, False)
    assert store.calibration_coverage(se.CONFIRM_DEFAULTS)["calibrated_cells"] == 0


def test_calibration_db_is_read_only(tmp_path):
    db = tmp_path / "calib.db"
    _calib_db(str(db), _rows("wick_rejection", "trend", 120, 100))
    own = tmp_path / "own.db"
    store = se.SignalStore(str(own), calib_db=str(db))
    reading = se.SignalReading(se.FAM_STRUCTURE, "wick_rejection", 1, 0.8)
    assert store.record_event("UNIUSDT", reading, "trend", 1700000000000, 5.0, 8, sl_pct=0.01) is True
    assert sqlite3.connect(str(own)).execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0] == 1
    assert sqlite3.connect(str(db)).execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0] == 120


def test_missing_calibration_db_falls_back_neutral(tmp_path):
    store = se.SignalStore(str(tmp_path / "own.db"), calib_db=str(tmp_path / "yok.db"))
    assert store.p_fake("wick_rejection", "trend", se.CONFIRM_DEFAULTS) == (0.5, False)
    assert "error" in store.calibration_coverage(se.CONFIRM_DEFAULTS)


def test_faz6_uses_calibration_db():
    src = inspect.getsource(auto_trader._faz6_confirm)
    assert "calib_db=_se.CALIB_DB" in src
    assert "adx15=" in src and "adx_trend=" in src


def test_calibration_status_logged_once(monkeypatch, caplog):
    calls = {"n": 0}

    def fake_coverage(self, cfg=None):
        calls["n"] += 1
        return {"path": "x", "cells": 3, "calibrated_cells": 1, "scorable_rows": 655, "min_events": 100}

    monkeypatch.setattr(se.SignalStore, "calibration_coverage", fake_coverage)
    monkeypatch.setattr(auto_trader, "_CALIB_LOGGED", {"done": False})
    with caplog.at_level(logging.INFO):
        auto_trader._log_calibration_status()
        auto_trader._log_calibration_status()
    assert calls["n"] == 1
    assert any("Kalibrasyon:" in r.message for r in caplog.records)


# ------------------------------------------------------------------ D) range/trend
def test_adx_above_40_is_not_range():
    assert se.regime_from_adx(43.0) == "trend"
    assert se.regime_from_adx(22.0) == "mid"
    assert se.regime_from_adx(10.0) == "range"
    assert se.regime_from_adx(None) == "unknown"


def test_range_does_not_have_strong_trend_against():
    trends = [{"tf": "1h", "score": -1.0, "adx": 40.5, "adx_strength": 1.0},
              {"tf": "4h", "score": -1.0, "adx": 43.0, "adx_strength": 1.0}]
    readings = [se.SignalReading(se.FAM_STRUCTURE, "wick_rejection", 1, 0.79)]
    v = se.evaluate(1, readings, trends, "range", lambda n, g: (0.5, False),
                    se.CONFIRM_DEFAULTS, regime_adx=11.4)
    assert v.veto is True and v.allow is False
    assert v.regime == "range" and v.regime_adx == 11.4 and v.trend_adx == 43.0
    assert "ADX_15m=11" in v.reason and "ADX_trend=43" in v.reason


def test_verdict_reports_both_adx_on_family_reject():
    trends = [{"tf": "1h", "score": 1.0, "adx": 25.7, "adx_strength": 0.43},
              {"tf": "4h", "score": 1.0, "adx": 51.5, "adx_strength": 1.0}]
    readings = [se.SignalReading(se.FAM_STRUCTURE, "sr_proximity", 1, 0.13)]
    v = se.evaluate(-1, readings, trends, "mid", lambda n, g: (0.5, False),
                    se.CONFIRM_DEFAULTS, regime_adx=23.8)
    assert v.allow is False and v.families == {}
    assert v.regime_adx == 23.8 and v.trend_adx == 51.5


# ------------------------------------------------------------------ E) tarama evreni
class _FakeBinance:
    def __init__(self, n):
        self._tickers = [
            {"symbol": f"C{i}USDT", "quoteVolume": str(1_000_000_000 - i), "priceChangePercent": "5.0",
             "lastPrice": "1.0"}
            for i in range(n)
        ] + [{"symbol": "BTCUSDT", "quoteVolume": "900000000", "priceChangePercent": "0.5", "lastPrice": "1"}]

    def tickers(self):
        return self._tickers


def test_scan_returns_at_least_top_n_candidates():
    picks = auto_trader.scan_top(_FakeBinance(30), 20)
    assert len(picks) == 20
    assert auto_trader._SCAN_FUNNEL["picks"] == 20


def test_scan_funnel_counts():
    auto_trader.scan_top(_FakeBinance(3), 20)
    f = auto_trader._SCAN_FUNNEL
    assert f["tickers"] == 4 and f["usdt"] == 4
    assert f["volume"] == 4 and f["pct"] == 3 and f["picks"] == 3


def test_scan_thresholds_env_overridable(monkeypatch):
    monkeypatch.setenv("MIN_VOLUME_USDT", "50000000")
    assert auto_trader._env_float("MIN_VOLUME_USDT", 200_000_000) == 50_000_000
    monkeypatch.setenv("MIN_VOLUME_USDT", "bozuk")
    assert auto_trader._env_float("MIN_VOLUME_USDT", 200_000_000) == 200_000_000
