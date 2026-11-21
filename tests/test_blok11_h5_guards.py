"""BLOK11 §3.2 — H5 koşucu GARD testleri (≥10 config-hatası kırmızıyı görür).

Doğrulanan sözleşme: her gard hatası KOŞUYU DURDURUR (fail-closed) ve üretim
base URL / halt=false / yanlış pencere/sembol/tf / var out-dir / eksik veri /
kaynakta emir-ağ-credential tokenı / ortamda Binance kimliği → RED.
Motor testleri: faz5_harness/faz7 birebir kopyalarının kimliği.
Gerçek veri/koşu YOK — yalnız tmp ağaç + sentetik bar.
"""
import copy
import datetime
import os
import random
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import h5_engine as HE          # noqa: E402
import h5_runner as HR          # noqa: E402


def _make_root(symbols=("ETHUSDT", "BCHUSDT", "GALAUSDT")):
    """G07'nin isteyeceği tüm ay-dosyaları: FULL2025 → 2024-08..2026-01."""
    root = tempfile.mkdtemp(prefix="h5root_")
    months = ["2024-%02d" % m for m in (8, 9, 10, 11, 12)] \
        + ["2025-%02d" % m for m in range(1, 13)] + ["2026-01"]
    for s in symbols:
        for tf in ("15m", "1h", "4h"):
            d = os.path.join(root, s, tf)
            os.makedirs(d)
            for ym in months:
                open(os.path.join(d, ym + ".csv"), "w").close()
    return root


def base_cfg(**over):
    cfg = {"base_url": "https://testnet.binancefuture.com",
           "halt": True,
           "symbols": ["ETHUSDT", "BCHUSDT", "GALAUSDT"],
           "timeframe": "15m",
           "window": {"from": "2025-01-01", "to": "2025-12-31"},
           "data_root": _make_root(),
           "faz3_root": "/nonexistent-faz3-root",
           "out_dir": os.path.join(tempfile.mkdtemp(prefix="h5out_"),
                                   "H5_RUN")}
    cfg.update(over)
    return cfg


