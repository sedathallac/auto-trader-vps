"""A2 (FAZ15 Stage2b sonu) — open_coin C6 kapısı ARTIK toplam slot kümesini
(borsa açık pozisyonları ∪ bekleyen dolumlar) sayar; HB RUNNING-çift sayısını
DEĞİL.

Doğrulanan DEFECT (4 bağımsız denetim): crash sonrası DB'de RUNNING kalan
`filled=0` stale satırlar eski kapıda girişleri kalıcı kilitleyebiliyordu
(stop POST'u öyle satıra no-op yazar; orphan temizliği yalnız API açılışında).

Bu dosya yalnız A2 senaryolarını kanıtlar; Stage 2b'nin 42 testli
test_faz15_c6_integrity.py dosyasına dokunmaz.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_trader
from position_reservation import PositionReserver

# Blocklist'te OLMAYAN test sembolleri (test_faz14t'nin sabit listesiyle uyumlu).
COIN_MAIN = "ETHUSDT"
COIN_OTHER = "DOGEUSDT"


class _Pass(Exception):
    """C6 kapısından GEÇİLDİĞİNİ kanıtlayan sentinel (liquidity aşamasında patlar)."""


class _B:
    def __init__(self, positions=(), boom=False):
        self.positions = list(positions)
        self.boom = boom
        self.position_risk_calls = 0

    def position_risk(self):
        self.position_risk_calls += 1
        if self.boom:
            raise RuntimeError("positionRisk down")
        return self.positions


class _HB:
    def __init__(self, rows=(), boom=False):
        self.rows = list(rows)
        self.boom = boom

    def active_executors(self):
        if self.boom:
            raise auto_trader.HBContractError("hb search failed")
        return self.rows


def _pos(sym, amt):
    return {"symbol": sym, "positionAmt": str(amt), "entryPrice": "1.0"}


def _mem_row(eid, pair):
    # memory-satırı şekli: closed_at ANAHTARI YOK, timestamp float.
    return {"executor_id": eid, "trading_pair": pair, "status": "RUNNING",
            "timestamp": 1763000000.0, "filled_amount_quote": 100.0}


def _stale_db_row(eid, pair):
    # _format_db_record şekli: closed_at anahtarı var + timestamp None.
    return {"executor_id": eid, "trading_pair": pair, "status": "RUNNING",
            "closed_at": None, "timestamp": None, "is_trading": False,
            "filled_amount_quote": 0.0}


def _coin(sym):
    return {"symbol": sym, "lastPrice": "100", "quoteVolume": "9e8",
            "_score": 0.9, "CQ": 0.9, "quality_vetoes": [], "quality_ok": True}


class Base(unittest.TestCase):
    def setUp(self):
        self._orig = (auto_trader.HB_OFFLINE, auto_trader._effective_max_positions,
                      auto_trader._entry_gate_closed, auto_trader.check_liquidity,
                      auto_trader._write_in_progress)
        auto_trader.HB_OFFLINE = False
        auto_trader._entry_gate_closed = lambda: ""
        auto_trader._write_in_progress = lambda: False

    def tearDown(self):
        (auto_trader.HB_OFFLINE, auto_trader._effective_max_positions,
         auto_trader._entry_gate_closed, auto_trader.check_liquidity,
         auto_trader._write_in_progress) = self._orig

    def _cap(self, n):
        auto_trader._effective_max_positions = lambda: n

    def _pass_at_liquidity(self):
        def _raise(*a, **k):
            raise _Pass()
        auto_trader.check_liquidity = _raise


class TestA2SlotCountGate(Base):
    def test_stale_db_running_row_does_not_block_other_symbol(self):
        """Borsada 1 gerçek pozisyon (SOL) + DB'de 1 stale RUNNING (NEAR):
        cap=2 iken FARKLI sembol (ETH) için kalan slot KULLANILABİLMELİ.
        Eski kapı 2 RUNNING çifti sayıp kalıcı blokluyordu (A2 DEFECT)."""
        self._cap(2)
        self._pass_at_liquidity()
        b = _B(positions=[_pos("SOLUSDT", 1.0)])
        hb = _HB(rows=[_mem_row("E0", "SOL-USDT"), _stale_db_row("E9", "NEAR-USDT")])
        with self.assertRaises(_Pass):
            auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                  risk=object())
        self.assertEqual(b.position_risk_calls, 1)  # fallback: tek taze okuma

    def test_two_real_positions_reject_new_entry(self):
        """Borsada 2 gerçek pozisyon -> yeni giriş reddedilmeli (politika cap=2)."""
        self._cap(2)
        calls = []
        auto_trader.check_liquidity = lambda *a, **k: calls.append(1) or (True, "x")
        b = _B(positions=[_pos("SOLUSDT", 1.0), _pos("ETHUSDT", 0.5)])
        hb = _HB(rows=[])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_OTHER), dry_run=False,
                                   risk=object())
        self.assertFalse(ok)
        self.assertEqual(calls, [], "kapıdan geçilmemeli")

    def test_position_read_failure_rejects(self):
        """Borsa sorgusu başarısız -> giriş reddedilmeli (fail-closed)."""
        self._cap(2)
        self._pass_at_liquidity()  # kapıdan geçilseydi _Pass ile patlardı
        b = _B(boom=True)
        hb = _HB(rows=[])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok)

    def test_hb_read_failure_rejects(self):
        """HB listesi alınamazsa slot sayımı belirsizleşir -> fail-closed
        (FAZ14v 'HB'ye ulaşılamazsa 0 görünür' koruması A2'de de geçerli)."""
        self._cap(2)
        self._pass_at_liquidity()
        b = _B(positions=[_pos("SOLUSDT", 1.0)])
        hb = _HB(boom=True)
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok)

    def test_write_in_progress_rejects(self):
        """Pending store yazımı sürüyor -> slot sayımı belirsiz -> fail-closed."""
        self._cap(2)
        self._pass_at_liquidity()
        auto_trader._write_in_progress = lambda: True
        b = _B(positions=[_pos("SOLUSDT", 1.0)])
        hb = _HB(rows=[])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok)

    def test_ghost_exchange_flat_still_only_warns(self):
        """Hayalet (memory RUNNING, borsa flat) A2'de de slot İŞGAL ETMEZ;
        yalnız uyarı. cap=2, 0 pozisyon, 1 hayalet -> ETH geçmeli."""
        self._cap(2)
        self._pass_at_liquidity()
        b = _B(positions=[])
        hb = _HB(rows=[_mem_row("E5", "NEAR-USDT")])
        with self.assertRaises(_Pass):
            auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                  risk=object())


class TestA2PassdownAndFreshness(Base):
    def test_process_candidates_passes_fresh_slots_without_second_read(self):
        """process_candidates her adaydan önce SAYIMI YENİLER ve open_coin'e
        geçirir; open_coin aynı okumayı tekrar yapmaz (A2 istek 6-7)."""
        self._cap(2)
        seen = []

        def _fake_open(binance, hb, c, **kw):
            seen.append((c["symbol"], kw.get("occupied_slots")))
            return False
        orig = auto_trader.open_coin
        auto_trader.open_coin = _fake_open
        self.addCleanup(setattr, auto_trader, "open_coin", orig)
        b = _B(positions=[_pos("SOLUSDT", 1.0)])
        hb = _HB(rows=[_mem_row("E0", "SOL-USDT"), _stale_db_row("E9", "NEAR-USDT")])
        picks = [_coin(COIN_MAIN), _coin(COIN_OTHER)]
        auto_trader.process_candidates(b, hb, picks, {"SOL-USDT"}, [], False,
                                       object(), None, None)
        self.assertEqual([s for s, _ in seen], [COIN_MAIN, COIN_OTHER])
        for _s, occ in seen:
            self.assertIsNotNone(occ)
            self.assertEqual(occ, {"SOLUSDT"})
        # 2 aday -> tam 2 taze sayım (loop başına 1); open_coin içinde TEKRAR yok:
        self.assertEqual(b.position_risk_calls, 2)

    def test_loop_breaks_when_slots_full(self):
        """cap doluysa döngü open_coin'e HİÇ inmez (üçüncü pozisyon imkânsız)."""
        self._cap(2)
        seen = []
        orig = auto_trader.open_coin
        auto_trader.open_coin = lambda *a, **k: seen.append(1) or False
        self.addCleanup(setattr, auto_trader, "open_coin", orig)
        b = _B(positions=[_pos("SOLUSDT", 1.0), _pos("ADAUSDT", 1.0)])
        hb = _HB(rows=[])
        auto_trader.process_candidates(b, hb, [_coin(COIN_MAIN)], set(), [], False,
                                       object(), None, None)
        self.assertEqual(seen, [])


