"""B.1 İŞ 4/5 hedefli testleri — canary_account_risk_gate + check_order bağı.

Kapsam (yalnızca YENİ/değişen sözleşmeler):
  - unknown-owner pozisyon/emir → fail-closed BLOCK,
  - stale/eksik snapshot → BLOCK (veri yok → sıfır VARSAYILMAZ),
  - eşik konfigi yok → güvenli varsayılan BLOCK,
  - availableBalance (borsa değeri) + öngörülen marjin kontrolü,
  - hesap-seviyesi double-count önleme (audit alanları),
  - tam envanter (flat dahil; 8 sembol filtresi YOK),
  - check_order entegrasyonu: account_gate=None → davranış DEĞİŞMEZ;
    bağlıyken BLOCK → OrderRejected(ACCOUNT_GATE ...).
Ağ çağrısı YOK (FakeTransport).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_risk_gate import (  # noqa: E402
    AccountRiskGate, AccountGateDecision, AG_UNKNOWN_OWNER_POSITION,
    AG_UNKNOWN_OWNER_ORDER, AG_COMPONENT_MISSING,
    AG_FRESHNESS_THRESHOLD_UNCONFIGURED, AG_STALE_ACCOUNT_DATA,
    AG_MARGIN_BUFFER_UNCONFIGURED, AG_INSUFFICIENT_AVAILABLE_BALANCE,
    AG_PROJECTED_MAINT_MARGIN, AG_BRACKETS_MISSING,
    AG_MULTI_ASSETS_UNSUPPORTED, AG_LEDGER_EXCHANGE_MISMATCH,
    AG_EVALUATION_ERROR,
)
from canary_risk import RiskManager, OrderRejected  # noqa: E402
from tests.test_canary_account_adapter import (  # noqa: E402
    FakeTransport, _full_responses,
)
from tests.test_canary_account_model import T_REQ  # noqa: E402

# FakeTransport son yanıtı ~T_REQ+1050'de bitirir; PASS için yaş < eşik
NOW = T_REQ + 2_000
MAX_AGE = 5_000
BUFFER = 50.0

BRACKETS = {"BTCUSDT": [
    {"bracket": 1, "floor": 0.0, "cap": 50_000.0, "ratio": 0.004,
     "cum": 0.0, "max_leverage": 20},
    {"bracket": 2, "floor": 50_000.0, "cap": 250_000.0, "ratio": 0.005,
     "cum": 50.0, "max_leverage": 10},
]}


class FakeRm:
    """Ledger taklidi: open_positions + total_net_loss yeterli."""

    def __init__(self, syms=("BTCUSDT", "DOGEUSDT")):
        self.syms = tuple(syms)

    def open_positions(self):
        return [{"symbol": s} for s in self.syms]

    def total_net_loss(self, now_ms):
        return 0.0


def _pass_responses():
    """PASS senaryosu: C281 sahipliğinde emirler, tam bileşenler."""
    resp = _full_responses()
    resp["/fapi/v1/openOrders"] = [
        {"orderId": 1001, "symbol": "BTCUSDT", "status": "NEW",
         "clientOrderId": "C281x1", "price": "220.0", "avgPrice": "0",
         "origQty": "0.5", "executedQty": "0", "cumQuote": "0",
         "timeInForce": "GTC", "type": "LIMIT", "side": "BUY",
         "stopPrice": "0", "updateTime": T_REQ, "workingType": "CONTRACT_PRICE",
         "reduceOnly": False, "closePosition": False, "positionSide": "BOTH"}]
    resp["/fapi/v1/openAlgoOrders"] = [
        {"algoId": 9001, "symbol": "BTCUSDT", "algoType": "CONDITIONAL",
         "side": "SELL", "algoStatus": "NEW", "clientAlgoId": "C281a1",
         "triggerPrice": "61234.5", "totalAmt": "0", "workingType": "MARK_PRICE",
         "positionSide": "BOTH", "updateTime": T_REQ}]
    return resp


def _gate(responses=None, rm=None, **kw):
    g = AccountRiskGate(
        FakeTransport(responses if responses is not None else _pass_responses()),
        brackets_by_symbol=BRACKETS,
        max_age_ms=kw.pop("max_age_ms", MAX_AGE),
        margin_buffer_usdt=kw.pop("margin_buffer_usdt", BUFFER),
        clock_ms=lambda: NOW)
    g._rm = rm if rm is not None else FakeRm()
    return g


def _eval(g, symbol="BTCUSDT", notional=100.0):
    return g.evaluate(symbol=symbol, direction=1, notional=notional,
                      rm=g._rm, now_ms=NOW)


class PassAndAuditTests(unittest.TestCase):

    def test_full_pass(self):
        d = _eval(_gate())
        self.assertTrue(d.passed, d.reasons)
        self.assertEqual(d.reasons, ())
        a = d.audit
        # denetim kaydı gerçek hesap alanlarını taşır
        self.assertAlmostEqual(a["available_balance"], 3001.10)
        self.assertAlmostEqual(a["total_cross_wallet"], 3491.46)
        self.assertAlmostEqual(a["total_cross_un_pnl"], -12.34)
        self.assertEqual(a["n_non_flat_positions"], 2)
        self.assertEqual(a["n_open_orders"], 1)
        self.assertEqual(a["n_open_algo_orders"], 1)
        self.assertAlmostEqual(a["candidate_im"], 100.0 / 20)
        self.assertAlmostEqual(a["candidate_mm"], 0.004 * 100.0)

    def test_double_count_prevention(self):
        """cross_margin_balance = wallet + cross uPnL (yalnız hesap-seviyesi);
        pozisyon uPnL'leri ÜSTÜNE EKLENMEZ; availableBalance hesaplanMAZ."""
        d = _eval(_gate())
        a = d.audit
        self.assertAlmostEqual(a["cross_margin_balance"],
                               3491.46 + (-12.34))
        # mevcut cross MM bottom-up (pozisyon satırlarından; toplama eklenmez)
        self.assertAlmostEqual(a["current_cross_maint"], 1.5 + 1.2)

    def test_full_inventory_no_symbol_filter(self):
        """Filtresiz envanter: flat ETHUSDT dahil 3 satır görünür."""
        d = _eval(_gate())
        self.assertEqual(d.audit["n_non_flat_positions"], 2)
        # ETHUSDT flat → sahiplik kontrolüne girmez (non-flat değil)


