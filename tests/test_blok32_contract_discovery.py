"""tests/test_blok32_contract_discovery.py — BLOK32 Görev 5 keşif modülü testleri.

TÜM fixture'lar SENTETİKTİR — gerçek Binance sözleşme sayılarıyla İLGİLİ
DEĞİL (Görev 5 talimatı: gerçek sayılar raporla, fixture sayılarıyla
karıştırma). Ağ ÇAĞRISI YOK: discover_contracts saf fonksiyondur,
fetch_exchange_infos testlerde ASLA çağrılmaz.
"""
import unittest

from contract_discovery import (
    LIVE_ELIGIBLE, UNSUPPORTED_QUOTE, UNSUPPORTED_MARGIN,
    UNSUPPORTED_CONTRACT_TYPE, classify_contract, discover_contracts,
    format_report, Coin, VENUE_FAPI, VENUE_DAPI,
)


def fapi_sym(symbol, base, quote="USDT", margin="USDT", ctype="PERPETUAL",
             underlying="COIN", status="TRADING"):
    """fapi (USDⓈ-M) fixture — durum alanı 'status'."""
    return {"symbol": symbol, "status": status, "baseAsset": base,
            "quoteAsset": quote, "marginAsset": margin,
            "contractType": ctype, "underlyingType": underlying}


def dapi_sym(symbol, base, quote="USD", margin=None, ctype="PERPETUAL",
             underlying="COIN", status="TRADING"):
    """dapi (COIN-M) fixture — durum alanı 'contractStatus'; marj baz coin."""
    return {"symbol": symbol, "contractStatus": status, "baseAsset": base,
            "quoteAsset": quote, "marginAsset": margin or base,
            "contractType": ctype, "underlyingType": underlying,
            "contractSize": 100}


def info(venue, symbols):
    return {venue: {"symbols": symbols}}


class TestClassifyContract(unittest.TestCase):

    def test_live_eligible_usdt_perp(self):
        c = classify_contract(fapi_sym("BTCUSDT", "BTC"), VENUE_FAPI)
        self.assertEqual(c.category, LIVE_ELIGIBLE)
        self.assertEqual(c.reasons, ())

    def test_tradifi_excluded(self):
        c = classify_contract(fapi_sym("TSLAUSDT", "TSLA",
                                       ctype="TRADIFI_PERPETUAL"), VENUE_FAPI)
        self.assertEqual(c.category, UNSUPPORTED_CONTRACT_TYPE)
        self.assertIn("TRADIFI_PERPETUAL", c.reasons[0])

    def test_index_and_premarket_excluded(self):
        for ut in ("INDEX", "PREMARKET"):
            c = classify_contract(fapi_sym("XUSDT", "X", underlying=ut),
                                  VENUE_FAPI)
            self.assertEqual(c.category, UNSUPPORTED_CONTRACT_TYPE)
            self.assertIn(ut, c.reasons[0])

    def test_quarterly_excluded(self):
        c = classify_contract(fapi_sym("BTCUSDT_261231", "BTC",
                                       ctype="CURRENT_QUARTER"), VENUE_FAPI)
        self.assertEqual(c.category, UNSUPPORTED_CONTRACT_TYPE)

    def test_usdc_quote_unsupported(self):
        c = classify_contract(fapi_sym("BTCUSDC", "BTC", quote="USDC",
                                       margin="USDC"), VENUE_FAPI)
        self.assertEqual(c.category, UNSUPPORTED_QUOTE)
        self.assertTrue(any("quote=USDC" in r for r in c.reasons))
        # marj da desteklenmiyorsa gerekçede görünür
        self.assertTrue(any("margin=USDC" in r for r in c.reasons))

    def test_coinm_inverse_unsupported_quote(self):
        c = classify_contract(dapi_sym("BTCUSD_PERP", "BTC"), VENUE_DAPI)
        self.assertEqual(c.category, UNSUPPORTED_QUOTE)
        self.assertTrue(any("quote=USD" in r for r in c.reasons))
        self.assertTrue(any("margin=BTC" in r for r in c.reasons))

    def test_usdt_quote_foreign_margin(self):
        c = classify_contract(fapi_sym("XYZUSDT", "XYZ", margin="BNB"),
                              VENUE_FAPI)
        self.assertEqual(c.category, UNSUPPORTED_MARGIN)
        self.assertIn("margin=BNB", c.reasons[0])

    def test_missing_fields_fail_closed(self):
        c = classify_contract({}, VENUE_FAPI)
        self.assertEqual(c.category, UNSUPPORTED_CONTRACT_TYPE)
        c2 = classify_contract({"symbol": "AUSDT", "contractType": "PERPETUAL",
                                "underlyingType": "COIN"}, VENUE_FAPI)
        self.assertEqual(c2.category, UNSUPPORTED_QUOTE)
        self.assertIn("MISSING", c2.reasons[0])


