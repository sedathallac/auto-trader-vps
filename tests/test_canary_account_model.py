"""AŞAMA A testleri — canary_account_model (saf parser; AĞ ÇAĞRISI YOK).

Kapsam (GÖREV: Uygulama Aşaması A):
  - Gerçek Binance yanıt biçimlerine uygun fixture'lar
    (accountConfig / v2,v3 account / v3 balance / positionRisk /
     openOrders / openAlgoOrders; alan adları 2026-11-26 doküman
     doğrulamasına ve mevcut FakeExchange yanıt şekline göre).
  - Double-count koruması (5 vaka).
  - Fail-closed doğrulama (eksik/NaN/inf/negatif/geçersiz zaman).
  - Sıfır semantiği (gerçek sıfır ≠ eksik veri).
  - CROSS/ISOLATED ayrımı.
  - Multi-Assets Mode fail-closed.
  - Zaman üstverisi ve atomik-olmama.
  - Determinizm.

YENİ KODUN testleridir; mevcut testlerin kapsamını tekrarlamaz
(canary_live/canary_risk/canary_liquidation testlerine dokunulmaz).
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_model import (  # noqa: E402
    AccountSnapshot, AccountConfigSnapshot, AccountTotals, BalanceSnapshot,
    PositionSnapshot, OpenOrderSnapshot, OpenAlgoOrderSnapshot,
    SnapshotMetadata, SnapshotValidationResult,
    InvalidFieldError, FieldContract, FIELD_CONTRACTS, field_contract,
    parse_account_config, parse_balance_v3, parse_account_v2,
    parse_account_v3, parse_account_positions, parse_position_risk,
    parse_open_orders, parse_open_algo_orders, build_account_snapshot,
    reconcile_positions_vs_totals, max_source_age_ms,
    SCOPE_ACCOUNT_ALL, SCOPE_ACCOUNT_CROSS, SCOPE_POSITION,
    R_MISSING_FIELD, R_INVALID_NUMERIC, R_INVALID_NEGATIVE,
    R_INVALID_TIMESTAMP, R_INVALID_MARGIN_TYPE, R_MULTI_ASSETS_UNSUPPORTED,
    DOC_VERIFIED, DOC_ASSUMED, EP_V2_ACCOUNT, EP_V3_ACCOUNT, EP_V3_BALANCE,
    EP_CONFIG, EP_POS_RISK, EP_OPEN_ORDERS, EP_OPEN_ALGOS,
    MARGIN_TYPE_CROSS, MARGIN_TYPE_ISOLATED,
)

T_REQ = 1_700_000_000_000       # örnek istemci ölçüm zamanları (ms)
T_RESP = T_REQ + 120
T_LOCAL = T_RESP + 5
T_UPD = T_REQ - 2_000           # borsanın payload updateTime'ı


def _meta(source, endpoint, upd=T_UPD):
    return SnapshotMetadata(source_name=source, endpoint=endpoint,
                            request_started_at_ms=T_REQ,
                            response_received_at_ms=T_RESP,
                            local_receive_time_ms=T_LOCAL,
                            source_update_time_ms=upd)


# ---------------------------------------------------------------------------
# Fixture'lar — gerçek yanıt biçimleri (DOC_VERIFIED alan adları)
# ---------------------------------------------------------------------------

FIX_CONFIG_OK = {"feeTier": 2, "multiAssetsMargin": False, "tradeGroupId": -1}
FIX_CONFIG_MULTI = {"feeTier": 2, "multiAssetsMargin": True, "tradeGroupId": -1}

FIX_BALANCE_V3 = [
    {"accountAlias": "SgsR", "asset": "USDT", "balance": "3491.46",
     "crossWalletBalance": "3491.46", "crossUnPnl": "-12.34",
     "availableBalance": "3001.10", "maxWithdrawAmount": "2988.76",
     "marginAvailable": True, "updateTime": T_UPD},
    {"accountAlias": "SgsR", "asset": "BNB", "balance": "0.5",
     "crossWalletBalance": "0.5", "crossUnPnl": "0",
     "availableBalance": "0.5", "maxWithdrawAmount": "0.5",
     "marginAvailable": True, "updateTime": T_UPD},
]

FIX_ACCOUNT_V2 = {
    "feeTier": 2, "feeBurn": False, "canTrade": True, "canDeposit": True,
    "canWithdraw": True, "updateTime": T_UPD,   # doküman: "Reserved, ignore"
    "multiAssetsMargin": False, "tradeGroupId": -1,
    "totalInitialMargin": "440.00",
    "totalMaintMargin": "36.00",
    "totalWalletBalance": "3491.46",
    "totalUnrealizedProfit": "-12.34",
    "totalMarginBalance": "3479.12",
    "totalPositionInitialMargin": "440.00",
    "totalOpenOrderInitialMargin": "10.00",
    "totalCrossWalletBalance": "3491.46",
    "totalCrossUnPnl": "-12.34",
    "availableBalance": "3001.10",
    "maxWithdrawAmount": "2988.76",
    "assets": [], "positions": [],
}

FIX_ACCOUNT_V3 = {
    "totalInitialMargin": "440.00",
    "totalMaintMargin": "36.00",
    "totalWalletBalance": "3491.46",
    "totalUnrealizedProfit": "-12.34",
    "totalMarginBalance": "3479.12",
    "totalPositionInitialMargin": "440.00",
    "totalOpenOrderInitialMargin": "10.00",
    "totalCrossWalletBalance": "3491.46",
    "totalCrossUnPnl": "-12.34",
    "availableBalance": "3001.10",
    "maxWithdrawAmount": "2988.76",
    "assets": [], "positions": [],
}

# Filtresiz positionRisk: cross + isolated + flat karışık
FIX_POSITION_RISK = [
    {"symbol": "BTCUSDT", "positionAmt": "0.002", "entryPrice": "96000.0",
     "breakEvenPrice": "96010.5", "markPrice": "95400.0",
     "unRealizedProfit": "-1.20", "liquidationPrice": "61234.5",
     "isolatedMargin": "0", "isolatedWallet": "0", "notional": "190.8",
     "initialMargin": "19.2", "maintMargin": "1.5",
     "positionInitialMargin": "19.2", "openOrderInitialMargin": "0",
     "leverage": "20", "marginType": "cross", "positionSide": "BOTH",
     "updateTime": T_UPD},
    {"symbol": "DOGEUSDT", "positionAmt": "-500", "entryPrice": "0.32",
     "breakEvenPrice": "0.32", "markPrice": "0.31",
     "unRealizedProfit": "-5.0", "liquidationPrice": "0.55",
     "isolatedMargin": "16.0", "isolatedWallet": "16.0", "notional": "-155.0",
     "initialMargin": "16.0", "maintMargin": "1.2",
     "positionInitialMargin": "16.0", "openOrderInitialMargin": "0",
     "leverage": "8", "marginType": "isolated", "positionSide": "BOTH",
     "updateTime": T_UPD},
    {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0",
     "breakEvenPrice": "0", "markPrice": "3500.0",
     "unRealizedProfit": "0", "liquidationPrice": "0",
     "isolatedMargin": "0", "isolatedWallet": "0", "notional": "0",
     "initialMargin": "0", "maintMargin": "0",
     "positionInitialMargin": "0", "openOrderInitialMargin": "0",
     "leverage": "16", "marginType": "cross", "positionSide": "BOTH",
     "updateTime": T_UPD},
]

FIX_OPEN_ORDERS = [
    {"orderId": 1001, "symbol": "SOLUSDT", "status": "NEW",
     "clientOrderId": "fcE5", "price": "220.0", "avgPrice": "0",
     "origQty": "0.5", "executedQty": "0", "cumQuote": "0",
     "timeInForce": "GTC", "type": "LIMIT", "side": "BUY",
     "stopPrice": "0", "updateTime": T_UPD, "workingType": "CONTRACT_PRICE",
     "reduceOnly": False, "closePosition": False, "positionSide": "BOTH"},
    {"orderId": 1002, "symbol": "BTCUSDT", "status": "NEW",
     "clientOrderId": "x-abc", "price": "0", "avgPrice": "0",
     "origQty": "0", "executedQty": "0", "cumQuote": "0",
     "timeInForce": "GTE_GTC", "type": "STOP_MARKET", "side": "SELL",
     "stopPrice": "61234.5", "updateTime": T_UPD,
     "workingType": "MARK_PRICE", "reduceOnly": False,
     "closePosition": True, "positionSide": "BOTH"},
]

FIX_OPEN_ALGOS = [
    {"algoId": 9001, "symbol": "BTCUSDT", "algoType": "CONDITIONAL",
     "side": "SELL", "algoStatus": "NEW", "clientAlgoId": "fcP1",
     "triggerPrice": "61234.5", "totalAmt": "0", "workingType": "MARK_PRICE",
     "positionSide": "BOTH", "updateTime": T_UPD},
]


def _full_snapshot(multi=None, positions=None):
    """Tüm bileşenleri geçerli fixture'lardan kurar (single-asset modu)."""
    cfg = parse_account_config(
        FIX_CONFIG_MULTI if multi else FIX_CONFIG_OK, _meta("config", EP_CONFIG))
    totals = parse_account_v2(FIX_ACCOUNT_V2, _meta("account", EP_V2_ACCOUNT))
    bal = parse_balance_v3(FIX_BALANCE_V3, _meta("balance", EP_V3_BALANCE))
    pos = parse_position_risk(
        positions if positions is not None else FIX_POSITION_RISK,
        _meta("positionRisk", EP_POS_RISK))
    oo = parse_open_orders(FIX_OPEN_ORDERS, _meta("openOrders", EP_OPEN_ORDERS))
    ao = parse_open_algo_orders(FIX_OPEN_ALGOS, _meta("algos", EP_OPEN_ALGOS))
    return build_account_snapshot(cfg, totals, bal, pos, oo, ao)


