"""canary_control.py — BLOK30 operatör kontrol aracı: ENTRY PAUSE yönetimi.

HALT'tan TAMAMEN BAĞIMSIZ, kalıcı bir "yeni giriş duraklatma" kontrolü.
Global HALT state machine'e, risk limitlerine, koruma emirlerine ve açık
pozisyonlara DOKUNMAZ. Borsa emir/iptal API'sini HİÇ ÇAĞIRMAZ.

Kullanım:
  python canary_control.py status  [--db /var/lib/futures-canary/canary_live.db]
  python canary_control.py pause   --reason "..." [--db PATH] [--timeout S]
  python canary_control.py resume  --confirm [--db PATH] [--reason "..."]

Davranış sözleşmesi:
  status : salt-okunur (DB mode=ro); PAUSE + HALT + risk durumu + bekleyen
           giriş emirlerini (INTENT/SUBMITTED/UNKNOWN) gösterir.
  pause  : devam eden bir giriş zinciri varsa sonucunu BEKLER (ortak
           entry-gate flock kilidi); tamamlandıktan sonra durumu yazar.
           Bekleyen/UNKNOWN giriş emri varsa açıkça raporlar.
  resume : yalnızca PAUSE bayrağını kaldırır (--confirm şart). Global HALT
           sıfırlanmaz, operator_reset/operator_release ÇAĞRILMAZ. Diğer
           güvenlik blokları (HALT, mutabakat, feed, koruma...) aynen
           geçerliliğini sürdürür.

Çıkış kodları: 0 başarı; 2 giriş kapısı kilidi timeout'u (durum DEĞİŞMEDİ);
3 kullanım hatası.
"""
import argparse
import os
import socket
import sys

from canary_live import (EntryGateBusy, entry_gate_lock_path,
                         read_entry_pause, set_entry_pause)

DEFAULT_DB = os.environ.get(
    "CANARY_DB", "/var/lib/futures-canary/canary_live.db")

EXIT_OK, EXIT_GATE_BUSY, EXIT_USAGE = 0, 2, 3


def _who():
    return f"cli:pid={os.getpid()}@{socket.gethostname()}"


