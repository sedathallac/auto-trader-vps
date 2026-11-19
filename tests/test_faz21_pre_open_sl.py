"""Faz 2.1 (K-1/K-2) — pre-open SL reconciliation: a newly opened position must never
end up with a DUPLICATE SL. Before placing the new algo SL, open_coin cancels any
leftover algo SL for the symbol (orphan from a prior/closed position).

Root cause (observed live): after an emergency-close, the closed position's SL lived
on as an orphan; before orphan_killer cancelled it a new position opened on the same
symbol -> TWO identical reduceOnly STOP_MARKET SLs on one position. Safe (reduceOnly),
but MAJOR tidiness/robustness defect. Fix prevents it at the source. Real open_coin;
only Binance/HB faked.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
from close_adapter import BinanceHBCloseAdapter
from close_coordinator import CloseCoordinator
from close_intent import CloseIntentStore

SYM = "SOLUSDT"
PAIR = "SOL-USDT"


def _live_sl(algo_id=878098):
    return {"algoId": algo_id, "orderType": "STOP_MARKET", "algoType": "CONDITIONAL",
            "symbol": SYM, "side": "SELL", "positionSide": "BOTH", "reduceOnly": True,
            "quantity": "0.3", "algoStatus": "NEW", "triggerPrice": "80.29"}


class FakeBinance:
    def __init__(self):
        self.amt = Decimal("0.3")
        self.cancelled_algos = []
        self._orphan = {"algoId": 786683, "orderType": "STOP_MARKET", "symbol": SYM}
        self.order_posts = []
        self._opened = False

    def filters(self, s):
        return {"LOT_SIZE": {"stepSize": "0.001"}, "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}

    def get_available_balance(self, a):
        return 1000.0

    def order_book(self, symbol, limit=20):
        # FAZ11 kalite kapısı: 2.5 bps spread, ~800k USDT derinlik.
        return {"bids": [["79.99", "5000"]], "asks": [["80.01", "5000"]]}

    def set_leverage(self, s, l):
        return {"leverage": l}

    def open_algo_orders_for(self, s):          # pre-open reconciliation source
        return [self._orphan] if self._orphan else []

    def cancel_algo_order(self, algo_id):
        self.cancelled_algos.append(algo_id)
        if self._orphan and self._orphan["algoId"] == algo_id:
            self._orphan = None
        return {"algoId": algo_id, "code": "200", "msg": "success"}

    def place_algo_sl(self, s, trig, qty, side, **_faz18kw):
        return {"algoId": 878098}

    def open_algo_orders_raw(self, s):          # sl_status source -> the NEW valid SL
        return [_live_sl()]

    def position_mode(self):
        return {"dualSidePosition": False}

    def position_risk(self):
        if not getattr(self, '_opened', False):
            return []
        return [{"symbol": SYM, "positionAmt": str(self.amt), "entryPrice": "81.27"}]

    def _simulate_fill(self):
        self._opened = True
    def account_snapshot(self):
        # FAZ16 (4331dd1): gerçek giriş yolu artık TEK snapshot ister — fixture katmanı.
        return auto_trader.AccountSnapshot(equity=1000.0, available_balance=1000.0,
                                           can_trade=True, raw={})

    def open_orders(self, s):
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
        return [] if not self.created else \
            [{"trading_pair": PAIR, "status": "RUNNING", "executor_id": "E1",
              "custom_info": {"order_ids": []}}]

    def create_executor(self, *a, **k):
        self.created.append(SYM)
        return {"executor_id": "E1"}

    def get_executor(self, eid):
        return {"filled_amount_quote": 24.4, "error_count": 0}

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
        # Bu testin boyutlandırma stub'ı (24.0) ile tutarlı hedef.
        return 24.0, "approved"


class Faz21Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self._orig_max = auto_trader.MAX_OPEN_POSITIONS
        auto_trader.MAX_OPEN_POSITIONS = 5
        self._orig = {}
        for n, v in [("check_liquidity", lambda b, s: (True, "ok")),
                     ("check_correlation_limit", lambda s, b: True),
                     ("decide_side", lambda c: (1, "LONG")),
                     ("calculate_atr", lambda b, s, interval="15m", period=14: 0.02),
                     ("calculate_position_size", lambda bal, c: (24.0, 1.0)),
                     ("add_blacklist", lambda *a, **k: None)]:
            self._orig[n] = getattr(auto_trader, n)
            setattr(auto_trader, n, v)
        self._slp = auto_trader.time.sleep
        auto_trader.time.sleep = lambda *a, **k: None
        # FAZ14p: dolum beklemesi tick'ler arası; bekleyen dolum defteri izole.
        self._orig_pf = auto_trader.PENDING_FILLS_JSON
        auto_trader.PENDING_FILLS_JSON = Path(self.tmp) / "pending_fills.json"
        auto_trader._PENDING_FILLS.clear()

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(auto_trader, n, v)
        auto_trader.time.sleep = self._slp
        auto_trader.MAX_OPEN_POSITIONS = self._orig_max
        auto_trader.PENDING_FILLS_JSON = self._orig_pf
        auto_trader._PENDING_FILLS.clear()

    def _cc(self, fb, hb):
        st = CloseIntentStore(self.db); st.init_schema()
        return CloseCoordinator(BinanceHBCloseAdapter(fb, hb), intent_store=st)

    def _coin(self):
        return {"symbol": SYM, "lastPrice": "81.27", "quoteVolume": "1e9",
                "_score": 0.7, "priceChangePercent": "-2"}


class TestPreOpenReconciliation(Faz21Base):
    def test_leftover_sl_cancelled_before_new(self):
        fb = FakeBinance(); hb = FakeHB()
        cc = self._cc(fb, hb)
        r = auto_trader.open_coin(fb, hb, self._coin(), risk=_RiskOK(), reserver=None,
                                  coordinator=cc)
        self.assertTrue(r)                       # emir kabul edildi, dolum bekliyor
        fb._simulate_fill()
        out = auto_trader._process_pending_fills(fb, hb, None, cc, None)
        self.assertEqual(out["filled"], 1)       # dolum onaylandı, SL yolu çalıştı
        # the orphan SL (786683) was cancelled before placing the new one...
        self.assertIn(786683, fb.cancelled_algos,
                      "leftover orphan SL not cancelled -> duplicate SL risk")
        # ...and the position is held (new SL verified PRESENT, no emergency close).
        self.assertEqual(hb.stopped, [])          # not emergency-closed
        self.assertEqual(fb.order_posts, [])      # no coordinated close


if __name__ == "__main__":
    unittest.main()