class TestA2DuplicateAndAtomicGate(Base):
    def test_same_symbol_excluded_before_open_coin(self):
        """Aynı sembol zaten aktif (HB RUNNING çifti) -> select_candidates
        adaylık vermez; mükerrer giriş engellenmeye devam eder (A2 istek 3)."""
        main, probes = auto_trader.select_candidates([_coin(COIN_MAIN)],
                                                     {"ETH-USDT"}, [], 3, 3)
        self.assertEqual(main, [])
        self.assertEqual(probes, [])

    def test_atomic_reserver_gate_remains_final(self, ):
        """PositionReserver (BEGIN IMMEDIATE) son kapıdır: exchange ∪ reserved
        cap'i ve duplicate_symbol reddi A2'den bağımsız çalışmaya devam eder."""
        import tempfile
        d = tempfile.mkdtemp()
        rs = PositionReserver(os.path.join(d, "res.db"), 2)
        rs.init_schema()
        r1 = rs.reserve("ETHUSDT", {"SOLUSDT"})
        self.assertTrue(r1.ok, r1)
        r2 = rs.reserve("ETHUSDT", {"SOLUSDT"})          # aynı sembol tekrar
        self.assertFalse(r2.ok)
        self.assertEqual(r2.reason, "duplicate_symbol")
        r3 = rs.reserve("DOGEUSDT", {"SOLUSDT"})         # SOL+ETH = 2 -> cap
        self.assertFalse(r3.ok)
        self.assertEqual(r3.reason, "cap_reached")


