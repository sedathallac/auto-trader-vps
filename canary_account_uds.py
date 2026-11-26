"""canary_account_uds.py — AŞAMA B: User Data Stream (UDS) istemcisi.

GÖREV (AŞAMA B): listenKey ile WS bağlanır, hesap olaylarını işler,
delta/patch uygular, dedup yapar; REST ile karşılaştırmaya hazır teşhis
üretir. Gözlem/gölge modu — canlı akışa BAĞLI DEĞİLDİR.

GÜVENLİK SÖZLEŞMESİ:
  - listenKey YALNIZCA resmî mekanizmayla yönetilir: POST (başlat) ve
    PUT (60 dk keepalive). DELETE (stream silme) ASLA çağrılmaz —
    transport'ta DELETE metodu bulunmaz (test güvencesi).
  - listenKey, API key, secret, imza HİÇBİR kayda/repr'a yazılmaz:
    UdsSessionResult.ws_url her zaman redakte edilir; olay kayıtlarında
    yalnızca olay içerikleri tutulur.
  - Bu modül emir göndermez; hiçbir state değişikliği REST'e yazılmaz.

DOKÜMAN-DOĞRULANMIŞ GERÇEKLER (2026-11-26, USDⓈ-M UDS sayfası):
  - WS: wss://fstream.binance.com/ws/<listenKey>
  - listenKey 60 dk geçerli; PUT uzatır; süresi dolarsa -1125.
  - listenKeyExpired olayı WS kopmasıyla İLGİLİ DEĞİLDİR; alındıktan
    sonra yeni geçerli key kullanılana dek olay GELMEZ → DESYNCED.
  - Olay türleri: MARGIN_CALL, ACCOUNT_UPDATE, ORDER_TRADE_UPDATE,
    ACCOUNT_CONFIG_UPDATE, TRADE_LITE, CONDITIONAL_ORDER_TRIGGER_REJECT,
    STRATEGY_UPDATE, GRID_UPDATE, ALGO_UPDATE, listenKeyExpired.

ALAN SEMANTİĞİ (KANIT DURUMU): olay adları ve davranış kuralları yukarıda
doküman-doğrulanmıştır; payload ALAN adlarının çoğu için resmî proza
sayfaları JS-render olduğundan KANIT EKSİK → DOC_ASSUMED işaretli
parser'lar fail-closed davranır: eksik alan → olay kaydedilir, ilgili
bileşen None kalır, durum 'unverified' olur; asla uydurulmuş değer YOK.

E/T/YEREL ZAMAN AYRIMI: her olayda E (event time) ve T (transaction
time) varsa ayrı ayrı tutulur; local_receive_time_ms istemci saatiyle
recv anında ölçülür. Üçü asla birbirinin yerine kullanılmaz.

DESYNC KURALLARI (fail-closed):
  - listenKeyExpired → DESYNCED (stream öldü; yeni key gerekir)
  - parse hatası / bozuk JSON → DESYNCED
  - bağlantı süre bitmeden kapandı → DESYNCED
  - bilinmeyen Olay TÜRÜ → DESYNCED DEĞİL: bilinen-liste dışı tür
    kaydedilir (unknown_event_types) + model kapsamı notu; hesap/pozisyon/
    emir durumu etkilenmiş olabilir → state 'coverage_limited' işareti.
  - ACCOUNT_UPDATE delta/patch'tir: yalnız değişen varlık/semboller
    gelir; OLMAYAN anahtar SIFIRLANMAZ (envanter değil fark).

Kural (görev 9): UDS'ten uygulanan iç durum KESİN bakiye gibi SUNULMAZ;
yalnızca REST baseline ile karşılaştırmaya giren teşhis görünümü üretir.
"""
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from canary_account_adapter import (
    BinanceHttpTransport, UDS_WS_BASE, redact,
)

# Dokümanda adı geçen tüm olay türleri (yukarıda doğrulandı).
KNOWN_EVENT_TYPES: Tuple[str, ...] = (
    "MARGIN_CALL", "ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE",
    "ACCOUNT_CONFIG_UPDATE", "TRADE_LITE",
    "CONDITIONAL_ORDER_TRIGGER_REJECT", "STRATEGY_UPDATE", "GRID_UPDATE",
    "ALGO_UPDATE", "listenKeyExpired",
)

