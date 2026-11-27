"""GÖREV 2026-11-27 ACİL DÜZELTME regresyon testleri — ENA fill kaybı
(kusur 1) + account parser leverage (kusur 2). YALNIZCA YENİ davranışlar;
mevcut test kapsamı tekrarlanmaz. AĞ ÇAĞRISI YOK (FakeExchange +
FakeTransport + sentetik yanıtlar; gerçek Binance emri ASLA gönderilmez).

CANLI VAKA (kanıt: 2026-11-27 08:06 UTC, C281E1 ENAUSDT SELL 185):
  POST yanıtı status=FILLED döndü ama executedQty/avgPrice TAŞIMADI →
  eski _on_entry_filled 'FILL_DOĞRULANAMADI' ile çıkıyordu: defter
  pozisyonu YOK, SL YOK; emir satırı FILLED+pos_id'siz kaldığı için
  çözümleme döngüleri de bir daha ele almıyordu → borsada KORUMASIZ
  sahipsiz pozisyon. İkinci kusur: /fapi/v3/account positions[] satırı
  leverage taşımıyor → parser MISSING_REQUIRED_FIELD → hesap kapısı
  AG_BASELINE_INCOMPLETE ile tüm girişleri reddediyordu.

12 senaryo (görev spec'i):
  1  POST yanıtı executedQty/avgPrice yok + GET emir FILLED → defter
  2  FILLED emir gerçek miktarıyla deftere alınır (çözümleme döngüsü)
  3  sorgu geçici hatası → yeniden POST YOK
  4  emir UNKNOWN → körlemesine tekrar YOK
  5  kısmi fill doğru miktarla kaydedilir
  6  aynı fill iki kez işlenmez (idempotency)
  7  clientOrderId/side provenance uyuşmazlığı → sahiplik RED
  8  fill doğrulandı → SL zinciri kesin çalışır (P_VERIFIED)
  9  SL ACK yok → P_FAILED + reduce-only çıkış (korumasız kalmaz)
  10 V3 pozisyon satırı leverage'sız → geçerli, leverage=None (BİLİNMEYOR)
  11 leverage BİLİNMEYOR → kapı denetiminde görünür; bağımlı hesap None'ı
     0 SAYMAZ (fail-closed sözleşme); positionRisk kaynağı zorunlu kalır
  12 diğer zorunlu alanlar fail-closed kalır
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_exchange import BinanceApiError, ExchangeUnavailable

from tests.blok28_1_fake import (FakeExchange, FakeClock, make_canary,
                                 prime, T0)
from canary_live import CLIENT_PREFIX
from canary_account_model import (
    InvalidFieldError, parse_account_positions, parse_position_risk,
    R_MISSING_FIELD, R_INVALID_NUMERIC)
from canary_account_risk_gate import AccountRiskGate
from tests.test_canary_account_adapter import FakeTransport, _full_responses
from tests.test_canary_account_model import (
    FIX_ACCOUNT_V3, FIX_POSITION_RISK, _meta, T_REQ)
from tests.test_canary_account_model import EP_V3_ACCOUNT, EP_POS_RISK

BAR = 4 * 3600 * 1000
STRIP_KEYS = ("executedQty", "avgPrice", "cumQuote", "fills")


def _entry_posts(ex):
    return [c for c in ex.post_calls
            if c["method"] == "new_order" and not c["reduce_only"]]


def _exit_posts(ex):
    return [c for c in ex.post_calls
            if c["method"] == "new_order" and c["reduce_only"]]


def _algo_posts(ex):
    return [c for c in ex.post_calls if c["method"] == "new_algo_order"]


def _risk_events(lc, kind):
    rows = lc._db_execute("SELECT kind FROM risk_events WHERE kind=?",
                          (kind,)).fetchall()
    return rows


class EnaRemediationBase(unittest.TestCase):
    """Ortak kurulum: FakeExchange + POST-yanıtı-eksik simülasyonu.

    _install_strip: ENTRY POST yanıtlarından fill alanlarını söker (borsa
    kaydı TAM kalır — sorgu gerçek veriyi döndürür; tam ENA vakası).
    _install_query_override: query_order sonucunu değiştirir/hata fırlatır
    (provenance + geçici hata senaryoları)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock(T0 + 100 * BAR)
        self.ex = FakeExchange(clock_ms=self.clock)
        self.lc = make_canary(self.tmp.name, self.ex)
        prime(self.lc)
        self.query_fail = False
        self.query_mutate = None      # callable: resp → resp (veya None)

    def _install_strip(self):
        orig = self.ex.new_order

        def new_order(symbol, side, type_, *a, **kw):
            resp = orig(symbol, side, type_, *a, **kw)
            cid = kw.get("client_order_id")
            if cid and cid.startswith(f"{CLIENT_PREFIX}E"):
                resp = {k: v for k, v in resp.items() if k not in STRIP_KEYS}
            return resp

        self.ex.new_order = new_order

    def _install_query_override(self):
        orig = self.ex.query_order

        def query_order(symbol, order_id=None, client_order_id=None):
            if self.query_fail:
                raise ExchangeUnavailable("simüle geçici sorgu hatası")
            resp = orig(symbol, order_id, client_order_id)
            # gerçek GET /fapi/v1/order fills[] DÖNDÜRMEZ — sorgu yolu
            # komisyon bilinmez (POST yanıtı bilir); simülasyonu buna
            # eşitle
            resp = {k: v for k, v in resp.items() if k != "fills"}
            if self.query_mutate is not None:
                resp = self.query_mutate(resp)
            return resp

        self.ex.query_order = query_order

    def _open_ena_style(self, symbol="ETHUSDT", direction=-1):
        """ENA vakası: POST FILLED ama alan yok; sorgu DA başarısız →
        FILL_UNCONFIRMED. Dönen rec + emir satırı FILLED+pos_id'sizdir."""
        self._install_strip()
        self._install_query_override()
        self.query_fail = True
        recs = self.lc.try_entry(symbol, direction, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        return recs[0]


# ---------------------------------------------------------------------------
# KUSUR 1 — fill doğrulama (senaryo 1-9)
# ---------------------------------------------------------------------------

class TestFillConfirmFromExchange(EnaRemediationBase):
    """Senaryo 1 + 8: POST alanları yok → SALT-OKUNUR sorgu ile doğrula →
    defter + SL zinciri kesin çalışır; yeniden POST YOK."""

    def test_post_missing_fields_confirmed_by_query_ledger_and_sl(self):
        self._install_strip()
        self._install_query_override()   # gerçek GET: fills YOK
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        rec = recs[0]
        # fill SORGUDAN doğrulandı → normal FILLED akışı
        self.assertEqual(rec["result"], "FILLED")
        self.assertIn("pos_id", rec)
        pos_id = rec["pos_id"]
        # defter pozisyonu GERÇEK miktar/fiyatla açıldı (FakeExchange
        # kaydından: 100 USDT hedef → 0.99 lot @ 100.0)
        pos = self.lc.rm._pos(pos_id)
        self.assertAlmostEqual(pos["units"], 0.99, places=6)
        self.assertAlmostEqual(pos["entry_px"], 100.0, places=6)
        self.assertEqual(pos["direction"], -1)
        # emir satırı pos_id'ye BAĞLANDI (artık kayıp fill mümkün değil)
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertEqual(o["pos_id"], pos_id)
        # senaryo 8: SL zinciri ÇALIŞTI — STOP_MARKET borsada açık +
        # doğrulandı (P_VERIFIED); ACK'sız 'korunuyor' raporlanmaz
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"], "PROTECTED")
        a = self.ex.algos[f"C281P{pos_id}"]
        self.assertEqual(a["side"], "BUY")          # SHORT → BUY stop
        self.assertEqual(a["closePosition"], "true")
        # komisyon bilinmiyor notu (sorgu yolu fills taşımaz)
        self.assertTrue(_risk_events(self.lc, "FILL_CONFIRMED_FROM_QUERY"))
        # ASLA yeniden POST edilmedi
        self.assertEqual(len(_entry_posts(self.ex)), 1)

    def test_avg_price_from_cumquote_when_missing(self):
        # sorgu yanıtı avgPrice taşımıyor ama cumQuote var → türetilir
        self._install_strip()
        self._install_query_override()

        def drop_avg(resp):
            r = dict(resp)
            r.pop("avgPrice", None)
            return r

        self.query_mutate = drop_avg
        recs = self.lc.try_entry("ETHUSDT", 1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        self.assertEqual(recs[0]["result"], "FILLED")
        pos = self.lc.rm._pos(recs[0]["pos_id"])
        self.assertAlmostEqual(pos["entry_px"], 100.0, places=6)
        self.assertAlmostEqual(pos["units"], 0.99, places=6)


class TestFilledUnprocessedResolution(EnaRemediationBase):
    """Senaryo 2: FILLED+pos_id'siz emir (ENA son durumu) çözümleme
    döngüsünde YENİDEN ele alınır → geç fill deftere işlenir."""

    def test_risk_loop_resolves_filled_without_pos_id(self):
        rec = self._open_ena_style()
        # ENA durumu kanıtla: emir FILLED, defter BOŞ, borsada pozisyon VAR
        self.assertIn("FILL_UNCONFIRMED", rec["result"])
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")
        self.assertIsNone(o["pos_id"])
        self.assertEqual(self.lc.rm.open_positions(), [])
        self.assertAlmostEqual(
            abs(self.ex.positions["ETHUSDT"]["positionAmt"]), 0.99)
        # sorgu düzeldi → risk döngüsü FILLED+pos_id'siz emri ele alır
        self.query_fail = False
        self.lc.risk_tick()
        positions = self.lc.rm.open_positions()
        self.assertEqual(len(positions), 1)
        pos_id = positions[0]["pos_id"]
        self.assertAlmostEqual(positions[0]["units"], 0.99, places=6)
        self.assertEqual(self.lc._order_row("C281E1")["pos_id"], pos_id)
        # geç fill kaydı (denetim izi)
        log = open(os.path.join(self.tmp.name, "live_orders.jsonl")).read()
        self.assertIn("LATE_ENTRY_FILL", log)
        # SL zinciri de çalıştı
        self.assertEqual(self.lc._live_pos_row(pos_id)["protection_status"],
                         "PROTECTED")

    def test_resolve_entry_order_direct_path(self):
        self._open_ena_style()
        self.query_fail = False
        self.lc._resolve_entry_order("C281E1", "ETHUSDT")
        positions = self.lc.rm.open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(self.lc._order_row("C281E1")["pos_id"],
                         positions[0]["pos_id"])

    def test_resolution_serialized_under_entry_lock(self):
        """Bağımsız inceleme C-1: çözümleme risk iş parçacığından çağrılır;
        try_entry'in _entry_lock'u BAŞKA iş parçacığında tutuluyorsa
        çözümleme BEKLER — aynı fill iki kez deftere yazılamaz (çift
        pozisyon + çift koruma yarışı kapalı)."""
        self._open_ena_style()
        self.query_fail = False
        lock_taken = threading.Event()
        release = threading.Event()

        def holder():                      # try_entry'i temsil eder
            with self.lc._entry_lock:
                lock_taken.set()
                release.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(lock_taken.wait(timeout=5))
        done = []

        def resolver():                    # risk_tick'i temsil eder
            self.lc._resolve_entry_order("C281E1", "ETHUSDT")
            done.append(True)

        r = threading.Thread(target=resolver)
        r.start()
        r.join(timeout=0.5)
        self.assertFalse(done)             # kilit dolu → hâlâ bekliyor
        release.set()
        t.join(timeout=5)
        r.join(timeout=5)
        self.assertTrue(done)              # kilit boşalınca tamamlandı
        self.assertEqual(len(self.lc.rm.open_positions()), 1)

    def test_filled_unprocessed_non_terminal_query_keeps_status(self):
        """Bağımsız inceleme L-1: defter FILLED iken sorgu terminal
        OLMAYAN durum döndürürse satır NEW'a ezilmez — yoksa emir hiçbir
        döngüce yeniden seçilmezdi (fill kaybı geri dönerdi)."""
        self._open_ena_style()
        self.query_fail = False

        def as_new(resp):
            r = dict(resp)
            r["status"] = "NEW"
            return r

        self.query_mutate = as_new
        self.lc._resolve_entry_order("C281E1", "ETHUSDT")
        self.assertEqual(self.lc._order_row("C281E1")["status"], "FILLED")
        self.assertEqual(self.lc.rm.open_positions(), [])
        self.assertTrue(_risk_events(self.lc, "FILLED_STATUS_CONTRADICTION"))


class TestNoBlindRetry(EnaRemediationBase):
    """Senaryo 3 + 4: sorgu geçici hatası / UNKNOWN emir → körlemesine
    tekrar POST ASLA yok (§2.3)."""

    def test_transient_query_error_no_duplicate_post(self):
        self._open_ena_style()
        n_posts_before = len(_entry_posts(self.ex))
        self.assertEqual(n_posts_before, 1)
        # sorgu hâlâ başarısız → çözümleme sessizce bırakır
        self.lc._resolve_entry_order("C281E1", "ETHUSDT")
        self.assertEqual(len(_entry_posts(self.ex)), 1)   # yeniden POST YOK
        o = self.lc._order_row("C281E1")
        self.assertEqual(o["status"], "FILLED")           # durum değişmedi
        self.assertIsNone(o["pos_id"])
        self.assertEqual(self.lc.rm.open_positions(), [])

    def test_unknown_order_transient_error_leaves_state(self):
        self.lc._upsert_order("C281E9", "ENTRY", "ETHUSDT", "UNKNOWN",
                              params={"side": "SELL", "quantity": "1",
                                      "type": "MARKET",
                                      "decision_id": "BR9:ETHUSDT:-1"})
        self._install_query_override()
        self.query_fail = True
        self.lc._resolve_entry_order("C281E9", "ETHUSDT")
        self.assertEqual(self.lc._order_row("C281E9")["status"], "UNKNOWN")
        self.assertEqual(_entry_posts(self.ex), [])       # POST YOK

    def test_unknown_order_not_on_exchange_cleans_to_canceled(self):
        # -2011: emir borsada hiç yok (INTENT aşaması) → CANCELED'a temizle
        self.lc._upsert_order("C281E9", "ENTRY", "ETHUSDT", "UNKNOWN",
                              params={"side": "SELL", "quantity": "1",
                                      "type": "MARKET",
                                      "decision_id": "BR9:ETHUSDT:-1"})
        self.lc._resolve_entry_order("C281E9", "ETHUSDT")   # -2011 doğal
        self.assertEqual(self.lc._order_row("C281E9")["status"], "CANCELED")
        self.assertEqual(_entry_posts(self.ex), [])

    def test_filled_unprocessed_unknown_on_exchange_not_cleaned(self):
        # defter FILLED ama borsa emri bilinmiyor → CANCELED'a TEMİZLENMEZ
        # (gerçek fill saklanabilir); alarm + durum korunur
        self.lc._upsert_order("C281E8", "ENTRY", "ETHUSDT", "FILLED",
                              params={"side": "SELL", "quantity": "1",
                                      "type": "MARKET",
                                      "decision_id": "BR8:ETHUSDT:-1"})
        self.lc._resolve_entry_order("C281E8", "ETHUSDT")   # -2011 doğal
        self.assertEqual(self.lc._order_row("C281E8")["status"], "FILLED")
        self.assertTrue(_risk_events(
            self.lc, "FILLED_ORDER_MISSING_ON_EXCHANGE"))
        self.assertEqual(_entry_posts(self.ex), [])


class TestPartialFill(EnaRemediationBase):
    """Senaryo 5: kısmi fill GERÇEK dolum miktarıyla deftere işlenir."""

    def test_partial_fill_ledgered_with_executed_qty(self):
        self.ex.partial_fill_qty["C281E1"] = 0.4    # hedef 0.99'nın yarısı
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        self.assertIn("risk döngüsü izleyecek", recs[0]["result"])
        self.assertEqual(self.lc.rm.open_positions(), [])
        # çözümleme: sorguda kalan EXPIRED, executedQty=0.4 → geç fill
        self.lc._resolve_entry_order("C281E1", "ETHUSDT")
        positions = self.lc.rm.open_positions()
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0]["units"], 0.4, places=6)
        self.assertEqual(self.lc._order_row("C281E1")["pos_id"],
                         positions[0]["pos_id"])
        # koruma GERÇEK miktar üzerinden (closePosition → tüm pozisyon)
        self.assertEqual(self.lc._live_pos_row(positions[0]["pos_id"])
                         ["protection_status"], "PROTECTED")