class TestA2PolicyCapFive(Base):
    """POLİTKA (2026-11-16): LIVE_MAX_OPEN_POSITIONS=5 — A2 sayımı politika
    bağımsızdır; bu sınıf tavan=5 ile kanıtlar:
      (1) 5 GERÇEK açık pozisyon -> 6. giriş open_coin ve aday döngüsünde YOK;
      (2) 5 HAYALET executor (0 gerçek) -> giriş AÇIK (canlı olay 2026-11-16:
          eski kapı '5/5 dolu' deyip aday döngüsünü her tick kırıyordu);
      (3) bekleyen dolum slot işgal eder (cap=5'te de);
      (4) okuma başarısız -> fail-closed (cap'ten bağımsız)."""

    FIVE = ("BTCUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT")

    def _five_real(self):
        return [_pos(s, 1.0) for s in self.FIVE]

    def test_five_real_positions_block_sixth_entry(self):
        self._cap(5)
        self._pass_at_liquidity()   # kapıdan geçerse _Pass patlar — geçmemeli
        b = _B(positions=self._five_real())
        hb = _HB(rows=[])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok, "5 gerçek pozisyon varken 6. giriş açıldı")

    def test_five_real_positions_block_sixth_in_candidate_loop(self):
        self._cap(5)
        seen = []
        orig = auto_trader.open_coin
        auto_trader.open_coin = lambda *a, **k: seen.append(1) or False
        self.addCleanup(setattr, auto_trader, "open_coin", orig)
        b = _B(positions=self._five_real())
        hb = _HB(rows=[])
        auto_trader.process_candidates(b, hb, [_coin(COIN_MAIN), _coin(COIN_OTHER)],
                                       set(), [], False, object(), None, None)
        self.assertEqual(seen, [], "döngü cap doluyken open_coin'e inmeli")

    def test_five_ghosts_do_not_block_entry(self):
        """Canlı olayın birebir şekli: BNB/ENA/NEAR/WLD/XRP RUNNING hayalet,
        borsada sıfır gerçek pozisyon -> slot 0/5, ETH adayı geçmeli."""
        self._cap(5)
        self._pass_at_liquidity()
        b = _B(positions=[])
        hb = _HB(rows=[_mem_row("E1", "BNB-USDT"), _mem_row("E2", "ENA-USDT"),
                       _mem_row("E3", "NEAR-USDT"), _mem_row("E4", "WLD-USDT"),
                       _mem_row("E5", "XRP-USDT")])
        with self.assertRaises(_Pass):
            auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                  risk=object())

    def test_four_ghosts_plus_one_real_leave_four_slots(self):
        """4 hayalet + 1 gerçek pozisyon (cap=5) -> kalan 4 slot kullanılabilir."""
        self._cap(5)
        self._pass_at_liquidity()
        b = _B(positions=[_pos("SOLUSDT", 1.0)])
        hb = _HB(rows=[_mem_row("E1", "BNB-USDT"), _mem_row("E2", "ENA-USDT"),
                       _mem_row("E3", "NEAR-USDT"), _mem_row("E4", "WLD-USDT")])
        with self.assertRaises(_Pass):
            auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                  risk=object())

    def test_pending_fill_counts_as_slot_at_cap5(self):
        """4 gerçek + 1 bekleyen dolum = 5 slot -> 6. aday reddedilmeli."""
        self._cap(5)
        self._pass_at_liquidity()
        auto_trader._PENDING_FILLS["E77"] = {"symbol": COIN_OTHER}
        self.addCleanup(auto_trader._PENDING_FILLS.clear)
        b = _B(positions=[_pos(s, 1.0) for s in self.FIVE[:4]])
        hb = _HB(rows=[])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok, "pending dolum slot saymadı")

    def test_five_real_plus_ghost_still_blocks(self):
        """5 gerçek + hayaletler: sayım gerçeklerle 5 -> blok sürer (hayalet
        sayıyı DÜŞÜRMEZ ve artırMAZ; bağlayıcı olan gerçek slotlar)."""
        self._cap(5)
        self._pass_at_liquidity()
        b = _B(positions=self._five_real())
        hb = _HB(rows=[_mem_row("E9", "TRX-USDT")])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok)

    def test_unreadable_position_read_blocks_entry_at_cap5(self):
        self._cap(5)
        self._pass_at_liquidity()
        b = _B(boom=True)
        hb = _HB(rows=[])
        ok = auto_trader.open_coin(b, hb, _coin(COIN_MAIN), dry_run=False,
                                   risk=object())
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
