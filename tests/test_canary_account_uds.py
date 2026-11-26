"""AŞAMA B testleri — canary_account_uds (AĞ ÇAĞRISI YOK; FakeWs).

Kapsam (görev: UDS istemcisi):
  - Olay ayrıştırma (ACCOUNT_UPDATE / ORDER_TRADE_UPDATE /
    ACCOUNT_CONFIG_UPDATE / MARGIN_CALL / listenKeyExpired).
  - E/T/YEREL zaman ayrımı.
  - Delta/patch uygulama: olmayan anahtar SIFIRLANMAZ.
  - Dedup/idempotensi (fingerprint).
  - DESYNC: listenKeyExpired, parse hatası, erken kapanma.
  - Bilinen-ama-ayrıştırılmayan ve bilinmeyen olay türleri (sessizce
    yutulmaz; coverage_limited / unknown_event_types).
  - listenKey POST/PUT; DELETE YOK (güvenlik testi).
  - listenKey redaksiyonu (ws_url ve tüm çıktılar).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_adapter import TransportResponse  # noqa: E402
from canary_account_uds import (  # noqa: E402
    UdsClient, UdsDeltaState, UdsEvent, UdsBalanceDelta, UdsSessionResult,
    parse_uds_message, UdsParseError, KNOWN_EVENT_TYPES, PARSED_EVENT_TYPES,
    ST_CONNECTED, ST_NOT_CONNECTED, ST_EXPIRED, ST_PARSE_ERROR,
    ST_CLOSED_EARLY,
)

FAKE_KEY = "TESTLISTENKEYdoNOTprint0000000000000000"

E_TIME = 1_700_000_100_000       # borsa E (olay saati)
T_TIME = 1_700_000_100_050       # borsa T (işlem saati)
T_LOCAL = 1_700_000_100_200      # istemci yerel saati


def _account_update(asset="USDT", cw="3491.46", bal="3491.46",
                    symbol="BTCUSDT", pa="0.002"):
    return {
        "e": "ACCOUNT_UPDATE", "E": E_TIME, "T": T_TIME,
        "a": {
            "B": [{"a": asset, "B": bal, "cw": cw, "bc": "0.0", "cc": "-12.34"}],
            "P": [{"s": symbol, "pa": pa, "ep": "96000.0", "ps": "BOTH",
                   "mt": "cross", "iw": "0", "up": "-1.20"}],
        },
    }


EV_ORDER = {
    "e": "ORDER_TRADE_UPDATE", "E": E_TIME, "T": T_TIME,
    "o": {"s": "BTCUSDT", "c": "x-abc", "S": "SELL", "X": "NEW",
          "i": 1002, "l": "0", "z": "0", "L": "0", "n": "0",
          "N": "USDT", "T": T_TIME, "t": T_TIME, "p": "0", "q": "0",
          "ap": "0", "sp": "61234.5", "ot": "STOP_MARKET",
          "wt": "MARK_PRICE", "ps": "BOTH", "cp": True, "R": False},
}
EV_CONFIG = {"e": "ACCOUNT_CONFIG_UPDATE", "E": E_TIME,
             "ac": {"s": "BTCUSDT", "l": 20}}
EV_MARGIN_CALL = {"e": "MARGIN_CALL", "E": E_TIME, "T": T_TIME,
                  "p": [{"s": "BTCUSDT", "pa": "0.002", "mt": "cross",
                         "iw": "0", "ps": "BOTH"}]}
EV_EXPIRED = {"e": "listenKeyExpired", "E": E_TIME}
EV_GRID = {"e": "GRID_UPDATE", "E": E_TIME, "T": T_TIME, "gu": {}}


class WebSocketTimeoutException(Exception):
    """websocket-client timeout taklidi (isim eşleşmesi yeterli)."""


class ConnectionClosed(Exception):
    pass


class FakeWs:
    """Betikli recv: mesajlar tükenince timeout; 'close_at' → erken kapanma."""

    def __init__(self, messages, close_at=None):
        self.messages = list(messages)
        self.close_at = close_at      # kalan mesaj sayısı bu sayıya inince kapat
        self.closed = False
        self.timeout_set = None

    def settimeout(self, t):
        self.timeout_set = t

    def recv(self):
        if self.close_at is not None and len(self.messages) <= self.close_at:
            raise ConnectionClosed("closed by peer")
        if not self.messages:
            raise WebSocketTimeoutException("timed out")
        return json.dumps(self.messages.pop(0))

    def close(self):
        self.closed = True


class FakeLkTransport:
    """listenKey POST/PUT sahte transport'u (signed_get YOK — kullanılmaz)."""

    def __init__(self, fail_post=False):
        self.fail_post = fail_post
        self.put_called = False

    def listen_key_post(self):
        if self.fail_post:
            return TransportResponse(
                status_code=401, headers={}, body={"code": -2015,
                                                    "msg": "bad key"},
                error=None, request_started_at_ms=1,
                response_received_at_ms=2)
        return TransportResponse(
            status_code=200, headers={}, body={"listenKey": FAKE_KEY},
            error=None, request_started_at_ms=1, response_received_at_ms=2)

    def listen_key_put(self):
        self.put_called = True
        return TransportResponse(
            status_code=200, headers={}, body={}, error=None,
            request_started_at_ms=3, response_received_at_ms=4)


