"""canary_account_model.py — AŞAMA A: saf Binance hesap veri modeli (CANARY AI BRAIN).

GÖREV 3 (Architecture V2, Bölüm 13) kapsamı: Hesap/spot/emir verisinin
RESMÎ Binance yanıtlarından ayrıştırılması, kapsam/doğrulama ve
double-count koruması. Bu modül SAF KATMANDIR:

  - AĞ ÇAĞRISI YOKTUR (endpoint bağlama Aşama B'nin konusudur).
  - canary_live / canary_runner / canary_risk ile ENTEGRE DEĞİLDİR
    (entry gate bağlantısı Aşama C'nin konusudur; ayrı operatör izni ister).
  - Yan etkisizdir: parser'lar yalnızca girdi dict → tipli model üretir.

ALAN SÖZLEŞMESİ (doc durumu — dürüst kayıt):
  DOĞRULANMIŞ (developers.binance.com, 2026-11-26):
    /fapi/v1/accountConfig, /fapi/v2/account, /fapi/v3/account,
    /fapi/v3/balance alan adları ve üst-düzey tanımları.
  VARSAYIM (doküman prozası JS-render; alan adları gerçek yanıtlarda kararlı):
    /fapi/v2/positionRisk proza tanımları, /fapi/v1/openOrders ve
    /fapi/v1/openAlgoOrders şemaları. Bu alanlar FIELD_CONTRACTS'te
    doc_status="ASSUMED" etiketi taşır; Aşama B'de gerçek yanıtlarla
    doğrulanmadan borsa garantisi gibi sunulmaz.

DOUBLE-COUNT GÜVENLİK SÖZLEŞMESİ (V2 raporu, Bölüm 3.2):
  1. Hesap riski hesaplamalarında YALNIZCA borsanın hesap-seviyesi
     toplamları kullanılır (AccountTotals). Pozisyon-seviyesi uPnL/IM/MM
     değerleri bu toplamlara ASLA eklenmez.
  2. Pozisyon verisi yalnız teşhis/uzlaştırmadır:
     reconcile_positions_vs_totals() farkları DİAGNOSTIK olarak döner,
     toplamları DEĞİŞTİRMEZ.
  3. cross kapsam (totalCrossWalletBalance, totalCrossUnPnl) ile
     all kapsam (totalWalletBalance, totalUnrealizedProfit, ...) birbiriyle
     toplanmaz; FIELD_CONTRACTS kapsam etiketleri ayrıktır.
  4. availableBalance borsanın bildirdiği değerdir; bileşenlerden YENİDEN
     HESAPLANMAZ (formülü resmî dokümanda yoktur — KANIT EKSİK).
  5. CROSS ve ISOLATED pozisyonlar PositionSnapshot içinde ayrıştırılır;
     cross uzlaştırmasına yalnız marginType="cross" satırları girer.

MULTI-ASSETS MODE: multiAssetsMargin=true → MULTI_ASSETS_MODE_UNSUPPORTED.
Veri ayrıştırılır ancak işlem için GEÇERSİZ sayılır (fail-closed; V2 raporu
Bölüm 3.3 — tam model ayrı tasarım görevi bekler).

SIFIR SEMANTİĞİ: "0" gerçek sıfırdır, eksik veri DEĞİLDİR. Yalnızca zaman
damgaları için 0 geçersizdir (ms epoch > 0 olmalıdır).

ZAMAN / ATOMİKLİK: REST yanıtları atomik değildir. Her bileşen kendi
SnapshotMetadata'sını taşır; AccountSnapshot.atomic=False sabitidir.
Kalibre edilmemiş eşik (10 sn vb.) BU MODÜLDE YOKTUR — yaş hesabı saf
fonksiyona (max_source_age_ms) bırakılır, eşik kararı Aşama C/operatördür.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Kapsam ve doğrulama sabitleri
# ---------------------------------------------------------------------------

SCOPE_ACCOUNT_CROSS = "ACCOUNT_CROSS"   # yalnız cross pozisyon/cüzdan kapsamı
SCOPE_ACCOUNT_ALL = "ACCOUNT_ALL"       # cross + isolated kapsayan hesap toplamı
SCOPE_ASSET = "ASSET"                   # varlık (USDT) satırı
SCOPE_POSITION = "POSITION"             # tek pozisyon satırı
SCOPE_ORDER = "ORDER"                   # emir envanteri satırı

UNIT_USDT = "USDT"                      # tek-varlık modu (Multi-Assets RED olur)
UNIT_CONTRACTS = "CONTRACTS"            # sözleşme adedi (positionAmt, qty)
UNIT_PRICE = "USDT_PER_CONTRACT"        # fiyat
UNIT_COUNT = "COUNT"
UNIT_MS = "MS_EPOCH"
UNIT_BOOL = "BOOL"
UNIT_FRACTION = "FRACTION"

DOC_VERIFIED = "DOC_VERIFIED"           # resmî dokümandan birebir (2026-11-26)
DOC_ASSUMED = "ASSUMED"                 # alan adı gerçek yanıtta kararlı, proza KANIT EKSİK

# Satır-seviyesi updateTime=0 sentinel durumu (B.1 İş 1; gerçek VPS kanıtı
# 2026-11-26: 930/958 positionRisk ve 9/11 balance satırı updateTime=0 —
# hiç işlem görmemiş sembol/varlık için borsanın normal davranışı).
# 0 GERÇEK Unix zamanı olarak YORUMLANMAZ; ham değer korunur, normalize
# edilmiş kaynak güncelleme zamanı None + durum NO_SOURCE_TIMESTAMP olur.
# REST yerel alım zamanı onun YERİNE yazılmaz (ayrı alanda tutulur).
NO_SOURCE_TIMESTAMP = "NO_SOURCE_TIMESTAMP"
SOURCE_TIMESTAMP = "SOURCE_TIMESTAMP"

# Doğrulama reddi sebepleri (enum değeri olarak; insan-okur detay message'da)
R_MISSING_FIELD = "MISSING_REQUIRED_FIELD"
R_INVALID_NUMERIC = "INVALID_NUMERIC_VALUE"
R_INVALID_NEGATIVE = "INVALID_NEGATIVE_VALUE"
R_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
R_INVALID_MARGIN_TYPE = "INVALID_MARGIN_TYPE"
R_MULTI_ASSETS_UNSUPPORTED = "MULTI_ASSETS_MODE_UNSUPPORTED"
R_COMPONENT_INVALID = "COMPONENT_PARSE_ERROR"
R_SCOPE_MISMATCH = "SCOPE_MISMATCH"

MARGIN_TYPE_CROSS = "cross"
MARGIN_TYPE_ISOLATED = "isolated"
_VALID_MARGIN_TYPES = (MARGIN_TYPE_CROSS, MARGIN_TYPE_ISOLATED)


class InvalidFieldError(ValueError):
    """Zorunlu alan eksik/bozuk — snapshot bu bileşenle ÜRETİLEMEZ (fail-closed).

    message biçimi: "<reason>: <alan yolu>: <detay>"
    Sessizce sıfıra çevirme YOKTUR; belirsiz veri geçerliymiş gibi gösterilmez.
    """

    def __init__(self, reason: str, path: str, detail: str):
        self.reason = reason
        self.path = path
        self.detail = detail
        super().__init__(f"{reason}: {path}: {detail}")


# ---------------------------------------------------------------------------
# Alan sözleşmesi kaydı (kaynak / birim / seviye / kapsam / zaman / doc durumu)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldContract:
    name: str          # alan adı (model yazımına göre)
    source: str        # üreten endpoint
    unit: str          # UNIT_*
    level: str         # "account" | "asset" | "position" | "order" | "config"
    scope: str         # SCOPE_*
    time_field: Optional[str]   # alanın tazelik kaynağı (varsa)
    doc_status: str    # DOC_VERIFIED | DOC_ASSUMED


def _c(name, source, unit, level, scope, time_field, doc):
    return FieldContract(name, source, unit, level, scope, time_field, doc)

EP_V2_ACCOUNT = "GET /fapi/v2/account"
EP_V3_ACCOUNT = "GET /fapi/v3/account"
EP_V3_BALANCE = "GET /fapi/v3/balance"
EP_CONFIG = "GET /fapi/v1/accountConfig"
EP_POS_RISK = "GET /fapi/v2/positionRisk"
EP_OPEN_ORDERS = "GET /fapi/v1/openOrders"
EP_OPEN_ALGOS = "GET /fapi/v1/openAlgoOrders"

# Kapsam-karışma koruması: ACCOUNT_ALL ile ACCOUNT_CROSS alan kümeleri
# bu kayıt üzerinden testlerde ayrışıklık denetlenir (double-count T2/T3).
FIELD_CONTRACTS: Dict[str, FieldContract] = {
    # — hesap toplamları (v2/v3 account üst-düzey; DOC_VERIFIED) —
    "AccountTotals.totalInitialMargin":        _c("totalInitialMargin", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalMaintMargin":          _c("totalMaintMargin", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalWalletBalance":        _c("totalWalletBalance", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalUnrealizedProfit":     _c("totalUnrealizedProfit", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalMarginBalance":        _c("totalMarginBalance", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalPositionInitialMargin": _c("totalPositionInitialMargin", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalOpenOrderInitialMargin": _c("totalOpenOrderInitialMargin", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.totalCrossWalletBalance":   _c("totalCrossWalletBalance", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_CROSS, None, DOC_VERIFIED),
    "AccountTotals.totalCrossUnPnl":           _c("totalCrossUnPnl", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_CROSS, None, DOC_VERIFIED),
    "AccountTotals.availableBalance":          _c("availableBalance", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    "AccountTotals.maxWithdrawAmount":         _c("maxWithdrawAmount", EP_V2_ACCOUNT, UNIT_USDT, "account", SCOPE_ACCOUNT_ALL, None, DOC_VERIFIED),
    # — varlık satırı (v3 balance; DOC_VERIFIED) —
    "BalanceEntry.balance":               _c("balance", EP_V3_BALANCE, UNIT_USDT, "asset", SCOPE_ASSET, "updateTime", DOC_VERIFIED),
    "BalanceEntry.crossWalletBalance":    _c("crossWalletBalance", EP_V3_BALANCE, UNIT_USDT, "asset", SCOPE_ACCOUNT_CROSS, "updateTime", DOC_VERIFIED),
    "BalanceEntry.crossUnPnl":            _c("crossUnPnl", EP_V3_BALANCE, UNIT_USDT, "asset", SCOPE_ACCOUNT_CROSS, "updateTime", DOC_VERIFIED),
    "BalanceEntry.availableBalance":      _c("availableBalance", EP_V3_BALANCE, UNIT_USDT, "asset", SCOPE_ASSET, "updateTime", DOC_VERIFIED),
    "BalanceEntry.maxWithdrawAmount":     _c("maxWithdrawAmount", EP_V3_BALANCE, UNIT_USDT, "asset", SCOPE_ASSET, "updateTime", DOC_VERIFIED),
    "BalanceEntry.marginAvailable":       _c("marginAvailable", EP_V3_BALANCE, UNIT_BOOL, "asset", SCOPE_ASSET, "updateTime", DOC_VERIFIED),
    # — pozisyon satırı (positionRisk; proza ASSUMED) —
    "PositionEntry.positionAmt":          _c("positionAmt", EP_POS_RISK, UNIT_CONTRACTS, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.entryPrice":           _c("entryPrice", EP_POS_RISK, UNIT_PRICE, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.markPrice":            _c("markPrice", EP_POS_RISK, UNIT_PRICE, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.unRealizedProfit":     _c("unRealizedProfit", EP_POS_RISK, UNIT_USDT, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.liquidationPrice":     _c("liquidationPrice", EP_POS_RISK, UNIT_PRICE, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.isolatedMargin":       _c("isolatedMargin", EP_POS_RISK, UNIT_USDT, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.isolatedWallet":       _c("isolatedWallet", EP_POS_RISK, UNIT_USDT, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.initialMargin":        _c("initialMargin", EP_POS_RISK, UNIT_USDT, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.maintMargin":          _c("maintMargin", EP_POS_RISK, UNIT_USDT, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.leverage":             _c("leverage", EP_POS_RISK, UNIT_COUNT, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    "PositionEntry.marginType":           _c("marginType", EP_POS_RISK, UNIT_BOOL, "position", SCOPE_POSITION, "updateTime", DOC_ASSUMED),
    # — config (DOC_VERIFIED) —
    "AccountConfigSnapshot.feeTier":          _c("feeTier", EP_CONFIG, UNIT_COUNT, "config", SCOPE_ASSET, None, DOC_VERIFIED),
    "AccountConfigSnapshot.multiAssetsMargin": _c("multiAssetsMargin", EP_CONFIG, UNIT_BOOL, "config", SCOPE_ASSET, None, DOC_VERIFIED),
    "AccountConfigSnapshot.tradeGroupId":     _c("tradeGroupId", EP_CONFIG, UNIT_COUNT, "config", SCOPE_ASSET, None, DOC_VERIFIED),
    # — emir envanteri (şema ASSUMED) —
    "OrderEntry.orderId":     _c("orderId", EP_OPEN_ORDERS, UNIT_COUNT, "order", SCOPE_ORDER, "updateTime", DOC_ASSUMED),
    "OrderEntry.origQty":     _c("origQty", EP_OPEN_ORDERS, UNIT_CONTRACTS, "order", SCOPE_ORDER, "updateTime", DOC_ASSUMED),
    "AlgoOrderEntry.algoId":  _c("algoId", EP_OPEN_ALGOS, UNIT_COUNT, "order", SCOPE_ORDER, "updateTime", DOC_ASSUMED),
    "AlgoOrderEntry.triggerPrice": _c("triggerPrice", EP_OPEN_ALGOS, UNIT_PRICE, "order", SCOPE_ORDER, "updateTime", DOC_ASSUMED),
}


def field_contract(qualified_name: str) -> FieldContract:
    """Alan sözleşmesi sorgusu. Bilinmeyen isim → KeyError (sessiz varsayım yok)."""
    return FIELD_CONTRACTS[qualified_name]


# ---------------------------------------------------------------------------
# Zaman üstverisi — REST atomik DEĞİLDİR (V2 raporu Bölüm 4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotMetadata:
    """Tek bir REST/WS bileşeninin zaman ve kaynak üstverisi.

    request_started_at / response_received_at / local_receive_time:
        toplayıcı katmanın (Aşama B) ölçtüğü istemci-saati değerleri (ms).
        Saf parser bunları bilmez; çağıran sağlar (None → bilinmiyor).
    source_update_time_ms: borsanın payload içinde bildirdiği güncelleme
        zamanı (balance/account `updateTime`). Yoksa None.
        NOT: /fapi/v2/account üst-düzey updateTime dokümanda
        "Reserved field, ignore" → bu modülde OKUNMAZ.
    """
    source_name: str
    endpoint: str
    request_started_at_ms: Optional[int] = None
    response_received_at_ms: Optional[int] = None
    local_receive_time_ms: Optional[int] = None
    source_update_time_ms: Optional[int] = None
    note: str = ""

    def validate(self) -> None:
        if not self.source_name or not self.endpoint:
            raise InvalidFieldError(R_MISSING_FIELD, "SnapshotMetadata.source_name/endpoint",
                                    "kaynak adı ve endpoint zorunludur")
        for fname in ("request_started_at_ms", "response_received_at_ms",
                      "local_receive_time_ms", "source_update_time_ms"):
            v = getattr(self, fname)
            if v is not None and (not isinstance(v, int) or v <= 0):
                raise InvalidFieldError(R_INVALID_TIMESTAMP,
                                        f"SnapshotMetadata.{fname}", f"değer={v!r} (ms epoch > 0 olmalı)")


# ---------------------------------------------------------------------------
# Doğrulama sonucu
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotValidationResult:
    """Snapshot doğrulaması. reasons boş + valid=True → işlem için kullanılabilir.

    valid=False → fail-closed (RED). warnings → teşhis (kapı DEĞİL).
    """
    valid: bool
    reasons: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def valid_for_trading(self) -> bool:
        return self.valid and not self.reasons


VALID = SnapshotValidationResult(valid=True)


# ---------------------------------------------------------------------------
# Sayısal ayrıştırma yardımcıları (saf; sessiz sıfır YOK)
# ---------------------------------------------------------------------------

def _num(value, path: str, *, allow_negative: bool = False) -> float:
    """Zorunlu sayısal alan. None/""/boşluk/NaN/±inf → InvalidFieldError.

    SIFIR GEÇERLİDİR (gerçek sıfır ≠ eksik veri). İşaret kuralı alanın
    semantiğine göre allow_negative ile verilir (positionAmt/uPnL serbest;
    bakiye/fiyat ≥ 0).
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise InvalidFieldError(R_MISSING_FIELD, path, "zorunlu alan eksik/boş")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise InvalidFieldError(R_INVALID_NUMERIC, path,
                                f"sayıya çevrilemiyor: {value!r}") from None
    if math.isnan(f) or math.isinf(f):
        raise InvalidFieldError(R_INVALID_NUMERIC, path,
                                f"NaN/inf kabul edilmez: {value!r}")
    if f < 0 and not allow_negative:
        raise InvalidFieldError(R_INVALID_NEGATIVE, path, f"negatif kabul edilmez: {value!r}")
    return f


