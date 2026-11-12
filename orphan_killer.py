#!/usr/bin/env python3
"""orphan_killer.py v2 — Akıllı yetim SL avcısı
Sadece daha önce işlem açtığımız sembolleri tarar.
"""
import os, sys, time, json, logging
from pathlib import Path
sys.path.insert(0, os.path.expanduser('~'))

LOG = Path.home() / "orphan_killer.log"
TRACKED = Path.home() / ".tracked_symbols.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("orphan_killer")


def load_tracked():
    if TRACKED.exists():
        try:
            return set(json.loads(TRACKED.read_text()))
        except Exception:
            return set()
    return set()


def save_tracked(syms):
    tmp = TRACKED.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(syms), indent=2))
    tmp.replace(TRACKED)


HEARTBEAT_FILE = Path.home() / ".auto_trader.heartbeat"
MAX_HEARTBEAT_AGE = 180  # 3 dakika


def check_heartbeat():
    """Ana botun kalp atışını kontrol et"""
    if not HEARTBEAT_FILE.exists():
        log.critical("🚨 HEARTBEAT DOSYASI YOK! Ana bot hiç çalışmamış olabilir")
        return False
    try:
        from datetime import datetime
        last_beat = datetime.fromisoformat(HEARTBEAT_FILE.read_text().strip())
        age = (datetime.now() - last_beat).total_seconds()
        if age > MAX_HEARTBEAT_AGE:
            log.critical(f"🚨 HEARTBEAT ESKİ! {age:.0f}sn (limit: {MAX_HEARTBEAT_AGE}sn) — ana bot çökmüş olabilir")
            return False
        log.info(f"✅ Heartbeat OK ({age:.0f}sn önce)")
        return True
    except Exception as e:
        log.error(f"Heartbeat okunamadı: {e}")
        return False


def scan_and_kill(binance=None):
    check_heartbeat()
    from auto_trader import BinanceAPI
    if binance is None:
        bk = os.environ.get("BINANCE_KEY")
        bs = os.environ.get("BINANCE_SECRET")
        if not bk or not bs:
            log.error("BINANCE_KEY/SECRET yok")
            return
        binance = BinanceAPI(bk, bs)
    b = binance

    # 1. Açık pozisyonlar
    positions = b.position_risk()
    if not isinstance(positions, list):
        log.error(f"positionRisk hata: {positions}")
        return
    open_syms = {p["symbol"] for p in positions if float(p["positionAmt"]) != 0}

    # K-5 Görev B: deterministik dedupe BACKSTOP. K-3'ün keep-max-algoId temizliği
    # (_dedupe_algo_sl) burada, her açık pozisyon için, ensure_sl_coverage'dan
    # BAĞIMSIZ çalışır. Böylece ensure_sl_coverage değişse veya bir istisna atsa
    # bile (aşağıda yakalanıyor) çift SL bu 60sn'lik döngüde yine de temizlenir.
    # Dedupe deterministik ve idempotenttir: en büyük algoId her zaman hayatta kalır,
    # bu yüzden eşzamanlı iki process asla iki kopyayı birden iptal edemez (korumasız
    # bırakmaz). Order temizliği ile pozisyon koruması ayrı endişelerdir (inc8).
    if open_syms:
        try:
            from auto_trader import _dedupe_algo_sl
            for _sym in open_syms:
                _dedupe_algo_sl(b, _sym)
        except Exception as e:
            log.exception(f"K-5 dedupe backstop: {e}")

    # ROOT-CAUSE FIX (inc8): SL protection for OPEN positions runs INDEPENDENT of
    # the orphan list. Previously ensure_sl_coverage was only reached at the end,
    # so an empty to_check (open positions but no closed-symbol orphans) returned
    # early and skipped SL coverage for open positions entirely (the SOLUSDT
    # class). Order cleanup and position protection are now separate concerns.
    if open_syms:
        try:
            from auto_trader import ensure_sl_coverage
            ensure_sl_coverage(b)
        except Exception as e:
            log.exception(f"ensure_sl_coverage: {e}")

    # 2. Tracked listesi güncelle (açık olanları ekle)
    tracked = load_tracked() | open_syms
    save_tracked(tracked)

    # 3. Sadece tracked olanları tara
    to_check = [s for s in tracked if s not in open_syms]
    if not to_check:
        log.info(f"✅ Kontrol edilecek yok (açık: {len(open_syms)})")
        return

    log.info(f"🔍 Açık: {len(open_syms)} | Kontrol: {len(to_check)} ({to_check})")

    orphans = []
    for sym in to_check:
        try:
            orders = b.open_algo_orders_for(sym)
            for o in orders:
                orphans.append((sym, o["algoId"], o.get("triggerPrice")))
        except Exception as e:
            log.debug(f"  {sym}: {e}")
        time.sleep(0.2)

    if not orphans:
        log.info(f"✅ Yetim yok")
        # Kontrol edilenleri tracked'den çıkar (temizse)
        for sym in to_check:
            tracked.discard(sym)
        save_tracked(tracked)
        return

    log.warning(f"🚨 {len(orphans)} yetim silinecek")
    for sym, aid, trig in orphans:
        try:
            r = b.cancel_algo_order(aid)
            if r.get("code") == "200":
                log.info(f"  ✅ {sym} iptal (algoId={aid}, trigger={trig})")
                tracked.discard(sym)
            else:
                log.warning(f"  ⚠️  {sym}: {r}")
        except Exception as e:
            log.error(f"  ❌ {sym}: {e}")
    save_tracked(tracked)
    # NOTE: SL protection for open positions already ran above (inc8), independent
    # of the orphan list, so it is not repeated here.


def main():
    if "--once" in sys.argv:
        scan_and_kill()
        return
    log.info("🚀 Orphan killer başladı (60sn döngü, akıllı takip)")
    while True:
        try:
            scan_and_kill()
        except Exception as e:
            log.exception(f"loop hata: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
