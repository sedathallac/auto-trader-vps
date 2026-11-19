"""test_faz4_decision_archive.py — M-5 sözleşmesi (SAHTE transport).

Kilitler: (0) modül GERÇEK transporta dokunamaz (kaynak taraması);
(1) zorunlu alan + model_id sürüm kilidi; (2) dedup — event_id tekrar
YASAK, dosya değişmez; (3) olay-sırası decision≤intent, sembol içinde geriye
yok; (4) mid/spread bid/ask'tan türetilir, tutarsızlık RED; (5) shortfall
bilinen sayılarla (BUY/SELL işaretleri) + fill'sız satır ölçüme GİRMEZ;
(6) CostModel sürümü ve ÖLÇÜLEMEDİ statüleri.
"""
import ast
import json
import os

import pytest

import faz4_decision_archive as DA

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "faz4_decision_archive.py")


def _code_only(path):
    """Docstring'leri atarak gerçek kod metni (docstring'deki kelime tetiklemez)."""
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
               isinstance(body[0].value, ast.Constant) and \
               isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


def _intent(eid="e1", sym="AUSDT", side="BUY", d=1000, i=1500, **kw):
    r = dict(event_id=eid, model_id=DA.MODEL_ID, symbol=sym,
             strategy_id="s1", signal_id="sig1", phase="ENTRY",
             decision_ts_utc_ms=d, order_intent_ts_utc_ms=i, side=side,
             intended_qty=1.0, intended_order_type="MARKET",
             clientOrderId="c-" + eid)
    r.update(kw)
    return r


class TestNoRealTransport:
    def test_module_source_pure_local(self):
        code = _code_only(SRC)
        for bad in ("urllib", "requests", "http.client", "socket",
                    "/fapi/v1/order", "X-MBX-APIKEY"):
            assert bad not in code, f"modül gerçek transport izi taşıyor: {bad}"


class TestSchemaAndVersion:
    def test_missing_required_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        r = _intent()
        del r["signal_id"]
        with pytest.raises(DA.ArchiveError, match="zorunlu alan"):
            ar.append(r)
        assert not os.path.exists(str(tmp_path / "a.ndjson"))

    def test_wrong_model_id_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        with pytest.raises(DA.ArchiveError, match="model_id"):
            ar.append(_intent(model_id="cm-v0-eski"))

    def test_unknown_field_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        with pytest.raises(DA.ArchiveError, match="şema-dışı"):
            ar.append(_intent(kur_usdt=35000))


class TestAppendOnly:
    def test_dedup_event_id(self, tmp_path):
        p = str(tmp_path / "a.ndjson")
        ar = DA.DecisionArchive(p)
        ar.append(_intent())
        with pytest.raises(DA.ArchiveError, match="dedup"):
            ar.append(_intent(fill_price=99.0))     # "güncelleme" niyeti de RED
        assert len(open(p).read().splitlines()) == 1

    def test_reload_dedup_persists(self, tmp_path):
        p = str(tmp_path / "a.ndjson")
        DA.DecisionArchive(p).append(_intent())
        ar2 = DA.DecisionArchive(p)
        with pytest.raises(DA.ArchiveError, match="dedup"):
            ar2.append(_intent())


class TestEventOrdering:
    def test_intent_before_decision_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        with pytest.raises(DA.ArchiveError, match="olay-sırası"):
            ar.append(_intent(d=2000, i=1000))

    def test_symbol_time_goes_back_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        ar.append(_intent(eid="e1", i=5000))
        with pytest.raises(DA.ArchiveError, match="olay-sırası"):
            ar.append(_intent(eid="e2", i=4000))
        ar.append(_intent(eid="e3", sym="BUSDT", d=0, i=10))  # başka sembol serbest


