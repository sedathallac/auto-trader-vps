"""Faz 6 RED/GREEN — strateji teyit katmanı (strategy_engine) ve STRATEGY_CONFIRMATION bayrağı.

RED:   Eski kod (bayrak 'off') yalnız priceChangePercent'e bakar: 1h/4h güçlü aşağı trendde bile
       LONG (dip alımı) devam eder; teyit/p_real kontrolü yoktur.
GREEN: Yeni kod trend VETO'su + ≥2 farklı aileden teyit + p_real ≥ 0.65 kontrolü yapar;
       'on' yalnız reddeder; veri yoksa fail-closed; 'off' ek API çağrısı yapmaz.
"""
import sqlite3

import pytest

import auto_trader
import strategy_engine as se
from strategy_engine import Candles, SignalReading, SignalStore


# ------------------------------------------------------------------ veri üreticileri
def _rows(closes, vol=100.0, spread=0.2):
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append([i * 900_000, str(o), str(max(o, c) + spread), str(min(o, c) - spread),
                     str(c), str(vol), i * 900_000 + 899_999])
        prev = c
    return rows


def _trend_closes(n=250, start=200.0, step=-0.4):
    return [start + step * i for i in range(n)]


def _choppy_closes(n=250, base=100.0):
    import math
    return [base + 2.0 * math.sin(i * 0.9) + 0.7 * math.sin(i * 2.3) for i in range(n)]


def _cd(closes, **kw):
    return se.parse_klines(_rows(closes + [closes[-1]], **kw))   # +1: oluşmakta olan mum atılır


# ------------------------------------------------------------------ göstergeler
def test_ema_hand_computed():
    out = se.ema([1, 2, 3], 2)         # seed SMA(1,2)=1.5 ; α=2/3 → 3·2/3 + 1.5/3 = 2.5
    assert out == pytest.approx([1.5, 2.5])
    assert se.ema([1], 2) == []


def test_rsi_wilder_hand_computed():
    # closes 10,11,10,12,11 ; n=3 → ilk ort. kazanç 1, kayıp 1/3 ; sonra kazanç 2/3, kayıp 5/9 → RS=1.2
    rsi = se.rsi_series([10, 11, 10, 12, 11], 3)
    assert rsi[0] == pytest.approx(75.0)
    assert rsi[1] == pytest.approx(100 - 100 / 2.2)


def test_rsi_monotone_extremes():
    assert se.rsi_series(list(range(1, 40)), 14)[-1] == pytest.approx(100.0)
    assert se.rsi_series(list(range(40, 1, -1)), 14)[-1] == pytest.approx(0.0)


def test_atr_constant_range():
    h, l, c = [11.0] * 30, [9.0] * 30, [10.0] * 30
    assert se.atr_series(h, l, c, 14)[-1] == pytest.approx(2.0)


def test_adx_trend_high_vs_chop_low():
    t = _cd(_trend_closes())
    ch = _cd(_choppy_closes())
    assert se.adx_last(t.h, t.l, t.c) > 40
    assert se.adx_last(ch.h, ch.l, ch.c) < 20
    assert se.adx_last([1, 2], [1, 2], [1, 2]) is None


def test_supertrend_direction():
    up, dn = _cd(_trend_closes(step=0.4, start=50)), _cd(_trend_closes(step=-0.4))
    assert se.supertrend_dir(up.h, up.l, up.c) == 1
    assert se.supertrend_dir(dn.h, dn.l, dn.c) == -1
    assert se.supertrend_dir([1], [1], [1]) == 0


def test_bollinger_z_sign():
    assert se.bollinger_z([10.0] * 19 + [5.0], 20) < -2
    assert se.bollinger_z([10.0] * 19 + [15.0], 20) > 2
    assert se.bollinger_z([10.0] * 20, 20) is None


def test_swing_levels_finds_support_and_resistance():
    closes = [10, 9, 8, 9, 10, 11, 12, 11, 10, 9, 8.05, 9, 10, 11]
    h = [c + 0.1 for c in closes]
    l = [c - 0.1 for c in closes]
    lv = se.swing_levels(h, l, 2, 2, tol=0.3)
    kinds = {k for _, _, k in lv}
    assert kinds == {"support", "resistance"}
    sup = [x for x in lv if x[2] == "support"]
    assert sup[0][1] == 2                                  # 8 ve 8.05 aynı kümede (2 temas)


