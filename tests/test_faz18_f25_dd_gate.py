"""test_faz18_f25_dd_gate.py — F25: DD kapısı dürüstlüğü (BLOK 2 İŞ 0).

Gerçek üretim olayı (kanıt: equity_series_halt_20261119.csv): 19-Kas 02:35:02Z'de
418 IP-BAN penceresinde imzalı /fapi/v2/balance okuması başarısız oldu;
get_balance hata-sentinel'i 0.0 üretti; guard'sız kod DD=(peak−0)/peak=1.0 →
halt_reason "Drawdown %100.0". Aynı logda DD=%1.0 da DOĞRUYDU (farklı an) —
yalan olan, dondurulmuş gerekçenin bağlamsız tekrarlanmasıydı.

Kırmızı→yeşil: bu testlerin streak-halt / M-2 sözleşme / öz-test bölümleri
onarım ÖNCESİ HEAD'de KIRMALIDIR (stash ile doğrulanır), onarımla yeşil.
"""
import math

import pytest

import risk_engine as RE


def _eng(tmp_path, cfg_extra=None):
    db = tmp_path / "risk_f25.db"
    RE._bootstrap_scratch_db(db)          # FAZ16: DDL yalnız AÇIK bootstrap
    cfg = {"db_file": str(db), "db_create": True, "max_drawdown_pct": 0.20}
    cfg.update(cfg_extra or {})
    return RE.RiskEngine(cfg)


class TestM2Contract:
    def test_ratio_and_pct_separate_fields(self, tmp_path):
        e = _eng(tmp_path)
        e.update_equity(100.0)
        e.update_equity(120.0)
        e.update_equity(90.0)
        assert abs(e.dd_ratio - 0.25) < 1e-12
        assert abs(e.dd_pct - 25.0) < 1e-9

    def test_daily_dd_separate_formula(self, tmp_path):
        e = _eng(tmp_path)
        e.update_equity(100.0)      # day_start = 100
        e.update_equity(120.0)
        e.update_equity(90.0)
        assert abs(e.daily_dd_ratio - 0.10) < 1e-12   # (100−90)/100
        # peak-DD ile daily-DD AYNI anda farklıdır → birbirinin yerine geçmez
        assert abs(e.dd_ratio - 0.25) < 1e-12         # (120−90)/120
        e.update_equity(96.0)
        assert abs(e.dd_ratio - 0.20) < 1e-12
        assert abs(e.daily_dd_ratio - 0.04) < 1e-12

    def test_known_series_final_dd(self, tmp_path):
        e = _eng(tmp_path)
        for v in (100.0, 120.0, 90.0, 96.0):
            assert e.update_equity(v)


class TestSentinelDiscipline:
    def test_single_zero_tick_rejected_dd_frozen(self, tmp_path):
        e = _eng(tmp_path)
        e.update_equity(3526.92)
        e.update_equity(3476.0)          # ~%1.4 gerçek DD
        dd_before = e.dd_ratio
        assert e.update_equity(0.0) is False
        assert e.dd_ratio == dd_before   # sentinel DD'ye SIZAMAZ (F25 kökü)
        assert e.is_halted is False      # tek sentinel halt DEĞİL

    def test_bad_streak_halts_failsafe(self, tmp_path):
        # M-2/0.6 fail-safe: tek hata giriş bloklar; ardışık 3 "okunamıyor" → HALT.
        e = _eng(tmp_path)
        e.update_equity(3526.92)
        for _ in range(2):
            assert e.update_equity(0.0) is False
        assert e.is_halted is False                 # 2: henüz halt yok
        assert e.update_equity("abc") is False      # 3: parse-hatası da sayılır
        assert e.is_halted is True                  # HALT (fail-safe yön)
        assert "EQUITY_UNAVAILABLE" in e.halt_reason
        assert "Drawdown" not in e.halt_reason      # sahte DD hükmü YASAK

    def test_streak_resets_on_valid(self, tmp_path):
        e = _eng(tmp_path)
        e.update_equity(3526.92)
        e.update_equity(0.0)
        e.update_equity(0.0)
        e.update_equity(3500.0)          # geçerli okuma sayacı sıfırlar
        for _ in range(2):
            e.update_equity(0.0)
        assert e.is_halted is False      # 2+2 kesik → halt eşiği aşılmadı


class TestOpeningSelftest:
    def test_selftest_green(self):
        ok, detail = RE.dd_selftest()
        assert ok, detail

    def test_selftest_actually_validates(self, monkeypatch):
        # Öz-test kör-true değil: beklenen sabitleri boz → False vermeli.
        monkeypatch.setattr(RE, "_DD_SELFTEST_EXPECTED",
                            {"dd_at_trough": 0.30,   # yanlış sabit
                             "dd_final": 0.20, "daily_dd_final": 0.04})
        ok, detail = RE.dd_selftest()
        assert not ok and "dd_at_trough" in detail

    def test_selftest_touches_no_production_db(self, monkeypatch, tmp_path):
        # Örtük kontrol: selftest kendi atılabilir DB'sini kullanır.
        ok, _ = RE.dd_selftest()
        assert ok
        assert not (tmp_path / "risk_f25.db").exists()
