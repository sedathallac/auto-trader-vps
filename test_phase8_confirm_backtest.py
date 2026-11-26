"""Faz 8 RED/GREEN — confirm_backtest (offline backtest/kalibrasyon koşucusu).

RED:   Backtest aracı yokken posterior'lar (p_fake/p_win) hiç eğitilemez → 'on' modu kalibrasyonsuz
       her girişi reddeder; ve eğitim verisiyle ölçülen başarı aşırı uyumu gizler.
GREEN: Walk-forward + purge + OOS; gerçek kenar bulunur, saf gürültüde kenar İDDİA EDİLMEZ;
       look-ahead yok; kapsam izolasyonu (canlı işlem/anahtar/varsayılan yazma yok).
"""
import json
import math
import random
import sqlite3
from pathlib import Path

import pytest

import auto_trader
import confirm_backtest as cb
import strategy_engine as se
import whale_engine as we
from strategy_engine import SignalReading

M15 = cb.TF_MS["15m"]


# ------------------------------------------------------------------ sentetik klines
def make_rows(n15, seed, end_ms, vol_sd=0.004, drift=0.0):
    """n15 adet KAPANMIŞ 15m mum (random walk) + aynı veriden toplulaştırılmış 1h/4h. 11 alanlı Binance satırı."""
    rng = random.Random(seed)
    t0 = end_ms - n15 * M15
    px, rows = 100.0, []
    for i in range(n15):
        o = px
        c = o * (1 + drift + rng.gauss(0, vol_sd))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol_sd / 3)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol_sd / 3)))
        v = 100 + rng.random() * 50
        tb = v * min(max(0.5 + (c - o) / o * 20 + rng.gauss(0, 0.05), 0.0), 1.0)
        rows.append([t0 + i * M15, str(o), str(h), str(l), str(c), str(v), t0 + (i + 1) * M15 - 1,
                     "0", 1, str(tb), "0", "0"])
        px = c
    return rows


def aggregate(rows15, tf):
    k = cb.TF_MS[tf] // M15
    out = []
    for i in range(0, len(rows15) - k + 1, k):
        g = rows15[i:i + k]
        if int(g[0][0]) % cb.TF_MS[tf] != 0:
            continue
        out.append([g[0][0], g[0][1], str(max(float(x[2]) for x in g)), str(min(float(x[3]) for x in g)),
                    g[-1][4], str(sum(float(x[5]) for x in g)), g[-1][6], "0", 1,
                    str(sum(float(x[9]) for x in g)), "0", "0"])
    return out


def aligned_end(now_ms=1_800_000_000_000):
    return now_ms - now_ms % cb.TF_MS["4h"]


def candles_from(rows):
    return se.parse_klines(rows, drop_unclosed=False)


@pytest.fixture(scope="module")
def series():
    end = aligned_end()
    r15 = make_rows(9008, seed=3, end_ms=end)   # 16'nın katı → 4h sınırlarına hizalı
    return {"end": end, "15m": r15, "1h": aggregate(r15, "1h"), "4h": aggregate(r15, "4h")}


def fake_getter_factory(series):
    calls = []

    def getter(params):
        calls.append(dict(params))
        rows = series[params["interval"]]
        out = [r for r in rows if params["startTime"] <= int(r[0]) < params["endTime"]]
        return out[:params["limit"]], 10
    getter.calls = calls
    return getter


# ------------------------------------------------------------------ veri çekme
def test_fetch_paginates_and_returns_only_closed_candles(series):
    g = fake_getter_factory(series)
    start = series["end"] - 3000 * M15
    rows = cb.fetch_klines_range(g, "X", "15m", start, series["end"], limit=1000, sleeper=lambda s: None)
    assert len(rows) == 3000 and len(g.calls) >= 3                      # sayfalama
    assert [int(r[0]) for r in rows] == sorted(int(r[0]) for r in rows)
    assert all(int(r[0]) + M15 <= series["end"] for r in rows)         # oluşmakta olan mum yok
    assert len({int(r[0]) for r in rows}) == len(rows)                 # tekrar yok


