"""canary_brain_adapter.py — CANARY AI BRAIN → Futures Canary execution
SÖZLEŞMESİ (görev §2, 2026-11-26).

Yeni beyin (futures-brain.service, canary_brain_runner.py) kararlarını
BRAIN_DB SQLite'ına yazar (prediction_records + brain_cycles). Bu modül
execution katmanının (futures-canary.service, canary_live.py) beynin
adaylarını GÜVENLİ şekilde okuması için tek nokta:

  - BRAIN_DB yalnız SALT-OKUNUR açılır (mode=ro — execution beynin
    araştırma belleğine ASLA yazmaz).
  - Karar kimliği decision_id = "BR{cycle_id}:{symbol}:{direction}" —
    beynin prediction_records satırından (cycle_id, symbol, direction)
    deterministik türetilir; aynı karar ikinci kez emir ÜRETEMEZ
    (canary_live brainsig:{decision_id} dedup anahtarı).
  - Tazelik: son döngünün yaşı max_age_ms'i aşarsa aday YOK
    (fail-closed — beyin ayakta değilse execution giriş yapmaz).
  - model_version doğrulaması: beklenen sürüm uyuşmazsa aday YOK
    (fail-closed — tanınmayan model çıktısı işlenmez).
  - Eski 8 sembol listesi (canary_live.SYMBOLS) bu sözleşmenin HİÇBİR
    yerinde kullanılmaz: evren beynin keşif çıktısıdır.

SÖZLEŞME ALANLARI (görev §2 — BrainDecision):
  symbol, direction, timestamp, model_version, expected_net_return,
  estimated_costs, probability/calibration_status, uncertainty,
  stop_distance, suggested_notional, suggested_quantity,
  rejection_reasons, decision_id (+ tam kayıt provenance için `record`).

EMRİ YOKTUR: bu modül yalnız OKUR. Emir kararı canary_live giriş
zincirinde verilir; CROSS risk kapısı (canary_risk.check_order +
AccountRiskGate) son kararı verir — beyin önerisi emir DEĞİLDİR.
"""
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

# ---- sinyal kaynağı etiketleri (canary_live.LiveCanary.signal_source) --------
SIGNAL_SOURCE_H1 = "H1_LOCAL"            # eski 8 sembollük yerel motor (legacy)
SIGNAL_SOURCE_BRAIN = "BRAIN_PRIMARY"    # yeni beyin ana sinyal kaynağı

# Beyin 4h bar kapanış+60s'de tarar. Bir tam bar + payı: son döngü bundan
# eskiyse beyin ayakta DEĞİL sayılır → aday YOK (fail-closed).
DEFAULT_MAX_AGE_MS = 5 * 3600 * 1000

# ---- fail-closed ret kodları (BA_ = brain adapter) --------------------------
BA_DB_MISSING = "BA_DB_MISSING"
BA_DB_ERROR = "BA_DB_ERROR"
BA_NO_CYCLE = "BA_NO_CYCLE"
BA_CYCLE_STALE = "BA_CYCLE_STALE"
BA_MODEL_MISMATCH = "BA_MODEL_MISMATCH"
BA_CYCLE_NO_TRADE = "BA_CYCLE_NO_TRADE"

DEFAULT_DB = "/var/lib/futures-canary-brain/brain.db"


def decision_id_for(cycle_id: int, symbol: str, direction: int) -> str:
    """Beyin kaydından deterministik karar kimliği. Aynı (döngü, sembol,
    yön) üçlüsü her zaman aynı kimliği üretir — mükerrer emir korumasının
    çapasıdır ve beynin prediction_records satırına birebir izlenebilir."""
    return f"BR{int(cycle_id)}:{symbol}:{int(direction)}"


@dataclass(frozen=True)
class BrainDecision:
    """§2 sözleşme kaydı — beyinden execution'a aktarılan aday."""
    decision_id: str
    symbol: str
    direction: int                    # +1 LONG / -1 SHORT
    ts: int                           # beyin değerlendirme anı (UTC epoch ms)
    cycle_id: int
    model_version: str
    expected_net_return_pct: Optional[float]
    expected_transaction_costs_pct: Optional[float]
    calibrated_probability: Optional[float]
    probability_is_calibrated: bool
    prediction_uncertainty_pct: Optional[float]
    stop_distance_pct: Optional[float]
    suggested_notional: Optional[float]
    suggested_quantity: Optional[float]
    rejection_reasons: tuple
    calibration_domain: Optional[str]
    price: Optional[float]
    sizing: Optional[dict]            # canary_brain_sizing çıktısı (tam)
    record: dict                      # tam beyin kaydı (provenance/audit)


