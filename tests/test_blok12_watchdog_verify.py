"""BLOK12 §6 — verify_watchdog parite doğrulayıcısının testleri.

Araç YALNIZ-okunur olmalı (dosyalar değişmemeli) ve sınıf sınıf kırmızısı
yakalamalı: farklı HOME (sessiz körlük), FAZ9_STATE_DIR tuzağı, eksik dosya.
Gerçek systemd/ağ/emir YOK — fixture ağacı üzerine yazılmış birim örnekleri.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
VERIFY = os.path.join(_ROOT, "tools", "verify_watchdog.py")
PY = sys.executable

_FILES = ("deploy/orphan_watchdog_faz9_live.service.example",
          "deploy/orphan_watchdog_faz9_live.timer.example",
          "deploy/orphan_watchdog_cron.example",
          "deploy/auto_trader_faz9_live.service",
          "tools/orphan_watchdog.py",
          "orphan_killer.py",
          "auto_trader.py")


def _mini_tree(tmp, env_body="BINANCE_KEY=redacted\nBINANCE_SECRET=redacted\n"):
    for rel in _FILES:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) != tmp else None
        shutil.copyfile(os.path.join(_ROOT, rel), dst)
    # EnvironmentFile'ı fixture içine taşı (ikisi de AYNI kalmalı — parite
    # sözleşmesi bunu gerektirir; ayrı dosyalar ayrı FAIL üretir).
    env_path = os.path.join(tmp, "live.env")
    with open(env_path, "w") as fh:
        fh.write(env_body)
    for rel in ("deploy/orphan_watchdog_faz9_live.service.example",
                "deploy/auto_trader_faz9_live.service"):
        p = os.path.join(tmp, rel)
        s = open(p).read().replace("/etc/auto_trader/live.env", env_path)
        open(p, "w").write(s)
    return env_path


def _run(root):
    r = subprocess.run([PY, "-I", VERIFY, root], capture_output=True,
                       text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def _hashes(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(
                    fh.read()).hexdigest()
    return out


class TestVerifyWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12wd-")
        self.env = _mini_tree(self.tmp)

    def test_temiz_agac_pass_ve_yalniz_okunur(self):
        before = _hashes(self.tmp)
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        self.assertIn("parite:HOME", out)
        self.assertEqual(before, _hashes(self.tmp))   # YALNIZ-OKUNUR kanıtı

    def test_farkli_home_fail(self):
        p = os.path.join(self.tmp,
                         "deploy/orphan_watchdog_faz9_live.service.example")
        s = open(p).read().replace(
            "Environment=HOME=/var/lib/auto_trader_faz9_live",
            "Environment=HOME/root")
        open(p, "w").write(s)
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("parite:HOME", out)
        self.assertIn("FAIL", out)

    def test_env_state_dir_tuzagi_fail(self):
        with open(self.env, "a") as fh:
            fh.write("FAZ9_STATE_DIR=/var/lib/otomatik\n")
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("env:STATE_DIR-tuzağı", out)
        self.assertIn("FAIL", out)

    def test_anahtarsiz_env_warn_ama_pass(self):
        # Anahtarsız env → sarmalayıcı exit 2 verir (sessiz körlük değil):
        # parite aracı WARN der, RED vermez.
        open(self.env, "w").write("# anahtar yok\n")
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 0)
        self.assertIn("env:anahtarlar", out)
        self.assertIn("WARN", out)

    def test_sarmalayici_emir_tokeni_eklenirse_fail(self):
        p = os.path.join(self.tmp, "tools/orphan_watchdog.py")
        with open(p, "a") as fh:
            fh.write("\n# planted: create_executor\n")
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("sarmalayici:emir-yasak", out)

    def test_timer_periyot_yoksa_fail(self):
        p = os.path.join(self.tmp,
                         "deploy/orphan_watchdog_faz9_live.timer.example")
        s = "\n".join(l for l in open(p) if "OnUnitActiveSec" not in l)
        open(p, "w").write(s)
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("timer:periyot", out)

    def test_eksik_dosya_dogrulanamaz_exit2(self):
        os.remove(os.path.join(self.tmp, "tools/orphan_watchdog.py"))
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 2)
        self.assertIn("dosya-yok", out)

    def test_kron_ve_systemd_kilidi_uyarsi(self):
        # Bilinen gerçek durum: cron örneğinin kilidi ayrı → WARN (FAIL değil).
        rc, out = _run(self.tmp)
        self.assertEqual(rc, 0)
        self.assertIn("kilit:cron-vs-systemd", out)


if __name__ == "__main__":
    unittest.main()
