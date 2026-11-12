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


# Tri-state result of verifying the exchange reduceOnly STOP algo SL (K1=B).
SL_PRESENT = "PRESENT"   # successful, valid query: a live protective SL exists
SL_ABSENT = "ABSENT"     # successful, valid query: genuinely no SL for this position
SL_UNKNOWN = "UNKNOWN"   # timeout/exception/API error/malformed/wrong symbol -> HALT

# Algo-order statuses that mean the SL is still live (protecting the position).
_SL_LIVE = {"", "NEW", "WORKING", "ACTIVE", "LIVE"}


# REGULAR-order statuses that mean the order can no longer fill (TERMINAL).
# NEW / PARTIALLY_FILLED / PENDING_CANCEL / WORKING / missing / unknown -> NON
# terminal (fail-closed). Algo orders are NOT classified here.
_TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def classify_order_terminal(order):
    """Classify a REGULAR order by its `status` field ONLY (not algoStatus).
    Terminal: FILLED/CANCELED/EXPIRED/REJECTED. Everything else (NEW,
    PARTIALLY_FILLED, PENDING_CANCEL, missing/unknown) -> False (fail-closed)."""
    st = str((order or {}).get("status") or "").upper()
    return st in _TERMINAL


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

    def _hb_order_ids(self, symbol):
        """Union of the client order ids tracked by ALL HB executors on this
        symbol (a pair can have several executors — grid / multi-level — each with
        its own _open_order/_close_order/_take_profit_limit_order via
        get_custom_info.order_ids). Does NOT stop at the first executor. Raises on
        an unavailable executor list OR any matched executor whose order_ids
        mapping is missing/not-a-list (restart/mapping lost -> UNKNOWN, fail-
        closed). Returns the (possibly empty) union when executors are found, or
        None when NO executor exists for the pair."""
        pair = symbol.replace("USDT", "-USDT")
        execs = self.hb.active_executors()
        if not isinstance(execs, list):
            raise CloseAdapterError(f"active_executors [HB] unavailable: {execs}")
        matched = [e for e in execs if e.get("trading_pair") == pair]
        if not matched:
            return None
        ids = set()
        for e in matched:
            info = e.get("custom_info")
            oids = info.get("order_ids") if isinstance(info, dict) else None
            if not isinstance(oids, list):
                raise CloseAdapterError(
                    f"HB executor {e.get('executor_id')} for {symbol} has no "
                    f"order_ids mapping -> UNKNOWN (fail-closed)")
            ids |= set(oids)
        return ids

    def protective_orders(self, symbol) -> list:
        """The orders tracked by THIS symbol's HB executor. get_custom_info.order_ids
        is [self._open_order, self._close_order, self._take_profit_limit_order]
        client ids (proven from dev-2.18.0 source) — a flat list with NO role label,
        so the ENTRY open order is included and must be resolved too (a still-live
        entry blocks the handoff; it is NOT pre-judged a close/TP). Each tracked id
        is returned as its live exchange order (matched by clientOrderId in
        openOrders) or as a stub {symbol, clientOrderId} so order_terminal can query
        it by origClientOrderId even when it is no longer resting. Unrelated open
        orders are NEVER returned (never cancelled). If the HB executor list is
        unavailable the handoff is UNKNOWN -> raise (fail-closed)."""
        orders = self.binance.open_orders(symbol)
        if not isinstance(orders, list):
            raise CloseAdapterError(
                f"openOrders [GET /fapi/v1/openOrders] unavailable for {symbol}: {orders}")
        hb_ids = self._hb_order_ids(symbol)  # raises if the executor list is unavailable
        if hb_ids is None:
            # No HB executor for this pair. If there are open orders we cannot
            # attribute them -> UNKNOWN handoff (fail-closed); unrelated orders are
            # never bulk-cancelled. No open orders -> nothing to resolve.
            if orders:
                raise CloseAdapterError(
                    f"{len(orders)} open order(s) for {symbol} but no HB executor "
                    f"-> UNKNOWN handoff (fail-closed; unrelated orders untouched)")
            return []
        if not hb_ids:
            return []  # executor tracks no orders; unrelated orders left untouched
        by_coid = {o.get("clientOrderId"): o for o in orders if o.get("clientOrderId")}
        result = []
        for coid in hb_ids:
            if coid in by_coid:
                o = dict(by_coid[coid]); o.setdefault("symbol", symbol)
                result.append(o)
            else:
                result.append({"symbol": symbol, "clientOrderId": coid})  # query stub
        return result

    def _query_terminal(self, o) -> bool:
        """Terminal ONLY when GET /fapi/v1/order reliably confirms it, with the
        queried order's symbol / orderId / clientOrderId cross-checked. Timeout,
        exception, any error code (incl. -2011/-2013 order-not-found), a missing or
        mismatched identity field -> False (UNKNOWN, fail-closed). A missing order
        is never auto-terminal. FILLED is terminal for the ORDER; whether the
        POSITION closed is confirmed separately via positionRisk (K2)."""
        symbol = o.get("symbol")
        oid = o.get("orderId")
        coid = o.get("clientOrderId")
        if symbol is None or (oid is None and coid is None):
            return False
        try:
            q = self.binance.query_order(symbol, order_id=oid, orig_client_order_id=coid)
        except Exception:
            return False
        if not isinstance(q, dict) or q.get("code") is not None or q.get("status") is None:
            return False
        if q.get("symbol") not in (None, symbol):
            return False
        if oid is not None and str(q.get("orderId")) != str(oid):
            return False
        if coid is not None and q.get("clientOrderId") not in (None, coid):
            return False
        return classify_order_terminal(q)

    def order_terminal(self, o) -> bool:
        return self._query_terminal(o)

    def cancel_order(self, o) -> bool:
        """Cancel a REGULAR tracked order (DELETE /fapi/v1/order by orderId or
        origClientOrderId), then CONFIRM its final state with a status query — a
        successful cancel response alone is NOT proof of resolution."""
        symbol = o.get("symbol")
        oid = o.get("orderId")
        coid = o.get("clientOrderId")
        if symbol is None or (oid is None and coid is None):
            return False
        try:
            self.binance.cancel_order(symbol, order_id=oid, orig_client_order_id=coid)
        except Exception:
            return False  # cancel send failed -> UNKNOWN
        return self._query_terminal(o)

    def sl_status(self, symbol, position_side=None):
        """Tri-state verification of the exchange reduceOnly STOP algo SL (K1=B).
        Uses open_algo_orders_raw() so an outage is NEVER mistaken for 'no SL':

          SL_PRESENT - query succeeded, response valid, a live protective STOP algo
                       order for THIS symbol exists (and, when position_side is
                       given, guards THIS side; a BOTH / ONEWAY order matches).
          SL_ABSENT  - query succeeded, response valid, no such SL -> UNPROTECTED.
          SL_UNKNOWN - timeout / exception / API error dict / malformed payload /
                       an order for the WRONG symbol (unverifiable match) -> HALT.

        Never sends an order and never changes a stop price (read-only)."""
        try:
            r = self.binance.open_algo_orders_raw(symbol)
        except Exception:
            return SL_UNKNOWN                      # timeout / network / exception
        if isinstance(r, dict):
            orders = r.get("orders")
            if not isinstance(orders, list):
                return SL_UNKNOWN                  # error dict ({code,msg}) / malformed
            r = orders
        if not isinstance(r, list):
            return SL_UNKNOWN                      # non-list, non-dict -> unverifiable
        for o in r:
            if not isinstance(o, dict):
                return SL_UNKNOWN                  # malformed element
            if str(o.get("symbol") or "").upper() != symbol.upper():
                return SL_UNKNOWN                  # wrong symbol -> cannot verify match
            typ = str(o.get("type") or o.get("algoType") or "").upper()
            st = str(o.get("algoStatus") or o.get("status") or "").upper()
            if "STOP" not in typ or st not in _SL_LIVE:
                continue
            if o.get("reduceOnly") is False:
                continue                           # non-reduceOnly STOP is not protective
            if position_side:                      # HEDGE: SL must guard THIS side
                psl = str(o.get("positionSide") or "").upper()
                if psl and psl not in ("BOTH", position_side.upper()):
                    continue
            return SL_PRESENT
        return SL_ABSENT

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
