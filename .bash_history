cd /etc/netplan
şs
ls
cd
ls
shutdown
apt update && apt upgrade -y && apt install -y curl wget git nano htop unzip
reboot
ps aux | grep -E "apt|docker" | grep -v grep
docker --version && docker compose version
cd /root && git clone https://github.com/hummingbot/hummingbot-api.git
cd /root/hummingbot-api && ls -la
cd /root/hummingbot-api && ./setup.sh
HummingbotApi2026
HummingbotConfig2026
cd /root/hummingbot-api && make deploy
cd /root/hummingbot-api && make doctor
curl -v http://localhost:8000/system/resources
curl -s -u "furkan_admin:HummingbotApi2026" http://localhost:8000/system/resources | head -c 300
cat /root/hummingbot-api/.env | grep -E "^USERNAME|^PASSWORD|^CONFIG_PASSWORD"
curl -s -u "furkan_admin:HmB0t_S3cure_2026x" http://localhost:8000/system/resources | head -c 300
scp ~/auto_trader.py ~/orphan_killer.py ~/archiver.py ~/archive_report.py ~/.config/hummingbot/binance_keys.env ~/.auto_trader_blacklist.json ~/.tracked_symbols.json root@179.61.207.234:/root/
exit
uname -r
curl -fsSL https://get.docker.com | sh
mkdir -p /root/.config/hummingbot && mv /root/binance_keys.env /root/.config/hummingbot/binance_keys.env && chmod 600 /root/.config/hummingbot/binance_keys.env && ls -la /root/.config/hummingbot/
cd /root && ls -la auto_trader.py orphan_killer.py archiver.py archive_report.py .auto_trader_blacklist.json .tracked_symbols.json
scp ~/risk_engine.py root@179.61.207.234:/root/
exit
export HB_USERNAME="furkan_admin" && export HB_PASSWORD="HmB0t_S3cure_2026x" && export BINANCE_KEY="$(awk -F' = ' '/binance_perpetual_api_key/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')" && export BINANCE_SECRET="$(awk -F' = ' '/binance_perpetual_api_secret/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')" && /root/venv/bin/python /root/auto_trader.py --dry
cat > /root/.env << 'EOF'
HB_USERNAME="furkan_admin"
HB_PASSWORD="HmB0t_S3cure_2026x"
BINANCE_KEY="$(awk -F' = ' '/binance_perpetual_api_key/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')"
BINANCE_SECRET="$(awk -F' = ' '/binance_perpetual_api_secret/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')"
EOF

cat /root/.env
cat > /root/.env << EOF
HB_USERNAME="furkan_admin"
HB_PASSWORD="HmB0t_S3cure_2026x"
BINANCE_KEY="$(awk -F' = ' '/binance_perpetual_api_key/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')"
BINANCE_SECRET="$(awk -F' = ' '/binance_perpetual_api_secret/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')"
EOF