def test_fetch_throttles_and_backs_off_near_weight_budget(series):
    sleeps = []

    def g(params):
        rows = series["15m"]
        out = [r for r in rows if params["startTime"] <= int(r[0]) < params["endTime"]][:params["limit"]]
        return out, 1500                                                # ağırlık yumuşak sınırın üstünde
    cb.fetch_klines_range(g, "X", "15m", series["end"] - 2000 * M15, series["end"],
                          limit=1000, sleeper=sleeps.append)
    assert 60.0 in sleeps                                               # dakikalık pencere beklendi
    sleeps2 = []
    cb.fetch_klines_range(fake_getter_factory(series), "X", "15m", series["end"] - 2000 * M15,
                          series["end"], limit=1000, sleep_s=0.35, sleeper=sleeps2.append)
    assert sleeps2 and all(s == 0.35 for s in sleeps2)                  # normalde istekler arası bekleme


def test_rate_limit_stops_without_retrying(series):
    n = {"c": 0}

    def g(params):
        n["c"] += 1
        raise cb.RateLimited("HTTP 429")
    with pytest.raises(cb.RateLimited):
        cb.fetch_klines_range(g, "X", "15m", series["end"] - 2000 * M15, series["end"],
                              sleeper=lambda s: None)
    assert n["c"] == 1                                                  # tekrar vurmadı


def test_cache_prevents_second_network_call(series, tmp_path):
    g = fake_getter_factory(series)
    start = series["end"] - 2000 * M15
    a = cb.load_or_fetch(g, "X", "15m", start, series["end"], tmp_path, sleeper=lambda s: None)
    n_first = len(g.calls)
    b = cb.load_or_fetch(g, "X", "15m", start, series["end"], tmp_path, sleeper=lambda s: None)
    assert len(g.calls) == n_first and a == b


# ------------------------------------------------------------------ replay: look-ahead + bot kuralı
@pytest.fixture(scope="module")
def full_events(series):
    return cb.replay_events("X", candles_from(series["15m"]), candles_from(series["1h"]),
                            candles_from(series["4h"]), horizon=8, stride=8)


def test_synthetic_series_aligned_for_all_timeframes(series):
    """Fixture sağlamlığı: 1h/4h toplulaştırma boş kalmamalı (c4h=None hatasının kök nedeni buydu)."""
    assert len(series["1h"]) > 2000 and len(series["4h"]) > 500
    assert int(series["4h"][0][0]) % cb.TF_MS["4h"] == 0


def test_replay_returns_empty_when_a_timeframe_is_missing(series):
    c15, c1h = candles_from(series["15m"]), candles_from(series["1h"])
    assert cb.replay_events("X", c15, c1h, None) == []
    assert cb.replay_events("X", c15, None, c1h) == []
    assert cb.replay_events("X", None, c1h, c1h) == []


def test_replay_produces_candidates_following_bot_side_rule(full_events):
    assert len(full_events) > 50
    for e in full_events[:200]:
        assert e["side"] in (1, -1) and e["regime"] in ("range", "trend", "mid")
        assert e["real"] in (0, 1) and e["horizon"] == 8 and e["cost"] == pytest.approx(0.0012)


def test_replay_has_no_lookahead(series, full_events):
    """Verinin sonunu kesmek, kesilmiş bölgeden ÖNCEKİ olayları DEĞİŞTİRMEMELİ."""
    cut = 7000
    r15 = series["15m"][:cut]
    cutoff_ts = int(r15[-1][0]) - 20 * M15
    end_cut = int(r15[-1][0]) + M15
    r1h = [r for r in series["1h"] if int(r[0]) + cb.TF_MS["1h"] <= end_cut]
    r4h = [r for r in series["4h"] if int(r[0]) + cb.TF_MS["4h"] <= end_cut]
    part = cb.replay_events("X", candles_from(r15), candles_from(r1h), candles_from(r4h),
                            horizon=8, stride=8)
    key = lambda e: (e["ts"], e["side"], e["regime"], e["real"], round(e["fwd"], 12))
    a = sorted(key(e) for e in full_events if e["ts"] < cutoff_ts)
    b = sorted(key(e) for e in part if e["ts"] < cutoff_ts)
    assert a == b and len(a) > 30


