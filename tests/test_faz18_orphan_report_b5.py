"""test_faz18_orphan_report_b5.py — FAZ18-B5: orphan_report kusur sözleşmeleri.

Kapsam (kusur bazlı):
  D1 env/anahtar: systemd EnvironmentFile + BINANCE_KEY/BINANCE_SECRET;
     canlı süreç ortamı (pid-environ) okuma yolu YOK; anahtar yazdırılmaz.
  D2 TP≠SL: TAKE_PROFIT* asla SL sayılmaz; STOP_TRAILING ayrı TRAILING sınıfı.
  D3 hata→boş-liste YOK: istisna/hata-sözlüğü/liste-değil → raise veya
     INCOMPLETE; 418/429 → Retry-After log + anında DUR (exit 3, retry yok).
  D4 ledger: mode=ro + PRAGMA şema doğrulaması; hata ⇒ INCOMPLETE (asla temiz değil).
  D5 SL geçerliliği: yön/kapsam/trigger/likidasyon/algoStatus/çift-SL.
  E1 sembolsüz tekil çağrılar; E2 apiRestrictions ön-kontrolü (exit 4);
  E3 final satır OK/FINDINGS/INCOMPLETE + exit 0/1/2 + "N/N sorgu başarılı"
     + X-MBX-USED-WEIGHT-1M logu + --max-weight bütçesi.
  Golden: gerçek ham algo_orders_20261119.ndjson (F20 muhasebe körlüğü kanıtı).

Ağ YOK: tüm testler sahte opener / sahte transport ile koşar.
"""
import ast
import json
import pathlib
import sqlite3
import urllib.error

import pytest

from tools import orphan_report as OR

SRC = pathlib.Path(OR.__file__).read_text()
GOLDEN = pathlib.Path("/home/reisfurkan/faz9-audit/raw/streams/"
                      "algo_orders_20261119.ndjson")

# ---------------------------------------------------------------- yardımcılar

LEDGER_COLS = ("symbol", "token", "state", "created_at", "updated_at", "gen",
               "algo_id", "client_algo_id")


_DB_SEQ = [0]


def make_db(tmp_path, columns=LEDGER_COLS, rows=()):
    _DB_SEQ[0] += 1
    p = tmp_path / f"ledger-{_DB_SEQ[0]}.db"      # aynı testte tekrar-güvenli
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE sl_placements (%s)"
                % ", ".join(f"{c} TEXT" for c in columns))
    for r in rows:
        con.execute("INSERT INTO sl_placements VALUES (%s)"
                    % ",".join("?" * len(columns)), r)
    con.commit()
    con.close()
    return str(p)


class FakeTransport:
    """Orkestra taşıyıcı stub'ı: path→veri; Exception değeri raise edilir.
    w1m_step>0 ise her çağrıda X-MBX-USED-WEIGHT-1M bu kadar artar."""

    def __init__(self, positions=(), algos=(), orders=(),
                 api_restrictions=None, w1m_step=0):
        def _lst(v):
            return v if isinstance(v, Exception) else list(v)
        self.data = {
            "/fapi/v2/positionRisk": _lst(positions),
            "/fapi/v1/openAlgoOrders": _lst(algos),
            "/fapi/v1/openOrders": _lst(orders),
            "/sapi/v1/account/apiRestrictions": api_restrictions
            if api_restrictions is not None
            else {"enableWithdrawals": False, "ipRestrict": True},
        }
        self.calls = []
        self.w1m = 0
        self.w1m_step = w1m_step

    def get(self, path, params=None, base=None):
        self.calls.append((path, params, base))
        self.w1m += self.w1m_step
        v = self.data.get(path)
        if isinstance(v, Exception):
            # gerçek transport gibi: ham taşıma hatası QueryError'a sarılır
            if isinstance(v, (OR.QueryError, OR.BanError)):
                raise v
            raise OR.QueryError(f"{type(v).__name__}: {v}")
        if v is None:
            raise OR.QueryError(f"bilinmeyen yol {path}")
        return v


