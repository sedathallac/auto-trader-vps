import sqlite3, time
import monitor_live as ml
import whale_engine as we
import strategy_engine as se


def _log(p, lines):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    p.write_text("\n".join(f"{ts} [INFO] {l}" for l in lines) + "\n")


def _whale(path, rows):
    c = sqlite3.connect(path); c.execute(we._SCHEMA)
    for q, sp in rows:
        c.execute("INSERT INTO whale_metrics (recorded_at,symbol,mode,quality_ok,spoof) VALUES (?,?,?,?,?)",
                  (time.time(), "BTCUSDT", "compare", q, sp))
    c.commit(); c.close()


def test_ok_report_and_rates(tmp_path):
    _whale(tmp_path / "w.db", [(1, 0)] * 6 + [(0, 1)] * 4)
    _log(tmp_path / "l.log", ["🔌 HB_OFFLINE aktif", "Faz5[compare]: x", "Faz6[compare]: y"])
    r = ml.build_report(whale_db=tmp_path / "w.db", signal_db=tmp_path / "s.db", log=tmp_path / "l.log")
    assert r["whale"]["total"] == 10 and abs(r["whale"]["obi_empty_rate"] - 0.4) < 1e-9
    assert abs(r["whale"]["spoof_rate"] - 0.4) < 1e-9
    assert r["exit_code"] == ml.WARN and any("OBI" in m for m in r["warn"])
    assert "Faz 9" in ml.render(r)


def test_rate_limit_and_missing_hb_offline_stop(tmp_path):
    _log(tmp_path / "l.log", ["HTTP Error 429: rate limit", "Faz5[compare]"])
    r = ml.build_report(whale_db=tmp_path / "w.db", signal_db=tmp_path / "s.db", log=tmp_path / "l.log")
    assert r["exit_code"] == ml.STOP
    assert any("429" in m for m in r["stop"]) and any("HB_OFFLINE" in m for m in r["stop"])


def test_readonly_does_not_create_dbs(tmp_path):
    ml.build_report(whale_db=tmp_path / "w.db", signal_db=tmp_path / "s.db", log=tmp_path / "none.log")
    assert not (tmp_path / "w.db").exists() and not (tmp_path / "s.db").exists()


def test_signal_stats_live_resolved(tmp_path):
    store = se.SignalStore(tmp_path / "s.db")
    store._conn().close()
    c = sqlite3.connect(tmp_path / "s.db")
    for i in range(3):
        c.execute("INSERT INTO signal_outcomes (recorded_at,symbol,signal,family,regime,direction,entry_ts,entry_price,"
                  "horizon,source,resolved) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                  (time.time(), "X", "rsi_extreme", "exhaustion", "trend", 1, i, 1.0, 8, "live"))
    c.commit(); c.close()
    s = ml.signal_stats(tmp_path / "s.db", 0)
    assert s["live_resolved"] == 3 and s["max_per_signal_regime"] == 3
