"""BLOK28.1 §11 — TOPLAM HALT TESTLERİ (mock/fixture; ağ çağrısı YOK).

-10 USDT toplam zarar eşiği (§3): eşik altı / -10.00 sınırı, pozisyonun
çıkışı, UNKNOWN emir varken HALT'in DEVAM etmesi, kısmi kalan, restart
korunumu, operatör onayı olmadan giriş yasağı, yalnız C281-sahipli giriş
emirlerinin iptali, UNKNOWN girişin geç fill'inin deftere işlenip çıkışa
alınması.

State machine (§3): CANARY_ACTIVE → CANARY_HALT_INTENT → CANARY_RISK_HALTED
→ CANARY_EXITING_ALL → CANARY_EXIT_RECONCILIATION → CANARY_FLAT_VERIFIED →
CANARY_HALT_LOCKED (yalnız operatör).

Matematik (BLOK31): tek ETH pozisyonu qty 0.99 @100 (ENTRY_SIZE_BUFFER'lı
hedef 99), komisyon 0.0495/yan. İkinci tam-boyut giriş toplam kapıdan
reddedildiğinden eşik testleri TEK pozisyonla yapılır; eşik markları
loss_mark() ile pozisyonun gerçek değerlerinden türetilir:
  -4.99 → eşik altı (çıkış YOK, HALT YOK)
  -10.00 → toplam -10 → HALT
  mark 80.00 → gap: ~-19.85 (limitler TETİK, cap değil)
Stop ~95.05'te olduğundan eşik testlerinde koruma dışarıdan iptal edilir
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, loss_mark, T0)

BAR = 4 * 3600 * 1000
from canary_live import (HALT_ACTIVE, HALT_INTENT, HALT_RISK, HALT_EXITING_ALL,
                         HALT_RECON, HALT_FLAT_VERIFIED, HALT_LOCKED)
from canary_risk import STATUS_HALTED, STATUS_ACTIVE


def cancel_protection(ex, cid):
    ex.algos[cid]["status"] = "CANCELED"


class TotalHaltTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)

    def tearDown(self):
        self.tmp.cleanup()

    def _open_one(self):
        """ETH: 100 USDT notional (BLOK29.1), FILLED + PROTECTED."""
        r1 = open_one(self.lc, "ETHUSDT", 1)
        self.assertEqual(r1["result"], "FILLED")
        return r1

    def _drive_halt_chain(self, max_ticks=12):
        """risk_tick'lerle LOCKED'a kadar sürükle; durumu döndür."""
        for _ in range(max_ticks):
            self.clock.advance(10_000)
            self.lc.risk_tick()
            if self.lc.halt_state == HALT_LOCKED:
                return HALT_LOCKED
        return self.lc.halt_state

    def _halt_transitions(self):
        rows = self.lc.rm.db.execute(
            "SELECT detail FROM risk_events WHERE kind='HALT_TRANSITION'"
        ).fetchall()
        return [r[0] for r in rows]

    # ---- §11: eşik altı / -10.00 eşiği ---------------------------------------

    def test_below_threshold_no_exit_no_halt(self):
        """Tek pozisyon: -4.99 → -5 çıkış tetiği YOK, -10 toplam HALT YOK."""
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        # pozisyon -4.99 (eşik altı)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 4.99))
        cyc = self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_ACTIVE)
        self.assertEqual(self.lc.halt_state, HALT_ACTIVE)
        self.assertEqual(cyc["risk_actions"], [])
        for p in self.lc.rm.open_positions():
            self.assertEqual(p["status"], "OPEN")

    def test_total_minus_10_00_halts_and_exits_all_positions(self):
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertNotEqual(self.lc.halt_state, HALT_ACTIVE)
        # zincir LOCKED'a kadar ilerler
        state = self._drive_halt_chain()
        self.assertEqual(state, HALT_LOCKED)
        # pozisyon CLOSED + borsada flat (positionRisk ile)
        pos = self.lc.rm._pos(r1["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(self.lc._live_pos_row(r1["pos_id"])
                         ["exit_status"], "EXIT_CONFIRMED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        # state machine sırası
        joined = " ".join(self._halt_transitions())
        for st in (HALT_INTENT, HALT_RISK, HALT_EXITING_ALL, HALT_RECON,
                   HALT_FLAT_VERIFIED, HALT_LOCKED):
            self.assertIn(st, joined)

    def test_gap_slippage_can_exceed_minus_10_limits_are_triggers(self):
        """§0: limitler TETİK, cap değil. Gap'te stop 95.10'da ama mark
        80'e düşerse SAFE_EXIT market 80'den dolar → toplam -10'u aşabilir."""
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(60_000)
        self.ex.set_mark("ETHUSDT", 80.0)      # gap: -20.05 tek başına
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        total = self.lc.rm.total_net_loss(self.clock())
        self.assertIsNotNone(total)
        self.assertGreater(total, 10.0)         # cap DEĞİL — raporlanır gerçek

    # ---- §11: UNKNOWN varken HALT devam eder ----------------------------------

    def test_halt_continues_while_exit_unknown(self):
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.ex.no_apply_timeouts.add("C281X1n1")   # ETH çıkışı UNKNOWN
        self.lc.risk_tick()                          # HALT + çıkış UNKNOWN
        # UNKNOWN hâlâ çözülmedi: LOCKED'a GEÇİLEMEZ, ETH borsada AÇIK
        self.assertNotEqual(self.lc.halt_state, HALT_LOCKED)
        self.assertNotEqual(self.lc.halt_state, HALT_FLAT_VERIFIED)
        self.assertAlmostEqual(self.ex.positions["ETHUSDT"]["positionAmt"],
                               self.lc.rm._pos(r1["pos_id"])["units"],
                               places=9)
        # çözüm zinciri: sorgu (-2011) → doğrulanmış kalan → YENİ intent
        # (körlemesine aynı emri tekrar DEĞİL) → kapanır → LOCKED
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        self.assertEqual(self.lc.rm._pos(r1["pos_id"])["status"], "CLOSED")
        eth_exits = [c["client_order_id"] for c in self.ex.post_calls
                     if c["method"] == "new_order" and c["reduce_only"]
                     and c["symbol"] == "ETHUSDT"]
        self.assertEqual(eth_exits, ["C281X1n1", "C281X1n2"])

    def test_halt_partial_exit_remainder_before_locked(self):
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.ex.partial_fill_qty["C281X1n1"] = 0.2    # ETH çıkışı kısmi
        self.lc.risk_tick()
        # kalan (units−0.2) kapanmadan LOCKED YOK
        for _ in range(3):
            self.clock.advance(10_000)
            self.lc.risk_tick()
            self.assertNotEqual(self.lc.halt_state, HALT_LOCKED)
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        pos = self.lc.rm._pos(r1["pos_id"])
        self.assertEqual(pos["status"], "CLOSED")
        # kalan miktar ikinci intent ile kapatıldı
        cids = [c["client_order_id"] for c in self.ex.post_calls
                if c["method"] == "new_order" and c["reduce_only"]
                and c["symbol"] == "ETHUSDT"]
        self.assertIn("C281X1n2", cids)

    # ---- §11: restart korunumu + operatör kilidi --------------------------------

    def test_restart_preserves_halt_and_blocks_entries(self):
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.lc.risk_tick()
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        # RESTART: aynı DB üzerinde yeni instance
        from canary_live import LiveCanary
        from canary_liquidation import (LiquidationModel,
                                        parse_leverage_brackets,
                                        POSITION_MODE_ONEWAY)
        liq = LiquidationModel(
            brackets_by_symbol=parse_leverage_brackets(self.ex.brackets_raw),
            margin_modes=self.ex.margin_modes,
            leverages=self.ex.leverages,
            position_mode=POSITION_MODE_ONEWAY, wallet_usdt=self.ex.wallet)
        lc2 = LiveCanary(self.lc.db_path, self.ex, symbols=self.ex.symbols,
                         t0_ms=T0, filters=self.ex.filters, liq_model=liq,
                         clock_ms=self.ex._clock,
                         log_dir=self.tmp.name)
        self.assertEqual(lc2.halt_state, HALT_LOCKED)      # durum KALICI
        self.assertEqual(lc2.rm.status, STATUS_HALTED)
        out = lc2.try_entry("LINKUSDT", 1, 100.0, T0 + 9 * BAR,
                            self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        self.assertIn("HALT", " ".join(out[0]["blockers"]))
        # operatör onayı olmadan otomatik sıfırlama YOK
        lc2.risk_tick()
        self.assertEqual(lc2.halt_state, HALT_LOCKED)

    def test_no_entry_after_halt_without_operator_release(self):
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.lc.risk_tick()
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        out = self.lc.try_entry("LINKUSDT", 1, 100.0, T0 + 9 * BAR,
                                self.clock())
        self.assertEqual(out[0]["result"], "BLOCKED")
        # operatör aracı: operator_release + rm.operator_reset
        self.lc.operator_release()
        self.lc.rm.operator_reset()
        self.assertEqual(self.lc.halt_state, HALT_ACTIVE)
        self.assertEqual(self.lc.rm.status, STATUS_ACTIVE)

    # ---- §3: yalnız canary-sahipli emirler iptal edilir --------------------------

    def test_halt_cancels_only_canary_owned_pending_entries(self):
        # canary dışı (başka bot) bekleyen LIMIT emri borsada
        self.ex.orders["OTHERBOT9"] = {
            "orderId": 99999, "symbol": "XRPUSDT", "side": "BUY",
            "type": "LIMIT", "status": "NEW", "executedQty": "0",
            "avgPrice": "0", "clientOrderId": "OTHERBOT9",
            "reduceOnly": False, "fills": []}
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.lc.risk_tick()
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        # başka botun emrine DOKUNULMADI (sahiplik kanıtı yok)
        self.assertEqual(self.ex.orders["OTHERBOT9"]["status"], "NEW")

    def test_halt_unowned_position_noted_not_touched(self):
        self.ex.set_position("XRPUSDT", 100.0, 1.0)   # canary dışı pozisyon
        r1 = self._open_one()
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, r1["pos_id"], 10.00))
        self.lc.risk_tick()
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        # canary dışı pozisyon DOKUNULMADI
        self.assertEqual(self.ex.positions["XRPUSDT"]["positionAmt"], 100.0)
        evs = [r[0] for r in self.lc.rm.db.execute(
            "SELECT detail FROM risk_events WHERE kind="
            "'HALT_UNOWNED_POSITION_NOTED'").fetchall()]
        self.assertTrue(any("XRPUSDT" in e for e in evs))

    # ---- §3: UNKNOWN girişin geç fill'i HALT sırasında deftere işlenir ----------

    def test_halt_resolves_unknown_entry_late_fill_and_exits_it(self):
        """BLOK29.1 (100/100): ikinci tam-boyut giriş toplam kapıdan
        reddedildiğinden senaryo tek pozisyonda: UNKNOWN giriş çözülür
        (defter + koruma), ardından -10 HALT çıkışıyla kapatılır —
        geç fill asla borsada sahipsiz kalmaz."""
        # 1. giriş: ETH — timeout ile UYGULANIR ama sonuç bilinmez
        self.ex.timeout_client_ids.add("C281E1")
        rec1 = open_one(self.lc, "ETHUSDT", 1)
        self.assertIn("ENTRY_UNKNOWN", rec1["result"])
        # borsada 0.99 ETH pozisyonu VAR, defterde YOK
        self.assertAlmostEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.99,
                               places=9)
        self.assertEqual(len(self.lc.rm.open_positions()), 0)
        # risk tick UNKNOWN girişi ÇÖZER: sorgu → FILLED → defter + koruma
        self.clock.advance(10_000)
        cyc = self.lc.risk_tick()
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertEqual(self.lc._live_pos_row(o["pos_id"])
                         ["protection_status"], "PROTECTED")
        # HALT'e götür: tek pozisyon -10.00 (eşik) ≥ -10
        cancel_protection(self.ex, "C281P1")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", loss_mark(self.lc, o["pos_id"], 10.00))
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        # geç fill deftere işlendi VE çıkışla kapatıldı
        rows = self.lc.rm.db.execute(
            "SELECT pos_id, status FROM positions WHERE symbol='ETHUSDT'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "CLOSED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertIsNotNone(o["pos_id"])


if __name__ == "__main__":
    unittest.main()
