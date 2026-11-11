import sys
sys.path.insert(0, '/root')
import json
from pathlib import Path
from auto_trader import RISK_CONFIG
from risk_engine import RiskEngine

print("=" * 60)
print("TEST 1: C1 — 25 USDT Hard Cap Doğrulaması")
print("=" * 60)
print(f"RISK_CONFIG['max_notional'] = {RISK_CONFIG['max_notional']}")
assert RISK_CONFIG['max_notional'] == 25.0, "HATA: max_notional 25 değil!"

risk = RiskEngine(RISK_CONFIG)
# Çok yüksek bakiye, çok düşük stop -> normalde 10000+ USDT notional ister
notional, status = risk.approve_trade(balance=100000.0, stop_distance_pct=0.01)
print(f"Test girdisi: balance=100000, stop=1%")
print(f"Çıktı: notional={notional} USDT, status={status}")

if notional > 25.0:
    print(f"❌ FAIL: Notional 25 USDT'yi aşıyor! ({notional})")
    sys.exit(1)
else:
    print(f"✅ PASS: Notional 25 USDT ile sınırlandı ({notional})")

print()
print("=" * 60)
print("TEST 2: C5 — is_halted Kalıcılığı")
print("=" * 60)

state_file = Path("/root/.risk_engine_state.json")
orig_backup = state_file.read_text()

# 1) Halt et ve kaydet
risk.is_halted = True
risk.halt_reason = "TEST_HALT_REASON"
risk._save_state()
saved = json.loads(state_file.read_text())
print(f"Kaydedilen: is_halted={saved.get('is_halted')}, halt_reason={saved.get('halt_reason')}")

# 2) Yeni obje oluştur (disk'ten okur)
risk2 = RiskEngine(RISK_CONFIG)
print(f"Yeni obje: is_halted={risk2.is_halted}, halt_reason={risk2.halt_reason}")

if risk2.is_halted is True and risk2.halt_reason == "TEST_HALT_REASON":
    print("✅ PASS: is_halted disk'ten doğru okundu")
else:
    print("❌ FAIL: is_halted kalıcı değil!")
    state_file.write_text(orig_backup)
    sys.exit(1)

# 3) Temizlik: orijinal state'i geri yükle
state_file.write_text(orig_backup)
print()
print("✅ Orijinal state geri yüklendi")
print()
print("=" * 60)
print("TÜM TESTLER BAŞARILI ✅")
print("=" * 60)
