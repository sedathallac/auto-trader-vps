"""K-5 Görev B RED/GREEN — orphan_killer deterministik dedupe BACKSTOP.

K-3'ün keep-max-algoId dedupe'u (auto_trader._dedupe_algo_sl) artık orphan_killer'da,
ensure_sl_coverage'dan BAĞIMSIZ bir backstop olarak çalışıyor.

RED:   ensure_sl_coverage dedupe yapmazsa (burada no-op'a sabitlenmiş) ve backstop
       YOKSA, açık pozisyondaki çift SL temizlenmeden kalır (count == 2).
GREEN: gerçek scan_and_kill, ensure_sl_coverage no-op olsa bile backstop sayesinde
       fazlalığı iptal eder → tam olarak tek SL kalır ve en büyük algoId hayatta kalır.
"""
import time

import pytest

import auto_trader
import orphan_killer

SYMBOL = "BTCUSDT"


class FakeBinance:
    """position_risk / open_algo_orders_for / cancel_algo_order sağlayan sahte borsa."""

    def __init__(self, positions, algo_orders):
        self._positions = positions
        self._orders = {s: list(v) for s, v in algo_orders.items()}

    def position_risk(self):
        return list(self._positions)

    def open_algo_orders_for(self, symbol):
        return list(self._orders.get(symbol, []))

    def cancel_algo_order(self, algo_id):
        for sym in list(self._orders):
            self._orders[sym] = [o for o in self._orders[sym] if o["algoId"] != algo_id]
        return {"code": "200"}

    def count(self, symbol):
        return len(self._orders.get(symbol, []))

    def algo_ids(self, symbol):
        return [o["algoId"] for o in self._orders.get(symbol, [])]


def _double_sl_fixture():
    """Bir açık BTCUSDT pozisyonu + aynı pozisyon için İKİ STOP algo SL (çift SL)."""
    positions = [{"symbol": SYMBOL, "positionAmt": "0.01", "entryPrice": "50000"}]
    orders = {
        SYMBOL: [
            {"algoId": 111, "orderType": "STOP_MARKET", "triggerPrice": "49000"},
            {"algoId": 222, "orderType": "STOP_MARKET", "triggerPrice": "49000"},
        ]
    }
    return FakeBinance(positions, orders)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Ev dizinine yazmayı önle ve ensure_sl_coverage'ı no-op yap (backstop'u izole et)."""
    monkeypatch.setattr(orphan_killer, "TRACKED", tmp_path / ".tracked_symbols.json")
    hb = tmp_path / ".heartbeat"
    hb.write_text(__import__("datetime").datetime.now().isoformat())
    monkeypatch.setattr(orphan_killer, "HEARTBEAT_FILE", hb)
    # ensure_sl_coverage'ı no-op'a sabitle: dedupe'u SADECE orphan_killer backstop'u
    # yapıyorsa testler geçer -> backstop'un BAĞIMSIZ olduğu kanıtlanır.
    monkeypatch.setattr(auto_trader, "ensure_sl_coverage", lambda b: None)


def test_red_without_backstop_duplicate_persists():
    """RED: yalnızca (no-op) ensure_sl_coverage çalışırsa çift SL temizlenmez."""
    b = _double_sl_fixture()
    auto_trader.ensure_sl_coverage(b)  # eski davranış: coverage dedupe yapmıyor
    assert b.count(SYMBOL) == 2  # backstop olmadan çift SL kalır (kapatılan boşluk)


def test_green_backstop_dedupes_open_position():
    """GREEN: scan_and_kill backstop'u çifti tek'e indirir (coverage no-op olsa bile)."""
    b = _double_sl_fixture()
    orphan_killer.scan_and_kill(binance=b)
    assert b.count(SYMBOL) == 1


def test_green_backstop_keeps_max_algo_id():
    """GREEN: hayatta kalan SL, deterministik olarak EN BÜYÜK algoId'dir."""
    b = _double_sl_fixture()
    orphan_killer.scan_and_kill(binance=b)
    assert b.algo_ids(SYMBOL) == [222]  # max(111, 222)


def test_green_backstop_idempotent_single_sl_untouched():
    """GREEN: tek SL varsa backstop hiçbir şeyi iptal etmez (korumasız bırakmaz)."""
    positions = [{"symbol": SYMBOL, "positionAmt": "0.01", "entryPrice": "50000"}]
    orders = {SYMBOL: [{"algoId": 111, "orderType": "STOP_MARKET", "triggerPrice": "49000"}]}
    b = FakeBinance(positions, orders)
    orphan_killer.scan_and_kill(binance=b)
    assert b.count(SYMBOL) == 1
    assert b.algo_ids(SYMBOL) == [111]
