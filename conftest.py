"""Repo-kökü test izolasyonu: balina order-book örneklemesi gerçek sleep(5) yapmasın (testler yavaşlamasın).
whale_engine.fetch_whale_inputs sleep/clock'u parametreyle de alır; bu fixture yalnız varsayılan time.sleep'i susturur."""
import pytest
import whale_engine


@pytest.fixture(autouse=True)
def _no_real_book_sampling_sleep(monkeypatch):
    monkeypatch.setattr(whale_engine.time, "sleep", lambda s: None)
