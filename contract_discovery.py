"""contract_discovery.py — BLOK32 Görev 5: Binance Futures dinamik sözleşme keşfi.

SAF keşif modülü (ağ YOK — scan_engine.py deseni). Gerçek exchangeInfo JSON'u
parametre olarak verilir; sonuç tamamen veriden türetilir. Testler sentetik
fixture'larla çalışır ve gerçek sözleşme sayılarıyla İLGİLİ DEĞİLDİR.

Keşif sözleşmesi (Görev 5 spec):
  1. USDⓈ-M (fapi) ve COIN-M (dapi) exchangeInfo sembolleri taranır. Durum
     alanı farkı modül içinde normalize edilir: fapi 'status', dapi
     'contractStatus' — ikisinden biri TRADING ise sözleşme keşfedilir.
  2. Yalnız TRADING durumundaki sözleşmeler keşfedilir; TRADING olmayanlar
     (SETTLING/BREAK/PENDING_TRADING…) yalnızca özet sayımına girer.
  3. TRADIFI (tokenize hisse/emtia), INDEX ve PREMARKET underlying'ler
     UNSUPPORTED_CONTRACT_TYPE olarak dışlanır ve raporlanır.
  4. Sözleşmeler baseAsset'e (coin kimliği) göre normalize edilir: aynı
     coinin USDT/USDC/USD1/COIN-M varyantları AYRI COİN SAYILMAZ.
  5. Execution sınıflandırması (öncelik sırasıyla, ilk tutan kategoridir):
       UNSUPPORTED_CONTRACT_TYPE — contractType != PERPETUAL (TRADIFI,
                                   quarterly) VEYA underlying INDEX/PREMARKET
       UNSUPPORTED_QUOTE         — quoteAsset != USDT (USDC/USD1/U/BTC/USD…;
                                   COIN-M'in coin-marjı da gerekçede görünür)
       UNSUPPORTED_MARGIN        — quote USDT ama marginAsset != USDT
       LIVE_ELIGIBLE             — PERPETUAL + COIN underlying + USDT quote
                                   + USDT margin (mevcut execution yığını)
  6. Hacim/likidite filtresi KEŞİF AŞAMASINDA KULLANILMAZ (200M USDT eşiği
     yok); coin sayısı sınırı YOK (8/20/50/500 gibi tavanlar YOK). Kalite
     skorlama ve aday havuzu sonraki görevlerin kapsamıdır.

Kapsam izolasyonu: bu modül canary_live.SYMBOLS'e ve canlı giriş bağlantısına
DOKUNMAZ — yalnız keşif altyapısıdır; canlı bağlantı değişikliği ayrı operatör
onayı ister. Ağ gerektiğinde yalnız public imzasız GET (fetch_exchange_infos)
kullanılır; emir/imzalı uç YOKTUR.
"""
from dataclasses import dataclass, field

# ---- sınıflandırma kategorileri (Görev 5 spec) -----------------------------
LIVE_ELIGIBLE = "LIVE_ELIGIBLE"
UNSUPPORTED_QUOTE = "UNSUPPORTED_QUOTE"
UNSUPPORTED_MARGIN = "UNSUPPORTED_MARGIN"
UNSUPPORTED_CONTRACT_TYPE = "UNSUPPORTED_CONTRACT_TYPE"

# Kategori önceliği: coin düzeyinde 'en iyi' sözleşmeyi seçerken kullanılır.
_CATEGORY_PRIORITY = (LIVE_ELIGIBLE, UNSUPPORTED_QUOTE,
                      UNSUPPORTED_MARGIN, UNSUPPORTED_CONTRACT_TYPE)

# ---- execution desteği sabitleri (mevcut yığın: USDT-marjlı linear) --------
SUPPORTED_CONTRACT_TYPE = "PERPETUAL"
SUPPORTED_QUOTE_ASSET = "USDT"
SUPPORTED_MARGIN_ASSET = "USDT"
EXCLUDED_UNDERLYING_TYPES = ("INDEX", "PREMARKET")

# ---- venue etiketleri -------------------------------------------------------
VENUE_FAPI = "fapi"   # USDⓈ-M  (https://fapi.binance.com/fapi/v1/exchangeInfo)
VENUE_DAPI = "dapi"   # COIN-M  (https://dapi.binance.com/dapi/v1/exchangeInfo)

DEFAULT_FAPI_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
DEFAULT_DAPI_URL = "https://dapi.binance.com/dapi/v1/exchangeInfo"


