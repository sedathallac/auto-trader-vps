"""BLOK12 §4 — rate-limit (429/418) gardı + KAPALI MUM sözleşmesi testleri.

Gerçek ağ/emir YOK. Sözleşme:
  * 429 → Retry-After varsa O, yoksa 60s taban; artan grevlerle üstel backoff,
    tavan 3600s. 418 → 900s (Retry-After öncelikli). Cooldown bitince açılır.
  * Her Binance REST yanıtı `_api_response`'tan geçer: 429/418 not edilir,
    2xx grevleri sıfırlar; gövde çözümlenemezse hata sözlüğü (exception değil).
  * Emir POST'u KÖR RETRY EDİLMEZ: urllib3 Retry allowed_methods yalnız GET ve
    429/418 status_forcelist'te YOK; api_guard da hiçbir isteği yeniden
    göndermez (sahte session çağrı sayısıyla kanıtlanır).
  * Strateji verisi YALNIZ kapanmış mumlardan: son açık mum ATR/hacme gremez;
    stale (son kapanan bar çok eski) / bozuk satır / hata sözlüğü → None →
    giriş kapalı. ATR ve hacim AYNI kapanmış bar setinden (paylaşımlı fetch).
  * Rate-limit cooldown'unda YENİ GİRİŞ fail-closed; dry-run etkilenmez.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_guard                      # noqa: E402
import auto_trader                    # noqa: E402
import contextlib
from test_inc13_live_wiring import FakeBinance, FakeHB, LiveWiringBase, _RiskOK, _coordinator

# Bazı eski test dosyaları auto_trader.calculate_atr'ı SÖZLEŞME gereği
# lambda ile değiştirir ve geri yüklemez (test_blok10_atr_failclosed:32,
# test_faz15_c6_integrity:776). Sıralamaya bağımlı kırmızıyı kesmek için bu
# dosya GERÇEK fonksiyonları import-anında sabitler (üretim koduna dokunmaz).
_REAL_CALC_ATR = auto_trader.calculate_atr
_REAL_VOL_CONF = auto_trader._volume_confirmation

IM = 900_000  # 15m


def _rows(n, now_ms, interval_ms=IM, unclosed_last=False, base=100.0,
          vol=10.0, close_time_shift=0):
    """8-alan klines satırı: [openTime,o,h,l,c,vol,closeTime,qvol]."""
    out = []
    for i in range(n):
        ct = now_ms - (n - i) * interval_ms + close_time_shift
        o = base + i
        c = o + 1
        v = vol
        if unclosed_last and i == n - 1:
            ct = now_ms + interval_ms          # close_time > now → AÇIK mum
        out.append([ct - interval_ms, str(o), str(o + 2), str(o - 1), str(c),
                    str(v), ct, str(v * c)])
    return out


class _Resp:
    def __init__(self, status=200, headers=None, body=None, bad_json=False):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("gövde json değil")
        return self._body


class _FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.get_calls = 0
        self.post_calls = 0
        self.delete_calls = 0

    def get(self, *a, **k):
        self.get_calls += 1
        return self.resp

    def post(self, *a, **k):
        self.post_calls += 1
        return self.resp

    def delete(self, *a, **k):
        self.delete_calls += 1
        return self.resp


class TestApiGuardModule(unittest.TestCase):
    def setUp(self):
        api_guard.reset()

    def tearDown(self):
        api_guard.reset()

    def test_429_retry_after_yoksa_60s_ve_blok(self):
        d = api_guard.note_http_status(429, {}, now=1000.0)
        self.assertEqual(d, 60.0)
        self.assertIsNotNone(api_guard.blocked(now=1030.0))
        self.assertIn("429-rate-limit", api_guard.blocked(now=1030.0))
        self.assertEqual(api_guard.cooldown_remaining(now=1030.0), 30.0)

    def test_429_retry_after_onceliklidir(self):
        d = api_guard.note_http_status(429, {"Retry-After": "7"}, now=1000.0)
        self.assertEqual(d, 7.0)
        self.assertIsNone(api_guard.blocked(now=1008.0))

    def test_grevlerle_usel_backoff_ve_tavan(self):
        t = 1000.0
        self.assertEqual(api_guard.note_http_status(429, {}, now=t), 60.0)
        self.assertEqual(api_guard.note_http_status(429, {}, now=t), 120.0)
        self.assertEqual(api_guard.note_http_status(429, {}, now=t), 240.0)
        for _ in range(8):
            d = api_guard.note_http_status(429, {}, now=t)
        self.assertLessEqual(d, api_guard.MAX_COOLDOWN_S)
        self.assertLessEqual(api_guard.cooldown_remaining(now=t),
                             api_guard.MAX_COOLDOWN_S)

    def test_418_uzun_cooldown(self):
        d = api_guard.note_http_status(418, {}, now=1000.0)
        self.assertEqual(d, 900.0)
        self.assertIn("418-ip-ban", api_guard.blocked(now=1500.0))
        self.assertIsNone(api_guard.blocked(now=1901.0))

    def test_418_retry_after(self):
        d = api_guard.note_http_status(418, {"Retry-After": "30"}, now=0.0)
        self.assertEqual(d, 30.0)

    def test_basari_grevleri_sifirlar_cooldowna_dokunmaz(self):
        api_guard.note_http_status(429, {}, now=1000.0)
        api_guard.note_http_status(429, {}, now=1000.0)      # strikes=2
        api_guard.success()                                   # grevler sıfır
        self.assertEqual(api_guard.note_http_status(429, {}, now=2000.0), 60.0)
        # cooldown hâlâ işliyordu mu? 2000'de önceki pencere çoktan bitti.
        api_guard.reset()
        api_guard.note_http_status(429, {"Retry-After": "100"}, now=0.0)
        api_guard.success()                                   # süreyi kısaltmaz
        self.assertIsNotNone(api_guard.blocked(now=50.0))

    def test_basari_cozumlenemeyen_retry_after_60a_duser(self):
        d = api_guard.note_http_status(429, {"Retry-After": "biraz-ac"}, now=0.0)
        self.assertEqual(d, 60.0)

    def test_diger_statuler_noop(self):
        for code in (200, 400, 418 - 1, 500):
            self.assertEqual(api_guard.note_http_status(code, {}), 0.0)
        self.assertIsNone(api_guard.blocked())


class TestApiResponseWiring(unittest.TestCase):
    def setUp(self):
        api_guard.reset()
        self._old = auto_trader._session
        # GÖREV 2026-11-27 §9.3: BinanceAPI artık taban adresi doğruluyor
        # (tek boğum noktası); bu sınıf rate-limit gardını sahte özel
        # hostta sınadığı için operatör opt-in'i temsil edilir.
        self._old_optin = os.environ.pop("LIVE_ALLOW_CUSTOM_BASE_URL", None)
        os.environ["LIVE_ALLOW_CUSTOM_BASE_URL"] = "1"

    def tearDown(self):
        auto_trader._session = self._old
        api_guard.reset()
        if self._old_optin is None:
            os.environ.pop("LIVE_ALLOW_CUSTOM_BASE_URL", None)
        else:
            os.environ["LIVE_ALLOW_CUSTOM_BASE_URL"] = self._old_optin

    def _api(self, resp):
        auto_trader._session = _FakeSession(resp)
        return auto_trader.BinanceAPI("k", "s", base="https://x.invalid")

    def test_get_429_not_eder_ve_blok_acilir(self):
        api = self._api(_Resp(429, {"Retry-After": "5"}, {"code": -1015}))
        body = api._get("/fapi/v1/klines")
        self.assertEqual(body, {"code": -1015})
        self.assertIsNotNone(api_guard.blocked())
        self.assertIn("429-rate-limit", api_guard.blocked())

    def test_post_429_not_edilir_tek_gonderim(self):
        sess = _FakeSession(_Resp(429, {}, {"code": -1015}))
        auto_trader._session = sess
        api = auto_trader.BinanceAPI("k", "s", base="https://x.invalid")
        body = api._post("/fapi/v1/order", {"symbol": "ETHUSDT"})
        self.assertEqual(body, {"code": -1015})
        self.assertEqual(sess.post_calls, 1)        # KÖR RETRY YOK (bu katman)
        self.assertIsNotNone(api_guard.blocked())

    def test_2xx_grev_sifirlar(self):
        api_guard.note_http_status(429, {}, now=0.0)
        api = self._api(_Resp(200, body=[1, 2, 3]))
        api._get("/fapi/v1/time")
        # grevler sıfırlandı: sonraki 429 taban değerden başlar
        self.assertEqual(api_guard.note_http_status(429, {}, now=10_000.0), 60.0)

    def test_nonjson_hata_sozlugu_exception_degil(self):
        api = self._api(_Resp(502, bad_json=True))
        self.assertEqual(api._get("/fapi/v1/time"),
                         {"code": -1, "msg": "non-json http 502"})

    def test_urllib3_post_retry_ve_429_force_list_yok(self):
        r = auto_trader._retry
        methods = set(getattr(r, "allowed_methods", None)
                      or getattr(r, "method_whitelist", None) or [])
        self.assertNotIn("POST", methods)
        self.assertNotIn("DELETE", methods)
        self.assertIn("GET", methods)
        force = set(r.status_forcelist or [])
        self.assertNotIn(429, force)
        self.assertNotIn(418, force)


class _KLFake:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def _get(self, path, params=None, signed=False):
        self.calls += 1
        return self.rows


class TestClosedBars(unittest.TestCase):
    def setUp(self):
        auto_trader._CLOSED_KL_CACHE.clear()
        self.now = 1_700_000_000_000

    def test_acik_mum_hic_girmez(self):
        rows = _rows(5, self.now, unclosed_last=True)
        bars, stale = auto_trader.closed_bars(rows, "15m", now_ms=self.now)
        self.assertEqual(len(bars), 4)
        self.assertFalse(stale)

    def test_tum_barlar_gelecekteyse_none_true(self):
        rows = _rows(3, self.now - 10 * IM, unclosed_last=False)
        # hepsi now'dan büyük close_time → kapanmış yok
        for r in rows:
            r[6] = self.now + IM
        bars, stale = auto_trader.closed_bars(rows, "15m", now_ms=self.now)
        self.assertIsNone(bars)
        self.assertTrue(stale)

    def test_kisa_satir_bozuk_zaman_none_true(self):
        rows = _rows(3, self.now)
        rows[1] = rows[1][:7]                       # closeTime alanı yok
        bars, stale = auto_trader.closed_bars(rows, "15m", now_ms=self.now)
        self.assertIsNone(bars)
        self.assertTrue(stale)
        rows = _rows(3, self.now)
        rows[0][6] = "bozuk"
        bars, stale = auto_trader.closed_bars(rows, "15m", now_ms=self.now)
        self.assertIsNone(bars)
        self.assertTrue(stale)

    def test_bos_veya_list_degil_none_true(self):
        for bad in (None, {"code": -1015}, []):
            bars, stale = auto_trader.closed_bars(bad, "15m", now_ms=self.now)
            self.assertIsNone(bars)
            self.assertTrue(stale)

    def test_stale_eski_son_kapanan_bar(self):
        rows = _rows(5, self.now - 3 * IM)          # son kapanış 3 interval önce
        bars, stale = auto_trader.closed_bars(rows, "15m", now_ms=self.now)
        self.assertTrue(stale)
        self.assertEqual(len(bars), 5)


class TestAtrVolumeClosedOnly(unittest.TestCase):
    def setUp(self):
        auto_trader._CLOSED_KL_CACHE.clear()
        self.now = int(1_700_000_000_500 // IM) * IM + 60_000  # kovayı sabitle
        # auto_trader.time = paylaşılan `time` modülü; sahte saat test boyunca
        # self.now'da sabitlenir (bozuk/veri-yol testleri bunu gerektirir).
        old = auto_trader.time.time
        auto_trader.time.time = lambda: self.now / 1000.0
        self.addCleanup(lambda: setattr(auto_trader.time, "time", old))

    def test_atr_acik_mumu_yok_sayar(self):
        # 16 satır: 15 kapalı + sonuncusu AÇIK ve AŞIRI değerli (patlatılmış)
        rows = _rows(16, self.now, unclosed_last=True)
        rows[-1][2] = "999999"; rows[-1][3] = "0.01"; rows[-1][4] = "50000"
        fake = _KLFake(rows)
        atr = _REAL_CALC_ATR(fake, "ETHUSDT", "15m", period=14)
        self.assertIsNotNone(atr)
        # Beklenen: YALNIZ ilk 15 kapalı bar
        closed = rows[:15]
        trs = []
        for i in range(1, 15):
            h, l, pc = float(closed[i][2]), float(closed[i][3]), float(closed[i - 1][4])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        self.assertAlmostEqual(atr, (sum(trs) / 14) / float(closed[-1][4]))

    def test_atr_stale_none(self):
        rows = _rows(20, self.now - 5 * IM)         # son kapanış 5 interval önce
        self.assertIsNone(_REAL_CALC_ATR(_KLFake(rows), "ETHUSDT"))

    def test_atr_hata_sozlugu_none(self):
        self.assertIsNone(_REAL_CALC_ATR(_KLFake({"code": -1015}),
                                                    "ETHUSDT"))

    def test_atr_yetersiz_kapali_bar_none(self):
        rows = _rows(6, self.now)                   # 6 < period+1=15
        self.assertIsNone(_REAL_CALC_ATR(_KLFake(rows), "ETHUSDT"))

    def test_volume_acik_mumu_yok_sayar_ve_oran(self):
        rows = _rows(22, self.now, unclosed_last=True)
        rows[-1][5] = "999999"                       # açık mumda devasa hacim
        rows[-2][5] = "30"                           # son KAPALI spike
        ratio = _REAL_VOL_CONF(_KLFake(rows), "ETHUSDT")
        self.assertIsNotNone(ratio)
        prev = [float(r[5]) for r in rows[:-2]][-20:]  # son 20 kapalı (spike öncesi)
        self.assertAlmostEqual(ratio, 30.0 / (sum(prev) / len(prev)))

    def test_volume_stale_none(self):
        rows = _rows(25, self.now - 5 * IM)
        self.assertIsNone(_REAL_VOL_CONF(_KLFake(rows),
                                                           "ETHUSDT"))

    def test_atr_ve_volume_ayni_set_paylasir_tek_fetch(self):
        rows = _rows(30, self.now)
        fake = _KLFake(rows)
        b1, s1 = auto_trader.closed_klines_for(fake, "ETHUSDT", "15m", need=15)
        n_after_atr = fake.calls
        b2, s2 = auto_trader.closed_klines_for(fake, "ETHUSDT", "15m", need=21)
        # daha büyük need → yeniden fetch kaçınılmaz; sonra need=15 tekrar → fetch YOK
        big_calls = fake.calls
        b3, _ = auto_trader.closed_klines_for(fake, "ETHUSDT", "15m", need=15)
        self.assertEqual(fake.calls, big_calls)
        self.assertEqual(b3, b2[-15:])
        self.assertEqual(n_after_atr, 1)

    def test_symbol_farki_cache_tezsiz_dokunmaz(self):
        rows = _rows(20, self.now)
        fake = _KLFake(rows)
        auto_trader.closed_klines_for(fake, "ETHUSDT", "15m", need=15)
        auto_trader.closed_klines_for(fake, "SOLUSDT", "15m", need=15)
        self.assertEqual(fake.calls, 2)


class TestOpenCoinRateLimitGate(LiveWiringBase):
    """Cooldown sürerken üretim girişi fail-closed; dry-run etkilenmez."""
    @contextlib.contextmanager
    def _vb(self):
        import validation_budget as vb
        old_env = {k: os.environ.get(k) for k in
                   ("CONTROLLED_LIVE_VALIDATION", "MAX_VALIDATION_TRADES",
                    "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
                    "MAX_VALIDATION_TOTAL_NOTIONAL_USDT",
                    "VALIDATION_APPROVAL_DB", "OPERATOR_APPROVAL_TOKEN_FILE")}
        old_dir, old_bl = auto_trader.STATE_DIR, auto_trader.BLACKLIST_FILE
        saved = (auto_trader._VB_CFG, auto_trader._VB_BUDGET)
        auto_trader.STATE_DIR = self._tmpdir_path
        auto_trader.BLACKLIST_FILE = self._tmpdir_path / ".bl.json"
        os.environ["CONTROLLED_LIVE_VALIDATION"] = "true"
        os.environ["MAX_VALIDATION_TRADES"] = "2"
        os.environ["MAX_VALIDATION_POSITION_NOTIONAL_USDT"] = "50"
        os.environ["MAX_VALIDATION_TOTAL_NOTIONAL_USDT"] = "100"
        os.environ["VALIDATION_APPROVAL_DB"] = str(self._tmpdir_path / "gate.db")
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

    def setUp(self):
        super().setUp()
        from pathlib import Path
        self._tmpdir_path = Path(self.tmp)
        api_guard.reset()

    def tearDown(self):
        api_guard.reset()
        super().tearDown()

    def _open(self, binance, hb, cc, dry_run=False):
        return auto_trader.open_coin(binance, hb, dict(self._coin()),
                                     dry_run=dry_run, risk=_RiskOK(),
                                     reserver=None, coordinator=cc)

    def test_cooldown_yeni_girisi_fail_closed_kapatir(self):
        with self._vb():
            api_guard.note_http_status(429, {"Retry-After": "600"})
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), False)
            self.assertEqual(hb.created, [])          # POST YOK
            if auto_trader._VB_BUDGET is not None:
                self.assertEqual(
                    auto_trader._VB_BUDGET.status()["trades_used"], 0)

    def test_418_sonrasi_giris_red(self):
        with self._vb():
            api_guard.note_http_status(418, {})
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), False)
            self.assertEqual(hb.created, [])

    def test_dry_run_cooldown_eklenmez(self):
        with self._vb():
            api_guard.note_http_status(429, {})
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            # dry-run emir göndermez; kapı CANLI yolu hedefler
            res = self._open(binance, hb, cc, dry_run=True)
            self.assertIsNot(res, False)
            self.assertEqual(hb.created, [])

    def test_cooldown_sonrasinda_giris_acilir(self):
        # Not: LiveWiringBase time.sleep'i no-op yapar → süre enjeksiyonuyla
        # kanıtlanır (gerçek uyku YOK).
        with self._vb():
            now = auto_trader.time.time()
            api_guard.note_http_status(429, {"Retry-After": "60"}, now=now)
            self.assertIsNotNone(api_guard.blocked())      # önce KAPALI
            api_guard.reset()
            # Süresi geçmiş not (now-10, RA=1 → pencere 9s önce kapandı):
            api_guard.note_http_status(429, {"Retry-After": "1"}, now=now - 10)
            self.assertIsNone(api_guard.blocked())          # sonra AÇIK
            binance, hb = FakeBinance(), FakeHB()
            cc, _ = _coordinator(binance, hb, self.db)
            self.assertIs(self._open(binance, hb, cc), True)
            self.assertEqual(len(hb.created), 1)


if __name__ == "__main__":
    unittest.main()
