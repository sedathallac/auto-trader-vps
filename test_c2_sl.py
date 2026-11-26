import sys, time
sys.path.insert(0, '/root')

class MockBinance:
    def __init__(self, scenario):
        self.scenario = scenario
        self.close_sent = False
    def place_algo_sl(self, symbol, trig, amount, side):
        if self.scenario == "sl_fails":
            return {"code": -1, "msg": "SL rejected"}
        return {"algoId": 12345}
    def open_algo_orders_for(self, symbol):
        if self.scenario == "sl_missing":
            return []
        if self.scenario == "sl_qty_wrong":
            return [{"algoId": 12345, "quantity": 0.01, "symbol": symbol}]
        return [{"algoId": 12345, "quantity": 1.0, "symbol": symbol}]
    def _post(self, path, params, **kwargs):
        self.close_sent = True
        return {"orderId": 999}

class MockRisk:
    def __init__(self):
        self.is_halted = False
        self.halt_reason = None
    def _save_state(self):
        pass

def run_c2_test(scenario_name, mock_binance):
    print(f"\n--- Test: {scenario_name} ---")
    amount = 1.0
    symbol = "BTCUSDT"
    side = 1
    risk = MockRisk()
    
    sr = mock_binance.place_algo_sl(symbol, 100, amount, side)
    algo_id = sr.get("algoId") if isinstance(sr, dict) else None
    
    sl_verified = False
    if algo_id:
        print(f"  SL emri gonderildi (algoId={algo_id})")
        for attempt in range(3):
            time.sleep(0.05)
            try:
                open_orders = mock_binance.open_algo_orders_for(symbol)
                for o in open_orders:
                    if str(o.get("algoId")) == str(algo_id):
                        o_qty = float(o.get("quantity") or o.get("origQty") or o.get("qty") or 0)
                        if o_qty >= float(amount) * 0.95:
                            sl_verified = True
                            break
                if sl_verified:
                    break
                print(f"  Deneme {attempt+1}/3: SL bulunamadi/eksik")
            except Exception as e:
                print(f"  API hatasi: {e}")
    
    if not sl_verified:
        print(f"  🚨 SL DOGRULANAMADI! Pozisyon kapatiliyor...")
        mock_binance._post("/fapi/v1/order", {})
        risk.is_halted = True
        risk.halt_reason = f"SL dogrulanamadi: {symbol}"
        print(f"  ✅ Kapatma emri gonderildi: {mock_binance.close_sent}")
        print(f"  ✅ is_halted: {risk.is_halted}")
        return False
    else:
        print(f"  ✅ SL dogrulandi")
        return True

print("=" * 60)
print("C2 — SL DOGRULAMA TESTI (4 SENARYO)")
print("=" * 60)
run_c2_test("Basarili SL (miktar dogru)", MockBinance("ok"))
run_c2_test("SL listede YOK", MockBinance("sl_missing"))
run_c2_test("SL yanlis miktar", MockBinance("sl_qty_wrong"))
run_c2_test("SL API reddetti", MockBinance("sl_fails"))

print()
print("=" * 60)
print("=== TUM SENARYOLAR TAMAMLANDI ===")
print("=" * 60)