# ---------------------------------------------------------------------------
# 1) Fixture ayrıştırma
# ---------------------------------------------------------------------------

class FixtureParseTests(unittest.TestCase):

    def test_account_config(self):
        cfg = parse_account_config(FIX_CONFIG_OK, _meta("config", EP_CONFIG))
        self.assertEqual(cfg.feeTier, 2)
        self.assertFalse(cfg.multiAssetsMargin)
        self.assertEqual(cfg.tradeGroupId, -1)
        self.assertTrue(cfg.validate().valid_for_trading)

    def test_balance_v3(self):
        snap = parse_balance_v3(FIX_BALANCE_V3, _meta("balance", EP_V3_BALANCE))
        usdt = snap.usdt()
        self.assertAlmostEqual(usdt.crossWalletBalance, 3491.46)
        self.assertAlmostEqual(usdt.crossUnPnl, -12.34)      # negatif GEÇERLİ
        self.assertAlmostEqual(usdt.availableBalance, 3001.10)
        self.assertTrue(usdt.marginAvailable)
        self.assertEqual(usdt.updateTime, T_UPD)

    def test_account_v2_totals(self):
        t = parse_account_v2(FIX_ACCOUNT_V2, _meta("account", EP_V2_ACCOUNT))
        self.assertAlmostEqual(t.totalCrossWalletBalance, 3491.46)
        self.assertAlmostEqual(t.totalCrossUnPnl, -12.34)
        self.assertAlmostEqual(t.totalUnrealizedProfit, -12.34)
        self.assertFalse(t.multiAssetsMargin)
        self.assertTrue(t.canTrade)

    def test_account_v3_totals_no_flags(self):
        t = parse_account_v3(FIX_ACCOUNT_V3, _meta("account", EP_V3_ACCOUNT))
        self.assertIsNone(t.multiAssetsMargin)   # v3 üst-düzeyde YOK
        self.assertAlmostEqual(t.totalMaintMargin, 36.0)
        self.assertEqual(t.source, EP_V3_ACCOUNT)

    def test_position_risk_mixed(self):
        snap = parse_position_risk(FIX_POSITION_RISK,
                                   _meta("positionRisk", EP_POS_RISK))
        self.assertEqual(len(snap.entries), 3)
        btc = snap.by_symbol("BTCUSDT")
        self.assertTrue(btc.is_cross)
        self.assertAlmostEqual(btc.unRealizedProfit, -1.20)
        self.assertTrue(btc.has_liquidation_price)
        doge = snap.by_symbol("DOGEUSDT")
        self.assertTrue(doge.is_isolated)
        self.assertAlmostEqual(doge.positionAmt, -500.0)     # SHORT negatif
        eth = snap.by_symbol("ETHUSDT")
        self.assertTrue(eth.is_flat)
        self.assertFalse(eth.has_liquidation_price)          # liq=0 → yok

    def test_account_positions_isolated_bool(self):
        resp = {"positions": [
            {"symbol": "DOGEUSDT", "positionSide": "BOTH", "positionAmt": "-500",
             "unrealizedProfit": "-5", "isolated": True, "leverage": "8",
             "entryPrice": "0.32", "maintMargin": "1.2",
             "positionInitialMargin": "16", "openOrderInitialMargin": "0",
             "updateTime": T_UPD},
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.002",
             "unrealizedProfit": "-1.2", "isolated": False, "leverage": "20",
             "entryPrice": "96000", "maintMargin": "1.5",
             "positionInitialMargin": "19.2", "openOrderInitialMargin": "0",
             "updateTime": T_UPD},
        ]}
        snap = parse_account_positions(resp, _meta("account", EP_V2_ACCOUNT))
        self.assertEqual(snap.by_symbol("DOGEUSDT").marginType,
                         MARGIN_TYPE_ISOLATED)
        self.assertEqual(snap.by_symbol("BTCUSDT").marginType, MARGIN_TYPE_CROSS)
        # bu kaynakta markPrice/liquidationPrice YOKTUR
        self.assertIsNone(snap.by_symbol("BTCUSDT").markPrice)
        self.assertIsNone(snap.by_symbol("BTCUSDT").liquidationPrice)

    def test_open_orders(self):
        snap = parse_open_orders(FIX_OPEN_ORDERS,
                                 _meta("openOrders", EP_OPEN_ORDERS))
        self.assertEqual(len(snap.entries), 2)
        o = snap.entries[1]
        self.assertTrue(o.closePosition)
        self.assertAlmostEqual(o.stopPrice, 61234.5)
        self.assertAlmostEqual(o.price, 0.0)          # gerçek sıfır GEÇERLİ

    def test_open_algo_orders(self):
        snap = parse_open_algo_orders(FIX_OPEN_ALGOS,
                                      _meta("algos", EP_OPEN_ALGOS))
        a = snap.entries[0]
        self.assertEqual(a.algoId, 9001)
        self.assertEqual(a.clientAlgoId, "fcP1")
        self.assertAlmostEqual(a.triggerPrice, 61234.5)


