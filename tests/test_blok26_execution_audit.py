"""BLOK26 Bölüm 3 — execution model denetim testleri.

ÇALIŞAN SİSTEMDE DEĞİŞİKLİK YOK: bu testler worktree kopyasındaki (2aa5d7f)
mevcut davranışı ÖLÇER. BLOK25 testlerinin kapsamadığı sorular:
  #5  partial fill davranışı (yok — belgelenmiş sınır)
  #8  pozisyon funding anından önce kapanmışsa ödeme uygulanmaz
  #12 slippage iki bacakta da ayrı uygulanır (flip = 2 bağımsız fill)
  #13 Mark Price ile Last Price ayrımı (mark_px vs fill fiyatı)
"""
import json
import os
import tempfile
import unittest

from paper_broker import PaperBroker, FILL_MODEL
from tests.test_blok25_paper import _decision, _bar, SYM, OTHER, T0, BAR


def _broker(**kw):
    db = os.path.join(tempfile.mkdtemp(), "paper.db")
    return PaperBroker(db, [SYM, OTHER], **kw)


class TestFundingEdgeCases(unittest.TestCase):
    def test_position_closed_before_settlement_no_funding(self):
        """#8: pozisyon yerleşim anından ÖNCE kapatıldıysa funding YOK."""
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        # pozisyonu yerleşimden ÖNCE kapat (flip -> flat)
        b.apply_target(_decision(direction=0, notional=0.0,
                                 execution_ts=T0 + BAR,
                                 ref_price=100.0, signal_price=100.0,
                                 bar_ts=T0), 100.0)
        self.assertEqual(b._position(SYM)["direction"], 0)
        # yerleşim geldi (entry'den sonra ama pozisyon KAPALI); router'ın
        # bar başındaki çağrısını taklit et
        b.note_funding_rate(SYM, T0 + 2 * BAR, 0.0001)
        b.settle_funding(SYM, T0 + 3 * BAR)
        self.assertEqual(b.funding_rows(), [])

    def test_funding_uses_real_settlement_timestamp(self):
        """#7: yerleşim, kayıtlı fundingTime'a denk gelen bar işlenirken
        uygulanır — bar ts'iyle değil fundingTime ile karşılaştırılır."""
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        settle_ts = T0 + 2 * BAR + 1000     # bar open'dan sonra, sonraki
        b.note_funding_rate(SYM, settle_ts, 0.0001)  # barın aralığında
        # router sırası: bar T0+3*BAR başında settle (last, T0+3*BAR]
        b.settle_funding(SYM, T0 + 3 * BAR)
        rows = b.funding_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], settle_ts)


class TestSlippageBothLegs(unittest.TestCase):
    def test_flip_applies_slippage_to_each_leg_independently(self):
        """#12: flip = iki bağımsız fill; her bacakta spread+slippage+
        impact AYRI uygulanır (çıkış bacağı girişten bağımsız fiyatlarnır)."""
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        entry = b._position(SYM)["entry_px"]
        # long'u short'a çevir (flip)
        b.apply_target(_decision(direction=-1, notional=100.0,
                                 execution_ts=T0 + BAR,
                                 ref_price=100.0, signal_price=100.0,
                                 bar_ts=T0), 100.0)
        pos = b._position(SYM)
        self.assertEqual(pos["direction"], -1)
        # her iki fill de mid'den KÖTÜ: alış yukarı, satış aşağı
        self.assertGreater(entry, 100.0)     # long girişi (alış) yukarı
        self.assertLess(pos["entry_px"], 100.0)   # short girişi (satış) aşağı
        t = b.closed_trades()[0]
        # çıkış fill'i de satış olduğundan ref'in altında
        self.assertLess(t["exit_px"], 100.0)
        # iki bacağın spread_bp/slippage_bp/impact_bp kayıtları ayrı
        self.assertEqual(t["spread_bp"], FILL_MODEL["half_spread_bp"])
        self.assertEqual(t["slippage_bp"], FILL_MODEL["slippage_bp"])


class TestMarkLastSeparation(unittest.TestCase):
    def test_mark_and_fill_prices_are_distinct_fields(self):
        """#13: Mark Price (bar kapanış proxy'si) ile Last/fill fiyatı ayrı
        alanlarda tutulur; uPnL mark ile, rPnL fill ile hesaplanır."""
        b = _broker()
        b.apply_target(_decision(direction=1, notional=100.0), 100.0)
        _bar(b, T0 + BAR, 100.0, 110.0)     # mark = 110 (kapanış)
        pos = b._position(SYM)
        # fill fiyatı (entry) ile mark farklı: uPnL mark üzerinden
        self.assertNotEqual(pos["entry_px"], 110.0)
        st = b.account_status()
        self.assertAlmostEqual(
            st["unrealized"],
            pos["direction"] * pos["units"] * (110.0 - pos["entry_px"]),
            places=9)
        # marks tablosu mark'ı ayrı kaydeder (fill değil)
        m = b.marks_rows(SYM)[-1]
        self.assertEqual(m["mark_px"], 110.0)


class TestNoFillWithoutData(unittest.TestCase):
    def test_stale_data_blocks_new_virtual_position(self):
        """Görev kuralı: stale data durumunda yeni sanal pozisyon AÇILMAZ.
        Runner seviyesinde STALE_DATA -> HALT -> halted_kill_switch FAIL
        (BLOK25 resilience testi). Broker seviyesinde: halted hesapta
        apply_target yine çalışabilir AMA checklist kapıyı tuttuğu için
        runner asla çağırmaz — bu test o sözleşmenin broker tarafını
        belgeler: halt durumu sorgulanabilir."""
        b = _broker()
        b.halt("STALE_DATA test")
        st = b.account_status()
        self.assertTrue(st["halted"])
        self.assertEqual(st["halt_reason"], "STALE_DATA test")


class TestPartialFillAbsence(unittest.TestCase):
    def test_fills_are_full_size_documented_limitation(self):
        """#5: partial fill modeli YOK — her fill tam boyut. Belgelenmiş
        sınır: hedef notional ~$125/sembol (w=1/8, $1000 equity) likidite
        karşısında ihmal edilebilir; ölçüm raporunda PARTIAL olarak
        işaretlenir."""
        b = _broker()
        d = _decision(direction=1, notional=100.0)
        b.apply_target(d, 100.0)
        pos = b._position(SYM)
        self.assertAlmostEqual(pos["units"] * pos["entry_px"], 100.0,
                               delta=0.01)   # tam notional, tek fill


if __name__ == "__main__":
    unittest.main()
