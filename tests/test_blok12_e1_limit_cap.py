"""BLOK12 devam-F §2 (E1): GERÇEKLEŞEN doluş için 50 USDT mutlak tavan —
LONG LIMIT sabit fiyat, SHORT fail-closed. Ağ YOK.

Kaynak kanıtı (canlı imaj, ~/faz9-c6-evidence/rootfs — tahmin DEĞİL):
  * OrderType: MARKET=1, LIMIT=2, LIMIT_MAKER=3
    (hummingbot/core/data_type/common.py) → eski open_order_type:1 = MARKET,
    gerçekleşen fiyata SINIR YOK (inceleme bulgusu doğru).
  * PositionExecutorConfig.entry_price top-level; LIMIT yolunda emir
    SABİT entry_price'a konur (oracle yalnız entry_price=None iken kullanılır;
    position_executor.py:48 ve entry_price property), activation_bounds=None
    → anında gönderilir. executor_service.py: config_class(**executor_config)
    — alan olduğu gibi geçer.
  * Borsa kuralı: BUY limit emir kendi fiyatının ÜSTÜNDE asla dolmaz →
    LONG için Σ gerçekleşen quote ≤ A × P_cap MATEMATİKSEL olarak sınırlı.
  * SHORT doluşları limitin ÜSTÜNDE fiyatla gerçekleşebilir → realized quote
    hiçbir emir türüyle yukarıdan sınırlandırILAMAZ → dürüst çözüm: validation
    modunda SHORT giriş fail-closed RED (VALIDATION_ALLOW_SHORT_ENTRIES=true
    ile operatör bilinçli olarak drift-zarfına razı olur; garanti DEĞİL).

Kabul kriterleri (görev §2): (1) aleyhe hareket altında tavan; (2) yuvarlama
sonrası amount×limit ≤ 50; (3) kısmi dolum+iptal+mutabakat tavanı aşmaz;
(4) ACK/UNKNOWN tekrar gönderim yok; (5) teyitli sıfır-dolum hakkı serbest;
(6) retry tavanı baypas edemez; (7) ESKİ MARKET yolunun ihlale izin verdiği
AÇIKÇA test edilir (sahte garanti üretilmez).
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_trader                                      # noqa: E402
from test_blok12_abs_cap import _budget_env             # noqa: E402
from test_inc13_live_wiring import (FakeBinance, FakeHB,  # noqa: E402
                                    LiveWiringBase, _RiskOK, _coordinator, SYM)

FILT = {"LOT_SIZE": {"stepSize": "0.1", "minQty": "0.01"},
        "PRICE_FILTER": {"tickSize": "0.01"},
        "MIN_NOTIONAL": {"notional": "5"}}


class TestCapHelper(unittest.TestCase):
    """_validation_entry_cap: P_cap tick'e, A step'e AŞAĞI yuvarlanır;
    Decimal A×P_cap ≤ 50 BİREBİR doğrulanır (tahmini slippage fiyat
    sınırının yerine GEÇMEZ — sınırın kendisi tavanı taşır)."""

    def _cap(self, price, amount, filt=None, side=1, slip=0.0, allow_short=None):
        tmp = Path(tempfile.mkdtemp(prefix="b12f-"))
        old_allow = os.environ.get("VALIDATION_ALLOW_SHORT_ENTRIES")
        if allow_short is not None:
            os.environ["VALIDATION_ALLOW_SHORT_ENTRIES"] = allow_short
        try:
            with _budget_env(tmp, per=50, total=100):
                return auto_trader._validation_entry_cap(
                    "ETHUSDT", side, price, amount, filt or FILT, slip)
        finally:
            if old_allow is None:
                os.environ.pop("VALIDATION_ALLOW_SHORT_ENTRIES", None)
            else:
                os.environ["VALIDATION_ALLOW_SHORT_ENTRIES"] = old_allow

    def test_01_normal_plan_tavan_alti_korunur(self):
        ok, why, p_cap, amt, maxq = self._cap(80.0, 0.6)
        self.assertTrue(ok, why)
        self.assertEqual(p_cap, Decimal("80.40"))     # 80×1.005 tick-floor
        self.assertEqual(amt, Decimal("0.6"))          # plan aynen kalır
        self.assertLessEqual(Decimal("0.6") * Decimal("80.40"),
                             auto_trader.ABS_ORDER_NOTIONAL_CAP_USDT_D)
        self.assertEqual(maxq, Decimal("0.6") * Decimal("80.40"))

    def test_02_tavan_asti_plan_red_kirpma_yok(self):
        # Plan 80 USDT'lik (1.0@80): E1 SİZE DEĞİŞTİRMEZ — POST'A GİTMEDEN RED.
        # (Operatör onaylı boyut rıza dışında küçültülmez; devam-C/D/E ÖN-gard
        # sözleşmesi.) Tavan-altı plan (0.6) aleyhe harekette bile A×P_cap'e
        # matematiksel bağlıdır (BUY limit üstü dolmaz — borsa kuralı).
        ok, why, _p, _a, _m = self._cap(80.0, 1.0)
        self.assertFalse(ok)
        self.assertIn("tavan", why.lower())
        self.assertIn("RED", why)
        ok2, _w2, p2, a2, m2 = self._cap(80.0, 0.6)
        self.assertTrue(ok2)
        self.assertLessEqual(a2 * p2, auto_trader.ABS_ORDER_NOTIONAL_CAP_USDT_D)
        self.assertEqual(m2, Decimal("0.6") * Decimal("80.40"))

    def test_03_yuvarlama_sonrasi_birebir_kontrol(self):
        # tick/step ondalık tuzak: 0.001 step, 100 fiyat → tavan-altı EN
        # BÜYÜK yuvarlak A 0.497'dir (0.498 → 50.049 > 50 olurdu — RED).
        filt = {"LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}
        ok, why, p_cap, amt, maxq = self._cap(100.0, 0.497, filt=filt)
        self.assertTrue(ok, why)
        self.assertEqual(p_cap, Decimal("100.50"))
        self.assertEqual(amt * p_cap, Decimal("49.948500"))
        self.assertLessEqual(amt * p_cap,
                             auto_trader.ABS_ORDER_NOTIONAL_CAP_USDT_D)
        ok2, why2, _p2, _a2, _m2 = self._cap(100.0, 0.498, filt=filt)
        self.assertFalse(ok2, "0.498×100.50=50.049 > 50 — RED vermeliydi")

    def test_04_min_qty_tavani_zorlarsa_red(self):
        # step 0.1 → A_max=0.6; minQty 10 ise TAVANLI geçerli miktar YOK →
        # sessiz küçültme YOK, emir RED (fail-closed).
        filt = {"LOT_SIZE": {"stepSize": "0.1", "minQty": "10"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "5"}}
        ok, why, _p, _a, _m = self._cap(80.0, 0.6, filt=filt)
        self.assertFalse(ok)
        self.assertIn("minQty", why)

    def test_05_min_notional_alti_plan_red(self):
        # Tavan-içi ama borsanın min-notional'ının altında: 99×0.5=49.5 ≤ 50
        # tavan ✓ fakat 49.5 < 55 MIN_NOTIONAL → RED (borsa reddederdi;
        # erken ve anlaşılır red).
        filt = {"LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
                "PRICE_FILTER": {"tickSize": "0.01"},
                "MIN_NOTIONAL": {"notional": "55"}}
        ok, why, _p, _a, _m = self._cap(0.5, 99.0, filt=filt)
        self.assertFalse(ok)
        self.assertIn("notional", why.lower())

    def test_06_short_fail_closed_varsayilan(self):
        ok, why, _p, _a, _m = self._cap(80.0, 0.6, side=2)
        self.assertFalse(ok)
        self.assertIn("SHORT", why)
        # OPERATör bilinçli izin verirse: garanti YOK — dürüst etiket.
        ok2, why2, p2, a2, m2 = self._cap(80.0, 0.6, side=2, allow_short="true")
        self.assertTrue(ok2, why2)
        self.assertIsNone(p2)                          # pin YOK
        self.assertIn("drift", why2.lower())           # garanti DEĞİL — zarf

    def test_07_validation_kapali_eski_yol_dokunulmaz(self):
        tmp = Path(tempfile.mkdtemp(prefix="b12f-"))
        old = os.environ.get("CONTROLLED_LIVE_VALIDATION")
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "false"
        try:
            ok, why, p, a, m = auto_trader._validation_entry_cap(
                "ETHUSDT", 1, 80.0, 0.6, FILT, 0.0)
            self.assertTrue(ok)
            self.assertIn("N/A", why)
            self.assertIsNone(p)
            self.assertEqual(a, Decimal("0.6"))        # miktar DEĞİŞMEZ
        finally:
            if old is None:
                os.environ.pop("CONTROLLED_LIVE_VALIDATION", None)
            else:
                os.environ["CONTROLLED_LIVE_VALIDATION"] = old


class TestBuildExecutorConfig(unittest.TestCase):
    def test_08_entry_price_limit_turu_sabitler(self):
        cfg = auto_trader.build_executor_config("ETHUSDT", 0.6, 3, 1,
                                                entry_price=Decimal("80.40"))
        tbc = cfg["triple_barrier_config"]
        self.assertEqual(tbc["open_order_type"], 2)    # LIMIT (MARKET=1)
        self.assertEqual(cfg["entry_price"], "80.40")  # top-level, str
        # K1=B sözleşmesi bozulmaz: kapanış bariyerleri hâlâ yok.
        for k in ("stop_loss", "take_profit", "time_limit", "trailing_stop"):
            self.assertIsNone(tbc[k])

    def test_09_eski_market_yolu_ihlale_izin_verir_ACIK_KANIT(self):
        # Kriter 7: pin OLMAYAN config MARKET (type 1) gider; emir her fiyatta
        # dolabilir → gerçekleşen quote üst sınırı YOKTUR. Bu, E1 öncesi
        # yolun belgelenmiş zaafıdır — sahte garanti iddia edilmez.
        cfg = auto_trader.build_executor_config("ETHUSDT", 0.6, 3, 1)
        self.assertEqual(cfg["triple_barrier_config"]["open_order_type"], 1)
        self.assertNotIn("entry_price", cfg)
        # Teyit: MARKET yolunda tek başına 0.6 ETH planı 5000 USDT'lik fill'e
        # kapı açar (fiyat bağı yok) — tavan ancak LIMIT pini ile vardır.
        self.assertGreater(Decimal("0.6") * Decimal("5000"),
                           auto_trader.ABS_ORDER_NOTIONAL_CAP_USDT_D)


class _RecordingHB(FakeHB):
    def __init__(self):
        super().__init__()
        self.calls = []

    def create_executor(self, symbol, amount, lev, sl, tp, tl, side=1,
                        entry_price=None):
        self.calls.append({"symbol": symbol, "amount": amount, "side": side,
                           "entry_price": entry_price})
        return super().create_executor(symbol, amount, lev, sl, tp, tl,
                                       side=side, entry_price=entry_price)


class _UnknownHB2(FakeHB):
    def create_executor(self, *a, side=1, entry_price=None, **k):
        return {"outcome": "UNKNOWN", "error": "timeout"}


class _FailHB2(FakeHB):
    def create_executor(self, *a, side=1, entry_price=None, **k):
        return {"outcome": "NOT_CREATED", "error": "hb refused"}


class TestOpenCoinE1(LiveWiringBase):
    """Gerçek open_coin, bütçe AÇIK (conftest 2/50/100): pin, pending zarfı,
    UNKNOWN/teyitli başarısızlık kaderleri E1 altında da doğru."""

    def _run(self, hb, symbol=None):
        binance = FakeBinance()
        cc, _ = _coordinator(binance, hb, self.db)
        coin = self._coin()
        if symbol:
            coin["symbol"] = symbol
        res = auto_trader.open_coin(binance, hb, coin, dry_run=False,
                                    risk=_RiskOK(), reserver=None,
                                    coordinator=cc)
        return binance, cc, res


class TestE1AtOpenCoin(TestOpenCoinE1):
    def setUp(self):
        super().setUp()
        # Teyitli başarısızlık karalisteye yazar: dosyayı bu sınıfın tmp'sine
        # çevir (suite-geneli sızıntı yok — devam-E desenleriyle aynı).
        self._old_bl = auto_trader.BLACKLIST_FILE
        auto_trader.BLACKLIST_FILE = Path(self.tmp) / ".bl.json"
        self.addCleanup(setattr, auto_trader, "BLACKLIST_FILE", self._old_bl)

    def test_10_gonderim_limit_pini_ile(self):
        hb = _RecordingHB()
        _b, _c, res = self._run(hb)
        self.assertTrue(res)
        self.assertEqual(len(hb.calls), 1)
        call = hb.calls[0]
        self.assertEqual(call["entry_price"], Decimal("80.40"))
        self.assertEqual(str(call["amount"]), "0.6")
        # Zarflar: limit tavanı pending'e işlendi (restart'ta da okunur).
        pend = list(auto_trader._PENDING_FILLS.values())[0]
        self.assertEqual(pend["entry_price_cap"], 80.4)
        self.assertEqual(pend["max_realized_quote"], 48.24)
        self.assertLessEqual(pend["max_realized_quote"], 50.0)
        # En-kötü zarf ARTIK plan×(1+drift) değil TAM A×P_cap (garanti):
        self.assertEqual(pend["worst_fill_usdt"], 48.24)

    def test_11_kismi_dolum_iptal_mutabakat_tavan_alti(self):
        # Kısmi dolum (0.2@80.40) + kalanın iptali: gerçekleşen 16.08 ≤ 48.24
        # → dolum denetimi İHLAL vermez; mutabakat aynı intent'i YENİDEN
        # göndermez (hb.created tek kalır).
        binance, hb = FakeBinance(), _RecordingHB()
        cc, _ = _coordinator(binance, hb, self.db)
        self.assertTrue(auto_trader.open_coin(binance, hb, self._coin(),
                                              dry_run=False, risk=_RiskOK(),
                                              reserver=None, coordinator=cc))
        pend = list(auto_trader._PENDING_FILLS.values())[0]
        worst = pend["worst_fill_usdt"]
        # a) kısmi dolum denetimden geçer:
        self.assertIsNone(auto_trader._validation_fill_exceeded(
            SYM, str(Decimal("0.2") * Decimal("80.40"))))
        # b) tavan-üstü GERÇEK dolum yakalanır (sahte-geçiş yok):
        self.assertIsNotNone(auto_trader._validation_fill_exceeded(
            SYM, str(worst + 5)))
        # c) retry aynı intent ile hak YENİDEN tüketmez / ikinci POST yok:
        res2 = auto_trader.open_coin(binance, hb, self._coin(), dry_run=False,
                                     risk=_RiskOK(), reserver=None,
                                     coordinator=cc)
        self.assertFalse(res2)
        self.assertEqual(len(hb.calls), 1)

    def test_12_unknown_kader_tek_gonderim_hak_korunur(self):
        # Kriter 4: UNKNOWN → tekrar GÖNDERİM yok; bütçe hakkı KORUNUR.
        hb = _UnknownHB2()
        _b, _c, res = self._run(hb)
        self.assertFalse(res)
        self.assertEqual(hb.created, [])
        st = auto_trader._VB_BUDGET.status()
        self.assertEqual(st["trades_used"], 1)         # hak iade EDİLMEZ

    def test_13_teyitli_sifir_dolum_hak_iade_retry_acik(self):
        # Kriter 5+6: teyitli başarısızlık → hak iade; retry tavanı baypas
        # edemez (retry da LIMIT piniyle gider).
        hb = _FailHB2()
        _b, _c, res = self._run(hb)
        self.assertFalse(res)
        st = auto_trader._VB_BUDGET.status()
        self.assertEqual(st["trades_used"], 0)         # iade edildi
        hb2 = _RecordingHB()
        # İlk deneme teyitli başarısızlıkla karalanan sembolü değil, YENİ
        # sembolü dene (karaliste davranışı bu testin konusu değil).
        _b2, _c2, res2 = self._run(hb2, symbol="BTCUSDT")
        self.assertTrue(res2)
        self.assertEqual(hb2.calls[0]["entry_price"], Decimal("80.40"))

    def test_14_tavan_ustu_plan_e1_red_post_yok(self):
        # Regresyon simülasyonu (stage 200 + RiskEngine 80): plan 80 USDT →
        # E1 RED, POST YOK, sessiz kırpma YOK, bütçe hakkı YANMAZ
        # (E1 reserve'den ÖNCE).
        class _Risk80(_RiskOK):
            def approve_trade(self, balance, slippage, take_profit_pct=None,
                              equity=None, max_position_pct=None):
                return 80.0, "approved"
        hb = _RecordingHB()
        binance = FakeBinance()
        cc, _ = _coordinator(binance, hb, self.db)
        with mock.patch.object(auto_trader, "MAX_NOTIONAL_USDT", 200.0):
            res = auto_trader.open_coin(binance, hb, self._coin(),
                                        dry_run=False, risk=_Risk80(),
                                        reserver=None, coordinator=cc)
        self.assertFalse(res)
        self.assertEqual(hb.calls, [])
        st = auto_trader._VB_BUDGET.status()
        self.assertEqual(st["trades_used"], 0)


class TestRotationOfGuarantee(unittest.TestCase):
    def test_15_garanti_kaydigi_sozlesme(self):
        # Dürüstlük kontrolü: SHORT allow=true durumunda dahi config MARKET
        # kalmazsa? Operatör izni pin ÜRETMEZ (garanti iddiası yok) — config
        # eski MARKET yolunda kalır ve bu BELGELENİR.
        cfg = auto_trader.build_executor_config("BTCUSDT", 0.5, 3, 2)
        self.assertEqual(cfg["triple_barrier_config"]["open_order_type"], 1)


if __name__ == "__main__":
    unittest.main()
