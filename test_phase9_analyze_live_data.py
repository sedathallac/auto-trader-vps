import random, sqlite3, time
import pytest
import analyze_live_data as al
import strategy_engine as se
import whale_engine as we

M15 = al.M15
DAY = 86_400_000
T0 = 1_760_000_000_000


def whale_rows(n, *, informative, seed=1, days=20):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        acc = i % 2 == 0
        pw = (0.70 if acc else 0.40) if informative else 0.55
        win = rng.random() < ((0.70 if acc else 0.40) if informative else 0.42)
        net = rng.gauss(0.004 if win else -0.005, 0.002)
        out.append({"ts": T0 + int(i * days * DAY / n), "symbol": f"S{i % 7}", "side": 1, "spoof": 0, "p_win": pw,
                    "e_net": 0.002, "cost": 0.0012, "allow": int(acc), "cvd": 0.3, "real": int(win), "fwd": net + 0.0012,
                    "net": net, "horizon": 8})
    return out


def test_whale_edge_exists_when_planted():
    r = al.whale_analysis(whale_rows(1400, informative=True), min_rows=200, min_days=10)
    assert r["verdict"] == al.EDGE and all(r["checks"].values())


def test_whale_no_edge_when_uninformative():
    r = al.whale_analysis(whale_rows(1400, informative=False), min_rows=200, min_days=10)
    assert r["verdict"] == al.NO_EDGE


def test_whale_insufficient_when_few_rows_or_days():
    r = al.whale_analysis(whale_rows(40, informative=True), min_rows=200, min_days=10)
    assert r["verdict"] == al.INSUFFICIENT and r["reason"]


def test_decluster_drops_overlapping_rows_per_symbol():
    rows = [{"symbol": "A", "ts": T0 + i * M15} for i in range(20)] + [{"symbol": "B", "ts": T0}]
    kept = al.decluster(rows, lambda r: r["symbol"], "ts", 8 * M15)
    assert sum(r["symbol"] == "A" for r in kept) == 3 and sum(r["symbol"] == "B" for r in kept) == 1


def test_brier_skill_signs():
    rows = whale_rows(600, informative=True)
    assert al.brier_skill_expanding(rows) > 0
    for r in rows:
        r["p_win"] = 0.0 if r["real"] else 1.0                    # sürekli yanlış model
    assert al.brier_skill_expanding(rows) < 0


def _klines(n, start, step=M15, drift=0.001):
    rows, px = [], 100.0
    for i in range(n):
        o, c = px, px * (1 + drift)
        rows.append([start + i * step, str(o), str(c * 1.0005), str(o * 0.9995), str(c), "100", start + (i + 1) * step - 1,
                     "0", 1, "50", "0", "0"])
        px = c
    return rows


def test_resolve_uses_next_bar_open_and_skips_unresolved(monkeypatch):
    now = T0 + 200 * M15
    kl = _klines(200, T0 - M15)
    getter = lambda p: ([r for r in kl if p["startTime"] <= r[0] <= p["endTime"]], 0)
    rows = [{"ts": T0 + 10 * M15 + 5, "symbol": "X", "side": 1, "cost": 0.0012},      # çözülür (yükselen veri → real)
            {"ts": T0 + 199 * M15, "symbol": "X", "side": 1, "cost": 0.0012}]          # ufuk yok → atlanır
    out, skipped = al.resolve_whale_rows(rows, getter, now_ms=now, sleeper=lambda s: None)
    assert len(out) == 1 and skipped == 1
    assert out[0]["real"] == 1 and out[0]["fwd"] > 0.007            # 8 mum * %0.1


def strat_rows(n, *, edge, seed=2):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        good = i % 3 == 0
        p = (0.75 if good else 0.40) if edge else 0.42
        win = rng.random() < p
        out.append({"ts": T0 + i * 12 * M15, "symbol": f"S{i % 9}", "signal": "rsi_extreme" if good else "bollinger_z",
                    "regime": "trend", "side": 1, "real": int(win),
                    "fwd": rng.gauss(0.006 if win else -0.006, 0.002), "horizon": 8})
    return out


def test_strategy_edge_found_out_of_sample():
    r = al.strategy_analysis(strat_rows(4500, edge=True), min_events=100, n_perm=300)
    assert r["verdict"] == al.EDGE and r["n_accepted"] >= 30
    assert r["posteriors"]["rsi_extreme|trend"]["p_real"] > 0.7


def test_strategy_no_edge_or_insufficient_on_noise():
    r = al.strategy_analysis(strat_rows(4500, edge=False), min_events=100, n_perm=300)
    assert r["verdict"] in (al.NO_EDGE, al.INSUFFICIENT) and r["verdict"] != al.EDGE


def test_strategy_low_p_real_gate_blocks():
    r = al.strategy_analysis(strat_rows(4500, edge=True), min_events=100, n_perm=300, min_p_real=0.95)
    assert r["verdict"] == al.INSUFFICIENT


def test_overall_decisions():
    E, N, I = {"verdict": al.EDGE}, {"verdict": al.NO_EDGE}, {"verdict": al.INSUFFICIENT}
    assert al.overall(E, N)[0] == al.EDGE and "on moduna" in al.overall(E, N)[1]
    assert al.overall(N, N)[0] == al.NO_EDGE and "off" in al.overall(N, N)[1]
    assert al.overall(I, I)[0] == al.INSUFFICIENT and al.overall(N, I)[0] == al.INSUFFICIENT


def test_run_is_readonly_and_handles_missing_dbs(tmp_path):
    rep, _ = al.run(tmp_path / "w.db", tmp_path / "s.db", lambda p: ([], 0))
    assert rep["verdict"] == al.INSUFFICIENT
    assert not (tmp_path / "w.db").exists() and not (tmp_path / "s.db").exists()


def test_run_end_to_end_from_dbs(tmp_path):
    c = sqlite3.connect(tmp_path / "w.db"); c.execute(we._SCHEMA)
    for i in range(30):
        c.execute("INSERT INTO whale_metrics (recorded_at,symbol,mode,side,quality_ok,spoof,p_win,e_net,cost,allow,"
                  "components_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ((T0 + i * 10 * M15) / 1000, "X", "compare", 1, 1, 0, 0.6, 0.002, 0.0012, i % 2, '{"cvd":0.3}'))
    c.commit(); c.close()
    s = se.SignalStore(tmp_path / "s.db")
    s._conn().close()
    before = (tmp_path / "w.db").read_bytes()
    kl = _klines(400, T0 - M15)
    rep, _ = al.run(tmp_path / "w.db", tmp_path / "s.db",
                    lambda p: ([r for r in kl if p["startTime"] <= r[0] <= p["endTime"]], 0), now_ms=T0 + 400 * M15,
                    sleeper=lambda s: None, min_rows=10, min_days=1)
    assert rep["whale"]["n_rows_loaded"] == 30 and rep["whale"]["n_declustered"] > 0
    assert (tmp_path / "w.db").read_bytes() == before
    assert "Faz 9 final analiz" in al.render(rep)
