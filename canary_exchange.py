"""canary_exchange.py — BLOK28.1: Binance USDⓈ-M Futures REST istemcisi (canary).

GERÇEK ağ YALNIZCA BinanceFuturesClient üzerinden; testler aynı arayüzü
uygulayan FakeExchange ile çalışır (ağ çağrısı YOK — §11/§13).

Sözleşme kaynakları (2026-11-24, developers.binance.com/docs/derivatives/
usds-margined-futures/trade/rest-api):
  - POST   /fapi/v1/order          normal emirler (MARKET/LIMIT; reduceOnly
                                   yalnız One-way; closePosition ile ÇAKIŞMAZ)
  - POST   /fapi/v1/algoOrder      CONDITIONAL emirler (algoType=CONDITIONAL;
                                   STOP_MARKET/TAKE_PROFIT_MARKET...;
                                   triggerPrice + workingType=MARK_PRICE;
                                   closePosition=true quantity/reduceOnly ile
                                   BİRLİKTE GÖNDERİLEMEZ; clientAlgoId)
  - GET    /fapi/v1/order          orderId VEYA origClientOrderId ile sorgu
  - DELETE /fapi/v1/order          iptal — -2011 (Unknown order sent) =
                                   cancel/fill yarışı sinyali → nihai durum
                                   için emri SORGULA
  - GET    /fapi/v1/openOrders     açık normal emirler
  - GET    /fapi/v1/algoOrder      algo emir sorgusu (algoId / clientAlgoId)
  - DELETE /fapi/v1/algoOrder      algo iptal
  - GET    /fapi/v1/algoOpenOrders açık algo emirleri
  - GET    /fapi/v2/positionRisk   GERÇEK pozisyon (flat doğrulamasının tek
                                   kabul edilen kaynağı — §2.1/§3)
  - GET    /fapi/v1/leverageBracket  bakım marjı katmanları (imzalı)
  - GET    /fapi/v1/positionSide/dual pozisyon modu (GET = sorgu, POST =
                                   DEĞİŞİKLİK — bu istemci POST'ı ASLA çağırmaz)
  - GET    /fapi/v1/premiumIndex   REST_SAMPLED_MARK_PRICE (public)
  - GET    /fapi/v1/exchangeInfo   tickSize/stepSize/minNotional (public)

Hata sınıflandırması (§2.3 UNKNOWN sözleşmesi):
  ExchangeUnavailable  — GET zaman aşımı/ağ: durum BİLİNİYOR (uygulanmadı),
                         güvenle tekrar denenebilir.
  UnknownResult        — POST/DELETE zaman aşımı: emir durumu BİLİNMİYOR.
                         Aynı emir KÖRLEMESİNE tekrar POST EDİLMEZ; client id
                         ile sorgu yapılır (§2.3).
  BinanceApiError      — borsa kesin reddi (kod+mesaj). -2011 ve -2021 için
                         yardımcılar: is_unknown_order() / is_immediately_trigger().
"""
import hashlib
import hmac
import time
import urllib.parse
from typing import Optional

import requests

BASE_URL = "https://fapi.binance.com"

# Binance hata kodları (dokümante semantikler)
CODE_UNKNOWN_ORDER = -2011        # cancel/fill yarışı: emir zaten yok (doldu/iptal)
CODE_IMMEDIATELY_TRIGGER = -2021  # "Order would immediately trigger"


class ExchangeError(Exception):
    """Borsa katmanı hatası (temel)."""


class ExchangeUnavailable(ExchangeError):
    """Ağ/timeout — GET sonrası: istek UYGULANMADI, durum biliniyor."""


class UnknownResult(ExchangeError):
    """POST/DELETE timeout — emir durumu BİLİNMİYOR (§2.3).
    Aynı intent körlemesine tekrar gönderilemez; client id ile çözülür."""

    def __init__(self, method, path, client_id=None, cause=None):
        self.method = method
        self.path = path
        self.client_id = client_id
        self.cause = cause
        super().__init__(f"UNKNOWN_RESULT {method} {path} client_id={client_id} "
                         f"cause={type(cause).__name__}: {cause}")


