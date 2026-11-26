"""blok28_1_fake.py — BLOK28.1 test altyapısı: Binance USDⓈ-M semantiğini
simüle eden FakeExchange (AĞ ÇAĞRISI YOK — §11/§13).

Gerçek sözleşme davranışları:
  - MARKET emir anında mark fiyatından dolar (slippage ayarlanabilir).
  - reduceOnly: pozisyonu ters çeviremez → -2022 reddi.
  - STOP_MARKET CONDITIONAL (algoOrder): closePosition=true tetiklendiğinde
    TAM pozisyonu trigger fiyatından kapatır; auto_trigger=True iken
    premiumIndex/set_mark mark'ı trigger'ı geçince tetikler (borsa tarafı
    koruma sürekliliği — §9).
  - POST timeout: timeout_client_ids → emir UYGULANIR + UnknownResult
    (istek ulaşmış olabilir); no_apply_timeouts → UYGULANMAZ + UnknownResult
    (hiç var olmamış → sorgu -2011).
  - cancel/fill yarışı: cancel_fills_first → emir önce dolar, iptal -2011.
  - partial fill: partial_fill_qty → kısmi dolar; bir sonraki sorguda
    kalan EXPIRED (IOC benzeri).
  - outage: GET → ExchangeUnavailable; POST/DELETE → UnknownResult.
"""
import time

from canary_exchange import (ExchangeUnavailable, UnknownResult,
                             BinanceApiError)

# Test filtreleri — gerçek exchangeInfo değerleri (2026-11-24 kanıt dosyası)
REAL_FILTERS = {
    "BTCUSDT": {"tickSize": "0.10", "stepSize": "0.001", "minQty": "0.001",
                "minNotional": "50", "quantityPrecision": 3,
                "pricePrecision": 2},
    "ETHUSDT": {"tickSize": "0.01", "stepSize": "0.001", "minQty": "0.001",
                "minNotional": "20", "quantityPrecision": 3,
                "pricePrecision": 2},
    "SOLUSDT": {"tickSize": "0.0100", "stepSize": "0.01", "minQty": "0.01",
                "minNotional": "5", "quantityPrecision": 2,
                "pricePrecision": 4},
    "ADAUSDT": {"tickSize": "0.00010", "stepSize": "1", "minQty": "1",
                "minNotional": "5", "quantityPrecision": 0,
                "pricePrecision": 5},
    "XRPUSDT": {"tickSize": "0.0001", "stepSize": "0.1", "minQty": "0.1",
                "minNotional": "5", "quantityPrecision": 1,
                "pricePrecision": 4},
    "LINKUSDT": {"tickSize": "0.001", "stepSize": "0.01", "minQty": "0.01",
                 "minNotional": "20", "quantityPrecision": 2,
                 "pricePrecision": 3},
    "BNBUSDT": {"tickSize": "0.010", "stepSize": "0.01", "minQty": "0.01",
                "minNotional": "5", "quantityPrecision": 2,
                "pricePrecision": 3},
    "DOGEUSDT": {"tickSize": "0.000010", "stepSize": "1", "minQty": "1",
                 "minNotional": "5", "quantityPrecision": 0,
                 "pricePrecision": 6},
}

# Gerçek biçimde leverageBracket yapısı (BTCUSDT benzeri örnek katmanlar;
# gerçek canlı değer anahtar geçersiz olduğu için kanıtlanamadı — raporda
# LIQUIDATION_RISK_UNVERIFIED olarak ayrıldı)
FIXTURE_BRACKETS_RAW = [
    {"symbol": s, "brackets": [
        {"bracket": 1, "initialLeverage": 50, "notionalFloor": 0,
         "notionalCap": 50_000, "maintMarginRatio": 0.004,
         "cum": 0.0},
        {"bracket": 2, "initialLeverage": 25, "notionalFloor": 50_000,
         "notionalCap": 250_000, "maintMarginRatio": 0.005,
         "cum": 50.0},
        {"bracket": 3, "initialLeverage": 10, "notionalFloor": 250_000,
         "notionalCap": 1_000_000, "maintMarginRatio": 0.01,
         "cum": 1300.0},
    ]} for s in REAL_FILTERS
]


