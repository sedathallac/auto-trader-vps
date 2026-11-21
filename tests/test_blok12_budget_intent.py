"""BLOK12 §2 — intent-idempotency, payload bağı, doluş-tavanı ön-koruması.

Kök neden (doğrulandı): auto_trader._validation_reserve her çağrıda
uuid4 üretiyordu → AYNI mantıksal girişin yeniden çağrılması yeni intent
sayılır, ikinci bütçe hakkı tüketilebilirdi. BLOK12: deterministik intent +
payload bağı + first_use (tekrar → POST yok, hak korunur) + worst-fill önden
miktar/fiyat koruması + gerçek fill denetimi.

Test katmanları: modül (ValidationBudget), yardımcı (auto_trader.*), migration.
Gerçek emir/ağ YOK.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import validation_budget as VB        # noqa: E402
import auto_trader                    # noqa: E402


def _budget(tmp):
    return VB.ValidationBudget(os.path.join(tmp, "vb.db"),
                               VB.Limits(2, 50.0, 100.0))


class TestIntentModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12vb-")
        self.b = _budget(self.tmp)

    def test_ayni_intent_ikinci_tuketim_yok(self):
        r1 = self.b.reserve("ETHUSDT", 40.0, "i1")
        r2 = self.b.reserve("ETHUSDT", 40.0, "i1")
        self.assertTrue(r1.ok and r1.first_use)
        self.assertTrue(r2.ok and not r2.first_use)   # IDEMPOTENT
        self.assertEqual(r1.rid, r2.rid)
        st = self.b.status()
        self.assertEqual(st["trades_used"], 1)
        self.assertEqual(st["notional_used"], 40.0)

    def test_ayni_intent_farkli_notional_red(self):
        self.assertTrue(self.b.reserve("ETHUSDT", 40.0, "i1").ok)
        r = self.b.reserve("ETHUSDT", 45.0, "i1")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "INTENT_PAYLOAD_MISMATCH")
        self.assertEqual(self.b.status()["trades_used"], 1)

    def test_ayni_intent_farkli_sembol_red(self):
        self.assertTrue(self.b.reserve("ETHUSDT", 40.0, "i1").ok)
        r = self.b.reserve("BCHUSDT", 40.0, "i1")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "INTENT_PAYLOAD_MISMATCH")

    def test_unknown_ack_tekrar_gonderim_yok_hak_korunur(self):
        r1 = self.b.reserve("ETHUSDT", 40.0, "i1")     # POST belirsiz: confirm yok
        r2 = self.b.reserve("ETHUSDT", 40.0, "i1")     # aynı giriş tekrarına izin YOK
        self.assertTrue(r2.ok and not r2.first_use)
        con = sqlite3.connect(os.path.join(self.tmp, "vb.db"))
        row = con.execute("SELECT status FROM validation_reservations "
                          "WHERE rid=?", (r1.rid,)).fetchone()
        con.close()
        self.assertEqual(row[0], "RESERVED")           # hak KORUNUYOR (iade yok)

    def test_tam_50_ve_ilki_first_use(self):
        r = self.b.reserve("ETHUSDT", 50.0, "k50")
        self.assertTrue(r.ok and r.first_use)
        self.assertFalse(self.b.reserve("ETHUSDT", 50.0001, "k51").ok)

    def test_confirm_emir_kimligi_baglar_partial_fill_iade_etmez(self):
        r = self.b.reserve("ETHUSDT", 50.0, "i9")
        self.assertTrue(self.b.confirm(r.rid, executor_id="exec-77",
                                       op_period="i9"))
        con = sqlite3.connect(os.path.join(self.tmp, "vb.db"))
        row = con.execute("SELECT status, executor_id, op_period "
                          "FROM validation_reservations WHERE rid=?",
                          (r.rid,)).fetchone()
        con.close()
        self.assertEqual(row, ("CONSUMED", "exec-77", "i9"))
        # Kısmi fill muhasebesi: hak TAM notional ile tüketilmiş kalır
        # (konservatif — iade yalnız teyitli executor-yok yolunda).
        self.assertEqual(self.b.status()["notional_used"], 50.0)
        self.assertFalse(self.b.release(r.rid))        # CONSUMED iade EDİLEMEZ

    def test_released_sonrasi_ayni_payload_tuketir_farkli_red(self):
        r = self.b.reserve("ETHUSDT", 30.0, "i2")
        self.b.release(r.rid, reason="confirmed_failure")
        # aynı intent, AYNI payload → yeniden tüketir (BLOK10 güvenli tarafı)
        r2 = self.b.reserve("ETHUSDT", 30.0, "i2")
        self.assertTrue(r2.ok and r2.first_use)
        # aynı intent, FARKLI payload → hâlâ RED
        r3 = self.b.reserve("ETHUSDT", 31.0, "i2")
        self.assertFalse(r3.ok)
        self.assertEqual(r3.reason, "INTENT_PAYLOAD_MISMATCH")

    def test_eszamanli_worker_hak_asimi_yok(self):
        results = []
        lk = threading.Lock()

        def worker(w):
            b = _budget(self.tmp)          # AYNI dosya, ayrı instance
            for k in range(6):
                try:
                    r = b.reserve("ETHUSDT", 10.0, f"w{w}k{k}")
                except VB.BudgetError:
                    # BLOK12: yoğun içeride-koşumda SQLite kilit çakışması
                    # FAIL-CLOSED RED'dir — hak TÜKETİLMEZ, muhasebeye yazılmaz.
                    r = None
                with lk:
                    results.append(r)
        ts = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        st = self.b.status()
        self.assertLessEqual(st["trades_used"], 2)
        self.assertLessEqual(st["notional_used"], 100.0 + 1e-9)
        consumed = [r for r in results
                    if r is not None and r.ok and r.first_use]
        self.assertEqual(len(consumed), st["trades_used"])   # mutabakat

    def test_migration_eski_sema_uyumlu(self):
        db = os.path.join(self.tmp, "eski.db")
        con = sqlite3.connect(db)
        con.executescript("""
