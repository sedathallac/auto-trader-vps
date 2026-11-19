"""test_faz3b_universe_recheck.py — BLOK3 İŞ A sözleşmeleri (sentetik).

Kilitler: A.1 pencere/roundlama; A.3 İŞARET KONVANSİYONU (LONG yükselen>0,
SHORT yükselen<0, SHORT düşen>0 — SOLUSDT tutarsızlığının kök testi);
A.2 Δ = exit_gap+entry_gap+resid özdeşliği sentetik turda; A.4
term1+term2 ≡ Σ s·N·r; permütasyon gün-başı işaret sayısını korur;
A.6 gate'leri (b)/(a)/(c)/(d) + öncelik.
"""
import os

import faz3b_universe_recheck as RB

T0 = 1_794_902_400.0          # 2026-11-18 civarı rastgele gün sınırlı saniye
BAR = 60.0


def _bars(prices, t0=T0):
    ts = [t0 + i * BAR for i in range(len(prices))]
    return ts, [p for p in prices], [p * 1.005 for p in prices]  # close=open*1.005


class TestPartOrdering:
    def test_part10_not_before_part2(self, tmp_path, monkeypatch):
        monkeypatch.setattr(RB, "KL1", str(tmp_path))
        d = tmp_path / "SUSDT" / "1m"
        d.mkdir(parents=True)
        hdr = "open_time,open,high,low,close,volume,close_time,x,x,x,x,x"

        def page(name, t_sec, px):
            rows = [hdr]
            for k in range(3):
                ot = int((t_sec + k * BAR) * 1000)
                rows.append(f"{ot},{px+k},1,1,{px+k},1,{ot},0,1,0,0,0")
            (d / name).write_text("\n".join(rows) + "\n")
        page("part2.csv", T0, 10.0)          # kronolojik olarak ÖNCE
        page("part10.csv", T0 + 300, 20.0)   # sözlüksel olarak önce → OLMAZ
        ts, o, c = RB.load_bars_1m("SUSDT")
        assert ts == sorted(ts) and o[0] == 10.0 and o[3] == 20.0


class TestWindowAndSignConvention:
    def test_long_uyen_r_pozitif(self):
        bars = _bars([100.0, 101.0, 102.0, 103.0])
        o0, c1, r, ok = RB.window_edges(bars, T0 + 5, T0 + 3 * BAR + 30)
        assert ok and abs(o0 - 100.0) < 1e-12
        assert abs(c1 - 103.0 * 1.005) < 1e-12
        assert (1.0) * r > 0                      # LONG × yükselen → +

    def test_short_yukselen_negatif_dusen_pozitif(self):
        up = _bars([100.0, 101.0, 102.0])
        dn = _bars([103.0, 102.0, 101.0])
        _, _, r_up, _ = RB.window_edges(up, T0 + 5, T0 + BAR + 5)
        _, _, r_dn, _ = RB.window_edges(dn, T0 + 5, T0 + BAR + 5)
        assert (-1.0) * r_up < 0                  # SHORT × yükselen → −
        assert (-1.0) * r_dn > 0                  # SHORT × düşen → +

    def test_out_of_range(self):
        bars = _bars([100.0, 101.0])
        assert RB.window_edges(bars, -1e9, T0)[3] is False


def _write_kl(tmp_path, monkeypatch, sym, opens):
    d = tmp_path / sym / "1m"
    d.mkdir(parents=True)
    hdr = "open_time,open,high,low,close,volume,close_time,x,x,x,x,x"
    lines = [hdr]
    for i, o in enumerate(opens):
        ot = int((T0 + i * BAR) * 1000)
        lines.append(f"{ot},{o},{o*1.01},{o*0.99},{o*1.005},100,{ot+59999},0,1,0,0,0")
    (d / "part1.csv").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(RB, "KL1", str(tmp_path))


def _fill(sid, side, qty, px, rp="0", ts=0):
    return {"symbol": "TESTUSDT", "id": sid, "orderId": 1000 + sid,
            "side": side, "price": str(px), "qty": str(qty),
            "realizedPnl": rp, "commission": "0",
            "time": str(int((T0 + 10 + ts) * 1000))}