class FakeExchange:
    """LiveCanary'nin kullandığı tüm metotları uygular."""

    def __init__(self, symbols=None, filters=None, commission_rate=0.0005,
                 wallet=10_000.0, clock_ms=None):
        self.symbols = symbols or list(REAL_FILTERS)
        self.filters = dict(filters or REAL_FILTERS)
        self.commission_rate = commission_rate
        self.wallet = wallet
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        now = self._clock()
        self.mark_px = {s: 100.0 for s in self.symbols}
        self.mark_ts = {s: now for s in self.symbols}
        # {symbol: positionAmt} + entryPrice (ortalama)
        self.positions = {s: {"positionAmt": 0.0, "entryPrice": 0.0}
                          for s in self.symbols}
        self.orders = {}          # client_id → order record
        self.algos = {}           # client_algo_id → algo record
        self.trades = []          # userTrades kayıtları
        self.incomes = []         # income kayıtları (FUNDING_FEE)
        self._seq = 1000
        # senaryo kontrolleri
        self.outage = False
        self.auto_trigger = False
        self.timeout_client_ids = set()     # UYGULA + UnknownResult
        self.no_apply_timeouts = set()      # UYGULAMA + UnknownResult
        self.algo_fail = {}                 # client_algo_id → BinanceApiError
        self.cancel_fills_first = set()     # iptal yarışı: önce dol
        self.partial_fill_qty = {}          # client_id → dolacak miktar
        self.post_calls = []                # tüm mutasyon çağrı kaydı
        self.kline_data = {}                # symbol → klines
        self.book_ticker_fail = set()       # sembol bazlı bookTicker hatası
        self.book_ticker_time = {}          # symbol → time override (stale)
        self.book_spread = 0.0002           # bid/ask spread payı
        self.position_mode_resp = {"dualSidePosition": False}
        self.margin_modes = {s: "isolated" for s in self.symbols}
        self.leverages = {s: 10 for s in self.symbols}
        self.brackets_raw = [dict(r) for r in FIXTURE_BRACKETS_RAW]

    # ---- test kumandaları ---------------------------------------------------
    def set_mark(self, symbol, px, ts=None):
        self.mark_px[symbol] = float(px)
        self.mark_ts[symbol] = int(ts) if ts is not None else self._clock()
        if self.auto_trigger:
            self._check_triggers()

    def set_position(self, symbol, amt, entry_px=0.0):
        """Borsada 'başka biri' açmış pozisyon (sahiplik testleri)."""
        self.positions[symbol] = {"positionAmt": float(amt),
                                  "entryPrice": float(entry_px)}

    def add_income(self, symbol, income, ts, income_type="FUNDING_FEE"):
        self.incomes.append({"symbol": symbol, "income": str(income),
                             "time": int(ts), "incomeType": income_type})

    def _next_id(self):
        self._seq += 1
        return self._seq

    def _check_triggers(self):
        """auto_trigger: mark, trigger'ı geçen algo emirlerini borsa tarafında
        çalıştırır (closePosition → TÜM pozisyonu kapat)."""
        for cid, a in list(self.algos.items()):
            if a["status"] != "NEW" or not a.get("closePosition"):
                continue
            if a.get("workingType") != "MARK_PRICE":
                continue
            trig = float(a["triggerPrice"])
            mark = self.mark_px.get(a["symbol"], 0.0)
            fire = (a["side"] == "SELL" and mark <= trig) or \
                   (a["side"] == "BUY" and mark >= trig)
            if not fire:
                continue
            pos = self.positions.get(a["symbol"])
            if pos and abs(pos["positionAmt"]) >= 1e-12:
                amt = pos["positionAmt"]
                px = trig
                comm = abs(amt) * px * self.commission_rate
                self.trades.append({
                    "time": self._clock(), "symbol": a["symbol"],
                    "side": a["side"], "price": str(px), "qty": str(abs(amt)),
                    "commission": str(comm),
                    "buyer": a["side"] == "BUY"})
                self.positions[a["symbol"]] = {"positionAmt": 0.0,
                                               "entryPrice": 0.0}
            a["status"] = "TRIGGERED"
            a["algoStatus"] = "TRIGGERED"

    # ---- public -----------------------------------------------------------
    def premium_index(self):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        self._check_triggers()
        return [{"symbol": s, "markPrice": str(self.mark_px[s]),
                 "time": self.mark_ts[s],
                 "indexPrice": str(self.mark_px[s]),
                 "estimatedSettlePrice": str(self.mark_px[s])}
                for s in self.symbols]

    def klines(self, symbol, interval="4h", limit=410):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return list(self.kline_data.get(symbol, []))

    def book_ticker(self, symbol):
        """BLOK31: en iyi bid/ask (mark ± spread). Senaryo kumandaları:
        book_ticker_fail (sembol bazlı hata), book_ticker_time (eski time)."""
        if self.outage or symbol in self.book_ticker_fail:
            raise ExchangeUnavailable("simulated bookTicker outage")
        px = self.mark_px.get(symbol, 100.0)
        spread = self.book_spread
        return {"symbol": symbol,
                "bidPrice": str(px * (1 - spread)),
                "bidQty": "1.0",
                "askPrice": str(px * (1 + spread)),
                "askQty": "1.0",
                "time": self.book_ticker_time.get(symbol, self._clock())}

    def funding_rate(self, symbol, start_ms=None, limit=100):
        return []

    def exchange_info(self):
        return {"symbols": []}

    # ---- imzalı GET ---------------------------------------------------------
    def position_risk(self):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        out = []
        for s in self.symbols:
            p = self.positions[s]
            out.append({
                "symbol": s, "positionAmt": str(p["positionAmt"]),
                "entryPrice": str(p["entryPrice"]),
                "markPrice": str(self.mark_px[s]),
                "unRealizedProfit": "0",
                "liquidationPrice": "0",
                "leverage": str(self.leverages[s]),
                "marginType": self.margin_modes[s],
                "isolatedMargin": "0", "notional": "0",
                "positionSide": "BOTH",
            })
        return out

    def open_orders(self, symbol=None):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return [dict(o) for o in self.orders.values()
                if o["status"] in ("NEW", "PARTIALLY_FILLED")
                and (symbol is None or o["symbol"] == symbol)]

    def open_algo_orders(self, symbol=None):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return [dict(a) for a in self.algos.values()
                if a["status"] == "NEW"
                and (symbol is None or a["symbol"] == symbol)]

    def _find_order(self, symbol, order_id=None, client_order_id=None):
        for o in self.orders.values():
            if order_id is not None and str(o["orderId"]) == str(order_id):
                return o
            if client_order_id is not None and \
                    o["clientOrderId"] == client_order_id:
                return o
        return None

    def query_order(self, symbol, order_id=None, client_order_id=None):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        o = self._find_order(symbol, order_id, client_order_id)
        if o is None:
            raise BinanceApiError(-2011, "Unknown order sent.")
        # kısmi dolmuş emrin kalanı IPTAL olur (IOC benzeri)
        if o["status"] == "PARTIALLY_FILLED":
            o["status"] = "EXPIRED"
        return dict(o)

    def query_algo_order(self, symbol=None, algo_id=None, client_algo_id=None):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        for a in self.algos.values():
            if algo_id is not None and str(a["algoId"]) == str(algo_id):
                return dict(a)
            if client_algo_id is not None and \
                    a["clientAlgoId"] == client_algo_id:
                return dict(a)
        raise BinanceApiError(-2011, "Unknown algo order sent.")

    def leverage_brackets(self):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return [dict(r) for r in self.brackets_raw]

    def position_mode(self):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return dict(self.position_mode_resp)

    def commission_rate(self):
        return {"symbol": "ALL", "makerCommissionRate": "0.0002",
                "takerCommissionRate": str(self.commission_rate)}

    def user_trades(self, symbol, start_ms=None, limit=100):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return [dict(t) for t in self.trades
                if t["symbol"] == symbol
                and (start_ms is None or int(t["time"]) >= start_ms)]

    def income(self, symbol=None, income_type=None, start_ms=None, limit=100):
        if self.outage:
            raise ExchangeUnavailable("simulated network outage")
        return [dict(r) for r in self.incomes
                if (symbol is None or r["symbol"] == symbol)
                and (income_type is None
                     or r["incomeType"] == income_type)
                and (start_ms is None or int(r["time"]) >= start_ms)]

    # ---- emir gönderimi -------------------------------------------------------
    def new_order(self, symbol, side, type_, quantity=None, price=None,
                  reduce_only=None, client_order_id=None,
                  new_order_resp_type="RESULT", time_in_force=None,
                  position_side=None):
        self.post_calls.append({"method": "new_order", "symbol": symbol,
                                "side": side, "quantity": quantity,
                                "reduce_only": reduce_only,
                                "client_order_id": client_order_id})
        if self.outage:
            raise UnknownResult("POST", "/fapi/v1/order", client_order_id,
                                RuntimeError("simulated outage"))
        if client_order_id in self.no_apply_timeouts:
            # istek HİÇ ulaşmadı: emir yok, pozisyon DEĞİŞMEDİ, sonuç bilinmiyor
            raise UnknownResult("POST", "/fapi/v1/order", client_order_id,
                                RuntimeError("timeout, not applied"))
        qty = float(quantity)
        f = self.filters.get(symbol, {})
        min_qty = float(f.get("minQty", 0))
        min_notional = float(f.get("minNotional", 0) or 0)
        if qty <= 0 or qty < min_qty:
            raise BinanceApiError(-4014, "Minimum quantity violated.")
        fill_px = self.mark_px.get(symbol, 100.0)
        if qty * fill_px < min_notional - 1e-9:
            raise BinanceApiError(-4164, "Order's notional must be no smaller "
                                         "than minNotional.")
        pos = self.positions.setdefault(symbol,
                                        {"positionAmt": 0.0,
                                         "entryPrice": 0.0})
        amt = pos["positionAmt"]
        fill_qty = self.partial_fill_qty.get(client_order_id, qty)
        signed_fill = fill_qty if side == "BUY" else -fill_qty
        # reduceOnly (One-way): pozisyon yok / ters çevirme / aşım → -2022
        if reduce_only:
            if abs(amt) < 1e-12:
                raise BinanceApiError(-2022, "ReduceOnly Order is rejected.")
            if (signed_fill > 0) != (amt < 0):
                raise BinanceApiError(-2022, "ReduceOnly Order is rejected.")
            if abs(signed_fill) > abs(amt) + 1e-12:
                raise BinanceApiError(-2022, "ReduceOnly Order is rejected.")
        new_amt = amt + signed_fill
        if abs(amt) < 1e-12:
            pos["entryPrice"] = fill_px
        elif abs(new_amt) > abs(amt):
            pos["entryPrice"] = (abs(amt) * pos["entryPrice"]
                                 + fill_qty * fill_px) / abs(new_amt)
        pos["positionAmt"] = new_amt
        comm = fill_qty * fill_px * self.commission_rate
        self.trades.append({
            "time": self._clock(), "symbol": symbol, "side": side,
            "price": str(fill_px), "qty": str(fill_qty),
            "commission": str(comm), "buyer": side == "BUY"})
        status = "FILLED" if fill_qty >= qty - 1e-12 else "PARTIALLY_FILLED"
        order = {
            "orderId": self._next_id(), "symbol": symbol, "side": side,
            "type": type_, "status": status,
            "executedQty": str(fill_qty), "avgPrice": str(fill_px),
            "cumQuote": str(fill_qty * fill_px),
            "clientOrderId": client_order_id or f"auto{self._seq}",
            "reduceOnly": bool(reduce_only),
            "fills": [{"commission": str(comm), "price": str(fill_px),
                       "qty": str(fill_qty)}],
        }
        self.orders[client_order_id or f"auto{self._seq}"] = order
        if client_order_id in self.timeout_client_ids:
            raise UnknownResult("POST", "/fapi/v1/order", client_order_id,
                                RuntimeError("timeout, applied"))
        return dict(order)

    def new_algo_order(self, symbol, side, type_, trigger_price,
                       working_type="MARK_PRICE", close_position=None,
                       quantity=None, reduce_only=None, client_algo_id=None,
                       price_protect=None):
        self.post_calls.append({"method": "new_algo_order", "symbol": symbol,
                                "side": side, "trigger_price": trigger_price,
                                "client_algo_id": client_algo_id})
        if self.outage:
            raise UnknownResult("POST", "/fapi/v1/algoOrder", client_algo_id,
                                RuntimeError("simulated outage"))
        if client_algo_id in self.no_apply_timeouts:
            # istek HİÇ ulaşmadı: algo emri yok, sonuç bilinmiyor
            raise UnknownResult("POST", "/fapi/v1/algoOrder", client_algo_id,
                                RuntimeError("timeout, not applied"))
        if close_position and (quantity is not None or reduce_only):
            raise BinanceApiError(-1106, "Parameter conflict: closePosition "
                                         "cannot combine quantity/reduceOnly.")
        if client_algo_id in self.algo_fail:
            raise self.algo_fail[client_algo_id]
        mark = self.mark_px.get(symbol, 100.0)
        trig = float(trigger_price)
        # -2021: stop, mevcut fiyatın yanlış tarafında
        if type_ == "STOP_MARKET":
            if (side == "SELL" and trig >= mark) or \
                    (side == "BUY" and trig <= mark):
                raise BinanceApiError(-2021, "Order would immediately "
                                             "trigger.")
        algo = {
            "algoId": self._next_id(), "clientAlgoId": client_algo_id,
            "symbol": symbol, "side": side, "type": type_,
            "triggerPrice": str(trig), "workingType": working_type,
            "closePosition": "true" if close_position else "false",
            "algoStatus": "NEW", "status": "NEW",
        }
        self.algos[client_algo_id] = algo
        if client_algo_id in self.timeout_client_ids:
            raise UnknownResult("POST", "/fapi/v1/algoOrder", client_algo_id,
                                RuntimeError("timeout, applied"))
        return dict(algo)

    def cancel_order(self, symbol, order_id=None, client_order_id=None):
        self.post_calls.append({"method": "cancel_order", "symbol": symbol,
                                "client_order_id": client_order_id})
        if self.outage:
            raise UnknownResult("DELETE", "/fapi/v1/order", client_order_id,
                                RuntimeError("simulated outage"))
        o = self._find_order(symbol, order_id, client_order_id)
        if o is None:
            raise BinanceApiError(-2011, "Unknown order sent.")
        if o["status"] in ("FILLED", "CANCELED", "EXPIRED"):
            raise BinanceApiError(-2011, "Unknown order sent.")
        if client_order_id in self.cancel_fills_first:
            # yarış: iptal işlenmeden emir doldu
            o["status"] = "FILLED"
            raise BinanceApiError(-2011, "Unknown order sent.")
        o["status"] = "CANCELED"
        return dict(o)

    def cancel_algo_order(self, symbol=None, algo_id=None, client_algo_id=None):
        self.post_calls.append({"method": "cancel_algo_order",
                                "symbol": symbol,
                                "client_algo_id": client_algo_id})
        if self.outage:
            raise UnknownResult("DELETE", "/fapi/v1/algoOrder",
                                client_algo_id, RuntimeError("simulated "
                                                             "outage"))
        for cid, a in self.algos.items():
            if (algo_id is not None and str(a["algoId"]) == str(algo_id)) or \
                    (client_algo_id is not None and cid == client_algo_id):
                if a["status"] in ("FILLED", "CANCELED", "TRIGGERED"):
                    raise BinanceApiError(-2011, "Unknown order sent.")
                if cid in self.cancel_fills_first:
                    a["status"] = "TRIGGERED"
                    raise BinanceApiError(-2011, "Unknown order sent.")
                a["status"] = "CANCELED"
                return dict(a)
        raise BinanceApiError(-2011, "Unknown algo order sent.")


