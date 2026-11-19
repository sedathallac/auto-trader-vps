"""sl_placement.py — cross-process SL-placement idempotency (K-3).

Both run_once (900s) and orphan_killer (60s) call ensure_sl_coverage, and open_coin
also places an SL. Without coordination, two PROCESSES can each see a just-opened
position as unprotected (the freshly placed SL not yet visible / a transient ABSENT)
and BOTH call place_algo_sl -> a DUPLICATE SL on one position (root cause of the
observed BTC/SOL double-SL race, orphan_killer.py:84-85 + run_once).

This makes SL placement idempotent across processes using the SAME shared SQLite
ledger as PositionReserver / CloseIntentStore (risk_ledger.db, WAL + BEGIN IMMEDIATE):
only the intent OWNER places an SL for a given (symbol, position-epoch); any other
process/path sees it owned and SKIPS. The epoch `token` ('<side>:<entryPrice>') comes
from exchange truth so a NEW position period supersedes a prior PLACED intent.

States: PENDING (owner acquired, about to place) -> PLACED (SL confirmed placed). On a
placement FAILURE the owner release()s so reality is reflected (the existing -2021 /
OPEN_UNPROTECTED alarm then governs; no silent block). FAZ14x adds CLOSED: a terminal
state set when sync sees exchange-flat evidence for that epoch (mark_closed_flat), so
settled epochs stop showing as live PLACED rows; rows are never deleted.

FAZ14x-2 (generation): exchange-flat CLOSED is ordered by a MONOTONIC INTEGER
`gen` maintained INSIDE SQLite (sl_placement_meta counter + per-row gen), not by
the wall clock — `updated_at` is bookkeeping only. `updated_at` is wall-clock
derived and can move BACKWARDS (NTP step-back), letting a stale snapshot pass a
timestamp predicate; an integer counter bumped atomically under BEGIN IMMEDIATE
cannot. Snapshot ordering rule: readers capture the counter value BEFORE issuing
the positionRisk request; a CLOSED UPDATE with max_gen matches only rows whose
last epoch transition happened at/before that counter value. A new epoch created
after the snapshot (even one that reuses the same symbol+token, or is stamped
with a rewound clock) carries a strictly larger gen and cannot be closed by the
older evidence. Schema migration for pre-gen DBs is idempotent and additive.
"""
import sqlite3
from datetime import datetime, timezone
from typing import Optional

ACQUIRED = "ACQUIRED"   # this caller owns the placement; it may place exactly one SL
BUSY = "BUSY"           # another process owns an in-flight / completed placement -> skip

