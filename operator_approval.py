#!/usr/bin/env python3
"""BLOK12 §1 — KALICI OPERATÖR ONAYI (ikinci kontrollü canlı işlem).

Runbook yeterli değildi; bu modül kod-seviyesi, SQLite üzerinde KALICI bir
kapıdır:

  İlk işlem tamamlanma kanıtları (ALTISI BİRLİKTE, operatör beyanıyla):
    position_flat · fills_reconciled · close_order_terminal ·
    sl_algo_verified · ledger_exchange_diff_zero · no_unknown_or_open_orders
  → durum AWAITING_OPERATOR_APPROVAL.

  İkinci işlem:
    * Açık ve TEK KULLANIMLIK onay olmadan gönderilemez (token, yalnız
      sha256 hash'i saklanır).
    * Onay SQLite'tadır; restart / worker yeniden başlaması onayı SIFIRLAMAZ.
    * Bozuk, süresi geçmiş, tüketilmiş veya yanlış token → RED.
    * İlk işlem kayıtlı tamamlanmadan ikinci işlem ONAYLANAMAZ.
    * Üçüncü işlem DAİMA RED (ayarlanamaz).

FAIL-CLOSED: depo hatası / bozuk satır / eksik bilgi → izin YOK.
Bu modül emir GÖNDERMEZ; yalnız izin verir/vermez. Tamamlanma kanıtlarının
operatör beyanı (CLI --attest) kaydı insan eylemidir; otomatik muhasebe
değildir — dürüstlük payı dokümandadır.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time

SIX_EVIDENCE = (
    "position_flat",
    "fills_reconciled",
    "close_order_terminal",
    "sl_algo_verified",
    "ledger_exchange_diff_zero",
    "no_unknown_or_open_orders",
)

MAX_OPS_HARD = 2                      # üçüncü işlem DAIMA RED (sıkılasabilir, gevşetilemez)
DEFAULT_TTL_S = 3600.0


class GateError(RuntimeError):
    """Depo hatası/bozukluk → çağıran fail-closed RED vermeli."""


def _h(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    ops_completed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
INSERT OR IGNORE INTO gate_state (id, ops_completed, updated_at) VALUES (1, 0, 0);
CREATE TABLE IF NOT EXISTS gate_completions (
    seq INTEGER PRIMARY KEY,
    recorded_at REAL NOT NULL,
    granted_by TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_approvals (
    seq INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('GRANTED','CONSUMED','REVOKED','EXPIRED')),
    consumed_at REAL
);
"""