@dataclass(frozen=True)
class Contract:
    """Tek TRADING sözleşmesi + execution sınıflandırması."""
    symbol: str
    venue: str                    # VENUE_FAPI | VENUE_DAPI
    base_asset: str
    quote_asset: str
    margin_asset: str
    contract_type: str
    underlying_type: str
    category: str                 # dört kategoriden biri
    reasons: tuple                # dışlama gerekçeleri (LIVE_ELIGIBLE'de boş)


@dataclass
class Coin:
    """baseAsset kimliğine göre normalize edilmiş coin ve onun sözleşmeleri."""
    base_asset: str
    contracts: list = field(default_factory=list)
    category: str = ""            # en iyi sözleşmenin kategorisi
    eligible_symbol: str = ""     # LIVE_ELIGIBLE sözleşmenin sembolü (yoksa "")

    def add(self, contract):
        self.contracts.append(contract)
        self._reclassify()

    def _reclassify(self):
        # Coin kategorisi: varsa LIVE_ELIGIBLE; yoksa öncelik sırasına göre
        # en 'yakın' destek kategorisi. eligible_symbol yalnız LIVE_ELIGIBLE'de.
        present = {c.category for c in self.contracts}
        for cat in _CATEGORY_PRIORITY:
            if cat in present:
                self.category = cat
                break
        self.eligible_symbol = ""
        for c in sorted(self.contracts, key=lambda c: c.symbol):
            if c.category == LIVE_ELIGIBLE:
                self.eligible_symbol = c.symbol
                break


@dataclass
class DiscoveryResult:
    """Tüm keşif çıktısı — sınır YOK, her coin ve gerekçe buradadır."""
    coins: list = field(default_factory=list)          # Coin, base'e göre sıralı
    contracts_by_category: dict = field(default_factory=dict)
    coin_counts_by_category: dict = field(default_factory=dict)
    not_trading: int = 0                               # keşfe girmeyen semboller
    venues: tuple = ()

    @property
    def contract_count(self):
        return sum(self.contracts_by_category.values())

    @property
    def coin_count(self):
        return len(self.coins)

    def eligible_coins(self):
        """LIVE_ELIGIBLE coinler (base, kanonik USDT sembolü) — sıralı liste."""
        return [(c.base_asset, c.eligible_symbol) for c in self.coins
                if c.category == LIVE_ELIGIBLE]


def _status_of(sym):
    """fapi 'status' / dapi 'contractStatus' alan adı farkını normalize eder."""
    return sym.get("status") or sym.get("contractStatus")


def classify_contract(sym, venue):
    """Tek sözleşmenin execution sınıflandırması (saf, hata toleranslı fail-closed:
    eksik alan → desteklenmiyor). Öncelik: tip → underlying → quote → margin."""
    contract_type = str(sym.get("contractType") or "")
    underlying = str(sym.get("underlyingType") or "")
    quote = str(sym.get("quoteAsset") or "")
    margin = str(sym.get("marginAsset") or "")
    base = str(sym.get("baseAsset") or "")
    symbol = str(sym.get("symbol") or "")

    if contract_type != SUPPORTED_CONTRACT_TYPE:
        return Contract(symbol, venue, base, quote, margin, contract_type,
                        underlying, UNSUPPORTED_CONTRACT_TYPE,
                        (f"contractType={contract_type or 'MISSING'}",))
    if underlying in EXCLUDED_UNDERLYING_TYPES:
        return Contract(symbol, venue, base, quote, margin, contract_type,
                        underlying, UNSUPPORTED_CONTRACT_TYPE,
                        (f"underlyingType={underlying}",))
    if quote != SUPPORTED_QUOTE_ASSET:
        reasons = [f"quote={quote or 'MISSING'}"]
        if margin != SUPPORTED_MARGIN_ASSET:
            reasons.append(f"margin={margin or 'MISSING'}")
        return Contract(symbol, venue, base, quote, margin, contract_type,
                        underlying, UNSUPPORTED_QUOTE, tuple(reasons))
    if margin != SUPPORTED_MARGIN_ASSET:
        return Contract(symbol, venue, base, quote, margin, contract_type,
                        underlying, UNSUPPORTED_MARGIN,
                        (f"margin={margin or 'MISSING'}",))
    return Contract(symbol, venue, base, quote, margin, contract_type,
                    underlying, LIVE_ELIGIBLE, ())


