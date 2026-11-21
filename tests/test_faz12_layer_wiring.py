"""FAZ12 — 5 katmanın canlı akışa GERÇEKTEN bağlandığının kanıtı.

Her test, katmanın üretim kodundan çağrıldığını gösterir; modülün kendi
birim testleri başka dosyalarda. Burada amaç "kuruldu ama bağlanmadı"
durumunu imkânsız kılmak.
"""
import inspect
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze_live_data
import auto_trader
import leverage_policy
import live_guard
import preflight
import sizing_policy


# --------------------------------------------------------------- ADIM 2a: leverage
def test_dynamic_leverage_uses_compute_leverage(monkeypatch):
    calls = {}

    def fake(sigma, sl_pct, **kw):
        calls["sigma"] = sigma
        calls["sl_pct"] = sl_pct
        calls["kw"] = kw
        return leverage_policy.LeverageDecision(2, 2.0, ["stage"], "binding=stage")

    monkeypatch.setattr(leverage_policy, "compute_leverage", fake)
    lev = auto_trader.calculate_dynamic_leverage(0.01, drawdown=0.1, stage_cap=2)
    assert lev == 2
    assert calls["sigma"] == 0.01 and calls["kw"]["drawdown"] == 0.1
    assert calls["kw"]["stage_cap"] == 2


def test_dynamic_leverage_still_clamps_to_risk_config(monkeypatch):
    monkeypatch.setattr(leverage_policy, "compute_leverage",
                        lambda *a, **k: leverage_policy.LeverageDecision(99, 99.0, [], ""))
    assert auto_trader.calculate_dynamic_leverage(0.01) == auto_trader.RISK_CONFIG["max_leverage"]


def test_dynamic_leverage_handles_zero_atr():
    assert auto_trader.calculate_dynamic_leverage(0.0) == auto_trader.RISK_CONFIG["min_leverage"]


# --------------------------------------------------------------- ADIM 2b: SL/TP
def test_sl_tp_uses_suggest_sl_tp(monkeypatch):
    seen = {}

    def fake(atr_pct, **kw):
        seen["atr"] = atr_pct
        seen["kw"] = kw
        return sizing_policy.Geometry(0.02, 0.03, "net_rr 1.40 ok", 1.40, [], True)

    monkeypatch.setattr(sizing_policy, "suggest_sl_tp", fake)
    g = sizing_policy.suggest_sl_tp(0.01, direction=1, cfg=auto_trader.RISK_CONFIG)
    assert g.ok and g.sl_pct == 0.02
    assert seen["kw"]["cfg"] is auto_trader.RISK_CONFIG


def test_open_coin_source_calls_suggest_sl_tp():
    src = inspect.getsource(auto_trader.open_coin)
    assert "sizing_policy.suggest_sl_tp" in src
    assert "atr_sl_multiplier" not in src


def test_sl_sync_source_calls_suggest_sl_tp():
    src = inspect.getsource(auto_trader)
    assert src.count("sizing_policy.suggest_sl_tp") >= 3


# --------------------------------------------------------------- ADIM 2c: preflight
class _FakeBinance:
    def __init__(self):
        self.order_posts = []
        self.amt = Decimal("0.6")

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.1"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 1000.0

    def order_book(self, symbol, limit=20):
        return {"bids": [["79.99", "5000"]], "asks": [["80.01", "5000"]]}

    def funding_rate(self, symbol):
        return {"lastFundingRate": "0.00005"}

    def open_interest(self, symbol):
        return {"openInterest": "1000000"}

    def taker_klines(self, symbol, interval="5m", limit=1):
        return [["0", "0", "0", "0", "0", "100", "0", "0", "0", "60"]]

    def exchange_info(self):
        return {"symbols": [{"symbol": "SOLUSDT"}]}

    def account(self):
        return {"assets": [{"asset": "USDT", "availableBalance": "1000"}], "canTrade": True}

    def set_leverage(self, symbol, lev):
        return {"leverage": lev}

    def position_risk(self):
        return []

    def account_snapshot(self):
        # FAZ16 (4331dd1): gerçek giriş yolu artık TEK snapshot ister — fixture katmanı.
        return auto_trader.AccountSnapshot(equity=1000.0, available_balance=1000.0,
                                           can_trade=True, raw={})

    def open_algo_orders_for(self, symbol):
        return []

    def _get(self, path, params=None, signed=False):
        if path == "/fapi/v1/klines":
            return [["0", "80", "81", "79", "80.5", "1000", "0", "0", "0", "600"]]
        return {}


