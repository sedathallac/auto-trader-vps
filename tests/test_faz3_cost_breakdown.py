"""test_faz3_cost_breakdown.py — BLOK 1 İŞ 3 ayrıştırma/mutabakat sözleşmeleri.

Sentetik mini-veri ile: (1) breakdown aritmetiği (net = gross − fee + funding);
(2) reconcile çift-kaynak çaprazı ve baseline tolansı; (3) verdict eşikleri;
(4) [ALGO] slippage işareti (SELL: gerçekleşen<tetik ⇒ eksi). Gerçek veriye
dokunmaz — scriptin kendisi CLI ile gerçek veride ayrıca koşullanır.
"""
import faz3_cost_breakdown as FC


def _fill(sym, qty, price, rp, com, fid):
    return {"symbol": sym, "id": fid, "orderId": fid, "side": "SELL",
            "price": str(price), "qty": str(qty), "realizedPnl": str(rp),
            "commission": str(com), "time": "1794902418791",
            "buyer": False, "maker": False}


FILLS = [_fill("AAAUSDT", 1, 100.0, -0.5, 0.05, 1),
         _fill("AAAUSDT", 1, 102.0, +0.2, 0.05, 2),
         _fill("BBBUSDT", 2, 50.0, -0.1, 0.02, 3)]
INC = [{"symbol": "AAAUSDT", "incomeType": "FUNDING_FEE", "income": "-0.01",
        "time": "1794902500000"},
       {"symbol": "", "incomeType": "TRANSFER", "income": "100", "time": "1"},
       {"symbol": "AAAUSDT", "incomeType": "REALIZED_PNL", "income": "-0.3",
        "time": "2"},
       {"symbol": "BBBUSDT", "incomeType": "REALIZED_PNL", "income": "-0.1",
        "time": "3"},
       {"symbol": "AAAUSDT", "incomeType": "COMMISSION", "income": "-0.1",
        "time": "4"},
       {"symbol": "BBBUSDT", "incomeType": "COMMISSION", "income": "-0.02",
        "time": "5"}]


class TestBreakdown:
    def test_per_symbol_arithmetic(self):
        per, n_fund = FC.breakdown(FILLS, INC)
        a = per["AAAUSDT"]
        assert a["n_fills"] == 2 and abs(a["notional"] - 202.0) < 1e-9
        assert abs(a["gross"] - (-0.3)) < 1e-12
        assert abs(a["fee"] - 0.10) < 1e-12
        assert abs(a["funding"] - (-0.01)) < 1e-12
        assert abs(a["net"] - (a["gross"] - a["fee"] + a["funding"])) < 1e-12
        assert n_fund == 1

    def test_transfer_never_counts(self):
        per, _ = FC.breakdown(FILLS, INC + [
            {"symbol": "AAAUSDT", "incomeType": "TRANSFER", "income": "999",
             "time": "9"}])
        assert per["AAAUSDT"]["funding"] == -0.01   # TRANSFER funding değildir


class TestReconcile:
    def test_two_source_exact(self):
        rec = FC.reconcile(FILLS, INC, baseline=-0.53, tol=0.01)
        # net = −0.4(rp) − 0.12(fee) − 0.01(fund) = −0.53
        assert rec["ok"] is True
        assert abs(rec["net"] - (-0.53)) < 1e-9
        assert rec["checks"]["rp_fills_vs_income"] < 1e-12
        assert rec["checks"]["fee_fills_vs_income"] < 1e-12

    def test_baseline_deviation_fails_loud(self):
        rec = FC.reconcile(FILLS, INC, baseline=-9.9, tol=0.01)
        assert rec["ok"] is False                    # yuvarlama yok, SAPMA var


class TestVerdict:
    def test_thresholds(self):
        assert FC.verdict(0.0)[0] == "İNFAZ"                  # gross ≈ 0
        assert FC.verdict(-0.0004)[0] == "İNFAZ"              # −0.04%
        assert FC.verdict(-0.0020)[0] == "SİNYAL"             # −0.20%
        assert FC.verdict(-0.0010)[0] == "KARIŞIK"            # eşikler arası


class TestAlgoSlippage:
    def test_sell_trigger_shortfall_sign(self):
        r = {"side": "SELL", "triggerPrice": "100", "actualPrice": "99.9",
             "actualQty": "10", "algoStatus": "FINISHED", "symbol": "X"}
        assert abs(FC.algo_shortfall_usdt(r) - (-1.0)) < 1e-12
