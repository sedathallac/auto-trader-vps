"""close_adapter.py — real BinanceAPI + HBAPI adapter for the CloseCoordinator.

Implements the exchange interface the CloseCoordinator depends on, backed by the
production BinanceAPI / HBAPI. The cardinal rule for the fault path: on ANY API
error / timeout / unexpected payload, the QUERY methods RAISE (CloseAdapterError)
— they never return a value (e.g. 0 / empty) that the coordinator would read as
"flat" or "no protective orders". That keeps the coordinator fail-closed: a
Binance outage can never be mistaken for a closed position.

Nothing here is exercised against the live exchange in this work package; the
CloseCoordinator tests inject a mock, and these adapter tests inject fakes. In
production the adapter would send a single reduce-only / positionSide close, but
that only runs after the separate live-rollout gate + user approval.
"""
from decimal import Decimal

from close_coordinator import ONEWAY, HEDGE


class CloseAdapterError(Exception):
    pass


# Algo/order statuses that mean the order can no longer fill.
_TERMINAL = {"FILLED", "CANCELLED", "CANCELED", "EXPIRED", "TRIGGERED",
             "FINISHED", "REJECTED", "ALREADY_TERMINAL"}


class BinanceHBCloseAdapter:
    def __init__(self, binance, hb):
        self.binance = binance
        self.hb = hb

    # ---- exchange truth (RAISE on failure -> coordinator fails closed) ----
    def position_amt(self, symbol) -> Decimal:
        r = self.binance.position_risk()
        if not isinstance(r, list):
            raise CloseAdapterError(f"positionRisk unavailable: {r}")
        for p in r:
            if p.get("symbol") == symbol:
                try:
                    return Decimal(str(p.get("positionAmt", "0")))
                except Exception as e:
                    raise CloseAdapterError(f"bad positionAmt for {symbol}: {e}")
        return Decimal("0")  # symbol absent -> genuinely no position

    def position_mode(self) -> str:
        r = self.binance.position_mode()
        if not isinstance(r, dict) or "dualSidePosition" not in r:
            raise CloseAdapterError(f"position mode unverifiable: {r}")
        v = r["dualSidePosition"]
        hedge = (v.strip().lower() == "true") if isinstance(v, str) else bool(v)
        return HEDGE if hedge else ONEWAY

    def protective_orders(self, symbol) -> list:
        r = self.binance.open_algo_orders_for(symbol)
        if not isinstance(r, list):
            raise CloseAdapterError(f"open algo orders unavailable for {symbol}: {r}")
        return r

    def order_terminal(self, o) -> bool:
        st = str(o.get("algoStatus") or o.get("status") or "").upper()
        return st in _TERMINAL

    def cancel_order(self, o) -> bool:
        aid = o.get("algoId") or o.get("orderId")
        resp = self.binance.cancel_algo_order(aid)
        if isinstance(resp, dict):
            code = resp.get("code")
            # success, or "unknown order" (-2011) == already terminal at exchange
            if str(code) == "200" or resp.get("algoId") or code in (-2011,):
                return True
        return False  # ambiguous / error -> NOT terminal -> coordinator stays safe

    def stop_executor(self, symbol):
        pair = symbol.replace("USDT", "-USDT")
        execs = self.hb.active_executors()
        if not isinstance(execs, list):
            raise CloseAdapterError(f"active_executors unavailable: {execs}")
        for e in execs:
            if e.get("trading_pair") == pair and e.get("status") == "RUNNING":
                eid = e.get("executor_id")
                if eid:
                    self.hb.stop_executor(eid, keep_position=True)
        # no RUNNING executor -> nothing to stop; the exchange position (if any)
        # is still handled by the reduce-only close below.

    def close_reduce_only(self, symbol, qty, position_side=None) -> Decimal:
        if position_side == "LONG":
            side = "SELL"
        elif position_side == "SHORT":
            side = "BUY"
        else:
            amt = self.position_amt(symbol)  # ONEWAY: derive side from position
            if amt == 0:
                return Decimal("0")
            side = "SELL" if amt > 0 else "BUY"
        params = {"symbol": symbol, "side": side, "type": "MARKET",
                  "quantity": f"{Decimal(str(qty)):f}"}
        if position_side:          # HEDGE: positionSide, reduceOnly is rejected
            params["positionSide"] = position_side
        else:                      # ONEWAY: reduceOnly can never cross zero (-2022)
            params["reduceOnly"] = "true"
        resp = self.binance._post("/fapi/v1/order", params)
        if not isinstance(resp, dict) or ("orderId" not in resp and resp.get("status") is None):
            raise CloseAdapterError(f"close order failed/rejected: {resp}")
        return Decimal(str(qty))
