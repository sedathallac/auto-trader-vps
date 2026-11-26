"""BLOK25 F-g — SL placement uses a FRESH positionRisk read (side / qty / entry),
never a stale snapshot; and the post-reservation correlation re-check closes the
sector-mix TOCTOU window.

Root cause (BLOK23 F-g): open_coin computed the SL trigger from the entry-time
price and placed it with the entry-time side/qty; ensure_sl_coverage placed from
its top-of-function snapshot. A flip / partial close / drift between the read
and the placement left a mis-sized or wrong-side SL (TOCTOU). The correlation
check also ran once, long before the create POST.

Layers:
  1. ensure_sl_coverage: fresh read unavailable -> NO SL; side mismatch -> NO SL;
     fresh read OK -> SL placed with the FRESH qty/entry.
  2. open_coin: the SL trigger/qty/side come from the fresh exchange position
     (entry-time values differ -> fresh values win).
  3. open_coin: no verifiable live position -> NO blind SL.
  4. Correlation re-check after the slot reservation: a rejection releases the
     slot and no executor is created.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from auto_trader import ensure_sl_coverage
from position_reservation import PositionReserver

SYM = "SOLUSDT"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_db = auto_trader.SL_LEDGER_DB
        auto_trader.SL_LEDGER_DB = os.path.join(self.tmp, "risk_ledger.db")
        self._unprot = dict(auto_trader._OPEN_UNPROTECTED)
        self._flag = auto_trader._PROTECTION_UNVERIFIED
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._PROTECTION_UNVERIFIED = False
        self._vo = auto_trader._verify_oneway
        auto_trader._verify_oneway = lambda b: (True, "ONEWAY")
        self._atr = auto_trader.calculate_atr
        auto_trader.calculate_atr = lambda b, s, **k: 0.02
        self._sleep = auto_trader.time.sleep
        auto_trader.time.sleep = lambda *a, **k: None

    def tearDown(self):
        auto_trader.SL_LEDGER_DB = self._orig_db
        auto_trader._OPEN_UNPROTECTED.clear()
        auto_trader._OPEN_UNPROTECTED.update(self._unprot)
        auto_trader._PROTECTION_UNVERIFIED = self._flag
        auto_trader._verify_oneway = self._vo
        auto_trader.calculate_atr = self._atr
        auto_trader.time.sleep = self._sleep


class CoverB:
    """ensure_sl_coverage fake: an open LONG with NO live SL (SL_ABSENT) and a
    SCRIPTED position_risk sequence for the F-g fresh re-read.

    Call 1 (snapshot) returns `snapshot`; each later call pops the next entry of
    `fresh` (a list, an error dict, or an Exception)."""

    def __init__(self, snapshot, fresh):
        self.snapshot = snapshot
        self.fresh = list(fresh)
        self.placed = []
        self.reads = 0

    def position_risk(self):
        self.reads += 1
        if self.reads == 1:
            return self.snapshot
        v = self.fresh.pop(0) if self.fresh else self.snapshot
        if isinstance(v, Exception):
            raise v
        return v

    def filters(self, s):
        return {"LOT_SIZE": {"stepSize": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def position_mode(self):
        return {"dualSidePosition": False}

    def open_algo_orders_raw(self, s):
        return []                      # SL_ABSENT -> the placement path runs

    def place_algo_sl(self, s, trig, qty, side):
        self.placed.append((trig, qty, side))
        return {"algoId": 878098}


_LONG = [{"symbol": SYM, "positionAmt": "0.3", "entryPrice": "81.27"}]


class TestEnsureSlCoverageFreshRead(_Base):
    def test_fresh_ok_places_with_fresh_values(self):
        # the position GREW between the snapshot and the placement -> the SL is
        # sized with the fresh 0.5 (not the stale 0.3) and the fresh entry 82.0
        b = CoverB(_LONG, [[{"symbol": SYM, "positionAmt": "0.5",
                             "entryPrice": "82.00"}]])
        ensure_sl_coverage(b)
        self.assertEqual(len(b.placed), 1)
        trig, qty, side = b.placed[0]
        self.assertEqual(qty, 0.5)                     # FRESH qty
        self.assertEqual(side, 1)
        # sl_pct: atr 0.02 * atr_sl_multiplier, clamped — trigger derives from
        # the FRESH entry 82.00, not the stale 81.27
        from auto_trader import RISK_CONFIG
        sl_pct = min(max(0.02 * RISK_CONFIG["atr_sl_multiplier"],
                         RISK_CONFIG["sl_min_pct"]), RISK_CONFIG["sl_max_pct"])
        expected = round(round((82.00 * (1 - sl_pct)) / 0.01) * 0.01, 8)
        self.assertEqual(trig, expected)

    def test_fresh_read_outage_no_blind_sl(self):
        b = CoverB(_LONG, [TimeoutError("positionRisk read timeout")])
        ensure_sl_coverage(b)
        self.assertEqual(b.placed, [], "a blind SL was placed without a fresh read")

    def test_fresh_side_flip_no_wrong_side_sl(self):
        # the position FLIPPED to SHORT since the snapshot -> placing the LONG
        # SL now would be a wrong-side SL; must not place
        b = CoverB(_LONG, [[{"symbol": SYM, "positionAmt": "-0.2",
                             "entryPrice": "80.00"}]])
        ensure_sl_coverage(b)
        self.assertEqual(b.placed, [], "an SL was placed against a flipped position")

    def test_fresh_flat_no_sl(self):
        b = CoverB(_LONG, [[{"symbol": SYM, "positionAmt": "0",
                             "entryPrice": "0"}]])
        ensure_sl_coverage(b)
        self.assertEqual(b.placed, [])

    def test_fresh_error_dict_no_blind_sl(self):
        b = CoverB(_LONG, [{"code": -1001, "msg": "Internal error"}])
        ensure_sl_coverage(b)
        self.assertEqual(b.placed, [])


class TestOpenCoinFresh(_Base):
    def setUp(self):
        super().setUp()
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2
        self._orig = {}
        for n, v in [("check_liquidity", lambda b, s: (True, "ok")),
                     ("check_correlation_limit", lambda s, b: True),
                     ("decide_side", lambda c: (1, "LONG")),
                     ("calculate_position_size", lambda bal, c: (24.0, 1.0)),
                     ("add_blacklist", lambda *a, **k: None),
                     ("get_real_slippage", lambda b, s: 0.0)]:
            self._orig[n] = getattr(auto_trader, n)
            setattr(auto_trader, n, v)
        self._hb_offline = auto_trader.HB_OFFLINE
        auto_trader.HB_OFFLINE = False

    def tearDown(self):
        super().tearDown()
        for n, v in self._orig.items():
            setattr(auto_trader, n, v)
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max
        auto_trader.HB_OFFLINE = self._hb_offline

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "81.27", "quoteVolume": "1e9",
                "_score": 0.7, "priceChangePercent": "-2"}

    class B(CoverB):
        """open_coin path fake: entry price 81.27, but the LIVE exchange
        position (read fresh in open_coin) differs."""

        def __init__(self, live):
            # open_coin reads position_risk several times; always return `live`
            self.live = live
            super().__init__(_LONG, [])
            self.reads = 0

        def position_risk(self):
            self.reads += 1
            return [self.live] if isinstance(self.live, dict) else self.live

        def get_available_balance(self, a):
            return 1000.0

        def set_leverage(self, s, l):
            return {"leverage": l}

    class HB:
        def __init__(self):
            self.created = []

        def active_executors(self):
            return []

        def create_executor(self, *a, **k):
            self.created.append(a)
            return {"executor_id": "E1"}

        def get_executor(self, eid):
            return {"filled_amount_quote": 24.4, "error_count": 0}

    def test_sl_uses_fresh_side_qty_entry(self):
        # entry-time decision: LONG @ 81.27 for `amount`; the LIVE position is a
        # SHORT 0.2 @ 80.00 (flip since) -> the SL must protect the FRESH state
        live = {"symbol": SYM, "positionAmt": "-0.2", "entryPrice": "80.00"}
        b = self.B(live)
        auto_trader.open_coin(b, self.HB(), self._coin(), risk=None,
                              reserver=None, coordinator=None)
        self.assertEqual(len(b.placed), 1)
        trig, qty, side = b.placed[0]
        self.assertEqual(side, 2)                      # FRESH side (SHORT)
        self.assertEqual(qty, 0.2)                     # FRESH qty
        from auto_trader import RISK_CONFIG
        sl_pct = min(max(0.02 * RISK_CONFIG["atr_sl_multiplier"],
                         RISK_CONFIG["sl_min_pct"]), RISK_CONFIG["sl_max_pct"])
        expected = round(round((80.00 * (1 + sl_pct)) / 0.01) * 0.01, 8)
        self.assertEqual(trig, expected)               # FRESH entry, SHORT side

    def test_no_live_position_no_blind_sl(self):
        # F-g: no verifiable live position -> never place a blind SL
        b = self.B({"code": -1001, "msg": "Internal error"})
        r = auto_trader.open_coin(b, self.HB(), self._coin(), risk=None,
                                  reserver=None, coordinator=None)
        self.assertEqual(b.placed, [], "a blind SL was placed with no live position")
        self.assertIn(SYM, auto_trader._OPEN_UNPROTECTED)


class TestCorrelationRecheck(_Base):
    """F-g: the post-reservation correlation re-check — a rejection releases the
    slot and no executor is created."""

    def setUp(self):
        super().setUp()
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 2
        self._orig = {}
        for n, v in [("check_liquidity", lambda b, s: (True, "ok")),
                     ("decide_side", lambda c: (1, "LONG")),
                     ("calculate_position_size", lambda bal, c: (24.0, 1.0)),
                     ("add_blacklist", lambda *a, **k: None),
                     ("get_real_slippage", lambda b, s: 0.0)]:
            self._orig[n] = getattr(auto_trader, n)
            setattr(auto_trader, n, v)
        self._hb_offline = auto_trader.HB_OFFLINE
        auto_trader.HB_OFFLINE = False
        # scripted correlation check: pass first, FAIL after the reservation
        self._corr = auto_trader.check_correlation_limit
        state = {"n": 0}

        def corr(s, b):
            state["n"] += 1
            return state["n"] < 2
        auto_trader.check_correlation_limit = corr

    def tearDown(self):
        super().tearDown()
        for n, v in self._orig.items():
            setattr(auto_trader, n, v)
        auto_trader.check_correlation_limit = self._corr
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max
        auto_trader.HB_OFFLINE = self._hb_offline

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "81.27", "quoteVolume": "1e9",
                "_score": 0.7, "priceChangePercent": "-2"}

    def test_rejection_releases_slot_no_executor(self):
        class B(TestOpenCoinFresh.B):
            def __init__(self):
                super().__init__({"symbol": SYM, "positionAmt": "0.3",
                                  "entryPrice": "81.27"})

        class HB(TestOpenCoinFresh.HB):
            pass

        db = os.path.join(self.tmp, "risk_ledger.db")
        reserver = PositionReserver(db, max_positions=2)
        reserver.init_schema()
        r = auto_trader.open_coin(B(), HB(), self._coin(), risk=None,
                                  reserver=reserver, coordinator=None)
        self.assertFalse(r)
        view = PositionReserver(db, max_positions=2)
        live = [x for x in view.active_reservations()
                if x["state"] != "RELEASED"]
        self.assertEqual(live, [], "the slot leaked after a correlation rejection")
        self.assertEqual(HB().created, [])              # (sanity: fresh HB unused)

    def test_no_executor_created_when_recheck_rejects(self):
        created = []

        class B(TestOpenCoinFresh.B):
            def __init__(self):
                super().__init__({"symbol": SYM, "positionAmt": "0.3",
                                  "entryPrice": "81.27"})

        class HB:
            def active_executors(self):
                return []

            def create_executor(self, *a, **k):
                created.append(a)
                return {"executor_id": "E1"}

            def get_executor(self, eid):
                return {"filled_amount_quote": 24.4, "error_count": 0}

        db = os.path.join(self.tmp, "risk_ledger.db")
        reserver = PositionReserver(db, max_positions=2)
        reserver.init_schema()
        r = auto_trader.open_coin(B(), HB(), self._coin(), risk=None,
                                  reserver=reserver, coordinator=None)
        self.assertFalse(r)
        self.assertEqual(created, [], "an executor was created despite the rejection")


if __name__ == "__main__":
    unittest.main()
