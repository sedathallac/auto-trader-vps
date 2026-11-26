#!/usr/bin/env bash
# FAZ 9 + GridC0 deploy — YEREL makinenden çalıştır (ssh vps-bot ile VPS'e bağlanır; VPS'te root olduğun için sudo yok).
#   ./deploy/faz9_gridc0_deploy.sh --check   # yalnız OKUMA: ön kontrol, hiçbir şeyi değiştirmez
#   ./deploy/faz9_gridc0_deploy.sh           # gerçek deploy
# Dokunmaz: auto_trader.service (canlı futures botu), GridC0'ın borsadaki emirleri (hiçbir iptal/halt yok).
set -euo pipefail
HOST="${HOST:-vps-bot}"
BUNDLE="${BUNDLE:-$HOME/gridc0-faz10.bundle}"
MODE="apply"; [ "${1:-}" = "--check" ] && MODE="check"
say() { printf '\n=== %s ===\n' "$*"; }
R() { ssh -o BatchMode=yes "$HOST" "$@"; }

say "0/6 ön kontrol ($MODE)"
[ -f "$BUNDLE" ] && git bundle verify "$BUNDLE" >/dev/null 2>&1 || { echo "bundle yok/bozuk: $BUNDLE"; exit 1; }
R 'bash -s' <<'REMOTE'
set -euo pipefail
echo "futures canlı bot : $(systemctl is-active auto_trader) since $(systemctl show auto_trader -p ActiveEnterTimestamp --value)"
echo "gridc0-live       : $(systemctl is-active gridc0-live) since $(systemctl show gridc0-live -p ActiveEnterTimestamp --value)"
echo "auto_trader_faz9  : $(systemctl is-active auto_trader_faz9) enabled=$(systemctl is-enabled auto_trader_faz9 2>&1 || true)"
for d in /opt/gridc0 /opt/auto_trader_faz9/app; do
  cd "$d"
  [ -z "$(git status --porcelain)" ] || { echo "HATA: $d içinde commit edilmemiş değişiklik var:"; git status --short; exit 1; }
  echo "$d : $(git rev-parse --abbrev-ref HEAD) $(git rev-parse --short HEAD)"
done
/root/venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/opt/gridc0/data/gridc0.db?mode=ro", uri=True, timeout=10)
print("gridc0 açık emirler (DB):", c.execute("select side,status,count(*) from orders where status in ('NEW','PARTIALLY_FILLED','SENT','UNKNOWN') group by side,status").fetchall())
print("gridc0 halt kaydı        :", c.execute("select count(*) from meta where key='engine_halt'").fetchone()[0], "(0 olmalı)")
PY
REMOTE
if [ "$MODE" = "check" ]; then echo; echo "(--check: hiçbir şey değiştirilmedi)"; exit 0; fi

say "1/6 GridC0: bundle'ı VPS'e kopyala + değişiklik sınıflandırması"
scp -q "$BUNDLE" "$HOST":/root/gridc0-faz10.bundle
R 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/gridc0
BR=$(git rev-parse --abbrev-ref HEAD); CUR=$(git rev-parse HEAD)
git fetch -q /root/gridc0-faz10.bundle feature/faz10-robustness
NEW=$(git rev-parse FETCH_HEAD)
echo "mevcut $CUR ($BR) -> hedef $NEW"
if [ "$CUR" = "$NEW" ]; then echo "GridC0 zaten güncel"; echo none > /root/.gridc0_deploy_action; exit 0; fi
git merge-base --is-ancestor "$CUR" "$NEW" || { echo "HATA: hedef mevcut sürümün devamı değil (fast-forward değil); durduruldu"; exit 1; }
CHANGED=$(git diff --name-only "$CUR" "$NEW" | grep -vE '^(scripts/|tests/|docs/|config\.live\.template\.json)' || true)
git diff --stat "$CUR" "$NEW" | tail -3
if [ -n "$CHANGED" ]; then echo "çalışma zamanı dosyaları değişti -> yeniden başlatma gerekli:"; echo "$CHANGED"; echo restart > /root/.gridc0_deploy_action
else echo "yalnız scripts/tests/docs değişti -> servis yeniden BAŞLATILMAZ (açık emirlere/döngüye dokunulmaz)"; echo ffonly > /root/.gridc0_deploy_action; fi
echo "$NEW" > /root/.gridc0_deploy_target
REMOTE