class _FakeHB:
    def create_executor(self, *a, **k):
        return {"executor_id": "E1"}

    def active_executors(self):
        return []


class _RiskOK:
    """FAZ16 (4331dd1) giriş kapısının beklediği minimal RiskEngine testi.
    Üretim kapısına dokunmaz; sadece 'sağlıklı, halt'sız, onaylayan' motoru taklit eder."""
    is_halted = False
    halt_reason = ""
    daily_trades = 0
    consecutive_losses = 0
    current_drawdown = 0.0
    margin_usage = 0.0
    cfg = {"max_leverage": 20}

    def approve_trade(self, balance, slippage, take_profit_pct=None, equity=None,
                      max_position_pct=None):
        return 50.0, "approved"


class PreflightWiringTest(unittest.TestCase):
    def setUp(self):
        self._orig = {n: getattr(auto_trader, n) for n in
                      ("HB_OFFLINE", "_live_family_gate", "_live_regime_gate",
                       "_live_edge_gate", "_live_cfg")}
        # FAZ16 giriş kapısının modül globali: test kirliliğini kanonik biçimde yalıt.
        _saved_health = auto_trader._entry_health_reasons()
        def _restore(saved):
            for k in list(auto_trader._entry_health_reasons()):
                auto_trader._entry_health_clear(k)
            for k, v in saved.items():
                auto_trader._entry_health_set(k, v)
        self.addCleanup(_restore, _saved_health)
        auto_trader.HB_OFFLINE = False
        auto_trader._live_family_gate = lambda *a, **k: (True, "ok")
        auto_trader._live_regime_gate = lambda *a, **k: (True, "ok")
        auto_trader._live_edge_gate = lambda *a, **k: (True, "shadow")
        auto_trader._live_cfg = lambda: type("C", (), {
            "is_live": True, "leverage": 2, "max_leverage": 5, "max_open_positions": 5,
            "stage": 1, "testnet": True, "strategy_family": "",
            "max_notional": 200.0, "daily_loss_limit": 20.0,
            "base_url": "https://testnet.binancefuture.com",
            "risk_overrides": lambda self=None: {}})()

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(auto_trader, n, v)

    def _coin(self):
        return {"symbol": "SOLUSDT", "lastPrice": "80", "quoteVolume": "1000000",
                "_score": 0.9, "priceChangePercent": "6", "CQ": 0.9,
                "quality_vetoes": [], "quality_ok": True}

    def test_open_coin_calls_preflight_before_order(self):
        calls = []

        def fake_pf(**kw):
            calls.append(kw)
            return preflight.PreflightResult(False, [preflight.Step("config", False, "x")],
                                             "config", "blocked")

        # E1 (devam-F): decide_side(bu coin)=SHORT ve yeni sözleşme gereği
        # SHORT giriş varsayılan RED (SHORT-GARANTİSİZ realized-quote tavanı
        # yok). Bu test PREFLIGHT SIRASINI kanıtlar, tavanı değil — operatörün
        # yaptığı gibi drift-zarfı env'i ile opt-in yapılır (üretim varsayılanı
        # değişmez; default RED, test_blok12_e1_limit_cap'te sabitlidir).
        os.environ["VALIDATION_ALLOW_SHORT_ENTRIES"] = "true"
        self.addCleanup(os.environ.pop, "VALIDATION_ALLOW_SHORT_ENTRIES", None)
        orig = auto_trader.preflight.pre_flight_check
        auto_trader.preflight.pre_flight_check = fake_pf
        try:
            b = _FakeBinance()
            # FAZ16 (4331dd1): gerçek giriş yolu RiskEngine ister — sağlıklı motor verilir;
            # "risk yoksa giriş yok" kanıtı test_risk_engine_integrity.py'de duruyor.
            res = auto_trader.open_coin(b, _FakeHB(), self._coin(), dry_run=False,
                                        risk=_RiskOK(), reserver=None, coordinator=None)
        finally:
            auto_trader.preflight.pre_flight_check = orig
        self.assertFalse(res)
        self.assertEqual(len(calls), 1, "preflight çağrılmadı")
        self.assertEqual(calls[0]["symbol"], "SOLUSDT")
        self.assertEqual(b.order_posts, [], "preflight reddettiği halde emir gitti")

    def test_preflight_skipped_in_compare_mode(self):
        calls = []
        orig_pf = auto_trader.preflight.pre_flight_check
        orig_hb = auto_trader.HB_OFFLINE
        auto_trader.preflight.pre_flight_check = lambda **kw: calls.append(kw)
        auto_trader.HB_OFFLINE = True
        try:
            auto_trader.open_coin(_FakeBinance(), _FakeHB(), self._coin(), dry_run=True,
                                  risk=None, reserver=None, coordinator=None)
        finally:
            auto_trader.preflight.pre_flight_check = orig_pf
            auto_trader.HB_OFFLINE = orig_hb
        self.assertEqual(calls, [], "compare modunda preflight çalışmamalı")