def _clock(start=1_000_000, step=250):
    state = {"now": start}

    def now():
        state["now"] += step
        return state["now"]
    return now


def _run(messages, close_at=None, duration_s=2.0, fail_post=False,
         ws_error=False):
    lk = FakeLkTransport(fail_post=fail_post)
    ws_holder = {}

    def factory(url, timeout):
        ws_holder["url"] = url
        if ws_error:
            raise OSError("connection refused")
        ws_holder["ws"] = FakeWs(messages, close_at=close_at)
        return ws_holder["ws"]

    client = UdsClient(lk, ws_factory=factory, duration_s=duration_s,
                       clock_ms=_clock())
    return client.run_probe(), lk, ws_holder


# ---------------------------------------------------------------------------
# Olay ayrıştırma
# ---------------------------------------------------------------------------

class ParseTests(unittest.TestCase):

    def test_account_update_parsed(self):
        ev = parse_uds_message(_account_update(), T_LOCAL)
        self.assertEqual(ev.event_type, "ACCOUNT_UPDATE")
        self.assertEqual(len(ev.balances), 1)
        self.assertEqual(len(ev.positions), 1)
        b = ev.balances[0]
        self.assertEqual(b.asset, "USDT")
        self.assertAlmostEqual(b.cross_wallet_balance, 3491.46)
        self.assertAlmostEqual(b.balance_change, 0.0)
        self.assertAlmostEqual(b.cross_un_pnl, -12.34)   # negatif GEÇERLİ
        p = ev.positions[0]
        self.assertEqual(p.symbol, "BTCUSDT")
        self.assertAlmostEqual(p.position_amount, 0.002)
        self.assertEqual(p.margin_type, "cross")

    def test_e_t_local_times_separate(self):
        ev = parse_uds_message(_account_update(), T_LOCAL)
        self.assertEqual(ev.event_time_ms, E_TIME)
        self.assertEqual(ev.transaction_time_ms, T_TIME)
        self.assertEqual(ev.local_receive_time_ms, T_LOCAL)
        # üçü birbirinin yerine kullanılmaz — değerleri farklı
        self.assertNotEqual(ev.event_time_ms, ev.local_receive_time_ms)

    def test_order_trade_update(self):
        ev = parse_uds_message(EV_ORDER, T_LOCAL)
        self.assertEqual(ev.order["s"], "BTCUSDT")
        self.assertEqual(ev.order["ot"], "STOP_MARKET")
        self.assertTrue(ev.order["cp"])  # closePosition stop

    def test_config_update_and_margin_call(self):
        cfg = parse_uds_message(EV_CONFIG, T_LOCAL)
        self.assertEqual(cfg.config_update["ac"]["s"], "BTCUSDT")
        mc = parse_uds_message(EV_MARGIN_CALL, T_LOCAL)
        self.assertEqual(len(mc.margin_call_positions), 1)

    def test_missing_body_recorded_not_invented(self):
        ev = parse_uds_message(
            {"e": "ACCOUNT_UPDATE", "E": E_TIME, "T": T_TIME, "a": {}},
            T_LOCAL)
        self.assertEqual(ev.balances, ())
        self.assertEqual(ev.positions, ())
        self.assertIsNotNone(ev.parse_note)   # boş gövde not edilir

    def test_empty_string_numeric_is_none(self):
        ev = parse_uds_message({
            "e": "ACCOUNT_UPDATE", "E": E_TIME, "T": T_TIME,
            "a": {"B": [{"a": "USDT", "B": "", "cw": "", "bc": "", "cc": ""}],
                  "P": []}}, T_LOCAL)
        self.assertIsNone(ev.balances[0].balance)
        self.assertIsNone(ev.balances[0].cross_wallet_balance)

    def test_broken_payload_raises(self):
        with self.assertRaises(UdsParseError):
            parse_uds_message([1, 2, 3], T_LOCAL)
        with self.assertRaises(UdsParseError):
            parse_uds_message({"E": 1, "T": 2}, T_LOCAL)     # e yok
        with self.assertRaises(UdsParseError):
            parse_uds_message({"e": "ORDER_TRADE_UPDATE", "E": 1}, T_LOCAL)