class TestDiscover(unittest.TestCase):

    def test_dapi_status_field_normalized(self):
        # dapi fixture'ında 'status' alanı YOK — yalnız contractStatus
        res = discover_contracts(
            None, info("dapi", [dapi_sym("ETHUSD_PERP", "ETH")])["dapi"])
        self.assertEqual(res.coin_count, 1)
        self.assertEqual(res.not_trading, 0)
        self.assertEqual(res.coins[0].category, UNSUPPORTED_QUOTE)

    def test_same_base_normalized_to_one_coin(self):
        # BTC'nin 3 farklı sözleşmesi → TEK coin, eligible_symbol kanonik
        res = discover_contracts(
            info("fapi", [fapi_sym("BTCUSDT", "BTC"),
                          fapi_sym("BTCUSDC", "BTC", quote="USDC",
                                   margin="USDC"),
                          fapi_sym("ETHBTC", "ETH", quote="BTC",
                                   margin="BTC")])["fapi"],
            info("dapi", [dapi_sym("BTCUSD_PERP", "BTC")])["dapi"])
        bases = [c.base_asset for c in res.coins]
        self.assertEqual(bases, ["BTC", "ETH"])      # BTC bir kez
        btc = res.coins[0]
        self.assertEqual(len(btc.contracts), 3)
        self.assertEqual(btc.category, LIVE_ELIGIBLE)
        self.assertEqual(btc.eligible_symbol, "BTCUSDT")
        eth = res.coins[1]
        self.assertEqual(eth.category, UNSUPPORTED_QUOTE)
        self.assertEqual(eth.eligible_symbol, "")

    def test_coin_with_only_usdc_not_eligible(self):
        res = discover_contracts(
            info("fapi", [fapi_sym("ABCUSDC", "ABC", quote="USDC",
                                   margin="USDC")])["fapi"], None)
        self.assertEqual(res.coin_count, 1)
        self.assertEqual(res.coins[0].category, UNSUPPORTED_QUOTE)
        self.assertEqual(res.eligible_coins(), [])

    def test_no_count_cap(self):
        # 600 sentetik coin — hiçbir tavan (8/20/50/500) kırpaması OLMAMALI
        syms = [fapi_sym(f"C{i:04d}USDT", f"C{i:04d}") for i in range(600)]
        res = discover_contracts(info("fapi", syms)["fapi"], None)
        self.assertEqual(res.coin_count, 600)
        self.assertEqual(res.contracts_by_category[LIVE_ELIGIBLE], 600)
        self.assertEqual(len(res.eligible_coins()), 600)

    def test_no_volume_filter_in_discovery(self):
        # exchangeInfo'da hacim alanı HIÇ YOK — keşif yalnız sözleşme
        # meta verisinden çalışır; 200M hacim eşiği keşif aşamasında YOK.
        # (Hacim/likidite filtresi sonraki görevlerin konusudur.)
        res = discover_contracts(
            info("fapi", [fapi_sym("DUSTUSDT", "DUST")])["fapi"], None)
        self.assertEqual(res.eligible_coins(), [("DUST", "DUSTUSDT")])

    def test_not_trading_excluded_from_discovery(self):
        res = discover_contracts(
            info("fapi", [fapi_sym("BTCUSDT", "BTC"),
                          fapi_sym("NEWUSDT", "NEW", status="PENDING_TRADING"),
                          fapi_sym("OLDUSDT", "OLD", status="SETTLING"),
                          {"symbol": "BROKEN", "status": "TRADING"},
                          "not-a-dict"])["fapi"], None)
        # TRADING olmayanlar keşfe girmez; string girişler tamamen atlanır;
        # baseAsset'siz TRADING sembolü ise sentetik kimlikle fail-closed
        # sınıflanır (ayrı testte ayrıntısı var).
        self.assertEqual(res.coin_count, 2)          # BTC + _BROKEN
        self.assertEqual(res.not_trading, 2)
        self.assertEqual(res.contract_count, 2)
        self.assertIn("_BROKEN", [c.base_asset for c in res.coins])

    def test_empty_and_malformed_inputs(self):
        empty = discover_contracts(None, None)
        self.assertEqual(empty.coin_count, 0)
        self.assertEqual(empty.contract_count, 0)
        self.assertEqual(empty.not_trading, 0)
        broken = discover_contracts({"symbols": "not-a-list"}, {})
        self.assertEqual(broken.coin_count, 0)

    def test_missing_base_gets_synthetic_identity(self):
        # baseAsset yoksa sözleşme yine sınıflanır ama ayrı coin kimliği
        # alt çizgili sentetik kimlikle tutulur (coin sayısını şişirmez,
        # gerçek base'lerle çakışamaz)
        res = discover_contracts(
            info("fapi", [{"symbol": "WEIRDUSDT", "status": "TRADING",
                           "quoteAsset": "USDT", "marginAsset": "USDT",
                           "contractType": "PERPETUAL",
                           "underlyingType": "COIN"}])["fapi"], None)
        self.assertEqual(res.coin_count, 1)
        self.assertEqual(res.coins[0].base_asset, "_WEIRDUSDT")
        self.assertEqual(res.coins[0].category, LIVE_ELIGIBLE)

    def test_deterministic_sorting(self):
        syms = [fapi_sym("ZULUUSDT", "ZULU"), fapi_sym("ALPHAUSDT", "ALPHA"),
                fapi_sym("MIDUSDT", "MID")]
        res = discover_contracts(info("fapi", syms)["fapi"], None)
        self.assertEqual([c.base_asset for c in res.coins],
                         ["ALPHA", "MID", "ZULU"])

    def test_category_counts_consistent(self):
        res = discover_contracts(
            info("fapi", [fapi_sym("BTCUSDT", "BTC"),
                          fapi_sym("BTCUSDC", "BTC", quote="USDC",
                                   margin="USDC"),
                          fapi_sym("TSLAUSDT", "TSLA",
                                   ctype="TRADIFI_PERPETUAL"),
                          fapi_sym("IDXUSDT", "IDX", underlying="INDEX"),
                          fapi_sym("QUSDT", "Q", ctype="NEXT_QUARTER")])["fapi"],
            None)
        self.assertEqual(res.contracts_by_category[LIVE_ELIGIBLE], 1)
        self.assertEqual(res.contracts_by_category[UNSUPPORTED_QUOTE], 1)
        self.assertEqual(res.contracts_by_category[UNSUPPORTED_CONTRACT_TYPE], 3)
        self.assertEqual(res.contracts_by_category.get(UNSUPPORTED_MARGIN, 0), 0)
        # coin düzeyi: BTC eligible, TSLA/IDX/Q tip-dışı
        self.assertEqual(res.coin_counts_by_category[LIVE_ELIGIBLE], 1)
        self.assertEqual(res.coin_counts_by_category[UNSUPPORTED_CONTRACT_TYPE], 3)


