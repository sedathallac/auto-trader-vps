"""test_faz3_universe_drift.py — M-4 sözleşmesi testleri (sentetik barlar).

Kilitler: pencere-okuma (bar içine yuvarlama, APPROXIMATE), SHORT işaret
konvansiyonu, (a)/(b)/(c)/(d) hüküm makinesinin sınırları, kapsama < %90 →
zorla hüküm YOK.
"""
import faz3_universe_drift as UD

BARS = ([0.0, 900.0, 1800.0, 2700.0],        # 15m'lik 4 bar (saniye)
        [100.0, 110.0, 120.0, 130.0],
        [110.0, 120.0, 130.0, 140.0])


class TestWindowReturn:
    def test_long_window_open_to_close(self):
        r, ok = UD.window_return(BARS, 100.0, 2000.0)   # bar0 open → bar2 close
        assert ok and abs(r - (130.0 / 100.0 - 1)) < 1e-12

    def test_short_sign_flip(self):
        r, _ = UD.window_return(BARS, 100.0, 2000.0)
        assert (-r) < 0                                   # SHORT: işaret ters

    def test_out_of_range_not_silent(self):
        r, ok = UD.window_return(BARS, -1e9, 100.0)
        assert ok is False


class TestVerdict:
    def test_a_universe_dominant(self):
        # drift -20, gross -21 → pure -1 ≤ e=3 → (a)
        assert UD.verdict(-20.0, -21.0, -28.0, 1.0).startswith("(a)")

    def test_b_signal_negative(self):
        assert UD.verdict(-12.0, -21.0, -28.0, 1.0).startswith("(b)")

    def test_c_cost_dominant(self):
        assert UD.verdict(-20.0, -2.0, -9.0, 1.0).startswith("(c)")

    def test_d_low_coverage_never_forced(self):
        # Aynı (b) sayılarıyken kapsama %89 → hüküm seçilmez
        assert UD.verdict(-12.0, -21.0, -28.0, 0.89).startswith("(d)")

    def test_d_ambiguous(self):
        # pure e'nin içinde, drift>0 → hiçbir kategori → (d)
        assert UD.verdict(0.5, 0.4, -0.3, 1.0).startswith("(d)")