def test_rsi_divergence_bullish():
    lows = [10, 9, 8, 9, 10, 9, 7.5, 9, 10, 11, 12]       # fiyat LL (8 → 7.5)
    highs = [x + 1 for x in lows]
    rsi = [50, 40, 25, 40, 50, 45, 30, 45, 55, 60, 65]     # RSI HL (25 → 30)
    assert se.rsi_divergence(lows, highs, rsi, width=2) == 1


# ------------------------------------------------------------------ look-ahead
def test_parse_klines_drops_unclosed_candle_no_lookahead():
    closes = _trend_closes(60)
    rows = _rows(closes)
    forming = list(rows[-1])
    forming[4] = "9999"                                   # oluşmakta olan mumda uç değer
    forming[2] = "9999"
    with_forming = rows[:-1] + [forming]
    a, b = se.parse_klines(rows), se.parse_klines(with_forming)
    assert a.c == b.c and len(a) == len(closes) - 1        # son mum hiç kullanılmaz
    assert se.rsi_series(a.c)[-1] == se.rsi_series(b.c)[-1]


def test_fetch_candles_error_returns_none():
    class Bad:
        def _get(self, *a, **k):
            raise RuntimeError("429")
    assert se.fetch_candles(Bad(), "XUSDT", "15m") is None


# ------------------------------------------------------------------ istatistik araçları
def test_wilson_interval_known_values():
    lo, hi = se.wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    assert se.wilson_interval(0, 0) == (0.0, 1.0)
    assert se.wilson_interval(10, 10)[1] == pytest.approx(1.0)


def test_benjamini_hochberg_known_example():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.060, 0.074, 0.205, 0.212, 0.216]
    assert se.benjamini_hochberg(p, 0.05) == [True, True] + [False] * 8
    assert se.benjamini_hochberg([0.5, 0.6], 0.1) == [False, False]


def test_kaplan_meier_with_censoring():
    curve = se.kaplan_meier([1, 2, 2, 3], [True, True, False, True])
    assert curve == [(1, pytest.approx(0.75)), (2, pytest.approx(0.5)), (3, pytest.approx(0.0))]


def test_permutation_pvalue_detects_lift():
    sig = [1] * 80 + [0] * 20
    base = [1] * 50 + [0] * 50
    assert se.permutation_pvalue(sig, base, 500, seed=1) < 0.01
    same = se.permutation_pvalue(base, list(base), 500, seed=1)
    assert same > 0.3


def test_outcome_from_bars_real_and_fake():
    up_c = [100.3, 100.6, 101.0, 101.2]
    real, fwd, persist = se.outcome_from_bars(1, 100.0, [c + 0.1 for c in up_c],
                                              [c - 0.1 for c in up_c], up_c, 4, 0.0008, 0.01)
    assert real and fwd == pytest.approx(0.012) and persist == 4
    dn_c = [99.5, 99.0, 98.0, 97.0]                        # yön korunmadı, SL aşıldı
    real, fwd, persist = se.outcome_from_bars(1, 100.0, [100.0] * 4, [c - 0.1 for c in dn_c], dn_c,
                                              4, 0.0008, 0.01)
    assert not real and persist == 1
    assert se.outcome_from_bars(1, 100.0, [100], [100], [100], 4, 0, 0.01) is None


# ------------------------------------------------------------------ signal_outcomes (ayrı DB)
def _reading(name="rsi_extreme", fam=se.FAM_EXHAUSTION, d=1, s=0.8):
    return SignalReading(fam, name, d, s, "15m")


def test_store_posterior_neutral_until_min_events_then_beta(tmp_path):
    st = SignalStore(tmp_path / "s.db")
    cfg = {"confirm_min_events": 5}
    assert st.p_fake("rsi_extreme", "range", cfg) == (0.5, False)
    c = sqlite3.connect(st.path)
    for i in range(10):
        c.execute("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,"
                  "entry_ts,entry_price,horizon,resolved,real) VALUES (0,'X','rsi_extreme','exhaustion',"
                  "'range',1,?,100,8,1,?)", (i, 1 if i < 8 else 0))
    c.commit(); c.close()
    pf, cal = st.p_fake("rsi_extreme", "range", cfg)
    assert cal and pf == pytest.approx(1 - (1 + 8) / (2 + 10))     # Beta(1+8, 1+2)
    assert st.p_fake("rsi_extreme", "trend", cfg) == (0.5, False)  # rejim ayrı


def test_store_record_dedup_and_resolve(tmp_path):
    st = SignalStore(tmp_path / "s.db")
    cd = _cd([100 + 0.3 * i for i in range(40)])
    ts = cd.t[20]
    assert st.record_event("X", _reading(), "range", ts, cd.c[20], 8)
    assert not st.record_event("X", _reading(), "range", ts, cd.c[20], 8)   # aynı olay tekrar yok
    assert st.resolve_pending("X", cd, 0.0008, 0.05) == 1
    row = sqlite3.connect(st.path).execute("SELECT resolved, real FROM signal_outcomes").fetchone()
    assert row == (1, 1)