# ---- test fabrikaları -------------------------------------------------------

T0 = 1_735_000_000_000          # ms epoch — geçmiş, güvenli


class FakeClock:
    """Deterministik saat (90dk kesinti simülasyonu dahil)."""

    def __init__(self, t=T0):
        self.t = int(t)

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += int(ms)


def make_canary(tmpdir, ex=None, t0=T0, verified_liq=True, symbols=None,
                entry_policy=None, signal_source=None, brain_source=None):
    """Doğrulanmış likidasyon modeli + gerçek filtrelerle LiveCanary.

    BLOK32: entry_policy varsayılanı FLIP_ONLY'dur — BLOK30/31/31.1 legacy
    testlerinin kanonik flip-only davranışı BLOK32 ile DEĞİŞMEZ. Quality
    politikası testleri entry_policy='QUALITY_BIDIRECTIONAL' ister.

    GÖREV (2026-11-26): signal_source/brain_source verilirse BRAIN_PRIMARY
    modunda LiveCanary kurulur (test_brain_primary.py); verilmezse davranış
    öncekiyle BİREBİR aynıdır (H1_LOCAL)."""
    import os

    from canary_live import LiveCanary, SIGNAL_SOURCE_H1
    from canary_liquidation import (LiquidationModel,
                                    parse_leverage_brackets,
                                    POSITION_MODE_ONEWAY)
    import canary_quality as cq
    ex = ex or FakeExchange()
    liq = None
    if verified_liq:
        liq = LiquidationModel(
            brackets_by_symbol=parse_leverage_brackets(ex.brackets_raw),
            margin_modes=ex.margin_modes, leverages=ex.leverages,
            position_mode=POSITION_MODE_ONEWAY, wallet_usdt=ex.wallet)
    db = os.path.join(str(tmpdir), "canary_live.db")
    kw = {}
    if signal_source is not None:
        kw["signal_source"] = signal_source
        kw["brain_source"] = brain_source
    return LiveCanary(db, ex, symbols=symbols or ex.symbols, t0_ms=t0,
                      filters=ex.filters, liq_model=liq,
                      clock_ms=ex._clock, log_dir=str(tmpdir),
                      entry_policy=entry_policy or cq.ENTRY_POLICY_FLIP_ONLY,
                      **kw)