# ---------------------------------------------------------------------------
# 2) Double-count koruması (5 zorunlu vaka)
# ---------------------------------------------------------------------------

class DoubleCountTests(unittest.TestCase):

    def test_t1_cross_upnl_not_added_on_top_of_totals(self):
        """totalCrossUnPnl ÜZERİNE pozisyon uPnL eklenmez.

        cross_margin_balance yalnız hesap-seviyesi iki alandan türetilir;
        pozisyon uPnL'leri (burada -1.20) sonuca ETKİ ETMEMELİDİR.
        """
        snap = _full_snapshot()
        t = snap.totals
        self.assertAlmostEqual(snap.cross_margin_balance,
                               t.totalCrossWalletBalance + t.totalCrossUnPnl)
        self.assertAlmostEqual(snap.cross_margin_balance, 3491.46 - 12.34)
        # pozisyon cross uPnL toplamı (-1.20) farklı olsa bile:
        rep = reconcile_positions_vs_totals(snap.positions, t)
        self.assertAlmostEqual(rep.cross_upnl_position_sum, -1.20)
        self.assertAlmostEqual(snap.cross_margin_balance, 3491.46 - 12.34)

    def test_t2_scope_sets_disjoint(self):
        """ACCOUNT_ALL ile ACCOUNT_CROSS alan kümeleri ayrıktır;
        totalWalletBalance + totalCrossWalletBalance toplamı yapılamaz."""
        all_fields = {k.split(".", 1)[1] for k, v in FIELD_CONTRACTS.items()
                      if k.startswith("AccountTotals.")
                      and v.scope == SCOPE_ACCOUNT_ALL}
        cross_fields = {k.split(".", 1)[1] for k, v in FIELD_CONTRACTS.items()
                        if k.startswith("AccountTotals.")
                        and v.scope == SCOPE_ACCOUNT_CROSS}
        self.assertIn("totalWalletBalance", all_fields)
        self.assertIn("totalCrossWalletBalance", cross_fields)
        self.assertEqual(all_fields & cross_fields, set())

    def test_t3_available_balance_passthrough_no_recomputation(self):
        """availableBalance borsa değeri BİREBİR korunur; bileşenlerden
        yeniden hesaplayan metot AccountTotals'ta YOKTUR."""
        snap = _full_snapshot()
        t = snap.totals
        # bileşen aritmetiği FARKLI bir değer verirdi:
        naive = (t.totalWalletBalance + t.totalUnrealizedProfit
                 - t.totalInitialMargin)
        self.assertNotAlmostEqual(naive, t.availableBalance)
        self.assertAlmostEqual(t.availableBalance, 3001.10)
        recomputers = [n for n in dir(t)
                       if "computed" in n or "calc" in n or "derived" in n]
        self.assertEqual(recomputers, [])

    def test_t4_reconcile_is_diagnostic_only(self):
        """Uzlaştırma toplamları DEĞİŞTİRMEZ ve kapı kararı üRETMEZ."""
        snap = _full_snapshot()
        t_before = copy.deepcopy(snap.totals)
        rep = reconcile_positions_vs_totals(snap.positions, snap.totals)
        self.assertEqual(snap.totals, t_before)
        # sapma var (pozisyon −1.20 vs hesap −12.34) ama bu bir RED DEĞİL:
        self.assertAlmostEqual(rep.cross_upnl_abs_diff, 11.14)
        self.assertTrue(any("CROSS_UPNL_MISMATCH" in n for n in rep.notes))
        v = snap.validate()
        self.assertTrue(v.valid)
        self.assertNotIn(R_MULTI_ASSETS_UNSUPPORTED, v.reasons)
        # ve sapma cross_margin_balance'i değiştirmez:
        self.assertAlmostEqual(snap.cross_margin_balance,
                               t_before.totalCrossWalletBalance
                               + t_before.totalCrossUnPnl)

    def test_t5_isolated_never_enters_cross_reconciliation(self):
        """ISOLATED satırlar cross uzlaştırmasına GİRMEZ."""
        snap = _full_snapshot()
        rep = reconcile_positions_vs_totals(snap.positions, snap.totals)
        # DOGE (isolated, −5.0) ve ETH flat cross upnl toplamında YOK:
        self.assertAlmostEqual(rep.cross_upnl_position_sum, -1.20)
        self.assertEqual(rep.isolated_position_count, 1)
        self.assertEqual(rep.cross_position_count, 2)   # BTC + ETH(flat, cross)
        self.assertAlmostEqual(rep.isolated_upnl_sum, -5.0)
        # ayrıştırma tam bölüştürme:
        parts = snap.positions.cross_positions() + \
            snap.positions.isolated_positions()
        self.assertEqual(len(parts), len(snap.positions.entries))