class TestGuardsRed(unittest.TestCase):
    def _fail(self, cfg, allow_partial=False, env=None):
        log, name, _ = HR.run_guards(cfg, env=env or {},
                                     allow_partial=allow_partial)
        return name, log

    def test_00_sag_taraf_yesil(self):
        name, log = self._fail(base_cfg())
        self.assertIsNone(name, [e for e in log if not e["ok"]])
        self.assertEqual({e["guard"] for e in log},
                         {"G01-schema", "G01-halt", "G02-base", "G03-symbols",
                          "G04-tf", "G05-window", "G06-outdir", "G09-env",
                          "G08-sourceban", "G07-data"})

    def test_01_bilinmeyen_anahtar_red(self):
        name, _ = self._fail(base_cfg(typos=1))
        self.assertEqual(name, "G01-schema")

    def test_02_eksik_anahtar_red(self):
        cfg = base_cfg()
        del cfg["halt"]
        name, _ = self._fail(cfg)
        self.assertEqual(name, "G01-schema")

    def test_03_halt_false_red(self):
        name, _ = self._fail(base_cfg(halt=False))
        self.assertEqual(name, "G01-halt")

    def test_04_uretim_base_red(self):
        for bu in ("https://fapi.binance.com", "https://api.binance.com",
                   "wss://fstream.binance.com", "https://testnet-binance.com"):
            name, _ = self._fail(base_cfg(base_url=bu))
            self.assertEqual(name, "G02-base", bu)

    def test_05_sembol_listesi_red(self):
        for syms in (["ETHUSDT"],                                  # eksik
                     ["ETHUSDT", "BCHUSDT", "GALAUSDT", "SOLUSDT"],  # fazla
                     ["ETHUSDT", "ETHUSDT", "BCHUSDT"]):            # mükerrer
            name, _ = self._fail(base_cfg(symbols=syms))
            self.assertEqual(name, "G03-symbols", syms)

    def test_06_tf_red(self):
        name, _ = self._fail(base_cfg(timeframe="1h"))
        self.assertEqual(name, "G04-tf")

    def test_07_pencere_red(self):
        for w in ({"from": "2024-01-01", "to": "2024-12-31"},
                  {"from": "2025-06-01", "to": "2025-12-31"},
                  {"from": "2025-11-01", "to": "2026-02-01"}):
            name, _ = self._fail(base_cfg(window=w))
            self.assertEqual(name, "G05-window", w)
        # window parça eksik → şema gardı (G05'e gelmeden)
        name, _ = self._fail(base_cfg(window={"from": "2025-01-01"}))
        self.assertEqual(name, "G01-schema")
        # geçerli alternatif pencere YEŞİL
        name, _ = self._fail(base_cfg(
            window={"from": "2025-11-01", "to": "2026-01-01"}))
        self.assertIsNone(name)

    def test_08_outdir_varsa_red(self):
        cfg = base_cfg()
        os.makedirs(cfg["out_dir"])
        name, _ = self._fail(cfg)
        self.assertEqual(name, "G06-outdir")

    def test_09_veri_eksik_onaysiz_red(self):
        cfg = base_cfg(data_root=_make_root(("ETHUSDT",)))  # BCH/GALA yok
        name, log = self._fail(cfg)
        self.assertEqual(name, "G07-data")
        # açık onayla: geçer, bloklular raporlanır (PARTIAL yolu)
        log2, name2, blocked = HR.run_guards(cfg, env={}, allow_partial=True)
        self.assertIsNone(name2)
        self.assertEqual(blocked, ["BCHUSDT", "GALAUSDT"])

    def test_10_tumu_eksik_se_red(self):
        """Kısmi veri: bloklar raporlanır ve PARTIAL yolu açılır; TÜM misyon
        kümesi eksikse allow-partial'a rağmen RED (koşulacak sembol yok)."""
        cfg = base_cfg(data_root=_make_root(("BCHUSDT",)))
        # ETH ve GALA eksik → 2 blok, 1 koşu: PARTIAL kabul
        log, name, blocked = HR.run_guards(cfg, env={}, allow_partial=True)
        self.assertIsNone(name)
        self.assertEqual(blocked, ["ETHUSDT", "GALAUSDT"])
        # Hiç sembol verisi yoksa → RED (allow-partial'a rağmen)
        cfg2 = base_cfg(data_root=_make_root(()))
        log, name, _ = HR.run_guards(cfg2, env={}, allow_partial=True)
        self.assertEqual(name, "G07-data")

    def test_11_ortamda_binance_key_red(self):
        for env in ({"BINANCE" + "_" + "KEY": "x"},
                    {"BINANCE" + "_" + "SECRET": "y"}):
            name, _ = self._fail(base_cfg(), env=env)
            self.assertEqual(name, "G09-env", env)

    def test_12_kaynak_token_taramasi(self):
        # Gerçek H5 dosyaları temiz olmalı
        self.assertIsNone(HR.check_source_ban(
            [os.path.join(_ROOT, "tools", "h5_engine.py"),
             os.path.join(_ROOT, "tools", "h5_runner.py")]))
        # Zehirlenmiş dosya bulunmalı (token'lar bileştirme — test kendisi
        # taransaydı yakalanırdı)
        p = os.path.join(tempfile.mkdtemp(), "evil.py")
        with open(p, "w") as fh:          # gerçek token yazılır (bileştirme
            fh.write("q = " + repr("pl" + "ace_order") + "\n")
        hit = HR.check_source_ban([p])    # test dosyasının kendisi temiz kalır)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "evil.py")

    def test_13_gard_sirasi_semalar(self):
        # İlk başarısız gard döner; sonrakiler çalışmaz (kısa devre)
        cfg = base_cfg(halt=False, timeframe="5m")
        log, name, _ = HR.run_guards(cfg, env={})
        self.assertEqual(name, "G01-halt")
        self.assertNotIn("G04-tf", [e["guard"] for e in log])

    def test_14_window_gecerli_degerleri(self):
        self.assertEqual(HR.WINDOWS[("2025-01-01", "2025-12-31")], "FULL2025")
        self.assertEqual(HR.WINDOWS[("2025-11-01", "2026-01-01")],
                         "EXACT_NOV_DEC")
        self.assertEqual(HR.MS_2026, 1767225600000)

    def test_15_allow_partial_run_a_iletilir(self):
        """REGRESYON: run(), allow_partial'ı run_guards'a İLETMEYİNCE ilk tam
        koşu yanlışlıkla RED olmuştu — bayrcak zinciri testli olmalı."""
        from unittest import mock
        seen = {}

        def fake_guards(cfg, env=None, allow_partial=False):
            seen["allow_partial"] = allow_partial
            return ([{"guard": "x", "ok": False, "why": "stop"}], "X", [])
        with mock.patch.object(HR, "run_guards", fake_guards):
            code, _, _ = HR.run(base_cfg(), allow_partial=True)
        self.assertEqual(code, 3)
        self.assertIs(seen.get("allow_partial"), True)


