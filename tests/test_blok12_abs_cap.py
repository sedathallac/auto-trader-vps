"""BLOK12 devam-E §1: 50 USDT MUTAK emir tavanı — KODDA, kanıtlı.

İnceleme bulgusu: "50 USDT mutlak emir tavanı → Kanıtlanmadı". Mevcut
savunma katmanları (kanıtlanarak belgelendi):
  L1 validation_budget config clamp: MAX_VALIDATION_POSITION_NOTIONAL_USDT
     > 50 → yapılandırma GEÇERSİZ → tüm giriş zinciri fail-closed RED;
  L2 reserve PER_TRADE_CAP: ayrılan notional > tavan → RED;
  L3 worst-fill guard (ön gard): plan×(1+drift) > tavan → POST YOK;
  L4 (YENİ — devam-E): auto_trader'da BAĞIMSIZ sabit
     ABS_ORDER_NOTIONAL_CAP_USDT = 50.0 — zarf/guard/dolum denetimi
     tavanı min(bütçe-tavanı, 50) ile hesaplar. L1 tek başına tek noktadan
     bağımlılıktı: clamp'ı biri gevşetirse (regresyon) 80 USDT'lik plan
     POST'a gidebilirdi — L4 bunu KODDA imkânsız kılar. Env tavanı yalnız
     SIKILAŞTIRABİLİR (min), gevşetemem.

Kırmızı→yeşil: L1 clamp'ı 100'e gevşetilmiş gibi simüle edildi (regresyon
senaryosu) — HEAD'de zarf tavanı 100'e çıktı ve 80 USDT'lik plan guard'dan
geçti (kanıt: hb.created dolu); L4 sonrası tavan 50'de kalır, POST YOK.
Ağ YOK.
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_trader                                      # noqa: E402
import validation_budget as VB                          # noqa: E402
from test_inc13_live_wiring import (FakeBinance, FakeHB,  # noqa: E402
                                    LiveWiringBase, _RiskOK, _coordinator)

_ENV_KEYS = ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
             "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
             "MAX_VALIDATION_TOTAL_NOTIONAL_USDT",
             "VALIDATION_APPROVAL_DB", "OPERATOR_APPROVAL_TOKEN_FILE")


@contextlib.contextmanager
def _budget_env(tmp, per, total):
    """Bütçeyi verilen tavanlarla kurar; _VB önbelleğini sıfırlar, geri yükler."""
    old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    old_dir, old_bl = auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE
    saved = (auto_trader._VB_CFG, auto_trader._VB_BUDGET)
    auto_trader.STATE_DIR = tmp
    auto_trader.BLACKLIST_FILE = tmp / ".bl.json"
    os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
    os.environ["MAX_VALIDATION_TRADES"] = "2"
    os.environ["MAX_VALIDATION_POSITION_NOTIONAL_USDT"] = str(per)
    os.environ["MAX_VALIDATION_TOTAL_NOTIONAL_USDT"] = str(total)
    os.environ["VALIDATION_APPROVAL_DB"] = str(tmp / "gate.db")
    os.environ.pop("OPERATOR_APPROVAL_TOKEN_FILE", None)
    auto_trader._VB_CFG = None
    auto_trader._VB_BUDGET = None
    try:
        yield
    finally:
        auto_trader._VB_CFG, auto_trader._VB_BUDGET = saved
        auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE = old_dir, old_bl
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _clamped_relaxed(per, total):
    """L1 regresyon simülasyonu: bütçe modülünün sert clamp'ı GEVŞETİLDİ
    (HARD 100/200) — L4'ün (auto_trader mutlak sabiti) bunu tek başına
    tuttuğunu kanıtlamak için. production koduna dokunmaz (mock)."""
    with mock.patch.object(VB, "HARD_MAX_PER_TRADE_USDT", float(per)), \
         mock.patch.object(VB, "HARD_MAX_TOTAL_USDT", float(total * 2)):
        with _budget_env(Path(tempfile.mkdtemp(prefix="b12e1-")),
                         per=per, total=total * 2):
            yield


class TestAbsCapConstant(unittest.TestCase):
    def test_sabit_50_ve_env_okumaz(self):
        # Sabit KODDA 50.0; env/parametre ile gevşetilemez.
        self.assertEqual(auto_trader.ABS_ORDER_NOTIONAL_CAP_USDT, 50.0)
        src = open(os.path.join(_ROOT, "auto_trader.py"), encoding="utf-8").read()
        i = src.index("ABS_ORDER_NOTIONAL_CAP_USDT =")
        decl = src[i:src.index("\n", i)]
        self.assertIn("= 50.0", decl)
        self.assertNotIn("getenv", decl)


class TestExistingLayers(unittest.TestCase):
    """L1'in bugünkü gerçeği (doğrulama): env > 50 YAPILEANDIRILAMAZ →
    tüm zincir fail-closed. (Bu test düzeltmeden önce de geçiyordu —
    incelemeye verilen cevap: katman vardı, KANIT yoktu.)"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="b12e1-"))

    def test_env_100_gecersiz_zincir_kapali(self):
        with _budget_env(self.tmp, per=100, total=200):
            enabled, valid, budget, why = auto_trader._validation_control()
            self.assertTrue(enabled)
            self.assertFalse(valid)          # L1 clamp: 100 > 50 → geçersiz
            self.assertIn("50", why)
            self.assertIsNone(auto_trader._validation_fill_envelope(40.0, 0.0))
            ok, _ = auto_trader._validation_fill_guard(40.0, 0.0)
            self.assertFalse(ok)             # geçersiz config → RED (fail-closed)

    def test_env_sikilastirabilir_min_korunur(self):
        with _budget_env(self.tmp, per=40, total=100):
            cap, _d, _w = auto_trader._validation_fill_envelope(30.0, 0.0)
            self.assertEqual(cap, 40.0)      # min(40, 50): env daha sıkı olabilir


