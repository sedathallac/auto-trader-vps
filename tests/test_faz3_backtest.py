"""test_faz3_backtest.py — FAZ3 replay omurgası testleri (SENTETİK veri).

Gerçek arşiv OKUNMAZ (audit alanı Git-dışı; testler repoya bağlı kalmalı).
Semantik sözleşmeler doğrulanır: giriş = sonraki bar AÇILIŞI, vade 16 bar,
maliyet yuvarlak-toplam, ≤3 eşzamanlı, sembol başına 1, determinizm.
"""
import datetime
import json
import math

import edge_engine
import faz3_backtest as FB


def _row(t, o, h, l, c, ct=None):
    """load_csv çıktısıyla BİREBİR aynı biçim: 13 eleman, 0=t_ms, 6=closeTime."""
    return [t, o, h, l, c, 10.0, ct if ct is not None else t + 1, 100.0, 5,
            5.0, 50.0, "0"]


def _series(n, t0, tf_ms, growth=0.0):
    rows = []
    c = 100.0
    for k in range(n):
        o = c
        c = o * (1.0 + growth)
        hi, lo = max(o, c) * 1.0005, min(o, c) * 0.9995
        rows.append(_row(t0 + k * tf_ms, o, hi, lo, c,
                         ct=t0 + (k + 1) * tf_ms - 1))
    return rows


def _symb(data15, data1h, data4h, sym="TESTUSDT"):
    sd = object.__new__(FB.SymbolData)
    sd.sym = sym
    sd.r15, sd.r1h, sd.r4h = data15, data1h, data4h
    sd.ct1h = [r[6] for r in data1h]
    sd.ct4h = [r[6] for r in data4h]
    return sd


T0 = 1704067200000  # 2024-01-01T00:00Z
M15, H1, H4 = 900_000, 3_600_000, 14_400_000


class TestLoadCsv:
    def test_header_and_us_timestamps(self, tmp_path):
        p = tmp_path / "x.csv"
        p.write_text(
            "open_time,open,high,low,close,volume,close_time,...\n"
            f"{T0 * 1000},1,2,0.5,1.5,10,{(T0 + M15) * 1000 - 1},1,1,1,1,1\n"
            f"{(T0 + M15) * 1000},1.5,2,1,1.8,10,{(T0 + 2 * M15) * 1000 - 1},1,1,1,1,1\n")
        rows = FB.load_csv(str(p), "2024-01", "2026-10")
        assert len(rows) == 2
        assert rows[0][0] == T0                      # µs → ms normalize
        assert rows[0][1] == 1.0 and rows[0][4] == 1.5

    def test_garbage_lines_skipped(self, tmp_path):
        p = tmp_path / "y.csv"
        p.write_text("bad\n\n1,x\n")
        assert FB.load_csv(str(p), "0000", "9999") == []


class TestWindow:
    def test_stub_parity_with_live_parse(self):
        r = _series(300, T0, M15)
        import strategy_engine as se
        w = FB.SymbolData.window(r, 299, 250)
        cd = se.parse_klines(w)                       # stub atılır
        assert len(cd.c) == 250                       # canlı _get(250) semantiği
        w2 = FB.SymbolData.window(r, 100, 250)
        assert len(se.parse_klines(w2).c) == 101      # ısıtma dönemi: mevcut hepsi


class TestScanDecisions:
    # trend_reading HER TF'de ≥210 çubuk ister → 4h için 250 çubuk = 41 gün,
    # 15m'de karşılığı ≈ 4000 bar (ısıtma + n4h>60 eşiği aşılır).
    BIG15, BIG1H, BIG4H = 4000, 1000, 250

    def _big(self, growth=0.0):
        return (_series(self.BIG15, T0, M15, growth),
                _series(self.BIG1H, T0, H1, growth),
                _series(self.BIG4H, T0, H4, growth))

    def test_scan_shape_and_no_lookahead(self, monkeypatch):
        import strategy_engine as se
        d15, d1h, d4h = self._big()
        calls = {"n": 0}
        real_eval = se.evaluate

        def counting(side, *a, **k):
            calls["n"] += 1
            return real_eval(side, *a, **k)

        monkeypatch.setattr(se, "evaluate", counting)
        sd = _symb(d15, d1h, d4h)
        dec = FB.scan_symbol_decisions(sd, {}, "2024-01", "2026-10")
        assert isinstance(dec, list)
        assert calls["n"] >= 4 * 50          # kapı mantığı GERÇEKTEN işletildi
        for x in dec[:50]:
            assert x["t"] == d15[x["i"] + 1][0]       # giriş zamanı = sonraki bar AÇILIŞI
            assert x["side"] in (1, -1)
            assert 0 <= x["fams"] <= 4
            assert x["atr_pct"] > 0
        # her işlenen bar hem long hem short kaydı üretir (karar-öncesi çift)
        by_i = {}
        for x in dec:
            by_i.setdefault(x["i"], set()).add(x["side"])
        assert all(v == {1, -1} for v in by_i.values())

    def test_window_filter_applied(self):
        d15 = _series(1200, T0, M15)
        sd = _symb(d15, _series(300, T0, H1), _series(75, T0, H4))
        assert FB.scan_symbol_decisions(sd, {}, "2030-01", "2030-12") == []

    def test_bar_exception_skipped_not_crash(self, monkeypatch):
        # PUMPUSDT kusuru: düz segmentte ATR=0 → structure_readings SIFIRA
        # BÖLÜNME. Canlı _faz6_confirm geniş try/except ile BARı atlar.
        # Guard'ın canlı-eşdeğeri burada patlatan okumayla doğrulanır.
        import strategy_engine as se
        calls = {"n": 0}
        real = se.structure_readings

        def boom(cd):
            calls["n"] += 1
            if calls["n"] % 7 == 0:            # her 7. bar'da patla
                raise ZeroDivisionError("float division by zero")
            return real(cd)

        monkeypatch.setattr(se, "structure_readings", boom)
        d15, d1h, d4h = self._big()
        sd = _symb(d15, d1h, d4h, sym="FLATUSDT")
        dec = FB.scan_symbol_decisions(sd, {}, "2024-01", "2026-10")
        assert isinstance(dec, list)                   # sembol ölmedi
        assert FB.SCAN_STATS["FLATUSDT"]["error_bars"] > 0


