"""BLOK12 §1 — kalıcı tek-kullanımlık operatör onayı testleri.

Sözleşme: ilk işlem ALTI kanıtla kaydedilmeden ikinci işlem ONAYLANAMAZ;
onay SQLite'ta kalıcıdır (restart sıfırlamaz), tek kullanımlıktır; bozuk/
süresi geçmiş/kullanılmış/yanlış token RED; ÜÇÜNCÜ işlem daima RED.
Bot yardımcı düzeyi + modül düzeyi. Gerçek emir/ağ YOK.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import operator_approval as OA        # noqa: E402
import auto_trader                    # noqa: E402

SIX = {k: True for k in OA.SIX_EVIDENCE}


def _gate(tmp, ttl=3600.0, now=None):
    return OA.ApprovalGate(os.path.join(tmp, "gate.db"),
                           approval_ttl_s=ttl, now=now)


class TestGateModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12gate-")
        self.g = _gate(self.tmp)

    def test_01_baslangic_ilk_islem_tamamlanmadi(self):
        st = self.g.state()
        self.assertEqual(st["state"], "FIRST_OP_INCOMPLETE")
        self.assertIsNone(self.g.grant("op"))      # onay ÜRETİLEMEZ

    def test_02_eksik_kanit_kabul_edilmez_altisi_ile_kayit(self):
        partial = dict(SIX)
        partial["position_flat"] = False
        self.assertFalse(self.g.record_completion(1, partial, "op"))
        self.assertFalse(self.g.record_completion(1, {"position_flat": True},
                                                  "op"))
        self.assertTrue(self.g.record_completion(1, SIX, "op"))
        self.assertEqual(self.g.state()["state"], "AWAITING_OPERATOR_APPROVAL")

    def test_03_sira_disi_completion_red(self):
        self.assertFalse(self.g.record_completion(2, SIX, "op"))

    def test_04_grant_tek_token_state_gecisi(self):
        self.g.record_completion(1, SIX, "op")
        tok = self.g.grant("op")
        self.assertIsInstance(tok, str)
        self.assertEqual(len(tok), 32)
        self.assertEqual(self.g.state()["state"], "APPROVED_AWAITING_USE")
        self.assertIsNone(self.g.grant("op2"))     # seq başına TEK grant

    def test_05_consume_tek_kullanim(self):
        self.g.record_completion(1, SIX, "op")
        tok = self.g.grant("op")
        ok, why = self.g.consume(2, tok)
        self.assertTrue(ok, why)
        ok2, why2 = self.g.consume(2, tok)
        self.assertFalse(ok2)
        self.assertIn("KULLANILDI", why2)

    def test_06_yanlis_bos_token_red(self):
        self.g.record_completion(1, SIX, "op")
        self.g.grant("op")
        self.assertEqual(self.g.consume(2, "yanlis")[0], False)
        self.assertIn("eşleşmedi", self.g.consume(2, "yanlis")[1])
        self.assertIn("ONAY-YOK", self.g.consume(2, None)[1])
        self.assertIn("ONAY-YOK", self.g.consume(2, "")[1])

    def test_07_suresi_gecen_onay_red(self):
        t = [1000.0]
        g = _gate(self.tmp, ttl=10.0, now=lambda: t[0])
        g.record_completion(1, SIX, "op")
        tok = g.grant("op")
        t[0] += 11.0
        ok, why = g.consume(2, tok)
        self.assertFalse(ok)
        self.assertIn("SURESI-DOLDU", why)
        # süre geçişi kayıt durumuna da işlendi
        con = sqlite3.connect(os.path.join(self.tmp, "gate.db"))
        row = con.execute("SELECT status FROM gate_approvals WHERE seq=2")\
                 .fetchone()
        con.close()
        self.assertEqual(row[0], "EXPIRED")

    def test_08_bozuk_kayit_fail_closed(self):
        self.g.record_completion(1, SIX, "op")
        tok = self.g.grant("op")
        con = sqlite3.connect(os.path.join(self.tmp, "gate.db"))
        con.execute("UPDATE gate_approvals SET expires_at='bozuk' WHERE seq=2")
        con.commit()
        con.close()
        ok, why = self.g.consume(2, tok)
        self.assertFalse(ok)
        self.assertIn("BOZUK", why)

    def test_09_restart_kaliciligi(self):
        self.g.record_completion(1, SIX, "op")
        tok = self.g.grant("op")
        g2 = _gate(self.tmp)                        # yeni süreç = yeni instance
        self.assertEqual(g2.state()["state"], "APPROVED_AWAITING_USE")
        self.assertTrue(g2.consume(2, tok)[0])
        self.assertFalse(g2.consume(2, tok)[0])     # tek kullanım KALICI

    def test_10_ucuncu_islem_daima_red(self):
        self.assertEqual(self.g.consume(3, "x")[1],
                         "UCUNCU-ISLEM-YASAK: üçüncü işlem daima reddedilir")
        ok, why = self.g.entry_consume(3, None)
        self.assertFalse(ok)
        self.assertIn("UCUNCU-ISLEM-YASAK", why)
        # iki işlem de tamamlansa grant ÜRETİLEMEZ (max_ops=2)
        self.g.record_completion(1, SIX, "op")
        tok = self.g.grant("op")
        self.g.consume(2, tok)
        self.g.record_completion(2, SIX, "op")
        self.assertEqual(self.g.state()["state"], "ALL_OPS_DONE_THIRD_ALWAYS_RED")
        self.assertIsNone(self.g.grant("op"))

    def test_11_entry_consume_birinci_islem_serbest_ikinci_sartli(self):
        ok, _ = self.g.entry_consume(1, None)
        self.assertTrue(ok)                          # runbook + bütçe yeter
        ok2, why2 = self.g.entry_consume(2, None)    # onay yok → RED
        self.assertFalse(ok2)
        self.assertIn("ONAY-YOK", why2)

    def test_12_revoke_kullanilamaz_yapar(self):
        self.g.record_completion(1, SIX, "op")
        tok = self.g.grant("op")
        self.assertTrue(self.g.revoke(2))
        ok, why = self.g.consume(2, tok)
        self.assertFalse(ok)
        self.assertIn("IPTAL", why)


class TestAutoTraderGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12atg-")
        self._saved = {k: os.environ.get(k) for k in
                       ("CONTROLLED_LIVE_VALIDATION", "VALIDATION_BUDGET_DB",
                        "VALIDATION_APPROVAL_DB", "OPERATOR_APPROVAL_TOKEN_FILE")}
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["VALIDATION_BUDGET_DB"] = os.path.join(self.tmp, "vb.db")
        os.environ["VALIDATION_APPROVAL_DB"] = os.path.join(self.tmp, "gate.db")
        os.environ.pop("OPERATOR_APPROVAL_TOKEN_FILE", None)
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None

    def test_birinci_islem_onay_gerekmez(self):
        ok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")
        self.assertTrue(ok, why)
        gok, gwhy = auto_trader._operator_gate_check()
        self.assertTrue(gok, gwhy)

    def test_ikinci_islem_onaysiz_red_okenli_gecer(self):
        ok, _, _ = auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")
        self.assertTrue(ok)
        gok, _ = auto_trader._operator_gate_check()    # 1. işlem geçti
        self.assertTrue(gok)
        ok2, _, _ = auto_trader._validation_reserve("ETHUSDT", 35.0, "i2")
        self.assertTrue(ok2)
        gok2, why2 = auto_trader._operator_gate_check()
        self.assertFalse(gok2)                           # onay YOK → RED
        self.assertIn("ONAY-YOK", why2)
        # operatör: altı kanıt + grant + token dosyası
        g = OA.ApprovalGate(os.environ["VALIDATION_APPROVAL_DB"])
        self.assertTrue(g.record_completion(1, SIX, "operator"))
        tok = g.grant("operator")
        tp = os.path.join(self.tmp, "tok")
        with open(tp, "w") as fh:
            fh.write(tok + "\n")
        os.chmod(tp, 0o600)
        os.environ["OPERATOR_APPROVAL_TOKEN_FILE"] = tp
        # BLOK12 devam-E §3: gönderim öncesi kapı DOĞRULAR (mutasyonsuz,
        # tekrarlanabilir); TEK KULLANIM tüketimde (_operator_gate_consume).
        gok3, why3 = auto_trader._operator_gate_check()
        self.assertTrue(gok3, why3)                      # doğrulandı
        gok3b, _ = auto_trader._operator_gate_check()
        self.assertTrue(gok3b)                           # verify tekrarlanabilir
        cok, cwhy = auto_trader._operator_gate_consume()
        self.assertTrue(cok, cwhy)                       # tek kullanım BURADA
        self.assertFalse(auto_trader._operator_gate_check()[0])  # verify de RED
        cok2, why4 = auto_trader._operator_gate_consume()
        self.assertFalse(cok2)                           # tek kullanım
        self.assertIn("KULLANILDI", why4)

    def test_token_dosyasi_okunamazsa_red(self):
        ok, _, _ = auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")
        self.assertTrue(ok)
        ok2, _, _ = auto_trader._validation_reserve("ETHUSDT", 35.0, "i2")
        self.assertTrue(ok2)
        os.environ["OPERATOR_APPROVAL_TOKEN_FILE"] = \
            os.path.join(self.tmp, "yok-boyle-dosya")
        gok, why = auto_trader._operator_gate_check()
        self.assertFalse(gok)
        self.assertIn("ONAY-DOSYASI-OKUNAMADI", why)


class TestTokenFilePermissions(unittest.TestCase):
    """BLOK12 devam-D: token DOSYASI izinleri KODDA doğrulanır — 0600 dışı
    (grup/diğer okuyabilir) RED; RED durumunda token HARCANMAZ."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12tok-")
        self._saved = {k: os.environ.get(k) for k in
                       ("CONTROLLED_LIVE_VALIDATION", "VALIDATION_BUDGET_DB",
                        "VALIDATION_APPROVAL_DB",
                        "OPERATOR_APPROVAL_TOKEN_FILE")}
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["VALIDATION_BUDGET_DB"] = os.path.join(self.tmp, "vb.db")
        os.environ["VALIDATION_APPROVAL_DB"] = os.path.join(self.tmp,
                                                            "gate.db")
        os.environ.pop("OPERATOR_APPROVAL_TOKEN_FILE", None)
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None
        # 2. işleme kadar gel: 1. reserve+gate, kayıt+grant
        self.assertTrue(auto_trader._validation_reserve(
            "ETHUSDT", 40.0, "i1")[0])
        self.assertTrue(auto_trader._operator_gate_check()[0])
        self.g = OA.ApprovalGate(os.environ["VALIDATION_APPROVAL_DB"])
        self.assertTrue(self.g.record_completion(1, SIX, "op"))
        self.tok = self.g.grant("op")
        self.assertIsNotNone(self.tok)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None

    def _to_second(self):
        self.assertTrue(auto_trader._validation_reserve(
            "ETHUSDT", 35.0, "i2")[0])

    def _write(self, mode):
        tp = os.path.join(self.tmp, "tok")
        with open(tp, "w") as fh:
            fh.write(self.tok)
        os.chmod(tp, mode)
        os.environ["OPERATOR_APPROVAL_TOKEN_FILE"] = tp
        return tp

    def test_dunya_oku_0644_red_ve_token_harcanmar(self):
        self._to_second()
        self._write(0o644)
        gok, why = auto_trader._operator_gate_check()
        self.assertFalse(gok)
        self.assertIn("600", why)
        self.assertEqual(self.g.state()["state"], "APPROVED_AWAITING_USE")

    def test_grup_oku_0640_red(self):
        self._to_second()
        self._write(0o640)
        gok, why = auto_trader._operator_gate_check()
        self.assertFalse(gok)
        self.assertIn("600", why)

    def test_0600_dogrulanir_token_henuz_harcanmaz(self):
        # BLOK12 devam-E §3: gönderim öncesi kapı DOĞRULAMA yapar; tüketim
        # emrin kaderinden sonra _operator_gate_consume ile olur.
        self._to_second()
        self._write(0o600)
        gok, why = auto_trader._operator_gate_check()
        self.assertTrue(gok, why)
        self.assertEqual(self.g.state()["state"], "APPROVED_AWAITING_USE")
        cok, cwhy = auto_trader._operator_gate_consume()
        self.assertTrue(cok, cwhy)
        self.assertEqual(self.g.state()["state"], "APPROVAL_CONSUMED")


if __name__ == "__main__":
    unittest.main()
