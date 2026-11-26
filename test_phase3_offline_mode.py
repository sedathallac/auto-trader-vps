"""Faz 3 — HB_OFFLINE test/kalibrasyon bypass'ı (kapsam izolasyonu).

HB_OFFLINE aktifken reconcile_positions ve sync_risk_engine Hummingbot API'ye
HİÇ dokunmaz. Varsayılan KAPALI iken production davranışı değişmez (gerçek HB
karşılaştırması çalışır).
"""
import auto_trader


class RaisingHB:
    """Çağrılırsa test'i patlatır — offline'da HB'ye dokunulmadığını kanıtlar."""
    auth = ("u", "p")

    def active_executors(self):
        raise AssertionError("HB_OFFLINE iken hb.active_executors() ÇAĞRILMAMALI")


class RaisingBinance:
    def position_risk(self):
        raise AssertionError("HB_OFFLINE reconcile'da binance.position_risk ÇAĞRILMAMALI")


class OkBinance:
    def position_risk(self):
        return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]


class OkHB:
    auth = ("u", "p")

    def active_executors(self):
        return [{"trading_pair": "BTC-USDT", "status": "RUNNING",
                 "filled_amount_quote": 10}]


# ----------------------------------------------------------------- OFFLINE (ON)
def test_reconcile_offline_returns_true_without_touching_hb(monkeypatch):
    """GREEN: offline → reconcile True döndürür, ne HB ne binance'e dokunur."""
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    assert auto_trader.reconcile_positions(RaisingBinance(), RaisingHB()) is True


def test_sync_risk_engine_offline_skips_without_touching_hb(monkeypatch):
    """GREEN: offline → sync_risk_engine sessizce geçer (None), HB'ye gitmez."""
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", True)
    assert auto_trader.sync_risk_engine(object(), RaisingHB(),
                                        binance=RaisingBinance()) is None


# ----------------------------------------------------------- DEFAULT OFF (prod)
def test_reconcile_default_off_still_runs_real_hb_comparison(monkeypatch):
    """Kapsam izolasyonu: HB_OFFLINE kapalı → gerçek HB↔borsa karşılaştırması çalışır."""
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", False)
    # Borsada BTCUSDT + HB'de RUNNING BTC-USDT → senkron → True (drift yok)
    assert auto_trader.reconcile_positions(OkBinance(), OkHB()) is True


def test_default_off_reconcile_touches_hb(monkeypatch):
    """Kapsam izolasyonu: kapalıyken HB GERÇEKTEN çağrılır (bypass yok)."""
    monkeypatch.setattr(auto_trader, "HB_OFFLINE", False)
    called = {"hb": False}

    class SpyHB(OkHB):
        def active_executors(self):
            called["hb"] = True
            return super().active_executors()

    auto_trader.reconcile_positions(OkBinance(), SpyHB())
    assert called["hb"] is True
