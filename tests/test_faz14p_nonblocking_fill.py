"""FAZ14p — dolum beklemesi tick'ler arası (bloklamayan) + döngü aralığı.

Kök neden: `open_coin` içinde `while True: ... time.sleep(...)` ile FILL_WAIT_S (300s)
ana thread'i kilitliyordu. Ölçüm (canlı): 23:53:29 emir → 23:58:29'a kadar tek satır log
yok; strace `clock_nanosleep(..., 300s)`. Bu sürede tarama, SL denetimi ve reconciliation
duruyordu. Artık bekleme tick'ler arasında ilerler ve diskte kalıcıdır.
"""
import inspect
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_trader


class _HB:
    def __init__(self, ex=None):
        self.ex = ex
        self.stopped = []

    def get_executor(self, eid):
        return self.ex

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))


class _Binance:
    def __init__(self, amt=0.0):
        self.amt = amt

    def position_risk(self):
        return [{"symbol": "NEARUSDT", "positionAmt": str(self.amt), "entryPrice": "3.19"}]

    def filters(self, symbol):
        return {"PRICE_FILTER": {"tickSize": "0.001"}}


class _Reserver:
    def __init__(self):
        self.released = []

    def release(self, rid, reason=""):
        self.released.append((rid, reason))


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_trader, "PENDING_FILLS_JSON", tmp_path / ".pending_fills.json")
    monkeypatch.setattr(auto_trader, "_PENDING_FILLS", {})
    yield


def _ctx(deadline_offset=300.0):
    return {"symbol": "NEARUSDT", "side": 2, "amount": 62.0, "sl_pct": 0.0108,
            "tp_pct": 0.0215, "price": 3.196, "dyn_lev": 11, "target_notional": 200.0,
            "res_id": 7, "deadline": time.time() + deadline_offset,
            "opened_at": time.time()}


def test_open_coin_does_not_block_on_fill_wait():
    """open_coin gövdesinde dolum beklemesi için time.sleep döngüsü OLMAMALI."""
    src = inspect.getsource(auto_trader.open_coin)
    assert "_register_pending_fill" in src
    assert "while True:" not in src, "bloklayan bekleme döngüsü kalmamalı"
    assert "time.sleep(min(max(FILL_POLL_S" not in src


def test_pending_fill_registered_and_persisted():
    auto_trader._register_pending_fill("E1", _ctx())
    assert "E1" in auto_trader._PENDING_FILLS
    on_disk = json.loads(auto_trader.PENDING_FILLS_JSON.read_text())
    assert on_disk["E1"]["symbol"] == "NEARUSDT"


def test_pending_fills_reload_after_restart():
    auto_trader._register_pending_fill("E1", _ctx())
    auto_trader._PENDING_FILLS = {}
    loaded = auto_trader._load_pending_fills()
    assert "E1" in loaded and loaded["E1"]["amount"] == 62.0


def test_process_waits_before_deadline(monkeypatch):
    called = []
    monkeypatch.setattr(auto_trader, "_finalize_open", lambda *a, **k: called.append(1))
    auto_trader._register_pending_fill("E1", _ctx(deadline_offset=300))
    out = auto_trader._process_pending_fills(_Binance(), _HB({"filled_amount_quote": 0, "error_count": 0}),
                                             None, None, _Reserver())
    assert out["waiting"] == 1 and not called
    assert "E1" in auto_trader._PENDING_FILLS


def test_process_finalizes_when_filled(monkeypatch):
    called = []

    def _fin(*a, **k):
        called.append(a[2])
        return True
    monkeypatch.setattr(auto_trader, "_finalize_open", _fin)
    auto_trader._register_pending_fill("E1", _ctx())
    out = auto_trader._process_pending_fills(_Binance(), _HB({"filled_amount_quote": 200.0, "error_count": 0}),
                                             None, None, _Reserver())
    assert out["filled"] == 1 and called == ["NEARUSDT"]
    assert "E1" not in auto_trader._PENDING_FILLS


def test_archive_db_path_is_home_relative_not_hardcoded_root():
    # FAZ14q: canlıda her açılışta "archive_symbols register failed: unable to open
    # database file" (ProtectHome=yes, HOME=/var/lib/auto_trader_faz9_live).
    src = Path(auto_trader.__file__).read_text()
    assert 'sqlite3.connect("/root' not in src
    assert auto_trader.TRADING_ARCHIVE_DB.name == ".trading_archive.db"
    assert str(auto_trader.TRADING_ARCHIVE_DB).startswith(str(Path.home()))


def test_process_counts_failed_when_finalize_returns_false(monkeypatch):
    # FAZ14q: _finalize_open False (ör. SL doğrulanamadı -> acil kapanış) "filled"
    # sayılamaz; aksi hâlde tick raporu kapanmış pozisyonu açık gibi gösteriyordu.
    monkeypatch.setattr(auto_trader, "_finalize_open", lambda *a, **k: False)
    auto_trader._register_pending_fill("E1", _ctx())
    out = auto_trader._process_pending_fills(_Binance(), _HB({"filled_amount_quote": 200.0, "error_count": 0}),
                                             None, None, _Reserver())
    assert out["failed"] == 1 and out["filled"] == 0


def test_process_adopts_when_position_exists(monkeypatch):
    called = []
    monkeypatch.setattr(auto_trader, "_finalize_open", lambda *a, **k: called.append(1) or True)
    hb = _HB({"filled_amount_quote": 0, "error_count": 0})
    auto_trader._register_pending_fill("E1", _ctx(deadline_offset=-1))
    out = auto_trader._process_pending_fills(_Binance(amt=-62.0), hb, None, None, _Reserver())
    assert out["adopted"] == 1 and called
    assert hb.stopped == [], "pozisyon varken executor durdurulmamalı"


def test_process_cancels_when_flat(monkeypatch):
    monkeypatch.setattr(auto_trader, "_finalize_open", lambda *a, **k: pytest.fail("flat iken finalize olmamalı"))
    hb = _HB({"filled_amount_quote": 0, "error_count": 0})
    rs = _Reserver()
    auto_trader._register_pending_fill("E1", _ctx(deadline_offset=-1))
    out = auto_trader._process_pending_fills(_Binance(amt=0.0), hb, None, None, rs)
    assert out["cancelled"] == 1
    assert hb.stopped == [("E1", True)]
    assert rs.released == [(7, "not_filled")]


def test_process_fails_closed_when_position_unreadable(monkeypatch):
    class _Boom(_Binance):
        def position_risk(self):
            raise RuntimeError("api down")

    monkeypatch.setattr(auto_trader, "_finalize_open", lambda *a, **k: pytest.fail("okunamazken finalize olmamalı"))
    hb = _HB({"filled_amount_quote": 0, "error_count": 0})
    auto_trader._register_pending_fill("E1", _ctx(deadline_offset=-1))
    out = auto_trader._process_pending_fills(_Boom(), hb, None, None, _Reserver())
    assert out["waiting"] == 1 and hb.stopped == []
    assert "E1" in auto_trader._PENDING_FILLS


def test_loop_interval_is_not_doubled():
    """--interval 30 toplamda 30s uyumalı; eski kod 60s uyuyordu."""
    src = inspect.getsource(auto_trader.main)
    assert "chunks = max(1, a.interval // 60)" not in src
    assert "chunk = min(60.0, remaining)" in src
