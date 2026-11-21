"""BLOK12 devam-C: 50 USDT sınırının GARANTİ SINIRI açık ve zorunlu.

Gerçek doluş fiyatı önceden bilinemez (piyasa emri); dürüst sözleşme:
(1) planlama anında ayrılan en-kötü notional = plan × (1+drift), drift =
    max(gerçek slipaj, taban) — TAVAN ancak gerçek doluş kayması ≤ drift ise
    ihlal edilmez (garanti budur, fazlası DEĞİL);
(2) bu sınır pending kayda YAZILIR (cap_per_trade_usdt, worst_fill_usdt);
(3) dolum tespitinde GERÇEK filled_amount_quote hem tavan hem ayrılan
    en-kötüyle kıyaslanır: tavan-üstü → TAVAN-İHLALİ; tavan-içi ama en-kötü
    üstü → GARANTİ-SINIRI-AŞIMI — İKİSİ DE yeni girişleri kilitler
    (fail-closed); bozuk envelope doğrulanamazsa da kilit (fail-closed);
(4) garanti-dışı kalan tek şey drift-ötesi kaymanın TESBIT GECIKMESİDİR:
    en fazla bir dolum-sonraki tick.
Ağ YOK. conftest bütçeyi 2/50/100 açık kurar.
"""
import contextlib
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_trader                                      # noqa: E402
from test_inc13_live_wiring import (FakeBinance, FakeHB,  # noqa: E402
                                    LiveWiringBase, _RiskOK, _coordinator)

_ENV_KEYS = ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
             "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
             "MAX_VALIDATION_TOTAL_NOTIONAL_USDT",
             "VALIDATION_APPROVAL_DB", "OPERATOR_APPROVAL_TOKEN_FILE",
             "VALIDATION_WORST_FILL_DRIFT_PCT")


class TestEnvelope(unittest.TestCase):
    def test_envelope_tavan_ve_drift_tabani(self):
        env = auto_trader._validation_fill_envelope(40.0, 0.0)
        self.assertIsNotNone(env)
        cap, drift, worst = env
        self.assertEqual(cap, 50.0)
        self.assertAlmostEqual(drift, 0.005)
        self.assertAlmostEqual(worst, 40.2)

    def test_envelope_gercek_slipaj_drift_olur(self):
        cap, drift, worst = auto_trader._validation_fill_envelope(40.0, 0.10)
        self.assertAlmostEqual(drift, 0.10)
        self.assertAlmostEqual(worst, 44.0)


class _HB:
    def __init__(self, filled, extra=None):
        self._f = filled
        self._extra = extra or {}

    def get_executor(self, eid):
        d = {"filled_amount_quote": self._f, "error_count": 0}
        d.update(self._extra)
        return d