class TestDeltaIdentity:
    def test_decomposition_exact(self, tmp_path, monkeypatch):
        _write_kl(tmp_path, monkeypatch, "TESTUSDT", [100.0, 101.0, 102.0])
        rounds = RB.walk_rounds_priced(
            [_fill(1, "BUY", 1, 100.5, ts=5),
             _fill(2, "SELL", 1, 102.2, rp="1.7", ts=125)])
        assert len(rounds) == 1
        rounds, cov, den = RB.build_round_data(rounds, {})
        assert rounds is not None and cov == den
        r = rounds[0]
        assert abs(r["exit_gap"] + r["entry_gap"] + r["resid"] - r["delta"]) < 1e-9
        assert abs(r["delta"] - (r["gross"] - r["recon"])) < 1e-12
        assert r["n_fills"] == 2 and "çok-fill" not in RB.classify(r)
        # 3 fill (pyramid) → çok-fill etiketi
        rounds2 = RB.walk_rounds_priced(
            [_fill(1, "BUY", 1, 100.5, ts=5), _fill(2, "BUY", 1, 101.2, ts=65),
             _fill(3, "SELL", 2, 102.4, rp="1.9", ts=125)])
        rounds2, _, _ = RB.build_round_data(rounds2, {})
        assert rounds2[0]["n_fills"] == 3
        assert "çok-fill" in RB.classify(rounds2[0])
        assert abs(rounds2[0]["exit_gap"] + rounds2[0]["entry_gap"] +
                   rounds2[0]["resid"] - rounds2[0]["delta"]) < 1e-9


class TestDecompAndPerm:
    def _rs(self):
        mk = lambda s, N, r, day: dict(s=s, entry_notional=N, r=r, day=day,
                                       delta=0.0, symbol="X")
        return [mk(1.0, 100, 0.01, 1), mk(-1.0, 200, 0.03, 1),
                mk(1.0, 50, -0.02, 2)]

    def test_identity(self):
        t1, t2 = RB.decomp_terms(self._rs())
        tgt = sum(r["s"] * r["entry_notional"] * r["r"] for r in self._rs())
        assert abs(t1 + t2 - tgt) < 1e-12

    def test_perm_preserves_day_sign_counts(self):
        import random
        rs = self._rs()
        rng = random.Random(3)
        s2 = RB.perm_signs_within_day(rs, rng)
        for day in (1, 2):
            obs = sorted(r["s"] for r in rs if r["day"] == day)
            new = sorted(s2[id(r)] for r in rs if r["day"] == day)
            assert obs == new                      # gün-başı çoklu küme aynı


class TestVerdictGates:
    def test_b_requires_all_four(self):
        assert RB.verdict(-9, -8.0, -1.0, (-12.0, -1.0), (-4, 3),
                          0.001, -5.0).startswith("(b)")
        # p eşiği aşılırsa (b) bozulur → başka gate de yoksa (d)
        assert RB.verdict(-9, -8.0, -1.0, (-12.0, -1.0), (-4, 3),
                          0.20, -5.0).startswith("(d)")
        # SOL-çıkık ≥ 0 ise (b) bozulur
        assert RB.verdict(-9, -8.0, -1.0, (-12.0, -1.0), (-4, 3),
                          0.001, +0.5).startswith("(d)")

    def test_a_gate(self):
        assert RB.verdict(-20.0, -1.0, 0.5, (-5.0, 4.0), (-2, 2),
                          0.40, -1.0).startswith("(a)")

    def test_c_gate(self):
        assert RB.verdict(1.0, 0.5, -9.0, (-3, 3), (-11.0, -5.0),
                          0.5, 0.5).startswith("(c)")

    def test_d_weak(self):
        assert RB.verdict(-1.0, -0.5, -0.2, (-4, 4), (-3, 3),
                          0.3, -0.5).startswith("(d)")