def test_store_uses_separate_db_not_risk_ledger(tmp_path):
    st = SignalStore(tmp_path / "s.db")
    st.record_event("X", _reading(), "range", 1, 100.0, 8)
    tables = {r[0] for r in sqlite3.connect(st.path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"signal_outcomes", "sqlite_sequence"}


# ------------------------------------------------------------------ confirm(): veto / aileler / p_real
class _FakeStore:
    def __init__(self, pf, cal=True):
        self.pf, self.cal = pf, cal

    def p_fake(self, name, regime, cfg=None):
        return self.pf, self.cal


def _flat_main(n=100):
    return _cd([100.0 + (0.01 if i % 2 else 0.0) for i in range(n)])


def _trend(tf, score, adx):
    return {"tf": tf, "score": score, "st": int(score), "ema_dir": int(score), "adx": adx,
            "adx_strength": min(max((adx - 15.0) / 25.0, 0.0), 1.0)}


@pytest.fixture
def patch_signals(monkeypatch):
    def _set(trend_score=0.0, adx=10.0, exh=(), struct=(), vol=()):
        monkeypatch.setattr(se, "trend_reading", lambda cd, tf: _trend(tf, trend_score, adx))
        monkeypatch.setattr(se, "exhaustion_readings", lambda cd: list(exh))
        monkeypatch.setattr(se, "structure_readings", lambda cd: list(struct))
        monkeypatch.setattr(se, "volume_readings", lambda cd: list(vol))
    return _set


def _trend_cds():
    return {"1h": _flat_main(), "4h": _flat_main()}


def test_confirm_fail_closed_on_missing_data():
    assert not se.confirm(1, None, _trend_cds()).allow
    assert not se.confirm(1, _flat_main(), {"1h": None, "4h": _flat_main()}).allow
    assert "fail-closed" in se.confirm(1, _flat_main(), {}).reason


def test_confirm_trend_veto_blocks_long_against_downtrend(patch_signals):
    patch_signals(trend_score=-1.0, adx=40.0,
                  exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)])
    v = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.1))
    assert not v.allow and v.veto and v.trend_against >= 0.6


def test_confirm_veto_symmetric_for_short(patch_signals):
    patch_signals(trend_score=+1.0, adx=40.0,
                  exh=[_reading(d=-1)], struct=[_reading("sr_proximity", se.FAM_STRUCTURE, d=-1)])
    assert se.confirm(-1, _flat_main(), _trend_cds(), _FakeStore(0.1)).veto
    assert not se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.1)).veto   # trend lehine


def test_confirm_no_veto_when_trend_weak(patch_signals):
    patch_signals(trend_score=-1.0, adx=18.0,
                  exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)])
    assert not se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.1)).veto


def test_confirm_same_family_counts_once(patch_signals):
    patch_signals(exh=[_reading("rsi_extreme"), _reading("bollinger_z"), _reading("rsi_divergence")])
    v = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.05))
    assert not v.allow and "teyit yetersiz (1/2" in v.reason


def test_confirm_opposite_direction_readings_do_not_count(patch_signals):
    patch_signals(exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE, d=-1)])
    assert not se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.05)).allow


def test_confirm_weak_strength_does_not_count(patch_signals):
    patch_signals(exh=[_reading(s=0.1)], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)])
    assert not se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.05)).allow


def test_confirm_cold_start_uncalibrated_rejects_even_with_two_families(patch_signals):
    """p_fake=0.5 nötr → p_real=prior=0.5 < 0.65: kalibrasyon olmadan 'on' muhafazakâr reddeder."""
    patch_signals(exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)])
    v = se.confirm(1, _flat_main(), _trend_cds(), None)
    assert not v.allow and v.p_real == pytest.approx(0.5) and not v.calibrated


def test_confirm_p_real_threshold_0_65(patch_signals):
    patch_signals(exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)])
    strong = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.2))     # 2·ln4 → p≈0.94
    assert strong.allow and strong.p_real > 0.9 and strong.calibrated
    edge = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.40))      # 2·ln1.5 → p≈0.69
    assert edge.allow and 0.65 <= edge.p_real < 0.75
    weak = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.45))      # p≈0.60 < 0.65
    assert not weak.allow and 0.55 < weak.p_real < 0.65
    assert se.CONFIRM_DEFAULTS["confirm_min_p_real"] == 0.65