# ---------------------------------------------------------------------------
# 3) Fail-closed doğrulama
# ---------------------------------------------------------------------------

class FailClosedTests(unittest.TestCase):

    def _assert_invalid(self, reason, fn, *args):
        with self.assertRaises(InvalidFieldError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.reason, reason)
        return cm.exception

    def test_missing_required_field(self):
        bad = dict(FIX_ACCOUNT_V2)
        del bad["totalCrossWalletBalance"]
        self._assert_invalid(R_MISSING_FIELD, parse_account_v2, bad,
                             _meta("account", EP_V2_ACCOUNT))

    def test_empty_string_numeric(self):
        bad = dict(FIX_ACCOUNT_V2, totalMaintMargin="")
        self._assert_invalid(R_MISSING_FIELD, parse_account_v2, bad,
                             _meta("account", EP_V2_ACCOUNT))

    def test_nan_rejected(self):
        bad = dict(FIX_ACCOUNT_V2, availableBalance="NaN")
        self._assert_invalid(R_INVALID_NUMERIC, parse_account_v2, bad,
                             _meta("account", EP_V2_ACCOUNT))

    def test_infinity_rejected(self):
        bad = dict(FIX_ACCOUNT_V2, totalWalletBalance="Infinity")
        self._assert_invalid(R_INVALID_NUMERIC, parse_account_v2, bad,
                             _meta("account", EP_V2_ACCOUNT))

    def test_negative_balance_rejected(self):
        bad = [dict(FIX_BALANCE_V3[0], crossWalletBalance="-1.0")]
        self._assert_invalid(R_INVALID_NEGATIVE, parse_balance_v3, bad,
                             _meta("balance", EP_V3_BALANCE))

    def test_negative_pnl_is_valid_but_negative_price_rejected(self):
        rows = [dict(FIX_POSITION_RISK[0], unRealizedProfit="-99.9",
                     entryPrice="-1.0")]
        self._assert_invalid(R_INVALID_NEGATIVE, parse_position_risk, rows,
                             _meta("positionRisk", EP_POS_RISK))
        rows = [dict(FIX_POSITION_RISK[0], unRealizedProfit="-99.9")]
        snap = parse_position_risk(rows, _meta("positionRisk", EP_POS_RISK))
        self.assertAlmostEqual(snap.entries[0].unRealizedProfit, -99.9)

    def test_zero_update_time_is_valid_sentinel(self):
        """B.1 İş 1 (2026-11-26 VPS gerçek kanıtı): positionRisk/balance
        satırı updateTime=0 → GEÇERLİ 'hiç güncellenmemiş' sentinel.
        (Eski sözleşme: geçersiz sayılırdı — gerçek veriyle değişti.)"""
        bal = parse_balance_v3([dict(FIX_BALANCE_V3[0], updateTime=0)],
                               _meta("balance", EP_V3_BALANCE))
        self.assertEqual(bal.entries[0].update_time_status,
                         "NO_SOURCE_TIMESTAMP")
        pr = parse_position_risk([dict(FIX_POSITION_RISK[0], updateTime=0)],
                                 _meta("positionRisk", EP_POS_RISK))
        self.assertEqual(pr.entries[0].update_time_status,
                         "NO_SOURCE_TIMESTAMP")
        # negatif hâlâ geçersiz
        self._assert_invalid(R_INVALID_TIMESTAMP, parse_balance_v3,
                             [dict(FIX_BALANCE_V3[0], updateTime=-5)],
                             _meta("balance", EP_V3_BALANCE))

    def test_invalid_margin_type(self):
        rows = [dict(FIX_POSITION_RISK[0], marginType="weird")]
        self._assert_invalid(R_INVALID_MARGIN_TYPE, parse_position_risk, rows,
                             _meta("positionRisk", EP_POS_RISK))

    def test_metadata_endpoint_required(self):
        with self.assertRaises(InvalidFieldError):
            parse_account_config(FIX_CONFIG_OK, SnapshotMetadata(
                source_name="config", endpoint=""))

    def test_metadata_bad_time(self):
        with self.assertRaises(InvalidFieldError):
            parse_account_config(FIX_CONFIG_OK, SnapshotMetadata(
                source_name="config", endpoint=EP_CONFIG,
                response_received_at_ms=0))

    def test_multi_assets_mode_unsupported(self):
        cfg = parse_account_config(FIX_CONFIG_MULTI, _meta("config", EP_CONFIG))
        v = cfg.validate()
        self.assertFalse(v.valid)
        self.assertIn(R_MULTI_ASSETS_UNSUPPORTED, v.reasons)

    def test_multi_assets_in_v2_account_also_blocks(self):
        cfg = parse_account_config(FIX_CONFIG_OK, _meta("config", EP_CONFIG))
        bad_totals = parse_account_v2(
            dict(FIX_ACCOUNT_V2, multiAssetsMargin=True),
            _meta("account", EP_V2_ACCOUNT))
        snap = build_account_snapshot(
            cfg, bad_totals,
            parse_balance_v3(FIX_BALANCE_V3, _meta("balance", EP_V3_BALANCE)),
            parse_position_risk(FIX_POSITION_RISK, _meta("p", EP_POS_RISK)),
            parse_open_orders(FIX_OPEN_ORDERS, _meta("o", EP_OPEN_ORDERS)),
            parse_open_algo_orders(FIX_OPEN_ALGOS, _meta("a", EP_OPEN_ALGOS)))
        v = snap.validate()
        self.assertFalse(v.valid_for_trading)
        self.assertTrue(any(r.startswith(R_MULTI_ASSETS_UNSUPPORTED)
                            for r in v.reasons))

    def test_empty_balance_list_rejected(self):
        self._assert_invalid(R_MISSING_FIELD, parse_balance_v3, [],
                             _meta("balance", EP_V3_BALANCE))

    def test_usdt_row_required(self):
        no_usdt = [dict(FIX_BALANCE_V3[1])]   # yalnız BNB
        snap = parse_balance_v3(no_usdt, _meta("balance", EP_V3_BALANCE))
        with self.assertRaises(InvalidFieldError):
            snap.usdt()


