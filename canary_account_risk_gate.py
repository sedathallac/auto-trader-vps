"""canary_account_risk_gate.py — B.1 İŞ 4: hesap-seviyesi CROSS risk kapısı.

GİRİŞ (her evaluate çağrısında):
  - doğrulanmış AccountSnapshot (canary_account_adapter REST baseline —
    7 endpoint, sembol filtresiz),
  - hesap konfigürasyonu (accountConfig; Multi-Assets durumu),
  - BÜTÜN positionRisk envanteri (958 sembol — 8 sembole filtre YOK),
  - standart açık emirler VE algo emirler (ayrı ayrı),
  - mevcut Canary ledger (RiskManager — rm),
  - aday işlem (symbol/direction/notional),
  - leverage bracket verisi (kurulumda fetch edilir; canary_liquidation
    parse_leverage_brackets formatı),
  - veri güncellik durumu (REST alım zamanından hesaplanır).

ÇIKIŞ: AccountGateDecision — PASS/BLOCK + makinece okunabilir neden
listesi (AG_* kodları) + denetlenebilir karar kaydı (audit dict).

KURALLAR (görev B.1 İŞ 4, A-F):
  A. availableBalance DOĞRUDAN Binance yanıtından; HİÇBİR formülle
     yeniden hesaplanmaz. totalCrossWalletBalance / totalCrossUnPnl ayrı;
     pozisyon uPnL'i hesap toplamına TEKRAR EKLENMEZ (double-count YOK).
     totalMaintMargin / totalInitialMargin / totalPositionInitialMargin /
     totalOpenOrderInitialMargin ayrı alanlarda; açık emir IM ikinci kez
     SAYILMAZ (availableBalance borsa tarafında zaten düşülüştir).
     CROSS ve ISOLATED karıştırılmaz. Multi-Assets açık/bilinmiyor/
     çelişkili → BLOCK.
  B. 8 sembole filtre YOK. Canary dışı (UNKNOWN_OWNER) pozisyon veya
     emir → BLOCK. Veri alınamazsa sıfır pozisyon VARSAYILMAZ → BLOCK.
     Standart + algo emirler birlikte görünür.
  C. Eski/eksik snapshot ile giriş YOK. Yaş = REST alım zamanından
     (response_received_at_ms). Eşik kaynağı YALNIZ operatör konfigi;
     kaynak kodda eşik ICAT EDİLMEZ. Konfig yoksa → BLOCK (güvenli
     varsayılan). [Öneri raporda; canlıya izin veren eşik operatör
     onayı olmadan AKTİF EDİLMEZ.]
  D. Exchange availableBalance + hesap marjin alanları doğrudan.
     Pozisyon/emir/aday marjinleri KARIŞTIRILMAZ. Leverage bracket
     verisi zorunlu (IM = notional/leverage TEK BAŞINA yeterli SAYILMAZ;
     bakım marjı ve bracket etkisi hesaba katılır). Hesap başı marjin
     tamponu YALNIZ operatör konfigi; yoksa → BLOCK. Binance -2019
     reddi tek güvenlik kontrolü DEĞİLDİR (ex-ante kontrol burada).
  E. 5/10 USDT zarar bütçeleri canary_risk'te OLDUĞU GİBİ (değişmez;
     100/200 notional ve 2 pozisyon sabitleri yalnız mevcut canary
     politikasıdır — bu kapının matematiksel sabiti DEĞİL).
  F. Likidasyon modelinin doğrulanmamış CROSS tahminleri bu kapının
     tek dayanağı DEĞİLDİR (yalnız uyarı; exchange verisi esastır).

BAĞLANTI: canary_risk.RiskManager.check_order başında çağrılır
(account_gate=None → davranış değişmez; geriye uyumlu). BLOCK →
OrderRejected(AG_* nedenleri) → try_entry REJECTED olarak KAYDEDER
(denetim izi live_orders.jsonl'de).
"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from canary_account_adapter import (
    AccountAdapter, RestBaselineResult, MA_SINGLE,
)

# Makinece okunabilir BLOCK kodları
AG_BASELINE_INCOMPLETE = "AG_BASELINE_INCOMPLETE"
AG_MULTI_ASSETS_UNSUPPORTED = "AG_MULTI_ASSETS_UNSUPPORTED"
AG_COMPONENT_MISSING = "AG_COMPONENT_MISSING"
AG_UNKNOWN_OWNER_POSITION = "AG_UNKNOWN_OWNER_POSITION"
AG_UNKNOWN_OWNER_ORDER = "AG_UNKNOWN_OWNER_ORDER"
AG_LEDGER_EXCHANGE_MISMATCH = "AG_LEDGER_EXCHANGE_MISMATCH"
AG_FRESHNESS_THRESHOLD_UNCONFIGURED = "AG_FRESHNESS_THRESHOLD_UNCONFIGURED"
AG_STALE_ACCOUNT_DATA = "AG_STALE_ACCOUNT_DATA"
AG_MARGIN_BUFFER_UNCONFIGURED = "AG_MARGIN_BUFFER_UNCONFIGURED"
AG_BRACKETS_MISSING = "AG_BRACKETS_MISSING"
AG_INSUFFICIENT_AVAILABLE_BALANCE = "AG_INSUFFICIENT_AVAILABLE_BALANCE"
AG_PROJECTED_MAINT_MARGIN = "AG_PROJECTED_MAINT_MARGIN"
AG_EVALUATION_ERROR = "AG_EVALUATION_ERROR"


@dataclass(frozen=True)
class AccountGateDecision:
    passed: bool
    reasons: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    audit: dict = field(default_factory=dict)

    def to_safe_dict(self) -> dict:
        return {"passed": self.passed, "reasons": list(self.reasons),
                "warnings": list(self.warnings), "audit": self.audit}


def _bracket_for(brackets: List[dict], notional: float) -> Optional[dict]:
    """Aday notional'ın düştüğü bracket (floor <= n < cap); yoksa None."""
    for b in brackets:
        if b["floor"] <= notional < b["cap"]:
            return b
    return None


