"""test_faz5_harness.py — B.4 harness kontrolleri (SADECE sentetik veri).

(i) düz rastgele yürüyüş → gross≈0, net≈−maliyet; (ii) enjekte momentum →
takipçi kol, enjekte mean-reversion → ters kol doğru işareti geri vermeli;
bariyer aynalama geometrisi; yok-barsonrası giriş (lookahead yok); kötümser
senaryo slippage'i her iki bacakta aleyhe uygular (model etiketi ayrı).
"""
import math
import random

import faz5_harness as H


def _flat_walk(n, t0=1_790_000_000, step=60.0, p0=100.0):
    return [(t0 + i * step, p0, p0, p0, p0) for i in range(n)]


def _apply(bars, path):
    out = []
    for b, p in zip(bars, path):
        out.append((b[0], p, p, p, p))     # o=h=l=c=p (bariyer belirlenimi)
    return out


def _rw_bars(n, seed):
    rng = random.Random(seed)
    p, out, t = 100.0, [], 1_790_000_000
    for i in range(n):
        p *= (1 + rng.gauss(0, 0.002))
        out.append((t + i * 60.0, p, p * 1.0005, p * 0.9995, p))
    return out


class TestRandomWalk:
    def test_gross_zero_net_minus_cost(self):
        bars = _rw_bars(6000, seed=11)
        sigs = [(bars[i][0], 1.0, 10, 0.20, 0.20) for i in range(0, 5900, 30)]
        r = H.run_pair(bars, sigs, 0.0005, 0.0)
        n = r["long"][2]
        for k in ("long", "short"):
            g = r[k][0] / n                      # tur-başı gross (RW'de ≈0)
            c = 0.0005 * 2 * 100.0               # ~2 bacak × fee × tipik px
            assert abs(g) < 3.0                  # 200 tur, σ~0.6$ → geniş tol.
            assert abs(r[k][1] / n + (abs(g) * 0 + c)) < 3.0   # net ≈ −maliyet

    def test_net_not_positive_without_edge(self):
        bars = _rw_bars(6000, seed=12)
        sigs = [(bars[i][0], 1.0, 10, 0.20, 0.20) for i in range(0, 5900, 30)]
        r = H.run_pair(bars, sigs, 0.0005, 0.0)
        assert r["long"][1] < 0 and r["short"][1] < 0   # maliyet yutar


class TestSignRecovery:
    def test_momentum_follower_wins(self):
        path = [100.0] * 50
        for i in range(50, 60):
            path.append(path[-1] * 1.01)          # sinyal sonrası yüksel
        bars = _apply(_flat_walk(60), path)
        sigs = [(bars[49][0], 1.0, 10, 0.50, 0.50)]
        r = H.run_pair(bars, sigs, 0.0005, 0.0)
        assert r["long"][0] > 0 > r["short"][0]   # takipçi LONG doğru

    def test_mean_reversion_reversal_wins(self):
        path = [100.0] * 50
        path.append(110.0)                        # spike
        for i in range(51, 60):
            path.append(path[-1] * 0.99)          # geri düşüş
        bars = _apply(_flat_walk(60), path)
        sigs = [(bars[50][0], 1.0, 9, 0.50, 0.50)]
        r = H.run_pair(bars, sigs, 0.0005, 0.0)
        assert r["short"][0] > 0 > r["long"][0]   # ters SHORT doğru


class TestMechanics:
    def test_no_lookahead_entry_next_bar_open(self):
        bars = _apply(_flat_walk(10), [100.0] * 10)
        bars[6] = (bars[6][0], 105.0, 105.0, 105.0, 105.0)   # sinyal barı
        bars[7] = (bars[7][0], 106.0, 106.0, 106.0, 106.0)   # giriş burada
        g, net, why = H.run_round(bars, (bars[6][0], 1.0, 1, 0.5, 0.5), 1,
                                  0.0, 0.0)
        assert why == "HOLD" and abs(g - 0.0) < 1e-9   # 106→106 (hold=1)

    def test_sl_before_tp_same_bar_conservative(self):
        bars = _apply(_flat_walk(10), [100.0] * 10)
        bars[6] = (bars[6][0], 100.0, 106.0, 94.0, 100.0)  # TP ve SL aynı bar
        g, _, why = H.run_round(bars, (bars[5][0], 1.0, 3, 0.05, 0.05), 1,
                                0.0, 0.0)
        assert why == "SL" and abs(g + 5.0) < 1e-9

    def test_pessimist_slippage_both_legs(self):
        bars = _apply(_flat_walk(10), [100.0] * 10)
        sig = (bars[0][0], 1.0, 3, 0.5, 0.5)
        g0, n0, _ = H.run_round(bars, sig, 1, 0.0005, 0.0)
        g1, n1, _ = H.run_round(bars, sig, 1, 0.0005, 25.0)
        assert n1 < n0 and g1 < g0                # iki bacakta da aleyhe
        assert H.PESS_MODEL_ID == "cm-v1-pess-v1"

    def test_mirror_geometry_symmetric(self):
        up = _apply(_flat_walk(20), [100.0] * 5 +
                    [100.0 * (1.02 ** i) for i in range(1, 16)] + [130.0, 130.0])
        sig = (up[4][0], 1.0, 10, 0.05, 0.05)   # eşit mesafe: ayna aynı seviye
        gl, _, wl = H.run_round(up, sig, 1, 0.0, 0.0)
        gs, _, ws = H.run_round(up, sig, -1, 0.0, 0.0)
        assert wl == "TP" and ws == "SL" and abs(gl + gs) < 1e-9
