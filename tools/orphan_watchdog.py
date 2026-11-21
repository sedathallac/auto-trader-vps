#!/usr/bin/env python3
"""BLOK11 §8.3 — orphan_killer için SCHEDULABLE watchdog sarmalayıcısı.

Kök neden (bağımsız doğrulandı, BLOK11 §2): orphan_killer.main() bağımsız bir
60-sn döngüsüdür ANCAK bu makinede onu hiç kimse planlamıyordu: crontab boş,
systemd unit yok, repo içinden çağıran üretim yolu yok, süreç yok. Yani SL
backstop'u fiilen HİÇ koşmuyordu (P1, N1).

Bu araç kodu YENİDEN icat etmez — orphan_killer.scan_and_kill'ı TEK SEFER
çalıştırır ve İzleme için zorunlu sözleşmeyi ekler:
  • flock (NON-BLOCKING): çakışan ikinci tur asla koşmaz → exit 2;
  • anahtar kontrolü İSTEKTEN ÖNCE: BINANCE_KEY/SECRET yok → exit 2, istek YOK
    (orphan_killer yalnız log.error + return ediyordu — cron bunu "başarılı"
    sayardı; izleme körlüğü);
  • çıkış kodları: 0=tur tamam, 1=scan istisnası (loglanır), 2=yapılandırma
    hatası (kilit/anahtar — emir/istek yok).

Kurulum OPERATÖR kararıdır (BLOK11 §0): cron satırı ve systemd örnekleri
deploy/orphan-watchdog.* içinde; hiçbiri bu görevde kurulmadı.
"""
import argparse
import fcntl
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_guard

EXIT_OK = 0
EXIT_SCAN_ERROR = 1
EXIT_CONFIG = 2

log = logging.getLogger("orphan_watchdog")


def default_guard_file():
    """auto_trader ile AYNI dosyayı çözer: API_GUARD_STATE_FILE env'i,
    olmazsa FAZ9_STATE_DIR (veya ~) / api_guard.json."""
    env = os.environ.get("API_GUARD_STATE_FILE")
    if env:
        return env
    base = os.environ.get("FAZ9_STATE_DIR") or os.path.expanduser("~")
    return os.path.join(base, "api_guard.json")


def run_once(scanner, lock_path, keys_present, guard_file=None):
    """Bir tur. Dönüş: EXIT_* kodu. scanner() inject edilir (test + üretim).

    BLOK12 devam-F §3: turdan ÖNCE süreç-desti cooldown dosyası denetlenir —
    418/429 ban sürerken turlama Binance isteği GÖNDERİRSE ban uzardı;
    aktif cooldown → exit 2, tek istek bile YOK."""
    if not keys_present:
        log.error("BINANCE_KEY/BINANCE_SECRET yok — turlama REDDEDİLDİ "
                  "(exit 2); hiçbir borsa isteği yapılmadı")
        return EXIT_CONFIG
    gf = guard_file if guard_file is not None else default_guard_file()
    _ban = api_guard.file_blocked(gf)
    if _ban:
        log.error(f"🚫 rate-limit/IP-ban cooldown AKTİF ({_ban}) — tur "
                  "ATLANDI (exit 2); hiçbir borsa isteği yapılmadı")
        return EXIT_CONFIG
    lock_fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("Başka bir watchdog turu zaten koşuyor — bu tur "
                        "ATLANDI (çakışma yok; exit 2)")
            return EXIT_CONFIG
        try:
            scanner()
        except Exception:
            log.exception("scan_and_kill istisnası — sonraki tur devam edecek")
            return EXIT_SCAN_ERROR
        return EXIT_OK
    finally:
        lock_fh.close()  # flock kapalı dosya tanımıyla serbest kalır


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", default=os.path.join(
        os.path.expanduser("~"), ".orphan_watchdog.lock"))
    ap.add_argument("--once", action="store_true",
                    help="tek tur (cron modu; zaten varsayılan — bayrak uyumu)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr)

    keys_present = bool(os.environ.get("BINANCE_KEY")
                        and os.environ.get("BINANCE_SECRET"))

    def _scanner():
        # Geç import: config-hatalı koşumda auto_trader/orphan_killer import
        # yan etkileri (log dosyaları) bile tetiklenmez.
        import orphan_killer
        orphan_killer.scan_and_kill()

    return run_once(_scanner, args.lock, keys_present)


if __name__ == "__main__":
    sys.exit(main())