class TestFillAuditAgainstGuarantee(unittest.TestCase):
    """Denetim: GERÇEK doluş notional'ı hem tavana hem ayrılan en-kötüye
    karşı; her aşım giriş kilidi üretir."""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="b12c-"))

    def _seed(self, worst=None, cap=50.0):
        ctx = {"symbol": "BTCUSDT", "side": 1, "amount": 0.001,
               "sl_pct": 2.0, "tp_pct": 3.0, "price": 100.0, "dyn_lev": 5,
               "target_notional": 40.0, "res_id": None,
               "deadline": time.time() + 300, "opened_at": time.time(),
               "entry_client_order_ids": [], "entry_client_order_id": None}
        if worst is not None:
            ctx["worst_fill_usdt"] = worst
        if cap is not None:
            ctx["cap_per_trade_usdt"] = cap

        def mut(pending, now):
            pending["eid-c"] = dict(ctx)
            return pending
        ok, _ = auto_trader._pending_transaction(mut)
        self.assertTrue(ok)

    def _run(self, filled, extra=None):
        fin = []
        with mock.patch.object(auto_trader, "_finalize_and_confirm",
                               lambda *a, **k: fin.append(1)):
            out = auto_trader._process_pending_fills(
                None, _HB(filled, extra), None, None, None)
        return out, fin

    def test_garanti_siniri_asimi_tavani_asmadan_kilitler(self):
        self._seed(worst=40.2)
        self._run("45.0")            # tavan(50) İÇİ, en-kötü(40.2) ÜSTÜ
        health = dict(auto_trader._ENTRY_HEALTH)
        self.assertIn("validation_fill_cap", health)
        self.assertIn("GARANTİ", health["validation_fill_cap"])

    def test_tavan_asimi_mevcut_mesaji_korunur(self):
        self._seed(worst=40.2)
        self._run("55.0")
        health = dict(auto_trader._ENTRY_HEALTH)
        self.assertIn("validation_fill_cap", health)
        self.assertIn("GERÇEK fill notional", health["validation_fill_cap"])

    def test_envelopesiz_kayit_yalniz_tavan_denetimi(self):
        # Eski kayıtlar (saha uyumu): alanlar yoksa tek denetim tavan — ASLA
        # sessiz geçiş değil.
        self._seed(worst=None, cap=None)
        self._run("55.0")
        self.assertIn("validation_fill_cap", dict(auto_trader._ENTRY_HEALTH))

    def test_bozuk_envelope_fail_closed_kilitler(self):
        self._seed(worst="bulut-bulut")
        self._run("30.0")
        health = dict(auto_trader._ENTRY_HEALTH)
        self.assertIn("validation_fill_cap", health)
        self.assertIn("doğrulanamadı", health["validation_fill_cap"])

    def test_garanti_icinde_temiz_dolum_kilitlemez(self):
        self._seed(worst=40.2)
        _out, fin = self._run("40.0")   # out sayaçları finalize İÇİNDE artar
        self.assertEqual(len(fin), 1)   # finalize çağrıldı (dolum yolu açık)
        self.assertNotIn("validation_fill_cap", dict(auto_trader._ENTRY_HEALTH))


class TestEnvelopePersistedOnOpen(LiveWiringBase):
    """Üretim yolu: successful open'ın pending kaydı garanti zarfını TAŞIR."""

    @contextlib.contextmanager
    def _vb(self):
        old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        old_dir, old_bl = auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE
        saved = (auto_trader._VB_CFG, auto_trader._VB_BUDGET)
        tmp = Path(self.tmp)
        auto_trader.STATE_DIR = tmp
        auto_trader.BLACKLIST_FILE = tmp / ".bl.json"
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["MAX_VALIDATION_TRADES"] = "2"
        os.environ["MAX_VALIDATION_POSITION_NOTIONAL_USDT"] = "50"
        os.environ["MAX_VALIDATION_TOTAL_NOTIONAL_USDT"] = "100"
        os.environ["VALIDATION_APPROVAL_DB"] = str(tmp / "gate.db")
        os.environ.pop("OPERATOR_APPROVAL_TOKEN_FILE", None)
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None
        try:
            yield
        finally:
            auto_trader._VB_CFG, auto_trader._VB_BUDGET = saved
            auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE = old_dir, old_bl
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_open_pending_kayda_zarf_yazar(self):
        with self._vb():
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            res = auto_trader.open_coin(binance, hb, dict(self._coin()),
                                        dry_run=False, risk=_RiskOK(),
                                        reserver=None, coordinator=cc)
            self.assertTrue(res)
            self.assertTrue(auto_trader._PENDING_FILLS)
            ctx = next(iter(auto_trader._PENDING_FILLS.values()))
            self.assertEqual(ctx.get("cap_per_trade_usdt"), 50.0)
            w = ctx.get("worst_fill_usdt")
            self.assertIsInstance(w, float)
            # zarf sözleşmesi: worst = final×(1+drift); final ≤ hedef (trim
            # küçültür), drift ≥ taban 0.005 → worst ≤ hedef×1.005 (geniş
            # üst sınır; drift slipajla büyüyebilir ama guard geçtiği için
            # worst ≤ tavan) ve worst > 0.
            self.assertGreater(w, 0.0)
            self.assertLessEqual(w, 50.0)              # guard geçti ⇒ ≤ tavan
            self.assertLessEqual(w, float(ctx["target_notional"]) * 1.005
                                 + 1e-6)
            # tutarlılık: worst/1.005 = final en-kötü hesabının hamlesi —
            # hedefle aynı büyüklük mertebesinde olmalı (yarış-bağımlı değil):
            self.assertGreaterEqual(w / 1.005, 1.0)


if __name__ == "__main__":
    unittest.main()