# ---------------------------------------------------------------------------
# 4) Sıfır semantiği (gerçek sıfır ≠ eksik)
# ---------------------------------------------------------------------------

class ZeroSemanticsTests(unittest.TestCase):

    def test_flat_position_zero_values_valid(self):
        """Flat satırda entryPrice=0, maintMargin=0, liq=0 GEÇERLİDİR."""
        snap = parse_position_risk([FIX_POSITION_RISK[2]],
                                   _meta("positionRisk", EP_POS_RISK))
        eth = snap.entries[0]
        self.assertTrue(eth.is_flat)
        self.assertAlmostEqual(eth.entryPrice, 0.0)
        self.assertAlmostEqual(eth.maintMargin, 0.0)
        self.assertAlmostEqual(eth.liquidationPrice, 0.0)

    def test_zero_wallet_balance_valid(self):
        rows = [dict(FIX_BALANCE_V3[0], asset="USDT", balance="0",
                     crossWalletBalance="0", availableBalance="0",
                     maxWithdrawAmount="0")]
        snap = parse_balance_v3(rows, _meta("balance", EP_V3_BALANCE))
        self.assertAlmostEqual(snap.usdt().crossWalletBalance, 0.0)

    def test_zero_leverage_invalid(self):
        rows = [dict(FIX_POSITION_RISK[0], leverage="0")]
        with self.assertRaises(InvalidFieldError):
            parse_position_risk(rows, _meta("positionRisk", EP_POS_RISK))

    def test_optional_missing_is_none_not_zero(self):
        rows = [dict(FIX_POSITION_RISK[0])]
        del rows[0]["isolatedWallet"]                # opsiyonel alan YOK
        snap = parse_position_risk(rows, _meta("positionRisk", EP_POS_RISK))
        self.assertIsNone(snap.entries[0].isolatedWallet)   # 0'a çevrilmedi