# ---------------------------------------------------------------------------
# Delta durum
# ---------------------------------------------------------------------------

class DeltaStateTests(unittest.TestCase):

    def test_missing_key_not_zeroed(self):
        st = UdsDeltaState()
        st.apply(parse_uds_message(_account_update(), T_LOCAL))
        self.assertIn("USDT", st.balances)
        self.assertNotIn("BNB", st.balances)   # gelmeyen varlık SIFIRLANMAZ
        self.assertEqual(st.balances["USDT"]["cross_wallet_balance"], 3491.46)

    def test_second_event_adds_without_reset(self):
        st = UdsDeltaState()
        st.apply(parse_uds_message(_account_update(), T_LOCAL))
        st.apply(parse_uds_message(
            _account_update(asset="BNB", cw="0.5", bal="0.5",
                            symbol="ETHUSDT", pa="0"), T_LOCAL + 1))
        self.assertAlmostEqual(st.balances["BNB"]["cross_wallet_balance"], 0.5)
        self.assertAlmostEqual(st.balances["USDT"]["cross_wallet_balance"],
                               3491.46)  # önceki korunur
        self.assertEqual(len(st.positions), 2)  # BTC + ETH

    def test_partial_field_update_kept(self):
        st = UdsDeltaState()
        st.apply(parse_uds_message(_account_update(), T_LOCAL))
        # yalnız pozisyon miktarı gelen olay
        st.apply(parse_uds_message({
            "e": "ACCOUNT_UPDATE", "E": E_TIME + 1, "T": T_TIME + 1,
            "a": {"B": [], "P": [{"s": "BTCUSDT", "pa": "0.004",
                                  "ps": "BOTH"}]}}, T_LOCAL + 2))
        self.assertAlmostEqual(
            st.positions[("BTCUSDT", "BOTH")]["position_amount"], 0.004)
        self.assertAlmostEqual(
            st.positions[("BTCUSDT", "BOTH")]["entry_price"], 96000.0)

    def test_dedup_by_fingerprint(self):
        st = UdsDeltaState()
        msg = _account_update()
        self.assertTrue(st.apply(parse_uds_message(msg, T_LOCAL)))
        self.assertFalse(st.apply(parse_uds_message(msg, T_LOCAL + 5)))
        self.assertEqual(st.duplicates, 1)
        self.assertEqual(st.applied, 1)

    def test_same_content_different_time_is_duplicate(self):
        st = UdsDeltaState()
        msg = _account_update()
        st.apply(parse_uds_message(msg, T_LOCAL))
        st.apply(parse_uds_message(msg, T_LOCAL + 999))
        self.assertEqual(st.duplicates, 1)   # E/T aynı → aynı borsa olayı


# ---------------------------------------------------------------------------
# Oturum (probe)
# ---------------------------------------------------------------------------

