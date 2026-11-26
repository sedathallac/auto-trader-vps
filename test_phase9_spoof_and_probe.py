"""Faz 9 düzeltmeleri — RED/GREEN.

A) Spoof: 15 dk arayla alınan iki örnekte kaybolan duvar spoof SAYILMAZ (eski kural ölçüm artefaktıydı: bot deftere
   15 dk'da bir bakıyor, defter sürekli değişiyor → %75 sahte spoof). Spoof yalnız AYNI aday için 5 sn arayla alınan 3
   örnekte (≥10 sn pencere) ilk örnekte var / son örnekte aynı fiyat seviyesinde yok ise.
B) Aday sayısı: WHALE_HUNTING=compare iken TOP_N_COMPARE=20 aday değerlendirilir; fazlası YALNIZ loglanır (emir yok).
"""
import sqlite3

import pytest

import auto_trader
import whale_engine as we
from whale_engine import WhaleTracker
from test_phase5_whale_engine import _inputs, _trades, _FakeBinance, _coin, _patch_open_coin

WALL_PX = 99.70


def _bids(wall_qty=60000.0, n=60, with_wall=True, floor=None):
    lv = [(round(100 - 0.01 * (i + 1), 2), 100.0) for i in range(n)]
    if with_wall:
        lv = [(p, wall_qty if p == WALL_PX else q) for p, q in lv]
    if floor is not None:
        lv = [(p, q) for p, q in lv if p >= floor]
    return lv


def _asks(n=60):
    return [(round(100 + 0.01 * (i + 1), 2), 100.0) for i in range(n)]


def _sample(t, **kw):
    return {"t": t, "bids": _bids(**kw), "asks": _asks()}


def _single(with_wall):
    return {"symbol": "XUSDT", "bids": _bids(with_wall=with_wall), "asks": _asks(),
            "trades": _trades(1000, 1000), "oi": 1000.0, "funding": 0.0}


# ============================ A) SPOOF ============================
def test_red_old_rule_flags_a_wall_that_moved_between_two_15min_samples_as_spoof():
    """RED (eski kod: True). 15 dk arayla iki örnek → duvar kaybolmuş olsa da bu SPOOF KANITI DEĞİL."""
    t = [0.0]
    tr = WhaleTracker(clock=lambda: t[0])
    we.compute_signal(_single(True), tr)
    t[0] = 900.0
    sig = we.compute_signal(_single(False), tr)
    assert sig.spoof_suspect is False


def test_default_without_multi_sample_is_never_spoof():
    sig = we.compute_signal(_single(True), WhaleTracker())
    assert sig.quality_ok and sig.spoof_suspect is False


def test_green_stable_wall_over_three_samples_is_persistent_and_not_spoof():
    s = [_sample(0.0), _sample(5.0), _sample(10.0)]
    bid_p, ask_p, spoof = we.analyze_wall_samples(s)
    assert WALL_PX in bid_p and ask_p == [] and spoof is False


def test_green_wall_present_first_sample_gone_at_10s_is_spoof():
    s = [_sample(0.0), _sample(5.0), _sample(10.0, with_wall=False)]
    _, _, spoof = we.analyze_wall_samples(s)
    assert spoof is True


def test_green_vanish_inside_less_than_10s_window_is_not_enough():
    s = [_sample(0.0), _sample(5.0, with_wall=False)]
    assert we.analyze_wall_samples(s)[2] is False


def test_green_wall_leaving_the_visible_depth_range_is_inconclusive_not_spoof():
    s = [_sample(0.0), _sample(10.0, floor=99.80)]          # 99.70 is now below the deepest visible bid
    assert we.analyze_wall_samples(s)[2] is False


def test_green_shrunk_below_fraction_counts_as_gone_but_partial_shrink_does_not():
    gone = [_sample(0.0), _sample(10.0, wall_qty=60000 * 0.1)]
    kept = [_sample(0.0), _sample(10.0, wall_qty=60000 * 0.6)]
    assert we.analyze_wall_samples(gone)[2] is True
    assert we.analyze_wall_samples(kept)[2] is False


def test_green_ask_side_spoof_is_detected_too():
    def asks(with_wall):
        lv = [(round(100 + 0.01 * (i + 1), 2), 100.0) for i in range(60)]
        return [(p, 60000.0 if (p == 100.30 and with_wall) else q) for p, q in lv]
    s = [{"t": 0.0, "bids": _bids(with_wall=False), "asks": asks(True)},
         {"t": 10.0, "bids": _bids(with_wall=False), "asks": asks(False)}]
    assert we.analyze_wall_samples(s)[2] is True