chmod 600 /root/.env
cat /root/.env
cat > /etc/systemd/system/auto_trader.service << 'EOF'
[Unit]
Description=Hummingbot Auto Trader
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/root
EnvironmentFile=/root/.env
ExecStart=/root/venv/bin/python /root/auto_trader.py --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/orphan_killer.service << 'EOF'
[Unit]
Description=Hummingbot Orphan Killer
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/root
EnvironmentFile=/root/.env
ExecStart=/root/venv/bin/python /root/orphan_killer.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/archiver.service << 'EOF'
[Unit]
Description=Hummingbot Archiver
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/root
EnvironmentFile=/root/.env
ExecStart=/root/venv/bin/python /root/archiver.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl status auto_trader.service orphan_killer.service archiver.service --no-pager
systemctl daemon-reload
systemctl enable --now auto_trader.service orphan_killer.service archiver.service
systemctl status auto_trader.service orphan_killer.service archiver.service --no-pager
journalctl -u auto_trader.service -f
docker logs hummingbot-api --tail 30
curl -s -u "furkan_admin:HmB0t_S3cure_2026x" http://localhost:8000/accounts/ | head -c 300
grep -n -A15 "def create_executor" /root/auto_trader.py
curl -X POST http://localhost:8000/accounts/master_account/credentials/binance_perpetual   -u "furkan_admin:HmB0t_S3cure_2026x"   -H "Content-Type: application/json"   -d "{\"api_key\": \"$(grep BINANCE_KEY /root/.env | cut -d'\"' -f2)\", \"api_secret\": \"$(grep BINANCE_SECRET /root/.env | cut -d'\"' -f2)\"}"
systemctl restart auto_trader.service
journalctl -u auto_trader.service -f
source /root/.env && curl -X POST http://localhost:8000/accounts/master_account/credentials/binance_perpetual -u "furkan_admin:HmB0t_S3cure_2026x" -H "Content-Type: application/json" -d "{\"api_key\": \"${BINANCE_KEY}\", \"api_secret\": \"${BINANCE_SECRET}\"}"
python3 -c "import json; p='/root/.auto_trader_blacklist.json'; d=json.load(open(p)); d.pop('ETHUSDT', None); d.pop('XRPUSDT', None); json.dump(d, open(p,'w'), indent=2); print(f'✅ Kalan: {len(d)} sembol')"
systemctl restart auto_trader.service
source /root/.env && curl -X POST "http://localhost:8000/accounts/add-credential/master_account/binance_perpetual" -u "furkan_admin:HmB0t_S3cure_2026x" -H "Content-Type: application/json" -d "{\"binance_perpetual_api_key\": \"${BINANCE_KEY}\", \"binance_perpetual_api_secret\": \"${BINANCE_SECRET}\"}"
systemctl restart auto_trader.service
sleep 30 && journalctl -u auto_trader.service -f --no-pager
rm -f /root/.auto_trader_blacklist.json
systemctl restart auto_trader.service
sleep 30 && journalctl -u auto_trader.service -f --no-pager
cd /root && for f in auto_trader.py risk_engine.py orphan_killer.py archiver.py archive_report.py; do python3 -c "import ast; ast.parse(open('$f').read())" && echo " $f OK" || echo " $f HATA"; done
python3 -c "import requests, eth_account, hmac, sqlite3; print(' Bamllklar OK')" 2>&1
pip3 install --break-system-packages requests eth-account
apt update && apt install -y python3-pip
pip3 install --break-system-packages requests eth-account
python3 -c "import requests, eth_account, hmac, sqlite3; print(' Bamllklar OK')"
python3 -m pip install --break-system-packages requests eth-account
python3 -c "import requests, eth_account, hmac, sqlite3; print(' Bamllklar OK')"
python3 -m venv /root/venv
source /root/venv/bin/activate && pip install requests eth-account
python3 -c "import eth_account; print(' eth_account OK')"
pip install requests eth-account
python3 -c "import eth_account; print(' eth_account OK')"
apt update && apt install -y python3-venv
python3 -m venv /root/venv
source /root/venv/bin/activate
pip install requests eth-account
python3 -c "import eth_account; print(' eth_account OK')"
export HB_USERNAME="furkan_admin" && export HB_PASSWORD="HmB0t_S3cure_2026x" && /root/venv/bin/python /root/auto_trader.py --dry
export HB_USERNAME="furkan_admin" && export HB_PASSWORD="HmB0t_S3cure_2026x" && export BINANCE_KEY="$(awk -F' = ' '/binance_perpetual_api_key/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')" && export BINANCE_SECRET="$(awk -F' = ' '/binance_perpetual_api_secret/{print $2; exit}' /root/.config/hummingbot/binance_keys.env | tr -d '[:space:]')" && /root/venv/bin/python /root/auto_trader.py --dry
uptime && echo "═══" && systemctl status auto_trader.service orphan_killer.service archiver.service --no-pager | grep -E 'Active:|●|Restart' && echo "═══" && top -bn1 | head -15
dmesg -T | grep -iE "out of memory|oom|panic|killed|watchdog|reset|reboot" | tail -20
journalctl -b -1 -n 30 --no-pager | tail -30
echo "═══ KİM BAĞLANDI + REBOOT GEÇMİŞİ ═══" && last -x | head -15 && echo "" && echo "═══ KERNEL UPDATE GEÇMİŞİ ═══" && grep -iE "kernel|linux-image|reboot" /var/log/apt/history.log | tail -15
sed -i 's|//Unattended-Upgrade::Automatic-Reboot "false";|Unattended-Upgrade::Automatic-Reboot "false";|' /etc/apt/apt.conf.d/50unattended-upgrades && sed -i 's|Unattended-Upgrade::Automatic-Reboot "true";|Unattended-Upgrade::Automatic-Reboot "false";|' /etc/apt/apt.conf.d/50unattended-upgrades && grep -iE "automatic-reboot" /etc/apt/apt.conf.d/50unattended-upgrades | head -3
echo "═══ UPTIME ═══" && uptime && echo "" && echo "═══ SERVİSLER ═══" && systemctl is-active auto_trader.service orphan_killer.service archiver.service && echo "" && echo "═══ DOCKER ═══" && docker ps --format 'table {{.Names}}\t{{.Status}}'
set -a && source /root/.env && set +a && /root/venv/bin/python /tmp/check_positions.py
cat > /tmp/check_positions.py << 'PYEOF'
import sys, os
sys.path.insert(0, '/root')
from auto_trader import BinanceAPI

