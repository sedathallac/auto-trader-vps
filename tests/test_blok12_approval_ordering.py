"""BLOK12 devam-E §3: onay token SIRALAMASI — doğrula ÖNCE, tüket SONRA.

İnceleme bulgusu: "İkinci onay token sıralaması → Düzeltme gerekli". Eski
sıralamada token create_executor POST'undan ÖNCE tüketiliyordu; teyitli emir
başarısızlığında bütçe hakkı iade edilse de token ÖLÜ kalıyordu — ikinci işlem
kalıcı olarak "ONAY-KULLANILDI" kilidine düşüyordu (operatör yeniden grant
vermeden bot ikinci işlemi bir daha hiç deneyemezdi).

Yeni sözleşme:
  GÖNDERİM ÖNCESİ  _operator_gate_check()  → entry_verify (OKUMA, mutasyonsuz;
                   izin denetimi dahil tüm kontroller; token dosyası değişmez)
  GÖNDERİM SONRASI _operator_gate_consume() → yalnız emrin kaderi "bu deneme
                   harcandı" ise tüketilir: executor_id VAR (CONSUMED doğrulandı)
                   VEYA UNKNOWN (kader belirsiz — tekrar gönderim yasağı).
  TEYİTLİ BAŞARISIZLIK (executor yok, outcome != UNKNOWN) → token HARCANMAZ;
                   bütçe iadesiyle birlikte ikinci işlem yeniden denenebilir.
Kalıntu riski dürüstçe: POST başarılı ↔ consume arasındaki ÇOK KÜÇÜK crash
penceresinde token tüketilmeden süreç ölebilir (yeniden kullanım). Sınırları:
§8 tek-süreç kilidi, bütçe 2 işlem hard-cap (3. her zaman RED), pending slot.

Kırmızı→yeşil: bugünkü kodda (a) verify token'ı harcıyor → ikinci gate çağrısı
ONAY-KULLANILDI; (b) teyitli başarısızlık sonrası retry kilitli. Ağ YOK.
"""
import contextlib
import inspect
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
import operator_approval as OA                          # noqa: E402
from test_inc13_live_wiring import (FakeBinance, FakeHB,  # noqa: E402
                                    LiveWiringBase, _RiskOK, _coordinator)

SIX = {k: True for k in OA.SIX_EVIDENCE}
_ENV_KEYS = ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
             "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
             "MAX_VALIDATION_TOTAL_NOTIONAL_USDT",
             "VALIDATION_APPROVAL_DB", "OPERATOR_APPROVAL_TOKEN_FILE")