class BrainDecisionSource:
    """BRAIN_DB salt-okunur karar kaynağı (ağ YOK, yazma YOK).

    Test edilebilirlik: db_path/now_ms enjekte edilir; ayrı bir BRAIN_DB
    fixture'ı ile çalışır (canary_brain_runner.BrainDB şemasıyla uyumlu).
    """

    def __init__(self, db_path: str = DEFAULT_DB,
                 max_age_ms: int = DEFAULT_MAX_AGE_MS,
                 expected_model_version: Optional[str] = None,
                 now_ms=None):
        self.db_path = str(db_path)
        self.max_age_ms = int(max_age_ms)
        self.expected_model_version = expected_model_version
        self._now_ms = now_ms or (lambda: int(__import__("time").time()
                                               * 1000))

    # ---- düşük seviye okuma ------------------------------------------------
    def _connect(self) -> Optional[sqlite3.Connection]:
        """BRAIN_DB'yi SALT-OKUNUR açar; yoksa/okunamazsa None."""
        if not self.db_path or not os.path.exists(self.db_path):
            return None
        try:
            return sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            return None

    def latest_cycle(self) -> Optional[dict]:
        """Son beyin döngüsü özeti (yoksa None)."""
        con = self._connect()
        if con is None:
            return None
        try:
            row = con.execute(
                "SELECT cycle_id, ts, decision, model_version,"
                " universe_eligible, candidates FROM brain_cycles"
                " ORDER BY cycle_id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            return {"cycle_id": row[0], "ts": row[1], "decision": row[2],
                    "model_version": row[3], "universe_eligible": row[4],
                    "candidates": row[5]}
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def candidates(self, cycle_id: int) -> list:
        """Bir döngünün CANDIDATE kayıtlarından BrainDecision listesi."""
        con = self._connect()
        if con is None:
            return []
        out = []
        try:
            rows = con.execute(
                "SELECT symbol, direction, record_json FROM prediction_records"
                " WHERE cycle_id=? AND decision='CANDIDATE'"
                " ORDER BY symbol, direction", (int(cycle_id),)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        for symbol, direction, rj in rows:
            try:
                rec = json.loads(rj)
            except (TypeError, ValueError):
                continue          # bozuk kayıt işlenmez (fail-closed)
            sizing = rec.get("sizing") or None
            out.append(BrainDecision(
                decision_id=decision_id_for(cycle_id, symbol, direction),
                symbol=symbol, direction=int(direction),
                ts=int(rec.get("ts") or 0), cycle_id=int(cycle_id),
                model_version=str(rec.get("model_version") or ""),
                expected_net_return_pct=rec.get("expected_net_return_pct"),
                expected_transaction_costs_pct=rec.get(
                    "expected_transaction_costs_pct"),
                calibrated_probability=rec.get("calibrated_probability"),
                probability_is_calibrated=bool(
                    rec.get("probability_is_calibrated")),
                prediction_uncertainty_pct=rec.get(
                    "prediction_uncertainty_pct"),
                stop_distance_pct=rec.get("stop_distance_pct"),
                suggested_notional=(sizing or {}).get("notional"),
                suggested_quantity=(sizing or {}).get("qty"),
                rejection_reasons=tuple(
                    rec.get("rejection_reasons") or ()),
                calibration_domain=rec.get("calibration_domain"),
                price=rec.get("price"),
                sizing=sizing, record=rec))
        return out

    # ---- execution girişi ----------------------------------------------------
    def fresh_candidates(self, now_ms=None) -> dict:
        """Taze + doğrulanmış adaylar (fail-closed).

        Dönüş: {"ok": bool, "reasons": [...], "cycle_id", "cycle_ts",
        "age_ms", "candidates": [BrainDecision]}
        ok=False iken candidates HER ZAMAN boştur ve reasons gerekçeyi
        taşır — execution eski/bilinmeyen model çıktısıyla emir YAPMAZ.
        """
        now = now_ms if now_ms is not None else self._now_ms()
        out = {"ok": False, "reasons": [], "cycle_id": None,
               "cycle_ts": None, "age_ms": None, "candidates": []}
        if not os.path.exists(self.db_path):
            out["reasons"].append(f"{BA_DB_MISSING}:{self.db_path}")
            return out
        cyc = self.latest_cycle()
        if cyc is None:
            # DB var ama okunamadı ya da döngü yok — ayrıştır
            con = self._connect()
            if con is not None:
                con.close()
                out["reasons"].append(BA_NO_CYCLE)
            else:
                out["reasons"].append(f"{BA_DB_ERROR}:{self.db_path}")
            return out
        out["cycle_id"] = cyc["cycle_id"]
        out["cycle_ts"] = cyc["ts"]
        out["age_ms"] = now - cyc["ts"]
        if out["age_ms"] > self.max_age_ms:
            out["reasons"].append(
                f"{BA_CYCLE_STALE}:age={out['age_ms']}ms>"
                f"{self.max_age_ms}ms (cycle {cyc['cycle_id']})")
            return out
        if self.expected_model_version and \
                cyc["model_version"] != self.expected_model_version:
            out["reasons"].append(
                f"{BA_MODEL_MISMATCH}:db={cyc['model_version']!r} != "
                f"beklenen={self.expected_model_version!r}")
            return out
        cands = self.candidates(cyc["cycle_id"])
        if not cands:
            out["reasons"].append(
                f"{BA_CYCLE_NO_TRADE}:cycle {cyc['cycle_id']} "
                f"decision={cyc['decision']} "
                f"candidates={cyc['candidates']}")
        out["candidates"] = cands
        out["ok"] = bool(cands)          # aday yoksa ok=False (NO_TRADE)
        return out


def brain_fixture_db(db_path: str, cycles: list) -> str:
    """Test yardımcısı: BrainDB şemasına sentetik döngü/kayıt yazar.
    Var olan satırları TEMİZLER (fixture dosyanın yeni içeriğidir).

    cycles: [{"cycle_id", "ts", "decision", "model_version",
              "universe_eligible", "candidates", "records": [record dicts]}]
    Gerçek BrainDB şemasıyla (canary_brain_runner.BrainDB.SCHEMA) birebir
    aynı tabloları üretir — testler ağ/gerçek beyin gerektirmez."""
    db = sqlite3.connect(db_path)
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS brain_cycles (
            cycle_id INTEGER PRIMARY KEY,
            ts INTEGER NOT NULL, decision TEXT NOT NULL,
            universe_eligible INTEGER, market_data_covered INTEGER,
            liquid INTEGER, history_warm INTEGER, scored INTEGER,
            candidates INTEGER, long_candidates INTEGER, short_candidates INTEGER,
            rejected INTEGER, no_trade_symbols INTEGER,
            model_version TEXT, funnel_json TEXT, notes_json TEXT);
        CREATE TABLE IF NOT EXISTS prediction_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL, ts INTEGER NOT NULL,
            symbol TEXT NOT NULL, direction INTEGER NOT NULL,
            decision TEXT NOT NULL, record_json TEXT NOT NULL);
        DELETE FROM brain_cycles;
        DELETE FROM prediction_records;
        """)
        for cyc in cycles:
            db.execute(
                "INSERT OR REPLACE INTO brain_cycles VALUES (?,?,?,?,?,?,?,?"
                ",?,?,?,?,?,?,?,?)",
                (cyc["cycle_id"], cyc["ts"], cyc["decision"],
                 cyc.get("universe_eligible", 0),
                 cyc.get("market_data_covered", 0), cyc.get("liquid", 0),
                 cyc.get("history_warm", 0), cyc.get("scored", 0),
                 len(cyc.get("records") or []),
                 cyc.get("long_candidates", 0), cyc.get("short_candidates", 0),
                 cyc.get("rejected", 0), cyc.get("no_trade_symbols", 0),
                 cyc.get("model_version", ""), "{}", "[]"))
            for rec in cyc.get("records") or []:
                db.execute(
                    "INSERT INTO prediction_records (cycle_id, ts, symbol,"
                    " direction, decision, record_json) VALUES (?,?,?,?,?,?)",
                    (cyc["cycle_id"], rec.get("ts", cyc["ts"]),
                     rec["symbol"], rec["direction"], rec["decision"],
                     json.dumps(rec)))
        db.commit()
    finally:
        db.close()
    return db_path
