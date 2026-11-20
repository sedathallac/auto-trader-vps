"""BLOK10 §5 (F-D) — orphan_killer: "error ≠ empty" ihlali ve ölü heartbeat bekçisi.

Doğrulanan bulgular:
  1. (P1) open_algo_orders_for sembol-bazında İSTİSNA fırlatırsa orphan_killer
     yalnız log.DEBUG basar (orphan_killer.py:122-124); sorgu HİÇ
     doğrulanamamış sembol, "Yetim yok" yolunda tracked'den DÜŞÜRÜLÜR
     (129-131) → yetim SL bir daha ASLA taranmaz (kayıp kalıcı). error ≠ empty
     doktrininin veri-kaybı üreten hali.
  2. (P2, davranış kararı) check_heartbeat() dönüş değeri İSTENMİYOR
     (59. satır) — bekçi dekoratif. BİLİNÇLİ KARAR: iptal fazı heartbeat
     bayatken DE çalışmaya devam eder, çünkü (a) tüm yıkıcı kararlar o
     turda tazelenen exchange okumalarına dayanır (positionRisk güvenilmezse
     71-74 zaten fail-closed çıkar), (b) ana bot çöktüğünde heartbeat
     tam da BAYATLAR ve yetim SL'lerin en tehlikeli olduğu an odur
     (SOLUSDT olay ailesi). Dekoratif bekçi SESSİZ yerine AÇIK: sonuç
     yakalanır, CRITICAL loglanır, temizlik gerekçesiyle sürer.

Testler modül yollarını (LOG/TRACKED/HEARTBEAT_FILE) tmp'ye bağlar; Binance
tek başına sahtedir — tarama/izleme/iptal mantığının kendisi gerçektir.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# orphan_killer import'ta HOME'a log açar → import sırasında HOME geçici.
_ORIG_HOME = os.environ.get("HOME")
_TMP_HOME = tempfile.mkdtemp(prefix="blok10-orphan-home-")
os.environ["HOME"] = _TMP_HOME
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import orphan_killer as ok
finally:
    if _ORIG_HOME is not None:
        os.environ["HOME"] = _ORIG_HOME

SYM_CLEAN = "SOLUSDT"
SYM_ERR = "BTCUSDT"
SYM_ORPHAN = "ETHUSDT"


class FakeBinance:
    def __init__(self, algos, errors=()):
        # algos: {sym: list}; errors: sorgunun patlayacağı semboller
        self._algos = algos
        self._errors = set(errors)
        self.cancelled = []

    def position_risk(self):
        return []                       # hepsi düz → orphan adayı taranır

    def open_algo_orders_for(self, sym):
        if sym in self._errors:
            raise RuntimeError("rate limit / transport")
        return list(self._algos.get(sym, []))

    def cancel_algo_order(self, algo_id):
        self.cancelled.append(algo_id)
        return {"code": "200", "msg": "cancel returned"}


class OrphanKillerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blok10-orphan-"))
        self._saved = (ok.LOG, ok.TRACKED, ok.HEARTBEAT_FILE, ok.MAX_HEARTBEAT_AGE)
        ok.LOG = self.tmp / "orphan.log"
        ok.TRACKED = self.tmp / ".tracked_symbols.json"
        ok.HEARTBEAT_FILE = self.tmp / ".auto_trader.heartbeat"
        ok.MAX_HEARTBEAT_AGE = 180
        self._beat(age_s=0)

    def tearDown(self):
        (ok.LOG, ok.TRACKED, ok.HEARTBEAT_FILE, ok.MAX_HEARTBEAT_AGE) = self._saved

    def _beat(self, age_s=0):
        ok.HEARTBEAT_FILE.write_text(
            (datetime.now() - timedelta(seconds=age_s)).isoformat())

    def _seed_tracked(self, syms):
        ok.TRACKED.write_text(json.dumps(sorted(syms)))

    def _tracked(self):
        return set(json.loads(ok.TRACKED.read_text()))


class TestErrorNotEmpty(OrphanKillerBase):
    def test_query_error_symbol_stays_tracked(self):
        """SYM_ERR sorgusu patlarken diğerleri temiz → yalnız hatasızlar düşünülür."""
        self._seed_tracked([SYM_CLEAN, SYM_ERR])
        b = FakeBinance({SYM_CLEAN: []}, errors={SYM_ERR})
        ok.scan_and_kill(binance=b)
        self.assertIn(SYM_ERR, self._tracked(),
                      "sorgulanamayan sembol tracked'den düştü — yetimi bir "
                      "daha kimse taramaz (kayıp kalıcı)")
        self.assertNotIn(SYM_CLEAN, self._tracked())

    def test_clean_run_untracks(self):
        self._seed_tracked([SYM_CLEAN])
        b = FakeBinance({SYM_CLEAN: []})
        ok.scan_and_kill(binance=b)
        self.assertNotIn(SYM_CLEAN, self._tracked())

    def test_orphan_cancelled_and_untracked(self):
        self._seed_tracked([SYM_ORPHAN])
        b = FakeBinance({SYM_ORPHAN: [{"algoId": 42, "triggerPrice": 90}]})
        ok.scan_and_kill(binance=b)
        self.assertEqual(b.cancelled, [42])
        self.assertNotIn(SYM_ORPHAN, self._tracked())


class TestHeartbeatHonesty(OrphanKillerBase):
    def test_stale_heartbeat_still_cleans_and_logs_critical(self):
        """Bilinçli karar: iptal fazı bayat heartbeat'te SÜRER (koruyuculuk
        taze exchange okumasından gelir; bot çöküşü = en kritik temizlik anı)
        ama bekçi ARTIK SESSİZ DEĞİL: CRITICAL kanıta geçer."""
        self._beat(age_s=3600)
        self._seed_tracked([SYM_ORPHAN])
        import logging
        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        cap = _Cap(level=logging.DEBUG)
        ok.log.addHandler(cap)
        try:
            b = FakeBinance({SYM_ORPHAN: [{"algoId": 7, "triggerPrice": 1}]})
            ok.scan_and_kill(binance=b)
        finally:
            ok.log.removeHandler(cap)
        self.assertEqual(b.cancelled, [7])           # temizlik sürdü (bilinçli)
        self.assertTrue(any("HEARTBEAT" in r.getMessage() and r.levelno >= logging.CRITICAL
                            for r in records),
                        "bekçi sonucu kanıt bırakmıyor — dekoratif guard")

    def test_missing_heartbeat_file_cleans_with_critical(self):
        ok.HEARTBEAT_FILE.unlink(missing_ok=True)
        self._seed_tracked([SYM_ORPHAN])
        import logging
        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        cap = _Cap(level=logging.DEBUG)
        ok.log.addHandler(cap)
        try:
            b = FakeBinance({SYM_ORPHAN: [{"algoId": 8, "triggerPrice": 1}]})
            ok.scan_and_kill(binance=b)
        finally:
            ok.log.removeHandler(cap)
        self.assertEqual(b.cancelled, [8])
        self.assertTrue(any(r.levelno >= logging.CRITICAL for r in records))


if __name__ == "__main__":
    unittest.main()
