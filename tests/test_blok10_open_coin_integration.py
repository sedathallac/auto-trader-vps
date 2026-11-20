"""BLOK10 §4 — Kontrollü canlı doğrulama bütçesinin GERÇEK open_coin() entegrasyonu.

Bu dosya §3 bağlantısının (validation_budget → auto_trader._validation_reserve)
ÜRETİM giriş zincirine gerçekten takılı olduğunu kanıtlar: sahte Binance/HB
sınırı dışında hiçbir bileşen taklit DEĞİLDİR (open_coin, merkezi risk kapısı,
RiskEngine-onayı, pending-store, create_executor akışı gerçektir).

test_inc13_live_wiring.LiveWiringBase tüm piyasa kapılarını stub'layarak
open_coin'i create_executor'a kadar getirir; biz üstüne bütçe bayrağını açık
tutup şunları doğrularız (görev mektubu §4 ile eşleşir):
  * işlem-başı tavanı aşan notional → create_executor HİÇ çağrılmaz (§11)
  * ikinci işlem sonrası üçüncü reddedilir (§10)
  * bayrak KAPALI iken sınır uygulaması devreye girmez, akış değişmez (§3)
  * HALTED motor → giriş yok; bütçe sayacı dokunulmadan kalır (§12)
  * executor oluştu → hak CONSUMED; acil kapanış hakki iade ETMEZ (S5/S6)

unittest tabanlı olduğundan pytest fixture KULLANILMAZ; bağlam bir
context-manager olarak her testin içinde kurulur (env + modül globali + STATE_DIR).
"""
import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_inc13_live_wiring import FakeBinance, FakeHB, LiveWiringBase, _RiskOK, _coordinator

_ENV_KEYS = ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
             "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
             "MAX_VALIDATION_TOTAL_NOTIONAL_USDT")