_ACTIVE = ("PENDING", "PLACED")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlPlacementStore:
    """Atomic, cross-process SL-placement intent backed by the shared SQLite ledger."""

    def __init__(self, db_file):
        self.db_file = str(db_file)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        # Determinizm: generation tetikçilerinin iç UPDATE'i yeniden tetiklen-
        # memeli (SQLite varsayılanı OFF; açıkça set edilir — eski sürüm
        # bağlantıları da varsayılan OFF ile aynı davranır).
        conn.execute("PRAGMA recursive_triggers=OFF")
        return conn

    # ---------------------------------------------------------------- schema
    def _migrate(self, conn) -> None:
        """Idempotent, saat-bağımsız generation şema geçişi. Her YAZMA
        transaction'ının (BEGIN IMMEDIATE) başında ve init_schema'da çağrılır.

        Eski (FAZ14x öncesi/01229ce) DB'lerde `gen` kolonu ve meta tablosu
        yoktur: kolon sabit defaultla ADD edilir (SQLite izin verir), meta
        mevcut MAX(gen)'den tohumlanır. Yalnız EKLEME yapar: veri silmez,
        sayacı SIFIRLAMAZ; tekrar çalıştırmalar no-op'tur (M-b). Şema geçişi
        yerel/test DB'lerinde kendiliğinden çalışır; canlı DB'ye bu görev
        kapsamında DOKUNULMAZ.
        """
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sl_placements)")]
        if cols and "gen" not in cols:
            conn.execute(
                "ALTER TABLE sl_placements ADD COLUMN gen INTEGER NOT NULL DEFAULT 0")
        # FAZ18 (1.3) additive: exchange-kimlik kolonları. PLACED artık YALNIZ
        # ack ile değil, saklanmış algoId/clientAlgoId ile doğrulanabilir;
        # restart re-map bu alanlarla yapılır. Eski satırlar NULL kalır.
        for _c in ("algo_id", "client_algo_id", "working_type"):
            if cols and _c not in cols:
                conn.execute(
                    f"ALTER TABLE sl_placements ADD COLUMN {_c} TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sl_placement_meta ("
            "id INTEGER PRIMARY KEY CHECK (id=1), gen INTEGER NOT NULL DEFAULT 0)")
        if cols:
            # meta yoksa mevcut en yüksek epoch gen'inden tohumlanır (monotonluk
            # korunur); varsa INSERT OR IGNORE ile dokunulmaz.
            conn.execute(
                "INSERT OR IGNORE INTO sl_placement_meta (id, gen) "
                "SELECT 1, COALESCE(MAX(gen), 0) FROM sl_placements")
            # Additive onarım: yedekten dönüş/el-le müdahale ile meta geriye
            # düşürülmüşse sayacı satır MAX(gen)'ine ÇEKER; asla geri almaz.
            conn.execute(
                "UPDATE sl_placement_meta SET gen = "
                "  (SELECT MAX(gen) FROM sl_placements) "
                "WHERE id = 1 AND gen < "
                "  COALESCE((SELECT MAX(gen) FROM sl_placements), gen)")
            # Generation değişmezliği ("her epoch değişimi gen'i artırır")
            # KOD SÜRÜMÜNDEN BAĞIMSIZ zorunlu kılınır: rollback hedefi
            # (01229ce) gen'den habersizdir ve karışık sürüm penceresinde
            # (deploy/rollback sırası, aynı DB'yi paylaşan 3 servis) eski
            # sürümün supersede/mark_placed/INSERT yazmaları gen'i oynatmaz;
            # bayat snapshot böyle bir epoch'u yanlışlıkla kapatabilirdi
            # (kanıt: tests/test_faz14x_rollback_gen.py R-a..R-d). Tetikçiler
            # ŞEMADA yaşar, kodda değil — hangi sürüm yazarsa yazsın ateş
            # alır; rollback sonrası da aktif kalırlar. İç satır UPDATE'i
            # recursive_triggers=OFF (SQLite varsayılanı; _connect'te de
            # açıkça set edilir) sayesinde ikinci kez tetiklenmez.
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_sl_placements_gen_ai
                AFTER INSERT ON sl_placements
                BEGIN
                    INSERT OR IGNORE INTO sl_placement_meta (id, gen)
                        VALUES (1, 0);
                    UPDATE sl_placement_meta SET gen = gen + 1 WHERE id = 1;
                    UPDATE sl_placements
                       SET gen = (SELECT gen FROM sl_placement_meta WHERE id = 1)
                     WHERE rowid = NEW.rowid;
                END""")
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_sl_placements_gen_au
                AFTER UPDATE OF token, state ON sl_placements
                BEGIN
                    INSERT OR IGNORE INTO sl_placement_meta (id, gen)
                        VALUES (1, 0);
                    UPDATE sl_placement_meta SET gen = gen + 1 WHERE id = 1;
                    UPDATE sl_placements
                       SET gen = (SELECT gen FROM sl_placement_meta WHERE id = 1)
                     WHERE rowid = NEW.rowid;
                END""")
        else:
            conn.execute(
                "INSERT OR IGNORE INTO sl_placement_meta (id, gen) VALUES (1, 0)")

    def _next_gen(self, conn) -> int:
        """Yazma-tx içinde atomik sayaç artışı (BEGIN IMMEDIATE altında)."""
        conn.execute("UPDATE sl_placement_meta SET gen = gen + 1 WHERE id=1")
        return conn.execute(
            "SELECT gen FROM sl_placement_meta WHERE id=1").fetchone()[0]

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sl_placements (
                    symbol TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    gen INTEGER NOT NULL DEFAULT 0
                )
            """)
            self._migrate(conn)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def current_generation(self) -> Optional[int]:
        """Snapshot-anında SL generation sayacını okur. Flat-kanıtı üreten her
        akış, exchange isteğini GÖNDERMEDEN ÖNCE bu değeri yakalar ve CLOSED
        UPDATE'ini max_gen olarak bununla koşullar. Okunamazsa None döner;
        çağıran fail-closed kalmalıdır (kanıt sıralanamazsa kapatma yok)."""
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT gen FROM sl_placement_meta WHERE id=1").fetchone()
            return int(r["gen"]) if r is not None else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    # ----------------------------------------------------------------- write
    def acquire(self, symbol: str, token: str) -> str:
        """Atomically become the SL placer for (symbol, token), or report BUSY.
        One BEGIN IMMEDIATE tx so concurrent processes serialise: at most one ACQUIRED
        per (symbol, token) epoch. Her ACQUIRED (yeni ya da supersede-yeniden-doğan
        dönem) yeni bir gen alır — BUSY'de sayaç artmaz."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn)
            row = conn.execute(
                "SELECT token, state FROM sl_placements WHERE symbol=?", (symbol,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sl_placements (symbol, token, state, created_at, "
                    "updated_at, gen) VALUES (?, ?, 'PENDING', ?, ?, ?)",
                    (symbol, token, _now(), _now(), self._next_gen(conn)))
                conn.execute("COMMIT")
                return ACQUIRED
            if row["token"] == token and row["state"] in _ACTIVE:
                conn.execute("ROLLBACK")
                return BUSY  # another process is placing, or already placed, this epoch
            # different token (a new position period) -> supersede the old intent
            conn.execute(
                "UPDATE sl_placements SET token=?, state='PENDING', created_at=?, "
                "updated_at=?, gen=? WHERE symbol=?",
                (token, _now(), _now(), self._next_gen(conn), symbol))
            conn.execute("COMMIT")
            return ACQUIRED
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def mark_placed(self, symbol: str, token: str, algo_id=None,
                    client_algo_id=None, working_type=None) -> None:
        """Epoch güvenliği için kritik durum geçişi: yeni gen alır. Böylece
        PENDING iken alınmış bir flat-snapshot, PLACED olduktan SONRA o satırı
        eski kanıtla kapayamaz.

        FAZ18 (1.3): exchange-doğrulamalı PLACED — çağıran YALNIZ ack (algoId)
        sonrası gelmeli; algoId/clientAlgoId/workingType satıra KALICI olarak
        yazılır (restart re-map ve watchdog denetimi bu kimlikle yapılır)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn)
            conn.execute(
                "UPDATE sl_placements SET state='PLACED', updated_at=?, gen=?, "
                "algo_id=COALESCE(?, algo_id), "
                "client_algo_id=COALESCE(?, client_algo_id), "
                "working_type=COALESCE(?, working_type) "
                "WHERE symbol=? AND token=?",
                (_now(), self._next_gen(conn),
                 str(algo_id) if algo_id is not None else None,
                 client_algo_id, working_type, symbol, token))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def remap_algo_ids(self, symbol: str, token: str, algo_id,
                       client_algo_id=None) -> bool:
        """FAZ18 (1.3) idempotent re-map: PLACED satırının exchange-emir
        kimliğini güncel gerçeğe taşır (ör. restart sonrası GET
        algoOrder?clientAlgoId ile bulunan yeni/gerçek algoId). Epoch değişmez
        — GEN ALZAMAZ (tetikçi yalnız token/state列'larını izler; bu UPDATE
        gen'i oynatmaz, kapanış kanıtlarını geçersiz kılmaz)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn)
            cur = conn.execute(
                "UPDATE sl_placements SET updated_at=?, "
                "algo_id=COALESCE(?, algo_id), "
                "client_algo_id=COALESCE(?, client_algo_id) "
                "WHERE symbol=? AND token=? AND state='PLACED'",
                (_now(), str(algo_id) if algo_id is not None else None,
                 client_algo_id, symbol, token))
            conn.execute("COMMIT")
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()

    def mark_closed_flat(self, symbol: str, token: Optional[str] = None,
                         max_gen: Optional[int] = None) -> bool:
        """FAZ14x: exchange-flat kanıtı görülen epoch'u CLOSED terminal duruma
        alır. Kayıt SİLİNMEZ (denetim tarihi korunur); Rowcount>0 ise True.

        FAZ14x-2 nesil-2 katı sözleşme (koşullu UPDATE, aynı BEGIN IMMEDIATE):
          * Yalnız state='PLACED' kapanır — PENDING bu kapıdan HİÇBİR koşulda
            geçemez (madde 8 DB-seviyesinde zorunlu kılındı).
          * token verilirse (symbol, token) eşleşmesi aranır. token=None,
            sembolün PLACED epoch'una anahtarlar: executor-akışı güncel
            satırın token'ını yeniden okuyup bayat kapanışa ATFETMEZ; epoch
            eşleşmesi GENERATION koşuluyla yapılır.
          * max_gen = flat-kanıtı snapshot'ından ÖNCE yakalanan generation.
            Verildiğinde yalnız satır gen <= max_gen kapanır; snapshot'tan
            sonra acquire/mark_placed olan ya da aynı token'la yeniden doğan
            epoch gen'i > max_gen taşır ve bu kanıtla KAPATILAMAZ. Duvar
            saatinden bağımsızdır: NTP step-back koşulu atlatamaz (test N1/G-e).
          * max_gen=None → generation koşulu yok; yalnız birim testler
            kullanır, flat-kanıtı yolları None GEÇEMEZ.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn)
            sql = ("UPDATE sl_placements SET state='CLOSED', updated_at=?, gen=? "
                   "WHERE symbol=? AND state='PLACED'")
            params = [_now(), self._next_gen(conn), symbol]
            if token is not None:
                sql += " AND token=?"
                params.append(token)
            if max_gen is not None:
                sql += " AND gen <= ?"
                params.append(max_gen)
            cur = conn.execute(sql, params)
            conn.execute("COMMIT")
            return (cur.rowcount or 0) > 0
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def release(self, symbol: str, token: str) -> None:
        """Free the intent after a placement FAILURE so a later attempt can re-acquire
        (the -2021 / OPEN_UNPROTECTED alarm governs the real exchange state).
        Sayaç GERİ ALINMAZ: silinen dönemin gen'i teleffuz edilmiştir."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sl_placements WHERE symbol=? AND token=?", (symbol, token))
            conn.execute("COMMIT")
        finally:
            conn.close()

    # ------------------------------------------------------------------ read
    def get(self, symbol: str) -> Optional[dict]:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT symbol, token, state, gen, algo_id, client_algo_id, "
                "working_type FROM sl_placements WHERE symbol=?",
                (symbol,)
            ).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def list_active(self):
        """FAZ14x-2: aktif (PENDING/PLACED) tüm epoch satırları — sync'in
        exchange-flat sweep'i için. CLOSED/SİLİNEN asla dönmez."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT symbol, token, state, gen, algo_id, client_algo_id, "
                "working_type FROM sl_placements "
                "WHERE state IN ('PENDING','PLACED')"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