class AccountRiskGate:
    """Hesap-seviyesi CROSS risk kapısı. DETERMİNİSTİK ve fail-closed:
    değerlendirilemeyen HER şey BLOCK'tur (sessiz geçiş YOK).

    Konfigürasyon (operatör onaylı; yoksa güvenli varsayılan BLOCK):
      max_age_ms          — hesap verisi güncellik eşiği (ms). ÖNERİ:
                            30_000 (REST baseline ~2-4s; sinyal anında
                            taze snapshot için yeterli; dayanak: giriş
                            kararı anlık hesap durumu ister, UDS'siz
                            kurulumda REST yenilenmesi gerekir).
      margin_buffer_usdt  — hesap başı marjin tamponu (USDT). ÖNERİ:
                            50 (aday IM üstünde tutulacak emniyet payı;
                            dayanak: 100 USDT notional politikası altında
                            tek pozisyon IM ~5-20 USDT; tampon komisyon+
                            funding+MM artışını karşılar). DEĞERLER
                            OPERATÖR ONAYI OLMADAN AKTİF EDİLMEZ.
    """

    def __init__(self, transport, brackets_by_symbol: Optional[dict] = None,
                 *, client_prefix: str = "C281",
                 max_age_ms: Optional[int] = None,
                 margin_buffer_usdt: Optional[float] = None,
                 clock_ms=None):
        self.transport = transport
        self.brackets_by_symbol = brackets_by_symbol or {}
        self.client_prefix = client_prefix
        self.max_age_ms = max_age_ms
        self.margin_buffer_usdt = margin_buffer_usdt
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    # -- yardımcılar --
    def _baseline(self) -> RestBaselineResult:
        return AccountAdapter(self.transport).collect_baseline()

    def _age_ms(self, baseline: RestBaselineResult,
                now_ms: int) -> Optional[int]:
        """Yaş = REST ALIM zamanından (kaynak updateTime'ından DEĞİL;
        updateTime=0 sentinel olabilir — İş 1)."""
        times = [r.response_received_at_ms for r in baseline.records
                 if r.response_received_at_ms]
        if not times:
            return None
        return now_ms - max(times)

    # -- ana değerlendirme --
    def evaluate(self, symbol: str, direction: int, notional: float,
                 rm, now_ms: Optional[int] = None) -> AccountGateDecision:
        now_ms = now_ms or self._clock_ms()
        reasons: List[str] = []
        warnings: List[str] = []
        audit: dict = {"t": now_ms, "kind": "ACCOUNT_GATE_DECISION",
                       "symbol": symbol, "direction": direction,
                       "notional": notional}

        try:
            baseline = self._baseline()
        except Exception as e:  # ağ/parse — fail-closed
            audit["error"] = f"{type(e).__name__}"
            return AccountGateDecision(
                False, (f"{AG_EVALUATION_ERROR}:baseline:{type(e).__name__}",),
                warnings, audit)

        audit["rest_complete"] = baseline.complete
        audit["multi_assets"] = baseline.multi_assets_status
        audit["weight_1m_end"] = baseline.records[-1].used_weight_1m \
            if baseline.records else None

        # --- A: kaynak bütünlüğü ---
        if not baseline.complete:
            reasons.append(f"{AG_BASELINE_INCOMPLETE}:"
                           f"{';'.join(baseline.incomplete_reasons)[:200]}")
        if baseline.multi_assets_status != MA_SINGLE:
            reasons.append(f"{AG_MULTI_ASSETS_UNSUPPORTED}:"
                           f"{baseline.multi_assets_status}")
        for name, comp in (("config", baseline.config),
                           ("totals_v2", baseline.totals_v2),
                           ("totals_v3", baseline.totals_v3),
                           ("balance_v3", baseline.balance_v3),
                           ("positions", baseline.positions),
                           ("open_orders", baseline.open_orders),
                           ("open_algo_orders", baseline.open_algo_orders)):
            if comp is None:
                # veri alınamadı → sıfır pozisyon/emir VARSAYILMAZ
                reasons.append(f"{AG_COMPONENT_MISSING}:{name}")

        # envanter sayıları (denetim)
        inv_positions = baseline.positions.non_flat() if baseline.positions else None
        audit["n_non_flat_positions"] = len(inv_positions) \
            if inv_positions is not None else None
        audit["n_open_orders"] = len(baseline.open_orders.entries) \
            if baseline.open_orders else None
        audit["n_open_algo_orders"] = len(baseline.open_algo_orders.entries) \
            if baseline.open_algo_orders else None

        # --- B: tam envanter + sahiplik ---
        if rm is not None:
            ledger_syms = {p["symbol"] for p in rm.open_positions()}
            audit["ledger_positions"] = sorted(ledger_syms)
            if inv_positions is not None:
                inv_syms = {p.symbol for p in inv_positions}
                foreign = inv_syms - ledger_syms
                for s in sorted(foreign):
                    reasons.append(f"{AG_UNKNOWN_OWNER_POSITION}:{s}")
                missing = ledger_syms - inv_syms
                if missing:  # ledger'da var, borsada yok → veri çelişkisi
                    reasons.append(f"{AG_LEDGER_EXCHANGE_MISMATCH}:"
                                   f"{','.join(sorted(missing))}")
        if baseline.open_orders is not None:
            for o in baseline.open_orders.entries:
                cid = o.clientOrderId or ""
                if not cid.startswith(self.client_prefix):
                    reasons.append(f"{AG_UNKNOWN_OWNER_ORDER}:{o.orderId}")
        if baseline.open_algo_orders is not None:
            for o in baseline.open_algo_orders.entries:
                cid = o.clientAlgoId or ""
                if not cid.startswith(self.client_prefix):
                    reasons.append(f"{AG_UNKNOWN_OWNER_ORDER}:algo:{o.algoId}")

        # --- C: güncellik (eşik yalnız operatör konfiginden) ---
        age = self._age_ms(baseline, now_ms)
        audit["account_data_age_ms"] = age
        if self.max_age_ms is None:
            reasons.append(f"{AG_FRESHNESS_THRESHOLD_UNCONFIGURED}:"
                           "CANARY_ACCOUNT_MAX_AGE_MS ayarlı değil — "
                           "güvenli varsayılan BLOCK")
        elif age is None:
            reasons.append(f"{AG_STALE_ACCOUNT_DATA}:REST alım zamanı yok")
        elif age > self.max_age_ms:
            reasons.append(f"{AG_STALE_ACCOUNT_DATA}:{age}ms > "
                           f"{self.max_age_ms}ms")

        # --- D: marjin (bracket + exchange availableBalance) ---
        if baseline.balance_v3 is not None and baseline.totals_v2 is not None:
            try:
                usdt = baseline.balance_v3.usdt()
                avail = usdt.availableBalance          # borsa değeri (A)
                t2 = baseline.totals_v2
                cross_mb = t2.totalCrossWalletBalance + t2.totalCrossUnPnl
                audit["available_balance"] = avail
                audit["total_cross_wallet"] = t2.totalCrossWalletBalance
                audit["total_cross_un_pnl"] = t2.totalCrossUnPnl
                audit["cross_margin_balance"] = cross_mb
                audit["total_maint_margin"] = t2.totalMaintMargin
                audit["total_initial_margin"] = t2.totalInitialMargin
                audit["total_position_im"] = t2.totalPositionInitialMargin
                audit["total_open_order_im"] = t2.totalOpenOrderInitialMargin
            except Exception as e:
                reasons.append(f"{AG_EVALUATION_ERROR}:balance:{type(e).__name__}")
            else:
                brackets = self.brackets_by_symbol.get(symbol)
                if not brackets:
                    reasons.append(f"{AG_BRACKETS_MISSING}:{symbol}")
                else:
                    br = _bracket_for(brackets, abs(notional))
                    if br is None:
                        reasons.append(f"{AG_BRACKETS_MISSING}:{symbol}:"
                                       "aday notional bracket dışında")
                    else:
                        # aday IM: bracket maksimum kaldıracıyla en düşük
                        # başlangıç marjı (IM=notional/leverage TEK BAŞINA
                        # yeterli SAYILMAZ — MM ve bracket etkisi ayrıca)
                        max_lev = br.get("max_leverage") or 1
                        cand_im = abs(notional) / max(1, max_lev)
                        cand_mm = br["ratio"] * abs(notional) + br["cum"]
                        # mevcut CROSS bakım marjı (bottom-up; toplamlarla
                        # KARIŞTIRILMAZ — double-count YOK)
                        cur_cross_mm = sum(
                            (p.maintMargin or 0.0) for p in inv_positions
                        ) if inv_positions else 0.0
                        audit["candidate_im"] = cand_im
                        audit["candidate_mm"] = cand_mm
                        audit["current_cross_maint"] = cur_cross_mm
                        if self.margin_buffer_usdt is None:
                            reasons.append(
                                f"{AG_MARGIN_BUFFER_UNCONFIGURED}:"
                                "CANARY_ACCOUNT_MARGIN_BUFFER_USDT ayarlı "
                                "değil — güvenli varsayılan BLOCK")
                        else:
                            buf = self.margin_buffer_usdt
                            if avail < cand_im + buf:
                                reasons.append(
                                    f"{AG_INSUFFICIENT_AVAILABLE_BALANCE}:"
                                    f"{avail:.4f} < IM {cand_im:.4f} + "
                                    f"tampon {buf}")
                            if cross_mb - (cur_cross_mm + cand_mm) < buf:
                                reasons.append(
                                    f"{AG_PROJECTED_MAINT_MARGIN}:"
                                    f"cross {cross_mb:.4f} - MM "
                                    f"{cur_cross_mm + cand_mm:.4f} < "
                                    f"tampon {buf}")
                # F: likidasyon modeli tahmini yalnız UYARI (dayanak DEĞİL)
                warnings.append("likidasyon modeli tahminleri bu kapının "
                                "dayanağı DEĞİLDİR (exchange verisi esastır)")

        # --- E: 5/10 USDT zarar bütçeleri canary_risk'te (değişmez) ---
        if rm is not None:
            try:
                audit["ledger_total_net_loss"] = rm.total_net_loss(now_ms)
            except Exception:
                audit["ledger_total_net_loss"] = None

        return AccountGateDecision(not reasons, tuple(reasons),
                                   tuple(warnings), audit)
