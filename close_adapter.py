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

# Protective STOP side for a given position side (close direction).
_PROTECTIVE_SIDE = {"LONG": "SELL", "SHORT": "BUY"}


def _truthy(v):
    """Binance sends booleans as bool or the strings 'true'/'false'."""
    return v is True or str(v).strip().lower() == "true"


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

    def position_token(self, symbol) -> str:
        """A position-PERIOD id for the cross-process close-intent, from exchange
        truth: '<side>:<entryPrice>'. side distinguishes a HEDGE LONG vs SHORT on the
        same symbol; entryPrice distinguishes a new position period from a prior
        (closed) one. RAISES on any API failure / missing position (fail-closed: no
        token -> no close POST). Never fabricated from local state."""
        r = self.binance.position_risk()
        if not isinstance(r, list):
            raise CloseAdapterError(f"positionRisk unavailable for token: {r}")
        for p in r:
            if p.get("symbol") == symbol:
                try:
                    amt = Decimal(str(p.get("positionAmt", "0")))
                except Exception as e:
                    raise CloseAdapterError(f"bad positionAmt for token {symbol}: {e}")
                if amt == 0:
                    raise CloseAdapterError(f"no open position for token {symbol}")
                side = "LONG" if amt > 0 else "SHORT"
                entry = str(p.get("entryPrice", "")).strip()
                if not entry:
                    raise CloseAdapterError(f"missing entryPrice for token {symbol}")
                return f"{side}:{entry}"
        raise CloseAdapterError(f"symbol {symbol} absent from positionRisk for token")

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
            # C-1: order_ids is [open, close, tp]; a DISABLED barrier slot (K1=B
            # disables close/TP) is None -> no order was ever placed, so it is not an
            # unresolvable order and must NOT become a query stub (None has no
            # orderId/clientOrderId -> order_terminal/cancel would always fail ->
            # UNRESOLVED -> the close would be refused forever). Drop None/empty ONLY;
            # every REAL tracked id is still resolved (an unknown/live real order
            # still blocks -> fail-closed).
            ids |= {o for o in oids if o is not None and o != ""}
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

    def sl_status(self, symbol, position_side=None, mode=None, position_amt=None):
        """Mode-aware tri-state verification of the exchange STOP algo SL (K1=B),
        validated against the REAL Binance ONEWAY/HEDGE order contract. Uses
        open_algo_orders_raw() so an outage is NEVER mistaken for 'no SL'.

          SL_PRESENT - query ok + valid + a LIVE STOP_MARKET algo order that FULLY
                       protects THIS position under its mode (see below).
          SL_ABSENT  - query ok + valid + no order gives full valid coverage.
          SL_UNKNOWN - timeout / exception / API error dict / malformed payload /
                       an order for the WRONG symbol (unverifiable match) -> HALT.

        Valid full-coverage protection (nothing else is accepted):
          ONEWAY  - positionSide BOTH or absent, close-direction `side`, and EITHER
                    closePosition=true, OR reduceOnly=true with quantity >= the full
                    position amount (partial quantity is NOT full coverage).
          HEDGE   - positionSide == THIS side, close-direction `side`, STOP_MARKET,
                    closePosition=true, and reduceOnly NOT true (Binance rejects
                    reduceOnly in hedge mode -> such a combo is not a valid SL).

        mode defaults to ONEWAY (production places ONEWAY reduceOnly STOP_MARKET via
        place_algo_sl). position_amt (abs) is required to confirm reduceOnly coverage;
        without it a reduceOnly-with-quantity order cannot be confirmed as full.
        Read-only: never sends an order, never changes a stop price."""
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

        is_hedge = (mode == HEDGE)
        pos_side = position_side.upper() if position_side else None
        want_side = _PROTECTIVE_SIDE.get(pos_side) if pos_side else None
        try:
            want_qty = abs(Decimal(str(position_amt))) if position_amt is not None else None
        except Exception:
            want_qty = None

        for o in r:
            if not isinstance(o, dict):
                return SL_UNKNOWN                  # malformed element
            if str(o.get("symbol") or "").upper() != symbol.upper():
                return SL_UNKNOWN                  # wrong symbol -> cannot verify match
            typ = str(o.get("type") or o.get("algoType") or "").upper()
            st = str(o.get("algoStatus") or o.get("status") or "").upper()
            if "STOP" not in typ or st not in _SL_LIVE:
                continue                           # not a STOP, or terminal -> not protective
            o_side = str(o.get("side") or "").upper()
            if want_side and o_side and o_side != want_side:
                continue                           # wrong close direction -> not protective
            o_pside = str(o.get("positionSide") or "").upper()
            reduce_only = _truthy(o.get("reduceOnly"))
            close_pos = _truthy(o.get("closePosition"))

            if is_hedge:
                # HEDGE: positionSide must guard THIS side; closePosition covers full;
                # reduceOnly is invalid in hedge mode -> reject the combo.
                if pos_side and o_pside != pos_side:
                    continue
                if reduce_only:
                    continue                       # unsupported ONEWAY-style combo in hedge
                if not close_pos:
                    continue                       # partial/quantity SL not accepted in hedge
                return SL_PRESENT
            else:
                # ONEWAY: positionSide BOTH or absent.
                if o_pside and o_pside != "BOTH":
                    continue
                if close_pos:
                    return SL_PRESENT              # closePosition closes the whole position
                if reduce_only:
                    if want_qty is None:
                        continue                   # coverage unverifiable -> not confirmed full
                    try:
                        o_qty = abs(Decimal(str(o.get("quantity")
                                                 or o.get("origQty") or "0")))
                    except Exception:
                        continue
                    if o_qty >= want_qty and o_qty > 0:
                        return SL_PRESENT          # full-position reduceOnly coverage
                    continue                       # insufficient quantity -> not full
                # neither closePosition nor reduceOnly -> not a protective SL
                continue
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
