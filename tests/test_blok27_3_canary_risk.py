"""BLOK27.3 testleri — Canary RiskManager: operatör sınırlarının TAMAMI.

Görev §5'in istediği testler:
  - 0, 1, 2 ve 3. pozisyon sınırları
  - 50 USDT pozisyon notional sınırı
  - 100 USDT toplam notional sınırı
  - -5 USDT pozisyon zarar tetiklemesi (SAFE_EXIT, yeni risk yok)
  - -10 USDT toplam zarar HALT tetiklemesi (kalıcı)
  - gerçekleşmiş + gerçekleşmemiş PnL birleşimi
  - komisyon ve funding dahil zarar hesabı
  - restart sonrası risk sayacı korunumu
  - iki eşzamanlı emir denemesi (yarış)
  - eksik/stale Mark Price (fail-closed)
  - hesap yapılamama / belirsiz veri (fail-closed; API timeout katmanı
    shadow runner'da ayrıca ele alınır)
  - HALT'ta risk azaltan kapanışa İZİN (kural 9)
"""
import os
import tempfile
import threading
import time
import unittest

import canary_risk as cr
from canary_risk import (RiskManager, OrderRejected, MAX_OPEN_POSITIONS,
                         MAX_POSITION_NOTIONAL, MAX_TOTAL_NOTIONAL,
                         MAX_POSITION_NET_LOSS, MAX_CANARY_NET_LOSS,
                         STATUS_HALTED, STATUS_ACTIVE, MARK_STALE_S)

T0 = 1795500000000        # 2026-11-24T06:40Z civarı sabit zaman
SYM, OTHER, THIRD = "BTCUSDT", "ETHUSDT", "SOLUSDT"


def _rm():
    return RiskManager(os.path.join(tempfile.mkdtemp(), "canary.db"))


def _fresh(rm, symbols=(SYM, OTHER, THIRD), px=100.0, ts=T0):
    """Taze mark yükle (stale yok)."""
    for s in symbols:
        rm.update_mark(s, px, ts)


class TestPositionCountLimits(unittest.TestCase):
    def test_zero_one_two_allowed_third_rejected(self):
        """0 → izin; 1 → izin; 2 → izin; 3. pozisyon KESİNLİKLE reddedilir."""
        rm = _rm()
        _fresh(rm)
        for i, s in enumerate((SYM, OTHER)):
            rm.check_order(s, 1, 40.0, now_ms=T0)
            pid = rm.open_position(s, 1, 40.0, 100.0, T0)
            self.assertEqual(len(rm.open_positions()), i + 1)
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(THIRD, 1, 10.0, now_ms=T0)
        self.assertIn("pozisyon sınırı", str(cm.exception))
        self.assertIn("üçüncü", str(cm.exception))
        self.assertEqual(len(rm.open_positions()), 2)

    def test_two_positions_after_one_closes(self):
        """Kapanış sonrası slot tekrar kullanılabilir (risk azaltan serbest)."""
        rm = _rm()
        _fresh(rm, ts=T0 + 2000)
        pid = rm.open_position(SYM, 1, 40.0, 100.0, T0)
        rm.close_position(pid, 101.0, T0 + 1000)      # kâr kapat
        rm.submit_order(OTHER, -1, 40.0, 100.0, now_ms=T0 + 2000)  # izin
        self.assertEqual(len(rm.open_positions()), 1)


