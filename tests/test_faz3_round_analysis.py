"""test_faz3_round_analysis.py — M-1/M-3 sözleşmeleri (sentetik veri).

Kilitler: (1) tur = pozisyon-delta kümesi, "fill/2" DEĞİL; (2) sıfır-geçişi
notional payı; (3) yürüyüş testlenmiş builder ile birebir; (4) bootstrap
deterministik (seed) ve sembol-kümeli; (5) funding alias şeması.
"""
import faz3_round_analysis as RA


def _f(sid, side, qty, px, rp="0", com="0", ts=1):
    return {"symbol": "XUSDT", "id": sid, "orderId": 1000 + sid, "side": side,
            "price": str(px), "qty": str(qty), "realizedPnl": rp,
            "commission": com, "time": str(1794902418791 + ts)}


class TestRoundMatching:
    def test_two_fills_one_round(self):
        rounds, tails = RA.walk_rounds([_f(1, "BUY", 1, 100, com="0.05"),
                                        _f(2, "SELL", 1, 110, rp="10",
                                           com="0.05")])
        assert len(rounds) == 1 and not tails
        r = rounds[0]
        assert r["direction"] == "LONG"
        assert abs(r["entry_notional"] - 100) < 1e-9
        assert abs(r["exit_notional"] - 110) < 1e-9
        assert abs(r["net"] - (10 - 0.10)) < 1e-9

    def test_zero_crossing_splits_notional(self):
        # 1 LONG açıkken 3 SELL: 1 kapanır (exit), 2 SHORT açar (entry=2×px)
        rounds, tails = RA.walk_rounds([
            _f(1, "BUY", 1, 100), _f(2, "SELL", 3, 110, rp="10"),
            _f(3, "BUY", 2, 105, rp="10")])
        assert len(rounds) == 2 and not tails
        r1, r2 = rounds
        assert abs(r1["entry_notional"] - 100) < 1e-9
        assert abs(r1["exit_notional"] - 110) < 1e-9      # yalnız kapanan kısım
        assert r2["direction"] == "SHORT"
        assert abs(r2["entry_notional"] - 220) < 1e-9     # 2×110
        assert abs(r2["exit_notional"] - 210) < 1e-9

    def test_open_tail_reported_not_rounded(self):
        rounds, tails = RA.walk_rounds([_f(1, "BUY", 1, 100, com="0.05")])
        assert not rounds and len(tails) == 1
        assert abs(tails[0]["unclosed_commission"] - 0.05) < 1e-12

    def test_cross_check_with_tested_builder(self):
        import exchange_ledger as EL
        fills = [_f(1, "BUY", 1, 100), _f(2, "SELL", 3, 110, rp="10"),
                 _f(3, "BUY", 2, 105, rp="10"),
                 {"symbol": "YUSDT", "id": 9, "orderId": 9, "side": "BUY",
                  "price": "5", "qty": "2", "realizedPnl": "0",
                  "commission": "0", "time": "1794902418999"}]
        rounds, _ = RA.walk_rounds(fills)
        ok, a, b = RA.cross_check(fills, rounds)
        assert ok, (a, b)

    def test_funding_alias_schema(self):
        import exchange_ledger as EL
        rounds, _ = RA.walk_rounds([_f(1, "BUY", 1, 100),
                                    _f(2, "SELL", 1, 110, rp="10")])
        inc = [{"symbol": "XUSDT", "incomeType": "FUNDING_FEE",
                "income": "-1.0", "time": "1794902418792"}]   # tutma penceresi içinde
        un = EL.assign_funding(rounds, inc)
        assert not un and abs(rounds[0]["funding"] + 1.0) < 1e-12


class TestBootstrap:
    def _rounds(self):
        rs = []
        for sym, vals in (("AUSDT", [-0.01, -0.02, 0.005]),
                          ("BUSDT", [0.01]), ("CUSDT", [-0.05])):
            for v in vals:
                rs.append({"symbol": sym, "net": v * 100,
                           "entry_notional": 100.0})
        return rs

    def test_deterministic_seed(self):
        a = RA.cluster_bootstrap(self._rounds(), b=500, seed=7)
        b = RA.cluster_bootstrap(self._rounds(), b=500, seed=7)
        assert a[:2] == b[:2]

    def test_kish_neff_le_n(self):
        lo, hi, neff, k = RA.cluster_bootstrap(self._rounds(), b=500, seed=7)
        assert k == 3 and 0 < neff <= 5
        assert lo <= hi