class FakeResp:
    """urlopen yanıtı taklidi: read()/headers/context-manager."""

    def __init__(self, body, w1m=None):
        self._b = json.dumps(body).encode()
        self.headers = {} if w1m is None else {"X-MBX-USED-WEIGHT-1M": str(w1m)}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def LONG(amt="0.1", entry="90000", mark="91000", liq=None, sym="BTCUSDT"):
    p = {"symbol": sym, "positionAmt": amt, "entryPrice": entry,
         "markPrice": mark}
    if liq is not None:
        p["liqPrice"] = liq
    return p


def SL(side="SELL", status="NEW", qty=None, trig="89000", close=True,
       sym="BTCUSDT", ps="BOTH", aid="1", otype="STOP_MARKET"):
    a = {"algoId": aid, "symbol": sym, "side": side, "algoStatus": status,
         "orderType": otype, "positionSide": ps, "triggerPrice": trig,
         "closePosition": close}
    if qty is not None:
        a["quantity"] = qty
    return a


def _last(out):
    return [l for l in out.out.strip().splitlines() if l.strip()][-1]


# ================================================================= D1 env/keys


class TestD1EnvKeys:
    def test_parse_env_file_systemd_format(self, tmp_path):
        f = tmp_path / "env"
        f.write_text(
            "# yorum\n"
            "\n"
            "; boş-benzeri yorum\n"
            "BINANCE_KEY=first\n"
            "OTHER=x=y\n"
            "BINANCE_KEY=second\n"            # last-wins
            'BINANCE_SECRET="q1"\n'          # çift tırnak soyulur
            "EMPTY=\n"
            "yorum-değil-satırı\n")          # '=' yok → atlanır
        env = OR.parse_env_file(str(f))
        assert env["BINANCE_KEY"] == "second"
        assert env["BINANCE_SECRET"] == "q1"
        assert env["OTHER"] == "x=y"
        assert env["EMPTY"] == ""

    def test_single_quotes_stripped(self, tmp_path):
        f = tmp_path / "env"
        f.write_text("BINANCE_KEY='abc'\nBINANCE_SECRET='d e f'\n")
        env = OR.parse_env_file(str(f))
        assert env["BINANCE_KEY"] == "abc"
        assert env["BINANCE_SECRET"] == "d e f"

    def test_main_uses_env_file_then_env_fallback(self, tmp_path, monkeypatch):
        # D1: dosyadaki BINANCE_KEY, ortam-düşüşü BINANCE_SECRET; MbxTransport'a
        # argv'den değil bu yollardan ulaşmalı. Anahtar değeri yazdırılmaz.
        f = tmp_path / "env"
        f.write_text("BINANCE_KEY=filekey\n")
        monkeypatch.setenv("BINANCE_KEY", "ENVKEY-SCRATCH")   # dosya kazanır
        monkeypatch.setenv("BINANCE_SECRET", "envsec")
        captured = {}

        class CapTransport(FakeTransport):
            def __init__(self, key, sec, base, opener=None):
                captured["kv"] = (key, sec, base)
                super().__init__(positions=[LONG()],
                                 algos=[SL()])

        monkeypatch.setattr(OR, "MbxTransport", CapTransport)
        db = make_db(tmp_path)
        rc = OR.main(["--env-file", str(f), "--db", db, "--skip-preflight"])
        assert rc == 0, "temiz senaryo 0 dönmeli"
        assert captured["kv"] == ("filekey", "envsec",
                                  "https://fapi.binance.com")

    def test_keys_never_printed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BINANCE_KEY", "SÜPER-GİZLİ-123")
        monkeypatch.setenv("BINANCE_SECRET", "SIR-GİZLİ-456")
        tr = FakeTransport(positions=[LONG()], algos=[SL()])
        rc = OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                     transport=tr)
        assert rc == 0
        out = capsys.readouterr()
        assert "GİZLİ" not in out.out + out.err

    def test_no_pid_environ_path_anywhere(self):
        # D1: pid-environ okuma yolu tamamen silindi (yalnız kod taraması).
        tree = ast.parse(SRC)
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                b = n.body
                if b and isinstance(b[0], ast.Expr) and isinstance(
                        b[0].value, ast.Constant) and isinstance(
                        b[0].value.value, str):
                    n.body = b[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        assert "/proc" not in code
        assert "_env_from_proc" not in code
        assert "--pid" not in code


# ================================================================== D2 TP≠SL


class TestD2TpNotSl:
    def test_classify_order_type(self):
        assert OR.classify_order_type("STOP_MARKET") == "SL"
        assert OR.classify_order_type("stop") == "SL"
        assert OR.classify_order_type("STOP_TRAILING") == "TRAILING"
        assert OR.classify_order_type("TRAILING_STOP_MARKET") == "TRAILING"
        assert OR.classify_order_type("TAKE_PROFIT") == "TP"
        assert OR.classify_order_type("TAKE_PROFIT_MARKET") == "TP"
        assert OR.classify_order_type("LIMIT") == ""

    def test_tp_only_is_sl_yok(self):
        algos = {"BTCUSDT": [SL(otype="TAKE_PROFIT_MARKET", trig="95000",
                                side="SELL")]}
        out = OR.analyze([LONG()], algos, [])
        row = out["rows"][0]
        assert "SL_YOK" in row["bulgu"]
        assert out["verdict"].startswith("VERDICT: FINDINGS")

    def test_trailing_protective_but_flagged(self):
        for ot in ("STOP_TRAILING", "TRAILING_STOP_MARKET"):
            algos = {"BTCUSDT": [SL(otype=ot, trig="89000", side="SELL")]}
            out = OR.analyze([LONG()], algos, [])
            row = out["rows"][0]
            assert "SL_YOK" not in row["bulgu"]
            assert "SL_TRAILING" in row["bulgu"]
            assert out["verdict"].startswith("VERDICT: FINDINGS")

    def test_tp_plus_sl_is_clean(self):
        algos = {"BTCUSDT": [SL(), SL(otype="TAKE_PROFIT_MARKET",
                                      trig="95000", aid="2")]}
        out = OR.analyze([LONG()], algos, [])
        assert out["verdict"].startswith("VERDICT: OK")


# ============================================================== D3 error paths


class TestD3NoSilentErrors:
    def test_query_error_marks_incomplete_and_suppresses_sl_yok(
            self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()],
                           algos=OR.QueryError("taşıma koptu"))
        rc = OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                     transport=tr)
        assert rc == 2
        out = capsys.readouterr()
        assert _last(out).startswith("VERDICT: INCOMPLETE")
        assert "SL_YOK" not in out.out          # eksik veriyle hüküm YOK
        assert "2/3 sorgu başarılı" in out.out

    def test_http_error_dict_raises_not_empty(self):
        def opener(req, timeout=None):
            return FakeResp({"code": -1003, "msg": "banned"})
        tr = OR.MbxTransport("k", "s", "https://fapi.binance.com",
                             opener=opener)
        with pytest.raises(OR.QueryError):
            tr.get("/fapi/v2/positionRisk")

    def test_nonlist_response_marks_incomplete(self, tmp_path, capsys):
        # Liste beklenen uçta liste-değil gövde → sorgu INCOMPLETE (D3);
        # Runner.query şema doğrulaması taşır, "temiz boş" üretmez.
        def opener(req, timeout=None):
            return FakeResp({"beklenmeyen": "şema"})
        tr = OR.MbxTransport("k", "s", "https://fapi.binance.com",
                             opener=opener)
        rc = OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                     transport=tr)
        assert rc == 2
        assert _last(capsys.readouterr()).startswith("VERDICT: INCOMPLETE")

    def test_runner_query_validates_schema_types(self):
        run = OR.Runner(lambda path, params=None: {"koddan": "kaçmış"})
        assert run.query("x", "/fapi/v1/openAlgoOrders") is None
        assert "x" in run.incomplete and run.ok == 0
        run2 = OR.Runner(lambda path, params=None: [1, 2])
        assert run2.query("x", "/fapi/v1/openAlgoOrders") == [1, 2]
        assert run2.ok == 1 and run2.total == 1

    @pytest.mark.parametrize("code,ra", [(418, "77"), (429, "5")])
    def test_ban_exit3_no_retry(self, code, ra, capsys):
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            raise urllib.error.HTTPError("https://fapi.binance.com/x", code,
                                         "ban", {"Retry-After": ra}, None)
        tr = OR.MbxTransport("k", "s", "https://fapi.binance.com",
                             opener=opener)
        rc = OR.main(["--skip-preflight", "--db", "/tmp/yok.db"],
                     transport=tr)
        assert rc == 3
        out = capsys.readouterr()
        assert f"418/429 Retry-After={ra}; DURUYORUM" in out.out + out.err
        assert len(calls) == 1                  # retry YOK — anında dur

    def test_other_http_error_incomplete_not_retry(self, tmp_path, capsys):
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            raise urllib.error.HTTPError(
                "https://fapi.binance.com" + req.full_url.split(
                    "fapi.binance.com", 1)[1].split("?")[0], 400, "bad",
                {}, None)
        tr = OR.MbxTransport("k", "s", "https://fapi.binance.com",
                             opener=opener)
        rc = OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                     transport=tr)
        assert rc == 2                          # INCOMPLETE, sessiz boş değil
        paths = [c.full_url.split("?")[0] for c in calls]
        assert len(paths) == len(set(paths)), "retry edildi — retry YOK"

    def test_transport_exception_marks_incomplete_never_empty(self):
        tr = FakeTransport(positions=OSError("sock koptu"))
        run = OR.Runner(tr.get)
        assert run.query("positionRisk", "/fapi/v2/positionRisk") is None
        assert "positionRisk" in run.incomplete
        assert run.ok == 0

    def test_ast_no_return_empty_list_anywhere(self):
        # B3-kanıtı: hiçbir fonksiyon boş liste DÖNDÜRMEZ (hata→boş evren
        # "temiz" okuması F20 körlüğüdür). Query fonksiyonları istisnayı
        # yutup [] yerine None/incomplete ile işaretler.
        for n in ast.walk(ast.parse(SRC)):
            if isinstance(n, ast.Return):
                v = n.value
                assert not (isinstance(v, ast.List) and not v.elts), \
                    "return [] bulundu — hata→boş-liste yolu yasak"
                assert not (isinstance(v, ast.Constant) and v.value == []), \
                    "return [] bulundu"

    def test_ast_query_funcs_no_broad_except(self):
        # İstisnalar YALNIZ tipli handler'la yakalanır; Exception/BaseException
        # ve çıplak except query yollarında yasak (BanError yutulamaz).
        tree = ast.parse(SRC)
        qfuncs = {"collect", "_read_ledger", "query", "get", "main"}
        checked = 0
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name in qfuncs:
                checked += 1
                for h in ast.walk(n):
                    if isinstance(h, ast.ExceptHandler):
                        assert h.type is not None, \
                            f"{n.name}: çıplak except"
                        t = ast.unparse(h.type)
                        assert "Exception" not in t and "BaseException" not in t, \
                            f"{n.name}: geniş except {t}"
                        assert "BanError" not in t or n.name == "main", \
                            "BanError yalnız main'de yakalanabilir"
        assert checked >= 4


