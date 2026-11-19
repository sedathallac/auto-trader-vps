"""FAZ14x-2 — ROLLBACK / KARIŞIK-SÜRÜM generation regresyonları (R serisi).

Soru (denetim görevi, madde 3): "Eski sürümün DB'ye yazdığı kayıtların gen=0
olarak kalabilmesi. Eski sürümle rollback sonrası yeniden yeni sürüm devreye
girdiğinde, eski sürümün oluşturduğu veya yeniden aktive ettiği bir epoch'un
yeni snapshot tarafından yanlışlıkla kapatılması mümkün mü?"

Somut yanıt bu dosyada TEST ile kanıtlanır. Eski sürüm (01229ce) gen'den
habersizdir: INSERT'i DEFAULT 0 alır, supersede/mark_placed UPDATE'leri gen
kolonuna DOKUNMAZ (git show 01229ce:sl_placement.py — SQL'ler aşağıdaki
V1LegacyWriter'a kelimesi kelimesine kopyalandı). Kuşak kuralı "her epoch
değişimi gen'i artırır" YALNIZ yeni sürümün kodunda uygulanıyor; bu yüzden
eski ve yeni sürüm AYNI DB'ye aynı anda yazarsa (rollback/deploy karışık
penceresi — 3 servis aynı risk_ledger.db'yi paylaşır) bayat snapshot yeni
epoch'u kapatabilir. Çözüm DB-seviyesinde olmalıdır: SQLite TRIGGER'ları
kod sürümünden bağımsız çalıştığı için eski sürümün yazmaları da gen'i
ilerletir (kod geri alınsa bile şemada kalırlar).

Yalnızca GEÇİCİ test DB'leri kullanılır; canlı DB'ye dokunulmaz.
"""
import os
import sys
import sqlite3
import tempfile
import shutil
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sl_placement as sp


def _now():
    return datetime.now(timezone.utc).isoformat()


