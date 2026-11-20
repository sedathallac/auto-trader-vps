"""BLOK10 §5 (F-B) — CloseIntentStore.reconcile() üretimde HİÇ çağrılmıyordu (P1).

Doğrulanan kök neden:
  * close_intent.py docstring'iUNKNOWN/crash sonrası kurtarmayı reconcile()'a
    havale eder ("reconcile() resolves it against positionRisk later"),
  * ancak grep: reconcile yalnız tests/ içinde çağrılıyor; üretim akışında
    tek alıcı yok,
  * close_coordinator.py:165-169 exchange-flat dalı intend'e DOKUNMADAN
    ALREADY_FLAT döner → sahibi çökmüş ACTIVE (PENDING/SENT) satır sonsuza
    dek active kalır; aynı sembolde YENİ pozisyon yeni token ile acquire()
    → STALE → FAILED_SAFE critical → O sembolün her türlü kapatma yolu
    (TP/süre/manual/acil) kalıcı kilitlenir. Tek kurtarma elle DB ameliyatı.

Onarım (minimal, docstring sözleşmesine uygun): exchange-flat branch —
reconcile()'ın istediği TEK güvenilir kanıt (positionRisk reliably flat).
Flat görüldüğünde intent reconcile(exchange_flat=True) ile CLOSED terminal
yapılır; DB hatası → FAILED_SAFE (fail-closed). Flat-olmayan STALE bilinçli
olarak fail-closed KALIR (CRITICAL + operatör müdahalesi; otomatik DB
değişikliği yok — BLOK10 §0).

Testler CloseCoordinator'ın enjekte-borsa arayüzünü birebir uygular;
store/koordinatör/adapter mantığı SAHİCİDİR.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from close_coordinator import CloseCoordinator, CloseState
from close_intent import CloseIntentStore

SYM = "SOLUSDT"


class FakeEx:
    """CloseCoordinator docstring'indeki enjekte arayüzün asgari gerçeği."""

    def __init__(self, amt="0.3", token="LONG:83.08", close_fail=None,
                 ghost_fill=False):
        self.amt = Decimal(str(amt))
        self.token = token
        self.close_fail = close_fail        # Exception → POST istisnası
        self.ghost_fill = ghost_fill        # POST ulaştı, cevap kayboldu
        self.close_calls = []
        self.stop_calls = []

    def position_amt(self, symbol):
        return self.amt

    def position_mode(self):
        return "ONEWAY"

    def protective_orders(self, symbol):
        return []

    def order_terminal(self, order):
        return True

    def cancel_order(self, order):
        return True

    def stop_executor(self, symbol):
        self.stop_calls.append(symbol)

    def close_reduce_only(self, symbol, qty, position_side=None):
        self.close_calls.append(qty)
        if self.close_fail is not None:
            if self.ghost_fill:
                self.amt = Decimal(0)       # emir exchange'e ULAŞTI ve doldu
            raise self.close_fail
        self.amt = Decimal(0)
        return qty

    def position_token(self, symbol):
        return self.token


class CloseIntentRecoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "risk_ledger.db")
        self.store = CloseIntentStore(self.db)
        self.store.init_schema()


class TestCloseIntentRecovery(CloseIntentRecoveryBase):
    # 1) Flat + sahibi çökmüş ACTIVE intent → CLOSED terminal (yeni dönem açılır).
    def test_flat_closes_stale_active_intent(self):
        self.store.acquire(SYM, "LONG:70.0")          # çöken sahibin PENDING'i
        ex = FakeEx(amt="0")
        cc = CloseCoordinator(ex, intent_store=self.store)
        r = cc.request_close(SYM)
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertEqual(self.store.get(SYM)["state"], "CLOSED",
                         "flat exchange'e rağmen ACTIVE intent kaldı — "
                         "sonraki dönem sonsuza dek STALE kalırdı")

    # 2) UNKNOWN gönderim → (ghost fill) → tekrar çağrıda flat kanıtı ile
    #    CLOSED; ARDINDAN yeni token dönemi KESİNTİSİZ kapanabilir.
    def test_unknown_send_then_new_epoch_recovers(self):
        ex = FakeEx(amt="0.3", close_fail=RuntimeError("timeout"),
                    ghost_fill=True)
        cc1 = CloseCoordinator(ex, intent_store=self.store)
        r1 = cc1.request_close(SYM)
        self.assertEqual(r1.state, CloseState.FAILED_SAFE)
        self.assertEqual(self.store.get(SYM)["state"], "PENDING")  # iade YOK

        # Kriz çözüldü: exchange artık flat (ghost fill). Tekrar kapatma çağrısı.
        cc2 = CloseCoordinator(ex, intent_store=self.store)
        r2 = cc2.request_close(SYM)
        self.assertEqual(r2.action, "ALREADY_FLAT")
        self.assertEqual(self.store.get(SYM)["state"], "CLOSED")

        # Yeni pozisyon dönemi (yeni token) — kapanış AKABİLİR (önce STALE idi).
        ex.amt = Decimal("0.3")
        ex.token = "LONG:90.0"
        ex.close_fail = None
        cc3 = CloseCoordinator(ex, intent_store=self.store)
        r3 = cc3.request_close(SYM)
        self.assertTrue(r3.ok)
        self.assertEqual(r3.action, "SENT_REDUCE_ONLY")
        self.assertEqual(len(ex.close_calls), 2)

    # 3) BUSY (gerçek uçuşta kapanış) davranışı DEĞİŞMEZ: ikinci süreç POST
    #    göndermez — regresyon çapası.
    def test_busy_inflight_still_never_double_posts(self):
        self.store.acquire(SYM, "LONG:83.08")         # 1. sürecin uçuşta sahipliği
        ex = FakeEx(amt="0.3")
        cc2 = CloseCoordinator(ex, intent_store=self.store)
        r = cc2.request_close(SYM)
        self.assertIs(r.ok, False)
        self.assertEqual(r.action, "IGNORED_IN_PROGRESS")
        self.assertEqual(ex.close_calls, [])          # ikinci POST YOK

    # 4) Flat-olmayan STALE: bilinçli fail-closed KALIR (CRITICAL, elle çözüm).
    def test_nonflat_stale_stays_fail_closed(self):
        self.store.acquire(SYM, "LONG:70.0")
        ex = FakeEx(amt="0.3", token="LONG:83.08")    # farklı dönem, hâlâ açık
        cc = CloseCoordinator(ex, intent_store=self.store)
        r = cc.request_close(SYM)
        self.assertEqual(r.state, CloseState.FAILED_SAFE)
        self.assertEqual(r.action, "INTENT_STALE")
        self.assertTrue(r.critical)
        self.assertEqual(ex.close_calls, [])          # kör POST yok

    # 5) intent_store=None (tek süreç) akışı birebir aynı.
    def test_single_process_mode_unchanged(self):
        ex = FakeEx(amt="0")
        cc = CloseCoordinator(ex)                     # intent yok
        r = cc.request_close(SYM)
        self.assertEqual(r.action, "ALREADY_FLAT")
        self.assertTrue(r.ok)

    # 6) ACQUIRED → POST → flat doğrulaması: mark_closed terminal (mevcut
    #    davranışın çapası; reconcile bunu bozmamalı).
    def test_happy_path_marks_closed_terminal(self):
        ex = FakeEx(amt="0.3")
        cc = CloseCoordinator(ex, intent_store=self.store)
        r = cc.request_close(SYM)
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "SENT_REDUCE_ONLY")
        self.assertEqual(self.store.get(SYM)["state"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
