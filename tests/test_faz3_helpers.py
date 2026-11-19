"""test_faz3_helpers.py — faz3_run yardımcıları (WF fold'ları, PBO matrisi)."""
import faz3_run as FR

M15 = 900_000
T0 = 1704067200000  # 2024-01-01T00:00Z


def _month_ms(y, mo, d=15):
    import datetime
    return int(datetime.datetime(
        y, mo, d, tzinfo=datetime.timezone.utc).timestamp() * 1000)


class TestWfFolds:
    def test_expanding_step3(self):
        months = ["2024-%02d" % m for m in range(1, 13)]
        folds = FR.wf_folds(months, step=3)
        assert len(folds) == 2
        assert folds[0] == (months[:6], months[6:9])
        assert folds[1] == (months[:9], months[9:12])
        for tr, te in folds:
            assert te[0] == months[len(tr)]           # test IS'i İZLER (sızıntı yok)

    def test_short_history_no_folds(self):
        assert FR.wf_folds(["2024-01"] * 8, step=3) == []


class TestMonthMatrix:
    def test_mean_per_month_empty_zero(self):
        trs = [{"t_exit": _month_ms(2024, 1) + k * M15, "net_ret": 0.1 * (k + 1)}
               for k in range(2)]                     # 2024-01, ort. 0.15
        months = ["2024-01", "2024-02", "2024-03"]
        mat = FR.month_matrix({0: trs}, months)
        assert mat[0] == [0.15, 0.0, 0.0] or abs(mat[0][0] - 0.15) < 1e-12
        assert mat[0][1:] == [0.0, 0.0]

    def test_rows_follow_dict_order(self):
        a = [{"t_exit": _month_ms(2024, 2), "net_ret": 0.5}]
        b = [{"t_exit": _month_ms(2024, 1), "net_ret": -0.5}]
        mat = FR.month_matrix({0: a, 1: b}, ["2024-01", "2024-02"])
        assert mat == [[0.0, 0.5], [-0.5, 0.0]]


class TestMonthLabel:
    def test_utc_month(self):
        assert FR._month(_month_ms(2025, 12, 31, )) == "2025-12"
        assert FR._month(_month_ms(2026, 1, 1)) == "2026-01"