say "2/6 GridC0: deploy"
R 'bash -s' <<'REMOTE'
set -euo pipefail
ACTION=$(cat /root/.gridc0_deploy_action)
cd /opt/gridc0
if [ "$ACTION" = "none" ]; then echo "yapılacak bir şey yok"; else
  NEW=$(cat /root/.gridc0_deploy_target)
  if [ "$ACTION" = "restart" ]; then systemctl stop gridc0-live; fi
  git merge --ff-only "$NEW"          # reset --hard YOK: yerel değişiklik varsa durur
  if [ "$ACTION" = "restart" ]; then
    # (tests, python run_tests.py ile ayrı doğrulanır; burada servis kodu değişti)
    systemctl start gridc0-live
  fi
fi
# legacy meta anahtarı (idempotent; zaten silindiyse 'nothing to clean')
/root/venv/bin/python scripts/cleanup_legacy_meta.py --db /opt/gridc0/data/gridc0.db --apply
git log --oneline -1
REMOTE

say "3/6 GridC0: doğrulama"
sleep 20
R 'bash -s' <<'REMOTE'
set -euo pipefail
systemctl is-active gridc0-live
journalctl -u gridc0-live -n 60 --no-pager | grep -E 'tick planned' | tail -2 | tee /tmp/.g0tick
grep -q 'blocked=False halted=False' /tmp/.g0tick || { echo "HATA: blocked=False halted=False görülmedi"; exit 1; }
set -a; . /etc/gridc0/gridc0.env; set +a
cd /opt/gridc0 && /root/venv/bin/python ops_cli.py status
/root/venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/opt/gridc0/data/gridc0.db?mode=ro", uri=True, timeout=10)
rows = c.execute("select side,status,count(*) from orders where status in ('NEW','PARTIALLY_FILLED','SENT','UNKNOWN') group by side,status").fetchall()
print("açık emirler (DB):", rows)
assert ("SELL", "NEW", 16) in rows, "16 TP SELL beklenirdi"
assert c.execute("select count(*) from meta where key='engine_halt'").fetchone()[0] == 0
print("OK: 16 TP SELL yerinde, halt yok")
PY
REMOTE

say "4/6 Faz 9: yeni kodu al, ayrı worktree'de TEST ET (başarısızsa durur, servise dokunulmaz)"
R 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/auto_trader_faz9/app
git fetch -q origin feature/futures-release-blockers
NEW=$(git rev-parse FETCH_HEAD); echo "$NEW" > /root/.faz9_deploy_target
echo "faz9 mevcut $(git rev-parse --short HEAD) -> hedef ${NEW:0:7}"
rm -rf /tmp/faz9-wt; git worktree prune; git worktree add -q --detach /tmp/faz9-wt "$NEW"
cd /tmp/faz9-wt
HOME=$(mktemp -d) /opt/auto_trader_faz9/venv/bin/python -m pytest -q -p no:cacheprovider \
   --ignore=test_c3.py --ignore=test_fixes.py --ignore=test_c6.py 2>&1 | tail -4
cd /opt/auto_trader_faz9/app && git worktree remove --force /tmp/faz9-wt
REMOTE