class BlockTests(unittest.TestCase):

    def test_unknown_owner_position(self):
        # ledger'da OLMAYAN sembol envanterde non-flat → BLOCK
        g = _gate(rm=FakeRm(syms=("BTCUSDT",)))   # DOGE ledger dışı
        d = _eval(g)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_UNKNOWN_OWNER_POSITION)
                            for r in d.reasons), d.reasons)

    def test_ledger_exchange_mismatch(self):
        # ledger'da VAR, borsada YOK → veri çelişkisi → BLOCK
        g = _gate(rm=FakeRm(syms=("BTCUSDT", "DOGEUSDT", "XRPUSDT")))
        d = _eval(g)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_LEDGER_EXCHANGE_MISMATCH)
                            for r in d.reasons), d.reasons)

    def test_unknown_owner_standard_order(self):
        resp = _pass_responses()
        o = dict(resp["/fapi/v1/openOrders"][0], clientOrderId="foreign1")
        resp["/fapi/v1/openOrders"] = [o]
        d = _eval(_gate(responses=resp))
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_UNKNOWN_OWNER_ORDER)
                            for r in d.reasons), d.reasons)

    def test_unknown_owner_algo_order(self):
        resp = _pass_responses()
        o = dict(resp["/fapi/v1/openAlgoOrders"][0], clientAlgoId="zzz9")
        resp["/fapi/v1/openAlgoOrders"] = [o]
        d = _eval(_gate(responses=resp))
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(f"{AG_UNKNOWN_OWNER_ORDER}:algo")
                            for r in d.reasons), d.reasons)

    def test_missing_component_means_block_not_zero(self):
        # openAlgoOrders alınamadı → sıfır algo emri VARSAYILMAZ
        resp = _pass_responses()
        t = FakeTransport(resp)
        t.fail = {"/fapi/v1/openAlgoOrders"}
        g = AccountRiskGate(t, brackets_by_symbol=BRACKETS,
                            max_age_ms=MAX_AGE, margin_buffer_usdt=BUFFER,
                            clock_ms=lambda: NOW)
        d = g.evaluate(symbol="BTCUSDT", direction=1, notional=100.0,
                       rm=FakeRm(), now_ms=NOW)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_COMPONENT_MISSING)
                            for r in d.reasons), d.reasons)

    def test_freshness_threshold_unconfigured_blocks(self):
        g = _gate(max_age_ms=None)
        d = _eval(g)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_FRESHNESS_THRESHOLD_UNCONFIGURED)
                            for r in d.reasons), d.reasons)

    def test_stale_data_blocks(self):
        g = _gate(max_age_ms=100)     # yaş ~950ms > 100ms
        d = _eval(g)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_STALE_ACCOUNT_DATA)
                            for r in d.reasons), d.reasons)

    def test_margin_buffer_unconfigured_blocks(self):
        g = _gate(margin_buffer_usdt=None)
        d = _eval(g)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_MARGIN_BUFFER_UNCONFIGURED)
                            for r in d.reasons), d.reasons)

    def test_insufficient_available_balance(self):
        # availableBalance 3.0 → IM 5 + tampon 50 karşılanamıyor
        resp = _pass_responses()
        resp["/fapi/v3/balance"] = [
            {"accountAlias": "S", "asset": "USDT", "balance": "3.0",
             "crossWalletBalance": "3491.46", "crossUnPnl": "-12.34",
             "availableBalance": "3.0", "maxWithdrawAmount": "3.0",
             "marginAvailable": True, "updateTime": T_REQ}]
        d = _eval(_gate(responses=resp))
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_INSUFFICIENT_AVAILABLE_BALANCE)
                            for r in d.reasons), d.reasons)

    def test_projected_maint_margin_breach(self):
        # cross cüzdanı küçük → öngörülen MM tamponu aşar
        resp = _pass_responses()
        resp["/fapi/v2/account"] = dict(
            resp["/fapi/v2/account"],
            totalCrossWalletBalance="10.0", totalCrossUnPnl="0")
        resp["/fapi/v3/balance"] = [
            {"accountAlias": "S", "asset": "USDT", "balance": "10.0",
             "crossWalletBalance": "10.0", "crossUnPnl": "0",
             "availableBalance": "500.0", "maxWithdrawAmount": "10.0",
             "marginAvailable": True, "updateTime": T_REQ}]
        d = _eval(_gate(responses=resp))
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_PROJECTED_MAINT_MARGIN)
                            for r in d.reasons), d.reasons)

    def test_brackets_missing(self):
        g = AccountRiskGate(FakeTransport(_pass_responses()),
                            brackets_by_symbol={},   # BTCUSDT bracket yok
                            max_age_ms=MAX_AGE, margin_buffer_usdt=BUFFER,
                            clock_ms=lambda: NOW)
        d = g.evaluate(symbol="BTCUSDT", direction=1, notional=100.0,
                       rm=FakeRm(), now_ms=NOW)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_BRACKETS_MISSING)
                            for r in d.reasons), d.reasons)

    def test_multi_assets_blocks(self):
        resp = _pass_responses()
        resp["/fapi/v1/accountConfig"] = {"feeTier": 2,
                                          "multiAssetsMargin": True,
                                          "tradeGroupId": -1}
        resp["/fapi/v2/account"] = dict(resp["/fapi/v2/account"],
                                        multiAssetsMargin=True)
        d = _eval(_gate(responses=resp))
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_MULTI_ASSETS_UNSUPPORTED)
                            for r in d.reasons), d.reasons)

    def test_evaluation_error_blocks(self):
        class BrokenTransport:
            def signed_get(self, path, params=None):
                raise RuntimeError("ağ hatası")
        g = AccountRiskGate(BrokenTransport(), brackets_by_symbol=BRACKETS,
                            max_age_ms=MAX_AGE, margin_buffer_usdt=BUFFER,
                            clock_ms=lambda: NOW)
        d = g.evaluate(symbol="BTCUSDT", direction=1, notional=100.0,
                       rm=FakeRm(), now_ms=NOW)
        self.assertFalse(d.passed)
        self.assertTrue(any(r.startswith(AG_EVALUATION_ERROR)
                            for r in d.reasons), d.reasons)

    def test_update_time_zero_rows_pass_through_gate(self):
        """İş 1 entegrasyonu: updateTime=0 satırlar (gerçek VPS biçimi)
        kapıya tam envanter olarak ulaşır — parse RED'i YOK."""
        resp = _pass_responses()
        zero_row = dict(resp["/fapi/v2/positionRisk"][2],
                        symbol="ALABUSDT", updateTime=0)
        resp["/fapi/v2/positionRisk"] = resp["/fapi/v2/positionRisk"] + [zero_row]
        d = _eval(_gate(responses=resp))
        self.assertTrue(d.passed, d.reasons)   # flat satır engel üretmez


