"""AŞAMA B testleri — canary_account_adapter (AĞ ÇAĞRISI YOK).

Kapsam (görev: REST adaptör):
  - FakeTransport ile 7 endpoint'lik baseline (gerçek biçimli fixture'lar
    AŞAMA A test modülünden yeniden kullanılır — tekrar YOK).
  - Endpoint başarısızlığı → complete=False (verisiz TAM envanter YOK).
  - Parse hatası → PARSE_ERROR sebebi.
  - Multi-Assets: tek-varlık / MULTI / CONFLICT / UNKNOWN.
  - Filtresiz pozisyon envanteri (flat dahil) korunur.
  - Standart ve algo emir envanterleri AYRI.
  - REST atomik değil: her çağrı kendi zamanlarını taşır.
  - Rate-limit header yakalama + serial-delta ölçümü.
  - Sıfır değer semantiği (0 geçerli).
  - GÜVENLİK: emir yüzeyi YOK; beyaz liste dışı endpoint RED;
    kimlik/listenKey/imza redaksiyonu.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_adapter import (  # noqa: E402
    AccountAdapter, BinanceHttpTransport, TransportResponse,
    SAFE_GET_ENDPOINTS, redact, MA_SINGLE, MA_MULTI, MA_CONFLICT,
)
from tests.test_canary_account_model import (  # noqa: E402
    FIX_CONFIG_OK, FIX_CONFIG_MULTI, FIX_BALANCE_V3, FIX_ACCOUNT_V2,
    FIX_ACCOUNT_V3, FIX_POSITION_RISK, FIX_OPEN_ORDERS, FIX_OPEN_ALGOS,
    T_REQ,
)

# v3 account positions[] (doc-verified azaltılmış alan kümesi; BTC cross +
# DOGE isolated — positionRisk ile aynı semboller)
FIX_ACCOUNT_V3_FULL = dict(
    FIX_ACCOUNT_V3,
    positions=[
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.002",
         "leverage": "20", "entryPrice": "96000.0", "updateTime": T_REQ,
         "isolated": False, "unrealizedProfit": "-1.20"},
        {"symbol": "DOGEUSDT", "positionSide": "BOTH", "positionAmt": "-500",
         "leverage": "8", "entryPrice": "0.32", "updateTime": T_REQ,
         "isolated": True, "unrealizedProfit": "-5.0"},
    ])

FAKE_LISTEN_KEY = "TESTLISTENKEYdoNOTprint0000000000000000"


def _resp(status, body, headers=None):
    return TransportResponse(status_code=status, headers=headers or {},
                             body=body, error=None,
                             request_started_at_ms=1, response_received_at_ms=2)


class FakeTransport:
    """Ağ YOK. Endpoint → yanıt eşlemi; ölçülen zamanlar ve ağırlık
    header'ları gerçek transport gibi artar."""

    def __init__(self, responses=None, fail=None, start_weight=0):
        self.responses = responses or {}
        self.fail = fail or set()          # endpoint seti → HTTP 401/-2015
        self.calls = []
        self._t = T_REQ
        self._w = start_weight

    def signed_get(self, path, params=None):
        self.calls.append(path)
        self._t += 100
        t0, t1 = self._t, self._t + 50
        self._t = t1
        self._w += 5
        if path in self.fail:
            body = {"code": -2015, "msg": "Invalid API-key, IP, or permissions"}
            return TransportResponse(
                status_code=401, headers={"X-MBX-USED-WEIGHT-1M": str(self._w)},
                body=body, error=None,
                request_started_at_ms=t0, response_received_at_ms=t1)
        body = self.responses[path]
        return TransportResponse(
            status_code=200, headers={"X-MBX-USED-WEIGHT-1M": str(self._w)},
            body=body, error=None,
            request_started_at_ms=t0, response_received_at_ms=t1)

    def listen_key_post(self):
        return _resp(200, {"listenKey": FAKE_LISTEN_KEY})

    def listen_key_put(self):
        return _resp(200, {})


def _full_responses():
    return {
        "/fapi/v1/accountConfig": dict(FIX_CONFIG_OK),
        "/fapi/v2/account": dict(FIX_ACCOUNT_V2),
        "/fapi/v3/account": FIX_ACCOUNT_V3_FULL,
        "/fapi/v3/balance": [dict(r) for r in FIX_BALANCE_V3],
        "/fapi/v2/positionRisk": [dict(r) for r in FIX_POSITION_RISK],
        "/fapi/v1/openOrders": [dict(r) for r in FIX_OPEN_ORDERS],
        "/fapi/v1/openAlgoOrders": [dict(r) for r in FIX_OPEN_ALGOS],
    }


