"""test_faz18_atr_guard.py — A-10: ATR=0 (düz piyasa) barında sessiz sinyal kaybı.

Üretim kanıtı: PUMPUSDT arşiv replay'de structure_readings ZeroDivisionError
(`d / a`, a=0.0 — düz segmentte tol=0 pivot kümelemesi seviye üretir, px o
seviyeye eşit → 0.0/0.0). Canlıda _faz6_confirm geniş except'i barı yutuyor;
log'da sınıf adı yok → sinyal kaybı izlenemiyor.

Kırmızı→yeşil discipline: bu testler ÖNCE mevcut kodda KIRMALI (hata üretilir
ve gösterilir), guard'dan SONRA yeşil.
"""
import logging

import pytest

import auto_trader as at
import strategy_engine as se

N = 300


def _flat_candles(n=N, px=100.0):
    return se.Candles([px] * n, [px] * n, [px] * n, [px] * n,
                      [1.0] * n, list(range(n)), [1.0] * n)


class TestStructureReadingsFlatATR:
    def test_flat_bar_does_not_raise_and_logs_warn(self, caplog):
        cd = _flat_candles()
        with caplog.at_level(logging.WARNING, logger="strategy_engine"):
            out = se.structure_readings(cd)          # patlamamalı
        assert isinstance(out, list)
        assert any("atr" in r.getMessage().lower() or "ATR" in r.getMessage()
                   for r in caplog.records), "ATR=0 için WARN loglanmadı"

    def test_mode_on_fail_closed_preserved(self, monkeypatch, caplog):
        # guard sonrası bile: confirm içinde hata çıkarsa mode='on' RED döner
        def boom(*a, **k):
            raise ZeroDivisionError("float division by zero")
        monkeypatch.setattr(se, "confirm", boom)
        with caplog.at_level(logging.WARNING):
            ok = at._faz6_confirm(object(), "PUMPUSDT", 1, 0.02, "on")
        assert ok is False                            # fail-closed korunur

    def test_loss_marker_in_log(self, monkeypatch, caplog):
        # A-10(b): yutulan istisna SINIF ADI + sinyal-kaybı işaretiyle loglanır
        def boom(*a, **k):
            raise ZeroDivisionError("float division by zero")
        monkeypatch.setattr(se, "confirm", boom)
        with caplog.at_level(logging.WARNING):
            at._faz6_confirm(object(), "PUMPUSDT", 1, 0.02, "on")
        msgs = " | ".join(r.getMessage() for r in caplog.records)
        assert "ZeroDivisionError" in msgs, f"sınıf adı logda yok: {msgs!r}"
        assert "SİNYAL-BARI-KAYBI" in msgs

    def test_normal_bar_geometry_untouched(self):
        # guard, normal (ATR>0) barda davranışı DEĞİŞTİRMEMELİ: deterministik
        # dalgalı seri — çıktı tipi ve aile adı korunuyor
        o = [];  h = [];  l = [];  c = []
        p = 100.0
        for k in range(N):
            p *= 1.001 if k % 7 < 4 else 0.998
            o.append(p); c.append(p * 1.0005); h.append(max(p, p * 1.0005) * 1.001)
            l.append(min(p, p * 1.0005) * 0.999)
        cd = se.Candles(o, h, l, c, [1.0] * N, list(range(N)), [1.0] * N)
        out = se.structure_readings(cd)
        assert isinstance(out, list)
        atr = se.atr_series(h, l, c, 14)
        assert atr and atr[-1] > 0                    # önkoşul: gerçek ATR
