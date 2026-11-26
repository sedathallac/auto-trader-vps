"""canary_account_reconcile.py — AŞAMA B: REST↔UDS mutabakat.

GÖREV (AŞAMA B): REST baseline ile UDS gözlemini kapsam-farkında
karşılaştırır. Gölge/teşhis modudur — canlı kapıya bağlı DEĞİL.

İLKELER:
  1. EŞİK UYDURMA YOK: 'yeterince yakın', 'tolere edilebilir gecikme'
     gibi kalibre edilmemiş eşikler YOKTUR. Karşılaştırma eşitlik veya
     kapsam bazlıdır; zaman çakışması varsa INCONCLUSIVE denir.
  2. VERİSİZ RECONCILED YOK: karşılaştırılabilir hiç alan yoksa durum
     asla RECONCILED olamaz.
  3. ZAMAN ÇAKIŞMASI → INCONCLUSIVE: bir UDS olayının E/T zamanı, bir
     REST çağrısının [request_start, response_end] penceresine düşerse
     o REST alanı ile WS türevi karşılaştırma güvenilmez sayılır.
  4. NON-ATOMIC KORUMA: her REST alanı kendi çağrı kaydının zamanıyla
     karşılaştırılır; baseline tek zaman DAMGASINA indirgenmez.
  5. HESAP TOPLAMLARI DEĞİŞMEZ: mutabakat yalnız okur; RestBaselineResult
     ve UdsDeltaState kopyalanmaz/değiştirilmez.

DURUMLAR (öncelik sırasıyla):
  DATA_INCOMPLETE         — baseline eksik (endpoint hatası/parse hatası/
                            Multi-Assets destek dışı)
  STREAM_DESYNCED         — UDS oturumu expired/parse/close-early
  RECONCILIATION_PENDING  — akış yok, olay yok, veya karşılaştırma
                            INCONCLUSIVE kaldı
  RECONCILIATION_MISMATCH — en az bir kesin MISMATCH
  RECONCILED              — karşılaştırılabilir TÜM alanlar MATCH

STREAM_CONNECTED ve BASELINE_VALID bilgi amaçlı bayrak olarak raporda
taşınır (tek durum etiketi yukarıdaki beşinden biri kalır).
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from canary_account_adapter import RestBaselineResult
from canary_account_uds import UdsSessionResult

# Durumlar
S_DATA_INCOMPLETE = "DATA_INCOMPLETE"
S_STREAM_DESYNCED = "STREAM_DESYNCED"
S_RECON_PENDING = "RECONCILIATION_PENDING"
S_RECON_MISMATCH = "RECONCILIATION_MISMATCH"
S_RECONCILED = "RECONCILED"

# Alan karşılaştırma durumları
C_MATCH = "MATCH"
C_MISMATCH = "MISMATCH"
C_NOT_COMPARABLE = "NOT_COMPARABLE"
C_INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class FieldComparison:
    name: str
    rest_value: object
    uds_value: object
    status: str                       # C_*
    note: str = ""


@dataclass(frozen=True)
class ReconciliationVerdict:
    state: str
    baseline_valid: bool              # complete + SINGLE_ASSET
    stream_connected: bool
    comparisons: Tuple[FieldComparison, ...]
    events_observed: int
    desynced: bool
    notes: Tuple[str, ...]

    @property
    def reconciled(self) -> bool:
        return self.state == S_RECONCILED

    def to_safe_dict(self) -> dict:
        return {
            "state": self.state,
            "baseline_valid": self.baseline_valid,
            "stream_connected": self.stream_connected,
            "events_observed": self.events_observed,
            "desynced": self.desynced,
            "notes": list(self.notes),
            "comparisons": [
                {"name": c.name, "rest": c.rest_value, "uds": c.uds_value,
                 "status": c.status, "note": c.note}
                for c in self.comparisons
            ],
        }


def _overlaps_events(ev_start_ms: Optional[int], ev_end_ms: Optional[int],
                     uds: UdsSessionResult) -> bool:
    """REST çağrı penceresi bir UDS olayının E/T zamanıyla kesişiyor mu?

    Olay E'si yoksa (eksik alan) kesişim KANITLANAMAZ → True varsay
    (fail-closed: INCONCLUSIVE tarafına düş).
    """
    for ev in uds.events:
        t_ev = ev.transaction_time_ms or ev.event_time_ms
        if t_ev is None:
            return True
        if ev_start_ms is not None and ev_end_ms is not None:
            if ev_start_ms <= t_ev <= ev_end_ms:
                return True
        else:
            return True  # pencere zamanı yok → kanıt yok → güvenli taraf
    return False


def _num_eq(a: float, b: float) -> bool:
    """Sayısal eşitlik — eşik YOK; birebir eşitlik (parse edilen float)."""
    return a == b


def reconcile(baseline: RestBaselineResult,
              uds: Optional[UdsSessionResult]) -> ReconciliationVerdict:
    notes: List[str] = []
    comps: List[FieldComparison] = []

    baseline_valid = (baseline.complete
                      and baseline.multi_assets_status == "SINGLE_ASSET")
    stream_connected = bool(uds and uds.connected)
    desynced = bool(uds and uds.stream_state in
                    ("LISTEN_KEY_EXPIRED", "PARSE_ERROR", "CLOSED_EARLY"))
    events_observed = len(uds.events) if uds else 0

    # -- 1) baseline eksik mi? --
    if not baseline_valid:
        for r in baseline.incomplete_reasons:
            notes.append(f"baseline: {r}")
    if not baseline.complete:
        return ReconciliationVerdict(
            state=S_DATA_INCOMPLETE, baseline_valid=False,
            stream_connected=stream_connected, comparisons=(),
            events_observed=events_observed, desynced=desynced,
            notes=tuple(notes))

    # -- 2) akış desync mi? --
    if desynced:
        notes.append(f"uds: {uds.stream_state}"
                     + (f" ({uds.error_detail})" if uds.error_detail else ""))
        return ReconciliationVerdict(
            state=S_STREAM_DESYNCED, baseline_valid=baseline_valid,
            stream_connected=stream_connected, comparisons=(),
            events_observed=events_observed, desynced=True,
            notes=tuple(notes))

    # -- 3) karşılaştırma --
    any_comparable = False
    any_mismatch = False
    any_inconclusive = False

    if uds is None or not uds.connected:
        notes.append("uds: oturum yok/bağlanamadı — karşılaştırma yapılamaz")
    elif events_observed == 0:
        notes.append("uds: CONNECTED_NO_ACCOUNT_EVENT_OBSERVED — "
                     "karşılaştırılacak olay yok (verisiz RECONCILED olmaz)")
    else:
        # 3a) USDT cross cüzdanı: v3 balance (ASSET satırı, USDT) vs
        #     WS'ten izlenen USDT cross_wallet_balance. Olay pencere
        #     kesişimi varsa INCONCLUSIVE.
        if baseline.balance_v3 is not None:
            rec = next((r for r in baseline.records
                        if r.endpoint == "/fapi/v3/balance"), None)
            overlap = _overlaps_events(
                rec.request_started_at_ms if rec else None,
                rec.response_received_at_ms if rec else None, uds)
            usdt_rest = None
            try:
                usdt_rest = baseline.balance_v3.usdt()
            except Exception:
                usdt_rest = None
            uds_usdt = None
            for ev in uds.events:
                for b in ev.balances:
                    if b.asset == "USDT" and b.cross_wallet_balance is not None:
                        uds_usdt = b.cross_wallet_balance
            if usdt_rest is None:
                comps.append(FieldComparison(
                    "usdt_cross_wallet_balance", None, uds_usdt,
                    C_NOT_COMPARABLE, "REST USDT satırı yok"))
            elif uds_usdt is None:
                comps.append(FieldComparison(
                    "usdt_cross_wallet_balance",
                    usdt_rest.crossWalletBalance, None,
                    C_NOT_COMPARABLE, "UDS'te USDT bakiye olayı yok"))
            elif overlap:
                any_inconclusive = True
                comps.append(FieldComparison(
                    "usdt_cross_wallet_balance",
                    usdt_rest.crossWalletBalance, uds_usdt,
                    C_INCONCLUSIVE,
                    "REST çağrı penceresi UDS olay zamanıyla kesişiyor"))
            else:
                any_comparable = True
                m = _num_eq(usdt_rest.crossWalletBalance, uds_usdt)
                any_mismatch = any_mismatch or not m
                comps.append(FieldComparison(
                    "usdt_cross_wallet_balance",
                    usdt_rest.crossWalletBalance, uds_usdt,
                    C_MATCH if m else C_MISMATCH,
                    "ASSET kapsamı (USDT satırı); hesap toplamı DEĞİL"))

        # 3b) REST içi tutarlılık (UDS'siz): v2 vs v3 account
        #     totalCrossWalletBalance — iki bağımsız çağrı; zaman çakışması
        #     KENDİ aralarında ölçülemez (aynı istemci, serial) → pencere
        #     ayrımları korunur, fark varsa zaman-kaynaklı olabilir notu.
        if baseline.totals_v2 is not None and baseline.totals_v3 is not None:
            v2, v3 = baseline.totals_v2, baseline.totals_v3
            rec2 = next((r for r in baseline.records
                         if r.endpoint == "/fapi/v2/account"), None)
            rec3 = next((r for r in baseline.records
                         if r.endpoint == "/fapi/v3/account"), None)
            windows_overlap = bool(
                rec2 and rec3
                and rec2.response_received_at_ms is not None
                and rec3.request_started_at_ms is not None
                and rec2.response_received_at_ms >= rec3.request_started_at_ms)
            # iki çağrı serial ve bitişikse zaten ayrık; overlap burada
            # yalnızca yanlış sıra/kayıt durumunda oluşur
            for fname, rv2, rv3 in (
                    ("totalCrossWalletBalance_v2_vs_v3",
                     v2.totalCrossWalletBalance, v3.totalCrossWalletBalance),
                    ("totalCrossUnPnl_v2_vs_v3",
                     v2.totalCrossUnPnl, v3.totalCrossUnPnl)):
                if rv2 is None or rv3 is None:
                    comps.append(FieldComparison(
                        fname, rv2, rv3, C_NOT_COMPARABLE, "değer yok"))
                elif windows_overlap:
                    any_inconclusive = True
                    comps.append(FieldComparison(
                        fname, rv2, rv3, C_INCONCLUSIVE,
                        "çağrı pencereleri ayrıştırılamadı"))
                else:
                    any_comparable = True
                    m = _num_eq(rv2, rv3)
                    any_mismatch = any_mismatch or not m
                    comps.append(FieldComparison(
                        fname, rv2, rv3,
                        C_MATCH if m else C_MISMATCH,
                        "ACCOUNT_CROSS kapsamı; iki bağımsız REST kaynağı"))

        # 3c) pozisyon envanteri: positionRisk (filtresiz) vs v3 account
        #     positions[] — ikisi de REST; UDS olayı pozisyonu değiştirdiyse
        #     INCONCLUSIVE. flat satır kapsamı farklı → NOT_COMPARABLE notu.
        if baseline.positions is not None \
                and baseline.positions_from_account_v3 is not None:
            rec_pr = next((r for r in baseline.records
                           if r.endpoint == "/fapi/v2/positionRisk"), None)
            overlap = _overlaps_events(
                rec_pr.request_started_at_ms if rec_pr else None,
                rec_pr.response_received_at_ms if rec_pr else None, uds)
            non_flat_pr = {p.symbol for p in baseline.positions.non_flat()}
            non_flat_v3 = {p.symbol
                           for p in baseline.positions_from_account_v3.non_flat()}
            if overlap:
                any_inconclusive = True
                comps.append(FieldComparison(
                    "non_flat_symbols_positionRisk_vs_account_v3",
                    sorted(non_flat_pr), sorted(non_flat_v3),
                    C_INCONCLUSIVE,
                    "REST çağrı penceresi UDS pozisyon olayıyla kesişiyor"))
            else:
                any_comparable = True
                m = non_flat_pr == non_flat_v3
                any_mismatch = any_mismatch or not m
                extra_note = ("v3 account yalnız pozisyon/açık emri olan "
                              "sembolleri döndürür — flat satırlar kapsam "
                              "dışı" if m else "")
                comps.append(FieldComparison(
                    "non_flat_symbols_positionRisk_vs_account_v3",
                    sorted(non_flat_pr), sorted(non_flat_v3),
                    C_MATCH if m else C_MISMATCH,
                    extra_note or "POSITION kapsamı; sembol kümesi"))

        # 3d) WS delta görünümü yalnızca teşhis olarak not edilir:
        #     toplam türetme YOK (double-count kuralı).
        notes.append("uds delta görünümü teşhis amaçlı; hesap toplamları "
                     "REST'ten gelir, WS'ten türetilmez")

    if not any_comparable:
        if not comps:
            state = S_RECON_PENDING
        else:
            state = S_RECON_PENDING
            notes.append("karşılaştırılabilir alan kalmadı — RECONCILED yok")
    elif any_mismatch:
        state = S_RECON_MISMATCH
    elif any_inconclusive:
        state = S_RECON_PENDING
        notes.append("INCONCLUSIVE alan(lar) var — eşik uydurmadan "
                     "RECONCILED denemez")
    else:
        state = S_RECONCILED

    return ReconciliationVerdict(
        state=state, baseline_valid=baseline_valid,
        stream_connected=stream_connected, comparisons=tuple(comps),
        events_observed=events_observed, desynced=desynced,
        notes=tuple(notes))
