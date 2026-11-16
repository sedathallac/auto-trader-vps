#!/bin/sh
# faz9_live_status.sh — tek satirlik canli durum. Salt-okunur; hicbir sey yazmaz.
# Kullanim: /opt/auto_trader_faz9/app/deploy/faz9_live_status.sh
set -u
STATE=/var/lib/auto_trader_faz9_live
PY=/opt/auto_trader_faz9/venv/bin/python

svc=$(systemctl is-active auto_trader_faz9_live 2>/dev/null || echo inactive)
pid=$(systemctl show -p MainPID --value auto_trader_faz9_live 2>/dev/null || echo 0)
restarts=$(systemctl show -p NRestarts --value auto_trader_faz9_live 2>/dev/null || echo 0)

metrics=$("$PY" - "$STATE" <<'PY'
import os, sqlite3, sys, time
state = sys.argv[1]
pending = 0
pnl = 0.0
halted = 0
try:
    c = sqlite3.connect(f"file:{state}/signal_outcomes.db?mode=ro", uri=True, timeout=5)
    pending = c.execute("SELECT COUNT(*) FROM signal_outcomes WHERE resolved=0").fetchone()[0]
    c.close()
except Exception:
    pass
try:
    r = sqlite3.connect(f"file:{state}/risk_ledger.db?mode=ro", uri=True, timeout=5)
    row = r.execute("SELECT daily_pnl, is_halted FROM risk_state WHERE id=1").fetchone()
    if row:
        pnl, halted = float(row[0] or 0.0), int(row[1] or 0)
    r.close()
except Exception:
    pass
hb = os.path.join(state, ".auto_trader.heartbeat")
age = int(time.time() - os.path.getmtime(hb)) if os.path.exists(hb) else -1
print(f"pnl={pnl:.2f} halted={halted} pending_signals={pending} heartbeat_age_s={age}")
PY
)

echo "live=$svc pid=$pid restarts=$restarts $metrics"