# ---------------------------------------------------------------------------
# 5) Zaman üstverisi ve atomik-olmama
# ---------------------------------------------------------------------------

class TimeMetadataTests(unittest.TestCase):

    def test_snapshot_is_never_atomic(self):
        snap = _full_snapshot()
        self.assertFalse(snap.atomic)

    def test_components_keep_own_metadata(self):
        snap = _full_snapshot()
        metas = {snap.config.metadata.endpoint, snap.totals.metadata.endpoint,
                 snap.balance.metadata.endpoint,
                 snap.positions.metadata.endpoint,
                 snap.open_orders.metadata.endpoint,
                 snap.open_algo_orders.metadata.endpoint}
        self.assertEqual(len(metas), 6)   # altı ayrı kaynak, altı ayrı zaman

    def test_max_source_age(self):
        snap = _full_snapshot()
        now = T_UPD + 5_000
        self.assertEqual(max_source_age_ms(snap, now), 5_000)

    def test_max_source_age_none_when_no_update_times(self):
        # hiçbir bileşende source_update_time yok → None (tahmin ÜRETİLMEZ)
        cfg = parse_account_config(FIX_CONFIG_OK, SnapshotMetadata(
            source_name="config", endpoint=EP_CONFIG))
        self.assertIsNone(max_source_age_ms(
            build_account_snapshot(cfg, None, None, None, None, None),
            T_REQ))

    def test_max_source_age_invalid_now(self):
        with self.assertRaises(InvalidFieldError):
            max_source_age_ms(_full_snapshot(), 0)

    def test_thresholds_not_hardcoded(self):
        """Kalibre edilmemiş eşikler modülde sabit DEĞİLDİR (10 sn vb.)."""
        import canary_account_model as m
        import inspect
        src = inspect.getsource(m)
        for forbidden in ("STALE", "TOLERANCE", "THRESHOLD", "MAX_AGE"):
            self.assertNotIn(forbidden, src,
                             f"kalibre edilmemiş eşik sabiti: {forbidden}")