class TestNotionalLimits(unittest.TestCase):
    def test_position_notional_50_usdt(self):
        """50.00 izin; 50.01 reddedilir (notional, marjin değil)."""
        rm = _rm()
        _fresh(rm)
        r = rm.check_order(SYM, 1, 50.0, now_ms=T0)
        self.assertTrue(r["allowed"])
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(SYM, 1, 50.01, now_ms=T0)
        self.assertIn("pozisyon notional", str(cm.exception))

    def test_total_notional_100_usdt_current_price(self):
        """2×50 = 100 izin; fiyat yükselince güncel notional 100'ü aşarsa
        yeni emir reddedilir (görev: notional güncel fiyatla)."""
        rm = _rm()
        _fresh(rm)
        rm.open_position(SYM, 1, 50.0, 100.0, T0)     # 0.5 birim
        rm.open_position(OTHER, 1, 50.0, 100.0, T0)
        rm.close_position  # noqa — yer tutucu değil; alt satırda gerçek kapanış
        # fiyat %2 arttı → toplam notional 102 > 100
        _fresh(rm, px=102.0, ts=T0 + 60_000)
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(SYM, 1, 5.0, now_ms=T0 + 60_000)
        self.assertIn("toplam notional", str(cm.exception))

    def test_leverage_cannot_bypass_notional(self):
        """Kaldıraç 50 USDT notional sınırını AŞAMAZ: kontrol notional
        üzerinden yapılır (marjin kavramı yok)."""
        rm = _rm()
        _fresh(rm)
        with self.assertRaises(OrderRejected):
            rm.check_order(SYM, 1, 500.0, now_ms=T0)  # 10x marjin = 50 olsa da


class TestLossLimits(unittest.TestCase):
    def test_position_loss_5_triggers_safe_exit_no_new_risk(self):
        """-5 pozisyon zararı: o pozisyon için yeni risk YOK (SAFE_EXIT),
        kapanış (risk azaltan) serbest; diğer sembole yeni emir hâlâ izinli."""
        rm = _rm()
        _fresh(rm)
        pid = rm.open_position(SYM, 1, 50.0, 100.0, T0)   # long 0.5 br
        rm.note_funding(pid, T0 + 1000, -0.10)            # 0.10 ödendi
        # mark 89.8 → uPnL = -5.1; + funding 0.10 → net -5.2 ≤ -5
        rm.update_mark(SYM, 89.8, T0 + 2000)
        actions = rm.enforce(now_ms=T0 + 2000)
        self.assertIn({"pos_id": pid, "action": "SAFE_EXIT"}, actions)
        # aynı sembole yeni risk: RED
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(SYM, 1, 10.0, now_ms=T0 + 2000)
        self.assertIn("yeni risk YOK", str(cm.exception))
        # risk AZALTAN kapanış HALT değil, çalışır (kural 9)
        realized = rm.close_position(pid, 89.8, T0 + 3000)
        self.assertAlmostEqual(realized, -5.1, places=6)
        # başka sembol, toplam zarar -5.2 < 10 → hâlâ izin
        r = rm.check_order(OTHER, -1, 30.0, now_ms=T0 + 4000)
        self.assertTrue(r["allowed"])

    def test_total_loss_10_halts_persistently(self):
        """-10 toplam net zarar → CANARY_RISK_HALTED; otomatik sıfırlama YOK."""
        rm = _rm()
        _fresh(rm)
        # 2 pozisyon, her biri -5.2 → toplam -10.4
        for s in (SYM, OTHER):
            pid = rm.open_position(s, 1, 50.0, 100.0, T0)
            rm.note_funding(pid, T0 + 1000, -0.10)
            rm.update_mark(s, 89.8, T0 + 2000)
        rm.enforce(now_ms=T0 + 2000)
        self.assertEqual(rm.status, STATUS_HALTED)
        # HALT'ta yeni emir: RED
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(THIRD, 1, 10.0, now_ms=T0 + 2000)
        self.assertIn("CANARY_RISK_HALTED", str(cm.exception))
        # risk azaltan kapanış HALT'ta DAHA çalışır (kural 9)
        pid = rm.open_positions()[0]["pos_id"]
        rm.close_position(pid, 89.8, T0 + 3000)
        # toplam düzeltilse bile HALT sürer (kural 6: otomatik sıfırlama yok)
        self.assertEqual(rm.status, STATUS_HALTED)

    def test_operator_reset_only_via_explicit_call(self):
        rm = _rm()
        _fresh(rm)
        for s in (SYM, OTHER):
            pid = rm.open_position(s, 1, 50.0, 100.0, T0)
            rm.update_mark(s, 89.8, T0 + 2000)
        rm.enforce(now_ms=T0 + 2000)
        self.assertEqual(rm.status, STATUS_HALTED)
        rm.operator_reset()                      # yalnız açık operatör eylemi
        self.assertEqual(rm.status, STATUS_ACTIVE)