# ================================================================== D4 ledger


class TestD4Ledger:
    def test_missing_column_is_incomplete_not_clean(self, tmp_path, capsys):
        cols = tuple(c for c in LEDGER_COLS if c != "state")
        db = make_db(tmp_path, columns=cols)
        rows, err = OR._read_ledger(db)
        assert err and "state" in err
        tr = FakeTransport(positions=[LONG()], algos=[SL()])
        rc = OR.main(["--skip-preflight", "--db", db], transport=tr)
        assert rc == 2
        assert _last(capsys.readouterr()).startswith("VERDICT: INCOMPLETE")

    def test_missing_table_is_incomplete(self, tmp_path):
        p = tmp_path / "empty.db"
        sqlite3.connect(str(p)).close()
        rows, err = OR._read_ledger(str(p))
        assert err and rows == []

    def test_missing_db_file_is_incomplete(self, tmp_path):
        rows, err = OR._read_ledger(str(tmp_path / "yok.db"))
        assert err and rows == []

    def test_reads_only_pending_placed(self, tmp_path):
        db = make_db(tmp_path, rows=[
            ("BTCUSDT", "t1", "PLACED", "a", "b", 1, "9", "c1"),
            ("SOLUSDT", "t2", "PENDING", "a", "b", 2, "8", "c2"),
            ("OLDUSDT", "t3", "DONE", "a", "b", 3, "7", "c3"),
        ])
        rows, err = OR._read_ledger(db)
        assert err is None
        assert {r["symbol"] for r in rows} == {"BTCUSDT", "SOLUSDT"}

    def test_opened_read_only(self, tmp_path):
        # mode=ro: dosya salt-okunur açılır; kaynakta uri-flag zorunlu.
        assert "mode=ro" in SRC
        db = make_db(tmp_path)
        OR._read_ledger(db)                      # hata üretmemeli
        # Salt-okunur kanıt: uri ile yazma denemesi patlar.
        with pytest.raises(sqlite3.OperationalError):
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.execute("INSERT INTO sl_placements VALUES (1,2,3,4,5,6,7,8)")


