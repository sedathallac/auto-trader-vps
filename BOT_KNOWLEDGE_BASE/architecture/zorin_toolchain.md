# Zorin Geliştirme Makinesi — Araç Zinciri (DOĞRULANDI 2026-11-11)

Makine: `reisfurkan@FURKAN` (Zorin OS, Ubuntu noble tabanlı). Bu kurulum
kullanıcının **yerel geliştirme makinesinde** yapıldı; canlı VPS ve kimlik
bilgilerine dokunulmadı. Tüm sürümler gerçek `--version` çıktısıyla doğrulandı.

## Mevcut taban
- Claude Code **2.1.322** (native kurulum, `~/.local/bin/claude`; `claude install`
  ile npm-global 2.1.302'den geçildi, oto-güncelleme sorunu çözüldü).
- git 2.43.0 · docker 29.1.3 · node v24.22.2 · npm 11.19.1
- Python: miniconda base **3.14.9** (çok yeni) + sistem **3.12.3**.
  Araç venv'i kasıtlı olarak **3.12** ile kuruldu (wheel uyumu için).

## Sistem araçları (apt — DOĞRULANDI)
rg 14.1.0 · fdfind 9.0.0 · jq 1.7 · tree 2.1.1 · tmux 3.4 · sqlite3 3.53.4 ·
psql 16.15 · bats 1.10.0 · yamllint 1.33.0 · shellcheck 0.9.0

## Güvenlik tarayıcıları (DOĞRULANDI)
gitleaks 8.21.2 · trivy 0.76.2 · actionlint 1.7.12 · shfmt 3.10.0

## Python araç ortamı (DOĞRULANDI)
Konum: `~/.local/share/bot-tooling/venv` (Python 3.12.3, sistem Python'una dokunmaz).
pytest 9.2.0 · pytest-cov 7.1.0 · pytest-timeout 2.4.0 · pytest-mock 3.17.0 ·
hypothesis 6.170.15 · coverage 7.17.2 · ruff 0.17.4 · mypy 2.4.1 · bandit 1.9.4 ·
pip-audit 2.10.1 · detect-secrets 1.5.0 · pre-commit 4.7.0 · semgrep 1.185.0 ·
psycopg 3.3.8 (binary, import OK) · requests 2.34.2
PATH'e eklendi: `~/.bashrc` içine `~/.local/share/bot-tooling/venv/bin`.

## Smoke testleri (DOĞRULANDI)
- `psycopg` import OK.
- `bandit` B105 (hardcoded password) bulgusu üretti, exit 1 (beklenen).
- `gitleaks detect` ev dizininde "no leaks found", exit 0.

## Sonraki adımlar (güvenli kapsam)
- CI workflow dosyaları (ayrı branch, mock tabanlı; canlı borsa API'si YOK).
- VPS tarafı salt okunur erişim boşlukları (postgres driver, docker inspect,
  /proc) — bunlar korumalar sökülerek değil, dar/özel izinlerle ele alınmalı.
