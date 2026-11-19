"""test_faz18_c2_balance_sentinel.py — C.2 kaynaka-inen sentinel sözleşmesi.

get_balance/get_available_balance: okunamayan bakiye → None (0.0 YASAK —
F25 kök nedeni: 418'de dönen 0.0 "ölçüm" sanılmıştı). Başarıda float.
F25 guard'ı (update_equity streak) DERİNLEMESİNE SAVUNMA olarak durur — bu
test onu kaldırmaz, None'un da aynı streak'i beslediğini kilitler (M-2).
"""
import risk_engine as RE
from auto_trader import BinanceAPI


def _api(resp):
    api = BinanceAPI.__new__(BinanceAPI)      # __init__ atlanır (config yok)
    api._get = lambda *a, **k: resp
    return api


class TestSentinelNone:
    def test_error_dict_returns_none(self):
        api = _api({"code": -1003, "msg": "IP banned"})
        assert api.get_balance() is None
        assert api.get_available_balance() is None

    def test_nonlist_returns_none(self):
        for resp in (None, "", {"foo": 1}, []):
            assert _api(resp).get_balance() is None

    def test_asset_missing_returns_none(self):
        api = _api([{"asset": "BTC", "balance": "1", "availableBalance": "1"}])
        assert api.get_balance("USDT") is None

    def test_success_float(self):
        api = _api([{"asset": "USDT", "balance": "3500.5",
                     "availableBalance": "100.25"}])
        assert api.get_balance() == 3500.5
        assert api.get_available_balance() == 100.25


class TestDefenseInDepth:
    def test_none_feeds_f25_streak(self, tmp_path):
        db = tmp_path / "c2_risk.db"
        RE._bootstrap_scratch_db(db)
        eng = RE.RiskEngine({"db_file": str(db), "db_create": True,
                             "max_drawdown_pct": 0.20})
        eng.update_equity(1000.0)
        assert eng.update_equity(None) is False      # None da geçersiz
        assert not eng.is_halted                     # tek okuma halt etmez
        for _ in range(3):
            eng.update_equity(None)
        assert eng.is_halted and "EQUITY_UNAVAILABLE" in eng.halt_reason