def prime(lc):
    """startup mutabakatı + ilk risk tick (marklar taze, kapılar hazır)."""
    lc.startup_reconcile()
    return lc.risk_tick()


def open_one(lc, symbol="ETHUSDT", direction=1, bar_ts=None, now=None):
    """Standart tam doğrulanmış giriş: FILLED + PROTECTED.

    ref_px=100.0 → icra ANI fiyatı (BLOK31); qty CANARY_NOTIONAL *
    (1 - ENTRY_SIZE_BUFFER) üzerinden boyutlanır (ETHUSDT 100 USDT
    hedef → 0.99 lot). Eşik testleri bu gerçek birimleri
    loss_mark()/stop_trigger() ile pozisyondan türetir."""
    now = now if now is not None else lc._now()
    recs = lc.try_entry(symbol, direction, 100.0,
                        bar_ts if bar_ts is not None else T0 + 4 * 3600_000,
                        now)
    return recs[0]


def stop_trigger(ex, pos_id):
    """Pozisyonun STOP_MARKET korumasının GERÇEK triggerPrice'ı (canlı
    koruma doğrulamasında borsanın kabul ettiği değer)."""
    return float(ex.algos[f"C281P{pos_id}"]["triggerPrice"])


def loss_mark(lc, pos_id, target_loss):
    """Hedeflenen zararı ÜRETEN mark fiyatı — pozisyonun gerçek
    units/entry_px/entry_comm değerinden türetilir (ENTRY_SIZE_BUFFER'lı
    boyutlamadan bağımsız, test amacı korunur).

    position_net_loss (long) = (entry - mark) * units + entry_comm
    """
    pos = lc.rm._pos(pos_id)
    return pos["entry_px"] - (target_loss - pos["entry_comm"]) / pos["units"]