# Modelimizde ayrıştırdığımız (alanları işlediğimiz) türler.
PARSED_EVENT_TYPES: Tuple[str, ...] = (
    "ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE", "ACCOUNT_CONFIG_UPDATE",
    "MARGIN_CALL", "listenKeyExpired",
)


class UdsParseError(ValueError):
    """Bozuk/eksik olay yükü — DESYNCED tetikler."""


# ---------------------------------------------------------------------------
# Olay modelleri (alan semantiği DOC_ASSUMED — bkz. modül docstring)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UdsBalanceDelta:
    asset: str
    balance: Optional[float] = None            # 'B' walletBalance
    cross_wallet_balance: Optional[float] = None  # 'cw' crossWalletBalance
    balance_change: Optional[float] = None     # 'bc' (PnL/komisyon HARİÇ)
    cross_un_pnl: Optional[float] = None       # 'cc' crossUnPnl


@dataclass(frozen=True)
class UdsPositionDelta:
    symbol: str
    position_side: Optional[str] = None        # 'ps'
    position_amount: Optional[float] = None    # 'pa'
    entry_price: Optional[float] = None        # 'ep'
    margin_type: Optional[str] = None          # 'mt' cross/isolated
    isolated_wallet: Optional[float] = None    # 'iw'
    unrealized_pnl: Optional[float] = None     # 'up'


@dataclass(frozen=True)
class UdsEvent:
    event_type: str
    local_receive_time_ms: int
    event_time_ms: Optional[int] = None        # 'E' — borsa olay saati
    transaction_time_ms: Optional[int] = None  # 'T' — borsa işlem saati
    balances: Tuple[UdsBalanceDelta, ...] = ()
    positions: Tuple[UdsPositionDelta, ...] = ()
    order: Optional[dict] = None               # 'o' (ORDER_TRADE_UPDATE)
    config_update: Optional[dict] = None       # ACCOUNT_CONFIG_UPDATE
    margin_call_positions: Tuple[dict, ...] = ()
    raw_size_bytes: Optional[int] = None
    fingerprint: str = ""                      # dedup anahtarı
    parse_note: Optional[str] = None           # eksik alan notu (uydurma YOK)

    def to_safe_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "event_time_ms": self.event_time_ms,
            "transaction_time_ms": self.transaction_time_ms,
            "local_receive_time_ms": self.local_receive_time_ms,
            "n_balances": len(self.balances),
            "n_positions": len(self.positions),
            "has_order": self.order is not None,
            "has_config_update": self.config_update is not None,
            "n_margin_call_positions": len(self.margin_call_positions),
            "fingerprint": self.fingerprint,
            "parse_note": self.parse_note,
        }


def _opt_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _opt_int(v) -> Optional[int]:
    f = _opt_float(v)
    if f is None or f != int(f):
        return None
    return int(f)