def _fmt_ts(ms):
    if not ms:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(
        int(ms) / 1000, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


def _print_state(st):
    print(f"DB                       : {st['db']}"
          f" (exists={st['exists']})")
    print(f"entry-gate kilit dosyası : {entry_gate_lock_path(st['db'])}"
          f"  ← uygulama ve bu araç aynı kilidi paylaşır")
    print(f"entry_paused             : "
          f"{'1 (YENİ GİRİŞLER DURDURULDU)' if st['entry_paused'] else '0'}")
    if st.get("entry_pause_last_action"):
        print(f"son işlem                : {st['entry_pause_last_action']}"
              f"  @ {_fmt_ts(st.get('entry_pause_ts'))}"
              f"  by={st.get('entry_pause_by')}")
    if st.get("entry_pause_reason"):
        print(f"neden                    : {st['entry_pause_reason']}")
    print(f"halt_state (global HALT) : {st.get('halt_state')}")
    print(f"rm_status (risk yönetici): {st.get('rm_status')}")
    print(f"worker_pid (çalışan uygulama): {st.get('worker_pid')}")
    pend = st.get("pending_entries") or []
    print(f"bekleyen giriş emirleri  : {len(pend)} "
          "(INTENT/SUBMITTED/UNKNOWN)")
    for p in pend:
        print(f"  - {p['client_id']} {p['symbol']} {p['status']} "
              f"@ {_fmt_ts(p['updated_ts'])}")


def cmd_status(args):
    st = read_entry_pause(args.db)
    _print_state(st)
    print("RESULT: OK (salt-okunur — borsa API'si çağrılmadı)")
    return EXIT_OK


def cmd_pause(args):
    st = read_entry_pause(args.db)          # öncelikli görünüm (bilgi)
    print("PAUSE isteği: yeni girişler durdurulacak. Mevcut pozisyonlar, "
          "koruma emirleri, risk döngüsü ve çıkışlar DOKUNULMAZ.")
    if st.get("pending_entries"):
        print("UYARI: bekleyen/sonucu bilinmeyen giriş emri VAR "
              "(duraklatma bunları iptal ETMEZ, çözümleri risk döngüsünde "
              "izlenir):")
        for p in st["pending_entries"]:
            print(f"  - {p['client_id']} {p['symbol']} {p['status']}")
    try:
        # Giriş kapısı kilidi: devam eden giriş zinciri varsa sonucunu
        # BEKLER; bu satır dönmeden PAUSE 'etkinleşti' DENMEZ.
        r = set_entry_pause(args.db, True, args.reason, _who(),
                            timeout_s=args.timeout)
    except EntryGateBusy as e:
        print(f"HATA: {e}")
        print("RESULT: FAILED — giriş zinciri devam ediyor, PAUSE "
              "yazılmadı (durum DEĞİŞMEDİ). Tekrar deneyin.")
        return EXIT_GATE_BUSY
    print(f"önceki durum             : entry_paused={r['previous']}")
    print(f"yazıldı                  : entry_paused=1 @ "
          f"{_fmt_ts(r['ts'])} by={r['by']}")
    print(f"audit                    : risk_events → ENTRY_PAUSED "
          f"(reason={r['reason']!r})")
    print("RESULT: OK — yeni giriş MARKET emirleri artık engellendi "
          "(kalıcı; restart'a dayanıklı). Resume: "
          "python canary_control.py resume --confirm")
    return EXIT_OK


def cmd_resume(args):
    if not args.confirm:
        print("HATA: resume açık operatör onayı gerektirir — --confirm "
              "bayrağı ekleyin.")
        print("RESULT: FAILED — PAUSE durumu değişmedi.")
        return EXIT_USAGE
    print("RESUME isteği: yalnızca ENTRY PAUSE bayrağı kaldırılacak. "
          "Global HALT sıfırlanMAZ; operator_reset/operator_release "
          "çağrılMAZ; diğer güvenlik blokları geçerliliğini korur.")
    try:
        r = set_entry_pause(args.db, False,
                            args.reason or "operatör resume onayı",
                            _who(), timeout_s=args.timeout)
    except EntryGateBusy as e:
        print(f"HATA: {e}")
        print("RESULT: FAILED — giriş zinciri devam ediyor, RESUME "
              "yazılmadı (durum DEĞİŞMEDİ). Tekrar deneyin.")
        return EXIT_GATE_BUSY
    print(f"önceki durum             : entry_paused={r['previous']}")
    print(f"yazıldı                  : entry_paused=0 @ "
          f"{_fmt_ts(r['ts'])} by={r['by']}")
    print(f"audit                    : risk_events → ENTRY_RESUMED")
    print("NOT: PAUSE kalktı, ancak global HALT/mutabakat/feed/koruma "
          "blokları varsa yeni giriş yine engellenir (bilinçli tasarım).")
    print("RESULT: OK — yeni girişler normal güvenlik kontrollerine tabi "
          "olarak değerlendirilecek.")
    return EXIT_OK


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB,
                        help=f"canary DB yolu (varsayılan: {DEFAULT_DB} "
                             f"veya $CANARY_DB)")
    ap = argparse.ArgumentParser(
        prog="canary_control.py", parents=[common],
        description="Futures canary ENTRY PAUSE operatör aracı (BLOK30)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common],
                   help="salt-okunur durum görünümü")
    p_pause = sub.add_parser("pause", parents=[common],
                             help="yeni girişleri durdur")
    p_pause.add_argument("--reason", required=True,
                         help="PAUSE nedeni (audit kaydına yazılır)")
    p_pause.add_argument("--timeout", type=float, default=120.0,
                         help="giriş zinciri bekleme süresi sn "
                              "(default 120)")
    p_resume = sub.add_parser("resume", parents=[common],
                              help="PAUSE bayrağını kaldır")
    p_resume.add_argument("--confirm", action="store_true",
                          help="açık operatör onayı (ZORUNLU)")
    p_resume.add_argument("--reason", default=None,
                          help="RESUME nedeni (audit kaydına yazılır)")
    p_resume.add_argument("--timeout", type=float, default=120.0,
                          help="giriş zinciri bekleme süresi sn")
    args = ap.parse_args(argv)
    return {"status": cmd_status, "pause": cmd_pause,
            "resume": cmd_resume}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
