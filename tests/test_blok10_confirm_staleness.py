"""BLOK10 §5 (F-A) — _LAST_CONFIRM BAYATLIK BULGUSU (P1, yön çevirme riski).

Kök neden (doğrulanmış): _faz6_confirm içinde _LAST_CONFIRM.clear() YALNIZ
strategy_engine.confirm() BAŞARIYLA döndükten sonraki satırda çalışır
(auto_trader.py:3078). confirm öncesi bir istisna + mode=="compare" yolu
3117-3119'dan return True ile çıkar ve globalde ÖNCEKİ SEMBOLÜN verisi kalır.
open_coin:3480 bunu okur → bayat effective_side canlı emrin YÖNÜNÜ
çevirebilir; _live_family_gate/_live_regime_gate (2944/2965) yanlış
sembolün aile/rejim kararıyla çalışır.

Onarım: _faz6_confirm entry'sinde KOŞULSUZ clear() — teyit yenilenmezse
global boş kalır; tüketiciler zaten boşta fail-closed:
  * effective_side yoksa yön çevrilmez (orijinal side korunur),
  * families boşsa canlı family gate reddeder (2945-2946),
  * compare/dry-run'da giriş zaten emir vermez.

Test strategy_engine'i sys.modules seviyesinde sahte ile değiştirir; başka
hiçbir bileşen taklit değildir (auto_trader gerçek, RISK_CONFIG gerçek).
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader

STALE_A = {
    "families": {"wick_rejection": 1},
    "regime": "trend",
    "p_real": 0.9,
    "allow": True,
    "effective_side": -1,          # A sembolünde SHORT'a çevirmiş
    "reversed": ("rsi_divergence",),
}


class _V6:
    """strategy_engine.confirm dönüşünün asgari gerçek-sözleşme kopyası."""
    families = {"momentum": 1}
    regime = "range"
    p_real = 0.6
    allow = True
    effective_side = 1
    reversed_signals = ()
    trend_against = 0.0
    calibrated = True
    regime_adx = 20.0
    trend_adx = 20.0
    reason = "test"
    readings = ()


def _fake_se(monkeypatch, *, boom=False, v6=None):
    mod = types.ModuleType("strategy_engine")
    mod.SIGNAL_DB = ":memory:"

    def calib_db_path():
        return ":memory:"

    class _Store:
        def __init__(self, *a, **k):
            pass

        def resolve_pending(self, *a, **k):
            pass

        def record_event(self, *a, **k):
            pass

    def fetch_candles(binance, symbol, tf, lim):
        if boom:
            raise RuntimeError("kline fetch patladı")
        return None                     # None -> resolve/record dalları atlanır

    def confirm(*a, **k):
        return v6 or _V6()

    def effective_direction(*a, **k):
        return 0

    mod.fetch_candles = fetch_candles
    mod.SignalStore = _Store
    mod.calib_db_path = calib_db_path
    mod.REVERSE_POLICY = None
    mod.confirm = confirm
    mod.effective_direction = effective_direction
    mod.SignalReading = tuple
    monkeypatch.setitem(sys.modules, "strategy_engine", mod)
    return mod


@pytest.fixture(autouse=True)
def _reset_last_confirm():
    saved = dict(auto_trader._LAST_CONFIRM)
    auto_trader._LAST_CONFIRM.clear()
    yield
    auto_trader._LAST_CONFIRM.clear()
    auto_trader._LAST_CONFIRM.update(saved)


def _seed_stale():
    auto_trader._LAST_CONFIRM.clear()
    auto_trader._LAST_CONFIRM.update(STALE_A)


def test_compare_exception_must_not_leave_stale_confirm(monkeypatch):
    """A sembolünün teyidi globalde dururken B için teyit istisnası + compare →
    fonksiyon True döner (compare reddetmez) ama bayat A verisi KALMAMALI."""
    _seed_stale()
    _fake_se(monkeypatch, boom=True)
    ret = auto_trader._faz6_confirm(None, "BUSDT", 1, 0.02, "compare")
    assert ret is True                       # sözleşme: compare asla reddetmez
    assert auto_trader._LAST_CONFIRM == {},  \
        "bayat _LAST_CONFIRM kaldı — open_coin:3480 yanlış yöne çevirebilir"


def test_on_mode_exception_fail_closed_and_cleared(monkeypatch):
    _seed_stale()
    _fake_se(monkeypatch, boom=True)
    ret = auto_trader._faz6_confirm(None, "BUSDT", 1, 0.02, "on")
    assert ret is False                      # on modu: fail-closed zaten doğru
    assert auto_trader._LAST_CONFIRM == {}   # ...ve bayat veri bırakılmamalı


def test_success_never_reads_previous_symbol(monkeypatch):
    """Başarılı teyit bayat veriyi EZMELİ (regresyon çapası — mevcut davranış)."""
    _seed_stale()

    class _V(_V6):
        effective_side = 1
        regime = "range"

    _fake_se(monkeypatch, v6=_V())
    ret = auto_trader._faz6_confirm(None, "CUSDT", 1, 0.02, "on")
    assert ret is True
    assert auto_trader._LAST_CONFIRM["regime"] == "range"
    assert auto_trader._LAST_CONFIRM["effective_side"] == 1
    assert auto_trader._LAST_CONFIRM["reversed"] == ()


def test_cleared_state_yields_no_side_flip():
    """Tüketici sözleşmesi: boş _LAST_CONFIRM → yön çevirme YOK (orijinal side).
    open_coin:3480-3484 bu okumayı birebir uygular."""
    auto_trader._LAST_CONFIRM.clear()
    eff = auto_trader._LAST_CONFIRM.get("effective_side")
    assert eff not in (1, -1)                # çevirme koşulu sağlanmaz
    fam_ok_live = list((auto_trader._LAST_CONFIRM.get("families") or {}).keys())
    assert fam_ok_live == []                 # canlı family gate bunu RED sayar
