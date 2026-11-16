"""Test isolation for the K-3 cross-process SL-placement ledger.

ensure_sl_coverage uses a shared SQLite store (auto_trader.SL_LEDGER_DB, default
~/risk_ledger.db) for the SL-placement intent. Without isolation that real file would
leak intent state between tests (a PLACED intent -> BUSY -> placement skipped). This
autouse fixture points SL_LEDGER_DB at a fresh per-test temp db.

FAZ14w: aynı sorun tüm durum dosyalarında vardı. VPS'te pytest çalıştırmak
/root/.pending_fills.json içine test artığı (E1/SOLUSDT) yazıyor, canlı servis de o
kaydı "bekleyen dolum" sanıp slot işgal ediyordu. Artık FAZ9_STATE_DIR import'tan
ÖNCE geçici bir dizine ayarlanır; hiçbir test gerçek HOME durum dosyasına dokunamaz.
"""
import os
import sys
import tempfile

import pytest

# auto_trader import edilmeden ÖNCE: STATE_DIR modül yüklemesinde hesaplanır.
os.environ["FAZ9_STATE_DIR"] = tempfile.mkdtemp(prefix="faz9-test-state-")

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


@pytest.fixture(autouse=True)
def _isolate_pending_store(tmp_path):
    """FAZ15b v5: her test taze ve TUTARLI bir pending store ile başlar.

    Transaction artık mutator'dan önce diski okuyup bellekle uzlaştırıyor. Bir test
    belleği (`_PENDING_FILLS.clear()`) diskteki kayıtla uyumsuz bırakırsa üretim
    kuralı gereği MALFORMED_STATE oluşur — bu doğru davranış, ama testler arası
    sızıntı olur. Bu fixture dosyayı, revision'ı ve sağlık/gate durumunu sıfırlar.
    """
    saved = {
        "path": auto_trader.PENDING_FILLS_JSON,
        "flock": auto_trader.PENDING_FLOCK_PATH,
        "loaded": list(auto_trader._PENDING_LOADED),
        "fills": dict(auto_trader._PENDING_FILLS),
        "rev": auto_trader._PENDING_REVISION[0],
        "healthy": auto_trader._PENDING_STORE_HEALTHY[0],
        "err": auto_trader._PENDING_STORE_ERROR[0],
        "blocked": auto_trader._GLOBAL_ENTRY_BLOCKED[0],
        "reason": auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0],
        "wip": auto_trader._PENDING_WRITE_IN_PROGRESS[0],
    }
    path = tmp_path / "pending_fills.json"
    auto_trader.PENDING_FILLS_JSON = path
    auto_trader.PENDING_FLOCK_PATH = path.with_suffix(".lock")
    auto_trader._PENDING_FILLS = {}
    auto_trader._PENDING_REVISION[0] = 0
    auto_trader._PENDING_LOADED[0] = True
    auto_trader._PENDING_STORE_HEALTHY[0] = True
    auto_trader._PENDING_STORE_ERROR[0] = None
    auto_trader._GLOBAL_ENTRY_BLOCKED[0] = False
    auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = None
    auto_trader._PENDING_WRITE_IN_PROGRESS[0] = False
    auto_trader._PENDING_TX_LOCAL.active = False
    try:
        yield
    finally:
        auto_trader.PENDING_FILLS_JSON = saved["path"]
        auto_trader.PENDING_FLOCK_PATH = saved["flock"]
        auto_trader._PENDING_LOADED[0] = saved["loaded"][0]
        auto_trader._PENDING_FILLS = saved["fills"]
        auto_trader._PENDING_REVISION[0] = saved["rev"]
        auto_trader._PENDING_STORE_HEALTHY[0] = saved["healthy"]
        auto_trader._PENDING_STORE_ERROR[0] = saved["err"]
        auto_trader._GLOBAL_ENTRY_BLOCKED[0] = saved["blocked"]
        auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = saved["reason"]
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = saved["wip"]
        auto_trader._PENDING_TX_LOCAL.active = False
