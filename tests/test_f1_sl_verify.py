"""F-1 fix — open_coin's inline SL verification uses the mode-aware tri-state
sl_status (inc10.1), not a bare algoId+qty>=95% check.

The old inline check accepted ANY algo order whose algoId matched and qty>=95%,
ignoring side / positionSide / reduceOnly|closePosition / status — so an SL that
does NOT actually protect the position (e.g. wrong side) was treated as verified
and the new position was kept UNPROTECTED. The fix requires sl_status == PRESENT
(full mode-aware coverage); ABSENT/UNKNOWN -> emergency close via the coordinator.
Real open_coin call chain; only the Binance/HB boundary is faked.
"""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from close_adapter import BinanceHBCloseAdapter, SL_PRESENT
from close_coordinator import CloseCoordinator
from close_intent import CloseIntentStore
import tempfile

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


def _algo(side="SELL", typ="STOP_MARKET", status="NEW", reduce_only="true",
          position_side="BOTH", quantity="0.6"):
    return {"symbol": SYM, "algoId": 123, "type": typ, "algoStatus": status,
            "side": side, "positionSide": position_side, "reduceOnly": reduce_only,
            "quantity": quantity}


class FakeBinance:
    def __init__(self, algo_raw, algo_for=None):
        self.amt = Decimal("0.6")
        self._algo_raw = algo_raw
        self._algo_for = algo_for if algo_for is not None else [{"algoId": 123,
                                                                 "quantity": "0.6"}]
        self.order_posts = []
        self._opened = False

    def filters(self, symbol):
        return {"LOT_SIZE": {"stepSize": "0.1"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, asset):
        return 1000.0

    def order_book(self, symbol, limit=20):
        # FAZ11 kalite kapısı: 2.5 bps spread, ~800k USDT derinlik.
        return {"bids": [["79.99", "5000"]], "asks": [["80.01", "5000"]]}

    def set_leverage(self, symbol, lev):
        return {"leverage": lev}

    def place_algo_sl(self, symbol, trig, qty, side):
        return {"algoId": 123}

    def open_algo_orders_for(self, symbol):          # old inline check source
        return list(self._algo_for)

    def open_algo_orders_raw(self, symbol):          # sl_status source
        if isinstance(self._algo_raw, Exception):
            raise self._algo_raw
        return list(self._algo_raw)

    def position_mode(self):
        return {"dualSidePosition": False}

    def position_risk(self):
        if not getattr(self, '_opened', False):
            return []
        return [{"symbol": SYM, "positionAmt": str(self.amt), "entryPrice": "80"}]

    def _simulate_fill(self):
        self._opened = True
    def account_snapshot(self):
        # FAZ16 (4331dd1): gerçek giriş yolu artık TEK snapshot ister — fixture katmanı.
        return auto_trader.AccountSnapshot(equity=1000.0, available_balance=1000.0,
                                           can_trade=True, raw={})

    def open_orders(self, symbol):
        return []

    def _post(self, path, params, method="POST"):
        if path == "/fapi/v1/order" and method == "POST":
            self.order_posts.append(dict(params))
            if str(params.get("reduceOnly", "")).lower() == "true":
                self.amt = Decimal("0")
            return {"orderId": 1, "status": "FILLED"}
        return {"orderId": 1, "status": "NEW"}


class FakeHB:
    def __init__(self):
        self.created = []
        self.stopped = []

    def active_executors(self):
        if not self.created:
            return []
        return [{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
                 "custom_info": {"order_ids": []}}]

    def create_executor(self, symbol, amount, lev, sl, tp, tl, side=1):
        self.created.append(symbol)
        return {"executor_id": "E1"}

    def get_executor(self, eid):
        return {"filled_amount_quote": 100.0, "error_count": 0}

    def stop_executor(self, eid, keep_position=True):
        self.stopped.append((eid, keep_position))
        return {"status": "ok"}


class _RiskOK:
    """FAZ16 (4331dd1) giriş kapısının beklediği minimal RiskEngine testi.
    Üretim kapısına dokunmaz; sadece 'sağlıklı, halt'sız, onaylayan' motoru taklit eder."""
    is_halted = False
    halt_reason = ""
    daily_trades = 0
    consecutive_losses = 0
    current_drawdown = 0.0
    margin_usage = 0.0
    cfg = {"max_leverage": 20}

    def approve_trade(self, balance, slippage, take_profit_pct=None, equity=None,
                      max_position_pct=None):
        return 50.0, "approved"


class F1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 5
        self._orig = {}
        for name, val in [
            ("check_liquidity", lambda b, s: (True, "ok")),
            ("check_correlation_limit", lambda s, b: True),
            ("decide_side", lambda coin: (1, "LONG")),   # LONG -> protective side SELL
            ("calculate_atr", lambda b, s, interval="15m", period=14: 0.02),
            ("calculate_position_size", lambda bal, coin: (50.0, 1.0)),
        ]:
            self._orig[name] = getattr(auto_trader, name)
            setattr(auto_trader, name, val)
        self._orig_sleep = auto_trader.time.sleep
        auto_trader.time.sleep = lambda *a, **k: None
        # FAZ14p: dolum beklemesi tick'ler arası ilerler; testte bekleyen dolum
        # defteri izole edilir (gerçek ~/.pending_fills.json'a yazılmaz).
        self._orig_pf = auto_trader.PENDING_FILLS_JSON
        auto_trader.PENDING_FILLS_JSON = Path(self.tmp) / "pending_fills.json"
        auto_trader._PENDING_FILLS.clear()
        # FAZ16 giriş kapısının modül globali: test kirliliğini kanonik biçimde yalıt.
        _saved_health = auto_trader._entry_health_reasons()

        def _restore(saved):
            for k in list(auto_trader._entry_health_reasons()):
                auto_trader._entry_health_clear(k)
            for k, v in saved.items():
                auto_trader._entry_health_set(k, v)
        self.addCleanup(_restore, _saved_health)

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(auto_trader, name, val)
        auto_trader.time.sleep = self._orig_sleep
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max
        auto_trader.PENDING_FILLS_JSON = self._orig_pf
        auto_trader._PENDING_FILLS.clear()

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "80", "quoteVolume": "1000000",
                "_score": 0.5, "priceChangePercent": "2"}

    def _cc(self, binance, hb):
        store = CloseIntentStore(self.db)
        store.init_schema()
        return CloseCoordinator(BinanceHBCloseAdapter(binance, hb), intent_store=store)

    def _drive_fill(self, fb, hb, cc):
        """FAZ14p: open_coin bloklamaz; dolum sonraki tick'te işlenir."""
        fb._simulate_fill()
        return auto_trader._process_pending_fills(fb, hb, None, cc, None)


