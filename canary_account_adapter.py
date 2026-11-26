"""canary_account_adapter.py — AŞAMA B: salt-okunur REST hesap adaptörü.

GÖREV (AŞAMA B): gerçek Binance USDⓈ-M hesap verisini, sembol filtresi
OLMAKSIZIN toplar; her çağrının zamanını, HTTP durumunu, kayıt adedini ve
rate-limit header'larını kaydeder. AŞAMA A modelini (canary_account_model)
değiştirmeden kullanır.

GÜVENLİK SÖZLEŞMESİ:
  - Bu modülde EMİR FONKSİYONU YOKTUR; BinanceFuturesClient (emir istemcisi)
    BURADA IMPORT EDİLMEZ/TAŞINMAZ. Transport yalnızca:
      * imzalı/imzasız GET (salt-okunur veri),
      * UDS için listenKey POST/PUT (resmî stream başlatma/keepalive;
        DELETE — stream silme — ASLA çağrılmaz).
  - Yalnızca SAFE_GET_ENDPOINTS beyaz listesindeki yollar çağrılır.
  - API anahtarı/secret/listenKey/imza HİÇBİR kayda, loga veya repr'e
    yazılmaz (redact() yardımcıları + test güvencesi).

REST ATOMİK DEĞİLDİR: her endpoint kendi RestCallRecord zamanlarını taşır;
baseline tek zaman damgasına indirgenmez.

DOUBLE-COUNT / KAPSAM KURALLARI (AŞAMA A, aynen):
  availableBalance borsa değeridir (yeniden hesaplanmaz);
  totalCrossWalletBalance/totalCrossUnPnl hesap-seviyesi ve cross-kapsam;
  pozisyon uPnL'leri hesap toplamına EKLENMEZ (yalnız teşhis);
  CROSS/ISOLATED ayrık; açık emir marjini ikinci kez sayma YOK.

Doküman doğrulama (2026-11-26, developers.binance.com):
  DOC_VERIFIED : accountConfig, v2/v3 account (alan adları), v3 balance,
                 listenKey kuralları (60 dk, -1125, PUT uzatma).
  ASSUMED      : v2 positionRisk prozası, openOrders/openAlgoOrders şemaları
                 (alan adları gerçek yanıtlarda kararlı; proza JS-render).

Ağırlık ölçümü dürüstlüğü: X-MBX-USED-WEIGHT-1M IP-GENELİDİR — aynı IP'den
çağrı yapan başka süreçler değeri etkiler; ölçülen delta yalnızca
'buadaptör-tek-istemci + serial çağrı' varsayımı altında bu çağrının
ağırlığına yaklaştır. Kesin ölçüm olarak SUNULMAZ.
"""
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from canary_account_model import (
    AccountConfigSnapshot, AccountTotals, BalanceSnapshot, OpenOrderSnapshot,
    OpenAlgoOrderSnapshot, PositionSnapshot, SnapshotMetadata,
    InvalidFieldError, parse_account_config, parse_account_v2,
    parse_account_v3, parse_account_positions, parse_balance_v3,
    parse_position_risk, parse_open_orders, parse_open_algo_orders,
    DOC_VERIFIED, DOC_ASSUMED,
)

BASE_URL = "https://fapi.binance.com"
UDS_WS_BASE = "wss://fstream.binance.com/ws"

# Bu adaptörün çağırabileceği TEK endpoint kümesi (beyaz liste).
SAFE_GET_ENDPOINTS: Tuple[str, ...] = (
    "/fapi/v1/accountConfig",     # DOC_VERIFIED, weight 5
    "/fapi/v2/account",           # DOC_VERIFIED, weight 5
    "/fapi/v3/account",           # DOC_VERIFIED, weight 5
    "/fapi/v3/balance",           # DOC_VERIFIED, weight 5
    "/fapi/v2/positionRisk",      # şema ASSUMED; ağırlık ölçülecek
    "/fapi/v1/openOrders",        # şema ASSUMED; ağırlık ölçülecek
    "/fapi/v1/openAlgoOrders",    # şema ASSUMED; ağırlık ölçülecek
)

# Yakalanacak rate-limit header adları (ham değerler saklanır).
RATE_LIMIT_HEADERS = ("X-MBX-USED-WEIGHT-1M", "X-MBX-USED-WEIGHT",
                      "X-MBX-ORDER-COUNT-1M", "Retry-After")

# Redaksiyon: hassas anahtar adları ve bunların değerleri aslek kayda girmez.
SENSITIVE_KEYS = ("listenkey", "listenKey", "apikey", "api_key",
                  "apisecret", "api_secret", "secret", "signature",
                  "x-mbx-apikey")

