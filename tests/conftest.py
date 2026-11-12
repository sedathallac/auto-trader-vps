"""Test isolation for the K-3 cross-process SL-placement ledger.

ensure_sl_coverage uses a shared SQLite store (auto_trader.SL_LEDGER_DB, default
~/risk_ledger.db) for the SL-placement intent. Without isolation that real file would
leak intent state between tests (a PLACED intent -> BUSY -> placement skipped). This
autouse fixture points SL_LEDGER_DB at a fresh per-test temp db.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


@pytest.fixture(autouse=True)
def _isolate_sl_ledger(tmp_path):
    _orig = auto_trader.SL_LEDGER_DB
    auto_trader.SL_LEDGER_DB = str(tmp_path / "risk_ledger.db")
    try:
        yield
    finally:
        auto_trader.SL_LEDGER_DB = _orig
