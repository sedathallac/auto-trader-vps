"""PRODUCTION-BOUND regression tests for auto_trader.reconcile_positions.

STATUS: WRITTEN on VPS, NOT RUN here (bash_guard blocks pytest on the VPS).
This is NOT model-only: it imports the real /root/auto_trader.py and calls the
real reconcile_positions() with fake exchange/HB clients (no network, no live code).

RUN ON ZORIN (repo cloned, tooling venv has `requests`):
    cd <repo_root>
    ~/.local/share/bot-tooling/venv/bin/pytest -q BOT_KNOWLEDGE_BASE/TESTING/tests/test_reconcile_positions_real.py

Invariants exercised: F2/F9/F13 — drift (exchange position with no matching HB
executor, the SOLUSDT class) must make reconcile_positions() return False so the
caller halts new entries. Grounding: findings/2026-11-11_sol_double_close.md.
"""
import sys
from pathlib import Path

# repo root = .../BOT_KNOWLEDGE_BASE/TESTING/tests/<this> -> parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import pytest

auto_trader = pytest.importorskip(
    "auto_trader",
    reason="run from the repo that contains auto_trader.py + risk_engine.py, with `requests` installed",
)


class FakeBinance:
    def __init__(self, positions):
        self._positions = positions

    def position_risk(self):
        return self._positions


class FakeHB:
    auth = ("user", "pass")  # only used on the orphaned-on-HB (network) branch, not here

    def __init__(self, executors):
        self._executors = executors

    def active_executors(self):
        return self._executors


def _pos(symbol, amt):
    return {"symbol": symbol, "positionAmt": str(amt), "entryPrice": "100", "markPrice": "100", "leverage": "3"}


def _exec(pair, amt_quote=25.0, status="RUNNING"):
    return {"trading_pair": pair, "status": status, "filled_amount_quote": amt_quote,
            "executor_id": "E_" + pair}


def test_reconcile_in_sync_returns_true():
    binance = FakeBinance([_pos("SOLUSDT", "0.3")])
    hb = FakeHB([_exec("SOL-USDT")])
    assert auto_trader.reconcile_positions(binance, hb) is True


def test_reconcile_sol_class_drift_returns_false():
    # Exchange holds a position, HB has no executor for it -> SOL double-close residue.
    binance = FakeBinance([_pos("SOLUSDT", "-0.3")])
    hb = FakeHB([])  # no executors
    assert auto_trader.reconcile_positions(binance, hb) is False


def test_reconcile_flat_returns_true():
    binance = FakeBinance([_pos("SOLUSDT", "0")])  # zero amt -> ignored
    hb = FakeHB([])
    assert auto_trader.reconcile_positions(binance, hb) is True