class TestBoundaryFilter(unittest.TestCase):
    """REGRESYON: karar 2025-12-31 23:45 → giriş 2026-01-01T00:00 OLMAMALI;
    olay simülasyona girmeden atılır ve SAYILIR (ilk tam koşul G-runtime
    değişmezini ihlal eden bu 4 bacağı crash ile yakalamıştı)."""

    class _Geom:
        sl_pct, tp_pct = 0.02, 0.03

    class _Sizing:
        @staticmethod
        def suggest_sl_tp(atr, *, direction=1, cfg=None, min_rr=None):
            return TestBoundaryFilter._Geom()

    class _SD:
        def __init__(self, sym, root, ymf, ymt):
            start = 1767139200000         # 2025-12-31T00:00Z
            self.r15 = [[start + k * 900000, 100.0 + k, 101.0 + k,
                         99.0 + k, 100.5 + k, 1.0, start + (k + 1) * 900000]
                        for k in range(200)]

    class _FB:
        SymbolData = None                 # aşağıda bağlanır
        SCAN_STATS = {}

        @staticmethod
        def scan_symbol_decisions(sd, cfg, yf, yt):
            return [{"i": 90, "t": sd.r15[91][0], "side": 1, "veto": False,
                     "fams": 1, "atr_pct": 0.01},          # içeride (15:15)
                    {"i": 95, "t": 1767225600000, "side": -1, "veto": False,
                     "fams": 1, "atr_pct": 0.01},          # tam 2026-01-01
                    {"i": 96, "t": 1767226500000, "side": 1, "veto": False,
                     "fams": 1, "atr_pct": 0.01}]          # 2026 içi

    def setUp(self):
        self._FB.SymbolData = self._SD

    def test_2026_giris_simulasyona_girmez(self):
        tr, sk, _ = HR.simulate_symbol(
            self._FB, self._Sizing, "ETHUSDT", "/tmp", "2026-01",
            "2025-01", "2025-12", {"base": {"fee_rate": 0.0005,
                                            "slip_bps": 0.0}}, 16,
            entry_lo_ms=1735689600000)
        self.assertEqual(sk["excluded_2026_entry"], 2)
        self.assertTrue(tr)
        for t in tr:
            self.assertLess(t["t_entry"], HR.MS_2026)


