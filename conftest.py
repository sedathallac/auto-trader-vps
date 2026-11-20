"""Repo-kökü test izolasyonu: balina order-book örneklemesi gerçek sleep(5) yapmasın (testler yavaşlamasın).
whale_engine.fetch_whale_inputs sleep/clock'u parametreyle de alır; bu fixture yalnız varsayılan time.sleep'i susturur.

BLOK10 (§6 test-hijyeni): FAZ14w ders — kök scriptler auto_trader'ı HOME durum
dosyalarıyla (pending_fills/blacklist/risk_ledger) import ediyordu; VPS'te test
artıkları canlı servis tarafından "bekleyen dolum" sanılmıştı. auto_trader
import'larından ÖNCE FAZ9_STATE_DIR tanımsızsa geçici dizine sabitlenir
(setdefault: tests/conftest.py ve deploy prosedüründeki HOME=mktemp davranışı
DEĞİŞMEZ; yalnız korunmasız çıplak `pytest .` koşumu korunur)."""
import os
import tempfile

os.environ.setdefault("FAZ9_STATE_DIR",
                      tempfile.mkdtemp(prefix="faz9-rootpytest-state-"))

import pytest
import whale_engine


@pytest.fixture(autouse=True)
def _no_real_book_sampling_sleep(monkeypatch):
    monkeypatch.setattr(whale_engine.time, "sleep", lambda s: None)