class BinanceApiError(ExchangeError):
    """Borsa kesin reddi / API hatası (kod + mesaj)."""

    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__(f"BINANCE {code}: {msg}")

    def is_unknown_order(self):
        return self.code == CODE_UNKNOWN_ORDER

    def is_immediately_trigger(self):
        return self.code == CODE_IMMEDIATELY_TRIGGER


class BinanceFuturesClient:
    """Tek ağ boğum noktası: _request. Testler bu sınıfı kullanmaz
    (FakeExchange); canlıda api_key/api_secret gerekir. Hesap ayarı
    DEĞİŞTİREN endpoint (POST positionSide/dual, leverage, marginType)
    bu istemcide YOKTUR — salt-okunur + emir gönderimi."""

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = BASE_URL, timeout: float = 10.0,
                 session=None, clock_ms=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    # ---- imza + tek ağ çıkışı ------------------------------------------------
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self.api_secret.encode(), qs.encode(),
                       hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _request(self, http_method: str, path: str, params: Optional[dict],
                 signed: bool, mutating: bool):
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._clock_ms()
            params["recvWindow"] = 5000
            url = f"{self.base_url}{path}?{self._sign(params)}"
        else:
            url = f"{self.base_url}{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
        headers = {"X-MBX-APIKEY": self.api_key}
        try:
            r = self.session.request(http_method, url, data=None
                                     if not signed else None,
                                     params=None if signed else params,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException as e:            # timeout/DNS/conn
            if mutating:
                # POST/DELETE → sonuç bilinmiyor (§2.3)
                cid = (params.get("newClientOrderId")
                       or params.get("clientAlgoId")
                       or params.get("origClientOrderId"))
                raise UnknownResult(http_method, path, cid, e)
            raise ExchangeUnavailable(f"{http_method} {path}: "
                                      f"{type(e).__name__}: {e}")
        if r.status_code >= 500:
            # sunucu hatası: GET → uygulanmadı; POST → belirsiz
            if mutating:
                cid = (params.get("newClientOrderId")
                       or params.get("clientAlgoId"))
                raise UnknownResult(http_method, path, cid,
                                    RuntimeError(f"HTTP {r.status_code}"))
            raise ExchangeUnavailable(f"{http_method} {path}: "
                                      f"HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise ExchangeUnavailable(f"{http_method} {path}: HTTP "
                                      f"{r.status_code} non-JSON")
        if isinstance(body, dict) and "code" in body and "msg" in body \
                and r.status_code >= 400:
            raise BinanceApiError(body["code"], body["msg"])
        return body

    # ---- public -----------------------------------------------------------
    def server_time(self):
        return self._request("GET", "/fapi/v1/time", None, False, False)

    def premium_index(self):
        """REST_SAMPLED_MARK_PRICE — tüm semboller (tek istek)."""
        return self._request("GET", "/fapi/v1/premiumIndex", None, False, False)

    def klines(self, symbol, interval="4h", limit=410):
        return self._request("GET", "/fapi/v1/klines",
                             {"symbol": symbol, "interval": interval,
                              "limit": limit}, False, False)

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo", None, False, False)

    def funding_rate(self, symbol, start_ms=None, limit=100):
        p = {"symbol": symbol, "limit": limit}
        if start_ms is not None:
            p["startTime"] = start_ms
        return self._request("GET", "/fapi/v1/fundingRate", p, False, False)

    # ---- imzalı GET (salt-okunur) -------------------------------------------
    def position_risk(self):
        """Flat doğrulamanın TEK kabul edilen kaynağı (§2.1 adım 9, §3 adım 9)."""
        return self._request("GET", "/fapi/v2/positionRisk", None, True, False)

    def account(self):
        return self._request("GET", "/fapi/v2/account", None, True, False)

    def balance(self):
        return self._request("GET", "/fapi/v2/balance", None, True, False)

    def open_orders(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", p, True, False)

    def open_algo_orders(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/algoOpenOrders", p, True, False)

    def query_order(self, symbol, order_id=None, client_order_id=None):
        """clientOrderId ile sorgu — UNKNOWN çözümünün birinci yolu (§2.3)."""
        p = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = order_id
        if client_order_id is not None:
            p["origClientOrderId"] = client_order_id
        return self._request("GET", "/fapi/v1/order", p, True, False)

    def query_algo_order(self, symbol=None, algo_id=None, client_algo_id=None):
        p = {}
        if symbol:
            p["symbol"] = symbol
        if algo_id is not None:
            p["algoId"] = algo_id
        if client_algo_id is not None:
            p["clientAlgoId"] = client_algo_id
        return self._request("GET", "/fapi/v1/algoOrder", p, True, False)

    def leverage_brackets(self):
        """Gerçek bakım marjı katmanları (§6) — imzalı salt-okunur."""
        return self._request("GET", "/fapi/v1/leverageBracket", None, True,
                             False)

    def position_mode(self):
        """GET /fapi/v1/positionSide/dual — One-way / Hedge sorgusu."""
        return self._request("GET", "/fapi/v1/positionSide/dual", None, True,
                             False)

    def commission_rate(self):
        return self._request("GET", "/fapi/v1/commissionRate", None, True,
                             False)

    def user_trades(self, symbol, start_ms=None, limit=100):
        p = {"symbol": symbol, "limit": limit}
        if start_ms is not None:
            p["startTime"] = start_ms
        return self._request("GET", "/fapi/v1/userTrades", p, True, False)

    def income(self, symbol=None, income_type=None, start_ms=None, limit=100):
        p = {"limit": limit}
        if symbol:
            p["symbol"] = symbol
        if income_type:
            p["incomeType"] = income_type
        if start_ms is not None:
            p["startTime"] = start_ms
        return self._request("GET", "/fapi/v1/income", p, True, False)

    # ---- imzalı POST/DELETE (emir — canlıda yalnız operatör onayı SONRASI) ----
    def new_order(self, symbol, side, type_, quantity=None, price=None,
                  reduce_only=None, client_order_id=None,
                  new_order_resp_type="RESULT", time_in_force=None,
                  position_side=None):
        """Normal emir. reduceOnly yalnız One-way; closePosition ile çakışmaz
        (bu metot closePosition GÖNDERMEZ — koruma için new_algo_order)."""
        p = {"symbol": symbol, "side": side, "type": type_,
             "newOrderRespType": new_order_resp_type}
        if quantity is not None:
            p["quantity"] = quantity
        if price is not None:
            p["price"] = price
        if time_in_force is not None:
            p["timeInForce"] = time_in_force
        if reduce_only is not None:
            p["reduceOnly"] = "true" if reduce_only else "false"
        if position_side is not None:
            p["positionSide"] = position_side
        if client_order_id is not None:
            p["newClientOrderId"] = client_order_id
        return self._request("POST", "/fapi/v1/order", p, True, True)

    def new_algo_order(self, symbol, side, type_, trigger_price,
                       working_type="MARK_PRICE", close_position=None,
                       quantity=None, reduce_only=None, client_algo_id=None,
                       price_protect=None):
        """CONDITIONAL emir (STOP_MARKET/TAKE_PROFIT_MARKET).
        closePosition=true → quantity VE reduceWith ASLA gönderilmez
        (dokümante çakışma). triggerPrice + workingType=MARK_PRICE."""
        p = {"algoType": "CONDITIONAL", "symbol": symbol, "side": side,
             "type": type_, "triggerPrice": trigger_price,
             "workingType": working_type}
        if close_position is not None:
            p["closePosition"] = "true" if close_position else "false"
        if quantity is not None:
            p["quantity"] = quantity
        if reduce_only is not None:
            p["reduceOnly"] = "true" if reduce_only else "false"
        if client_algo_id is not None:
            p["clientAlgoId"] = client_algo_id
        if price_protect is not None:
            p["priceProtect"] = "true" if price_protect else "false"
        return self._request("POST", "/fapi/v1/algoOrder", p, True, True)

    def cancel_order(self, symbol, order_id=None, client_order_id=None):
        p = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = order_id
        if client_order_id is not None:
            p["origClientOrderId"] = client_order_id
        return self._request("DELETE", "/fapi/v1/order", p, True, True)

    def cancel_algo_order(self, symbol=None, algo_id=None, client_algo_id=None):
        p = {}
        if symbol:
            p["symbol"] = symbol
        if algo_id is not None:
            p["algoId"] = algo_id
        if client_algo_id is not None:
            p["clientAlgoId"] = client_algo_id
        return self._request("DELETE", "/fapi/v1/algoOrder", p, True, True)