class TestLossComputation(unittest.TestCase):
    def test_realized_plus_unrealized_combined(self):
        """Kapalı realized + açık uPnL birlikte toplam zarara girer."""
        rm = _rm()
        _fresh(rm)
        p1 = rm.open_position(SYM, 1, 50.0, 100.0, T0)
        rm.close_position(p1, 98.0, T0 + 1000)          # realized -1.0
        rm.open_position(OTHER, -1, 40.0, 100.0, T0 + 2000)  # short 0.4
        rm.update_mark(OTHER, 103.0, T0 + 3000)         # uPnL -1.2
        total = rm.total_net_loss(T0 + 3000)
        self.assertAlmostEqual(total, 2.2, places=6)

    def test_commission_and_funding_included(self):
        """Açılış/kapanış komisyonu + funding ödemesi net zarara dahil."""
        rm = _rm()
        _fresh(rm)
        # açık pozisyon: uPnL 0, komisyon 0.05+0.05, funding -0.20 (ödendi)
        pid = rm.open_position(SYM, 1, 50.0, 100.0, T0, entry_comm=0.05)
        rm.note_funding(pid, T0 + 1000, -0.20)
        rm.update_mark(SYM, 100.0, T0 + 2000)
        self.assertAlmostEqual(rm.position_net_loss(rm.open_positions()[0],
                                                    T0 + 2000), 0.25)
        # kapanınca exit komisyonu da eklenir
        rm.close_position(pid, 100.0, T0 + 3000, exit_comm=0.05)
        pos = rm._pos(pid)
        self.assertAlmostEqual(rm.position_net_loss(pos, T0 + 4000), 0.30)


class TestPersistence(unittest.TestCase):
    def test_restart_preserves_counters_and_halt(self):
        """Restart sayaçları SIFIRLAYAMAZ (kural 7): pozisyonlar, zarar,
        HALT durumu yeni RiskManager ile aynı DB'den aynen gelir."""
        d = tempfile.mkdtemp()
        f = os.path.join(d, "canary.db")
        rm = RiskManager(f)
        _fresh(rm)
        pid = rm.open_position(SYM, 1, 50.0, 100.0, T0)
        rm.update_mark(SYM, 89.8, T0 + 2000)
        rm.enforce(now_ms=T0 + 2000)
        halted = rm.status
        rm2 = RiskManager(f)                      # "restart"
        self.assertEqual(rm2.status, halted)
        self.assertEqual(len(rm2.open_positions()), 1)
        # mark yaşı korunur: T0+2000'den T0+2000'de hâlâ hesaplanır
        l = rm2.position_net_loss(rm2.open_positions()[0], T0 + 2000)
        self.assertAlmostEqual(l, 5.1, places=6)


class TestConcurrency(unittest.TestCase):
    def test_two_simultaneous_orders_cannot_open_third(self):
        """İki eşzamanlı submit yarışı: 3. pozisyon doğamaz (atomik
        check+yazım tek kilitle)."""
        rm = _rm()
        _fresh(rm)
        rm.open_position(SYM, 1, 40.0, 100.0, T0)
        accepted = []
        barrier = threading.Barrier(2)

        def attempt(symbol):
            barrier.wait()
            try:
                rm.submit_order(symbol, 1, 40.0, 100.0, now_ms=T0)
                accepted.append(symbol)
            except OrderRejected:
                pass

        t1 = threading.Thread(target=attempt, args=(OTHER,))
        t2 = threading.Thread(target=attempt, args=(THIRD,))
        t1.start(); t2.start(); t1.join(); t2.join()
        # 1 açık + 2 eşzamanlı deneme: TAM OLARAK BİRİ geçer → 2 pozisyon
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rm.open_positions()), 2)

    def test_simultaneous_orders_cannot_exceed_total_notional(self):
        """İki eşzamanlı submit toplam notional sınırını aşamaz.
        (Fixture: 55 USDT'lik mevcut pozisyon doğrudan yazıldı — kapı
        bypass fixture'ı, yarış matematikini zorlamak için.)"""
        rm = _rm()
        _fresh(rm)
        rm.open_position(SYM, 1, 55.0, 100.0, T0)
        accepted = []
        barrier = threading.Barrier(2)

        def attempt(symbol):
            barrier.wait()
            try:
                rm.submit_order(symbol, 1, 25.0, 100.0, now_ms=T0)
                accepted.append(symbol)
            except OrderRejected:
                pass

        t1 = threading.Thread(target=attempt, args=(OTHER,))
        t2 = threading.Thread(target=attempt, args=(THIRD,))
        t1.start(); t2.start(); t1.join(); t2.join()
        # 55 + 25 = 80 ≤ 100 (birincisi geçer); ikincisi 80+25=105 > 100 RET
        self.assertEqual(len(accepted), 1)
        total = rm.total_notional(T0)
        self.assertLessEqual(total, 100.0 + 1e-9)