def _fingerprint(d: dict) -> str:
    canon = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def parse_uds_message(payload: dict, local_receive_time_ms: int) -> UdsEvent:
    """Tek olay yükünü UdsEvent'e çevirir (fail-closed, uydurma YOK).

    Bilinmeyen TÜR: known=False değil — parse_uds_message her adı kabul
    eder, 'known' sınıflandırması UdsSession tarafında yapılır. Zorunlu
    yapı bozuksa (e/E yok, a dizisi dict değil vb.) UdsParseError.
    """
    if not isinstance(payload, dict):
        raise UdsParseError("olay yükü dict değil")
    etype = payload.get("e")
    if not etype or not isinstance(etype, str):
        raise UdsParseError("olay türü (e) yok")
    etime = _opt_int(payload.get("E"))
    ttime = _opt_int(payload.get("T"))
    notes: List[str] = []

    balances: Tuple[UdsBalanceDelta, ...] = ()
    positions: Tuple[UdsPositionDelta, ...] = ()
    order = None
    config_update = None
    mc_positions: Tuple[dict, ...] = ()

    if etype == "ACCOUNT_UPDATE":
        a = payload.get("a")
        if not isinstance(a, dict):
            raise UdsParseError("ACCOUNT_UPDATE gövdesi (a) yok/bozuk")
        B = a.get("B") or []
        P = a.get("P") or []
        if not isinstance(B, list) or not isinstance(P, list):
            raise UdsParseError("ACCOUNT_UPDATE B/P dizisi bozuk")
        bl = []
        for b in B:
            if not isinstance(b, dict) or not b.get("a"):
                notes.append("balance satırı asset'siz — atlandı")
                continue
            bl.append(UdsBalanceDelta(
                asset=str(b["a"]),
                balance=_opt_float(b.get("B")),
                cross_wallet_balance=_opt_float(b.get("cw")),
                balance_change=_opt_float(b.get("bc")),
                cross_un_pnl=_opt_float(b.get("cc")),
            ))
        pl = []
        for p in P:
            if not isinstance(p, dict) or not p.get("s"):
                notes.append("position satırı symbol'süz — atlandı")
                continue
            pl.append(UdsPositionDelta(
                symbol=str(p["s"]),
                position_side=p.get("ps"),
                position_amount=_opt_float(p.get("pa")),
                entry_price=_opt_float(p.get("ep")),
                margin_type=p.get("mt"),
                isolated_wallet=_opt_float(p.get("iw")),
                unrealized_pnl=_opt_float(p.get("up")),
            ))
        balances, positions = tuple(bl), tuple(pl)
        if not balances and not positions:
            notes.append("ACCOUNT_UPDATE gövdesi boş (B/P yok)")
    elif etype == "ORDER_TRADE_UPDATE":
        o = payload.get("o")
        if not isinstance(o, dict):
            raise UdsParseError("ORDER_TRADE_UPDATE gövdesi (o) yok")
        order = dict(o)
    elif etype == "ACCOUNT_CONFIG_UPDATE":
        ac = payload.get("ac")
        ai = payload.get("ai")
        if isinstance(ac, dict) or isinstance(ai, dict):
            config_update = {"ac": ac, "ai": ai}
        else:
            notes.append("ACCOUNT_CONFIG_UPDATE gövdesi (ac/ai) yok")
    elif etype == "MARGIN_CALL":
        p = payload.get("p")
        if isinstance(p, list):
            mc_positions = tuple(p)
        else:
            notes.append("MARGIN_CALL gövdesi (p) yok")

    return UdsEvent(
        event_type=etype,
        local_receive_time_ms=local_receive_time_ms,
        event_time_ms=etime,
        transaction_time_ms=ttime,
        balances=balances,
        positions=positions,
        order=order,
        config_update=config_update,
        margin_call_positions=mc_positions,
        raw_size_bytes=len(json.dumps(payload)) if payload else 0,
        fingerprint=_fingerprint(payload),
        parse_note="; ".join(notes) if notes else None,
    )


# ---------------------------------------------------------------------------
# Delta durum (teşhis; KESİN bakiye DEĞİL)
# ---------------------------------------------------------------------------

class UdsDeltaState:
    """WS olaylarından uygulanan delta/patch görünümü.

    KURAL (görev 9): bu durum envanter DEĞİLDİR — REST baseline ile
    karşılaştırma girdisi olarak sunulur; hesap toplamlarını ASLA
    türetmez/değiştirmez. Olayda gelmeyen varlık/sembol SIFIRLANMAZ.
    """

    def __init__(self):
        self.balances: Dict[str, dict] = {}
        self.positions: Dict[Tuple[str, str], dict] = {}
        self.applied: int = 0
        self.duplicates: int = 0
        self._seen: set = set()

    def apply(self, event: UdsEvent) -> bool:
        """Olayı uygular. Aynı fingerprint tekrar gelirse dedup (False)."""
        if event.fingerprint and event.fingerprint in self._seen:
            self.duplicates += 1
            return False
        if event.fingerprint:
            self._seen.add(event.fingerprint)
        for b in event.balances:
            cur = self.balances.setdefault(b.asset, {})
            for k, v in (("balance", b.balance),
                         ("cross_wallet_balance", b.cross_wallet_balance),
                         ("balance_change", b.balance_change),
                         ("cross_un_pnl", b.cross_un_pnl),
                         ("last_update_ms", event.event_time_ms)):
                if v is not None:
                    cur[k] = v
        for p in event.positions:
            key = (p.symbol, p.position_side or "BOTH")
            cur = self.positions.setdefault(key, {})
            for k, v in (("position_amount", p.position_amount),
                         ("entry_price", p.entry_price),
                         ("margin_type", p.margin_type),
                         ("isolated_wallet", p.isolated_wallet),
                         ("unrealized_pnl", p.unrealized_pnl),
                         ("last_update_ms", event.event_time_ms)):
                if v is not None:
                    cur[k] = v
        self.applied += 1
        return True

    def to_safe_dict(self) -> dict:
        return {
            "applied_events": self.applied,
            "duplicate_events": self.duplicates,
            "tracked_assets": sorted(self.balances.keys()),
            "tracked_position_keys": [f"{s}|{ps}"
                                      for (s, ps) in sorted(self.positions)],
            "note": "delta/patch teşhis görünümü; kesin bakiye DEĞİL "
                    "(olmayan anahtar sıfırlanmaz, envanter REST'ten)",
        }


