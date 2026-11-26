"""B.1 İŞ 1 hedefli testleri — updateTime=0 alan-sözleşmesi semantiği.

GERÇEK KANIT TEMELİ (VPS REST probu, 2026-11-26):
  - positionRisk: 958 satırın 930'unda updateTime=0 (hiç işlem görmemiş
    semboller; ör. ALABUSDT flat satırı updateTime=0).
  - balance v3: 11 satırın 9'unda updateTime=0 (U, BFUSD, BNB, RWUSD,
    BTC, USD1, FDUSD, LDUSDT, ETH); USDT satırında gerçek updateTime.
  - account v2 üst-düzey updateTime dokümanda "Reserved, ignore" →
    güncellik kaynağı olarak OKUNMAZ.

SÖZLEŞME:
  - positionRisk satırı ve balance v3 satırı updateTime=0 → GEÇERLİ
    sentinel: ham 0 korunur; update_time_status=NO_SOURCE_TIMESTAMP;
    source_update_time_ms=None; REST alım zamanı YERİNE yazılmaz.
  - 0 gerçek Unix zamanı olarak YORUMLANMAZ.
  - Diğer zaman alanları (order/algo/account.positions/metadata) SIKI
    kalır: 0 → INVALID_TIMESTAMP (genelleme YOK).
  - Negatif/NaN/±inf/metin → fail-closed (önceki gibi).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_model import (  # noqa: E402
    InvalidFieldError, SnapshotMetadata, parse_balance_v3,
    parse_position_risk, parse_account_v2, parse_open_orders,
    parse_account_positions, NO_SOURCE_TIMESTAMP, SOURCE_TIMESTAMP,
    R_INVALID_TIMESTAMP,
)
from tests.test_canary_account_model import (  # noqa: E402
    FIX_ACCOUNT_V2, FIX_ACCOUNT_V3, FIX_OPEN_ORDERS, T_REQ, T_UPD, _meta,
)

# Gerçek VPS yanıtlarından türetilmiş, hassas bilgi İÇERMEYEN satırlar
# (değerler gerçek biçimi temsil eden nötr sayılardır).
REAL_PR_ZERO_ROW = {           # gerçek: ALABUSDT flat, updateTime=0
    "symbol": "ALABUSDT", "positionAmt": "0.00", "entryPrice": "0",
    "breakEvenPrice": "0", "markPrice": "0.1234",
    "unRealizedProfit": "0.00", "liquidationPrice": "0",
    "isolatedMargin": "0", "isolatedWallet": "0", "notional": "0",
    "initialMargin": "0", "maintMargin": "0",
    "positionInitialMargin": "0", "openOrderInitialMargin": "0",
    "leverage": "20", "marginType": "cross", "positionSide": "BOTH",
    "updateTime": 0,
}
REAL_BAL_ZERO_ROW = {          # gerçek: BFUSD satırı, updateTime=0
    "accountAlias": "xx", "asset": "BFUSD", "balance": "0",
    "crossWalletBalance": "0", "crossUnPnl": "0",
    "availableBalance": "0", "maxWithdrawAmount": "0",
    "marginAvailable": True, "updateTime": 0,
}
REAL_BAL_USDT_ROW = {          # gerçek: USDT satırı, gerçek updateTime
    "accountAlias": "xx", "asset": "USDT", "balance": "1000.0",
    "crossWalletBalance": "1000.0", "crossUnPnl": "0",
    "availableBalance": "900.0", "maxWithdrawAmount": "890.0",
    "marginAvailable": True, "updateTime": 1795704000000,
}


class ZeroSentinelTests(unittest.TestCase):

    def test_position_risk_zero_update_time_valid(self):
        snap = parse_position_risk([REAL_PR_ZERO_ROW],
                                   _meta("positionRisk", "pr"))
        e = snap.entries[0]
        self.assertEqual(e.updateTime, 0)               # ham 0 korunur
        self.assertEqual(e.update_time_status, NO_SOURCE_TIMESTAMP)
        self.assertIsNone(e.source_update_time_ms)      # gerçek zaman DEĞİL
        self.assertTrue(e.is_flat)

    def test_balance_zero_update_time_valid(self):
        snap = parse_balance_v3([REAL_BAL_ZERO_ROW, REAL_BAL_USDT_ROW],
                                _meta("balance", "bal"))
        bfusd, usdt = snap.entries
        self.assertEqual(bfusd.updateTime, 0)
        self.assertEqual(bfusd.update_time_status, NO_SOURCE_TIMESTAMP)
        self.assertIsNone(bfusd.source_update_time_ms)
        self.assertEqual(usdt.update_time_status, SOURCE_TIMESTAMP)
        self.assertEqual(usdt.source_update_time_ms, 1795704000000)

    def test_real_shaped_snapshot_parses(self):
        """Gerçek VPS biçiminde karışık envanter (sentinel + gerçek zaman)."""
        rows = [REAL_PR_ZERO_ROW,
                dict(REAL_PR_ZERO_ROW, symbol="BTCUSDT",
                     positionAmt="0.002", entryPrice="96000",
                     updateTime=1795704000000),
                dict(REAL_PR_ZERO_ROW, symbol="SNTUSDT"),
                dict(REAL_PR_ZERO_ROW, symbol="KORUUSDT")]
        snap = parse_position_risk(rows, _meta("positionRisk", "pr"))
        self.assertEqual(len(snap.entries), 4)
        statuses = [e.update_time_status for e in snap.entries]
        self.assertEqual(statuses.count(NO_SOURCE_TIMESTAMP), 3)
        self.assertEqual(statuses.count(SOURCE_TIMESTAMP), 1)

    def test_v2_account_top_level_update_time_not_read(self):
        """'Reserved, ignore' alanı — güncellik kaynağı olarak OKUNMAZ;
        0 olsa bile toplam parse'ı etkilemez ve metadata'ya yazılmaz."""
        fix = dict(FIX_ACCOUNT_V2, updateTime=0)
        meta = SnapshotMetadata(source_name="account", endpoint="v2")
        t = parse_account_v2(fix, meta)
        self.assertAlmostEqual(t.totalCrossWalletBalance, 3491.46)
        # parser yanıttan metadata'ya updateTime ENJEKTE ETMEZ (yerine koyma YOK)
        self.assertIsNone(t.metadata.source_update_time_ms)

    def test_rest_receive_time_not_substituted(self):
        meta = _meta("positionRisk", "pr")   # response_received_at_ms dolu
        snap = parse_position_risk([REAL_PR_ZERO_ROW], meta)
        e = snap.entries[0]
        # sentinel durumda None kalır; REST alım zamanı yazılmaz
        self.assertIsNone(e.source_update_time_ms)
        self.assertNotEqual(e.source_update_time_ms,
                            meta.response_received_at_ms)


class StillStrictTests(unittest.TestCase):
    """Genelleme YOK: diğer zaman alanları sıkı kalır."""

    def test_order_update_time_zero_still_invalid(self):
        fix = [dict(FIX_OPEN_ORDERS[0], updateTime=0)]
        with self.assertRaises(InvalidFieldError) as cm:
            parse_open_orders(fix, _meta("openOrders", "oo"))
        self.assertEqual(cm.exception.reason, R_INVALID_TIMESTAMP)

    def test_account_positions_update_time_zero_still_invalid(self):
        fix = dict(FIX_ACCOUNT_V3, positions=[{
            "symbol": "BTCUSDT", "positionSide": "BOTH",
            "positionAmt": "0.002", "leverage": "20",
            "entryPrice": "96000.0", "updateTime": 0, "isolated": False}])
        with self.assertRaises(InvalidFieldError) as cm:
            parse_account_positions(fix, _meta("account", "v3"))
        self.assertEqual(cm.exception.reason, R_INVALID_TIMESTAMP)

    def test_corrupt_values_still_fail_closed(self):
        for bad in (-1, "abc", float("nan"), float("inf"), ""):
            row = dict(REAL_PR_ZERO_ROW, updateTime=bad)
            with self.assertRaises(InvalidFieldError) as cm:
                parse_position_risk([row], _meta("positionRisk", "pr"))
            self.assertIn(cm.exception.reason,
                          (R_INVALID_TIMESTAMP, "MISSING_REQUIRED_FIELD",
                           "INVALID_NUMERIC_VALUE"))
        for bad in (-1, "abc"):
            row = dict(REAL_BAL_USDT_ROW, updateTime=bad)
            with self.assertRaises(InvalidFieldError):
                parse_balance_v3([row], _meta("balance", "bal"))

    def test_metadata_timestamps_still_strict(self):
        meta = SnapshotMetadata(source_name="x", endpoint="y",
                                request_started_at_ms=0)
        with self.assertRaises(InvalidFieldError):
            meta.validate()


if __name__ == "__main__":
    unittest.main()