# ================================================================ D5 validity


class TestD5StopValidity:
    def test_valid_long_sl_clean(self):
        out = OR.analyze([LONG()], {"BTCUSDT": [SL()]}, [])
        assert out["verdict"].startswith("VERDICT: OK")
        assert out["rows"][0]["bulgu"] == "-"

    def test_wrong_side_invalid(self):
        # LONG pozisyona BUY stop → SL_GEÇERSİZ.
        out = OR.analyze([LONG()], {"BTCUSDT": [SL(side="BUY")]}, [])
        assert "SL_GEÇERSİZ" in out["rows"][0]["bulgu"]
        assert "SL_YOK" not in out["rows"][0]["bulgu"]

    def test_position_side_mismatch_invalid(self):
        # SHORT pozisyon, hedge modunda positionSide=LONG SL → geçersiz.
        pos = [LONG(amt="-2.0", entry="200", mark="195", sym="SOLUSDT")]
        algos = {"SOLUSDT": [SL(sym="SOLUSDT", side="BUY", ps="LONG",
                                trig="210")]}
        out = OR.analyze(pos, algos, [])
        assert "SL_GEÇERSİZ" in out["rows"][0]["bulgu"]

    def test_short_side_valid(self):
        pos = [LONG(amt="-2.0", entry="200", mark="195", sym="SOLUSDT")]
        algos = {"SOLUSDT": [SL(sym="SOLUSDT", side="BUY", trig="210")]}
        out = OR.analyze(pos, algos, [])
        assert out["verdict"].startswith("VERDICT: OK")

    def test_trigger_on_free_side_invalid(self):
        # LONG: stopPrice ≥ markPrice → tetiklenmiş/absürt → SL_GEÇERSİZ.
        out = OR.analyze([LONG()], {"BTCUSDT": [SL(trig="92000")]}, [])
        assert "SL_GEÇERSİZ" in out["rows"][0]["bulgu"]

    def test_insufficient_coverage_invalid(self):
        algos = {"BTCUSDT": [SL(close=False, qty="0.01")]}
        out = OR.analyze([LONG()], algos, [])
        assert "SL_GEÇERSİZ" in out["rows"][0]["bulgu"]

    def test_close_position_covers_any_size(self):
        algos = {"BTCUSDT": [SL(close=True, qty="0.001")]}
        out = OR.analyze([LONG()], algos, [])
        assert out["verdict"].startswith("VERDICT: OK")

    def test_liquidation_sanity(self):
        # LONG: trigger ≤ liqPrice → likidasyon önce gelir → SL_GEÇERSİZ.
        out = OR.analyze([LONG(liq="89500")],
                         {"BTCUSDT": [SL(trig="89000")]}, [])
        assert "SL_GEÇERSİZ" in out["rows"][0]["bulgu"]
        # liqPrice yoksa kontrol atlanır (spec: skip if absent).
        out2 = OR.analyze([LONG()], {"BTCUSDT": [SL(trig="89000")]}, [])
        assert out2["verdict"].startswith("VERDICT: OK")

    def test_finished_or_canceled_not_live(self):
        # F20: tetiklenmiş (FINISHED) / iptal (CANCELED) SL koruma DEĞİLDİR.
        for st in ("FINISHED", "CANCELED", "REJECTED", "EXPIRED"):
            out = OR.analyze([LONG()],
                             {"BTCUSDT": [SL(status=st, trig="89000")]}, [])
            assert "SL_YOK" in out["rows"][0]["bulgu"], st

    def test_partially_filled_counts_live(self):
        out = OR.analyze([LONG()],
                         {"BTCUSDT": [SL(status="PARTIALLY_FILLED")]}, [])
        assert out["verdict"].startswith("VERDICT: OK")

    def test_double_sl_flagged(self):
        algos = {"BTCUSDT": [SL(aid="1"), SL(aid="2")]}
        out = OR.analyze([LONG()], algos, [])
        assert "ÇİFT_SL" in out["rows"][0]["bulgu"]

    def test_sl_plus_tp_not_double(self):
        algos = {"BTCUSDT": [SL(), SL(otype="TAKE_PROFIT_MARKET", aid="2",
                                      trig="95000")]}
        out = OR.analyze([LONG()], algos, [])
        assert "ÇİFT_SL" not in out["rows"][0]["bulgu"]

    def test_orphan_regular_stop_flagged(self):
        # openOrders'daki canlı STOP_MARKET, pozisyon yok → YETIM_SL.
        out = OR.analyze([], {}, [], orders_by_sym={"XUSDT": [
            {"orderId": "1", "symbol": "XUSDT", "status": "WORKING",
             "type": "STOP_MARKET", "side": "SELL"}]})
        row = [r for r in out["rows"] if r["symbol"] == "XUSDT"][0]
        assert "YETIM_SL" in row["bulgu"]

    def test_regular_stop_does_not_clear_sl_yok_a1(self):
        # A-1 doktrini: yokluk hükmü yalnız algo service'ten; normal emir
        # STOP_MARKET SL_YOK'u temize çıkaramaz.
        out = OR.analyze([LONG()], {}, [], orders_by_sym={"BTCUSDT": [
            {"orderId": "1", "symbol": "BTCUSDT", "status": "WORKING",
             "type": "STOP_MARKET", "side": "SELL"}]})
        assert "SL_YOK" in out["rows"][0]["bulgu"]


