"""test_faz18_atr_veto.py — BLOK 4 İŞ 3.2: ATR≤0 barında GİRİŞ VETOSU.

A-10 (f7c99d4) structure ailesini susturuyordu; ANCAK confirm_min_families=1
olduğundan DİĞER aileler kapıyı tek başına geçebiliyordu → ATR=0 barında
giriş hâlâ mümkündü. İstenen: mutlak veto (fail-closed) + WARN; backtest
D-4 (@dcc23cb, sıfır-ATR barını atlama) ile canlı-hizalı.
KIRMIZI: monkeypatch'lenmiş tek güçlü momentum ailesi ATR=0 barında bugün
allow=True veriyor (aile kapısı ATR'dan bağımsız).
"""
import strategy_engine as SE

T0 = 1_760_000_000_000


def _cd(prices, tf_ms):
    n = len(prices)
    return SE.Candles(o=list(prices), h=[p * 1.001 for p in prices],
                      l=[p * 0.999 for p in prices], c=list(prices),
                      v=[1.0] * n, t=[T0 + i * tf_ms for i in range(n)])


def _flat(n):
    """Gerçek ATR=0 barı: o=h=l=c sabit — Binance'ın işlem-görmemiş barı
    (D-4 @dcc23cb'nin backtest'te atladığı bar). h≠l olursa ATR>0 olur,
    veto tetiklenmez; fixture bunu BOZMAMALI."""
    return SE.Candles(o=[100.0] * n, h=[100.0] * n, l=[100.0] * n,
                      c=[100.0] * n, v=[0.0] * n,
                      t=[T0 + i * 900_000 for i in range(n)])


def _uptrend(n):
    return _cd([100.0 * (1.001 ** i) for i in range(n)], 3_600_000)


def _patch_readings(monkeypatch):
    r = SE.SignalReading(family="momentum", name="fake", direction=1,
                         strength=0.9)
    monkeypatch.setattr(SE, "exhaustion_readings", lambda cd: [r])
    monkeypatch.setattr(SE, "structure_readings", lambda cd: [])
    monkeypatch.setattr(SE, "volume_readings", lambda cd: [])


class TestAtrZeroVeto:
    def test_confirm_vetoes_atr_zero_even_with_one_strong_family(self, monkeypatch):
        _patch_readings(monkeypatch)
        v = SE.confirm(+1, _flat(80), {"1h": _uptrend(240), "4h": _uptrend(240)},
                       store=None)
        assert v.allow is False
        assert "A-10" in v.reason and "ATR" in v.reason

    def test_normal_bar_single_family_still_allowed(self, monkeypatch):
        _patch_readings(monkeypatch)
        up = _cd([100.0 * (1.0005 ** i) for i in range(80)], 900_000)
        v = SE.confirm(+1, up, {"1h": _uptrend(240), "4h": _uptrend(240)},
                       store=None)
        assert v.allow is True          # veto aşırı-duyarlı değildir
