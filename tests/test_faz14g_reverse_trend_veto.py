"""FAZ14g — ters sinyalin trend vetosu TERS yöne göre hesaplanmalı.

Kök neden: `resolve_side` beraberlikte (net == 0) orijinal yönü döndürüyor ama
`reversed_names` dolu kalıyordu. Sinyal "ters" etiketleniyor, veto ise ORİJİNAL
yönde hesaplanıyordu: BTCUSDT'de against(orijinal)=0.600 → RED, oysa
against(ters)=0.000. Etiket ile karar aynı yönü göstermek zorunda.
"""
import strategy_engine as se

R = se.SignalReading

# 4h ve 1h trend aşağı (score=-1) → LONG'a karşı 1.000 (veto eşiği 0.70), SHORT'a karşı 0.000
TRENDS_DOWN = [
    {"tf": "1h", "score": -1.0, "adx": 26.0, "adx_strength": 0.44},
    {"tf": "4h", "score": -1.0, "adx": 64.0, "adx_strength": 1.0},
]
POLICY = {("wick_rejection", "trend"): "reverse"}


def _p_fake(name, regime):
    """wick_rejection/trend kalibre posterioru: p_fake=0.725 (canlı log ile aynı)."""
    if name == "wick_rejection" and regime == "trend":
        return 0.725, True
    return 0.5, False


def _readings_tie():
    """Ters çevrilecek LONG wick_rejection + çevrilmeyen LONG rsi_extreme → oy beraberliği."""
    return [R("structure", "wick_rejection", 1, 0.5, "15m"),
            R("exhaustion", "rsi_extreme", 1, 0.5, "15m")]


def test_reverse_signal_uses_reversed_trend_against():
    """Ters sinyalde trend_against TERS yönün değeri olmalı (0.000), orijinalin değil (0.600)."""
    v = se.evaluate(1, _readings_tie(), TRENDS_DOWN, "trend", _p_fake, None, policy=POLICY)
    assert v.effective_side == -1, f"ters sinyalin nihai yönü -1 olmalı, {v.effective_side} geldi"
    assert v.reversed_signals == ("reverse_wick_rejection",), v.reversed_signals
    assert abs(v.trend_against - 0.0) < 1e-9, (
        f"ters yönün trend_against değeri 0.000 olmalı, {v.trend_against:.3f} geldi "
        f"(orijinal yönün değeri kullanılıyor)")


def test_reverse_signal_passes_when_original_was_vetoed():
    """Orijinal yön vetoya takılırken ters yön GEÇMELİ."""
    orig = se.evaluate(1, _readings_tie(), TRENDS_DOWN, "trend", _p_fake, None, policy=None)
    assert orig.veto is True, "orijinal yön veto edilmeliydi (against=1.000 >= 0.70)"
    assert orig.allow is False

    rev = se.evaluate(1, _readings_tie(), TRENDS_DOWN, "trend", _p_fake, None, policy=POLICY)
    assert rev.veto is False, f"ters yön veto edilmemeliydi (against={rev.trend_against:.3f})"
    assert rev.allow is True, f"ters yön GEÇER olmalıydı, RED: {rev.reason}"
    assert rev.p_real >= 0.65, f"p_real 0.65 üstü olmalı, {rev.p_real:.3f}"


def test_trend_veto_respects_reversed_direction():
    """Veto kararı nihai (ters) yöne göre verilmeli; ADX trend gücü yön bağımsız kalmalı."""
    rev = se.evaluate(1, _readings_tie(), TRENDS_DOWN, "trend", _p_fake, None, policy=POLICY)
    orig = se.evaluate(1, _readings_tie(), TRENDS_DOWN, "trend", _p_fake, None, policy=None)
    assert rev.veto != orig.veto, "ters ve orijinal yön aynı veto kararını vermemeli"
    assert rev.trend_adx == orig.trend_adx == 64.0, "ADX trend gücü yön bağımsız olmalı"
    assert rev.trend_against < orig.trend_against, (
        f"ters yönün karşıtlığı daha düşük olmalı: {rev.trend_against:.3f} < {orig.trend_against:.3f}")


def test_tie_honours_policy_but_no_policy_keeps_side():
    """Politika yokken beraberlik yönü değiştirmemeli (regresyon koruması)."""
    eff, rev = se.resolve_side(1, _readings_tie(), "trend", None, None)
    assert (eff, rev) == (1, ()), (eff, rev)
    eff2, rev2 = se.resolve_side(1, _readings_tie(), "trend", POLICY, None)
    assert (eff2, rev2) == (-1, ("reverse_wick_rejection",)), (eff2, rev2)