class _BookBinance:
    """order_book(limit=100) returns the scripted books in order; other endpoints canned."""
    def __init__(self, books, fail_at=None):
        self.books, self.i, self.fail_at = books, 0, fail_at

    def order_book(self, symbol, limit=20):
        i, self.i = self.i, self.i + 1
        if self.fail_at == i:
            raise RuntimeError("429")
        b = self.books[min(i, len(self.books) - 1)]
        return {"bids": [[str(p), str(q)] for p, q in b["bids"]], "asks": [[str(p), str(q)] for p, q in b["asks"]]}

    def _get(self, path, params=None, signed=False):
        return {"/fapi/v1/aggTrades": _trades(1000, 1000), "/fapi/v1/openInterest": {"openInterest": "1000"},
                "/fapi/v1/premiumIndex": {"lastFundingRate": "0.0"}}[path]


class _Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s); self.t += s


def _books(*wall_flags):
    return [{"bids": _bids(with_wall=f), "asks": _asks()} for f in wall_flags]


def test_green_fetch_takes_three_samples_five_seconds_apart_and_detects_spoof_end_to_end():
    ck = _Clock()
    wi = we.fetch_whale_inputs(_BookBinance(_books(True, True, False)), "XUSDT", clock=ck.now, sleep=ck.sleep)
    assert ck.sleeps == [5.0, 5.0] and len(wi["book_samples"]) == 3
    assert wi["book_samples"][-1]["t"] - wi["book_samples"][0]["t"] >= 10.0
    assert we.compute_signal(wi, WhaleTracker()).spoof_suspect is True


def test_green_fetch_stable_book_is_not_spoof():
    ck = _Clock()
    wi = we.fetch_whale_inputs(_BookBinance(_books(True, True, True)), "XUSDT", clock=ck.now, sleep=ck.sleep)
    sig = we.compute_signal(wi, WhaleTracker())
    assert sig.spoof_suspect is False and sig.components["wall"] == 1.0


def test_green_fetch_failure_modes():
    ck = _Clock()
    assert we.fetch_whale_inputs(_BookBinance(_books(True), fail_at=0), "XUSDT", clock=ck.now, sleep=ck.sleep) is None
    wi = we.fetch_whale_inputs(_BookBinance(_books(True, True), fail_at=2), "XUSDT", clock=ck.now, sleep=ck.sleep)
    assert wi is not None and len(wi["book_samples"]) == 2          # degraded, still usable
    assert we.compute_signal(wi, WhaleTracker()).spoof_suspect is False   # window not reached -> no spoof claim


# ============================ B) 20 ADAY ============================
def _picks(n=30):
    return [{"symbol": f"C{i}USDT", "lastPrice": "100", "quoteVolume": "1e9", "priceChangePercent": "-5",
             "_score": 1.0 - i * 0.01} for i in range(n)]


def test_top_n_compare_default_is_20(monkeypatch):
    assert auto_trader.TOP_N == 2 and auto_trader.TOP_N_COMPARE == 20


def test_select_candidates_splits_main_and_probe_without_overlap():
    picks = _picks(30)
    main, probes = auto_trader.select_candidates(picks, active={"C0-USDT"}, bl={"C1USDT": 1}, top_n=2, total_n=20)
    assert [c["symbol"] for c in main] == ["C2USDT", "C3USDT"]
    assert len(probes) == 18 and not ({c["symbol"] for c in main} & {c["symbol"] for c in probes})
    assert all(c["symbol"] not in ("C0USDT", "C1USDT") for c in main + probes)
    m2, p2 = auto_trader.select_candidates(picks, set(), {}, top_n=2, total_n=2)
    assert len(m2) == 2 and p2 == []


@pytest.mark.parametrize("mode,expected_probes", [("compare", 18), ("off", 0), ("on", 0)])
def test_process_candidates_old_code_handles_2_new_code_handles_20_only_in_compare(monkeypatch, mode, expected_probes):
    opened, probed = [], []
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", mode)
    monkeypatch.setattr(auto_trader, "open_coin", lambda b, h, c, **k: opened.append(c["symbol"]) or True)
    monkeypatch.setattr(auto_trader, "compare_probe", lambda b, c, risk=None: probed.append(c["symbol"]) or True)
    ok, n_main, n_probe = auto_trader.process_candidates(None, None, _picks(30), set(), {}, False, None, None, None)
    assert (len(opened), n_main) == (2, 2)
    assert (len(probed), n_probe) == (expected_probes, expected_probes)


