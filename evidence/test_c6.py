import sys, time
sys.path.insert(0, '/root')

class MockHB:
    def __init__(self, active_count):
        self.active_count = active_count
    def active_executors(self):
        if self.active_count == "error":
            raise RuntimeError("API timeout")
        return [{"trading_pair": f"SYM{i}-USDT", "status": "RUNNING"} for i in range(self.active_count)]

def test_run_once_c6(label, active_count, max_pos, num_candidates):
    print(f"\n--- Test: {label} ---")
    hb = MockHB(active_count)
    opened = 0
    for i in range(num_candidates):
        try:
            _cur_active = {e["trading_pair"] for e in hb.active_executors() if e.get("status") == "RUNNING"}
        except Exception as e:
            print(f"  🚨 C6: fail-closed - {e}")
            break
        if len(_cur_active) >= max_pos:
            print(f"  🚫 C6: limit doldu ({len(_cur_active)}/{max_pos}), break")
            break
        print(f"  ✅ Aday {i+1} açılıyor (aktif: {len(_cur_active)}/{max_pos})")
        opened += 1
        if not isinstance(hb.active_count, str):
            hb.active_count += 1
    print(f"  Sonuç: {opened}/{num_candidates} açıldı")

print("=" * 60)
print("C6 — POZISYON SAYIMI TESTI (4 SENARYO)")
print("=" * 60)

test_run_once_c6("0 aktif + limit 2 + 2 aday", 0, 2, 2)         # 2/2 açılmalı
test_run_once_c6("1 aktif + limit 2 + 2 aday", 1, 2, 2)         # 1/2 açılmalı
test_run_once_c6("2 aktif + limit 2 + 2 aday", 2, 2, 2)         # 0/2 açılmalı
test_run_once_c6("HB API fail + limit 2 + 2 aday", "error", 2, 2)  # 0/2 + fail-closed

print()
print("=" * 60)
print("=== TÜM C6 TESTLERİ TAMAMLANDI ===")
print("=" * 60)