class TestReport(unittest.TestCase):

    def test_report_lists_every_coin_and_reasons(self):
        syms = [fapi_sym("BTCUSDT", "BTC"),
                fapi_sym("BTCUSDC", "BTC", quote="USDC", margin="USDC"),
                fapi_sym("ETHUSD_PERP_DUMMY", "ETH", quote="USDT",
                         margin="ETH")]
        res = discover_contracts(info("fapi", syms)["fapi"], None)
        text = format_report(res)
        self.assertIn("BTC", text)
        self.assertIn("BTCUSDT", text)
        self.assertIn("quote=USDC", text)          # dışlama gerekçesi görünür
        self.assertIn("margin=ETH", text)
        self.assertIn("LIVE_ELIGIBLE", text)

    def test_report_max_lines_display_only(self):
        syms = [fapi_sym(f"S{i:03d}USDT", f"S{i:03d}") for i in range(50)]
        res = discover_contracts(info("fapi", syms)["fapi"], None)
        text = format_report(res, max_lines=5)
        self.assertIn("coin (baseAsset)     : 50", text)  # toplamlar TAM
        self.assertIn("+45 coin daha", text)        # görüntü kırpıldı notu
        # keşif SONUCU kırpmadan: eligible listesi tam
        self.assertEqual(len(res.eligible_coins()), 50)

    def test_coin_dataclass_reclassify(self):
        coin = Coin(base_asset="X")
        coin.add(classify_contract(fapi_sym("XUSDC", "X", quote="USDC",
                                            margin="USDC"), VENUE_FAPI))
        self.assertEqual(coin.category, UNSUPPORTED_QUOTE)
        self.assertEqual(coin.eligible_symbol, "")
        coin.add(classify_contract(fapi_sym("XUSDT", "X"), VENUE_FAPI))
        self.assertEqual(coin.category, LIVE_ELIGIBLE)
        self.assertEqual(coin.eligible_symbol, "XUSDT")


if __name__ == "__main__":
    unittest.main()