DOC_STATUS_BY_ENDPOINT = {
    "/fapi/v1/accountConfig": DOC_VERIFIED,
    "/fapi/v2/account": DOC_VERIFIED,
    "/fapi/v3/account": DOC_VERIFIED,
    "/fapi/v3/balance": DOC_VERIFIED,
    "/fapi/v2/positionRisk": DOC_ASSUMED,
    "/fapi/v1/openOrders": DOC_ASSUMED,
    "/fapi/v1/openAlgoOrders": DOC_ASSUMED,
}


def redact(obj):
    """Dict/list/string içinden hassas anahtar değerlerini maskeler.
    Döndürülen yapı log/rapor güvenlidir; orijinal DEĞİŞTİRİLMEZ."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s.lower() == k.lower()
                                          for s in SENSITIVE_KEYS):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        # URL/qs içindeki signature/listenKey değerlerini de maskele
        if "signature=" in obj:
            obj = obj.split("signature=")[0] + "signature=<redacted>"
        if "/ws/" in obj and len(obj.split("/ws/")[-1]) > 8:
            # listenKey içeren ws url'si
            obj = obj.split("/ws/")[0] + "/ws/<redacted>"
        return obj
    return obj


# ---------------------------------------------------------------------------
# Transport katmanı (yalnız ağ; parser YOK — testlerde FakeTransport ile
# değiştirilir)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransportResponse:
    status_code: Optional[int]
    headers: Dict[str, str]
    body: object                    # parse edilmiş JSON (veya ham metin)
    error: Optional[str]            # ağ/timeout hatası açıklaması
    request_started_at_ms: Optional[int]
    response_received_at_ms: Optional[int]

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and self.error is None


class BinanceHttpTransport:
    """Emir YOK. Yalnız GET + listenKey POST/PUT.

    BinanceFuturesClient'ten BAĞIMSIZDIR (emir istemcisine referans
    TAŞINMAZ); imzalama (~10 satır) bilinçli olarak yinelenir — izolasyon
    için. Testler bu sınıfın arayüzünü uygulayan FakeTransport kullanır.
    """

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = BASE_URL, timeout: float = 10.0,
                 session=None, clock_ms=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    # -- imza (client'tan yalıtılmış kopya) --
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self.api_secret.encode(), qs.encode(),
                       hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _do(self, http_method: str, path: str, params: Optional[dict],
            signed: bool) -> TransportResponse:
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._clock_ms()
            params["recvWindow"] = 5000
            url = f"{self.base_url}{path}?{self._sign(params)}"
            qs_params = None
        else:
            url = f"{self.base_url}{path}"
            qs_params = params or None
        headers = {"X-MBX-APIKEY": self.api_key}
        t0 = self._clock_ms()
        try:
            r = self.session.request(http_method, url, params=qs_params,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            return TransportResponse(
                status_code=None, headers={}, body=None,
                error=f"{type(e).__name__}: {e}",
                request_started_at_ms=t0, response_received_at_ms=None)
        t1 = self._clock_ms()
        rl_headers = {h: str(r.headers.get(h))
                      for h in RATE_LIMIT_HEADERS if r.headers.get(h) is not None}
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return TransportResponse(
            status_code=r.status_code, headers=rl_headers, body=body,
            error=None, request_started_at_ms=t0, response_received_at_ms=t1)

    def signed_get(self, path: str, params: Optional[dict] = None
                   ) -> TransportResponse:
        if path not in SAFE_GET_ENDPOINTS:
            raise ValueError(f"endpoint beyaz liste dışı: {path}")
        return self._do("GET", path, params, signed=True)

    def listen_key_post(self) -> TransportResponse:
        """UDS başlatma (resmî mekanizma; yalnız API key header, imzasız).
        DELETE (stream silme) bu modülde YOKTUR."""
        return self._do("POST", "/fapi/v1/listenKey", None, signed=False)

    def listen_key_put(self) -> TransportResponse:
        """UDS keepalive (60 dk uzatma; doküman-doğrulanmış)."""
        return self._do("PUT", "/fapi/v1/listenKey", None, signed=False)


# ---------------------------------------------------------------------------
# REST çağrı kaydı ve baseline sonucu
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RestCallRecord:
    endpoint: str
    http_status: Optional[int]
    request_started_at_ms: Optional[int]
    response_received_at_ms: Optional[int]
    local_receive_time_ms: Optional[int]      # REST'te = yanıt anı
    record_count: Optional[int]               # dönen satır/obje adedi
    used_weight_1m: Optional[str]             # HAM header (IP-geneli!)
    rate_limit_headers: Dict[str, str]        # ham değerler
    doc_status: str
    error_code: Optional[int] = None          # Binance hata kodu (-2015 vb.)
    error_msg: Optional[str] = None
    parse_error: Optional[str] = None         # Aşama A parser hatası
    weight_delta: Optional[int] = None        # serial-istek varsayımıyla

    def to_safe_dict(self) -> dict:
        """Rapor/log güvenli görünüm (hassas veri YOK)."""
        return {
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "request_started_at_ms": self.request_started_at_ms,
            "response_received_at_ms": self.response_received_at_ms,
            "record_count": self.record_count,
            "used_weight_1m_raw": self.used_weight_1m,
            "rate_limit_headers": dict(self.rate_limit_headers),
            "doc_status": self.doc_status,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
            "parse_error": self.parse_error,
            "weight_delta_serial_assumption": self.weight_delta,
        }


MA_SINGLE = "SINGLE_ASSET"
MA_MULTI = "MULTI_ASSETS"
MA_UNKNOWN = "UNKNOWN"
MA_CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class RestBaselineResult:
    """Tek seferlik REST baseline. ATOMİK DEĞİL — her kaynak kendi zamanında.

    Bir endpoint başarısızsa diğerlerinden gelen veri TAM envanter gibi
    SUNULMAZ: complete=False + incomplete_reasons dolu gelir.
    """
    records: Tuple[RestCallRecord, ...]
    config: Optional[AccountConfigSnapshot]
    totals_v2: Optional[AccountTotals]
    totals_v3: Optional[AccountTotals]
    balance_v3: Optional[BalanceSnapshot]
    positions: Optional[PositionSnapshot]            # positionRisk (filtresiz)
    positions_from_account_v3: Optional[PositionSnapshot]  # doc-verified kaynak
    open_orders: Optional[OpenOrderSnapshot]
    open_algo_orders: Optional[OpenAlgoOrderSnapshot]
    multi_assets_status: str                         # MA_*
    complete: bool
    incomplete_reasons: Tuple[str, ...]

    def to_safe_dict(self) -> dict:
        return {
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "multi_assets_status": self.multi_assets_status,
            "calls": [r.to_safe_dict() for r in self.records],
            "counts": {
                "positions_position_risk":
                    len(self.positions.entries) if self.positions else None,
                "positions_non_flat":
                    len(self.positions.non_flat()) if self.positions else None,
                "positions_account_v3":
                    len(self.positions_from_account_v3.entries)
                    if self.positions_from_account_v3 else None,
                "open_orders":
                    len(self.open_orders.entries) if self.open_orders else None,
                "open_algo_orders":
                    len(self.open_algo_orders.entries)
                    if self.open_algo_orders else None,
            },
        }


def _count(payload) -> Optional[int]:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("positions"), list):
        return len(payload["positions"])
    return None


class AccountAdapter:
    """RestBaseline üreticisi. EMİR YOK — sınıfta emir metodu bulunmaz
    (test güvencesi: test_canary_account_adapter.Güvenlik)."""

    def __init__(self, transport):
        self.transport = transport

    def collect_baseline(self) -> RestBaselineResult:
        calls: List[RestCallRecord] = []
        parsed: Dict[str, object] = {}
        reasons: List[str] = []
        prev_weight: Optional[int] = None

        def call(path: str):
            nonlocal prev_weight
            resp = self.transport.signed_get(path)
            w_raw = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            try:
                w_now = int(w_raw) if w_raw is not None else None
            except ValueError:
                w_now = None
            delta = (w_now - prev_weight) if (w_now is not None
                                              and prev_weight is not None) else None
            prev_weight = w_now if w_now is not None else prev_weight
            err_code = err_msg = parse_err = None
            count = None
            if resp.ok and not (isinstance(resp.body, dict)
                                and "code" in resp.body and "msg" in resp.body):
                count = _count(resp.body)
            elif resp.status_code is not None and resp.status_code >= 400 \
                    and isinstance(resp.body, dict):
                err_code = resp.body.get("code")
                err_msg = resp.body.get("msg")
            elif resp.error:
                err_msg = resp.error
            rec = RestCallRecord(
                endpoint=path,
                http_status=resp.status_code,
                request_started_at_ms=resp.request_started_at_ms,
                response_received_at_ms=resp.response_received_at_ms,
                local_receive_time_ms=resp.response_received_at_ms,
                record_count=count,
                used_weight_1m=w_raw,
                rate_limit_headers=dict(resp.headers),
                doc_status=DOC_STATUS_BY_ENDPOINT.get(path, DOC_ASSUMED),
                error_code=err_code,
                error_msg=err_msg,
                parse_error=parse_err,
                weight_delta=delta,
            )
            calls.append(rec)
            return rec, resp

        def parse_or_flag(rec: RestCallRecord, parser, payload, key):
            path = rec.endpoint
            meta = SnapshotMetadata(
                source_name="rest-baseline", endpoint=path,
                request_started_at_ms=rec.request_started_at_ms,
                response_received_at_ms=rec.response_received_at_ms,
                local_receive_time_ms=rec.local_receive_time_ms,
                source_update_time_ms=None,
                note="REST atomik değil; zamanlar bu çağrıya aittir")
            try:
                parsed[key] = parser(payload, meta)
                return None
            except InvalidFieldError as e:
                reasons.append(f"PARSE_ERROR:{path}:{e.reason}:{e.path}")
                return str(e)

        # 1) accountConfig
        rec, resp = call("/fapi/v1/accountConfig")
        if resp.ok:
            parse_or_flag(rec, parse_account_config, resp.body, "config")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v1/accountConfig:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # 2) v2 account (toplamlar; pozisyon dizisi positionRisk'tan ayrı)
        rec, resp = call("/fapi/v2/account")
        if resp.ok:
            parse_or_flag(rec, parse_account_v2, resp.body, "totals_v2")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v2/account:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # 3) v3 account (toplamlar + doc-verified positions[])
        rec, resp = call("/fapi/v3/account")
        if resp.ok:
            parse_or_flag(rec, parse_account_v3, resp.body, "totals_v3")
            parse_or_flag(rec, parse_account_positions, resp.body, "positions_v3")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v3/account:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # 4) v3 balance
        rec, resp = call("/fapi/v3/balance")
        if resp.ok:
            parse_or_flag(rec, parse_balance_v3, resp.body, "balance_v3")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v3/balance:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # 5) positionRisk (FİLTRESİZ — tüm semboller; sıfır pozisyon dahil)
        rec, resp = call("/fapi/v2/positionRisk")
        if resp.ok:
            parse_or_flag(rec, parse_position_risk, resp.body, "positions")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v2/positionRisk:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # 6) openOrders (tüm semboller; yalnız baseline — polling yok)
        rec, resp = call("/fapi/v1/openOrders")
        if resp.ok:
            parse_or_flag(rec, parse_open_orders, resp.body, "open_orders")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v1/openOrders:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # 7) openAlgoOrders (tüm semboller; yalnız baseline)
        rec, resp = call("/fapi/v1/openAlgoOrders")
        if resp.ok:
            parse_or_flag(rec, parse_open_algo_orders, resp.body, "open_algo_orders")
        else:
            reasons.append(f"ENDPOINT_FAILED:/fapi/v1/openAlgoOrders:"
                           f"{rec.error_code or rec.error_msg or rec.http_status}")

        # Multi-Assets durumu: config (birincil) + v2 account bayrağı (çapraz)
        cfg = parsed.get("config")
        tv2 = parsed.get("totals_v2")
        cfg_ma = cfg.multiAssetsMargin if cfg is not None else None
        v2_ma = tv2.multiAssetsMargin if tv2 is not None else None
        if cfg_ma is None and v2_ma is None:
            ma_status = MA_UNKNOWN
            reasons.append("MULTI_ASSETS_UNKNOWN: iki kaynak da yok")
        elif cfg_ma is None:
            ma_status = MA_MULTI if v2_ma else MA_SINGLE
            reasons.append("MULTI_ASSETS_PARTIAL: yalnız v2 account bayrağı")
        elif v2_ma is None:
            ma_status = MA_MULTI if cfg_ma else MA_SINGLE
        elif cfg_ma != v2_ma:
            ma_status = MA_CONFLICT
            reasons.append("MULTI_ASSETS_CONFLICT: config vs v2 account")
        else:
            ma_status = MA_MULTI if cfg_ma else MA_SINGLE
        if ma_status in (MA_MULTI, MA_CONFLICT, MA_UNKNOWN):
            reasons.append(f"MULTI_ASSETS_STATUS:{ma_status}:"
                           f"işlem destek kapsamı dışı (fail-closed)")

        complete = not reasons
        return RestBaselineResult(
            records=tuple(calls),
            config=parsed.get("config"),
            totals_v2=parsed.get("totals_v2"),
            totals_v3=parsed.get("totals_v3"),
            balance_v3=parsed.get("balance_v3"),
            positions=parsed.get("positions"),
            positions_from_account_v3=parsed.get("positions_v3"),
            open_orders=parsed.get("open_orders"),
            open_algo_orders=parsed.get("open_algo_orders"),
            multi_assets_status=ma_status,
            complete=complete,
            incomplete_reasons=tuple(reasons),
        )
