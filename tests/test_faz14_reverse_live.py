"""FAZ14 — ters politika CANLIDA ve vetodan ÖNCE.

Kök neden (yeniden üretildi): `evaluate()` trend vetosunu `side_dir` ile hesaplıyordu,
politika ise sonra uygulanıyordu. `wick_rejection/trend` hücresinde sinyal asıl yönüyle
vetoya takılıyor, `reversed_signals` boş kalıyor ve ters yön hiç denenmiyordu.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
import strategy_engine as se

# Canlı logdaki gerçek durum: rejim=trend, ADX_trend=64, ADX_15m=28
TRENDS = [{"tf": "1h", "score": -1.0, "adx": 64.0, "adx_strength": 1.0},
          {"tf": "4h", "score": -1.0, "adx": 60.0, "adx_strength": 1.0}]
PF = lambda n, g: (0.28, True)
PF_HIGH = lambda n, g: (0.725, True)   # asıl yön çoğunlukla sahte → ters yön kanıtlı
# Tek sinyal testleri: aile sayısı kapısı ayrı test edilir, burada yön çevirme ölçülür.
ONE_FAM = dict(se.CONFIRM_DEFAULTS, confirm_min_families=1)


def _wick():
    return [se.SignalReading(se.FAM_STRUCTURE, "wick_rejection", 1, 0.79)]


def _rsi_div():
    return [se.SignalReading(se.FAM_EXHAUSTION, "rsi_divergence", 1, 0.70)]


def test_reverse_policy_flips_wick_rejection_in_live():
    v = se.evaluate(1, _wick(), TRENDS, "trend", PF_HIGH, ONE_FAM,
                    regime_adx=28.0, policy=se.REVERSE_POLICY)
    assert v.effective_side == -1, "wick_rejection/trend ters çevrilmedi"
    assert "reverse_wick_rejection" in v.reversed_signals
    assert v.veto is False and v.allow is True


def test_reverse_policy_flips_rsi_divergence_in_live():
    v = se.evaluate(1, _rsi_div(), TRENDS, "trend", PF_HIGH, ONE_FAM,
                    regime_adx=28.0, policy=se.REVERSE_POLICY)
    assert v.effective_side == -1, "rsi_divergence/trend ters çevrilmedi"
    assert "reverse_rsi_divergence" in v.reversed_signals
    assert v.veto is False and v.allow is True


def test_reverse_policy_applied_before_trend_veto():
    """Veto nihai yöne göre hesaplanır: ters yön trendle aynı yönde olduğu için against=0."""
    v = se.evaluate(1, _wick(), TRENDS, "trend", PF, se.CONFIRM_DEFAULTS,
                    regime_adx=28.0, policy=se.REVERSE_POLICY)
    assert v.trend_against == pytest.approx(0.0), "veto hâlâ asıl yöne bakıyor"
    # Politika yoksa eski davranış korunur: veto asıl yöne karşı çalışır.
    v0 = se.evaluate(1, _wick(), TRENDS, "trend", PF, se.CONFIRM_DEFAULTS,
                     regime_adx=28.0, policy=None)
    assert v0.veto is True and v0.trend_against == pytest.approx(1.0)
    assert v0.reversed_signals == ()


def test_sr_proximity_range_not_flipped():
    readings = [se.SignalReading(se.FAM_STRUCTURE, "sr_proximity", 1, 0.8)]
    flat = [{"tf": "1h", "score": 0.0, "adx": 18.0, "adx_strength": 0.1},
            {"tf": "4h", "score": 0.0, "adx": 16.0, "adx_strength": 0.1}]
    v = se.evaluate(1, readings, flat, "range", PF, ONE_FAM,
                    regime_adx=18.0, policy=se.REVERSE_POLICY)
    assert v.effective_side == 1 and v.reversed_signals == ()
    assert v.allow is True


def test_disabled_strategies_never_fire_in_live():
    for name, fam in (("rsi_extreme", se.FAM_EXHAUSTION),
                      ("bollinger_z", se.FAM_EXHAUSTION),
                      ("sr_proximity", se.FAM_STRUCTURE)):
        readings = [se.SignalReading(fam, name, 1, 0.9)]
        v = se.evaluate(1, readings, TRENDS, "trend", PF, se.CONFIRM_DEFAULTS,
                        regime_adx=28.0, policy=se.REVERSE_POLICY)
        assert v.allow is False, f"{name}/trend devre dışı olmalıydı"
        assert v.effective_side in (1, -1)


def test_live_mode_passes_the_policy(monkeypatch):
    """Canlı ('on') modunda politika confirm()'e geçirilir; compare'da da geçirilir."""
    src = __import__("inspect").getsource(auto_trader._faz6_confirm)
    assert 'mode in ("compare", "on") and REVERSE_POLICY_ENABLED' in src


def test_live_regime_gate_opens_for_reversed_signals(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: type("C", (), {
        "is_live": True, "winning_regimes": frozenset({"range", "mid"})})())
    auto_trader._LAST_CONFIRM.clear()
    auto_trader._LAST_CONFIRM.update({"regime": "trend", "reversed": ("reverse_wick_rejection",)})
    ok, reason = auto_trader._live_regime_gate()
    assert ok is True and "reverse policy" in reason


def test_live_edge_gate_opens_for_reversed_signals(monkeypatch):
    monkeypatch.setattr(auto_trader, "_live_cfg", lambda: type("C", (), {"is_live": True})())
    auto_trader._LAST_CONFIRM.clear()
    auto_trader._LAST_CONFIRM.update({"regime": "trend", "families": {"structure": "wick_rejection"},
                                      "reversed": ("reverse_wick_rejection",)})
    ok, reason = auto_trader._live_edge_gate()
    assert ok is True and "backtest" in reason


def test_no_policy_reversal_never_overrides_decide_side():
    """Politika hiçbir sinyali çevirmediyse oy dengesizliği yönü EZMEMELİ."""
    readings = [se.SignalReading(se.FAM_STRUCTURE, "sr_proximity", -1, 0.9),
                se.SignalReading(se.FAM_EXHAUSTION, "volume_climax", -1, 0.8)]
    flat = [{"tf": "1h", "score": 0.0, "adx": 18.0, "adx_strength": 0.1},
            {"tf": "4h", "score": 0.0, "adx": 16.0, "adx_strength": 0.1}]
    v = se.evaluate(1, readings, flat, "range", PF, ONE_FAM,
                    regime_adx=18.0, policy=se.REVERSE_POLICY)
    assert v.effective_side == 1, "politika ters çevirme yapmadığı halde yön değişti"
    assert v.reversed_signals == ()


def test_reversed_signal_flips_its_fake_probability():
    """Ters sinyalde p_fake çevrilir: asıl yönün sahteliği ters yönün kanıtıdır."""
    # p_fake=0.725 (asıl yön sahte) → ters yön için 0.275 → LLR pozitif → p_real > 0.5
    v = se.evaluate(1, _wick() + _rsi_div(), TRENDS, "trend", lambda n, g: (0.725, True),
                    ONE_FAM, regime_adx=28.0, policy=se.REVERSE_POLICY)
    assert v.allow is True, f"ters sinyal p_real'i yükseltmedi: {v.p_real}"
    assert v.p_real > 0.8
    # Politika yoksa aynı p_fake p_real'i prior'ın ALTINA çeker (asıl yön sahte).
    v0 = se.evaluate(1, _wick() + _rsi_div(), TRENDS, "trend", lambda n, g: (0.725, True),
                     ONE_FAM, regime_adx=28.0, policy=None)
    assert v0.p_real < 0.5


def test_open_coin_applies_the_effective_side(monkeypatch):
    """Kapı GEÇER ve yön ters çevrildiyse emir ters yönde açılır."""
    src = __import__("inspect").getsource(auto_trader.open_coin)
    assert '_LAST_CONFIRM.get("effective_side")' in src
    assert "ters politika: yön" in src