# ---------------------------------------------------------------------------
# Oturum sonucu ve istemci
# ---------------------------------------------------------------------------

ST_CONNECTED = "CONNECTED"
ST_NOT_CONNECTED = "NOT_CONNECTED"
ST_EXPIRED = "LISTEN_KEY_EXPIRED"
ST_PARSE_ERROR = "PARSE_ERROR"
ST_CLOSED_EARLY = "CLOSED_EARLY"


@dataclass(frozen=True)
class UdsSessionResult:
    stream_state: str                       # ST_* (ilk ciddi durum)
    connected: bool
    listen_key_created: bool
    keepalive_put_ok: Optional[bool]
    events: Tuple[UdsEvent, ...]
    duplicates: int
    parse_errors: int
    unknown_event_types: Tuple[str, ...]    # doküman-listesi DIŞI türler
    coverage_limited: bool                  # bilinen ama ayrıştırılmayan tür geldi
    ws_url: str                             # HER ZAMAN redakte
    session_started_at_ms: Optional[int]
    session_ended_at_ms: Optional[int]
    error_detail: Optional[str] = None

    def to_safe_dict(self) -> dict:
        return {
            "stream_state": self.stream_state,
            "connected": self.connected,
            "listen_key_created": self.listen_key_created,
            "keepalive_put_ok": self.keepalive_put_ok,
            "n_events": len(self.events),
            "event_types": sorted({e.event_type for e in self.events}),
            "duplicates": self.duplicates,
            "parse_errors": self.parse_errors,
            "unknown_event_types": list(self.unknown_event_types),
            "coverage_limited": self.coverage_limited,
            "ws_url": self.ws_url,
            "session_started_at_ms": self.session_started_at_ms,
            "session_ended_at_ms": self.session_ended_at_ms,
            "error_detail": self.error_detail,
            "events": [e.to_safe_dict() for e in self.events],
        }