# ================================================================== E1/E2/E3


class TestE1Scope:
    def test_symbolless_single_calls(self):
        calls = []

        def fake_get(path, params=None):
            calls.append((path, params))
            if path.endswith("positionRisk"):
                return [{"symbol": "BTCUSDT", "positionAmt": "0.1"}]
            return []
        OR.collect(fake_get, [], sleep=lambda s: None)
        assert len([c for c in calls if c[0].endswith("openAlgoOrders")]) == 1
        assert len([c for c in calls if c[0].endswith("openOrders")]) == 1
        assert all(p in (None, {}) for _, p in calls)   # sembol paramsız

    def test_drill_down_only_with_symbol_flag(self, tmp_path):
        tr = FakeTransport(positions=[LONG()], algos=[SL()])
        rc = OR.main(["--skip-preflight", "--db", make_db(tmp_path),
                      "--symbol", "BTCUSDT"], transport=tr)
        assert rc == 0
        algo_calls = [c for c in tr.calls if c[0].endswith("openAlgoOrders")]
        assert any(c[1] and c[1].get("symbol") == "BTCUSDT"
                   for c in algo_calls)


class TestE2Preflight:
    def test_withdrawal_enabled_refuses_exit4(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()],
                           api_restrictions={"enableWithdrawals": True,
                                             "ipRestrict": True})
        assert OR.main(["--db", make_db(tmp_path)], transport=tr) == 4
        cap = capsys.readouterr()
        assert "RET" in cap.out + cap.err

    def test_ip_unrestricted_refuses_exit4(self, tmp_path):
        tr = FakeTransport(positions=[LONG()], algos=[SL()],
                           api_restrictions={"enableWithdrawals": False,
                                             "ipRestrict": False})
        assert OR.main(["--db", make_db(tmp_path)], transport=tr) == 4

    def test_ok_policy_runs_and_counts_preflight(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()])
        rc = OR.main(["--db", make_db(tmp_path)], transport=tr)
        assert rc == 0
        out = capsys.readouterr()
        assert "4/4 sorgu başarılı" in out.out
        assert tr.calls[0][0] == "/sapi/v1/account/apiRestrictions"
        assert tr.calls[0][2] == "https://api.binance.com"   # --sapi-base

    def test_preflight_query_failure_incomplete(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()],
                           api_restrictions=OR.QueryError("sapi erişilemedi"))
        assert OR.main(["--db", make_db(tmp_path)], transport=tr) == 2
        assert _last(capsys.readouterr()).startswith("VERDICT: INCOMPLETE")

    def test_skip_preflight_for_tests_only(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()],
                           api_restrictions={"enableWithdrawals": True,
                                             "ipRestrict": True})
        rc = OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                     transport=tr)
        assert rc == 0
        assert not [c for c in tr.calls if "apiRestrictions" in c[0]]