b = BinanceAPI(os.environ['BINANCE_KEY'], os.environ['BINANCE_SECRET'])
pos = b.position_risk()
print('═══ AÇIK POZİSYONLAR ═══')
open_count = 0
for p in pos:
    amt = float(p['positionAmt'])
    if amt != 0:
        open_count += 1
        print(p['symbol'], '| amt=', amt, '| entry=', p['entryPrice'])
if open_count == 0:
    print('Hiç açık pozisyon yok')
PYEOF

echo "✅ Oluşturuldu" && wc -l /tmp/check_positions.py
set -a && source /root/.env && set +a && /root/venv/bin/python /tmp/check_positions.py
sleep 60 && journalctl -u auto_trader.service --since '3 minutes ago' | grep -E '🧠 Dinamik|leverage=|📊 ATR=|executor=|doldu|Risk-bazlı|ERROR' | tail -20
journalctl -u auto_trader.service --since '10 minutes ago' --no-pager | tail -30
systemctl status auto_trader.service --no-pager | head -15 && echo "═══" && date && echo "═══" && ps aux | grep -E "auto_trader.py|orphan_killer.py|archiver.py" | grep -v grep
sleep 360 && journalctl -u auto_trader.service --since '7 minutes ago' --no-pager | tail -40
set -a && source /root/.env && set +a && /root/venv/bin/python - << 'PYEOF'
import sys, os
sys.path.insert(0, "/root")
from auto_trader import BinanceAPI

b = BinanceAPI(os.environ["BINANCE_KEY"], os.environ["BINANCE_SECRET"])
print("═══ POZİSYONLAR KAPATILIYOR ═══")
closed = 0
for p in b.position_risk():
    amt = float(p["positionAmt"])
    if amt != 0:
        qty = abs(amt)
        side = "SELL" if amt > 0 else "BUY"
        try:
            r = b._post("/fapi/v1/order", {
                "symbol": p["symbol"], "side": side, "type": "MARKET",
                "quantity": str(qty), "reduceOnly": "true"
            })
            if r.get("status") == "FILLED" or r.get("orderId"):
                print(f"✅ {p['symbol']} kapatıldı (qty={qty}, {side})")
                closed += 1
            else:
                print(f"⚠️ {p['symbol']} cevap: {r}")
        except Exception as e:
            print(f"❌ {p['symbol']} hata: {e}")
print(f"\n═══ TOPLAM: {closed} pozisyon kapatıldı ═══")
PYEOF

set -a && source /root/.env && set +a && /root/venv/bin/python /tmp/check_positions.py
set -a && source /root/.env && set +a && /root/venv/bin/python -c "
import sys, os
sys.path.insert(0, '/root')
from auto_trader import BinanceAPI
b = BinanceAPI(os.environ['BINANCE_KEY'], os.environ['BINANCE_SECRET'])
print('Toplam bakiye:', round(b.get_balance('USDT'), 2), 'USDT')
print('Kullanılabilir:', round(b.get_available_balance('USDT'), 2), 'USDT')
"
systemctl restart auto_trader.service && sleep 3 && systemctl is-active auto_trader.service
sleep 90 && journalctl -u auto_trader.service --since '2 minutes ago' --no-pager | tail -30
set -a && source /root/.env && set +a && /root/venv/bin/python /tmp/check_positions.py
