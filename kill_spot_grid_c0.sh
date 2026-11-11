#!/bin/bash
# C0 Grid Emergency Kill Switch
# ONLY affects the executor passed as $1, cancels BTC-USDT orders
# Does NOT touch auto_trader, MAX_OPEN_POSITIONS, or any other service
set -e
EID="$1"
if [ -z "$EID" ]; then echo "Usage: $0 <executor_id>"; exit 1; fi
HB_USER="furkan_admin"; HB_PASS="HmB0t_S3cure_2026x"; HB_URL="http://localhost:8000"

echo "=== 1) HB API: STOP executor $EID ==="
curl -s -X POST -u "$HB_USER:$HB_PASS" "$HB_URL/executors/$EID/stop" \
  -H "Content-Type: application/json" \
  -d '{"close_type": "EARLY_STOP", "keep_position": false}' | python3 -m json.tool || echo "  (HB stop failed — proceeding to Binance-side)"

echo
echo "=== 2) Binance: Cancel all BTCUSDT open orders ==="
source /root/.env
/root/venv/bin/python -c "
import os, hmac, hashlib, requests, time
k = os.environ['BINANCE_KEY']; s = os.environ['BINANCE_SECRET']
ts = int(time.time()*1000)
q = symbol=BTCUSDT