def test_probe_failure_does_not_stop_the_scan_or_touch_the_blacklist(monkeypatch):
    calls = []
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "compare")
    monkeypatch.setattr(auto_trader, "open_coin", lambda *a, **k: True)
    monkeypatch.setattr(auto_trader, "add_blacklist", lambda *a, **k: calls.append(a))
    def boom(b, c, risk=None):
        raise RuntimeError("x")
    monkeypatch.setattr(auto_trader, "compare_probe", boom)
    ok, n_main, n_probe = auto_trader.process_candidates(None, None, _picks(30), set(), {}, False, None, None, None)
    assert n_main == 2 and calls == []


def test_compare_probe_records_whale_metrics_and_never_orders(monkeypatch, tmp_path):
    db = str(tmp_path / "w.db")
    fake = _FakeBinance(_inputs(ask_mult=4.0, buy=500, sell=9500))
    _patch_open_coin(monkeypatch, fake)
    monkeypatch.setattr(auto_trader, "WHALE_HUNTING", "compare")
    monkeypatch.setattr(we, "WHALE_DB", db)
    monkeypatch.setattr(auto_trader, "_WHALE_TRACKER", None)
    forbidden = []
    for name in ("add_blacklist", "create_order", "place_order", "new_order", "place_algo_sl"):
        monkeypatch.setattr(auto_trader, name, lambda *a, _n=name, **k: forbidden.append(_n), raising=False)
    assert auto_trader.compare_probe(fake, _coin()) is True
    assert forbidden == []
    rows = sqlite3.connect(db).execute("SELECT mode, allow FROM whale_metrics").fetchall()
    assert rows == [("compare", 0)]


# ============================ C) SPOOF TANI KAYDI (components_json) ============================
def test_spoof_detail_records_distance_and_notional_of_vanished_wall():
    s = [_sample(0.0), _sample(5.0), _sample(10.0, with_wall=False)]
    detail = []
    _, _, spoof = we.analyze_wall_samples(s, detail=detail)
    assert spoof is True and len(detail) == 1
    d = detail[0]
    mid = (s[0]["bids"][0][0] + s[0]["asks"][0][0]) / 2
    assert d["side"] == "bid" and d["price"] == WALL_PX
    assert d["dist_bps"] == pytest.approx(abs(WALL_PX - mid) / mid * 1e4, abs=0.1)
    assert d["first_notional"] == pytest.approx(WALL_PX * 60000.0, rel=1e-6)
    assert d["last_notional"] == pytest.approx(WALL_PX * 100.0)        # level is back to its ordinary size
    assert d["last_notional"] < 0.2 * d["first_notional"] and d["elapsed_s"] == 10.0


def test_signal_components_carry_spoof_detail_and_are_json_serializable(tmp_path, monkeypatch):
    import json
    ck = _Clock()
    wi = we.fetch_whale_inputs(_BookBinance(_books(True, True, False)), "XUSDT", clock=ck.now, sleep=ck.sleep)
    sig = we.compute_signal(wi, WhaleTracker())
    det = sig.components["spoof_detail"]
    assert sig.spoof_suspect and len(det) == 1 and det[0]["side"] == "bid"
    assert sig.components["book_window_s"] == 10.0 and sig.components["book_samples_n"] == 3
    json.dumps(sig.components, default=str)
    stable = we.compute_signal(we.fetch_whale_inputs(_BookBinance(_books(True, True, True)), "XUSDT",
                                                     clock=_Clock().now, sleep=_Clock().sleep), WhaleTracker())
    assert stable.components["spoof_detail"] == []
    db = str(tmp_path / "w.db")
    we.record_metrics(db, "XUSDT", "compare", 1, sig, None, False, "x")
    raw = sqlite3.connect(db).execute("SELECT components_json FROM whale_metrics").fetchone()[0]
    assert json.loads(raw)["spoof_detail"][0]["dist_bps"] >= 0


def test_single_book_has_no_spoof_detail_claim():
    sig = we.compute_signal(_single(True), WhaleTracker())
    assert sig.components["spoof_detail"] == [] and sig.components["book_samples_n"] == 0
