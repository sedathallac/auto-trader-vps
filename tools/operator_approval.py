#!/usr/bin/env python3
"""BLOK12 §1 — Operatör onay CLI (offline; emir GÖNDERMEZ).

Ortam:
  VALIDATION_APPROVAL_DB  onay deposu (varsayılan: ./validation_gate.db)
  VALIDATION_APPROVAL_TTL onay saniyesi (varsayılan 3600)

Komutlar:
  status
  complete --seq 1 --by AD --attest KANIT [--attest ...]   (6 kanıt zorunlu)
  grant    --by AD                                          (token BİR KEZ basılır)
  revoke   --seq 2

Not: --attest beyanı OPERATÖRÜN uzlaştırma kontrolüdür; altı kanıt
(position_flat, fills_reconciled, close_order_terminal, sl_algo_verified,
ledger_exchange_diff_zero, no_unknown_or_open_orders) birlikte verilmeden
tamamlanma kaydedilmez. Token terminal'e yalnız grant anında yazılır —
paylaşımlı ekran/kayıtta paylaşılmamalıdır.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from operator_approval import ApprovalGate, GateError, SIX_EVIDENCE  # noqa: E402


def _gate():
    db = os.getenv("VALIDATION_APPROVAL_DB", "validation_gate.db")
    ttl = float(os.getenv("VALIDATION_APPROVAL_TTL", "3600") or 3600)
    return ApprovalGate(db, approval_ttl_s=ttl)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("complete")
    c.add_argument("--seq", type=int, required=True)
    c.add_argument("--by", required=True)
    c.add_argument("--attest", action="append", default=[],
                   choices=list(SIX_EVIDENCE))
    g = sub.add_parser("grant")
    g.add_argument("--by", required=True)
    g.add_argument("--seq", type=int, default=2)
    r = sub.add_parser("revoke")
    r.add_argument("--seq", type=int, default=2)
    a = ap.parse_args(argv)
    try:
        gate = _gate()
        if a.cmd == "status":
            print(gate.state())
            return 0
        if a.cmd == "complete":
            ev = {k: (k in set(a.attest)) for k in SIX_EVIDENCE}
            missing = [k for k in SIX_EVIDENCE if k not in set(a.attest)]
            if missing:
                print(f"RED: eksik kanıt beyanı: {missing}")
                return 2
            ok = gate.record_completion(a.seq, ev, granted_by=a.by)
            print("KAYDEDILDI" if ok else "RED: sıradışı seq veya beyan eksik")
            return 0 if ok else 2
        if a.cmd == "grant":
            tok = gate.grant(granted_by=a.by, seq=a.seq)
            if tok is None:
                print("RED: onay üretilemedi (ilk işlem tamamlanmamış / "
                      "tek grant hakkı kullanılmış / limit dolu)")
                return 2
            print("ONAY VERILDI — token (BİR KEZ gösterilir, tek kullanımlık):")
            print(tok)
            print("Bot tarafı: OPERATOR_APPROVAL_TOKEN_FILE ile işaretlenen "
                  "0600 dosyaya yazın (chmod 600).")
            return 0
        if a.cmd == "revoke":
            print("IPTAL" if gate.revoke(a.seq) else "RED: iptal edilecek GRANTED onay yok")
            return 0
    except GateError as e:
        print(f"GATE-HATASI (fail-closed): {e}")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
