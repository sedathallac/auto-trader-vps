"""FAZ14l — drift döngüsü regresyon testleri.

Kök neden: `create_executor` sonrası yalnız 8 saniye bekleniyor, `filled_amount_quote < 1`
ise executor `keep_position=True` ile durduruluyordu. Limit emir 8 saniyede dolmayınca
pozisyon borsada açık kalıyor, HB onu unutuyor ve her tick'te
"DRIFT: Borsada X var, HB'de executor yok" üretiliyordu (52 döngü), yeni girişler süresiz
bloklanıyordu. Düzeltme: 300s bekleme + karar borsa gerçeğine göre (pozisyon varsa
executor benimsenir, yalnız flat ise durdurulur).
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_trader


class _FakeBinance:
    def __init__(self, amt=0.0, raise_on_pos=False):
        self.amt = amt
        self.raise_on_pos = raise_on_pos

    def position_risk(self):
        if self.raise_on_pos:
            raise RuntimeError("api down")
        return [{"symbol": "UNIUSDT", "positionAmt": str(self.amt)}]


class _FakeHB:
    def __init__(self, fills):
        self.fills = list(fills)
        self.stopped = []

    def get_executor(self, eid):
        if len(self.fills) > 1:
            return {"filled_amount_quote": self.fills.pop(0), "error_count": 0}
        return {"filled_amount_quote": self.fills[0], "error_count": 0}

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"ok": True}


def test_fill_wait_defaults_to_300s():
    assert auto_trader.FILL_WAIT_S == pytest.approx(300.0)
    assert auto_trader.FILL_POLL_S == pytest.approx(5.0)


def test_fill_wait_env_overridable(monkeypatch):
    import importlib
    monkeypatch.setenv("LIVE_FILL_WAIT_S", "45")
    mod = importlib.reload(auto_trader)
    try:
        assert mod.FILL_WAIT_S == pytest.approx(45.0)
    finally:
        monkeypatch.delenv("LIVE_FILL_WAIT_S", raising=False)
        importlib.reload(mod)


def test_no_hardcoded_eight_second_sleep():
    src = inspect.getsource(auto_trader)
    assert "time.sleep(8)" not in src, "sabit 8 saniye bekleme kaldırılmalı"


def test_position_amt_helper_reads_signed_amount():
    assert auto_trader._exchange_position_amt(_FakeBinance(-38.0), "UNIUSDT") == pytest.approx(-38.0)
    assert auto_trader._exchange_position_amt(_FakeBinance(0.0), "UNIUSDT") == pytest.approx(0.0)
    assert auto_trader._exchange_position_amt(_FakeBinance(raise_on_pos=True), "UNIUSDT") is None


def test_adopts_executor_when_position_exists():
    """Pozisyon varsa executor durdurulmamalı: durdurmak drift üretir."""
    src = inspect.getsource(auto_trader.open_coin)
    assert "executor BENİMSENDİ" in src
    assert "_pos_amt != 0" in src
    # durdurma yalnız flat dalında olmalı
    flat_branch = src.split("_pos_amt != 0")[1]
    assert "stop_executor" in flat_branch


def test_unknown_position_fails_closed():
    src = inspect.getsource(auto_trader.open_coin)
    assert "_pos_amt is None" in src
    assert "rezervasyon KORUNDU" in src


def test_binding_log_has_no_double_prefix():
    src = inspect.getsource(auto_trader)
    assert "binding={_levd.reason}" not in src, "binding=binding= çift önek kaldırılmalı"
    assert "_levd.caps[0] if _levd.caps else 'none'" in src
