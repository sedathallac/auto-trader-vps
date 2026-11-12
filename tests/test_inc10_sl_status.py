"""Increment 10 — sl_status tri-state hardening (PRESENT / ABSENT / UNKNOWN).

The exchange SL check must never mistake an outage for 'no SL'. It reads the RAW
algo-order query (open_algo_orders_raw, which does NOT swallow an error into []):
  PRESENT - query ok + valid + a live protective STOP for THIS symbol/side.
  ABSENT  - query ok + valid + genuinely no SL -> UNPROTECTED.
  UNKNOWN - timeout / exception / API error dict / malformed / wrong symbol -> HALT.
Behavioural RED (revert to the swallowing open_algo_orders_for) is shown in the
delivery notes: the error-dict/timeout cases then return ABSENT / raise instead of
UNKNOWN. Here the required scenarios are asserted GREEN.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_adapter import (BinanceHBCloseAdapter, SL_PRESENT, SL_ABSENT, SL_UNKNOWN)
from migration import migrate_all, block_new_entries, UNKNOWN

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


def _stop(symbol="SOLUSDT", status="WORKING", typ="STOP_MARKET",
          reduce_only=True, position_side="BOTH"):
    return {"symbol": symbol, "type": typ, "algoStatus": status,
            "reduceOnly": reduce_only, "positionSide": position_side}


class RawBinance:
    """Only what sl_status needs: open_algo_orders_raw returning a canned value
    (or raising it, if it is an Exception)."""
    def __init__(self, raw):
        self._raw = raw

    def open_algo_orders_raw(self, symbol):
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _sl(raw, position_side=None):
    return BinanceHBCloseAdapter(RawBinance(raw), hb=None).sl_status(SYM, position_side)


class TestSlStatusTriState(unittest.TestCase):
    def test_success_sl_present(self):
        self.assertEqual(_sl([_stop()]), SL_PRESENT)

    def test_success_sl_present_orders_envelope(self):
        self.assertEqual(_sl({"orders": [_stop()]}), SL_PRESENT)

    def test_success_no_sl_absent(self):
        self.assertEqual(_sl([]), SL_ABSENT)

    def test_success_only_non_stop_absent(self):
        self.assertEqual(_sl([{"symbol": SYM, "type": "TAKE_PROFIT_MARKET",
                               "algoStatus": "WORKING"}]), SL_ABSENT)

    def test_success_stop_but_finished_absent(self):
        self.assertEqual(_sl([_stop(status="CANCELLED")]), SL_ABSENT)

    def test_timeout_unknown(self):
        import requests
        self.assertEqual(_sl(requests.exceptions.Timeout("read timeout")), SL_UNKNOWN)

    def test_exception_unknown(self):
        self.assertEqual(_sl(RuntimeError("boom")), SL_UNKNOWN)

    def test_api_error_dict_unknown(self):
        self.assertEqual(_sl({"code": -1021, "msg": "timestamp out of recvWindow"}),
                         SL_UNKNOWN)

    def test_nonempty_invalid_response_unknown(self):
        self.assertEqual(_sl("not-a-list"), SL_UNKNOWN)       # string payload
        self.assertEqual(_sl([42, "x"]), SL_UNKNOWN)          # non-dict elements

    def test_wrong_symbol_unknown(self):
        self.assertEqual(_sl([_stop(symbol="BTCUSDT")]), SL_UNKNOWN)

    # ONEWAY: positionSide BOTH matches regardless of the side asked.
    def test_oneway_both_matches(self):
        self.assertEqual(_sl([_stop(position_side="BOTH")], position_side="LONG"),
                         SL_PRESENT)

    # HEDGE: the SL must guard THIS side.
    def test_hedge_matching_side_present(self):
        self.assertEqual(_sl([_stop(position_side="SHORT")], position_side="SHORT"),
                         SL_PRESENT)

    def test_hedge_wrong_side_absent(self):
        # a LONG-side SL does not protect the SHORT position -> no match -> ABSENT
        self.assertEqual(_sl([_stop(position_side="LONG")], position_side="SHORT"),
                         SL_ABSENT)

    def test_non_reduce_only_stop_not_protective(self):
        self.assertEqual(_sl([_stop(reduce_only=False)]), SL_ABSENT)


class _FullBinance:
    """Enough of BinanceAPI for a migrate() end-to-end: an open SOL position, no
    pre-sent orders, and a configurable raw algo-order response."""
    def __init__(self, algo_raw):
        self._algo_raw = algo_raw
    def position_risk(self):
        return [{"symbol": SYM, "positionAmt": "-0.3"}]
    def position_mode(self):
        return {"dualSidePosition": False}  # ONEWAY
    def open_orders(self, symbol):
        return []
    def open_algo_orders_raw(self, symbol):
        if isinstance(self._algo_raw, Exception):
            raise self._algo_raw
        return self._algo_raw


class _NoHB:
    def active_executors(self):
        return []


class TestUnknownBlocksNewEntries(unittest.TestCase):
    def test_sl_unknown_migration_halts_and_blocks(self):
        # algo query returns an API error dict -> sl_status UNKNOWN -> migrate UNKNOWN
        adapter = BinanceHBCloseAdapter(_FullBinance({"code": -1, "msg": "err"}), _NoHB())
        results = migrate_all(adapter, [SYM])
        self.assertEqual(results[SYM].state, UNKNOWN)
        self.assertTrue(results[SYM].critical)
        self.assertTrue(block_new_entries(results))   # new entries blocked on HALT

    def test_sl_present_migration_allows(self):
        adapter = BinanceHBCloseAdapter(_FullBinance([_stop()]), _NoHB())
        results = migrate_all(adapter, [SYM])
        self.assertEqual(results[SYM].state, "MIGRATED")
        self.assertFalse(block_new_entries(results))


if __name__ == "__main__":
    unittest.main()