class BaselineTests(unittest.TestCase):

    def test_full_baseline_complete(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        self.assertTrue(b.complete)
        self.assertEqual(b.incomplete_reasons, ())
        self.assertEqual(b.multi_assets_status, MA_SINGLE)
        self.assertEqual(len(b.records), 7)
        self.assertIsNotNone(b.config)
        self.assertIsNotNone(b.totals_v2)
        self.assertIsNotNone(b.totals_v3)
        self.assertIsNotNone(b.balance_v3)
        self.assertIsNotNone(b.positions)
        self.assertIsNotNone(b.positions_from_account_v3)
        self.assertIsNotNone(b.open_orders)
        self.assertIsNotNone(b.open_algo_orders)

    def test_all_seven_endpoints_called_in_whitelist(self):
        t = FakeTransport(_full_responses())
        AccountAdapter(t).collect_baseline()
        self.assertEqual(t.calls, list(SAFE_GET_ENDPOINTS))
        for c in t.calls:
            self.assertIn(c, SAFE_GET_ENDPOINTS)

    def test_unfiltered_positions_flat_rows_preserved(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        # positionRisk filtresiz: BTC + DOGE + ETH(flat) = 3 satır
        self.assertEqual(len(b.positions.entries), 3)
        syms = {p.symbol for p in b.positions.entries}
        self.assertEqual(syms, {"BTCUSDT", "DOGEUSDT", "ETHUSDT"})
        # sembol filtresi YOK: flat ETHUSDT da envanterde
        self.assertEqual(len(b.positions.non_flat()), 2)

    def test_standard_and_algo_orders_separate(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        self.assertEqual(len(b.open_orders.entries), 2)
        self.assertEqual(len(b.open_algo_orders.entries), 1)
        # normal envanterde algo emri YOK; algo envanterinde normal YOK
        ids = {o.orderId for o in b.open_orders.entries}
        self.assertEqual(ids, {1001, 1002})
        self.assertEqual(b.open_algo_orders.entries[0].algoId, 9001)

    def test_rest_not_atomic_per_call_times(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        starts = [r.request_started_at_ms for r in b.records]
        # her çağrı kendi penceresini taşır; hepsi ayrı ve artan
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), len(starts))
        for r in b.records:
            self.assertGreaterEqual(r.response_received_at_ms,
                                    r.request_started_at_ms)

    def test_rate_limit_header_capture_and_serial_delta(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        weights = [int(r.used_weight_1m) for r in b.records]
        self.assertEqual(weights, [5, 10, 15, 20, 25, 30, 35])
        # serial-istek varsayımıyla delta = 5 (IP-genişliği ayrı konu)
        deltas = [r.weight_delta for r in b.records]
        self.assertEqual(deltas[0], None)   # ilk çağrıda önceki değer yok
        self.assertEqual(deltas[1:], [5] * 6)

    def test_record_counts(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        by_ep = {r.endpoint: r for r in b.records}
        self.assertEqual(by_ep["/fapi/v2/positionRisk"].record_count, 3)
        self.assertEqual(by_ep["/fapi/v1/openOrders"].record_count, 2)
        self.assertEqual(by_ep["/fapi/v1/openAlgoOrders"].record_count, 1)
        self.assertEqual(by_ep["/fapi/v3/balance"].record_count, 2)


class FailureHandlingTests(unittest.TestCase):

    def test_endpoint_failure_marks_incomplete(self):
        t = FakeTransport(_full_responses(), fail={"/fapi/v2/positionRisk": 1})
        b = AccountAdapter(t).collect_baseline()
        self.assertFalse(b.complete)
        self.assertTrue(any(r.startswith("ENDPOINT_FAILED:/fapi/v2/positionRisk")
                            for r in b.incomplete_reasons))
        # hata kodu dürüstçe kaydedilir (-2015 = IP whitelist olasılığı)
        rec = next(r for r in b.records
                   if r.endpoint == "/fapi/v2/positionRisk")
        self.assertEqual(rec.error_code, -2015)
        # diğer bileşenler gelir ama TAM envanter gibi sunulamaz
        self.assertIsNone(b.positions)
        self.assertIsNotNone(b.balance_v3)

    def test_parse_error_marks_incomplete(self):
        resp = _full_responses()
        resp["/fapi/v3/balance"] = [{"asset": "USDT"}]  # zorunlu alanlar yok
        b = AccountAdapter(FakeTransport(resp)).collect_baseline()
        self.assertFalse(b.complete)
        self.assertTrue(any(r.startswith("PARSE_ERROR:/fapi/v3/balance")
                            for r in b.incomplete_reasons))
        self.assertIsNone(b.balance_v3)

    def test_network_error_marks_incomplete(self):
        resp = _full_responses()
        t = FakeTransport(resp)
        def broken(path, params=None):
            t.calls.append(path)
            return TransportResponse(
                status_code=None, headers={}, body=None,
                error="ConnectionError: probe", request_started_at_ms=1,
                response_received_at_ms=None)
        t.signed_get = broken
        b = AccountAdapter(t).collect_baseline()
        self.assertFalse(b.complete)
        self.assertTrue(any("ConnectionError" in r
                            for r in b.incomplete_reasons))


class MultiAssetsTests(unittest.TestCase):

    def test_multi_assets_from_config(self):
        resp = _full_responses()
        resp["/fapi/v1/accountConfig"] = dict(FIX_CONFIG_MULTI)
        # v2 account bayrağı da True (çapraz kaynak anlaşması)
        resp["/fapi/v2/account"] = dict(FIX_ACCOUNT_V2, multiAssetsMargin=True)
        b = AccountAdapter(FakeTransport(resp)).collect_baseline()
        self.assertEqual(b.multi_assets_status, MA_MULTI)
        # destek kapsamı dışı → complete=False (fail-closed)
        self.assertFalse(b.complete)
        self.assertTrue(any("MULTI_ASSETS_STATUS:MULTI_ASSETS" in r
                            for r in b.incomplete_reasons))

    def test_multi_assets_conflict(self):
        resp = _full_responses()
        resp["/fapi/v1/accountConfig"] = dict(FIX_CONFIG_OK)
        v2 = dict(FIX_ACCOUNT_V2, multiAssetsMargin=True)
        resp["/fapi/v2/account"] = v2
        b = AccountAdapter(FakeTransport(resp)).collect_baseline()
        self.assertEqual(b.multi_assets_status, MA_CONFLICT)
        self.assertFalse(b.complete)

    def test_multi_assets_agree_single(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        self.assertEqual(b.multi_assets_status, MA_SINGLE)
        # config False + v2 account False → anlaşma, engel yok


class ZeroSemanticsTests(unittest.TestCase):

    def test_zero_available_balance_is_valid(self):
        resp = _full_responses()
        resp["/fapi/v3/balance"] = [
            {"accountAlias": "SgsR", "asset": "USDT", "balance": "0",
             "crossWalletBalance": "0", "crossUnPnl": "0",
             "availableBalance": "0", "maxWithdrawAmount": "0",
             "marginAvailable": True, "updateTime": T_REQ}]
        b = AccountAdapter(FakeTransport(resp)).collect_baseline()
        self.assertTrue(b.complete)
        usdt = b.balance_v3.usdt()
        self.assertEqual(usdt.availableBalance, 0.0)   # gerçek sıfır geçerli
        self.assertEqual(usdt.crossWalletBalance, 0.0)


class Güvenlik(unittest.TestCase):
    """Salt-okunur sözleşme: emir yüzeyi YOK, redaksiyon VAR."""

    def test_adapter_has_no_order_methods(self):
        for name in ("new_order", "new_algo_order", "cancel_order",
                     "cancel_algo_order", "place", "post_order"):
            self.assertFalse(hasattr(AccountAdapter, name),
                             f"AccountAdapter.{name} var — YASAK")

    def test_transport_whitelist_rejects_order_endpoint(self):
        t = BinanceHttpTransport("k", "s")
        with self.assertRaises(ValueError):
            t.signed_get("/fapi/v1/order")
        with self.assertRaises(ValueError):
            t.signed_get("/fapi/v1/leverage")

    def test_no_trading_client_import(self):
        import canary_account_adapter as m
        src = open(m.__file__, encoding="utf-8").read()
        # emir istemcisi IMPORT EDİLMEZ (docstring'de adı geçebilir)
        self.assertNotIn("import canary_exchange", src)
        self.assertNotIn("from canary_exchange", src)
        self.assertNotIn("/fapi/v1/order\"", src)
        # sys.modules kontrolü İZOLE alt süreçte: tam paket koşusunda başka
        # testlerin importu sys.modules'i kirletir (sıra bağımlılığı YOK)
        import subprocess
        code = ("import sys\n"
                "import canary_account_adapter\n"
                "print('FOUND' if 'canary_exchange' in sys.modules "
                "else 'CLEAN')\n")
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.abspath(m.__file__)),
            capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         r.stderr.decode()[-500:])
        self.assertEqual(r.stdout.decode().strip().splitlines()[-1],
                         "CLEAN")

    def test_redaction_dict(self):
        out = redact({"listenKey": "SECRET", "api_key": "SECRET",
                      "signature": "SECRET", "ok": {"value": 1}})
        self.assertEqual(out["listenKey"], "<redacted>")
        self.assertEqual(out["api_key"], "<redacted>")
        self.assertEqual(out["signature"], "<redacted>")
        self.assertEqual(out["ok"], {"value": 1})

    def test_redaction_ws_url_and_signature(self):
        url = "wss://fstream.binance.com/ws/" + FAKE_LISTEN_KEY
        self.assertEqual(redact(url),
                         "wss://fstream.binance.com/ws/<redacted>")
        signed = "https://fapi.binance.com/fapi/v2/account?timestamp=1&signature=abcdef"
        self.assertIn("signature=<redacted>", redact(signed))

    def test_safe_dict_contains_no_credentials(self):
        b = AccountAdapter(FakeTransport(_full_responses())).collect_baseline()
        blob = json.dumps(b.to_safe_dict())
        self.assertNotIn("SECRET", blob)
        self.assertNotIn(FAKE_LISTEN_KEY, blob)


if __name__ == "__main__":
    unittest.main()