class ApprovalGate:
    def __init__(self, db_file: str, max_ops: int = MAX_OPS_HARD,
                 approval_ttl_s: float = DEFAULT_TTL_S, now=None):
        self._db = str(db_file)
        self._max_ops = min(int(max_ops), MAX_OPS_HARD)   # yalnız sıkılaşır
        self._ttl = float(approval_ttl_s)
        self._now = now or time.time

    # ------------------------------------------------------------------ infra
    def _conn(self) -> sqlite3.Connection:
        try:
            con = sqlite3.connect(self._db, timeout=10, isolation_level=None)
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as e:
            raise GateError(f"onay deposuna erişilemedi: {e}") from e
        try:
            con.executescript(_SCHEMA)
        except sqlite3.Error as e:
            con.close()
            raise GateError(f"onay deposu başlatılamadı: {e}") from e
        return con

    # ------------------------------------------------------------------ read
    def _ops_completed(self, con) -> int:
        row = con.execute("SELECT ops_completed FROM gate_state WHERE id=1")\
                 .fetchone()
        if row is None:
            raise GateError("gate_state satırı yok (bozuk depo)")
        return int(row[0])

    def state(self) -> dict:
        con = self._conn()
        try:
            ops = self._ops_completed(con)
            ap = None
            if ops == 1 and self._max_ops >= 2:
                ap = con.execute(
                    "SELECT status, expires_at FROM gate_approvals WHERE seq=2")\
                      .fetchone()
            if ops >= self._max_ops:
                st = "ALL_OPS_DONE_THIRD_ALWAYS_RED"
            elif ops == 0:
                st = "FIRST_OP_INCOMPLETE"
            elif ap is None:
                st = "AWAITING_OPERATOR_APPROVAL"
            elif ap[0] == "GRANTED" and float(ap[1]) > self._now():
                st = "APPROVED_AWAITING_USE"
            else:
                st = "APPROVAL_" + str(ap[0])
            return {"ops_completed": ops, "max_ops": self._max_ops,
                    "state": st}
        finally:
            con.close()

    def ops_completed(self) -> int:
        con = self._conn()
        try:
            return self._ops_completed(con)
        finally:
            con.close()

    # ------------------------------------------------------------- completion
    def record_completion(self, seq: int, evidence: dict, granted_by: str) -> bool:
        """İşlem tamamlanma kanıtı — ALTI kanıtın TAMAMI True olmalı ve
        yalnız sıradaki işlem (ops_completed+1) için kaydedilebilir."""
        if not granted_by:
            return False
        if not isinstance(evidence, dict) \
                or set(evidence) != set(SIX_EVIDENCE) \
                or not all(evidence[k] is True for k in SIX_EVIDENCE):
            return False
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                ops = self._ops_completed(con)
                if seq != ops + 1 or seq > self._max_ops:
                    con.execute("ROLLBACK")
                    return False
                now = self._now()
                con.execute(
                    "INSERT INTO gate_completions (seq, recorded_at, granted_by,"
                    " evidence_json) VALUES (?,?,?,?)",
                    (seq, now, granted_by, json.dumps(evidence, sort_keys=True)))
                con.execute("UPDATE gate_state SET ops_completed=?, updated_at=?"
                            " WHERE id=1", (seq, now))
                con.execute("COMMIT")
                return True
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise GateError(f"tamamlanma kaydı başarısız: {e}") from e
        finally:
            con.close()

    # ------------------------------------------------------------------ grant
    def grant(self, granted_by: str, seq: int = 2) -> str | None:
        """Tek kullanımlık onay üretir; token METNİNİ yalnız bir kez döner
        (depoda hash var). Yalnız 2. işlem için ve ilk işlem KAYITLI
        tamamlanmışsa verilir; aksi halde None."""
        if not granted_by:
            return None
        if seq != 2 or self._max_ops < 2:
            return None
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                ops = self._ops_completed(con)
                if ops < 1:
                    con.execute("ROLLBACK")
                    return None            # İLK İŞLEM TAMAMLANMADAN onay YOK
                if ops >= self._max_ops:
                    con.execute("ROLLBACK")
                    return None            # bütçe/limit dolmuş — üçüncü yok
                target = seq
                exists = con.execute(
                    "SELECT 1 FROM gate_approvals WHERE seq=?",
                    (target,)).fetchone()
                if exists is not None:
                    con.execute("ROLLBACK")
                    return None            # seq başına TEK grant (defans)
                tok = secrets.token_hex(16)
                now = self._now()
                con.execute(
                    "INSERT INTO gate_approvals (seq, token_hash, granted_by,"
                    " granted_at, expires_at, status) VALUES (?,?,?,?,?,'GRANTED')",
                    (target, _h(tok), granted_by, now, now + self._ttl))
                con.execute("COMMIT")
                return tok
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise GateError(f"onay üretimi başarısız: {e}") from e
        finally:
            con.close()

    def revoke(self, seq: int) -> bool:
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute(
                    "UPDATE gate_approvals SET status='REVOKED' "
                    "WHERE seq=? AND status='GRANTED'", (seq,))
                con.execute("COMMIT")
                return cur.rowcount == 1
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise GateError(f"onay iptali başarısız: {e}") from e
        finally:
            con.close()

    # ---------------------------------------------------------------- consume
    def consume(self, seq: int, token) -> tuple[bool, str]:
        """Tek kullanımlık tüketim. Her başarısızlık nedeni RED'dir."""
        if not isinstance(token, str) or not token:
            return False, "ONAY-YOK: token verilmedi"
        if seq >= MAX_OPS_HARD + 1:
            return False, "UCUNCU-ISLEM-YASAK: üçüncü işlem daima reddedilir"
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute(
                    "SELECT token_hash, expires_at, status FROM gate_approvals"
                    " WHERE seq=?", (seq,)).fetchone()
                if row is None:
                    con.execute("ROLLBACK")
                    return False, "ONAY-YOK: bu seq için kayıtlı onay yok"
                try:
                    thash, exp, status = str(row[0]), float(row[1]), str(row[2])
                except (TypeError, ValueError):
                    con.execute("ROLLBACK")
                    return False, "ONAY-BOZUK: kayıt çözümlenemedi (fail-closed)"
                if status == "CONSUMED":
                    con.execute("ROLLBACK")
                    return False, "ONAY-KULLANILDI: tek kullanımlık tüketildi"
                if status == "EXPIRED":
                    con.execute("ROLLBACK")
                    return False, "ONAY-SURESI-DOLDU"
                if status == "REVOKED":
                    con.execute("ROLLBACK")
                    return False, "ONAY-IPTAL-EDILMIS"
                if status != "GRANTED":
                    con.execute("ROLLBACK")
                    return False, "ONAY-BOZUK: beklenmeyen durum (fail-closed)"
                now = self._now()
                if now >= exp:
                    con.execute("UPDATE gate_approvals SET status='EXPIRED' "
                                "WHERE seq=?", (seq,))
                    con.execute("COMMIT")
                    return False, "ONAY-SURESI-DOLDU"
                if not secrets.compare_digest(thash, _h(token)):
                    con.execute("ROLLBACK")
                    return False, "ONAY-GECERSIZ: token eşleşmedi"
                # ONAY ANI: bu işlemin ÖNCESİ tamamlanmış olmalı — seq. işlemin
                # onayı için seq-1 tamamlanma şarttır (kapanış DEĞİL, tüketim
                # kapıyı açar; kapanış sonrası ikinci onay ayrı seq'dedir).
                if self._ops_completed(con) < seq - 1:
                    con.execute("ROLLBACK")
                    return False, "ILK-ISLEM-TAMAMLANMADI: onay kullanılamaz"
                con.execute("UPDATE gate_approvals SET status='CONSUMED',"
                            " consumed_at=? WHERE seq=?", (now, seq))
                con.execute("COMMIT")
                return True, "ONAY-TUKETILDI"
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise GateError(f"onay tüketimi başarısız: {e}") from e
        finally:
            con.close()

    def entry_consume(self, seq: int, token) -> tuple[bool, str]:
        """Giriş yolu için tek kapı çağrısı.

        seq = bu girişin sıra numarası (bütçe reserve SONRASI trades_used).
        1. işlem: runbook onayı + bütçe kapısı yeter (onay tokenı GEREKMEZ).
        2. işlem: kalıcı tek kullanımlık onay ŞART.
        3+: daima RED — ayrıca consume içinde de kilitli."""
        if seq <= 0:
            return False, "SIRA-BELIRSIZ: işlem sırası çözümlenemedi"
        if seq == 1:
            return True, "BIRINCI-ISLEM: bütçe kapısı uygulandı"
        if seq >= MAX_OPS_HARD + 1:
            return False, "UCUNCU-ISLEM-YASAK: üçüncü işlem daima reddedilir"
        ok, why = self.consume(2, token)
        return ok, why

    # ---- BLOK12 devam-E §3: OKUMA-SADECE doğrulama (gönderim ÖNCESİ) ----
    # Eski sıralama token'ı POST'tan ÖNCE tüketiyordu; teyitli emir
    # başarısızlığında bütçe iade olsa da token ölü kalır ve ikinci işlem
    # kalıcı kilitlenirdi. Yeni sözleşme: önce verify (yazma YOK), emir
    # doğrulanınca consume. Kalıntu risk dürüstçe belgeli: POST↔consume crash
    # penceresi (sınırlar: tek-süreç kilidi + 2 işlem hard-cap + pending slot).

    def verify(self, seq: int, token) -> tuple[bool, str]:
        """consume'un YAZMASIZ aynası — aynı RED nedenleri, tek fark: hiçbir
        satır güncellenmez (süre-dolması bile işaretlenmez; consume anında
        yeniden ve atomik doğrulanır). Başarılı dönüş, aynı token ile
        consume'un da başarılı olacağını taahhüt ETMEZ (yarış: revoke/expiry/
        consume-arası); consumption anı için consume otoritedir."""
        if not isinstance(token, str) or not token:
            return False, "ONAY-YOK: token verilmedi"
        if seq >= MAX_OPS_HARD + 1:
            return False, "UCUNCU-ISLEM-YASAK: üçüncü işlem daima reddedilir"
        con = self._conn()
        try:
            row = con.execute(
                "SELECT token_hash, expires_at, status FROM gate_approvals"
                " WHERE seq=?", (seq,)).fetchone()
            if row is None:
                return False, "ONAY-YOK: bu seq için kayıtlı onay yok"
            try:
                thash, exp, status = str(row[0]), float(row[1]), str(row[2])
            except (TypeError, ValueError):
                return False, "ONAY-BOZUK: kayıt çözümlenemedi (fail-closed)"
            if status == "CONSUMED":
                return False, "ONAY-KULLANILDI: tek kullanımlık tüketildi"
            if status == "EXPIRED":
                return False, "ONAY-SURESI-DOLDU"
            if status == "REVOKED":
                return False, "ONAY-IPTAL-EDILMIS"
            if status != "GRANTED":
                return False, "ONAY-BOZUK: beklenmeyen durum (fail-closed)"
            if self._now() >= exp:
                return False, "ONAY-SURESI-DOLDU"
            if not secrets.compare_digest(thash, _h(token)):
                return False, "ONAY-GECERSIZ: token eşleşmedi"
            if self._ops_completed(con) < seq - 1:
                return False, "ILK-ISLEM-TAMAMLANMADI: onay kullanılamaz"
            return True, "ONAY-DOGRULANDI (henüz tüketilmedi)"
        except sqlite3.Error as e:
            raise GateError(f"onay doğrulaması başarısız: {e}") from e
        finally:
            con.close()

    def entry_verify(self, seq: int, token) -> tuple[bool, str]:
        """entry_consume'un okuma-sadece aynası (gönderim öncesi kapı)."""
        if seq <= 0:
            return False, "SIRA-BELIRSIZ: işlem sırası çözümlenemedi"
        if seq == 1:
            return True, "BIRINCI-ISLEM: bütçe kapısı uygulandı"
        if seq >= MAX_OPS_HARD + 1:
            return False, "UCUNCU-ISLEM-YASAK: üçüncü işlem daima reddedilir"
        return self.verify(seq, token)
