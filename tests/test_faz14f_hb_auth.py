"""FAZ14f — Hummingbot kimlik hatası, karaliste kirlenmesi ve slot logu.

Bulunan hatalar:
  1. HB_USERNAME/HB_PASSWORD gömülü varsayılan (FURKAN/1973) → yanlış kimlikle 401
  2. 401 alınca SEMBOL karalisteye yazılıyordu (kimlik düzelince bile sembol bloke)
  3. slot rezervasyon logu paydada MAX_OPEN_POSITIONS globalini (canlıda 0) yazıyordu
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


def test_no_hardcoded_hb_credentials():
    src = inspect.getsource(auto_trader.run_once)
    assert '"FURKAN"' not in src, "gömülü HB kullanıcı adı kalmamalı"
    assert '"1973"' not in src, "gömülü HB parolası kalmamalı"
    assert 'os.environ.get("HB_USERNAME")' in src
    assert 'os.environ.get("HB_PASSWORD")' in src


def test_live_missing_hb_credentials_is_fail_closed(monkeypatch):
    src = inspect.getsource(auto_trader.run_once)
    assert "_HB_AUTH_BROKEN = True" in src
    assert "HB_USERNAME/HB_PASSWORD yok" in src


def test_auth_error_detected_from_detail():
    assert auto_trader._is_hb_auth_error({"detail": "Incorrect username or password"}) is True
    assert auto_trader._is_hb_auth_error({"detail": "Unauthorized"}) is True
    assert auto_trader._is_hb_auth_error({"status_code": 401}) is True
    assert auto_trader._is_hb_auth_error({"status_code": 403}) is True


def test_non_auth_errors_are_not_auth_errors():
    assert auto_trader._is_hb_auth_error({"detail": "Insufficient balance"}) is False
    assert auto_trader._is_hb_auth_error({"status_code": 400}) is False
    assert auto_trader._is_hb_auth_error({"error": "pre-create snapshot failed"}) is False
    assert auto_trader._is_hb_auth_error(None) is False


def test_auth_error_does_not_blacklist_symbol():
    src = inspect.getsource(auto_trader.open_coin)
    auth_idx = src.index("_is_hb_auth_error(er)")
    blacklist_idx = src.index("add_blacklist(symbol, f\"executor: {er}\")")
    assert auth_idx < blacklist_idx, "kimlik hatası karalisteden ÖNCE ele alınmalı"
    assert "return False" in src[auth_idx:blacklist_idx]


def test_auth_broken_flag_blocks_further_orders(monkeypatch):
    monkeypatch.setattr(auto_trader, "_HB_AUTH_BROKEN", True)
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", False)
    monkeypatch.setattr(auto_trader, "_effective_max_positions", lambda: 5)
    called = {"n": 0}

    class _HB:
        def active_executors(self):
            called["n"] += 1
            return []

    monkeypatch.setattr(auto_trader, "HBAPI", lambda *a, **k: _HB())
    monkeypatch.setenv("BINANCE_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    monkeypatch.setenv("HB_USERNAME", "u")
    monkeypatch.setenv("HB_PASSWORD", "p")
    auto_trader.run_once()
    assert called["n"] == 0, "kimlik bozukken executor taraması bile yapılmamalı"


def test_slot_log_uses_effective_limit():
    src = inspect.getsource(auto_trader.open_coin)
    assert "kullanım={_rr.used}/{_effective_max_positions()}" in src
    assert "kullanım={_rr.used}/{MAX_OPEN_POSITIONS}" not in src


def test_auth_broken_flag_defaults_false():
    assert auto_trader._HB_AUTH_BROKEN is False
