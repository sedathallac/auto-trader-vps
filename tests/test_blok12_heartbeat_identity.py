"""BLOK12 §5 — gömülü heartbeat kimliği temizliği + secret scanner testleri.

Sözleşme: (1) kill_spot_grid.py/.sh sabit kimlik İÇERMEZ; (2) kimlik eksikse
fail-closed SystemExit(2); (3) değer hiçbir çıktıya yazılmaz; (4) scanner
kalıpları ve sız mı ş kimlik imzalarını yakalar, temiz ağaçta exit 0.
Test dosyasının kendisi eski değeri İÇERMEZ (bileştirme/kısa sentetik).
"""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import secret_scan as SS          # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNoEmbeddedIdentity(unittest.TestCase):
    def test_kill_py_no_credential_patterns(self):
        with open(os.path.join(_ROOT, "kill_spot_grid.py"), encoding="utf-8") as fh:
            src = fh.read()
        hits = [h for h in SS.scan_text(src) if h[1] != "KNOWN-DIGEST"]
        self.assertEqual(hits, [], hits)
        self.assertNotIn("HB_AUTH =", src)

    def test_c0_sh_no_credential_patterns(self):
        with open(os.path.join(_ROOT, "kill_spot_grid_c0.sh"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual([h for h in SS.scan_text(src) if h[1] != "KNOWN-DIGEST"], [])
        self.assertIn("kill_spot_grid.py", src)     # tek uygulamaya delege

    def test_scanner_flags_historical_credential_digest(self):
        # Eski gömülü kimlik biçiminde satır: imza listesi değer içermese de
        # dosya-özet/token taramasıyla bulunur (token loop).
        old_user = "furkan" + "_admin"
        old_pass = "HmB0t_S3c" + "ure_2026x"
        line = 'HB_AUTH = ("%s", "%s")\n' % (old_user, old_pass)
        hits = SS.scan_text(line)
        self.assertTrue(any(h[1] == "KNOWN-DIGEST" for h in hits), hits)
        self.assertTrue(any(h[1] == "LITERAL-HB" for h in hits), hits)
        # maskeleme: maskelenen bulgu ham değeri taşımaz
        for _, _, masked in hits:
            self.assertNotIn(old_pass, masked)


class TestLoadHbAuth(unittest.TestCase):
    def setUp(self):
        self.k = _load("ksg_under_test", "kill_spot_grid.py")

    def test_missing_credentials_exit2(self):
        with self.assertRaises(SystemExit) as cm:
            self.k.load_hb_auth(env={})
        self.assertEqual(cm.exception.code, 2)

    def test_empty_string_credentials_exit2(self):
        with self.assertRaises(SystemExit) as cm:
            self.k.load_hb_auth(env={"HB_ADMIN_USER": "", "HB_ADMIN_PASS": "x"})
        self.assertEqual(cm.exception.code, 2)

    def test_env_vars_ok_and_never_printed(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            auth = self.k.load_hb_auth(
                env={"HB_ADMIN_USER": "sen" + "tentUser",
                     "HB_ADMIN_PASS": "sen" + "tentPass123"})
        self.assertEqual(auth, ("sententUser", "sententPass123"))
        self.assertNotIn("sententPass123", buf.getvalue())

    def test_env_file_600_ok_world_readable_red(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hb.env")
        with open(p, "w") as fh:
            fh.write("HB_ADMIN_USER=u1\nHB_ADMIN_PASS=p1\n")
        os.chmod(p, 0o600)
        env = {"HB_ADMIN_ENV_FILE": p}
        self.assertEqual(self.k.load_hb_auth(env=env), ("u1", "p1"))
        self.assertEqual(env["HB_ADMIN_USER"], "u1")   # setdefault ile dolar
        os.chmod(p, 0o644)
        with self.assertRaises(SystemExit) as cm:
            self.k.load_hb_auth(env={"HB_ADMIN_ENV_FILE": p})
        self.assertEqual(cm.exception.code, 2)

    def test_env_file_unreadable_red(self):
        with self.assertRaises(SystemExit) as cm:
            self.k.load_hb_auth(env={"HB_ADMIN_ENV_FILE": "/nonexistent/hb.env"})
        self.assertEqual(cm.exception.code, 2)


class TestSecretScanner(unittest.TestCase):
    def _poison(self, content):
        p = os.path.join(tempfile.mkdtemp(), "poison.txt")
        with open(p, "w") as fh:
            fh.write(content)
        return p

    def test_literal_hb_assignment_caught(self):
        p = self._poison('HB_' + 'PASS="sup" + "ersecret"\n')
        self.assertTrue(SS.scan_paths([p]))

    def test_basic_auth_caught(self):
        text = 'curl -s -u "ad' + 'min:s3cret" http://x\n'
        self.assertTrue(any(h[1] == "BASIC-AUTH" for h in SS.scan_text(text)))
        self.assertTrue(SS.scan_paths([self._poison(text)]))

    def test_placeholder_not_flagged(self):
        p = self._poison('BINANCE_KEY="YOUR_KEY_HERE"\n')
        self.assertEqual(SS.scan_paths([p]), [])

    def test_known_digest_extra(self):
        secret = "averylongsentinelsecret:andsecondhalf"
        text = 'pair = "%s"\n' % secret
        hits = SS.scan_text(text, extra_digests=[hashlib.sha256(secret.encode()).hexdigest()])
        self.assertTrue(any(h[1] == "KNOWN-DIGEST" for h in hits), hits)
        self.assertNotIn(secret.split(":")[0], "".join(h[2] for h in hits))

    def test_repo_subset_clean(self):
        files = ["kill_spot_grid.py", "kill_spot_grid_c0.sh", "tools/secret_scan.py",
                 "tools/testnet_smoke.py", "tools/h5_runner.py", "validation_budget.py",
                 "close_adapter.py", "close_coordinator.py"]
        findings = SS.scan_paths([os.path.join(_ROOT, f) for f in files])
        self.assertEqual(findings, [], findings)

    def test_cli_exit_codes(self):
        sentinel = "verylongsentinelvalue"
        p = self._poison('HB_' + 'USER="' + sentinel + '"\n')
        r = subprocess.run([sys.executable, "-I",
                            os.path.join(_ROOT, "tools", "secret_scan.py"), p],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(sentinel, r.stdout)      # bulgu maskelenir
        r2 = subprocess.run([sys.executable, "-I",
                             os.path.join(_ROOT, "tools", "secret_scan.py"),
                             os.path.join(_ROOT, "validation_budget.py")],
                            capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0)


if __name__ == "__main__":
    unittest.main()