class TestAbsCapSurvivesClampRegression(unittest.TestCase):
    """L1 gevşetilse bile (regresyon) L4 tavanı 50'de tutar — kırmızı kanıt
    HEAD'de üretildi: cap 100'e çıktı, 80'lük plan guard'dan geçti."""

    def test_zarf_cap_clamp_100_iken_bile_50(self):
        with _clamped_relaxed(per=100, total=100):
            env = auto_trader._validation_fill_envelope(40.0, 0.0)
            self.assertIsNotNone(env)
            cap, _drift, worst = env
            self.assertEqual(cap, 50.0)              # L4: min(100, 50)
            self.assertAlmostEqual(worst, 40.2)

    def test_guard_80_plan_clamp_100_iken_bile_red(self):
        with _clamped_relaxed(per=100, total=100):
            ok, why = auto_trader._validation_fill_guard(80.0, 0.0)
            self.assertFalse(ok)                     # 80×1.005 > 50
            self.assertIn("50.00", why)
            # 50 altı plan geçmeye devam eder (tavan kilitledi, kapı kapanmadı):
            ok2, _ = auto_trader._validation_fill_guard(30.0, 0.0)
            self.assertTrue(ok2)

    def test_dolum_denetimi_de_mutlak_tavanla_konusur(self):
        # GERÇEK 60 USDT dolum, bütçe-tavanı 100 okunsa bile İHLALDİR —
        # denetim sessiz geçmez (fail-closed kilit üretir).
        with _clamped_relaxed(per=100, total=100):
            why = auto_trader._validation_fill_exceeded("BTCUSDT", "60.0")
            self.assertIsNotNone(why)
            self.assertIn("60.00", why)


class _Risk80(_RiskOK):
    """RiskEngine 80 USDT onaylar (regresyon senaryosunun 80'lik planı)."""
    def approve_trade(self, balance, slippage, take_profit_pct=None,
                      equity=None, max_position_pct=None):
        return 80.0, "approved"


class TestAbsCapAtOpenCoin(LiveWiringBase):
    """Uçtan uca regresyon senaryosu: bütçe clamp'ı 100 + stage-max 200 +
    RiskEngine 80 → HEAD'de POST GİTTİ (kırmızı kanıt); L4 sonrası POST YOK,
    bütçe hakkı yanmaz."""

    def test_mutlak_tavan_postu_engeller(self):
        with _clamped_relaxed(per=100, total=100), \
                mock.patch.object(auto_trader, "MAX_NOTIONAL_USDT", 200.0):
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            res = auto_trader.open_coin(binance, hb, self._coin(),
                                        dry_run=False, risk=_Risk80(),
                                        reserver=None, coordinator=cc)
            self.assertFalse(res)
            self.assertEqual(hb.created, [])       # HEAD'de: [SYM] (POST gitti)
            self.assertFalse(auto_trader._PENDING_FILLS)
            st = auto_trader._VB_BUDGET.status()
            self.assertEqual(st["trades_used"], 0)  # hak yanmadı (guard öncesi)


if __name__ == "__main__":
    unittest.main()
