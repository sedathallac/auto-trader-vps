"""BLOK28.1 §11 — TOPLAM HALT TESTLERİ (mock/fixture; ağ çağrısı YOK).

-10 USDT toplam zarar eşiği (§3): -9.99 / -10.00 sınırları, iki pozisyonun
da çıkışı, UNKNOWN emir varken HALT'in DEVAM etmesi, kısmi kalan, restart
korunumu, operatör onayı olmadan giriş yasağı, yalnız C281-sahipli giriş
emirlerinin iptali, UNKNOWN girişin geç fill'inin HALT sırasında deftere
işlenip çıkışa alınması.

State machine (§3): CANARY_ACTIVE → CANARY_HALT_INTENT → CANARY_RISK_HALTED
→ CANARY_EXITING_ALL → CANARY_EXIT_RECONCILIATION → CANARY_FLAT_VERIFIED →
CANARY_HALT_LOCKED (yalnız operatör).

Matematik: ETH+SOL her biri qty 0.5 @100, komisyon 0.025/yan.
  tek pozisyon mark 90.05 → -5.00 | 90.06 → -4.995
  iki pozisyon 90.06 → toplam -9.99 (HALT YOK)
  iki pozisyon 90.05 → toplam -10.00 (HALT)
Stop'lar 90.10'da olduğundan eşik testlerinde korumalar dışarıdan iptal
edilir (borsa stop'u önce atlar; -10 gap senaryosu ayrıca test edilir).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary, prime,
                                 open_one, T0)

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

    def _open_two(self):
        """ETH + SOL: her biri 50 USDT notional, FILLED + PROTECTED."""
        r1 = open_one(self.lc, "ETHUSDT", 1)
        r2 = open_one(self.lc, "SOLUSDT", 1)
        self.assertEqual(r1["result"], "FILLED")
        self.assertEqual(r2["result"], "FILLED")
        return r1, r2

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

    # ---- §11: -9.99 / -10.00 eşikleri ---------------------------------------

    def test_total_minus_9_99_no_halt(self):
        self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.06)     # her pozisyon -4.995
        self.ex.set_mark("SOLUSDT", 90.06)
        cyc = self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_ACTIVE)
        self.assertEqual(self.lc.halt_state, HALT_ACTIVE)
        self.assertEqual(cyc["risk_actions"], [])
        for p in self.lc.rm.open_positions():
            self.assertEqual(p["status"], "OPEN")

    def test_total_minus_10_00_halts_and_exits_all_positions(self):
        r1, r2 = self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)     # her pozisyon -5.00
        self.ex.set_mark("SOLUSDT", 90.05)
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertNotEqual(self.lc.halt_state, HALT_ACTIVE)
        # zincir LOCKED'a kadar ilerler
        state = self._drive_halt_chain()
        self.assertEqual(state, HALT_LOCKED)
        # İKİ pozisyon da CLOSED + borsada flat (positionRisk ile)
        for rec in (r1, r2):
            pos = self.lc.rm._pos(rec["pos_id"])
            self.assertEqual(pos["status"], "CLOSED")
            self.assertEqual(self.lc._live_pos_row(rec["pos_id"])
                             ["exit_status"], "EXIT_CONFIRMED")
        for s in ("ETHUSDT", "SOLUSDT"):
            self.assertEqual(self.ex.positions[s]["positionAmt"], 0.0)
        # state machine sırası
        joined = " ".join(self._halt_transitions())
        for st in (HALT_INTENT, HALT_RISK, HALT_EXITING_ALL, HALT_RECON,
                   HALT_FLAT_VERIFIED, HALT_LOCKED):
            self.assertIn(st, joined)

    def test_gap_slippage_can_exceed_minus_10_limits_are_triggers(self):
        """§0: limitler TETİK, cap değil. 90dk gap'te stop 90.10'da ama mark
        80'e düşerse SAFE_EXIT market 80'den dolar → toplam -10'u aşabilir."""
        r1, r2 = self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(60_000)
        self.ex.set_mark("ETHUSDT", 80.0)      # gap: -10.025 tek başına
        self.ex.set_mark("SOLUSDT", 80.0)
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        total = self.lc.rm.total_net_loss(self.clock())
        self.assertIsNotNone(total)
        self.assertGreater(total, 10.0)         # cap DEĞİL — raporlanır gerçek

    # ---- §11: UNKNOWN varken HALT devam eder ----------------------------------

    def test_halt_continues_while_exit_unknown(self):
        r1, _ = self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.ex.set_mark("SOLUSDT", 90.05)
        self.ex.no_apply_timeouts.add("C281X1n1")   # ETH çıkışı UNKNOWN
        self.lc.risk_tick()                          # HALT + SOL çıkışı tamam
        # UNKNOWN hâlâ çözülmedi: LOCKED'a GEÇİLEMEZ, ETH borsada AÇIK
        self.assertNotEqual(self.lc.halt_state, HALT_LOCKED)
        self.assertNotEqual(self.lc.halt_state, HALT_FLAT_VERIFIED)
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.5)
        # çözüm zinciri: sorgu (-2011) → doğrulanmış kalan → YENİ intent
        # (körlemesine aynı emri tekrar DEĞİL) → kapanır → LOCKED
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        self.assertEqual(self.lc.rm._pos(r1["pos_id"])["status"], "CLOSED")
        eth_exits = [c["client_order_id"] for c in self.ex.post_calls
                     if c["method"] == "new_order" and c["reduce_only"]
                     and c["symbol"] == "ETHUSDT"]
        self.assertEqual(eth_exits, ["C281X1n1", "C281X1n2"])

    def test_halt_partial_exit_remainder_before_locked(self):
        r1, _ = self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.ex.set_mark("SOLUSDT", 90.05)
        self.ex.partial_fill_qty["C281X1n1"] = 0.2    # ETH çıkışı kısmi
        self.lc.risk_tick()
        # kalan 0.3 kapanmadan LOCKED YOK
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
        r1, r2 = self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.ex.set_mark("SOLUSDT", 90.05)
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
        self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.ex.set_mark("SOLUSDT", 90.05)
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
        self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.ex.set_mark("SOLUSDT", 90.05)
        self.lc.risk_tick()
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        # başka botun emrine DOKUNULMADI (sahiplik kanıtı yok)
        self.assertEqual(self.ex.orders["OTHERBOT9"]["status"], "NEW")

    def test_halt_unowned_position_noted_not_touched(self):
        self.ex.set_position("XRPUSDT", 100.0, 1.0)   # canary dışı pozisyon
        r1, _ = self._open_two()
        cancel_protection(self.ex, "C281P1")
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("ETHUSDT", 90.05)
        self.ex.set_mark("SOLUSDT", 90.05)
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
        # 1. giriş: ETH — timeout ile UYGULANIR ama sonuç bilinmez
        self.ex.timeout_client_ids.add("C281E1")
        rec1 = open_one(self.lc, "ETHUSDT", 1)
        self.assertIn("ENTRY_UNKNOWN", rec1["result"])
        # borsada 0.5 ETH pozisyonu VAR, defterde YOK
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.5)
        self.assertEqual(len(self.lc.rm.open_positions()), 0)
        # risk tick UNKNOWN girişi ÇÖZER: sorgu → FILLED → defter + koruma
        self.clock.advance(10_000)
        cyc = self.lc.risk_tick()
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertEqual(self.lc._live_pos_row(o["pos_id"])
                         ["protection_status"], "PROTECTED")
        # 2. giriş: SOL — normal, korumalı
        rec2 = open_one(self.lc, "SOLUSDT", 1)
        self.assertEqual(rec2["result"], "FILLED")
        # SOL'u -10 tetiğiyle HALT'e götür (tek pozisyon -10.025 ≥ -10)
        cancel_protection(self.ex, "C281P2")
        self.clock.advance(10_000)
        self.ex.set_mark("SOLUSDT", 80.0)
        self.lc.risk_tick()
        self.assertEqual(self.lc.rm.status, STATUS_HALTED)
        # HALT zinciri: UNKNOWN girişi ÇÖZER (sorgu → FILLED → defter + çıkış)
        self.assertEqual(self._drive_halt_chain(), HALT_LOCKED)
        # geç fill deftere işlendi VE çıkışla kapatıldı
        rows = self.lc.rm.db.execute(
            "SELECT pos_id, status FROM positions WHERE symbol='ETHUSDT'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "CLOSED")
        self.assertEqual(self.ex.positions["ETHUSDT"]["positionAmt"], 0.0)
        self.assertEqual(self.lc.rm._pos(rec2["pos_id"])["status"], "CLOSED")
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertIsNotNone(o["pos_id"])


if __name__ == "__main__":
    unittest.main()
