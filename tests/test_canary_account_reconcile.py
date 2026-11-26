"""AŞAMA B testleri — canary_account_reconcile (AĞ ÇAĞRISI YOK).

Kapsam (görev: REST↔UDS mutabakat):
  - Durum makinesi: DATA_INCOMPLETE / STREAM_DESYNCED /
    RECONCILIATION_PENDING / RECONCILIATION_MISMATCH / RECONCILED.
  - Verisiz RECONCILED YOK (olay yoksa PENDING kalır).
  - Alan bazlı MATCH / MISMATCH / NOT_COMPARABLE / INCONCLUSIVE.
  - Zaman çakışması → INCONCLUSIVE (eşik uydurma YOK).
  - REST atomik-olmama korunur (çağrı bazlı pencere).
  - Kapsam-farkındalık: ASSET/ACCOUNT_CROSS/POSITION karışmaz.
  - Mutabakat okunur-sonda: baseline değişmez.
  - Modülde kalibre edilmemiş eşik sabiti YOK (kaynak taraması).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_adapter import AccountAdapter  # noqa: E402
from canary_account_reconcile import (  # noqa: E402
    reconcile, S_DATA_INCOMPLETE, S_STREAM_DESYNCED, S_RECON_PENDING,
    S_RECON_MISMATCH, S_RECONCILED, C_MATCH, C_MISMATCH,
    C_NOT_COMPARABLE, C_INCONCLUSIVE,
)
from canary_account_uds import (  # noqa: E402
    UdsEvent, UdsBalanceDelta, UdsSessionResult, ST_CONNECTED,
)
from tests.test_canary_account_adapter import (  # noqa: E402
    FakeTransport, _full_responses,
)
from tests.test_canary_account_model import T_REQ  # noqa: E402

# REST pencereleri FakeTransport'ta T_REQ..T_REQ+~1050 aralığında
# (çağrı k: [T_REQ+150k-50, T_REQ+150k]); /fapi/v3/balance = 4. çağrı →
# [T_REQ+550, T_REQ+600].
# Çakışma OLMAYAN olay zamanı: tüm REST pencerelerinden SONRA.
EV_LATE = T_REQ + 60_000
# Çakışan olay zamanı: /fapi/v3/balance penceresinin içine düşer.
EV_DURING = T_REQ + 575


def _baseline():
    return AccountAdapter(FakeTransport(_full_responses())).collect_baseline()


def _uds(events, state=ST_CONNECTED, connected=True):
    return UdsSessionResult(
        stream_state=state, connected=connected, listen_key_created=True,
        keepalive_put_ok=True, events=tuple(events), duplicates=0,
        parse_errors=0, unknown_event_types=(), coverage_limited=False,
        ws_url="wss://fstream.binance.com/ws/<redacted>",
        session_started_at_ms=T_REQ + 50_000,
        session_ended_at_ms=T_REQ + 70_000, error_detail=None)


def _bal_event(cw, t=EV_LATE):
    return UdsEvent(event_type="ACCOUNT_UPDATE", local_receive_time_ms=t + 5,
                    event_time_ms=t, transaction_time_ms=t,
                    balances=(UdsBalanceDelta(asset="USDT",
                                              cross_wallet_balance=cw),),
                    fingerprint=f"fp-{cw}-{t}")


class StateMachineTests(unittest.TestCase):

    def test_baseline_incomplete_wins_over_everything(self):
        b = _baseline()
        self.assertTrue(b.complete)  # sanity
        resp = _full_responses()
        del resp["/fapi/v2/positionRisk"]
        t = FakeTransport(resp)
        t.fail = {"/fapi/v2/positionRisk"}
        bad = AccountAdapter(t).collect_baseline()
        v = reconcile(bad, _uds([_bal_event(3491.46)],
                                state="LISTEN_KEY_EXPIRED"))
        # öncelik: DATA_INCOMPLETE > STREAM_DESYNCED
        self.assertEqual(v.state, S_DATA_INCOMPLETE)
        self.assertFalse(v.baseline_valid)

    def test_desynced_stream(self):
        v = reconcile(_baseline(), _uds([], state="LISTEN_KEY_EXPIRED"))
        self.assertEqual(v.state, S_STREAM_DESYNCED)
        self.assertTrue(v.desynced)
        for s in ("PARSE_ERROR", "CLOSED_EARLY"):
            v2 = reconcile(_baseline(), _uds([], state=s))
            self.assertEqual(v2.state, S_STREAM_DESYNCED)

    def test_no_uds_session_is_pending(self):
        v = reconcile(_baseline(), None)
        self.assertEqual(v.state, S_RECON_PENDING)
        self.assertFalse(v.stream_connected)

    def test_uds_not_connected_is_pending(self):
        v = reconcile(_baseline(), _uds([], connected=False,
                                        state="NOT_CONNECTED"))
        self.assertEqual(v.state, S_RECON_PENDING)

    def test_connected_no_events_never_reconciled(self):
        """VERİSİZ RECONCILED YOK — olay yoksa karşılaştırma yok."""
        v = reconcile(_baseline(), _uds([]))
        self.assertEqual(v.state, S_RECON_PENDING)
        self.assertEqual(v.events_observed, 0)
        self.assertFalse(v.reconciled)
        self.assertTrue(any("CONNECTED_NO_ACCOUNT_EVENT_OBSERVED" in n
                            for n in v.notes))

    def test_full_match_is_reconciled(self):
        v = reconcile(_baseline(), _uds([_bal_event(3491.46)]))
        self.assertEqual(v.state, S_RECONCILED)
        self.assertTrue(v.baseline_valid)
        self.assertTrue(v.stream_connected)
        statuses = {c.name: c.status for c in v.comparisons}
        self.assertEqual(statuses["usdt_cross_wallet_balance"], C_MATCH)
        self.assertEqual(statuses["totalCrossWalletBalance_v2_vs_v3"],
                         C_MATCH)
        self.assertEqual(statuses["non_flat_symbols_positionRisk_vs_account_v3"],
                         C_MATCH)


class FieldStatusTests(unittest.TestCase):

    def test_mismatch_on_usdt_balance(self):
        v = reconcile(_baseline(), _uds([_bal_event(3400.0)]))
        self.assertEqual(v.state, S_RECON_MISMATCH)
        comp = next(c for c in v.comparisons
                    if c.name == "usdt_cross_wallet_balance")
        self.assertEqual(comp.status, C_MISMATCH)
        self.assertEqual(comp.rest_value, 3491.46)
        self.assertEqual(comp.uds_value, 3400.0)

    def test_time_overlap_is_inconclusive_not_mismatch(self):
        # olay zamanı /fapi/v3/balance penceresinin İÇİNDE → eşit olsa
        # bile kesin MATCH denemez (REST atomik değil)
        v = reconcile(_baseline(), _uds([_bal_event(3491.46, t=EV_DURING)]))
        comp = next(c for c in v.comparisons
                    if c.name == "usdt_cross_wallet_balance")
        self.assertEqual(comp.status, C_INCONCLUSIVE)
        self.assertIn("kesişiyor", comp.note)

    def test_inconclusive_blocks_reconciled(self):
        v = reconcile(_baseline(), _uds([_bal_event(3491.46, t=EV_DURING)]))
        self.assertEqual(v.state, S_RECON_PENDING)
        self.assertFalse(v.reconciled)

    def test_no_usdt_event_not_comparable(self):
        ev = UdsEvent(event_type="ACCOUNT_UPDATE",
                      local_receive_time_ms=EV_LATE,
                      event_time_ms=EV_LATE, transaction_time_ms=EV_LATE,
                      balances=(UdsBalanceDelta(asset="BNB",
                                                cross_wallet_balance=0.5),),
                      fingerprint="fp-bnb")
        v = reconcile(_baseline(), _uds([ev]))
        comp = next(c for c in v.comparisons
                    if c.name == "usdt_cross_wallet_balance")
        self.assertEqual(comp.status, C_NOT_COMPARABLE)

    def test_event_without_time_is_treated_as_overlap(self):
        # E/T yok → kesişim KANITLANAMAZ → fail-closed INCONCLUSIVE
        ev = UdsEvent(event_type="ACCOUNT_UPDATE",
                      local_receive_time_ms=EV_LATE,
                      balances=(UdsBalanceDelta(asset="USDT",
                                                cross_wallet_balance=3491.46),),
                      fingerprint="fp-notime")
        v = reconcile(_baseline(), _uds([ev]))
        comp = next(c for c in v.comparisons
                    if c.name == "usdt_cross_wallet_balance")
        self.assertEqual(comp.status, C_INCONCLUSIVE)

    def test_scope_notes_present(self):
        v = reconcile(_baseline(), _uds([_bal_event(3491.46)]))
        comp = next(c for c in v.comparisons
                    if c.name == "usdt_cross_wallet_balance")
        self.assertIn("ASSET kapsamı", comp.note)  # hesap toplamı DEĞİL
        self.assertTrue(any("WS'ten türetilmez" in n for n in v.notes))


class IntegrityTests(unittest.TestCase):

    def test_baseline_not_mutated(self):
        b = _baseline()
        before = json.dumps(b.to_safe_dict(), sort_keys=True)
        reconcile(b, _uds([_bal_event(3491.46)]))
        reconcile(b, _uds([_bal_event(1.0)]))
        after = json.dumps(b.to_safe_dict(), sort_keys=True)
        self.assertEqual(before, after)

    def test_safe_output_no_credentials(self):
        v = reconcile(_baseline(), _uds([_bal_event(3491.46)]))
        blob = json.dumps(v.to_safe_dict())
        self.assertNotIn("TESTLISTENKEY", blob)
        self.assertNotIn("signature", blob)

    def test_no_calibrated_threshold_constants(self):
        """Eşik uydurma YOK: modülde TOLERANCE/THRESHOLD/STALE sabiti
        yok; karşılaştırma eşitlik veya kapsam bazlı."""
        import canary_account_reconcile as m
        src = open(m.__file__, encoding="utf-8").read()
        for token in ("TOLERANCE", "THRESHOLD", "STALE", "MAX_AGE",
                      "eps", "1e-", "0.01", "0.05"):
            self.assertNotIn(token, src.split('"""', 2)[2],  # docstring dışı
                             f"eşik sabiti bulundu: {token}")

    def test_v2_v3_internal_consistency_compared(self):
        v = reconcile(_baseline(), _uds([]))
        # olay olmasa bile REST-içi v2/v3 karşılaştırması listelenir mi?
        # Hayır: karşılaştırmalar yalnız olay varken yapılır — PENDING
        self.assertEqual(v.comparisons, ())


if __name__ == "__main__":
    unittest.main()