class TestMidSpread:
    def test_derived_and_consistent(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        r = ar.append(_intent(best_bid=99.99, best_ask=100.01))
        assert abs(r["mid_price"] - 100.0) < 1e-12
        assert abs(r["spread_bps"] - 2.0) < 1e-9

    def test_inconsistent_verbatim_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        with pytest.raises(DA.ArchiveError, match="uyuşmuyor"):
            ar.append(_intent(best_bid=99.0, best_ask=101.0, mid_price=500.0))

    def test_crossed_book_rejected(self, tmp_path):
        ar = DA.DecisionArchive(str(tmp_path / "a.ndjson"))
        with pytest.raises(DA.ArchiveError, match="best_bid"):
            ar.append(_intent(best_bid=101.0, best_ask=99.0))


class TestShortfall:
    def _rows(self):
        return [dict(phase="ENTRY", side="BUY", mid_price=100.0,
                     fill_price=100.05, fill_qty=1.0),
                dict(phase="EXIT", side="SELL", mid_price=100.0,
                     fill_price=99.95, fill_qty=1.0),
                dict(phase="EXIT", side="SELL", mid_price=100.0,
                     fill_price=None, fill_qty=None)]   # fill'sız: ölçüm dışı

    def test_signs_and_usdt(self):
        s = DA.implementation_shortfall(self._rows())
        assert s["measured"] == 2 and s["n"] == 3
        assert abs(s["mean_bps"] - 5.0) < 1e-9          # ikisi de +5 bps maliyet
        assert abs(s["total_usdt"] - (0.05 + 0.05)) < 1e-9

    def test_no_rows_honest_unmeasurable(self):
        s = DA.implementation_shortfall([{"phase": "ENTRY", "side": "BUY"}])
        assert s["measured"] == 0 and "ÖLÇÜLEMEDİ" in s["status"]

    def test_phase_filter(self):
        s = DA.implementation_shortfall(self._rows(), phase="ENTRY")
        assert s["measured"] == 1


class TestCostModel:
    def test_version_and_status(self):
        cm = DA.CostModel()
        st = cm.measurement_status()
        assert st["model_id"] == DA.MODEL_ID
        assert "ÖLÇÜLÜ" in st["fee_per_side"]
        assert "ÖLÇÜLEMEDİ" in st["slippage"]
        assert "ÖLÇÜLEMEDİ" in st["latency"]
        assert cm.SLIPPAGE_BPS_PER_SIDE is None      # 0 uydurulmaz

    def test_fee_only_cost(self):
        # BLOK1 [FILL] çapraz kontrol: 2×0.05%×107.86 ≈ 6.4635/60
        assert abs(DA.CostModel().cost_per_round_usdt(107.86) - 0.10773) < 5e-4


class TestFakeTransport:
    def test_entry_exit_round_trip_archived(self, tmp_path):
        p = str(tmp_path / "a.ndjson")
        ar = DA.DecisionArchive(p)
        ft = DA.FakeTransport(ar, fills_by_client={
            "c-e1": dict(fill_id="f1", qty=1.0, price=100.05, maker=False,
                         latency_ms=42, exchange_order_id=9)},
            clock_ms=lambda: 1)
        r1 = ft.submit(_intent(best_bid=99.99, best_ask=100.01))
        assert r1["fill_price"] == 100.05 and r1["latency_ms"] == 42
        r2 = ft.submit(_intent(eid="e2", phase="EXIT", side="SELL",
                               d=9000, i=9500))     # fill senaryoda yok
        assert "fill_price" not in r2 or r2.get("fill_price") is None
        rows = [json.loads(x) for x in open(p)]
        assert [r["phase"] for r in rows] == ["ENTRY", "EXIT"]
        s = DA.implementation_shortfall(rows)
        assert s["measured"] == 1 and s["mean_bps"] > 0
        assert ft.sent == ["c-e1", "c-e2"]           # sahte transport kaydı


class TestRetroCLI:
    def test_fill_count_flattens_symbol_wrappers(self, tmp_path, capsys):
        f = tmp_path / "fills.ndjson"
        f.write_text(json.dumps(
            {"dataset": "userTrades_fromid_AUSDT",
             "rows": [{"id": 1}, {"id": 2}]}) + "\n" +
            json.dumps({"dataset": "allOrders_X", "rows": [{"x": 1}]}) + "\n")
        assert DA.main(["--fills", str(f)]) == 0
        out = capsys.readouterr().out
        assert "2 geçmiş fill" in out            # sembol-sarmalı düzleştirme
        assert "ÖLÇÜLEMEDİ" in out and DA.MODEL_ID in out