def test_confirm_three_families_more_confident_than_two(patch_signals):
    patch_signals(exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)],
                  vol=[_reading("volume_climax", se.FAM_VOLUME)])
    three = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.40))
    patch_signals(exh=[_reading()], struct=[_reading("sr_proximity", se.FAM_STRUCTURE)])
    two = se.confirm(1, _flat_main(), _trend_cds(), _FakeStore(0.40))
    assert three.p_real > two.p_real


# ------------------------------------------------------------------ bayrak + open_coin entegrasyonu
def test_default_flag_off_and_config_in_risk_config():
    assert auto_trader.STRATEGY_CONFIRMATION == "off"
    for k in se.CONFIRM_DEFAULTS:
        assert k in auto_trader.RISK_CONFIG
    assert auto_trader.RISK_CONFIG["confirm_min_p_real"] == 0.65
    assert auto_trader.RISK_CONFIG["confirm_tf_main"] == "15m"
    assert auto_trader.RISK_CONFIG["confirm_tf_veto"] == ("1h", "4h")


def _coin():
    return {"symbol": "XUSDT", "lastPrice": "100", "quoteVolume": "1e9",
            "priceChangePercent": "-5", "_score": 0.9}


class _FakeBinance:
    """Aşağı trend klines (15m/1h/4h) — decide_side LONG (düşeni al) der, trend tersine."""

    def __init__(self, closes=None, fail=False):
        self.closes = closes if closes is not None else _trend_closes()
        self.fail = fail
        self.kline_calls = 0

    def _get(self, path, params=None, signed=False):
        if path == "/fapi/v1/klines":
            self.kline_calls += 1
            if self.fail:
                raise RuntimeError("429")
            return _rows(self.closes + [self.closes[-1]])
        raise AssertionError(path)

    def order_book(self, symbol, limit=20):
        return {"bids": [["99.99", "1000"]], "asks": [["100.01", "1000"]]}

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"}, "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 1000.0


def _patch_open_coin(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    monkeypatch.setattr(auto_trader, "SCIENTIFIC_SIZING", "off")
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "off")
    monkeypatch.setattr(auto_trader, "check_liquidity", lambda b, s: (True, "ok"))
    monkeypatch.setattr(auto_trader, "check_correlation_limit", lambda s, b: True)
    monkeypatch.setattr(auto_trader, "calculate_atr", lambda *a, **k: 0.01)
    monkeypatch.setattr(auto_trader, "get_real_slippage", lambda b, s: 0.0002)
    monkeypatch.setitem(auto_trader.RISK_CONFIG, "risk_guards_enabled", False)
    monkeypatch.setattr(se, "SIGNAL_DB", str(tmp_path / "so.db"))


def test_red_old_code_only_uses_price_change_and_ignores_trend(monkeypatch, tmp_path):
    """RED: 'off' (eski) → 1h/4h güçlü aşağı trendde bile LONG dip-alımı devam eder; klines hiç istenmez."""
    fake = _FakeBinance()
    _patch_open_coin(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_trader, "STRATEGY_CONFIRMATION", "off")
    assert auto_trader.decide_side(_coin())[0] == 1                       # yalnız %−5 değişim → LONG
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is True
    assert fake.kline_calls == 0


def test_green_on_vetoes_long_against_strong_downtrend(monkeypatch, tmp_path):
    """GREEN: aynı girdi, 'on' → güçlü aşağı trendde LONG mean-reversion girişi VETO edilir."""
    fake = _FakeBinance()
    _patch_open_coin(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_trader, "STRATEGY_CONFIRMATION", "on")
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is False
    assert fake.kline_calls == 3                                          # 15m + 1h + 4h


def test_green_on_fail_closed_when_klines_fail(monkeypatch, tmp_path):
    fake = _FakeBinance(fail=True)
    _patch_open_coin(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_trader, "STRATEGY_CONFIRMATION", "on")
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is False


def test_compare_logs_records_but_does_not_block(monkeypatch, tmp_path):
    fake = _FakeBinance()
    _patch_open_coin(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_trader, "STRATEGY_CONFIRMATION", "compare")
    assert auto_trader.open_coin(fake, None, _coin(), dry_run=True) is True   # karar etkilenmedi
    n = sqlite3.connect(str(tmp_path / "so.db")).execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
    assert n > 0                                                               # hipotetik olaylar yazıldı


def test_on_never_adds_entries_verdict_is_bool_only():
    v = se.Verdict(False, "x")
    assert isinstance(v.allow, bool) and not hasattr(v, "side")