class TestControlledBudgetIntegration(LiveWiringBase):
    @contextlib.contextmanager
    def _vb(self, enabled=True, trades="2", per="50", total="100"):
        old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        old_dir = auto_trader.STATE_DIR
        saved = (auto_trader._VB_CFG, auto_trader._VB_BUDGET)
        # KARALİSTE YALITIMI: executor-başarısızlığı yolları add_blacklist çağırır;
        # BLACKLIST_FILE oturum-önlüklü STATE_DIR'de yaşar ve sıralı testlere SIZARDI
        # (bunu tam-paket koşusunda test_inc13 üzerine yazarak kanıtladık).
        old_bl = auto_trader.BLACKLIST_FILE
        auto_trader.BLACKLIST_FILE = Path(self.tmp) / ".test_blacklist.json"
        auto_trader.STATE_DIR = Path(self.tmp)
        if enabled:
            os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
            os.environ["MAX_VALIDATION_TRADES"] = trades
            os.environ["MAX_VALIDATION_POSITION_NOTIONAL_USDT"] = per
            os.environ["MAX_VALIDATION_TOTAL_NOTIONAL_USDT"] = total
        else:
            os.environ["CONTROLLED_LIVE_VALIDATION"] = "false"
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None
        try:
            yield
        finally:
            auto_trader._VB_CFG, auto_trader._VB_BUDGET = saved
            auto_trader.STATE_DIR = old_dir
            auto_trader.BLACKLIST_FILE = old_bl
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _open(self, binance, hb, cc, risk=None):
        return auto_trader.open_coin(binance, hb, self._coin(), dry_run=False,
                                     risk=risk or _RiskOK(), reserver=None,
                                     coordinator=cc)

    # §11 — işlem-başı tavanı (40 USDT): ~48 USDT'lik gerçek final notional
    #       reddedilir ve create_executor HİÇ çağrılmaz (borsaya emir gitmez).
    def test_per_trade_cap_blocks_before_submit(self):
        with self._vb(per="40"):
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            res = self._open(binance, hb, cc)
            self.assertIs(res, False)
            self.assertEqual(hb.created, [])          # POST YOK
            st = auto_trader._VB_BUDGET.status()
            self.assertEqual(st["trades_used"], 0)    # ret hak tüketmedi

    # §10 — üçüncü işlem reddi: ilk iki executor oluşur, üçüncüsü gelmez.
    def test_third_trade_rejected(self):
        with self._vb():
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), True)
            self.assertIs(self._open(binance, hb, cc), True)
            self.assertEqual(len(hb.created), 2)
            self.assertIs(self._open(binance, hb, cc), False)
            self.assertEqual(len(hb.created), 2)
            self.assertEqual(auto_trader._VB_BUDGET.status()["trades_used"], 2)

    # §3 (BLOK10) → BLOK11 §10.3 ÇEVRİMİ: ESKİ varsayım ("bayrak KAPALI iken
    # normal akış birebir devam eder") misyonla GEÇERSİZ kılındı: "bütçe kontrolü
    # kapalıysa hiçbir işlem açılmasın". Kapalı artık canlı giriş için RED'dir;
    # bütçe deposu yine HİÇ oluşturulmaz (red DB yazmaz). dry_run/compare bundan
    # etkilenmez — open_coin "[DRY] emir yok" noktasında bu kapıya varmadan döner.
    def test_mode_off_denies_live_entry(self):
        with self._vb(enabled=False):
            import validation_budget as vb
            self.assertFalse(vb.Config.from_env().enabled)
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), False)
            self.assertEqual(hb.created, [])           # create_executor hiç
            self.assertIsNone(auto_trader._VB_BUDGET)  # depo hiç kurulmadı
            self.assertFalse((Path(self.tmp) / "validation_budget.db").exists())

    # §12 — HALTED: merkezi risk kapısı bütçeden ÖNCE bloklar; sayaç dokunmaz.
    def test_halted_blocks_before_budget(self):
        with self._vb():
            class _Halted(_RiskOK):
                is_halted = True
                halt_reason = "test_halt"

            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            res = self._open(binance, hb, cc, risk=_Halted())
            self.assertIs(res, False)
            self.assertEqual(hb.created, [])
            if auto_trader._VB_BUDGET is not None:
                self.assertEqual(auto_trader._VB_BUDGET.status()["trades_used"], 0)

    # Bayrak açık + limit GEÇERSİZ → her giriş reddedilir (fail-closed, §3).
    def test_invalid_limits_fail_closed(self):
        with self._vb(per="51"):                       # sert tavan 50 aşılamaz
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), False)
            self.assertEqual(hb.created, [])

    # S5/S6 — executor oluştu → CONSUMED. SL doğrulanamaz → acil kapanış
    #         pozisyonu kapatır ama DOĞRULAMA HAKKI iade edilmez (konservatif).
    def test_emergency_close_keeps_consumed(self):
        with self._vb():
            binance = FakeBinance(sl_result={"code": -2021})
            hb = FakeHB()
            cc, store = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), True)
            self.assertEqual(auto_trader._VB_BUDGET.status()["trades_used"], 1)
            out = self._drive_fill(binance, hb, cc)
            self.assertEqual(out["failed"], 1)         # SL'sizlik → acil kapanış
            self.assertEqual(auto_trader._VB_BUDGET.status()["trades_used"], 1)

    # ACK-belirsiz (UNKNOWN) create_executor sonucu → hak İADE EDİLMEZ (S5).
    def test_unknown_outcome_keeps_budget(self):
        with self._vb():
            binance, hb = FakeBinance(), FakeHB()

            class _UnknownHB(FakeHB):
                def create_executor(self, *a, **k):
                    self.created.append("GHOST")       # çağrıldı ama sonuç belirsiz
                    return {"error": "timeout", "outcome": "UNKNOWN"}

            ucb = _UnknownHB()
            cc, _ = _coordinator(binance, ucb, self.db)
            res = self._open(binance, ucb, cc)
            # eid yok → open_coin False döner; ÖNEMLİ olan: UNKNOWN yolu
            # bütçe iadesine DOKUNMAZ (release yalnız teyitli başarısızlıkta).
            self.assertIs(res, False)
            st = auto_trader._VB_BUDGET.status()
            self.assertEqual(st["trades_used"], 1)     # iade YOK — rezerv korundu