class TestF1(F1Base):
    # Wrong-side SL present (algoId+qty match) -> old check verifies, F-1 rejects
    # (ABSENT) -> emergency close via the coordinator (HB stopped, close sent).
    def test_wrong_side_sl_triggers_emergency_close(self):
        fb = FakeBinance(algo_raw=[_algo(side="BUY")])   # LONG needs SELL -> invalid
        hb = FakeHB()
        cc = self._cc(fb, hb)
        res = auto_trader.open_coin(fb, hb, self._coin(), risk=_RiskOK(), reserver=None,
                                    coordinator=cc)
        self.assertTrue(res)          # emir kabul edildi, dolum bekliyor
        out = self._drive_fill(fb, hb, cc)
        self.assertEqual(out["failed"], 1, "SL doğrulanamadı -> başarısız sayılmalı")
        self.assertEqual(hb.stopped, [("E1", True)], "SL not re-verified: no emergency close")
        self.assertEqual(len(fb.order_posts), 1)
        self.assertEqual(fb.amt, Decimal("0"))

    # UNKNOWN SL status (algo query error) -> emergency close (fail-closed).
    def test_unknown_sl_triggers_emergency_close(self):
        fb = FakeBinance(algo_raw={"code": -1, "msg": "err"})
        hb = FakeHB()
        cc = self._cc(fb, hb)
        res = auto_trader.open_coin(fb, hb, self._coin(), risk=_RiskOK(), reserver=None,
                                    coordinator=cc)
        self.assertTrue(res)
        out = self._drive_fill(fb, hb, cc)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(hb.stopped, [("E1", True)])
        self.assertEqual(len(fb.order_posts), 1)

    # Valid full-coverage SL -> sl_status PRESENT (unit: no open_coin side effects).
    def test_valid_sl_is_present(self):
        fb = FakeBinance(algo_raw=[_algo(side="SELL", quantity="0.6")])
        adapter = BinanceHBCloseAdapter(fb, None)
        self.assertEqual(adapter.sl_status(SYM, "LONG", mode="ONEWAY",
                                           position_amt="0.6"), SL_PRESENT)


if __name__ == "__main__":
    unittest.main()
