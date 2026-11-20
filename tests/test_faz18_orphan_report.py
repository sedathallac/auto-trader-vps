"""test_faz18_orphan_report.py — P0-6 salt-okur yetim raporunun testleri.

Kilit sözleşmeler: (1) YALNIZ GET; (2) SL-yokluk hükmü yalnız algo service
girdisinden (A-1); (3) hiçbir düzeltme/eylem yok; (4) deterministik birleştirme.

FAZ18-B5 güncellemesi: E1 sembolsüz tarama (openAlgoOrders+openOrders tek
çağrı), D1 canlı-pid-env okuma yolu kaldırıldı (--pid yok; BINANCE_KEY/
BINANCE_SECRET + EnvironmentFile), verdict final satırı OK/FINDINGS/INCOMPLETE.
"""
import ast
import pathlib

import pytest

from tools import orphan_report as OR

SRC = pathlib.Path(OR.__file__).read_text()


def _code_only(src):
    """Docstring/yorum metnini atıp YALNIZ çalıştırılabilir kodu taratır
    (docstring'deki 'POST/PUT/DELETE YOK' sözünün taramayı tetiklememesi için)."""
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef,
                          ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value,
                                                               ast.Constant) \
                    and isinstance(b[0].value.value, str):
                n.body = b[1:] or [ast.Pass()]
    return ast.unparse(tree)


class TestReadOnlyGuarantee:
    def test_source_has_no_mutating_calls(self):
        code = _code_only(SRC)
        for banned in ("_post", "requests.post", "'POST", '"POST',
                       "DELETE", "PUT", "place_algo_sl", "cancel",
                       "algoOrder/place", "algoOrder/update",
                       "algoOrder/cancel",
                       "/proc", "_env_from_proc"):   # D1: pid-environ yolu ölü
            assert banned not in code, f"kodda yasak çağrı izi: {banned!r}"

    def test_no_request_method_override(self):
        # urllib.request.Request(...) çağrılarında method= yoksa metot GET'tir.
        for n in ast.walk(ast.parse(SRC)):
            if isinstance(n, ast.Call):
                f = n.func
                fname = f.attr if isinstance(f, ast.Attribute) else \
                    (f.id if isinstance(f, ast.Name) else "")
                assert fname != "Request" or \
                    all(k.arg != "method" for k in n.keywords), \
                    "Request(..., method=...) bulundu — GET garantisi bozulur"

    def test_collect_uses_only_get_contract(self):
        # E1: sembolsüz tekil çağrılar — positionRisk + openAlgoOrders +
        # openOrders; sembol parametresi YOK (drill-down ayrı yol).
        calls = []

        def fake_get(path, params=None):
            calls.append(("GET", path, params))
            if path.endswith("positionRisk"):
                return [{"symbol": "BTCUSDT", "positionAmt": "0.1"}]
            return []
        pos, algos, orders = OR.collect(fake_get, [], sleep=lambda s: None)
        assert all(c[0] == "GET" for c in calls)
        assert {c[1] for c in calls} == {"/fapi/v2/positionRisk",
                                         "/fapi/v1/openAlgoOrders",
                                         "/fapi/v1/openOrders"}
        assert all(not c[2] for c in calls), "sembolsüz taramada parametre yok"


class TestAnalyze:
    POS = [{"symbol": "BTCUSDT", "positionAmt": "0.10", "entryPrice": "90000"},
           {"symbol": "SOLUSDT", "positionAmt": "-2.0", "entryPrice": "200"},
           {"symbol": "FLATXUSDT", "positionAmt": "0.0", "entryPrice": "1"}]

    def test_sl_less_position_flagged(self):
        out = OR.analyze(self.POS, {"BTCUSDT": [], "SOLUSDT": []}, [])
        row = [r for r in out["rows"] if r["symbol"] == "BTCUSDT"][0]
        assert "SL_YOK" in row["bulgu"]
        assert "TEMİZ" not in out["verdict"]

    def test_orphan_sl_flagged(self):
        algos = {"GHOSTUSDT": [{"algoId": "9", "algoStatus": "NEW",
                                "orderType": "STOP_MARKET"}]}
        led = [{"symbol": "GHOSTUSDT", "token": "t", "state": "PLACED",
                "algo_id": "9", "client_algo_id": "c", "gen": 1}]
        out = OR.analyze([], algos, led)       # pozisyon YOK ama SL duruyor
        row = [r for r in out["rows"] if r["symbol"] == "GHOSTUSDT"][0]
        assert "YETIM_SL" in row["bulgu"]

    def test_identity_drift_flagged(self):
        algos = {"BTCUSDT": [{"algoId": "555", "algoStatus": "NEW",
                              "orderType": "STOP_MARKET"}]}
        led = [{"symbol": "BTCUSDT", "token": "t", "state": "PLACED",
                "algo_id": "999", "client_algo_id": "c", "gen": 1}]
        out = OR.analyze(self.POS, algos, led)
        row = [r for r in out["rows"] if r["symbol"] == "BTCUSDT"][0]
        assert "KİMLİK-UYUŞMAZ" in row["bulgu"]

    def test_clean_verdict(self):
        algos = {"BTCUSDT": [{"algoId": "9", "algoStatus": "NEW",
                              "orderType": "STOP_MARKET"}],
                 "SOLUSDT": [{"algoId": "8", "algoStatus": "NEW",
                              "orderType": "STOP_MARKET"}]}
        led = [{"symbol": "BTCUSDT", "token": "t", "state": "PLACED",
                "algo_id": "9", "client_algo_id": "c", "gen": 1}]
        out = OR.analyze(self.POS, algos, led)
        # E3: final verdict sınıfı OK (final satır sözleşmesi).
        assert out["verdict"].startswith("VERDICT: OK")

    def test_zero_amt_not_position(self):
        out = OR.analyze(self.POS, {"BTCUSDT": [
            {"algoId": "1", "algoStatus": "NEW", "orderType": "STOP_MARKET"}],
            "SOLUSDT": [{"algoId": "2", "algoStatus": "NEW",
                         "orderType": "STOP_MARKET"}]}, [])
        assert not any(r["symbol"] == "FLATXUSDT" and r["bulgu"] != "-"
                       for r in out["rows"])

    def test_a1_doctrine_verdict_only_from_algo_input(self):
        # A-1: normal openOrders'da STOP_MARKET görse bile analyze'e ALGO listesi
        # verilmedikçe hüküm SL_YOK'tur — analyze'in girdi sözleşmesi algo-only.
        out = OR.analyze(self.POS, {}, [])
        assert all("SL_YOK" in r["bulgu"] for r in out["rows"]
                   if r["pos_amt"])


class TestCliSafety:
    def test_main_requires_keys_not_pid(self, monkeypatch, capsys):
        # D1: --pid/pid-environ yolu öldü; anahtarlar BINANCE_KEY/BINANCE_SECRET.
        monkeypatch.delenv("BINANCE_KEY", raising=False)
        monkeypatch.delenv("BINANCE_SECRET", raising=False)
        assert OR.main(["--db", "/nonexistent"]) == 2   # anahtar yok → erken çıkış
        out = capsys.readouterr()
        assert "BINANCE_KEY" in (out.err + out.out)
        assert "--pid" not in (out.err + out.out)