class TestE3Protocol:
    def test_verdict_final_line_and_exit_codes(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()])
        assert OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                       transport=tr) == 0
        assert _last(capsys.readouterr()).startswith("VERDICT: OK")

        bad = FakeTransport(positions=[LONG()], algos=[])   # SL yok
        assert OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                       transport=bad) == 1
        assert _last(capsys.readouterr()).startswith("VERDICT: FINDINGS")

        broken = FakeTransport(positions=OR.QueryError("pat"), algos=[])
        assert OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                       transport=broken) == 2
        assert _last(capsys.readouterr()).startswith("VERDICT: INCOMPLETE")

    def test_weight_header_logged_per_query(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()], w1m_step=40)
        OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                transport=tr)
        out = capsys.readouterr().out
        assert out.count("W1M=") >= 3

    def test_weight_budget_stop_incomplete(self, tmp_path, capsys):
        tr = FakeTransport(positions=[LONG()], algos=[SL()], w1m_step=100)
        # delta: 0, 100, 200 → 3. sorguda 150 bütçeyi aşar; 4. sorgu koşmaz.
        rc = OR.main(["--max-weight", "150", "--db", make_db(tmp_path)],
                     transport=tr)
        assert rc == 2
        assert len(tr.calls) == 3
        assert _last(capsys.readouterr()).startswith("VERDICT: INCOMPLETE")

    def test_default_max_weight_300(self, tmp_path):
        tr = FakeTransport(positions=[LONG()], algos=[SL()], w1m_step=400)
        # adım 400 > varsayılan 300 → bütçe aşımı INCOMPLETE.
        assert OR.main(["--skip-preflight", "--db", make_db(tmp_path)],
                       transport=tr) == 2