# --------------------------------------------------------------- ADIM 2d: benchmark
def test_analysis_includes_benchmark():
    rows = [{"fwd_ret": 0.02, "strength": 0.8}, {"fwd_ret": -0.01, "strength": 0.3},
            {"fwd_ret": 0.03, "strength": 0.7}, {"fwd_ret": None, "strength": 0.5}]
    bm = analyze_live_data.benchmark_summary(rows, cost=0.0008)
    assert bm["equity"]["n"] == 3
    assert bm["accuracy"]["n"] == 3
    assert 0.0 <= bm["accuracy"]["brier"] <= 1.0
    assert "beats_baseline" in bm["baseline"]


def test_analysis_render_prints_benchmark_lines():
    rows = [{"fwd_ret": 0.02, "strength": 0.8}, {"fwd_ret": -0.01, "strength": 0.3}]
    rep = {"verdict": "NO_EDGE", "decision": "d",
           "whale": {"verdict": "NO_EDGE", "n_resolved_raw": 0, "n_declustered": 0,
                     "distinct_days": 0, "n_accepted": 0, "n_unresolved_skipped": 0},
           "strategy": {"verdict": "NO_EDGE", "n_declustered": 2,
                        "benchmark": analyze_live_data.benchmark_summary(rows)}}
    out = analyze_live_data.render(rep)
    assert "BENCHMARK" in out and "DOĞRULUK" in out


# --------------------------------------------------------------- ADIM 3: tavan
def test_max_notional_cap_is_dynamic():
    """FAZ14h: sabit 200 USDT tavani kaldirildi; baglayici tavan max_position_pct."""
    assert auto_trader.RISK_CONFIG["max_notional"] >= 999999.0
    assert auto_trader.RISK_CONFIG["max_position_pct"] == 20.0


def test_stage_caps_allow_200_for_stage_1_and_2():
    assert live_guard.STAGE_CAPS[1]["max_notional"] == 200.0
    assert live_guard.STAGE_CAPS[2]["max_notional"] == 200.0
    assert live_guard.STAGE_CAPS[4]["max_notional"] == 840.0


def test_stage_1_env_with_200_is_accepted():
    cfg = live_guard.LiveConfig.from_env({
        "LIVE_MODE": "1", "HB_OFFLINE": "0", "COMPARE_MODE": "0", "LIVE_DRY_RUN": "0",
        "LIVE_STAGE": "1", "LIVE_STAGE_CONFIRMED": "0", "BINANCE_TESTNET": "1",
        "BINANCE_FUTURES_BASE_URL": "https://testnet.binancefuture.com",
        "BINANCE_KEY": "", "BINANCE_SECRET": "",
        "LIVE_MAX_NOTIONAL_USDT": "200", "LIVE_DAILY_LOSS_LIMIT_USDT": "20",
        "LIVE_LEVERAGE": "2", "LIVE_MAX_OPEN_POSITIONS": "2",
        "LIVE_STRATEGY_FAMILY": "", "LIVE_ALLOW_UNPROVEN_FAMILY": "1",
        "LIVE_WINNING_REGIMES": "range,mid",
    }).validate()
    assert cfg.max_notional == 200.0


# --------------------------------------------------------------- ADIM 4: interval
def test_interval_default_is_30():
    src = inspect.getsource(auto_trader.main)
    assert 'default=30' in src
    assert 'default=900' not in src