class TestEngineCopy(unittest.TestCase):
    def _bars(self, seed=7, n=4000, start=1735689600000):
        rng = random.Random(seed)
        px, bars = 3000.0, []
        for k in range(n):
            o = px
            for _ in range(15):
                px *= (1 + rng.gauss(0, 0.002))
            h = max(o, px) * 1.001
            l = min(o, px) * 0.999
            bars.append([start + k * 900000, o, h, l, px, 1.0,
                         start + (k + 1) * 900000 - 1])
        return bars

    def test_run_round_ex_kimlik(self):
        """run_round_ex, run_round'un (gross,net,why) üçlüsünü bozmaz."""
        bars = self._bars()
        rng = random.Random(11)
        for trial in range(60):
            i0 = rng.randrange(0, len(bars) - 40)
            sig = (bars[i0][0], 1.0, 16, rng.uniform(0.005, 0.05),
                   rng.uniform(0.003, 0.04))
            side = 1 if trial % 2 else -1
            slip = rng.choice([0.0, 25.0])
            g1, n1, w1 = HE.run_round(bars, sig, side, HE.FEE_RATE, slip)
            g2, n2, w2, nh, i0b, e_px, x_px = HE.run_round_ex(
                bars, sig, side, HE.FEE_RATE, slip)
            self.assertEqual((g1, n1, w1), (g2, n2, w2))
            self.assertGreaterEqual(nh, 1)
            self.assertEqual(i0b, i0 + 1)
            self.assertGreater(e_px, 0)
            self.assertGreater(x_px, 0)
            # ts-önbelleği yolu da aynı sonucu vermeli (yalnız hızlandırma)
            r_ts = HE.run_round_ex(bars, sig, side, HE.FEE_RATE, slip,
                                   ts=[b[0] for b in bars])
            self.assertEqual(r_ts, (g2, n2, w2, nh, i0b, e_px, x_px))

    def test_mirror_kimlik_ve_aynalama(self):
        bars = self._bars()
        sig = (bars[10][0], 1.0, 16, 0.02, 0.01)
        self.assertEqual(HE.mirror(sig), sig)   # mesafeler simetrik (B.3)

    def test_no_bar(self):
        bars = self._bars(n=50)
        g, n, w = HE.run_round(bars, (bars[-1][0] + 10**10, 1, 16, .02, .01),
                               1, HE.FEE_RATE, 25.0)
        self.assertEqual((g, n, w), (0.0, 0.0, "NO-BAR"))

    def test_funding_suruklenmesi_negatif(self):
        self.assertLess(HE.funding_drag_pct(16), 0)
        self.assertAlmostEqual(HE.funding_drag_pct(96),
                               2 * HE.funding_drag_pct(48))

    def test_boot_lo_ve_dsr_sagligi(self):
        rng = random.Random(20261120)
        lo = HE.boot_lo([0.01] * 50, [1] * 50, rng, B=200)
        self.assertGreater(lo, 0)               # hepsi pozitif → alt uç > 0
        lo2 = HE.boot_lo([-0.01] * 50, [1] * 50, rng, B=200)
        self.assertLess(lo2, 0)
        self.assertEqual(HE.boot_lo([1, 2], [1, 1], rng, B=100), -1.0)  # <3 küme
        good = [abs(x) * 0 + 0.01 + 0.001 * ((i * 7919) % 5) for i, x
                in enumerate(range(60))]
        d = HE.dsr(good, 20)
        self.assertTrue(0.0 <= d <= 1.0 or d == -1.0)

    def test_run_pair_toplamlar(self):
        bars = self._bars(seed=3, n=200)
        sigs = [(bars[k][0], 1.0, 16, 0.02, 0.01) for k in range(50, 100)]
        out = HE.run_pair(bars, sigs, HE.FEE_RATE, 25.0)
        self.assertEqual(out["long"][2], 50)
        self.assertEqual(out["short"][2], 50)
        g, n, _ = HE.run_round(bars, sigs[0], 1, HE.FEE_RATE, 25.0)
        self.assertAlmostEqual(out["long"][1],
                               sum(HE.run_round(bars, s, 1, HE.FEE_RATE, 25.0)[1]
                                   for s in sigs))


if __name__ == "__main__":
    unittest.main()