# ==================================================================== golden


def _golden():
    open_by_sym, hist = {}, []
    for line in GOLDEN.read_text().splitlines():
        d = json.loads(line)
        ds = d["dataset"]
        sym = ds.rsplit("_", 1)[1]
        assert d["error"] is None and isinstance(d["rows"], list)
        if ds.startswith("algo_open"):
            open_by_sym[sym] = d["rows"]
        elif ds.startswith("algo_hist"):
            hist.extend(d["rows"])
    return open_by_sym, hist


@pytest.mark.skipif(not GOLDEN.exists(), reason="golden ham dosya yok")
class TestGoldenRealAlgoFile:
    def test_wrapper_schema(self):
        open_by_sym, hist = _golden()
        assert len(open_by_sym) == 42 and len(hist) == 76
        assert all(r["orderType"] == "STOP_MARKET" for r in hist)

    def test_finished_canceled_are_never_live(self):
        _, hist = _golden()
        assert all(not OR.is_live(r) for r in hist)   # F20 çekirdek kanıt

    def test_triggered_sl_does_not_protect_sl_yok(self):
        # AAVEUSDT'nin 2 FINISHED + 2 CANCELED algo kaydı var; pozisyon açılsa
        # bunların hiçbiri koruma DEĞİLDİR → SL_YOK (muhasebe körlüğü onarımı).
        open_by_sym, hist = _golden()
        algos = {}
        for r in hist:
            algos.setdefault(r["symbol"], []).append(r)
        pos = [LONG(amt="1.7", entry="112", mark="113", sym="AAVEUSDT")]
        out = OR.analyze(pos, algos, [])
        row = [r for r in out["rows"] if r["symbol"] == "AAVEUSDT"][0]
        assert "SL_YOK" in row["bulgu"]
        # Açık algo listesi boş evreni taşımıyor mu? open_* doğrulaması:
        assert open_by_sym["AAVEUSDT"] == []
