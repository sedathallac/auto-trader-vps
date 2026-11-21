#!/usr/bin/env bash
# BLOK12 §3 — GERÇEK deploy preflight'ı (BLOK12 öncesi: --ignore=test_c3.py
# test_fixes.py test_c6.py — bu dosyalar ağaçta YOKTU; bayat ignore'lar
# "yeşil" görüntüsü üretiyordu. Artık İGNORE YOK: suite tümüyle yeşil olmalı).
#
# Kullanım:  deploy/faz9_preflight.sh [REPO_DIR] [REV]
#   REPO_DIR (varsayılan: içinde bulunulan dizin) — git deposu/worktree
#   REV      (varsayılan: HEAD)                  — doğrulanacak devir
#   PYTHON   env                — yorumlayıcı (varsayılan: python3)
#
# İzolasyon: hedef REV için GEÇİCİ worktree + GEÇİCİ HOME; çalışan servise ve
# gerçek ~/.pending_fills.json'a dokunmaz (salt-okuma; FAZ14w sızıntı dersi).
# Kapılar: 1) secret_scan (TEMİZ değilse RED)  2) TAM test paketi — kök
# test_*.py + tests/ + BOT_KNOWLEDGE_BASE/TESTING/tests, tek bare pytest
# (pytest.ini norecursedirs: evidence/.git/.claude). Herhangi bir başarısızlık
# → sıfır OLMAYAN çıkış (set -euo pipefail; PIPESTATUS yok — pipe kullanılmaz).
set -euo pipefail

REPO_DIR="$(cd "${1:-$(pwd)}" && pwd)"
REV="${2:-HEAD}"
PY="${PYTHON:-python3}"

if [ ! -e "$REPO_DIR/.git" ]; then
    echo "PREFLIGHT RED: $REPO_DIR bir git deposu/worktree değil" >&2
    exit 2
fi

ROOT_TMP="$(mktemp -d)"
WT="$ROOT_TMP/wt"
cleanup() {
    git -C "$REPO_DIR" worktree remove --force "$WT" 2>/dev/null || true
    rm -rf "$ROOT_TMP"
}
trap cleanup EXIT

git -C "$REPO_DIR" worktree prune
git -C "$REPO_DIR" worktree add -q --detach "$WT" "$REV"
cd "$WT"
echo "== PREFLIGHT rev=$(git rev-parse --short HEAD) wt=$WT py=$PY =="

# 1) Sır veren tarama: maskelenmiş çıktı; bulgu → exit 1 → preflight RED.
"$PY" -I tools/secret_scan.py

# 2) TAM test paketi (ignore YOK). pytest.ini norecursedirs yeterlidir.
HOME="$(mktemp -d "$ROOT_TMP/home-XXXX")" "$PY" -I -m pytest -q -p no:cacheprovider

echo "PREFLIGHT OK: $(git rev-parse HEAD)"
