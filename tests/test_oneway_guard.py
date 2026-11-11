"""Futures ONEWAY start guarantee (finding A9 / condition C).

The whole futures design — including the atomic position reservation (a44ea99,
symbol-keyed) — assumes ONEWAY netting (one position per symbol). If the account
is in HEDGE mode, a symbol can hold both a long and a short leg and the
symbol-level assumptions break. The bot never checked the real position mode
(invariant F7). This adds an explicit startup verification via
GET /fapi/v1/positionSide/dual: ONEWAY -> proceed; HEDGE or unverifiable ->
fail-closed (block new entries, CRITICAL alarm). HEDGE is intentionally NOT
half-supported (future work keyed on (symbol, positionSide)).

RED before position_mode()/_verify_oneway exist; GREEN after.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at


class _FakeBinance:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def position_mode(self):
        self.calls.append("position_mode")
        return self._resp


class TestVerifyOneway(unittest.TestCase):
    def test_oneway_allows(self):
        ok, mode = at._verify_oneway(_FakeBinance({"dualSidePosition": False}))
        self.assertTrue(ok)
        self.assertEqual(mode, "ONEWAY")

    def test_hedge_blocks(self):
        ok, mode = at._verify_oneway(_FakeBinance({"dualSidePosition": True}))
        self.assertFalse(ok)
        self.assertEqual(mode, "HEDGE")

    def test_string_bool_hedge_blocks(self):
        # Binance sometimes returns JSON booleans as strings "true"/"false".
        ok, mode = at._verify_oneway(_FakeBinance({"dualSidePosition": "true"}))
        self.assertFalse(ok)
        self.assertEqual(mode, "HEDGE")

    def test_oneway_string_false(self):
        ok, mode = at._verify_oneway(_FakeBinance({"dualSidePosition": "false"}))
        self.assertTrue(ok)
        self.assertEqual(mode, "ONEWAY")

    def test_api_error_fails_closed(self):
        ok, mode = at._verify_oneway(_FakeBinance({"code": -1, "msg": "boom"}))
        self.assertFalse(ok)
        self.assertEqual(mode, "UNKNOWN")

    def test_non_dict_fails_closed(self):
        ok, mode = at._verify_oneway(_FakeBinance(None))
        self.assertFalse(ok)
        self.assertEqual(mode, "UNKNOWN")


class TestPositionModeEndpoint(unittest.TestCase):
    def test_position_mode_hits_dual_endpoint(self):
        calls = {}

        class _B(at.BinanceAPI):
            def __init__(self):
                pass  # skip network/keys

            def _get(self, path, params=None, signed=False):
                calls["path"] = path
                calls["signed"] = signed
                return {"dualSidePosition": False}

        r = _B().position_mode()
        self.assertEqual(calls["path"], "/fapi/v1/positionSide/dual")
        self.assertTrue(calls["signed"])
        self.assertIn("dualSidePosition", r)


if __name__ == "__main__":
    unittest.main()