CREATE TABLE validation_state (id INTEGER PRIMARY KEY CHECK (id=1),
  trades_used INTEGER NOT NULL DEFAULT 0, notional_used REAL NOT NULL DEFAULT 0.0,
  updated_at TEXT NOT NULL);
INSERT INTO validation_state VALUES (1, 0, 0.0, 'init');
CREATE TABLE validation_reservations (rid TEXT PRIMARY KEY,
  idem_key TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, notional REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('RESERVED','CONSUMED','RELEASED')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT);
""")
        con.commit()
        con.close()
        b = VB.ValidationBudget(db, VB.Limits(2, 50.0, 100.0))
        r = b.reserve("ETHUSDT", 20.0, "m1")
        self.assertTrue(r.ok)
        self.assertTrue(b.confirm(r.rid, executor_id="e1", op_period="m1"))
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT executor_id FROM "
                                     "validation_reservations").fetchone()[0], "e1")
        con.close()


class TestWorstFill(unittest.TestCase):
    def test_worst_case_fill_tablosu(self):
        self.assertTrue(VB.worst_case_fill_ok(50.0, 50.0, 0.0))
        self.assertFalse(VB.worst_case_fill_ok(50.0, 50.0, 0.005))
        self.assertTrue(VB.worst_case_fill_ok(49.7, 50.0, 0.005))
        self.assertFalse(VB.worst_case_fill_ok(49.9, 50.0, 0.005))
        self.assertFalse(VB.worst_case_fill_ok(50.0, 50.0, -0.01))   # negatif drift yok
        self.assertFalse(VB.worst_case_fill_ok(float("nan"), 50.0, 0.005))
        self.assertFalse(VB.worst_case_fill_ok(10.0, 50.0, float("nan")))

    def test_entry_intent_deterministik(self):
        a = auto_trader.entry_intent("ETHUSDT", 1, 10.0, 80.0)
        b = auto_trader.entry_intent("ETHUSDT", 1, 10.0, 80.0)
        self.assertEqual(a, b)
        self.assertNotEqual(a, auto_trader.entry_intent("ETHUSDT", 2, 10.0, 80.0))
        self.assertNotEqual(a, auto_trader.entry_intent("ETHUSDT", 1, 10.0, 81.0))
        self.assertNotEqual(a, auto_trader.entry_intent("ETHUSDT", 1, 11.0, 80.0))
        # HB side sözleşmesi: 1 LONG, 1 dışı SHORT; -1 de SHORT'a normalize olur
        self.assertEqual(auto_trader.entry_intent("ETHUSDT", -1, 10.0, 80.0),
                         auto_trader.entry_intent("ETHUSDT", 2, 10.0, 80.0))
        self.assertIsNone(auto_trader.entry_intent("ETHUSDT", 1, "abc", 80.0))
        self.assertIsNone(auto_trader.entry_intent("ETHUSDT", 1, 0.0, 80.0))


class TestAutoTraderHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12at-")
        self._saved = {k: os.environ.get(k) for k in
                       ("CONTROLLED_LIVE_VALIDATION", "VALIDATION_BUDGET_DB")}
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["VALIDATION_BUDGET_DB"] = os.path.join(self.tmp, "vb.db")
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

    def test_intent_tekrari_yardimci_duzeyinde_red(self):
        ok1, rid1, _ = auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")
        self.assertTrue(ok1, msg=rid1)
        ok2, rid2, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")
        self.assertFalse(ok2)                       # tekrar gönderim YOK
        self.assertIsNone(rid2)                     # yeni hak yok
        self.assertIn("INTENT-TEKRAR", why)
        st = auto_trader._VB_BUDGET.status()
        self.assertEqual(st["trades_used"], 1)      # TEK tüketim korundu

    def test_intent_yoksa_red(self):
        ok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, None)
        self.assertFalse(ok)
        self.assertIn("INTENT-YOK", why)

    def test_dolus_guard_acik_modda(self):
        ok, why = auto_trader._validation_fill_guard(49.9, 0.0)
        self.assertFalse(ok)
        self.assertIn("WORST-FILL-TAVAN", why)
        ok2, _ = auto_trader._validation_fill_guard(49.7, 0.0)
        self.assertTrue(ok2)
        ok3, _ = auto_trader._validation_fill_guard(10.0, 0.0)   # normal boyut
        self.assertTrue(ok3)

    def test_fill_asimi_sonradan_denetlenir(self):
        r = auto_trader._validation_fill_exceeded("ETHUSDT", 50.5)
        self.assertIsNotNone(r)
        self.assertIn("50.50", r)
        self.assertIsNone(auto_trader._validation_fill_exceeded("ETHUSDT", 49.9))

    def test_kapali_modda_guard_na_reserve_yine_red(self):
        os.environ.pop("CONTROLLED_LIVE_VALIDATION")
        auto_trader._VB_CFG = None
        ok, _ = auto_trader._validation_fill_guard(40.0, 0.0)
        self.assertTrue(ok)                          # guard N/A
        rok, rid, why = auto_trader._validation_reserve("ETHUSDT", 40.0, "i1")
        self.assertFalse(rok)                        # BLOK11 §10.3: reserve RED
        self.assertIn("BÜTÇE-KAPALI", why)


if __name__ == "__main__":
    unittest.main()