class V1LegacyWriter:
    """01229ce (rollback hedefi) sl_placement.py yazma yollarının BİREBİR
    kopyası — gen'den habersiz. SQL metinleri `git show 01229ce:sl_placement.py`
    satırlarından kopyalandı; ekleme/çıkarma yok."""

    def __init__(self, db_file):
        self.db_file = str(db_file)

    def _connect(self):
        conn = sqlite3.connect(self.db_file, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def acquire_insert(self, symbol, token):
        """v1 acquire — satır YOKKEN (01229ce:76)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO sl_placements (symbol, token, state, created_at, "
                "updated_at) VALUES (?, ?, 'PENDING', ?, ?)",
                (symbol, token, _now(), _now()))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def acquire_supersede(self, symbol, token):
        """v1 acquire — farklı token supersede (01229ce:85). gen DOKUNMAZ."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE sl_placements SET token=?, state='PENDING', created_at=?, "
                "updated_at=? WHERE symbol=?", (token, _now(), _now(), symbol))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def mark_placed(self, symbol, token):
        """v1 mark_placed (01229ce:103). gen DOKUNMAZ."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE sl_placements SET state='PLACED', updated_at=? "
                "WHERE symbol=? AND token=?", (_now(), symbol, token))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def mark_closed_flat(self, symbol, token):
        """v1 mark_closed_flat (01229ce:123)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE sl_placements SET state='CLOSED', updated_at=? "
                "WHERE symbol=? AND token=? AND state IN ('PENDING','PLACED')",
                (_now(), symbol, token))
            conn.execute("COMMIT")
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()

    def release(self, symbol, token):
        """v1 release — DELETE (01229ce:143)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sl_placements WHERE symbol=? AND token=?",
                         (symbol, token))
            conn.execute("COMMIT")
        finally:
            conn.close()


class RollbackGenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="faz14x_rb_")
        self.db = os.path.join(self.tmp, "risk_ledger_test.db")
        self.store = sp.SlPlacementStore(self.db)
        self.store.init_schema()
        self.v1 = V1LegacyWriter(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # R-a — KARIŞIK SÜRÜM, sweep yolu (token'lı): v2 snapshot sayacını
    # exchange isteğinden ÖNCE yakalar; ROLLBACK'TEKİ v1 bu anda yeni epoch
    # açar (supersede+placed, gen'i oynatmaz) → v2'nin bayat flat kanıtı bu
    # YENİ epoch'u kapatmamalıdır.
    def test_stale_snapshot_cannot_close_v1_reactivated_epoch_sweep(self):
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        snap = self.store.current_generation()   # ← exchange isteğinden önce
        # ...bayat snapshot DÖNENE KADAR rollback'teki v1 yeni epoch açar:
        self.v1.acquire_supersede("BTCUSDT", "LONG:200")
        self.v1.mark_placed("BTCUSDT", "LONG:200")
        # v2 sweep: bayat kanıt + eski snap ile kapatmayı DENEYEemez:
        self.assertFalse(
            self.store.mark_closed_flat("BTCUSDT", "LONG:200", max_gen=snap),
            "R-a: bayat snapshot, v1'in yeniden aktive ettiği epoch'u kapattı")
        row = self.store.get("BTCUSDT")
        self.assertEqual((row["token"], row["state"]), ("LONG:200", "PLACED"),
                         "R-a: v1 epoch'u bayat kanıtla bozuldu")

    # R-b — Aynı yarış, executor yolu (token=None, salt generation kilidi).
    def test_stale_snapshot_cannot_close_v1_reactivated_epoch_executor(self):
        self.store.acquire("BTCUSDT", "LONG:100")
        self.store.mark_placed("BTCUSDT", "LONG:100")
        snap = self.store.current_generation()
        self.v1.acquire_supersede("BTCUSDT", "SHORT:1")
        self.v1.mark_placed("BTCUSDT", "SHORT:1")
        self.assertFalse(
            self.store.mark_closed_flat("BTCUSDT", token=None, max_gen=snap),
            "R-b: executor yolu bayat snap ile v1 epoch'unu kapattı")
        self.assertEqual(self.store.get("BTCUSDT")["state"], "PLACED")

    # R-c — v1'in epoch değiştirme yazmaları (supersede / mark_placed) gen'i
    # İLERLETMELİ; aksi halde "gen <= snap ⇒ dönem değişmedi" çıkarımı
    # karışık sürümde yanlışlanır. (DB-seviyesi tetikçi zorunluluğu.)
    def test_v1_epoch_transitions_bump_generation(self):
        self.store.acquire("BTCUSDT", "LONG:100")
        g_before = self.store.current_generation()
        self.v1.acquire_supersede("BTCUSDT", "LONG:200")
        self.assertGreater(self.store.current_generation(), g_before,
                           "R-c: v1 supersede generation'ı ilerletmedi")
        g_mid = self.store.current_generation()
        self.v1.mark_placed("BTCUSDT", "LONG:200")
        self.assertGreater(self.store.current_generation(), g_mid,
                           "R-c: v1 mark_placed generation'ı ilerletmedi")
        self.assertGreater(self.store.get("BTCUSDT")["gen"], g_before,
                           "R-c: v1 sonrası satır gen'i eski snapshot'ı geçmedi")

    # R-d — v1'in YENİ satır INSERT'i (DEFAULT 0 tuzağı): INSERT'ten önce
    # yakalanan snap, INSERT'ten sonraki bayat kanıtla bu satırı kapatamamalı;
    # bunun için satır gen'i snap'ten büyük olmalı (gen=0 kabul EDİLEMEZ).
    def test_v1_new_row_gets_generation_above_prior_snapshot(self):
        snap = self.store.current_generation()
        self.v1.acquire_insert("ETHUSDT", "LONG:50")
        self.v1.mark_placed("ETHUSDT", "LONG:50")
        self.assertFalse(
            self.store.mark_closed_flat("ETHUSDT", "LONG:50", max_gen=snap),
            "R-d: gen=0 sanılan v1 satırı bayat snapshot kanıtıyla kapandı")
        self.assertEqual(self.store.get("ETHUSDT")["state"], "PLACED")

    # R-e — AŞIRI ENGELLEME YOK: rollback sonrası yepyeni snapshot (snap, v1
    # yazmalarından SONRA alındı) v1'in PLACED epoch'unu düzgün kapatabilir;
    # PENDING kuralı ise bozulmadan durur.
    def test_fresh_evidence_still_closes_v1_epoch(self):
        self.v1.acquire_insert("ETHUSDT", "LONG:50")
        self.v1.mark_placed("ETHUSDT", "LONG:50")
        snap = self.store.current_generation()   # taze snapshot (v1'den sonra)
        self.assertTrue(
            self.store.mark_closed_flat("ETHUSDT", "LONG:50", max_gen=snap),
            "R-e: taze kanıt v1 epoch'unu kapatamadı (aşırı engelleme)")
        self.assertEqual(self.store.get("ETHUSDT")["state"], "CLOSED")
        self.v1.acquire_insert("SOLUSDT", "LONG:7")
        snap2 = self.store.current_generation()
        self.assertFalse(
            self.store.mark_closed_flat("SOLUSDT", "LONG:7", max_gen=snap2),
            "R-e: PENDING, taze kanıtla bile kapanamaz kuralı bozuldu")

    # R-f — Şema uyumu: v2 migration'ından SONRA v1'in TÜM SQL yolları
    # hatasız çalışır (kolon sayısı/gate uyumu) — rollback güvenliği.
    def test_v1_all_write_paths_work_after_migration(self):
        self.v1.acquire_insert("BTCUSDT", "LONG:1")
        self.v1.acquire_supersede("BTCUSDT", "LONG:2")
        self.v1.mark_placed("BTCUSDT", "LONG:2")
        self.assertTrue(self.v1.mark_closed_flat("BTCUSDT", "LONG:2"))
        self.v1.acquire_insert("XRPUSDT", "SHORT:3")
        self.v1.release("XRPUSDT", "SHORT:3")
        self.assertIsNone(self.store.get("XRPUSDT"))
        self.assertEqual(self.store.get("BTCUSDT")["state"], "CLOSED")

    # R-g — Tetikçili migration idempotent: init_schema tekrarları sayaçı
    # sıfırlamaz, veri bozmaz, çift tetikçi hatası vermez.
    def test_migrate_with_triggers_stays_idempotent(self):
        self.store.acquire("BTCUSDT", "LONG:1")
        g = self.store.current_generation()
        self.store.init_schema()
        self.store.init_schema()
        self.assertEqual(self.store.current_generation(), g,
                         "R-g: tekrar migration sayacı değiştirdi")
        row = self.store.get("BTCUSDT")
        self.assertEqual((row["token"], row["state"]), ("LONG:1", "PENDING"))

    # R-h — Meta onarımı (additive): sl_placement_meta silinirse/geri kalırsa
    # bir sonraki yazma migration'ı sayacı satır MAX(gen)'ine çeker; asla
    # geriye almaz. (Bozuk/yedekten dönmüş DB senaryosu.)
    def test_meta_repair_never_goes_below_row_max(self):
        self.store.acquire("BTCUSDT", "LONG:1")
        self.store.acquire("BTCUSDT", "LONG:2")   # supersede: iki epoch geçişi
        row_max = self.store.get("BTCUSDT")["gen"]
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            conn.execute("UPDATE sl_placement_meta SET gen = 0 WHERE id=1")
        finally:
            conn.close()
        self.store.acquire("ETHUSDT", "LONG:9")   # yeni yazma → _migrate onarır
        self.assertGreater(self.store.current_generation(), row_max,
                           "R-h: meta onarımı sayacı satır MAX(gen)'ine çekmedi "
                           "(monotonluk bozuk)")


if __name__ == "__main__":
    unittest.main()