say "5/6 Faz 9: durdur -> reset --hard -> eski (spoof-kirli) whale DB'yi kenara al -> env -> enable + start"
R 'bash -s' <<'REMOTE'
set -euo pipefail
NEW=$(cat /root/.faz9_deploy_target)
LIVE_BEFORE=$(systemctl show auto_trader -p MainPID,ActiveEnterTimestamp --value | tr '\n' ' ')
systemctl stop auto_trader_faz9
cd /opt/auto_trader_faz9/app && git reset --hard "$NEW" && git log --oneline -1
H=/var/lib/auto_trader_faz9; TS=$(date +%s)
if [ -f "$H/whale_metrics.db" ]; then
  mv "$H/whale_metrics.db" "$H/whale_metrics.db.pre-spoof-fix-$TS"
  echo "eski whale_metrics.db kenara alındı (15 dk'lık spoof kuralıyla toplanmış, analizi bozar): pre-spoof-fix-$TS"
fi
grep -q '^TOP_N_COMPARE=' /etc/auto_trader/auto_trader.env || echo 'TOP_N_COMPARE=20' >> /etc/auto_trader/auto_trader.env
grep -E '^(HB_OFFLINE|WHALE_HUNTING|STRATEGY_CONFIRMATION|SCIENTIFIC_SIZING|HIERARCHICAL_SCAN|TOP_N_COMPARE|MAX_OPEN_POSITIONS)=' /etc/auto_trader/auto_trader.env
systemctl enable auto_trader_faz9 >/dev/null 2>&1
date '+%Y-%m-%d %H:%M:%S' > /root/.faz9_started_at
systemctl start auto_trader_faz9
LIVE_AFTER=$(systemctl show auto_trader -p MainPID,ActiveEnterTimestamp --value | tr '\n' ' ')
[ "$LIVE_BEFORE" = "$LIVE_AFTER" ] || { echo "HATA: canlı futures botu değişti!"; exit 1; }
echo "canlı futures botu dokunulmadı: $LIVE_AFTER"
REMOTE

say "6/6 Faz 9: doğrulama (ilk tarama ~2 dk sürer: 2 ana aday + 18 probe, her biri ~10 sn örnekleme)"
sleep 120
R 'bash -s' <<'REMOTE'
set -euo pipefail
SINCE=$(cat /root/.faz9_started_at)
J=$(journalctl -u auto_trader_faz9 --since "$SINCE" --no-pager)
echo "$J" | grep -E 'HB_OFFLINE aktif|Faz[3-6]\[compare\]|probe' | tail -12 | cut -c1-200
echo "--- sayımlar"
n() { echo "$J" | grep -c -E "$1" || true; }
echo "HB_OFFLINE aktif : $(n 'HB_OFFLINE aktif')"
echo "Faz5[compare]    : $(n 'Faz5\[compare\]')"
echo "Faz6[compare]    : $(n 'Faz6\[compare\]')"
echo "probe satırı     : $(n 'probe')"
echo "Traceback        : $(n 'Traceback')"
echo "spoof=True       : $(n 'spoof=True')   spoof=False: $(n 'spoof=False')"
[ "$(n 'HB_OFFLINE aktif')" -ge 1 ] || { echo "HATA: HB_OFFLINE aktif görülmedi"; exit 1; }
[ "$(n 'Faz5\[compare\]')" -ge 1 ] || { echo "HATA: Faz5[compare] satırı yok"; exit 1; }
[ "$(n 'Traceback')" -eq 0 ] || { echo "HATA: Traceback var"; exit 1; }
systemctl is-active auto_trader_faz9
cd /opt/auto_trader_faz9/app && HOME=/var/lib/auto_trader_faz9 ../venv/bin/python monitor_live.py 2>&1 | grep -v RuntimeWarning || true
REMOTE
echo; echo "TAMAM. Faz 9 verisi bundan sonra temiz spoof kuralıyla birikir; ilk ~1 saat sonra tekrar: ssh $HOST 'cd /opt/auto_trader_faz9/app && HOME=/var/lib/auto_trader_faz9 ../venv/bin/python monitor_live.py'"