def _opt_num(value, path: str, *, allow_negative: bool = False) -> Optional[float]:
    """Opsiyonel sayısal alan: None/"" → None (eksik = bilinmiyor, 0'a çevrilmez)."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return _num(value, path, allow_negative=allow_negative)


def _int(value, path: str, *, minimum: Optional[int] = None,
         allow_negative: bool = False) -> int:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise InvalidFieldError(R_MISSING_FIELD, path, "zorunlu alan eksik/boş")
    try:
        i = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(inf) — NaN ValueError, ±inf OverflowError;
        # ikisi de fail-closed (sessiz sıfır YOK)
        raise InvalidFieldError(R_INVALID_NUMERIC, path,
                                f"tam sayıya çevrilemiyor: {value!r}") from None
    if minimum is not None and i < minimum:
        raise InvalidFieldError(R_INVALID_NUMERIC, path,
                                f"{i} < minimum {minimum}")
    if i < 0 and not allow_negative:
        raise InvalidFieldError(R_INVALID_NEGATIVE, path, f"negatif kabul edilmez: {value!r}")
    return i


def _ts(value, path: str) -> int:
    """Zaman damgası: ms epoch, ZORUNLU ve > 0. (Sıfır = geçersiz zaman.)

    Sayısal/harf/≤0 sorunları zaman damgası semantiğiyle etiketlenir
    (R_INVALID_TIMESTAMP); yalnız 'alan hiç yok' durumu R_MISSING_FIELD
    kalır (daha kesin teşhis).
    """
    try:
        return _int(value, path, minimum=1)
    except InvalidFieldError as e:
        if e.reason == R_MISSING_FIELD:
            raise
        raise InvalidFieldError(R_INVALID_TIMESTAMP, path,
                                f"ms epoch > 0 olmalı; ham değer={value!r}") from None


def _ts_or_zero_sentinel(value, path: str) -> int:
    """Zaman damgası; ALAN-SÖZLEŞMESİNE ÖZEL sıfır sentinel izni (İş 1).

    Yalnız positionRisk satırı ve balance v3 satırı updateTime'larında
    kullanılır: gerçek yanıtlarda updateTime=0 ('hiç güncellenmemiş')
    gözlemlenmiştir. 0 → geçerli (NO_SOURCE_TIMESTAMP durumu); ham değer
    korunur. Eksik/bozuk/negatif/NaN/±inf → önceki gibi fail-closed.
    Diğer zaman alanları _ts ile SIKI kalır (genelleme YOK).
    """
    try:
        return _int(value, path, minimum=0)
    except InvalidFieldError as e:
        if e.reason == R_MISSING_FIELD:
            raise
        raise InvalidFieldError(R_INVALID_TIMESTAMP, path,
                                f"ms epoch >= 0 olmalı; ham değer={value!r}") from None


def _req_str(value, path: str) -> str:
    if value is None or not isinstance(value, str) or value.strip() == "":
        raise InvalidFieldError(R_MISSING_FIELD, path, f"zorunlu string eksik: {value!r}")
    return value


def _opt_bool(value, path: str) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise InvalidFieldError(R_INVALID_NUMERIC, path, f"bool bekleniyordu: {value!r}")


# ---------------------------------------------------------------------------
# Modeller
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountConfigSnapshot:
    """GET /fapi/v1/accountConfig (DOC_VERIFIED; weight 5).

    multiAssetsMargin=true → MULTI_ASSETS_MODE_UNSUPPORTED (fail-closed):
    veri geçerli, İŞLEM izni yoktur.
    """
    feeTier: int
    multiAssetsMargin: bool
    tradeGroupId: int
    metadata: SnapshotMetadata

    def validate(self) -> SnapshotValidationResult:
        if self.multiAssetsMargin:
            return SnapshotValidationResult(
                valid=False, reasons=(R_MULTI_ASSETS_UNSUPPORTED,))
        return VALID


@dataclass(frozen=True)
class BalanceEntry:
    """GET /fapi/v3/balance tek varlık satırı (DOC_VERIFIED; weight 5).

    Birim: tek-varlık modunda USDT. crossUnPnl YALNIZ cross pozisyonların
    uPnL'idir — pozisyon upnl'leriyle toplanMAZ (double-count).
    """
    asset: str
    balance: float                    # walletBalance
    crossWalletBalance: float
    crossUnPnl: float
    availableBalance: float
    maxWithdrawAmount: float
    marginAvailable: Optional[bool]
    updateTime: int
    metadata: SnapshotMetadata

    @property
    def update_time_status(self) -> str:
        """NO_SOURCE_TIMESTAMP: updateTime=0 ('hiç güncellenmemiş' sentinel;
        gerçek VPS kanıtı 2026-11-26). 0 gerçek zaman olarak YORUMLANMAZ."""
        return NO_SOURCE_TIMESTAMP if self.updateTime == 0 else SOURCE_TIMESTAMP

    @property
    def source_update_time_ms(self) -> Optional[int]:
        """Normalize kaynak güncelleme zamanı; sentinel durumda None.
        REST yerel alım zamanı (metadata) bunun YERİNE kullanılmaz."""
        return None if self.updateTime == 0 else self.updateTime


@dataclass(frozen=True)
class BalanceSnapshot:
    entries: Tuple[BalanceEntry, ...]
    metadata: SnapshotMetadata

    def by_asset(self, asset: str) -> Optional[BalanceEntry]:
        for e in self.entries:
            if e.asset == asset:
                return e
        return None

    def usdt(self) -> BalanceEntry:
        """USDT satırı (tek-varlık modu zorunluluğu; yoksa fail-closed)."""
        e = self.by_asset("USDT")
        if e is None:
            raise InvalidFieldError(R_MISSING_FIELD, "BalanceSnapshot.usdt",
                                    "USDT varlık satırı yok")
        return e


@dataclass(frozen=True)
class PositionEntry:
    """Tek pozisyon satırı. Kaynak: /fapi/v2/positionRisk (ASSUMED) veya
    /fapi/v2|v3/account.positions[] (DOC_VERIFIED; azaltılmış alan kümesi).

    scope=POSITION. marginType: "cross" | "isolated" (cüzdan kapsamı).
    liquidationPrice=0.0 → Binance 'likidasyon fiyatı yok' göndermiştir
    (flat pozisyon satırı); 0 GEÇERLİ değerdir, eksik DEĞİLDİR.
    """
    symbol: str
    positionSide: str
    positionAmt: float                # işaretli: SHORT < 0
    marginType: str                   # cross | isolated
    leverage: int
    entryPrice: float
    updateTime: int
    metadata: SnapshotMetadata
    markPrice: Optional[float] = None
    unRealizedProfit: Optional[float] = None
    liquidationPrice: Optional[float] = None
    isolatedMargin: Optional[float] = None
    isolatedWallet: Optional[float] = None
    initialMargin: Optional[float] = None
    maintMargin: Optional[float] = None
    positionInitialMargin: Optional[float] = None
    openOrderInitialMargin: Optional[float] = None
    notional: Optional[float] = None
    breakEvenPrice: Optional[float] = None
    source: str = EP_POS_RISK         # alan kümesi hangi endpoint'ten geldi

    @property
    def is_cross(self) -> bool:
        return self.marginType == MARGIN_TYPE_CROSS

    @property
    def update_time_status(self) -> str:
        """NO_SOURCE_TIMESTAMP: updateTime=0 ('hiç güncellenmemiş' sentinel;
        gerçek VPS kanıtı 2026-11-26 — 930/958 satır). Gerçek zaman DEĞİL."""
        return NO_SOURCE_TIMESTAMP if self.updateTime == 0 else SOURCE_TIMESTAMP

    @property
    def source_update_time_ms(self) -> Optional[int]:
        """Normalize kaynak güncelleme zamanı; sentinel durumda None.
        REST yerel alım zamanı bunun YERİNE yazılmaz."""
        return None if self.updateTime == 0 else self.updateTime

    @property
    def is_isolated(self) -> bool:
        return self.marginType == MARGIN_TYPE_ISOLATED

    @property
    def is_flat(self) -> bool:
        return self.positionAmt == 0.0

    @property
    def has_liquidation_price(self) -> bool:
        return self.liquidationPrice is not None and self.liquidationPrice > 0.0


@dataclass(frozen=True)
class PositionSnapshot:
    """Filtresiz (TÜM semboller) pozisyon envanteri. scope=POSITION."""
    entries: Tuple[PositionEntry, ...]
    metadata: SnapshotMetadata

    def cross_positions(self) -> Tuple[PositionEntry, ...]:
        return tuple(p for p in self.entries if p.is_cross)

    def isolated_positions(self) -> Tuple[PositionEntry, ...]:
        return tuple(p for p in self.entries if p.is_isolated)

    def non_flat(self) -> Tuple[PositionEntry, ...]:
        return tuple(p for p in self.entries if not p.is_flat)

    def by_symbol(self, symbol: str) -> Optional[PositionEntry]:
        for p in self.entries:
            if p.symbol == symbol:
                return p
        return None


@dataclass(frozen=True)
class OrderEntry:
    """GET /fapi/v1/openOrders tek satır (şema ASSUMED). scope=ORDER."""
    orderId: int
    symbol: str
    side: str
    type: str
    status: str
    updateTime: int
    metadata: SnapshotMetadata
    clientOrderId: Optional[str] = None
    price: Optional[float] = None
    origQty: Optional[float] = None
    executedQty: Optional[float] = None
    cumQuote: Optional[float] = None
    stopPrice: Optional[float] = None
    reduceOnly: Optional[bool] = None
    closePosition: Optional[bool] = None
    positionSide: Optional[str] = None


@dataclass(frozen=True)
class OpenOrderSnapshot:
    """Normal (koşulsuz) açık emir envanteri. scope=ORDER.

    NOT: koşullu/algo emirler burada YOKTUR — ayrı sorgulanırlar
    (OpenAlgoOrderSnapshot); envanter bütünlüğü ikisini de gerektirir.
    """
    entries: Tuple[OrderEntry, ...]
    metadata: SnapshotMetadata


@dataclass(frozen=True)
class AlgoOrderEntry:
    """GET /fapi/v1/openAlgoOrders tek satır (şema ASSUMED; KANIT EKSİK).

    Alan adları gerçek yanıtta gözlemlenen biçimdedir; resmî proza
    doğrulanamamıştır. Zorunlu: algoId, symbol. Diğerleri opsiyonel ve
    doğrulanır-ise-varsa.
    """
    algoId: int
    symbol: str
    updateTime: Optional[int]
    metadata: SnapshotMetadata
    algoType: Optional[str] = None
    side: Optional[str] = None
    status: Optional[str] = None
    clientAlgoId: Optional[str] = None
    triggerPrice: Optional[float] = None
    totalAmt: Optional[float] = None
    workingType: Optional[str] = None
    positionSide: Optional[str] = None


@dataclass(frozen=True)
class OpenAlgoOrderSnapshot:
    entries: Tuple[AlgoOrderEntry, ...]
    metadata: SnapshotMetadata


@dataclass(frozen=True)
class AccountTotals:
    """Hesap-seviyesi toplamlar (v2/v3 account üst-düzey; DOC_VERIFIED).

    GÜVENLİK SÖZLEŞMESİ:
      - ACCOUNT_CROSS alanları (totalCrossWalletBalance, totalCrossUnPnl):
        yalnız cross kapsam. CROSS risk hesabında KULLANILABİLEN tek küme.
      - ACCOUNT_ALL alanları (totalWalletBalance, totalUnrealizedProfit,
        totalMaintMargin, ...): cross+isolated karışık — cross riskte
        KULLANILMAZ.
      - availableBalance: borsanın bildirdiği değer; bileşenlerden yeniden
        hesaplayan HİÇBİR metot bu sınıfta YOKTUR.
      - Pozisyon verisi kabul eden HİÇBİR metot YOKTUR (double-count T1/T4).
    """
    totalWalletBalance: float
    totalUnrealizedProfit: float
    totalMarginBalance: float
    totalInitialMargin: float
    totalMaintMargin: float
    totalPositionInitialMargin: float
    totalOpenOrderInitialMargin: float
    totalCrossWalletBalance: float
    totalCrossUnPnl: float
    availableBalance: float
    maxWithdrawAmount: float
    metadata: SnapshotMetadata
    multiAssetsMargin: Optional[bool] = None   # v2 account'ta mevcut; v3'te yok
    feeTier: Optional[int] = None
    canTrade: Optional[bool] = None
    source: str = EP_V2_ACCOUNT


@dataclass(frozen=True)
class AccountSnapshot:
    """Bileşik hesap görüntüsü. ATOMİK DEĞİLDİR (atomic=False sabiti).

    Her bileşenin kendi SnapshotMetadata'sı vardır; farklı REST çağrıları
    farklı anlara aittir ve bu model bunu GİZLEMEZ.
    """
    config: Optional[AccountConfigSnapshot]
    totals: Optional[AccountTotals]
    balance: Optional[BalanceSnapshot]
    positions: Optional[PositionSnapshot]
    open_orders: Optional[OpenOrderSnapshot]
    open_algo_orders: Optional[OpenAlgoOrderSnapshot]

    atomic: bool = False  # sabit; değiştirilemez (frozen dataclass)

    def validate(self) -> SnapshotValidationResult:
        reasons: List[str] = []
        warnings: List[str] = []
        if self.config is None:
            reasons.append(R_MISSING_FIELD + ":config")
        else:
            r = self.config.validate()
            reasons.extend(r.reasons)
            warnings.extend(r.warnings)
        if self.totals is None:
            reasons.append(R_MISSING_FIELD + ":totals")
        elif self.totals.multiAssetsMargin is True:
            # v2 account bayrağı config ile çelişkili duruma karşı ikinci kilit
            reasons.append(R_MULTI_ASSETS_UNSUPPORTED + ":totals")
        if self.balance is None:
            reasons.append(R_MISSING_FIELD + ":balance")
        if self.positions is None:
            reasons.append(R_MISSING_FIELD + ":positions")
        if self.open_orders is None:
            reasons.append(R_MISSING_FIELD + ":open_orders")
        if self.open_algo_orders is None:
            reasons.append(R_MISSING_FIELD + ":open_algo_orders")
        # v2/v3 toplam kaynağı karışması: kapsam tutarlılığı denetimi
        if (self.totals is not None and self.balance is not None
                and self.totals.source == EP_V2_ACCOUNT
                and self.balance.metadata.endpoint == EP_V3_BALANCE):
            warnings.append("CROSS_SOURCE_TOTALS_V2_BALANCE_V3: kaynak sürümleri "
                            "farklı — uzlaştırma (reconcile) önerilir")
        return SnapshotValidationResult(valid=not reasons,
                                        reasons=tuple(reasons),
                                        warnings=tuple(warnings))

    # -- double-count-güvenli türevler (yalnız hesap-seviyesi alanlardan) --

    @property
    def cross_margin_balance(self) -> Optional[float]:
        """W_cross = totalCrossWalletBalance + totalCrossUnPnl.

        Yalnız ACCOUNT_CROSS kapsamındaki İKİ hesap-seviyesi alandan türetilir.
        Pozisyon uPnL'si ASLA eklenmez (bkz. modül dokümantasyonu, sözleşme 1).
        totals yoksa None (hesaplanamıyorsa tahmin ÜRETİLMEZ).
        """
        if self.totals is None:
            return None
        return self.totals.totalCrossWalletBalance + self.totals.totalCrossUnPnl


# ---------------------------------------------------------------------------
# Parser'lar (saf: dict → model; ağ YOK)
# ---------------------------------------------------------------------------

def parse_account_config(response: dict, metadata: SnapshotMetadata
                         ) -> AccountConfigSnapshot:
    """GET /fapi/v1/accountConfig yanıtı (DOC_VERIFIED).

    response: {"feeTier": int, "multiAssetsMargin": bool, "tradeGroupId": int}
    """
    metadata.validate()
    return AccountConfigSnapshot(
        feeTier=_int(response.get("feeTier"), "accountConfig.feeTier"),
        multiAssetsMargin=_opt_bool(response.get("multiAssetsMargin"),
                                    "accountConfig.multiAssetsMargin"),
        tradeGroupId=_int(response.get("tradeGroupId"), "accountConfig.tradeGroupId",
                          allow_negative=True),
        metadata=metadata,
    )


def parse_balance_v3(response: list, metadata: SnapshotMetadata
                     ) -> BalanceSnapshot:
    """GET /fapi/v3/balance yanıtı (DOC_VERIFIED).

    response: [ {"asset", "balance", "crossWalletBalance", "crossUnPnl",
                 "availableBalance", "maxWithdrawAmount", "marginAvailable",
                 "updateTime"}, ... ]
    """
    metadata.validate()
    if not isinstance(response, list) or not response:
        raise InvalidFieldError(R_MISSING_FIELD, "balance[]",
                                "yanıt boş veya liste değil")
    entries = []
    for i, row in enumerate(response):
        p = f"balance[{i}]"
        entries.append(BalanceEntry(
            asset=_req_str(row.get("asset"), f"{p}.asset"),
            balance=_num(row.get("balance"), f"{p}.balance"),
            crossWalletBalance=_num(row.get("crossWalletBalance"),
                                    f"{p}.crossWalletBalance"),
            crossUnPnl=_num(row.get("crossUnPnl"), f"{p}.crossUnPnl",
                            allow_negative=True),
            availableBalance=_num(row.get("availableBalance"),
                                  f"{p}.availableBalance"),
            maxWithdrawAmount=_num(row.get("maxWithdrawAmount"),
                                   f"{p}.maxWithdrawAmount"),
            marginAvailable=_opt_bool(row.get("marginAvailable"),
                                      f"{p}.marginAvailable"),
            updateTime=_ts_or_zero_sentinel(row.get("updateTime"),
                                            f"{p}.updateTime"),
            metadata=metadata,
        ))
    return BalanceSnapshot(entries=tuple(entries), metadata=metadata)


def _parse_account_totals(response: dict, metadata: SnapshotMetadata,
                          source: str) -> AccountTotals:
    """v2/v3 account üst-düzey toplamlar (ortak zorunlu küme; DOC_VERIFIED).

    NOT: üst-düzey `updateTime` dokümanda "Reserved field, ignore" — OKUNMAZ.
    """
    metadata.validate()
    return AccountTotals(
        totalWalletBalance=_num(response.get("totalWalletBalance"),
                                "account.totalWalletBalance"),
        totalUnrealizedProfit=_num(response.get("totalUnrealizedProfit"),
                                   "account.totalUnrealizedProfit",
                                   allow_negative=True),
        totalMarginBalance=_num(response.get("totalMarginBalance"),
                                "account.totalMarginBalance",
                                allow_negative=True),
        totalInitialMargin=_num(response.get("totalInitialMargin"),
                                "account.totalInitialMargin"),
        totalMaintMargin=_num(response.get("totalMaintMargin"),
                              "account.totalMaintMargin"),
        totalPositionInitialMargin=_num(response.get("totalPositionInitialMargin"),
                                        "account.totalPositionInitialMargin"),
        totalOpenOrderInitialMargin=_num(response.get("totalOpenOrderInitialMargin"),
                                         "account.totalOpenOrderInitialMargin"),
        totalCrossWalletBalance=_num(response.get("totalCrossWalletBalance"),
                                     "account.totalCrossWalletBalance"),
        totalCrossUnPnl=_num(response.get("totalCrossUnPnl"),
                             "account.totalCrossUnPnl", allow_negative=True),
        availableBalance=_num(response.get("availableBalance"),
                              "account.availableBalance"),
        maxWithdrawAmount=_num(response.get("maxWithdrawAmount"),
                               "account.maxWithdrawAmount"),
        metadata=metadata,
        multiAssetsMargin=_opt_bool(response.get("multiAssetsMargin"),
                                    "account.multiAssetsMargin"),
        feeTier=(_int(response["feeTier"], "account.feeTier", allow_negative=False)
                 if response.get("feeTier") is not None else None),
        canTrade=_opt_bool(response.get("canTrade"), "account.canTrade"),
        source=source,
    )


def parse_account_v2(response: dict, metadata: SnapshotMetadata) -> AccountTotals:
    """GET /fapi/v2/account üst-düzey toplamlar (DOC_VERIFIED; weight 5).

    Pozisyon dizisi bu parser'da ayrıştırılmaz — pozisyon envanteri için
    parse_position_risk / parse_account_positions kullanılır (tek sorumluluk).
    """
    return _parse_account_totals(response, metadata, EP_V2_ACCOUNT)


def parse_account_v3(response: dict, metadata: SnapshotMetadata) -> AccountTotals:
    """GET /fapi/v3/account üst-düzey toplamlar (DOC_VERIFIED; weight 5).

    v3'te multiAssetsMargin/feeTier/canTrade ÜST DÜZEYDE YOKTUR →
    multiAssetsMargin=None (bilinmiyor; config endpoint'inden alınır).
    """
    return _parse_account_totals(response, metadata, EP_V3_ACCOUNT)


def parse_account_positions(response: dict, metadata: SnapshotMetadata
                            ) -> PositionSnapshot:
    """/fapi/v2|v3/account positions[] (DOC_VERIFIED; azaltılmış alan kümesi).

    Bu kaynakta markPrice/liquidationPrice/notional YOKTUR (None kalır).
    marginType, v2'deki `isolated` bool'undan türetilir.
    """
    metadata.validate()
    rows = response.get("positions")
    if not isinstance(rows, list):
        raise InvalidFieldError(R_MISSING_FIELD, "account.positions",
                                "positions dizisi eksik")
    entries = []
    for i, row in enumerate(rows):
        p = f"account.positions[{i}]"
        isolated_raw = row.get("isolated", row.get("isolatedMargin") is not None)
        margin_type = (MARGIN_TYPE_ISOLATED if isolated_raw
                       else MARGIN_TYPE_CROSS)
        if margin_type not in _VALID_MARGIN_TYPES:
            raise InvalidFieldError(R_INVALID_MARGIN_TYPE, f"{p}.marginType",
                                    f"bilinmeyen marjin tipi: {margin_type!r}")
        entries.append(PositionEntry(
            symbol=_req_str(row.get("symbol"), f"{p}.symbol"),
            positionSide=_req_str(row.get("positionSide", "BOTH"),
                                  f"{p}.positionSide"),
            positionAmt=_num(row.get("positionAmt"), f"{p}.positionAmt",
                             allow_negative=True),
            marginType=margin_type,
            leverage=_int(row.get("leverage"), f"{p}.leverage", minimum=1),
            entryPrice=_num(row.get("entryPrice"), f"{p}.entryPrice"),
            updateTime=_ts(row.get("updateTime"), f"{p}.updateTime"),
            metadata=metadata,
            unRealizedProfit=_opt_num(row.get("unrealizedProfit"),
                                      f"{p}.unrealizedProfit", allow_negative=True),
            initialMargin=_opt_num(row.get("initialMargin"), f"{p}.initialMargin"),
            maintMargin=_opt_num(row.get("maintMargin"), f"{p}.maintMargin"),
            positionInitialMargin=_opt_num(row.get("positionInitialMargin"),
                                           f"{p}.positionInitialMargin"),
            openOrderInitialMargin=_opt_num(row.get("openOrderInitialMargin"),
                                            f"{p}.openOrderInitialMargin"),
            isolatedWallet=_opt_num(row.get("isolatedWallet"),
                                    f"{p}.isolatedWallet"),
            notional=_opt_num(row.get("notional"), f"{p}.notional",
                              allow_negative=True),
            source="account.positions",
        ))
    return PositionSnapshot(entries=tuple(entries), metadata=metadata)


def parse_position_risk(response: list, metadata: SnapshotMetadata
                        ) -> PositionSnapshot:
    """GET /fapi/v2/positionRisk yanıtı (alan semantiği ASSUMED — modül
    dokümantasyonuna bkz; Aşama B'de gerçek yanıtlarla doğrulanacak).

    response: FİLTRESİZ (sembolsüz) çağrının TAM listesi beklenir —
    ownership envanteri tüm sembolleri gerektirir (V2 raporu Bölüm 5).
    """
    metadata.validate()
    if not isinstance(response, list):
        raise InvalidFieldError(R_MISSING_FIELD, "positionRisk[]",
                                "yanıt liste değil")
    entries = []
    for i, row in enumerate(response):
        p = f"positionRisk[{i}]"
        margin_type = _req_str(row.get("marginType"), f"{p}.marginType").lower()
        if margin_type not in _VALID_MARGIN_TYPES:
            raise InvalidFieldError(R_INVALID_MARGIN_TYPE, f"{p}.marginType",
                                    f"bilinmeyen marjin tipi: {margin_type!r}")
        entries.append(PositionEntry(
            symbol=_req_str(row.get("symbol"), f"{p}.symbol"),
            positionSide=_req_str(row.get("positionSide", "BOTH"),
                                  f"{p}.positionSide"),
            positionAmt=_num(row.get("positionAmt"), f"{p}.positionAmt",
                             allow_negative=True),
            marginType=margin_type,
            leverage=_int(row.get("leverage"), f"{p}.leverage", minimum=1),
            entryPrice=_num(row.get("entryPrice"), f"{p}.entryPrice"),
            updateTime=_ts_or_zero_sentinel(row.get("updateTime"),
                                            f"{p}.updateTime"),
            metadata=metadata,
            markPrice=_opt_num(row.get("markPrice"), f"{p}.markPrice"),
            unRealizedProfit=_opt_num(row.get("unRealizedProfit"),
                                      f"{p}.unRealizedProfit", allow_negative=True),
            liquidationPrice=_opt_num(row.get("liquidationPrice"),
                                      f"{p}.liquidationPrice"),
            isolatedMargin=_opt_num(row.get("isolatedMargin"),
                                    f"{p}.isolatedMargin"),
            isolatedWallet=_opt_num(row.get("isolatedWallet"),
                                    f"{p}.isolatedWallet"),
            initialMargin=_opt_num(row.get("initialMargin"), f"{p}.initialMargin"),
            maintMargin=_opt_num(row.get("maintMargin"), f"{p}.maintMargin"),
            positionInitialMargin=_opt_num(row.get("positionInitialMargin"),
                                           f"{p}.positionInitialMargin"),
            openOrderInitialMargin=_opt_num(row.get("openOrderInitialMargin"),
                                            f"{p}.openOrderInitialMargin"),
            notional=_opt_num(row.get("notional"), f"{p}.notional",
                              allow_negative=True),
            breakEvenPrice=_opt_num(row.get("breakEvenPrice"),
                                    f"{p}.breakEvenPrice"),
            source=EP_POS_RISK,
        ))
    return PositionSnapshot(entries=tuple(entries), metadata=metadata)


def parse_open_orders(response: list, metadata: SnapshotMetadata
                      ) -> OpenOrderSnapshot:
    """GET /fapi/v1/openOrders yanıtı (şema ASSUMED). TÜM semboller."""
    metadata.validate()
    if not isinstance(response, list):
        raise InvalidFieldError(R_MISSING_FIELD, "openOrders[]",
                                "yanıt liste değil")
    entries = []
    for i, row in enumerate(response):
        p = f"openOrders[{i}]"
        entries.append(OrderEntry(
            orderId=_int(row.get("orderId"), f"{p}.orderId"),
            symbol=_req_str(row.get("symbol"), f"{p}.symbol"),
            side=_req_str(row.get("side"), f"{p}.side"),
            type=_req_str(row.get("type"), f"{p}.type"),
            status=_req_str(row.get("status"), f"{p}.status"),
            updateTime=_ts(row.get("updateTime"), f"{p}.updateTime"),
            metadata=metadata,
            clientOrderId=row.get("clientOrderId"),
            price=_opt_num(row.get("price"), f"{p}.price"),
            origQty=_opt_num(row.get("origQty"), f"{p}.origQty"),
            executedQty=_opt_num(row.get("executedQty"), f"{p}.executedQty"),
            cumQuote=_opt_num(row.get("cumQuote"), f"{p}.cumQuote"),
            stopPrice=_opt_num(row.get("stopPrice"), f"{p}.stopPrice"),
            reduceOnly=_opt_bool(row.get("reduceOnly"), f"{p}.reduceOnly"),
            closePosition=_opt_bool(row.get("closePosition"), f"{p}.closePosition"),
            positionSide=row.get("positionSide"),
        ))
    return OpenOrderSnapshot(entries=tuple(entries), metadata=metadata)


def parse_open_algo_orders(response: list, metadata: SnapshotMetadata
                           ) -> OpenAlgoOrderSnapshot:
    """GET /fapi/v1/openAlgoOrders yanıtı (şema ASSUMED / KANIT EKSİK).

    Zorunlu: algoId, symbol. Geri kalan alanlar 'varsa doğrulanır'.
    """
    metadata.validate()
    if not isinstance(response, list):
        raise InvalidFieldError(R_MISSING_FIELD, "openAlgoOrders[]",
                                "yanıt liste değil")
    entries = []
    for i, row in enumerate(response):
        p = f"openAlgoOrders[{i}]"
        ut = row.get("updateTime")
        entries.append(AlgoOrderEntry(
            algoId=_int(row.get("algoId"), f"{p}.algoId"),
            symbol=_req_str(row.get("symbol"), f"{p}.symbol"),
            updateTime=_ts(ut, f"{p}.updateTime") if ut is not None else None,
            metadata=metadata,
            algoType=row.get("algoType"),
            side=row.get("side"),
            status=row.get("algoStatus") or row.get("status"),
            clientAlgoId=row.get("clientAlgoId"),
            triggerPrice=_opt_num(row.get("triggerPrice") or row.get("stopPrice"),
                                  f"{p}.triggerPrice"),
            totalAmt=_opt_num(row.get("totalAmt") or row.get("origQty"),
                              f"{p}.totalAmt"),
            workingType=row.get("workingType"),
            positionSide=row.get("positionSide"),
        ))
    return OpenAlgoOrderSnapshot(entries=tuple(entries), metadata=metadata)


# ---------------------------------------------------------------------------
# Bileşim + uzlaştırma (diagnostik; toplamlar ASLA değişmez)
# ---------------------------------------------------------------------------

def build_account_snapshot(config: Optional[AccountConfigSnapshot],
                           totals: Optional[AccountTotals],
                           balance: Optional[BalanceSnapshot],
                           positions: Optional[PositionSnapshot],
                           open_orders: Optional[OpenOrderSnapshot],
                           open_algo_orders: Optional[OpenAlgoOrderSnapshot]
                           ) -> AccountSnapshot:
    """Bileşenlerden AccountSnapshot üretir. Bileşen yokluğu model düzeyinde
    kayda geçer (validate → RED); parse hataları zaten parser'da patlar."""
    return AccountSnapshot(config=config, totals=totals, balance=balance,
                           positions=positions, open_orders=open_orders,
                           open_algo_orders=open_algo_orders)


@dataclass(frozen=True)
class ReconciliationReport:
    """Pozisyon verisi ↔ hesap toplamları UZLAŞTIRMASI (yalnız teşhis).

    Double-count sözleşmesi 2: bu rapor AccountTotals'ı DEĞİŞTİRMEZ ve
    HİÇBİR kapı kararı ÜRETMEZ; sapmalar warning olarak raporlanır.
    Eşik YOKTUR (kalibre edilmemiş tolerans kıyası Aşama C'ye aittir).
    """
    cross_upnl_position_sum: Optional[float]    # Σ upnl, yalnız cross satırlar
    total_cross_unpnl: Optional[float]          # borsanın hesap-seviye alanı
    cross_upnl_abs_diff: Optional[float]
    cross_position_count: int
    isolated_position_count: int
    cross_mm_position_sum: Optional[float]      # Σ maintMargin (cross satırlar)
    isolated_upnl_sum: Optional[float]          # teşhis: isolated ayrı tutulur
    notes: Tuple[str, ...] = ()

    @property
    def has_cross_upnl_mismatch(self) -> Optional[bool]:
        """True/False; None → karşılaştırma verisi eksik (sessiz tahmin yok)."""
        if self.cross_upnl_position_sum is None or self.total_cross_unpnl is None:
            return None
        return self.cross_upnl_abs_diff is not None and self.cross_upnl_abs_diff != 0.0


def reconcile_positions_vs_totals(positions: PositionSnapshot,
                                  totals: AccountTotals) -> ReconciliationReport:
    """Cross pozisyon uPnL toplamını hesap-seviye totalCrossUnPnl ile
    karşılaştırır (SAPMA = UYARI, kapı değil — farklı an/zamanlama doğaldır).

    ISOLATED satırlar cross uzlaştırmasına ASLA girmez (sözleşme 5).
    """
    cross = positions.cross_positions()
    isolated = positions.isolated_positions()

    cross_upnl_sum = None
    has_any_upnl = any(p.unRealizedProfit is not None for p in positions.entries)
    if has_any_upnl:
        cross_upnl_sum = sum(p.unRealizedProfit for p in cross
                             if p.unRealizedProfit is not None)
    cross_mm_sum = None
    has_any_mm = any(p.maintMargin is not None for p in positions.entries)
    if has_any_mm:
        cross_mm_sum = sum(p.maintMargin for p in cross
                           if p.maintMargin is not None)
    iso_upnl_sum = None
    if has_any_upnl:
        iso_upnl_sum = sum(p.unRealizedProfit for p in isolated
                           if p.unRealizedProfit is not None)

    diff = (abs(cross_upnl_sum - totals.totalCrossUnPnl)
            if cross_upnl_sum is not None else None)
    notes = []
    if diff is not None and diff != 0.0:
        notes.append("CROSS_UPNL_MISMATCH: pozisyon toplamı ile hesap alanı "
                     "farklı — REST çağrıları farklı anlara ait olabilir "
                     "(atomik DEĞİL); eşik kararı Aşama C kalibrasyonuna aittir")
    return ReconciliationReport(
        cross_upnl_position_sum=cross_upnl_sum,
        total_cross_unpnl=totals.totalCrossUnPnl,
        cross_upnl_abs_diff=diff,
        cross_position_count=len(cross),
        isolated_position_count=len(isolated),
        cross_mm_position_sum=cross_mm_sum,
        isolated_upnl_sum=iso_upnl_sum,
        notes=tuple(notes),
    )


def max_source_age_ms(snapshot: AccountSnapshot, now_ms: int) -> Optional[int]:
    """Bileşen kaynak güncelleme zamanlarının en yaşlısının yaşı (ms).

    Saf: şimdi anı parametre olarak verilir (gizli saat yok).
    Hiçbir bileşende source_update_time yoksa None döner (bilinmiyor →
    tahmin ÜRETİLMEZ; Aşama B toplayıcı her REST yanıtında updateTime taşır).
    Eşik (ör. 10 sn) BU MODÜLDE TANIMLI DEĞİLDİR — kalibre edilmemiştir.
    """
    if not isinstance(now_ms, int) or now_ms <= 0:
        raise InvalidFieldError(R_INVALID_TIMESTAMP, "max_source_age_ms.now_ms",
                                f"geçersiz şimdi anı: {now_ms!r}")
    ages = []
    for meta in (m for m in (
        snapshot.config and snapshot.config.metadata,
        snapshot.totals and snapshot.totals.metadata,
        snapshot.balance and snapshot.balance.metadata,
        snapshot.positions and snapshot.positions.metadata,
        snapshot.open_orders and snapshot.open_orders.metadata,
        snapshot.open_algo_orders and snapshot.open_algo_orders.metadata,
    ) if m is not None):
        if meta.source_update_time_ms is not None:
            ages.append(now_ms - meta.source_update_time_ms)
    return max(ages) if ages else None