def test_replay_declusters_by_horizon(full_events):
    ts = sorted(e["ts"] for e in full_events)
    assert all(b - a >= 8 * M15 for a, b in zip(ts, ts[1:]))


def test_bot_constants_match_sim_defaults():
    s = cb.SIM_DEFAULTS
    assert s["long_dip_pct"] == auto_trader.LONG_ON_DIP_PCT
    assert s["short_pump_pct"] == auto_trader.SHORT_ON_PUMP_PCT
    assert s["min_abs_pct"] == auto_trader.MIN_PCT_CHANGE and s["max_abs_pct"] == auto_trader.MAX_PCT_CHANGE
    assert s["fee_rt"] == auto_trader.RISK_CONFIG["fee_pct_round_trip"]
    assert s["atr_sl_mult"] == auto_trader.RISK_CONFIG["atr_sl_multiplier"]
    assert (s["sl_min"], s["sl_max"]) == (auto_trader.RISK_CONFIG["sl_min_pct"], auto_trader.RISK_CONFIG["sl_max_pct"])


# ------------------------------------------------------------------ sentetik OLAY havuzu (kenar kontrollü)
def _ev(i, rng, regime, sigs, real, side=1, trend_score=0.0):
    readings = [SignalReading(f, n, side, 0.8, "15m") for n, f in sigs]
    tr = [{"tf": "1h", "score": trend_score, "st": 0, "ema_dir": 0, "adx": 10.0, "adx_strength": 0.0},
          {"tf": "4h", "score": trend_score, "st": 0, "ema_dir": 0, "adx": 10.0, "adx_strength": 0.0}]
    return {"symbol": "X", "ts": 1_700_000_000_000 + i * 8 * M15, "side": side, "regime": regime,
            "readings": readings, "trends": tr, "real": int(real), "fwd": 0.01 if real else -0.01,
            "persist": 8, "sl": 0.01, "cvd": None, "entry": 100.0, "horizon": 8, "cost": 0.0012}


SIG_A = ("rsi_extreme", se.FAM_EXHAUSTION)
SIG_B = ("sr_proximity", se.FAM_STRUCTURE)
SIG_NOISE = ("volume_climax", se.FAM_VOLUME)