class TestFillIdempotency(EnaRemediationBase):
    """Senaryo 6: aynı fill ikinci kez (yinelenen olay / döngü / restart)
    işlenmez — çift pozisyon/emir satırı OLUŞMAZ."""

    def test_second_process_is_duplicate_noop(self):
        self._open_ena_style()
        self.query_fail = False
        self.lc._resolve_entry_order("C281E1", "ETHUSDT")
        positions = self.lc.rm.open_positions()
        self.assertEqual(len(positions), 1)
        pos_id = positions[0]["pos_id"]
        resp = self.ex.orders["C281E1"]
        # aynı fill yeniden beslenirse → DUPLICATE_FILL, defter değişmez
        out = self.lc._on_entry_filled("C281E1", resp, "ETHUSDT", -1,
                                       self.lc._now())
        self.assertIn("DUPLICATE_FILL", out["result"])
        self.assertEqual(out["pos_id"], pos_id)
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        # çözümleme döngüsü de artık dokunmaz (FILLED+pos_id'li)
        self.lc._resolve_entry_order("C281E1", "ETHUSDT")
        self.assertEqual(len(self.lc.rm.open_positions()), 1)
        rows = self.lc._db_execute(
            "SELECT COUNT(*) FROM live_positions WHERE symbol='ETHUSDT'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(len(_entry_posts(self.ex)), 1)


class TestProvenanceMismatch(EnaRemediationBase):
    """Senaryo 7: sahiplik kanıtı (clientOrderId/symbol/side birlikte)
    sağlanamazsa pozisyon BENİMSNENMEZ — yalnız C281 öneki kanıt DEĞİL."""

    def test_side_mismatch_refuses_ownership(self):
        self._install_strip()
        self._install_query_override()

        def flip_side(resp):
            r = dict(resp)
            r["side"] = "BUY" if r.get("side") == "SELL" else "SELL"
            return r

        self.query_mutate = flip_side
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        # sahiplik RED → defter YOK (borsadaki pozisyon drift kontrolüne
        # kalır: UNOWNED_POSITION latch'i girişleri fail-closed bloklar)
        self.assertIn("FILL_UNCONFIRMED", recs[0]["result"])
        self.assertEqual(self.lc.rm.open_positions(), [])
        self.assertIsNone(self.lc._order_row("C281E1")["pos_id"])
        self.assertTrue(_risk_events(self.lc, "FILL_PROVENANCE_MISMATCH"))
        log = open(os.path.join(self.tmp.name, "live_orders.jsonl")).read()
        self.assertIn("FILL_PROVENANCE_MISMATCH", log)
        # yeniden POST da YOK
        self.assertEqual(len(_entry_posts(self.ex)), 1)

    def test_symbol_mismatch_refuses_ownership(self):
        self._install_strip()
        self._install_query_override()

        def wrong_symbol(resp):
            r = dict(resp)
            r["symbol"] = "SOLUSDT"
            return r

        self.query_mutate = wrong_symbol
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        self.assertIn("FILL_UNCONFIRMED", recs[0]["result"])
        self.assertEqual(self.lc.rm.open_positions(), [])

    def test_client_order_id_mismatch_refuses_ownership(self):
        self._install_strip()
        self._install_query_override()

        def wrong_cid(resp):
            r = dict(resp)
            r["clientOrderId"] = "C281E999"
            return r

        self.query_mutate = wrong_cid
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        self.assertIn("FILL_UNCONFIRMED", recs[0]["result"])
        self.assertEqual(self.lc.rm.open_positions(), [])

    def test_missing_client_order_id_refuses_ownership(self):
        """Bağımsız inceleme M-1: kanıt alanı YOKSA da kanıt YOKTUR
        (fail-closed — 'yoksa atla' değil)."""
        self._install_strip()
        self._install_query_override()

        def drop_cid(resp):
            r = dict(resp)
            r.pop("clientOrderId", None)
            return r

        self.query_mutate = drop_cid
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        self.assertIn("FILL_UNCONFIRMED", recs[0]["result"])
        self.assertEqual(self.lc.rm.open_positions(), [])
        self.assertTrue(_risk_events(self.lc, "FILL_PROVENANCE_MISMATCH"))

    def test_corrupted_params_side_refuses_adoption(self):
        """Bağımsız inceleme M-1: defter params'ı bozuksa (side yok) yön
        TAHMİN EDİLMEZ — BUY girişi SHORT benimsenip stopun ters tarafa
        konması engellenir."""
        self.lc._upsert_order("C281E7", "ENTRY", "ETHUSDT", "FILLED",
                              params={"quantity": "1", "type": "MARKET"})
        resp = {"status": "FILLED", "symbol": "ETHUSDT",
                "clientOrderId": "C281E7", "side": "SELL",
                "executedQty": "0.5", "avgPrice": "100", "orderId": 99}
        self.lc._process_late_entry_fill("C281E7", resp, "ETHUSDT")
        self.assertEqual(self.lc.rm.open_positions(), [])
        self.assertTrue(_risk_events(self.lc, "FILL_PROVENANCE_MISMATCH"))


class TestProtectionFailureAfterConfirmedFill(EnaRemediationBase):
    """Senaryo 9: fill doğrulandı ama SL yerleştirilemedi → pozisyon
    'korunuyor' SAYILMAZ: P_FAILED + alarm + reduce-only çıkış (korumasız
    pozisyon bırakılmaz)."""

    def test_sl_failure_risk_exit_not_unprotected(self):
        self._install_strip()
        self.ex.algo_fail["C281P1"] = BinanceApiError(
            -2021, "Order would immediately trigger.")
        recs = self.lc.try_entry("ETHUSDT", -1, 100.0,
                                 T0 + 400 * BAR, self.lc._now())
        rec = recs[0]
        self.assertEqual(rec["result"], "FILLED")
        pos_id = rec["pos_id"]
        lp = self.lc._live_pos_row(pos_id)
        self.assertEqual(lp["protection_status"],
                         "PROTECTION_FAILED_EXIT_PENDING")
        # çıkış zinciri başlatıldı (FakeExchange anında doldurur →
        # EXIT_CONFIRMED; canlıda X_PENDING'den ilerler) — kritik olan:
        # korumasız açık pozisyon BIRAKILMAZ
        self.assertIn(lp["exit_status"], ("EXIT_PENDING", "EXIT_CONFIRMED"))
        self.assertEqual(self.lc.rm._pos(pos_id)["status"], "CLOSED")
        # kontrolsüz retry YOK: tek algo POST, tek reduce-only çıkış
        self.assertEqual(len(_algo_posts(self.ex)), 1)
        self.assertEqual(len(_exit_posts(self.ex)), 1)


# ---------------------------------------------------------------------------
# KUSUR 2 — account parser leverage (senaryo 10-12)
# ---------------------------------------------------------------------------

V3_ROW_NO_LEVERAGE = {"symbol": "ENAUSDT", "positionSide": "BOTH",
                      "positionAmt": "-185", "entryPrice": "0.1067",
                      "updateTime": T_REQ, "isolated": False,
                      "unrealizedProfit": "-0.42"}


class TestV3LeverageOptional(unittest.TestCase):
    """Senaryo 10: V3 account positions[] satırı leverage TAŞIMAZ →
    parser ÇÖKMEZ; leverage=None (BİLİNMEYOR; 0 veya keyfi sabit DEĞİL)."""

    def test_v3_row_without_leverage_parses_to_none(self):
        snap = parse_account_positions(
            {"positions": [dict(V3_ROW_NO_LEVERAGE)]},
            _meta("account", EP_V3_ACCOUNT))
        self.assertEqual(len(snap.entries), 1)
        e = snap.entries[0]
        self.assertIsNone(e.leverage)                 # BİLİNMEYOR
        self.assertAlmostEqual(e.positionAmt, -185.0)
        self.assertEqual(e.symbol, "ENAUSDT")

    def test_v3_row_with_leverage_still_parses(self):
        row = dict(V3_ROW_NO_LEVERAGE, leverage="20")
        snap = parse_account_positions(
            {"positions": [row]}, _meta("account", EP_V3_ACCOUNT))
        self.assertEqual(snap.entries[0].leverage, 20)

    def test_v3_leverage_zero_still_invalid(self):
        # alan VARSA katı doğrulama sürer: 0 geçersiz (minimum 1)
        row = dict(V3_ROW_NO_LEVERAGE, leverage="0")
        with self.assertRaises(InvalidFieldError) as cm:
            parse_account_positions(
                {"positions": [row]}, _meta("account", EP_V3_ACCOUNT))
        self.assertEqual(cm.exception.reason, R_INVALID_NUMERIC)

    def test_position_risk_leverage_still_mandatory(self):
        # kesin kaynak positionRisk'tir: orada alan ZORUNLU kalır
        row = {k: v for k, v in dict(FIX_POSITION_RISK[0]).items()
               if k != "leverage"}
        with self.assertRaises(InvalidFieldError) as cm:
            parse_position_risk([row], _meta("positionRisk", EP_POS_RISK))
        self.assertEqual(cm.exception.reason, R_MISSING_FIELD)
        self.assertIn("leverage", cm.exception.path)


class TestGateUnknownLeverageExposure(unittest.TestCase):
    """Senaryo 11: leverage BİLİNMEYEN → kapı denetim kaydında görünür;
    bağımlı hesap None'ı 0 SAYMAZ (fail-closed sözleşme). V3 eksik alan
    artık AG_BASELINE_INCOMPLETE ÜRETMEZ (canlı vakadaki LDO/MARSCOIN
    reddinin kök nedeni)."""

    NOW = T_REQ + 2_000
    BRACKETS = {"BTCUSDT": [
        {"bracket": 1, "floor": 0.0, "cap": 50_000.0, "ratio": 0.004,
         "cum": 0.0, "max_leverage": 20}]}

    def _gate(self, v3_positions):
        resp = _full_responses()
        resp["/fapi/v3/account"] = dict(
            FIX_ACCOUNT_V3, positions=v3_positions)
        # açık emirler C281 sahipliğinde (aksi AG_UNKNOWN_OWNER_ORDER —
        # bu testin konusu değil; B.1 testlerindeki _pass_responses deseni)
        resp["/fapi/v1/openOrders"] = [
            {"orderId": 1001, "symbol": "BTCUSDT", "status": "NEW",
             "clientOrderId": "C281x1", "price": "220.0", "avgPrice": "0",
             "origQty": "0.5", "executedQty": "0", "cumQuote": "0",
             "timeInForce": "GTC", "type": "LIMIT", "side": "BUY",
             "stopPrice": "0", "updateTime": T_REQ,
             "workingType": "CONTRACT_PRICE", "reduceOnly": False,
             "closePosition": False, "positionSide": "BOTH"}]
        resp["/fapi/v1/openAlgoOrders"] = [
            {"algoId": 9001, "symbol": "BTCUSDT", "algoType": "CONDITIONAL",
             "side": "SELL", "algoStatus": "NEW", "clientAlgoId": "C281a1",
             "triggerPrice": "61234.5", "totalAmt": "0",
             "workingType": "MARK_PRICE", "positionSide": "BOTH",
             "updateTime": T_REQ}]
        return AccountRiskGate(
            FakeTransport(resp), brackets_by_symbol=self.BRACKETS,
            max_age_ms=5_000, margin_buffer_usdt=50.0,
            clock_ms=lambda: self.NOW)

    def test_v3_without_leverage_no_parse_error_gate_passes(self):
        positions = [
            {"symbol": "BTCUSDT", "positionSide": "BOTH",
             "positionAmt": "0.002", "entryPrice": "96000.0",
             "updateTime": T_REQ, "isolated": False,
             "unrealizedProfit": "-1.20"},            # leverage YOK
            {"symbol": "DOGEUSDT", "positionSide": "BOTH",
             "positionAmt": "-500", "entryPrice": "0.32",
             "updateTime": T_REQ, "isolated": True,
             "unrealizedProfit": "-5.0"}]
        d = self._gate(positions).evaluate(
            symbol="BTCUSDT", direction=1, notional=100.0,
            rm=None, now_ms=self.NOW)
        # kök neden düzeltmesi: PARSE_ERROR/AG_BASELINE_INCOMPLETE YOK
        self.assertNotIn("AG_BASELINE_INCOMPLETE",
                         " ".join(d.reasons))
        self.assertTrue(d.passed, d.reasons)
        # BİLİNMEYEN kaldıraç denetimde GÖRÜNÜR (sessiz geçiş yok)
        self.assertEqual(d.audit.get("positions_with_unknown_leverage"),
                         ["BTCUSDT", "DOGEUSDT"])
        self.assertTrue(any("AG_UNKNOWN_LEVERAGE" in w for w in d.warnings))

    def test_position_risk_leverage_missing_blocks_gate(self):
        """Kesin kaynak (positionRisk) kaldıraç taşımıyorsa parse ÇÖKER →
        baseline inkomplet → kapı BLOCK. Leverage UNKNOWN davranışı kapıyı
        yanlışlıkla PASS'e getirmez (V3'deki Optional yalnız V3 kaynağına
        aittir; zorunlu kaynak zorunlu KALIR)."""
        resp = _full_responses()
        resp["/fapi/v3/account"] = dict(
            FIX_ACCOUNT_V3,
            positions=[{"symbol": "BTCUSDT", "positionSide": "BOTH",
                        "positionAmt": "0.002", "entryPrice": "96000.0",
                        "updateTime": T_REQ, "isolated": False,
                        "unrealizedProfit": "-1.20"}])
        resp["/fapi/v2/positionRisk"] = [
            {k: v for k, v in dict(FIX_POSITION_RISK[0]).items()
             if k != "leverage"}]
        resp["/fapi/v1/openOrders"] = []
        resp["/fapi/v1/openAlgoOrders"] = []
        d = AccountRiskGate(
            FakeTransport(resp), brackets_by_symbol=self.BRACKETS,
            max_age_ms=5_000, margin_buffer_usdt=50.0,
            clock_ms=lambda: self.NOW).evaluate(
            symbol="BTCUSDT", direction=1, notional=100.0,
            rm=None, now_ms=self.NOW)
        self.assertFalse(d.passed)
        self.assertIn("AG_BASELINE_INCOMPLETE", " ".join(d.reasons))
        self.assertIn("PARSE_ERROR", " ".join(d.reasons))


class TestOtherMandatoryFieldsFailClosed(unittest.TestCase):
    """Senaryo 12: leverage dışındaki zorunlu alanlar fail-closed KALIR."""

    def _v3(self, row):
        return parse_account_positions(
            {"positions": [row]}, _meta("account", EP_V3_ACCOUNT))

    def test_missing_position_amt_raises(self):
        row = {k: v for k, v in dict(V3_ROW_NO_LEVERAGE).items()
               if k != "positionAmt"}
        with self.assertRaises(InvalidFieldError) as cm:
            self._v3(row)
        self.assertEqual(cm.exception.reason, R_MISSING_FIELD)
        self.assertIn("positionAmt", cm.exception.path)

    def test_missing_symbol_raises(self):
        row = {k: v for k, v in dict(V3_ROW_NO_LEVERAGE).items()
               if k != "symbol"}
        with self.assertRaises(InvalidFieldError) as cm:
            self._v3(row)
        self.assertEqual(cm.exception.reason, R_MISSING_FIELD)

    def test_missing_entry_price_raises(self):
        row = {k: v for k, v in dict(V3_ROW_NO_LEVERAGE).items()
               if k != "entryPrice"}
        with self.assertRaises(InvalidFieldError) as cm:
            self._v3(row)
        self.assertEqual(cm.exception.reason, R_MISSING_FIELD)

    def test_missing_update_time_raises(self):
        row = {k: v for k, v in dict(V3_ROW_NO_LEVERAGE).items()
               if k != "updateTime"}
        with self.assertRaises(InvalidFieldError) as cm:
            self._v3(row)
        self.assertIn(cm.exception.reason,
                      (R_MISSING_FIELD, "INVALID_TIMESTAMP"))

    def test_positions_array_missing_raises(self):
        with self.assertRaises(InvalidFieldError) as cm:
            parse_account_positions({}, _meta("account", EP_V3_ACCOUNT))
        self.assertEqual(cm.exception.reason, R_MISSING_FIELD)


if __name__ == "__main__":
    unittest.main()
