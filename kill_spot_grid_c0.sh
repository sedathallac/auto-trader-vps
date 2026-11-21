#!/bin/bash
# C0 Grid Emergency Kill Switch — BLOK12 §5: GÖMÜLÜ KİMLİK YOK.
# ONLY affects the executor passed as $1, cancels BTC-USDT orders.
# Does NOT touch auto_trader, MAX_OPEN_POSITIONS, or any other service.
#
# Kimlik sözleşmesi (fail-closed):
#   HB_ADMIN_USER / HB_ADMIN_PASS ortamda (veya HB_ADMIN_ENV_FILE=... 0600 dosya)
#   olmadan HB adımına gidilmez — kill_spot_grid.py load_hb_auth() RED verir.
# Eski gömülü kimlik git geçmişinde yaşıyor; ROTASYON zorunludur:
#   docs/OPERATOR_ROTATION_HEARTBEAT_20261121.md
set -euo pipefail
EID="$1"
if [ -z "$EID" ]; then echo "Usage: $0 <executor_id>"; exit 1; fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Binance anahtarları (operatör VPS ortamı) — yoksa python tarafı KeyError ile düşer.
[ -f /root/.env ] && source /root/.env

# Tek gerçek uygulama kill_spot_grid.py'dedir (yalnız test edilmiş yol).
exec "${PYTHON:-/root/venv/bin/python}" "$HERE/kill_spot_grid.py" "$EID"