class TestSimulateTrades:
    def _up_symbol(self):
        d15 = _series(400, T0, M15, growth=0.002)     # monoton yükseliş
        return d15

    def _dec(self, i, side=1):
        return {"sym": "TESTUSDT", "i": i, "t": None, "side": side,
                "veto": False, "fams": 2, "atr_pct": 0.02}

    def test_long_in_uptrend_time_exit_positive(self):
        d15 = self._up_symbol()
        sd = _symb(d15, _series(120, T0, H1), _series(40, T0, H4))
        cfg = dict(FB.GRID[3])
        trs = FB.simulate_trades(sd, [self._dec(300)], cfg, rng_seed=7)
        assert len(trs) == 1
        t = trs[0]
        assert t["t_entry"] == d15[301][0]
        assert t["reason"] in ("sl", "tp", "time")
        assert 1 <= t["bars"] <= FB.BARS_TIME_LIMIT
        # bağımsız maliyet kontrolü: net = gross − fee − 2·slip + funding·gün
        for s in FB.SLIPPAGE_EMPIRICAL:
            for b in range(1, 17):
                exit_i = 301 + b - 1
                cand = edge_engine.net_return_pct(1, d15[301][1], d15[exit_i][4],
                                                  FB.FEE_ROUND_TRIP, 2 * s)
                cand += FB.FUNDING_PER_DAY * (b * 15.0 / 1440.0)
                if abs(cand - t["net_ret"]) < 1e-12:
                    assert b == t["bars"]
                    break
            else:
                continue
            break
        else:
            raise AssertionError("net_ret hiçbir (slip,bar) çiftiyle eşleşmedi")

    def test_veto_and_fam_filter(self):
        d15 = self._up_symbol()
        sd = _symb(d15, _series(120, T0, H1), _series(40, T0, H4))
        cfg = dict(FB.GRID[0])                        # min_fam=1
        dec_v = self._dec(300)
        dec_v["veto"] = True
        assert FB.simulate_trades(sd, [dec_v], cfg, 7) == []
        dec_f = self._dec(300)
        dec_f["fams"] = 0
        assert FB.simulate_trades(sd, [dec_f], cfg, 7) == []
        cfg2 = dict(FB.GRID[1])                       # min_fam=2
        dec_f1 = self._dec(300)
        dec_f1["fams"] = 1
        assert FB.simulate_trades(sd, [dec_f1], cfg2, 7) == []

    def test_determinism(self):
        d15 = self._up_symbol()
        sd = _symb(d15, _series(120, T0, H1), _series(40, T0, H4))
        a = FB.simulate_trades(sd, [self._dec(i) for i in (200, 260, 320)],
                               FB.GRID[3], rng_seed=7)
        b = FB.simulate_trades(sd, [self._dec(i) for i in (200, 260, 320)],
                               FB.GRID[3], rng_seed=7)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class TestPortfolio:
    def _t(self, sym, k, net, sl=0.03):
        te = T0 + k * M15
        return {"sym": sym, "side": 1, "t_entry": te, "t_exit": te + 8 * M15,
                "bars": 8, "reason": "time", "net_ret": net, "sl_pct": sl,
                "tp_pct": sl * 2}

    def test_max_three_concurrent(self):
        trades = [self._t(s, 0, 0.01) for s in ("AUSDT", "B", "C", "D")]
        closed, daily = FB.simulate_portfolio(trades)
        assert len(closed) == 3

    def test_one_per_symbol(self):
        closed, _ = FB.simulate_portfolio(
            [self._t("AUSDT", 0, 0.01), self._t("AUSDT", 1, 0.02)])
        assert [t["sym"] for t in closed] == ["AUSDT"]

    def test_sequential_after_exit_allowed(self):
        t1 = self._t("AUSDT", 0, 0.01)
        t2 = self._t("AUSDT", 9, 0.01)                # t1 çıkışından sonra
        closed, _ = FB.simulate_portfolio([t1, t2])
        assert len(closed) == 2

    def test_compound_notional(self):
        closed, daily = FB.simulate_portfolio(
            [self._t("AUSDT", 0, 0.10), self._t("BUSDT", 8, 0.10)])
        # sl=0.03 → notional oranı min(0.20, 0.5)=0.20
        assert closed[0]["notional"] == 200.0
        assert closed[0]["equity_after"] == 1020.0
        assert closed[1]["notional"] == 204.0
        assert closed[1]["equity_after"] == 1040.4
        # her iki çıkış da AYNI takvim gününde → günlük seri tek gözlem taşır
        assert daily == [1040.4 / 1000 - 1]


class TestGrid:
    def test_grid_18_configs(self):
        assert len(FB.GRID) == 18                     # DEVIATIONS §D-2: 18+1
        fams = {c["confirm_min_families"] for c in FB.GRID}
        assert fams == {1, 2}