class SessionTests(unittest.TestCase):

    def test_listen_key_post_and_ws_url(self):
        res, lk, ws_holder = _run([_account_update()])
        self.assertTrue(res.listen_key_created)
        self.assertTrue(lk.put_called)         # keepalive PUT yapıldı
        self.assertTrue(res.connected)
        self.assertEqual(res.stream_state, ST_CONNECTED)
        # ws_url REDAKTE — gerçek key asla yazılmaz
        self.assertEqual(res.ws_url, "wss://fstream.binance.com/ws/<redacted>")
        self.assertNotIn(FAKE_KEY, res.ws_url)
        # factory gerçek url ile çağrıldı (redaksiyon yalnız raporda)
        self.assertEqual(ws_holder["url"],
                         f"wss://fstream.binance.com/ws/{FAKE_KEY}")

    def test_keepalive_ok_flag(self):
        res, _, _ = _run([])
        self.assertTrue(res.keepalive_put_ok)

    def test_events_collected_and_ws_closed(self):
        res, _, ws_holder = _run([_account_update(), EV_ORDER, EV_CONFIG])
        self.assertEqual(len(res.events), 3)
        types = [e.event_type for e in res.events]
        self.assertEqual(types, ["ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE",
                                 "ACCOUNT_CONFIG_UPDATE"])
        self.assertTrue(ws_holder["ws"].closed)   # yerel kapatma yapılır

    def test_duplicate_counted_once(self):
        msg = _account_update()
        res, _, _ = _run([msg, dict(msg)])
        self.assertEqual(len(res.events), 2)      # ham olaylar ikisi de kayıtlı
        self.assertEqual(res.duplicates, 1)

    def test_no_events_connected_state(self):
        res, _, _ = _run([])
        self.assertTrue(res.connected)
        self.assertEqual(res.stream_state, ST_CONNECTED)
        self.assertEqual(len(res.events), 0)

    def test_listen_key_expired_marks_desync(self):
        res, _, _ = _run([EV_EXPIRED])
        self.assertEqual(res.stream_state, ST_EXPIRED)
        self.assertIn("yeni geçerli key", res.error_detail)

    def test_json_parse_error_marks_desync(self):
        class BadWs(FakeWs):
            def recv(self):
                if self.messages:
                    return "bu geçerli json değil {{{"   # ilk recv'ler bozuk
                raise WebSocketTimeoutException("t")
        lk = FakeLkTransport()
        client = UdsClient(lk, ws_factory=lambda u, t: BadWs([1]),
                           duration_s=1.0, clock_ms=_clock())
        res = client.run_probe()
        self.assertEqual(res.stream_state, ST_PARSE_ERROR)
        # süre boyunca tekrar tekrar bozuk mesaj gelebilir (döngü sürer)
        self.assertGreaterEqual(res.parse_errors, 1)
        self.assertNotIn("{", res.error_detail)  # ham metin loglanmaz

    def test_early_close_marks_desync(self):
        res, _, _ = _run([_account_update()], close_at=0)
        self.assertEqual(res.stream_state, ST_CLOSED_EARLY)

    def test_unknown_event_type_retained(self):
        res, _, _ = _run([{"e": "BRAND_NEW_EVENT", "E": E_TIME}])
        # bilinmeyen tür yutulmaz; desync de değildir
        self.assertEqual(res.stream_state, ST_CONNECTED)
        self.assertEqual(res.unknown_event_types, ("BRAND_NEW_EVENT",))
        self.assertEqual(len(res.events), 1)

    def test_known_but_unparsed_type_flags_coverage(self):
        res, _, _ = _run([EV_GRID])
        self.assertEqual(res.stream_state, ST_CONNECTED)
        self.assertTrue(res.coverage_limited)   # GRID_UPDATE ayrıştırılmaz
        self.assertEqual(res.unknown_event_types, ())  # dokümanda VAR

    def test_listen_key_post_failure(self):
        res, _, ws_holder = _run([], fail_post=True)
        self.assertFalse(res.listen_key_created)
        self.assertFalse(res.connected)
        self.assertEqual(res.stream_state, ST_NOT_CONNECTED)
        self.assertIn("-2015", res.error_detail or "")
        self.assertNotIn(FAKE_KEY, json.dumps(res.to_safe_dict()))

    def test_ws_connect_failure(self):
        res, _, _ = _run([], ws_error=True)
        self.assertFalse(res.connected)
        self.assertEqual(res.stream_state, ST_NOT_CONNECTED)
        self.assertIn("WS bağlantı hatası", res.error_detail)


# ---------------------------------------------------------------------------
# Güvenlik
# ---------------------------------------------------------------------------

class Güvenlik(unittest.TestCase):

    def test_no_delete_method_anywhere(self):
        import canary_account_uds as uds
        import canary_account_adapter as adapter
        for cls in (adapter.BinanceHttpTransport, FakeLkTransport):
            self.assertFalse(hasattr(cls, "listen_key_delete"))
            self.assertFalse(hasattr(cls, "delete_listen_key"))
        # modül kaynaklarında stream silme çağrısı YOK
        for mod in (uds, adapter):
            src = open(mod.__file__, encoding="utf-8").read()
            self.assertNotIn('"DELETE"', src)
            self.assertNotIn("'DELETE'", src)

    def test_client_has_no_order_methods(self):
        for name in ("new_order", "cancel_order", "send_order"):
            self.assertFalse(hasattr(UdsClient, name))

    def test_session_result_output_never_contains_key(self):
        res, _, _ = _run([_account_update(), EV_ORDER])
        blob = json.dumps(res.to_safe_dict())
        self.assertNotIn(FAKE_KEY, blob)
        # olay kayıtları da güvenli görünümde
        self.assertIn("ACCOUNT_UPDATE", blob)

    def test_all_doc_events_in_known_list(self):
        for t in ("MARGIN_CALL", "ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE",
                  "ACCOUNT_CONFIG_UPDATE", "TRADE_LITE",
                  "CONDITIONAL_ORDER_TRIGGER_REJECT", "STRATEGY_UPDATE",
                  "GRID_UPDATE", "ALGO_UPDATE", "listenKeyExpired"):
            self.assertIn(t, KNOWN_EVENT_TYPES)
        self.assertIn("ACCOUNT_UPDATE", PARSED_EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
