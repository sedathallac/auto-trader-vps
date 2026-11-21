"""BLOK11 §10.3 + BLOK12 §2 — bütçe kapısı fail-closed + deterministik intent: bütçe KAPALI iken canlı giriş YOK.

BLOK10 doğrulanmış durumu: DISABLED modda bütçe pas-through idi
(ok=True, "DISABLED"). Misyon artık emrediyor: "Bütçe kontrolü kapalıysa
hiçbir işlem açılmasın." Bu dosya çevrimin KIRMIZI kanıtıdır.

BLOK12 §2 imza değişikliği: reserve artık DETERMİNİSTİK intent alır
(uuid4 yerine); intent=None → RED (fail-closed).

dry_run kapsam dışıdır: open_coin "[DRY] emir yok" noktasından (auto_trader.py
~3677) bu kapıya ulaşmadan döner; bütçe hakkı yalnız GERÇEK yolun
create_executor POST'u hemen öncesinde ayrılır (BLOK10 §3 bağlantı noktası).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_trader
from test_inc13_live_wiring import LiveWiringBase


def _reset_vb():
    """Modül-seviyesi tembel önbellek testler arası sızmasın."""
    auto_trader._VB_CFG = None
    auto_trader._VB_BUDGET = None


class TestBudgetGateFailClosed(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="blok11-vb-")
        os.environ["VALIDATION_BUDGET_DB"] = os.path.join(self.tmp, "vb.db")
        for k in ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
                  "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
                  "MAX_VALIDATION_TOTAL_NOTIONAL_USDT"):
            os.environ.pop(k, None)
        _reset_vb()

    def tearDown(self):
        _reset_vb()
        for k in ("VALIDATION_BUDGET_DB", "CONTROLLED_LIVE_VALIDATION"):
            os.environ.pop(k, None)

    # ---- helper düzeyi ------------------------------------------------------
    def test_disabled_mod_reddedilir(self):
        """BAYRAK KAPALI (varsayılan üretim hali) → RED + DB dosyası YARATILMAZ."""
        ok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "val:ETHUSDT:1:40:100")
        self.assertFalse(ok)
        self.assertIsNone(rid)
        self.assertIn("BÜTÇE-KAPALI", why)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "vb.db")))

    def test_gecersiz_config_reddedilir(self):
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["MAX_VALIDATION_TRADES"] = "99"  # HARD_MAX_TRADES=2'den büyük
        _reset_vb()
        ok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "val:ETHUSDT:1:40:100")
        self.assertFalse(ok)
        self.assertIn("geçersiz", why)

    def test_acik_ve_gecerli_izin_verir(self):
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        _reset_vb()
        ok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "val:ETHUSDT:1:40:100")
        self.assertTrue(ok, msg=why)
        self.assertIsNotNone(rid)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "vb.db")))

    def test_ucuncu_islem_reddedilir(self):
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        _reset_vb()
        r1 = auto_trader._validation_reserve("ETHUSDT", 40.0, "val:ETHUSDT:1:40:100")
        r2 = auto_trader._validation_reserve("BCHUSDT", 15.0, "val:BCHUSDT:1:15:100")
        r3 = auto_trader._validation_reserve("GALAUSDT", 10.0, "val:GALAUSDT:1:10:100")
        self.assertTrue(r1[0], msg=r1[2])
        self.assertTrue(r2[0], msg=r2[2])
        self.assertFalse(r3[0])
        self.assertIn("işlem", r3[2])

    def test_per_trade_tavan_asilamaz(self):
        """§13 tablosu: >50 USDT/emir meşru RED (c6 harness bypass'ının gerekçesi)."""
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        _reset_vb()
        ok, rid, why = auto_trader._validation_reserve("NEARUSDT", 50.01, "val:NEARUSDT:1:50.01:100")
        self.assertFalse(ok)
        self.assertIsNone(rid)
        self.assertIn("PER_TRADE_CAP", why)
        ok2, rid2, _ = auto_trader._validation_reserve("NEARUSDT", 50.0, "val:NEARUSDT:1:50:100")
        self.assertTrue(ok2, msg="tam 50 kabul (≤ tavan)")

    def test_db_acilamazsa_fail_closed(self):
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["VALIDATION_BUDGET_DB"] = os.path.join(
            self.tmp, "yok-boyle-bir-dizin", "vb.db")
        _reset_vb()
        ok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "val:ETHUSDT:1:40:100")
        self.assertFalse(ok)
        # Hata açılışta ("açılamadı") ya da reserve'da (BudgetError) yakalanabilir;
        # ikisi de fail-closed RED'dir — önemli olan RED + rid YOK.
        self.assertIsNone(rid)
        self.assertIn("bütçe deposu", why)


class TestOpenCoinLiveRejectsWithoutBudget(LiveWiringBase):
    """Uçtan uca: BLOK10'un gerçek canlı harness'iyle (LiveWiringBase stub'ları)
    — bütçe kapalıyken open_coin RED ve create_executor HİÇ çağrılmaz (emir
    yok); açık+geçerli iken AYNI harness TRUE geçer (pozitif kontrol)."""

    def setUp(self):
        super().setUp()
        os.environ["VALIDATION_BUDGET_DB"] = os.path.join(self.tmp, "vb.db")
        os.environ.pop("CONTROLLED_LIVE_VALIDATION", None)
        _reset_vb()

    def tearDown(self):
        _reset_vb()
        os.environ.pop("VALIDATION_BUDGET_DB", None)
        super().tearDown()

    def _drive_live_entry(self):
        from test_inc13_live_wiring import FakeBinance, FakeHB, _RiskOK, SYM
        binance = FakeBinance()
        hb = FakeHB()
        coin = {"symbol": SYM, "lastPrice": "80", "quoteVolume": "1000000",
                "_score": 0.5, "priceChangePercent": "2"}
        res = auto_trader.open_coin(binance, hb, coin, dry_run=False,
                                    risk=_RiskOK(), reserver=None,
                                    coordinator=None)
        return res, hb

    def test_open_coin_butceli_gecer_kontrol(self):
        """POZİTİF KONTROL: bütçe AÇIK + geçerli iken aynı harness True döner ve
        create_executor ÇAĞRILIR. Bu, aşağıdaki RED'in yegâne farkının bütçe
        kapısı olduğunu kanıtlar (başka bir kapı sızdırmıyor)."""
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        _reset_vb()
        res, hb = self._drive_live_entry()
        self.assertTrue(res)
        self.assertEqual(len(getattr(hb, "created", [])), 1)

    def test_open_coin_canli_butsuz_red(self):
        os.environ.pop("CONTROLLED_LIVE_VALIDATION", None)
        _reset_vb()
        res, hb = self._drive_live_entry()
        self.assertFalse(res)
        self.assertEqual(getattr(hb, "created", []), [],
                         "bütçe kapalıyken create_executor çağrılmamalı")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "vb.db")),
                         "reddedilen giriş bütçe deposu bile yaratmamalı")


if __name__ == "__main__":
    unittest.main()
