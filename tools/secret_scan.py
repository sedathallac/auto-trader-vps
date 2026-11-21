#!/usr/bin/env python3
"""BLOK12 §5 — SECRET SCANNER (yerel, offline, fail-closed).

Taranan: `git ls-files` altındaki tüm izlenen dosyalar (veya verilen yollar).
Bulgular `dosya:satır` + MASKOLENMİŞ satır olarak raporlanır; secret değeri
asla basılmaz. Kaçış kodu: 0 = temiz, 1 = bulgu, 2 = kullanım/erişim hatası.

Tipler:
  LITERAL-HB     HB_ADMIN_USER/PASS/AUTH/HB_USER/HB_PASS sabit ataması
  BASIC-AUTH     curl -u "user:pass" düz metin
  LITERAL-KEY    BINANCE_KEY/SECRET sabit ataması (placeHolder şablonları hariç)
  LONG-SECRET    60-128 [A-Za-z0-9] + satırda key/secret bağlamı
  KNOWN-DIGEST   sha256(bilinmiş-sızdırmış-değer) imzası (değer gömülü değil)

Git GEÇMİŞİ taranmaz ve temizlenmez: geçmişteki eski heartbeat kimliği
raporda sabit olarak bildirilir (docs/OPERATOR_ROTATION_HEARTBEAT_20261121.md);
geçmişteki kimlik ROTASYON ile geçersiz kılınır, silme ile değil.
"""
import hashlib
import re
import subprocess
import sys

# BLOK12 §5: dosyada düz metin gömülü iken sızmış eski heartbeat kimliklerinin
# sha256 imzaları (DEĞERLERİ DEĞİL). user:pass ve pass ayrık imzalar.
KNOWN_CRED_DIGESTS = {
    "bc12d4a0c880f614b39dc80bcbc1ae1f846f463711d94c71c0b02b4603d07633",
    "75e9700b440462f77c2d6668d77074bbdadd9634e51b589552a870d765b8d166",
}

# BLOK12 §3: belgegeçer (belge/test) bağlamları — YAPIŞTIRMA ŞABLONU ve BOŞ
# sağ-taraf, gerçek kimlik İÇERMEZ: `%s`/`%d` format şablonu, `(user, pass)`
# sözlüğü, `= ""`/`= ")` boş alıtek. Değer-digest eşleşmesi bundan ETKİLENMEZ
# (KNOWN_CRED_DIGESTS ayrıca ve her satırda çalışır).
_PLACEHOLDER = re.compile(
    r"(<[^>]+>|CHANGE_?ME|xxx+|YOUR_|\$\{|\$[A-Z_]+|example|user:pass"
    r"|\(user, pass\)|%[sd]\b|=\s*[\"']\s*[,)])",
    re.IGNORECASE)

_PATTERNS = [
    ("LITERAL-HB", re.compile(
        r"\b(HB_(USER|PASS|AUTH)|HB_ADMIN_(USER|PASS))\s*[:=]\s*[\"'(]")),
    ("BASIC-AUTH", re.compile(r"""-u\s+["'][^"'$\s]+:[^"'$\s]+["']""")),
    ("LITERAL-KEY", re.compile(
        r"""BINANCE_(KEY|SECRET)\s*=\s*['"][^'"]{16,}['"]""")),
    ("LONG-SECRET", re.compile(r"\b[A-Za-z0-9]{60,128}\b")),
]
_LONG_CTX = re.compile(r"key|secret|token|pass", re.IGNORECASE)


def _mask(line):
    """Tokenları maskele — bulgu raporu secret yayınlamaz."""
    return re.sub(r"[A-Za-z0-9_./+-]{12,}", lambda m: m.group(0)[:4] + "***", line)


def scan_text(text, extra_digests=()):
    """[(line_no, type, masked_line)] — KNOWN-DIGEST metnin tamamına uygulanır."""
    hits = []
    lines = text.splitlines()
    digests = set(KNOWN_CRED_DIGESTS) | set(extra_digests or ())
    if digests:
        whole = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
        for d in sorted(digests):
            if d == whole:
                hits.append((0, "KNOWN-DIGEST", "<file sha256 masked>"))
        # satır-ici gömülü: her satırın tüm alt dizileri pratik değil;
        # user:pass biçimli görünen alanları dene.
        for ln, line in enumerate(lines, 1):
            for tok in re.findall(r"[^\s:,'\"=()]+:[^\s,'\"()]+", line):
                if hashlib.sha256(tok.encode()).hexdigest() in digests:
                    hits.append((ln, "KNOWN-DIGEST", _mask(line)))
            for tok in re.findall(r"[^\s,'\"=()]+", line):
                if hashlib.sha256(tok.encode()).hexdigest() in digests:
                    hits.append((ln, "KNOWN-DIGEST", _mask(line)))
    for ln, line in enumerate(lines, 1):
        if "secret_scan" in line or "KNOWN_CRED_DIGESTS" in line:
            continue  # scanner'ın kendi imza listesi bulgu değildir
        for typ, rx in _PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            if typ == "LONG-SECRET" and not _LONG_CTX.search(line):
                continue
            if _PLACEHOLDER.search(line):
                continue
            hits.append((ln, typ, _mask(line.strip()[:160])))
    return hits


def scan_paths(paths, extra_digests=()):
    findings = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
        except IsADirectoryError:
            continue                    # gitlink/dizin girdisi (ör. submodule)
        except OSError as e:
            print(f"ERİŞİM HATASI {p}: {e.__class__.__name__}")
            return None
        if b"\0" in raw[:4096]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for ln, typ, masked in scan_text(text, extra_digests):
            findings.append((p, ln, typ, masked))
    return findings


def main(argv):
    if len(argv) > 1 and argv[1] not in ("--all",):
        paths = argv[1:]
    else:
        r = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
        if r.returncode != 0:
            print("git ls-files başarısız")
            return 2
        paths = [p for p in r.stdout.splitlines() if p]
    findings = scan_paths(paths)
    if findings is None:
        return 2
    for p, ln, typ, masked in findings:
        print(f"FINDING {p}:{ln} [{typ}] {masked}")
    if findings:
        print(f"SECRET SCAN: {len(findings)} BULGU — RED")
        return 1
    print(f"SECRET SCAN: TEMİZ ({len(paths)} dosya)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