class CheckOrderIntegrationTests(unittest.TestCase):
    """canary_risk.check_order ↔ account_gate bağı."""

    def _rm(self, tmpdir):
        import pathlib
        db = str(pathlib.Path(tmpdir) / "gate.db")
        return RiskManager(db)

    def test_gate_blocks_raise_order_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rm = self._rm(td)
            rm.account_gate = _gate(max_age_ms=None)  # eşik yok → BLOCK
            with self.assertRaises(OrderRejected) as cm:
                rm.check_order("BTCUSDT", 1, 100.0, now_ms=NOW)
            self.assertIn("ACCOUNT_GATE", str(cm.exception))
            self.assertIn(AG_FRESHNESS_THRESHOLD_UNCONFIGURED,
                          str(cm.exception))

    def test_no_gate_unchanged_behavior(self):
        """account_gate=None → mevcut kapı davranışı DEĞİŞMEZ (5/10 bütçe,
        notional sınırları aynen; geçerli emir hâlâ geçer)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rm = self._rm(td)
            self.assertIsNone(rm.account_gate)
            rm.update_mark("BTCUSDT", 96000.0, NOW)
            res = rm.check_order("BTCUSDT", 1, 100.0, now_ms=NOW)
            self.assertTrue(res["allowed"])
            # bütçe sınırı değişmedi: -10 toplam zarar → HALT
            rm._halt("test")
            with self.assertRaises(OrderRejected):
                rm.check_order("BTCUSDT", 1, 100.0, now_ms=NOW)

    def test_passing_gate_then_budget_checks(self):
        """Kapı PASS olsa bile mevcut 5/10 bütçe kontrolleri SONRA da
        çalışır (katmanlı güvenlik). Gerçek rm'nin ledger'i boş olduğundan
        PASS senaryosu flat-envanter (borsa da pozisyon yok) kullanır."""
        import tempfile
        resp = _pass_responses()
        resp["/fapi/v2/positionRisk"] = [
            dict(r, positionAmt="0", maintMargin="0")   # hepsi flat
            for r in resp["/fapi/v2/positionRisk"]]
        with tempfile.TemporaryDirectory() as td:
            rm = self._rm(td)
            rm.account_gate = _gate(responses=resp)  # PASS üreten konfig
            rm.update_mark("BTCUSDT", 96000.0, NOW)
            res = rm.check_order("BTCUSDT", 1, 100.0, now_ms=NOW)
            self.assertTrue(res["allowed"])
            # notional sınırı (100) aşıldığında yine RED (mevcut politika)
            with self.assertRaises(OrderRejected):
                rm.check_order("BTCUSDT", 1, 150.0, now_ms=NOW)


if __name__ == "__main__":
    unittest.main()