def planted_events(n=3000, seed=1, edge=True):
    """Baz gerçek-oranı 0.45; edge=True ise A&B birlikte aktifken 0.80; NOISE sinyali etkisiz."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        has_ab = rng.random() < 0.3
        sigs = [SIG_A, SIG_B] if has_ab else []
        if rng.random() < 0.3:
            sigs.append(SIG_NOISE)
        p = 0.80 if (edge and has_ab) else 0.45
        out.append(_ev(i, rng, "range", sigs, rng.random() < p))
    return out


def test_fit_signal_table_approves_real_edge_and_rejects_noise_signal():
    ev = planted_events()
    t = cb.fit_signal_table(ev, min_events=100, fdr=0.10, n_perm=400, seed=0)
    assert t[("rsi_extreme", "range")]["approved"] and t[("sr_proximity", "range")]["approved"]
    assert not t[("volume_climax", "range")]["approved"]                  # etkisiz sinyal seçilmez
    pf = t[("rsi_extreme", "range")]["p_fake"]
    assert pf < 0.4                                                       # Beta posterior ≈ 1 − 0.80


def test_fit_signal_table_respects_min_events():
    t = cb.fit_signal_table(planted_events(300), min_events=10_000, n_perm=50)
    assert not any(r["approved"] for r in t.values())


def test_green_walk_forward_finds_planted_edge_out_of_sample():
    wf = cb.walk_forward(planted_events(), n_folds=4, min_events=100, n_perm=400, seed=0)
    assert wf["verdict"] == "PASS"
    assert wf["accepted_real_rate"] > wf["base_real_rate"] + 0.15
    assert wf["accepted_expectancy"] > wf["base_expectancy"]
    assert wf["accepted_wilson"][0] > wf["base_real_rate"]                # Wilson alt sınırı bile üstte
    assert wf["p_value"] < 0.05 and wf["n_accepted"] >= 30


def test_green_walk_forward_claims_no_edge_on_pure_noise():
    """Aşırı uyum koruması: gerçek kenar YOKKEN OOS 'PASS' vermemeli."""
    wf = cb.walk_forward(planted_events(edge=False, seed=7), n_folds=4, min_events=100, n_perm=400, seed=0)
    assert wf["verdict"] != "PASS"


def test_red_in_sample_selection_would_overfit_but_oos_does_not():
    """RED: aynı gürültü verisinde TÜM veriyle seçip aynı veriyle ölçmek 'kenar' bulabilir (aşırı uyum);
    OOS walk-forward bunu bulmaz. Burada yalnız OOS sonucunun dürüstlüğünü sabitliyoruz."""
    ev = planted_events(edge=False, seed=11)
    wf = cb.walk_forward(ev, n_folds=4, min_events=20, fdr=0.5, n_perm=200, seed=0)   # gevşek seçim
    if wf.get("p_value") is not None and wf["verdict"] == "PASS":
        pytest.fail("gürültüde OOS PASS = aşırı uyum")
    assert wf["verdict"] in ("NO_EDGE", "INSUFFICIENT_DATA")


def test_walk_forward_purges_and_never_tests_fold0(monkeypatch):
    ev = planted_events(800)
    seen = []
    real_fit = cb.fit_signal_table

    def spy(train, **kw):
        seen.append((max(e["ts"] for e in train) + 8 * M15, len(train)))
        return real_fit(train, **kw)
    monkeypatch.setattr(cb, "fit_signal_table", spy)
    wf = cb.walk_forward(ev, n_folds=4, min_events=10, n_perm=20)
    bounds = cb.time_folds(ev, 4)
    assert len(seen) == len(wf["folds"]) == 3 and [f["fold"] for f in wf["folds"]] == [1, 2, 3]
    for (max_end, _), k in zip(seen, (1, 2, 3)):
        assert max_end < bounds[k]                                         # purge: eğitim sonuçları test başlamadan bitti
    assert seen[0][1] < seen[1][1] < seen[2][1]                            # genişleyen pencere


def test_walk_forward_insufficient_data_is_reported():
    wf = cb.walk_forward(planted_events(5), n_folds=4)
    assert wf["verdict"] == "INSUFFICIENT_DATA"


def test_oos_uses_same_evaluate_as_live():
    """Backtest kuralı canlıdaki se.evaluate ile birebir: onaylı sinyalli olay kabul, onaysız ret."""
    ev = planted_events()
    t = cb.fit_signal_table(ev, min_events=100, n_perm=300, seed=0)
    pf = cb.p_fake_fn_from_table(t)
    e_ab = next(e for e in ev if {r.name for r in e["readings"]} >= {"rsi_extreme", "sr_proximity"})
    assert se.evaluate(e_ab["side"], e_ab["readings"], e_ab["trends"], e_ab["regime"], pf).allow
    e_none = next(e for e in ev if not e["readings"])
    assert not se.evaluate(e_none["side"], e_none["readings"], e_none["trends"], e_none["regime"], pf).allow
    assert pf("rsi_extreme", "trend") == (0.5, False)                      # görülmemiş rejim → nötr


def test_trend_veto_still_applies_in_backtest_decision():
    ev = planted_events(50)[0]
    ev["trends"] = [{"tf": "1h", "score": -1.0, "st": -1, "ema_dir": -1, "adx": 45.0, "adx_strength": 1.0},
                    {"tf": "4h", "score": -1.0, "st": -1, "ema_dir": -1, "adx": 45.0, "adx_strength": 1.0}]
    pf = lambda n, r: (0.1, True)
    assert se.evaluate(1, ev["readings"] or [SignalReading(se.FAM_EXHAUSTION, "x", 1, 0.9)],
                       ev["trends"], "trend", pf).veto


# ------------------------------------------------------------------ whale p_win kalibrasyonu
def whale_events(n=4000, seed=2, informative=True):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        side = 1 if rng.random() < 0.5 else -1
        cvd = rng.uniform(-1, 1)
        align = side * cvd if abs(cvd) >= 0.15 else 0.0
        p = (0.5 + 0.3 * align) if informative else 0.5
        e = _ev(i, rng, "range", [], rng.random() < p, side=side)
        e["cvd"] = cvd
        out.append(e)
    return out


def test_green_whale_pwin_calibration_skill_positive_when_informative():
    calib, _ = cb.whale_pwin_walk_forward(whale_events(), n_folds=4, min_events=100)
    assert calib["oos_brier_skill"] > 0.02 and calib["n_oos"] > 1000
    b = calib["buckets"]
    assert b[-1]["p_win"] > b[0]["p_win"] + 0.2                               # hizalı güçlü > ters güçlü


def test_green_whale_calibration_not_written_when_no_skill(tmp_path):
    calib, _ = cb.whale_pwin_walk_forward(whale_events(informative=False, seed=5), n_folds=4, min_events=100)
    assert calib["oos_brier_skill"] <= 0.005
    p = tmp_path / "w.json"
    assert not cb.write_whale_calibration({**calib, "oos_brier_skill": -0.01}, p) and not p.exists()


def test_whale_calibration_written_and_consumed_by_engine(tmp_path):
    calib, _ = cb.whale_pwin_walk_forward(whale_events(), n_folds=4, min_events=100)
    p = tmp_path / "w.json"
    assert cb.write_whale_calibration(calib, p)
    loaded = we.load_pwin_calibration(p)
    hi = we.pwin_from_calibration(0.9, loaded, min_n=100)
    lo = we.pwin_from_calibration(-0.9, loaded, min_n=100)
    assert hi is not None and lo is not None and hi > lo + 0.2


def test_engine_ignores_calibration_without_oos_skill_or_data():
    base = {"edges": list(we.PWIN_EDGES),
            "buckets": [{"n": 500, "p_win": 0.7}] * 5, "oos_brier_skill": 0.0}
    assert we.pwin_from_calibration(0.5, base) is None                       # skill ≤ 0 → kullanma
    assert we.pwin_from_calibration(0.5, {**base, "oos_brier_skill": 0.1,
                                          "buckets": [{"n": 5, "p_win": 0.7}] * 5}) is None
    assert we.pwin_from_calibration(0.5, None) is None
    assert we.load_pwin_calibration("/nonexistent.json") is None


def test_pwin_bucket_mapping_edges():
    assert we.pwin_bucket(-1.0) == 0 and we.pwin_bucket(0.0) == 2 and we.pwin_bucket(1.0) == 4
    assert we.pwin_bucket(-0.4) == 1 and we.pwin_bucket(0.15) == 3


def test_expected_edge_uses_calibrated_pwin_and_drops_uncertainty_prime():
    bids = [(99.99 - 0.01 * i, 1000.0) for i in range(30)]
    asks = [(100.01 + 0.01 * i, 1000.0) for i in range(30)]
    sig = we.WhaleSignal(1, 0.5, True, {"mid": 100.0})
    kw = dict(sl_pct=0.01, tp_pct=0.015, atr_pct=0.01, drawdown=0.0, notional=25.0, bids=bids, asks=asks,
              fee_rt=0.0008)
    cold = we.expected_edge(1, sig, **kw)
    cal = we.expected_edge(1, sig, p_win_calibrated=0.62, calibrated=True, **kw)
    assert cal.p_win == pytest.approx(0.62) and cal.risk_premium < cold.risk_premium


# ------------------------------------------------------------------ DB yazımı (açık bayrak, ayrı dosya)
def test_write_outcomes_db_only_approved_and_posterior_picks_it_up(tmp_path):
    ev = planted_events()
    t = cb.fit_signal_table(ev, min_events=100, n_perm=300, seed=0)
    db = tmp_path / "so.db"
    n = cb.write_outcomes_db(ev, t, db, 8)
    assert n > 100
    sigs = {r[0] for r in sqlite3.connect(db).execute("SELECT DISTINCT signal FROM signal_outcomes")}
    assert "volume_climax" not in sigs and {"rsi_extreme", "sr_proximity"} <= sigs
    assert cb.write_outcomes_db(ev, t, db, 8) == 0                            # tekrar yazmaz (idempotent)
    store = se.SignalStore(db)
    pf, cal = store.p_fake("rsi_extreme", "range", {"confirm_min_events": 100})
    assert cal and pf < 0.4                                                  # canlı motor posterior'ı kullanır
    srcs = {r[0] for r in sqlite3.connect(db).execute("SELECT DISTINCT source FROM signal_outcomes")}
    assert srcs == {"offline"}


# ------------------------------------------------------------------ uçtan uca + kapsam izolasyonu
def test_run_backtest_end_to_end_synthetic(series, tmp_path):
    g = fake_getter_factory(series)
    events, report, table, whale = cb.run_backtest(
        g, ["X"], days=60, folds=3, stride=8, cache_dir=tmp_path, n_perm=50,
        now_ms=series["end"], sleeper=lambda s: None)
    assert report["params"]["n_events"] == len(events) > 30
    assert report["strategy_walk_forward"]["verdict"] in ("PASS", "NO_EDGE", "INSUFFICIENT_DATA")
    assert "whale_pwin" in report and "final_signal_table" in report
    json.dumps(report, default=str)                                          # serileştirilebilir


def test_main_default_writes_nothing_persistent_and_no_orders(series, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cb, "default_getter", fake_getter_factory(series))
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)
    monkeypatch.setattr(cb.time, "time", lambda: series["end"] / 1000.0)
    monkeypatch.setattr(se, "SIGNAL_DB", str(tmp_path / "so.db"))
    monkeypatch.setattr(we, "WHALE_PWIN_CALIBRATION", str(tmp_path / "w.json"))
    rc = cb.main(["--symbols", "X", "--days", "60", "--folds", "3", "--stride", "8", "--n-perm", "30",
                  "--cache-dir", str(tmp_path / "cache")])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["params"]["symbols"] == ["X"]
    assert not (tmp_path / "so.db").exists() and not (tmp_path / "w.json").exists()   # varsayılan: yazma YOK


def test_main_returns_2_on_rate_limit(monkeypatch, tmp_path):
    def boom(params):
        raise cb.RateLimited("HTTP 429")
    monkeypatch.setattr(cb, "default_getter", boom)
    assert cb.main(["--symbols", "X", "--cache-dir", str(tmp_path)]) == 2


def test_scope_isolation_no_live_trading_no_keys():
    src = Path(cb.__file__).read_text()
    assert "import auto_trader" not in src and "from auto_trader" not in src
    for forbidden in ("X-MBX-APIKEY", "signature", "api_key", "secret", "/fapi/v1/order", "set_leverage",
                      "create_executor", "positionRisk", "signed=True"):
        assert forbidden not in src, forbidden
    assert cb.KLINES_PATH == "/fapi/v1/klines"
    # canlı bot varsayılanları değişmedi
    assert auto_trader.STRATEGY_CONFIRMATION == "off" and auto_trader.WHALE_HUNTING == "off"
    assert auto_trader.SCIENTIFIC_SIZING == "off" and auto_trader.HIERARCHICAL_SCAN == "off"