class _SecondTradeBase(unittest.TestCase):
    """2. işleme hazırlanmış durum: bütçe 1/2 kullanıldı+kanıtlandı, grant var."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12e3-")
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        self._old_dir = auto_trader.STATE_DIR
        self._old_bl = auto_trader.BLACKLIST_FILE
        auto_trader.STATE_DIR = Path(self.tmp)
        auto_trader.BLACKLIST_FILE = Path(self.tmp) / ".bl.json"
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["MAX_VALIDATION_TRADES"] = "2"
        os.environ["MAX_VALIDATION_POSITION_NOTIONAL_USDT"] = "50"
        os.environ["MAX_VALIDATION_TOTAL_NOTIONAL_USDT"] = "100"
        os.environ["VALIDATION_APPROVAL_DB"] = os.path.join(self.tmp, "gate.db")
        os.environ.pop("OPERATOR_APPROVAL_TOKEN_FILE", None)
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None
        # 1. işlemi tamamla: reserve(seq1 gate serbest) + SIX kanıt + grant
        self.assertTrue(auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")[0])
        self.assertTrue(auto_trader._operator_gate_check()[0])
        self.g = OA.ApprovalGate(os.environ["VALIDATION_APPROVAL_DB"])
        self.assertTrue(self.g.record_completion(1, SIX, "op"))
        self.tok = self.g.grant("op")
        self.assertIsNotNone(self.tok)
        tp = os.path.join(self.tmp, "tok")
        with open(tp, "w") as fh:
            fh.write(self.tok)
        os.chmod(tp, 0o600)
        os.environ["OPERATOR_APPROVAL_TOKEN_FILE"] = tp

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None
        auto_trader.STATE_DIR = self._old_dir
        auto_trader.BLACKLIST_FILE = self._old_bl

    def _to_second(self):
        # open_coin'in reserve'ünü taklit et: trades_used → 2 (gate seq=2)
        self.assertTrue(auto_trader._validation_reserve("ETHUSDT", 35.0, "i2")[0])


class TestVerifyDoesNotConsume(_SecondTradeBase):
    def test_gate_cagrisi_token_harcamaz_tekrar_denebilir(self):
        self._to_second()
        ok, why = auto_trader._operator_gate_check()
        self.assertTrue(ok, why)
        self.assertEqual(self.g.state()["state"], "APPROVED_AWAITING_USE")
        # Bugünkü hata: ikinci çağrı "ONAY-KULLANILDI" veriyordu. Doğrulama
        # mutasyonsuz olmalı ki teyitli başarısızlıkta retry açık kalsın.
        ok2, why2 = auto_trader._operator_gate_check()
        self.assertTrue(ok2, f"retry kilitlendi: {why2}")

    def test_explicit_consume_tüketir_ve_tekildir(self):
        self._to_second()
        self.assertTrue(auto_trader._operator_gate_check()[0])
        ok, why = auto_trader._operator_gate_consume()
        self.assertTrue(ok, why)
        self.assertEqual(self.g.state()["state"], "APPROVAL_CONSUMED")
        ok2, why2 = auto_trader._operator_gate_consume()
        self.assertFalse(ok2)
        self.assertIn("KULLANILDI", why2)

    def test_birinci_islem_consume_noop(self):
        # seq=1 (trades_used==1, setUp'ın i1 reserve'i): token gerekmez;
        # consume çağırmak zararsız geçiştir (entry_consume 1. işlem serbest).
        ok, why = auto_trader._operator_gate_consume()
        self.assertTrue(ok, why)


class _FailHB(FakeHB):
    """Teyitli başarısızlık: executor YOK, outcome UNKNOWN değil."""
    def create_executor(self, *a, **k):
        return {"outcome": "NOT_CREATED", "error": "hb refused"}


class _UnknownHB(FakeHB):
    """Kader belirsiz: gönderilmiş olabilir — tekrar gönderim YASAK."""
    def create_executor(self, *a, **k):
        return {"outcome": "UNKNOWN", "error": "timeout"}


class TestOpenCoinOrdering(LiveWiringBase, _SecondTradeBase):
    """Gerçek open_coin akışında sıralama: teyitli başarısızlık token'ı
    YAKMAZ; UNKNOWN kader token'ı HARCAR."""

    def setUp(self):
        LiveWiringBase.setUp(self)
        _SecondTradeBase.setUp(self)

    def tearDown(self):
        _SecondTradeBase.tearDown(self)
        LiveWiringBase.tearDown(self)

    def _coin(self, symbol=None):
        c = LiveWiringBase._coin(self)
        if symbol:
            c["symbol"] = symbol
        return c

    def test_teysitli_basarisizlik_token_ve_butce_iade_retry_acik(self):
        hb_fail, hb_ok = _FailHB(), FakeHB()
        binance = FakeBinance()
        cc, _ = _coordinator(binance, hb_fail, self.db)
        res1 = auto_trader.open_coin(binance, hb_fail, self._coin(),
                                     dry_run=False, risk=_RiskOK(),
                                     reserver=None, coordinator=cc)
        self.assertFalse(res1)
        self.assertEqual(hb_fail.created, [])
        # Bütçe iade edildi mi? (trades_used 1'e dönmeli)
        st = auto_trader._VB_BUDGET.status()
        self.assertEqual(st["trades_used"], 1)
        # Token Hâlâ durmalı — bugünkü hata: APPROVAL_CONSUMED.
        self.assertEqual(self.g.state()["state"], "APPROVED_AWAITING_USE")
        # Retry: FARKLI sembol (ilki teyitli başarısızlık blacklist'ine yazıldı)
        cc2, _ = _coordinator(binance, hb_ok, self.db)
        res2 = auto_trader.open_coin(binance, hb_ok, self._coin("BTCUSDT"),
                                     dry_run=False, risk=_RiskOK(),
                                     reserver=None, coordinator=cc2)
        self.assertTrue(res2, "teyitli başarısızlık sonrası retry kilitli")
        self.assertEqual(hb_ok.created, ["BTCUSDT"])
        self.assertEqual(self.g.state()["state"], "APPROVAL_CONSUMED")

    def test_unknown_kader_token_harcar_retry_kapali(self):
        hb = _UnknownHB()
        binance = FakeBinance()
        cc, _ = _coordinator(binance, hb, self.db)
        res = auto_trader.open_coin(binance, hb, self._coin(),
                                    dry_run=False, risk=_RiskOK(),
                                    reserver=None, coordinator=cc)
        self.assertFalse(res)
        # UNKNOWN → deneme harcandı: token tüketilmiş olmalı (bugün de böyle;
        # bu test düzeltmenin bu güvenliği KORUDUĞUNU sabitler).
        self.assertEqual(self.g.state()["state"], "APPROVAL_CONSUMED")
        st = auto_trader._VB_BUDGET.status()
        self.assertEqual(st["trades_used"], 2)      # hak KORUNDU (UNKNOWN)
        # Retry asla ikinci emir üretmez.
        hb2 = FakeHB()
        res2 = auto_trader.open_coin(binance, hb2, self._coin("BTCUSDT"),
                                     dry_run=False, risk=_RiskOK(),
                                     reserver=None,
                                     coordinator=_coordinator(binance, hb2, self.db)[0])
        self.assertFalse(res2)
        self.assertEqual(hb2.created, [])


class TestSourceOrderingPin(unittest.TestCase):
    def test_consume_kaynagi_create_executor_sonrasinda(self):
        src = inspect.getsource(auto_trader.open_coin)
        self.assertIn("_operator_gate_consume(", src)
        self.assertLess(src.index("hb.create_executor("),
                        src.index("_operator_gate_consume("),
                        "token tüketimi create_executor ÖNCESİNE dönemeyiz")

    def test_gate_check_mutasyonsuz_entry_verify_kullanir(self):
        src = inspect.getsource(auto_trader._operator_gate_check)
        self.assertIn("entry_verify", src)
        self.assertNotIn("entry_consume", src)


if __name__ == "__main__":
    unittest.main()
