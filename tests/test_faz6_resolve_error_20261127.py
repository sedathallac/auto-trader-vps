"""GÖREV 2026-11-27 (devam) — _faz6_confirm: resolve_pending hatasında
kalibrasyon eski/eksik veriyle başarılı sonuç ÜRETİLMEZ.

Kusur: SignalStore.resolve_pending hata verdiğinde (artık ölçülebilir:
last_resolve_error) _faz6_confirm bunu görmezden gelip confirm()/p_fake
kalibrasyonunu eski veriyle 'başarılı' sayabiliyordu.

Düzeltme (auto_trader._faz6_confirm):
  * mode 'on'    + resolve hatası → fail-closed RED (False); confirm()
                  ÇAĞRILMAZ (kalibrasyon durdurulur — giriş yok);
  * mode 'compare' + resolve hatası → compare sözleşmesi korunur (ASLA
                  reddetmez) ama p_fake açıkça UNKNOWN işaretlenir (log);
  * hatasız akış ve veri-yok (main6=None) davranışı DEĞİŞMEZ.

AĞ ÇAĞRISI YOK: fetch_candles / SignalStore / confirm yamalı; canlı
signal_outcomes.db'ye DOKUNULMAZ (SignalStore gerçek sınıfı örneklenmez).
"""
import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_trader
import strategy_engine as se
from strategy_engine import Candles


def _cd(n=30):
    return Candles(o=[100.0] * n, h=[101.0] * n, l=[99.0] * n,
                   c=[100.5] * n, v=[10.0] * n,
                   t=list(range(1_700_000_000_000,
                                1_700_000_000_000 + n * 60_000, 60_000)))


class _Verdict:
    """confirm() sahte kararı — GEÇER."""

    regime = "range"
    trend_against = 0.0
    families = 2
    p_real = 0.62
    calibrated = True
    allow = True
    reason = "test"
    readings = []


class _FakeStore:
    """SignalStore yamasi — resolve_pending hata durumunu kontrollü üretir;
    gerçek DB'ye yazmaz."""

    def __init__(self, error=None):
        self.last_resolve_error = error
        self.resolved_calls = 0
        self.recorded = 0

    def resolve_pending(self, *a, **k):
        self.resolved_calls += 1
        return 0

    def record_event(self, *a, **k):
        self.recorded += 1
        return True


class Faz6ResolveErrorTest(unittest.TestCase):

    def _run(self, mode, store, main_cd, capture_logs=False):
        log_ctx = (self.assertLogs("auto_trader", level="WARNING")
                   if capture_logs
                   else contextlib.nullcontext())
        with mock.patch.object(se, "fetch_candles",
                               return_value=main_cd), \
             mock.patch.object(se, "SignalStore", return_value=store), \
             mock.patch.object(se, "confirm",
                               return_value=_Verdict()) as m_confirm, \
             log_ctx as lg:
            ok = auto_trader._faz6_confirm(object(), "BTCUSDT", 1, 0.02,
                                           mode)
        logs = lg.output if capture_logs else []
        return ok, m_confirm, logs

    def test_on_mode_resolve_error_fail_closed(self):
        """mode 'on' + resolve hatası → RED (False) VE confirm() çağrılmaz:
        p_fake kalibrasyonu eski/eksik veriyle ÜRETİLMEZ."""
        store = _FakeStore(error="RuntimeError: boom")
        ok, m_confirm, logs = self._run("on", store, _cd(),
                                        capture_logs=True)
        self.assertFalse(ok)
        m_confirm.assert_not_called()          # kalibrasyon DURDURULDU
        joined = " ".join(logs)
        self.assertIn("ÜRETİLMEZ", joined)
        self.assertIn("fail-closed", joined)
        self.assertIn("RuntimeError: boom", joined)

    def test_on_mode_no_error_success_unchanged(self):
        """Hatasız akış DEĞİŞMEZ: confirm() çağrılır, GEÇER kararı döner."""
        store = _FakeStore(error=None)
        ok, m_confirm, _ = self._run("on", store, _cd())
        self.assertTrue(ok)
        m_confirm.assert_called_once()
        self.assertEqual(store.resolved_calls, 1)

    def test_compare_mode_resolve_error_marks_unknown(self):
        """compare sözleşmesi korunur (reddetmez) ama p_fake açıkça
        UNKNOWN işaretlenir — sessiz 'başarılı kalibrasyon' YOK."""
        store = _FakeStore(error="sqlite3.OperationalError: locked")
        ok, m_confirm, logs = self._run("compare", store, _cd(),
                                        capture_logs=True)
        self.assertTrue(ok)                    # compare ASLA reddetmez
        m_confirm.assert_called_once()
        joined = " ".join(logs)
        self.assertIn("UNKNOWN", joined)
        self.assertIn("karar etkilenmedi", joined)

    def test_no_candles_resolve_not_called(self):
        """Veri yok (main6=None): resolve_pending çağrılmaz, hata durumu
        yok, normal akış devam eder."""
        store = _FakeStore(error=None)
        ok, m_confirm, _ = self._run("on", store, None)
        self.assertTrue(ok)
        self.assertEqual(store.resolved_calls, 0)
        m_confirm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