class UdsClient:
    """Tek atımlık (probe) UDS oturumu. Gözlem modu; kalıcı bağlantı DEĞİL.

    Test edilebilirlik: ws_factory(url, timeout) bağlantı benzeri nesne
    döndürür (recv()/close()); testlerde FakeWs enjekte edilir.
    clock_ms: zaman enjeksiyonu. duration_s: gözlem süresi.
    """

    def __init__(self, transport: BinanceHttpTransport,
                 ws_factory=None, ws_url_base: str = UDS_WS_BASE,
                 duration_s: float = 20.0, clock_ms=None,
                 connect_timeout: float = 5.0):
        self.transport = transport
        self._ws_factory = ws_factory
        self._ws_url_base = ws_url_base.rstrip("/")
        self.duration_s = duration_s
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.connect_timeout = connect_timeout

    def _default_ws_factory(self, url: str, timeout: float):
        import websocket  # websocket-client (mark_price_observer ile aynı)
        return websocket.create_connection(url, timeout=timeout)

    def run_probe(self) -> UdsSessionResult:
        t_start = self._clock_ms()
        events: List[UdsEvent] = []
        unknown_types: List[str] = []
        state = UdsDeltaState()
        parse_errors = 0
        coverage_limited = False
        first_state = None
        error_detail = None
        connected = False
        keepalive_ok: Optional[bool] = None

        # 1) listenKey POST (resmî başlatma; DELETE YOK)
        lk_resp = self.transport.listen_key_post()
        listen_key = None
        if lk_resp.ok and isinstance(lk_resp.body, dict):
            listen_key = lk_resp.body.get("listenKey")
        if not listen_key:
            detail = f"listenKey POST başarısız: status={lk_resp.status_code}"
            if isinstance(lk_resp.body, dict) and lk_resp.body.get("code") is not None:
                detail += f" code={lk_resp.body.get('code')}"
            if lk_resp.error:
                detail += f" {lk_resp.error}"
            return UdsSessionResult(
                stream_state=ST_NOT_CONNECTED,
                connected=False, listen_key_created=False,
                keepalive_put_ok=None, events=(), duplicates=0,
                parse_errors=0, unknown_event_types=(),
                coverage_limited=False,
                ws_url=f"{self._ws_url_base}/<no-key>",
                session_started_at_ms=t_start,
                session_ended_at_ms=self._clock_ms(),
                error_detail=detail)
        # 2) keepalive PUT (60 dk uzatma; doküman-doğrulanmış)
        ka_resp = self.transport.listen_key_put()
        keepalive_ok = ka_resp.ok

        url = f"{self._ws_url_base}/{listen_key}"
        factory = self._ws_factory or self._default_ws_factory
        ws = None
        try:
            ws = factory(url, self.connect_timeout)
            connected = True
            ws.settimeout = getattr(ws, "settimeout", None)
            if callable(ws.settimeout):
                ws.settimeout(1.0)  # dilimli recv → süre kontrolü
            deadline = t_start + int(self.duration_s * 1000)
            while self._clock_ms() < deadline:
                try:
                    raw = ws.recv()
                except Exception as e:  # timeout → devam; close/bozuk → çık
                    ename = type(e).__name__
                    if ename in ("WebSocketTimeoutException", "SocketTimeout",
                                 "timeout", "TimeoutError"):
                        continue
                    if first_state is None:
                        first_state = ST_CLOSED_EARLY
                        error_detail = f"recv hatası: {ename}"
                    break
                if raw is None or raw == "":
                    continue
                now_local = self._clock_ms()
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    parse_errors += 1
                    if first_state is None:
                        first_state = ST_PARSE_ERROR
                        error_detail = "JSON parse hatası (ham metin loglanmaz)"
                    continue
                try:
                    ev = parse_uds_message(payload, now_local)
                except UdsParseError as e:
                    parse_errors += 1
                    if first_state is None:
                        first_state = ST_PARSE_ERROR
                        error_detail = f"olay parse hatası: {e}"
                    continue
                events.append(ev)
                if ev.event_type == "listenKeyExpired":
                    if first_state is None:
                        first_state = ST_EXPIRED
                        error_detail = ("listenKeyExpired: yeni geçerli key'e "
                                        "kadar olay GELMEZ (doküman)")
                if ev.event_type not in KNOWN_EVENT_TYPES:
                    unknown_types.append(ev.event_type)
                if ev.event_type not in PARSED_EVENT_TYPES:
                    coverage_limited = True
                state.apply(ev)
        except Exception as e:
            if first_state is None:
                first_state = ST_NOT_CONNECTED
                error_detail = f"WS bağlantı hatası: {type(e).__name__}"
        finally:
            if ws is not None:
                try:
                    ws.close()   # yerel kapatma; DELETE çağrısı YOK
                except Exception:
                    pass

        t_end = self._clock_ms()
        stream_state = first_state or (ST_CONNECTED if connected
                                       else ST_NOT_CONNECTED)
        return UdsSessionResult(
            stream_state=stream_state,
            connected=connected,
            listen_key_created=True,
            keepalive_put_ok=keepalive_ok,
            events=tuple(events),
            duplicates=state.duplicates,
            parse_errors=parse_errors,
            unknown_event_types=tuple(sorted(set(unknown_types))),
            coverage_limited=coverage_limited,
            ws_url=redact(url),
            session_started_at_ms=t_start,
            session_ended_at_ms=t_end,
            error_detail=error_detail,
        )