def discover_contracts(fapi_info=None, dapi_info=None):
    """exchangeInfo JSON'larından tam keşif (saf — ağ YOK, sınır YOK).

    fapi_info/dapi_info: /fapi/v1/exchangeInfo ve /dapi/v1/exchangeInfo
    yanıtları (dict; 'symbols' listesi). None/bozuk girişler boş katkıdır.
    TRADING olmayan semboller keşfe GİRMEZ (yalnız not_trading sayımına).
    """
    coins_by_base = {}
    contracts_by_category = {}
    not_trading = 0
    venues = []

    for venue, info in ((VENUE_FAPI, fapi_info), (VENUE_DAPI, dapi_info)):
        symbols = (info or {}).get("symbols")
        if not isinstance(symbols, list):
            continue
        venues.append(venue)
        for sym in symbols:
            if not isinstance(sym, dict) or _status_of(sym) != "TRADING":
                if isinstance(sym, dict):
                    not_trading += 1
                continue
            contract = classify_contract(sym, venue)
            contracts_by_category[contract.category] = \
                contracts_by_category.get(contract.category, 0) + 1
            base = contract.base_asset
            if not base:
                # baseAsset'i olmayan sembol coin kimliğine bağlanamaz;
                # yine de sözleşme düzeyinde raporlanabilir kalması için
                # sentetik kimlik kullanılır (ayrı 'coin' sayılmaz üzere
                # alt çizgiyle adlandırılır — çakışma riski yok).
                base = f"_{contract.symbol}"
            coin = coins_by_base.get(base)
            if coin is None:
                coin = Coin(base_asset=base)
                coins_by_base[base] = coin
            coin.add(contract)

    coins = sorted(coins_by_base.values(), key=lambda c: c.base_asset)
    coin_counts = {}
    for coin in coins:
        coin_counts[coin.category] = coin_counts.get(coin.category, 0) + 1
    return DiscoveryResult(coins=coins,
                           contracts_by_category=contracts_by_category,
                           coin_counts_by_category=coin_counts,
                           not_trading=not_trading,
                           venues=tuple(venues))


def format_report(res, max_lines=None):
    """İnsan-okur keşif raporu: her coin, sözleşmeleri ve dışlama gerekçeleri.
    max_lines verilirse yalnız ilk N coin satırı yazılır (toplam sayılar her
    zaman tam verilir) — keşif SONUCUNU kırpMAZ, yalnız görüntüyü sınırlar."""
    lines = []
    lines.append(f"venue'ler            : {', '.join(res.venues) or 'yok'}")
    lines.append(f"TRADING olmayan sembol: {res.not_trading}")
    lines.append(f"sözleşme (toplam)    : {res.contract_count}")
    for cat in _CATEGORY_PRIORITY:
        lines.append(f"  {cat:<28}: {res.contracts_by_category.get(cat, 0)}")
    lines.append(f"coin (baseAsset)     : {res.coin_count}")
    for cat in _CATEGORY_PRIORITY:
        lines.append(f"  {cat:<28}: {res.coin_counts_by_category.get(cat, 0)}")
    lines.append("")
    shown = res.coins if max_lines is None else res.coins[:max_lines]
    for coin in shown:
        tag = (f"{coin.eligible_symbol}" if coin.category == LIVE_ELIGIBLE
               else coin.category)
        lines.append(f"{coin.base_asset:<12} {tag}")
        for c in sorted(coin.contracts, key=lambda c: (c.venue, c.symbol)):
            why = (" — " + "; ".join(c.reasons)) if c.reasons else ""
            lines.append(f"    {c.venue}:{c.symbol:<18} {c.category}{why}")
    if max_lines is not None and len(res.coins) > max_lines:
        lines.append(f"… (+{len(res.coins) - max_lines} coin daha "
                     "(görüntü kırpıldı — keşif sonucu TAM)")
    return "\n".join(lines)


def fetch_exchange_infos(fapi_url=DEFAULT_FAPI_URL,
                         dapi_url=DEFAULT_DAPI_URL, timeout=20.0):
    """Salt-okunur public exchangeInfo GET'leri (imza/emir YOK).
    Keşif modülünün tek ağ çıkışı; testler bunu ÇAĞIRMAZ (fixture kullanır)."""
    import requests
    fapi = requests.get(fapi_url, timeout=timeout).json()
    try:
        dapi = requests.get(dapi_url, timeout=timeout).json()
    except Exception:
        dapi = {}          # COIN-M erişilemezse USDT-M keşfi sürer (fail-open
                           # yalnız RAPOR içindir; canlı yürütme bağlantısı YOK)
    return fapi, dapi


def main():
    """Salt-okunur keşif raporu (CLI): python3 contract_discovery.py"""
    fapi, dapi = fetch_exchange_infos()
    res = discover_contracts(fapi, dapi)
    print(format_report(res))
    print("\nRESULT: OK (salt-okunur public GET — emir/imza YOK)")


if __name__ == "__main__":
    main()