# ---------------------------------------------------------------------------
# 6) Bileşim doğrulaması + determinizm
# ---------------------------------------------------------------------------

class ComposeAndDeterminismTests(unittest.TestCase):

    def test_full_snapshot_valid(self):
        v = _full_snapshot().validate()
        self.assertTrue(v.valid)
        self.assertTrue(v.valid_for_trading)

    def test_missing_component_blocks(self):
        snap = build_account_snapshot(
            parse_account_config(FIX_CONFIG_OK, _meta("c", EP_CONFIG)),
            parse_account_v2(FIX_ACCOUNT_V2, _meta("a", EP_V2_ACCOUNT)),
            parse_balance_v3(FIX_BALANCE_V3, _meta("b", EP_V3_BALANCE)),
            parse_position_risk(FIX_POSITION_RISK, _meta("p", EP_POS_RISK)),
            parse_open_orders(FIX_OPEN_ORDERS, _meta("o", EP_OPEN_ORDERS)),
            None)   # algo envanteri YOK
        v = snap.validate()
        self.assertFalse(v.valid_for_trading)
        self.assertIn(R_MISSING_FIELD + ":open_algo_orders", v.reasons)

    def test_cross_source_warning(self):
        """v2 toplam + v3 balance karışımı uyarı üretir (kapı değil)."""
        snap = build_account_snapshot(
            parse_account_config(FIX_CONFIG_OK, _meta("c", EP_CONFIG)),
            parse_account_v2(FIX_ACCOUNT_V2, _meta("a", EP_V2_ACCOUNT)),
            parse_balance_v3(FIX_BALANCE_V3, _meta("b", EP_V3_BALANCE)),
            parse_position_risk(FIX_POSITION_RISK, _meta("p", EP_POS_RISK)),
            parse_open_orders(FIX_OPEN_ORDERS, _meta("o", EP_OPEN_ORDERS)),
            parse_open_algo_orders(FIX_OPEN_ALGOS, _meta("a2", EP_OPEN_ALGOS)))
        # totals zaten v2 — uyarı bekleniyor
        self.assertTrue(any("CROSS_SOURCE" in w for w in snap.validate().warnings))

    def test_parse_deterministic(self):
        a = parse_position_risk(FIX_POSITION_RISK, _meta("p", EP_POS_RISK))
        b = parse_position_risk(FIX_POSITION_RISK, _meta("p", EP_POS_RISK))
        self.assertEqual(a, b)
        self.assertEqual(_full_snapshot(), _full_snapshot())

    def test_field_contracts_lookup(self):
        fc = field_contract("AccountTotals.totalCrossUnPnl")
        self.assertEqual(fc.scope, SCOPE_ACCOUNT_CROSS)
        self.assertEqual(fc.doc_status, DOC_VERIFIED)
        fc2 = field_contract("PositionEntry.liquidationPrice")
        self.assertEqual(fc2.scope, SCOPE_POSITION)
        self.assertEqual(fc2.doc_status, DOC_ASSUMED)   # dürüst etiket
        with self.assertRaises(KeyError):
            field_contract("Bilinmeyen.alan")

    def test_module_has_no_network(self):
        """Saf katman sözleşmesi: requests/urllib/socket IMPORT EDİLMEZ."""
        import canary_account_model as m
        self.assertFalse(hasattr(m, "requests"))
        self.assertFalse(hasattr(m, "urllib"))
        self.assertFalse(hasattr(m, "socket"))

    def test_no_canary_live_integration(self):
        """Aşama A entegrasyonsuzdur: canary_live/canary_runner import EDİLMEZ."""
        import canary_account_model as m
        self.assertFalse(hasattr(m, "canary_live"))
        self.assertFalse(hasattr(m, "canary_runner"))


if __name__ == "__main__":
    unittest.main()