class TestFailClosed(unittest.TestCase):
    def test_missing_mark_rejects_order(self):
        """Mark hiç yoksa → emir YOK."""
        rm = _rm()
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(SYM, 1, 10.0, now_ms=T0)
        self.assertIn("FAIL_CLOSED", str(cm.exception))

    def test_stale_mark_rejects_order(self):
        """Mark stale (>30s) → emir YOK; taze mark → izin."""
        rm = _rm()
        rm.update_mark(SYM, 100.0, T0)
        with self.assertRaises(OrderRejected) as cm:
            rm.check_order(SYM, 1, 10.0, now_ms=T0 + int(MARK_STALE_S * 1000) + 1)
        self.assertIn("FAIL_CLOSED", str(cm.exception))
        rm.update_mark(SYM, 100.0, T0 + int(MARK_STALE_S * 1000) + 2000)
        r = rm.check_order(SYM, 1, 10.0, now_ms=T0 + int(MARK_STALE_S * 1000) + 2000)
        self.assertTrue(r["allowed"])

    def test_unknown_loss_state_rejects(self):
        """Bir pozisyonun mark'ı yoksa toplam zarar hesaplanamaz →
        belirsiz durumda emir YOK (API timeout/veri eksikliği katmanı)."""
        rm = _rm()
        rm.update_mark(SYM, 100.0, T0)      # YALNIZ SYM mark'ı var
        rm.open_position(SYM, 1, 50.0, 100.0, T0)
        # OTHER sembolünün mark'ı hiç gelmedi → o sembole emir ret
        with self.assertRaises(OrderRejected):
            rm.check_order(OTHER, 1, 10.0, now_ms=T0)
        # SYM pozisyonunun mark'ı bayatladı → toplam zarar hesaplanamaz → ret
        with self.assertRaises(OrderRejected):
            rm.check_order(SYM, 1, 10.0, now_ms=T0 + 60_000)

    def test_enforce_reports_fail_closed_no_data(self):
        rm = _rm()
        rm.open_position(SYM, 1, 50.0, 100.0, T0)   # mark YOK
        actions = rm.enforce(now_ms=T0 + 1000)
        self.assertIn({"pos_id": 1, "action": "FAIL_CLOSED_NO_DATA"}, actions)
        self.assertEqual(rm.status, STATUS_ACTIVE)  # halt değil; veri bekler


class TestReconcile(unittest.TestCase):
    def test_reconcile_detects_drift(self):
        rm = _rm()
        _fresh(rm)
        rm.open_position(SYM, 1, 50.0, 100.0, T0)
        r = rm.reconcile([{"symbol": SYM, "positionAmt": "0.5"},
                          {"symbol": OTHER, "positionAmt": "-1.0"}])
        self.assertFalse(r["ok"])
        kinds = {d["kind"] for d in r["drift"]}
        self.assertIn("UNOWNED_POSITION", kinds)
        r2 = rm.reconcile([{"symbol": SYM, "positionAmt": "0.5"}])
        self.assertTrue(r2["ok"])


if __name__ == "__main__":
    unittest.main()
