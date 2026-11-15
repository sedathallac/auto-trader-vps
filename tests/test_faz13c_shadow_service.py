"""FAZ13-C — shadow servisi: ters politika yalnız gözlem, emir yok, izole state.

Kanıtlanan davranışlar:
  * shadow birimi compare modunda ve HB_OFFLINE=1 ile çalışır (fail-closed ExecStartPre)
  * state dizini compare/live'dan ayrıdır
  * shadow hiçbir koşulda emir göndermez (dry-run zorlanır)
  * ters politika shadow'da uygulanır, canlıda uygulanmaz
  * canlı servis shadow'dan etkilenmez
"""
import configparser
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import strategy_engine as se

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT = os.path.join(ROOT, "deploy", "auto_trader_faz9_shadow.service")
ENV = os.path.join(ROOT, "deploy", "shadow.env.template")


def _unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(UNIT)
    return cp


def _env():
    out = {}
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_shadow_service_uses_compare_mode():
    cp = _unit()
    lines = _unit_lines()
    assert "EnvironmentFile=/etc/auto_trader/shadow.env" in lines
    assert "EnvironmentFile=/etc/auto_trader/keys.env" in lines
    assert 'test \\"$HB_OFFLINE\\" = 1' in cp["Service"]["ExecStartPre"]
    assert "--interval 30" in cp["Service"]["ExecStart"]
    assert _env()["STRATEGY_CONFIRMATION"] == "compare"
    assert _env()["HB_OFFLINE"] == "1"


def test_risk_engine_creates_its_schema_in_a_fresh_state_dir(tmp_path):
    """Yeni state dizininde risk_state tablosu yoktu → FAIL-CLOSED halt. Şema artık kodda."""
    import risk_engine
    db = tmp_path / "risk_ledger.db"
    eng = risk_engine.RiskEngine({"db_file": str(db)})
    assert eng.is_halted is False, f"fresh state dir halted: {eng.halt_reason}"
    import sqlite3
    cols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(risk_state)")]
    assert "peak_equity" in cols and "is_halted" in cols and "last_reset_date" in cols


def test_risk_engine_schema_is_idempotent(tmp_path):
    import risk_engine
    db = tmp_path / "risk_ledger.db"
    risk_engine.RiskEngine({"db_file": str(db)})._save_state()
    again = risk_engine.RiskEngine({"db_file": str(db)})
    assert again.is_halted is False and again.halt_reason is None


def _unit_lines():
    with open(UNIT) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def test_shadow_uses_separate_state_dir():
    lines = _unit_lines()
    assert "Environment=HOME=/var/lib/auto_trader_faz9_shadow" in lines
    rw = [ln for ln in lines if ln.startswith("ReadWritePaths=")]
    assert len(rw) == 1
    assert "/var/lib/auto_trader_faz9_shadow" in rw[0]
    assert "/var/lib/auto_trader_faz9_live" not in rw[0]
    assert "/var/lib/auto_trader_faz9 " not in rw[0] + " "


def test_shadow_does_not_place_orders():
    e = _env()
    assert e["HB_OFFLINE"] == "1" and e["LIVE_DRY_RUN"] == "1" and e["LIVE_MODE"] == "0"
    # HB_OFFLINE=1 open_coin içinde dry_run'ı zorlar; create_executor'a hiç gidilmez.
    src = inspect.getsource(auto_trader.open_coin)
    assert "if HB_OFFLINE:\n        dry_run = True" in src


def test_reverse_policy_applied_in_shadow_mode():
    assert _env()["REVERSE_POLICY"] == "on"
    readings = [se.SignalReading(se.FAM_STRUCTURE, "wick_rejection", 1, 0.79),
                se.SignalReading(se.FAM_EXHAUSTION, "rsi_divergence", 1, 0.7)]
    trends = [{"tf": "1h", "score": 0.0, "adx": 18.0, "adx_strength": 0.12},
              {"tf": "4h", "score": 0.0, "adx": 16.0, "adx_strength": 0.04}]
    v = se.evaluate(-1, readings, trends, "trend", lambda n, g: (0.3, True),
                    se.CONFIRM_DEFAULTS, regime_adx=30.0, policy=se.REVERSE_POLICY)
    assert v.allow is True and set(v.reversed_signals) == {"wick_rejection", "rsi_divergence"}


def test_live_unaffected_by_shadow():
    # Canlı birim shadow env'ini okumaz; kendi EnvironmentFile'ı ve HB_OFFLINE=0 kapısı vardır.
    live = os.path.join(ROOT, "deploy", "auto_trader_faz9_live.service")
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(live)
    assert cp["Service"]["EnvironmentFile"] == "/etc/auto_trader/live.env"
    assert 'test \\"$HB_OFFLINE\\" = 0' in cp["Service"]["ExecStartPre"]
    assert "shadow" not in cp["Service"]["Environment"].lower()
    # Kod tarafı: canlı modda politika None → ters yön sızmaz.
    src = inspect.getsource(auto_trader._faz6_confirm)
    assert 'mode == "compare" and REVERSE_POLICY_ENABLED' in src
