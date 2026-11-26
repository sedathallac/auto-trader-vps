"""canary_risk.py — BLOK27.3: bağımsız, merkezî Canary RiskManager.

Operatör onaylı KESİN sınırlar (2026-11-23 görev metni; BLOK29.1 güncellemesi
2026-11-25: pozisyon başına notional 50 → 100 USDT, diğerleri DEĞİŞMEDİ):
  MAX_OPEN_POSITIONS      = 2      # eş zamanlı açık pozisyon
  MAX_POSITION_NOTIONAL   = 100.0  # USDT, pozisyon NOTIONAL değeri (marjin DEĞİL)
  MAX_TOTAL_NOTIONAL      = 100.0  # USDT, toplam açık notional
  MAX_POSITION_NET_LOSS   = 5.0    # -5 USDT: o pozisyon için yeni risk YOK + güvenli çıkış
  MAX_CANARY_NET_LOSS     = 10.0   # -10 USDT: CANARY_RISK_HALTED (yalnız operatör sıfırlar)

Net zarar tanımı (görev §2): gerçekleşmiş PnL + gerçekleşmemiş PnL (Mark) +
açılış/kapanış komisyonları + funding ödemeleri.

Davranış kuralları (görev §2, 1-10):
  1. üçüncü pozisyon KESİNLİKLE reddedilir
  2-3. notional sınırları atomik ön-kontrolle uygulanır
  4. pozisyon net zararı -5'e ulaşınca o pozisyon için yeni risk durur,
     SAFE_EXIT bayrağı kalıcı olur (risk AZALTAN kapanışa izin verilir)
  5. toplam -10 → CANARY_RISK_HALTED; yeni pozisyon AÇILMAZ
  6. HALT otomatik sıfırlanamaz; yalnız operatör_onayı() ile
  7. restart/crash/DB hatası sayaçları SIFIRLAYAMAZ (durum SQLite'ta kalıcı)
  8. risk hesabı yapılamıyorsa emir YOK (fail-closed)
  9. risk azaltan kapanış, yeni pozisyon yasağından BAĞIMSIZ olarak devam eder
  10. kontroller emir öncesi ATOMİK olarak tekrar uygulanır (kilitle)

Mark fiyatı: REST_SAMPLED_MARK_PRICE (premiumIndex 5s örnekleme); stale
eşik MARK_STALE_S saniyedir — aşılırsa yeni emir reddedilir (fail-closed).
"""
import json
import sqlite3
import threading
import time
from typing import Optional

MAX_OPEN_POSITIONS = 2
MAX_POSITION_NOTIONAL = 100.0       # BLOK29.1 (2026-11-25): 50 → 100
MAX_TOTAL_NOTIONAL = 100.0
MAX_POSITION_NET_LOSS = 5.0
MAX_CANARY_NET_LOSS = 10.0
MARK_STALE_S = 30.0          # REST örneklemesi için önceden belgelenmiş eşik

STATUS_ACTIVE = "ACTIVE"
STATUS_HALTED = "CANARY_RISK_HALTED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS canary_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    pos_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, direction INTEGER NOT NULL,
    units REAL NOT NULL, entry_px REAL NOT NULL, entry_ts INTEGER NOT NULL,
    entry_comm REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',       -- OPEN / CLOSED / SAFE_EXIT
    exit_px REAL, exit_ts INTEGER, exit_comm REAL,
    realized REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS funding (
    pos_id INTEGER NOT NULL, ts INTEGER NOT NULL, amount REAL NOT NULL,
    PRIMARY KEY (pos_id, ts));
CREATE TABLE IF NOT EXISTS marks (
    symbol TEXT PRIMARY KEY, mark_px REAL NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS risk_events (
    ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""


class OrderRejected(Exception):
    """Fail-closed ret — reason alanı insan-okur."""


class RiskManager:
    """Merkezî risk kapısı. Tüm emirler check_order()'dan geçer.

    thread-safe: tek process içinde eşzamanlı iki emir yarışı kilitle
    çözülür; kalıcılık SQLite (WAL) ile sağlanır.
    """

    def __init__(self, db_file: str):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_file, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        if self._get_state("status") is None:
            self._set_state("status", STATUS_ACTIVE)
        # B.1 İŞ 4: hesap-seviyesi CROSS risk kapısı (canary_account_risk_gate.
        # AccountRiskGate). None → kapı bağlı DEĞİL → check_order davranışı
        # DEĞİŞMEZ (geriye uyumlu). Runner kurulumda bağlar (operatör konfigi
        # yoksa kapı güvenli varsayılan BLOCK üretir).
        self.account_gate = None

    # ---- kalıcı durum -----------------------------------------------------
    def _get_state(self, key):
        # okuma yolları da kilide bağlı: risk+strateji thread'leri aynı
        # sqlite bağlantısını paylaşır (§4/§8 — eşzamanlı execute imleç
        # bozulması üretir); RLock reentrant → iç içe çağrı güvenli
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM canary_state WHERE key=?",
                (key,)).fetchone()
            return row[0] if row else None

    def _set_state(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO canary_state VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE "
                            "SET value=excluded.value",
                            (key, value))
            self.db.commit()

    @property
    def status(self):
        return self._get_state("status")

    def operator_reset(self):
        """HALT yalnızca AÇIK operatör onayıyla sıfırlanır (kural 6).
        Kod bu metodu otomatik ÇAĞIRMAZ; operatör aracıdır."""
        with self._lock:
            self._set_state("status", STATUS_ACTIVE)
            self._event("OPERATOR_RESET", "HALT operatör onayıyla sıfırlandı")

    def _event(self, kind, detail):
        with self._lock:
            self.db.execute("INSERT INTO risk_events VALUES (?,?,?)",
                            (int(time.time() * 1000), kind, detail))
            self.db.commit()

    # ---- piyasa verisi ----------------------------------------------------
    def update_mark(self, symbol, mark_px, ts_ms):
        with self._lock:
            self.db.execute(
                "INSERT INTO marks VALUES (?,?,?) ON CONFLICT(symbol) "
                "DO UPDATE SET mark_px=excluded.mark_px, ts=excluded.ts",
                (symbol, mark_px, ts_ms))
            self.db.commit()

    def _mark(self, symbol, now_ms):
        """Mark yoksa/stale ise None — hesap YAPILAMIYOR (fail-closed)."""
        with self._lock:
            row = self.db.execute(
                "SELECT mark_px, ts FROM marks WHERE symbol=?",
                (symbol,)).fetchone()
            if row is None:
                return None
            if (now_ms - row[1]) / 1000 > MARK_STALE_S:
                return None
            return row[0]

    # ---- pozisyon muhasebesi ----------------------------------------------
    def open_position(self, symbol, direction, notional, entry_px, now_ms,
                      entry_comm=0.0):
        """check_order ile onaylanmış emrin sonucunu kaydeder."""
        with self._lock:
            units = notional / entry_px
            cur = self.db.execute(
                "INSERT INTO positions (symbol, direction, units, entry_px,"
                " entry_ts, entry_comm) VALUES (?,?,?,?,?,?)",
                (symbol, direction, units, entry_px, now_ms, entry_comm))
            self.db.commit()
            return cur.lastrowid

    def close_position(self, pos_id, exit_px, now_ms, exit_comm=0.0):
        """Risk AZALTAN işlem — HALT'ta da çağrılabilir (kural 9)."""
        with self._lock:
            pos = self._pos(pos_id)
            realized = (pos["direction"] * pos["units"] *
                        (exit_px - pos["entry_px"]))
            self.db.execute(
                "UPDATE positions SET status='CLOSED', exit_px=?, exit_ts=?,"
                " exit_comm=?, realized=? WHERE pos_id=?",
                (exit_px, now_ms, exit_comm, realized, pos_id))
            self.db.commit()
            self.enforce(now_ms)
            return realized

    def note_funding(self, pos_id, ts_ms, amount):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO funding VALUES (?,?,?)",
                            (pos_id, ts_ms, amount))
            self.db.commit()

    def _pos(self, pos_id):
        with self._lock:
            self.db.row_factory = sqlite3.Row
            return dict(self.db.execute(
                "SELECT * FROM positions WHERE pos_id=?",
                (pos_id,)).fetchone())

    def open_positions(self):
        with self._lock:
            self.db.row_factory = sqlite3.Row
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM positions WHERE status IN "
                "('OPEN','SAFE_EXIT')")]

    # ---- zarar hesabı (görev §2: tamamı dahil) ------------------------------
    def position_net_loss(self, pos, now_ms):
        """Pozisyon net zararı (pozitif = zarar):
        realized(0 iken yok) + uPnL(Mark) + komisyonlar + funding.
        Mark yoksa/stale ise None → hesap YAPILAMIYOR."""
        with self._lock:
            loss = 0.0
            if pos["status"] == "CLOSED":
                loss += -pos["realized"]
                loss += pos["entry_comm"] + (pos["exit_comm"] or 0)
            else:
                mark = self._mark(pos["symbol"], now_ms)
                if mark is None:
                    return None
                loss += -(pos["direction"] * pos["units"] *
                          (mark - pos["entry_px"]))
                loss += pos["entry_comm"]
            fund = self.db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM funding "
                "WHERE pos_id=?", (pos["pos_id"],)).fetchone()[0]
            loss += -fund      # funding ödemesi (negatif cf = ödenmiş)
            return loss

    def total_net_loss(self, now_ms):
        """Toplam canary net zararı: kapalı realized + açık uPnL + tüm
        komisyon + tüm funding. Herhangi biri hesaplanamıyorsa None
        (fail-closed)."""
        with self._lock:
            total = 0.0
            self.db.row_factory = sqlite3.Row
            for pos in self.db.execute("SELECT * FROM positions"):
                l = self.position_net_loss(dict(pos), now_ms)
                if l is None:
                    return None
                total += l
            return total

    def position_notional(self, pos, now_ms):
        """Güncel fiyatla notional (görev §5: güncel fiyattan yeniden)."""
        if pos["status"] == "CLOSED":
            return 0.0
        mark = self._mark(pos["symbol"], now_ms)
        if mark is None:
            return None
        return abs(pos["units"]) * mark

    def total_notional(self, now_ms):
        total = 0.0
        for pos in self.open_positions():
            n = self.position_notional(pos, now_ms)
            if n is None:
                return None
            total += n
        return total

    # ---- ATOMİK emir-öncesi kontrol (kural 10) ------------------------------
    def check_order(self, symbol, direction, notional, now_ms=None):
        """Yeni/açıcı emir için atomik kapı. İzin → dict; ret → OrderRejected.
        Risk AZALTAN (kapanış) işlemler bu kapıdan GEÇMEZ (kural 9):
        close_position doğrudan çağrılır.

        B.1 İŞ 4: account_gate bağlıysa hesap-seviyesi CROSS kontrolü
        KİLİT DIŞINDA önce yapılır (ağ çağrısı rm kilidini bloklamaz;
        RLock reentrant olduğundan submit_order içinden de güvenli).
        Kapı BLOCK → OrderRejected(AG_* nedenleri) — denetim izi
        try_entry tarafından live_orders.jsonl'ye yazılır."""
        now_ms = now_ms or int(time.time() * 1000)
        if self.account_gate is not None:
            decision = self.account_gate.evaluate(
                symbol=symbol, direction=direction, notional=notional,
                rm=self, now_ms=now_ms)
            if not decision.passed:
                raise OrderRejected(
                    "ACCOUNT_GATE " + " | ".join(decision.reasons))
        with self._lock:
            # 8: hesap yapılamıyorsa emir YOK — her None rettir
            total_loss = self.total_net_loss(now_ms)
            if total_loss is None:
                raise OrderRejected("FAIL_CLOSED: zarar hesabı yapılamıyor "
                                    "(mark yok/stale veya veri eksik)")
            total_notional = self.total_notional(now_ms)
            if total_notional is None:
                raise OrderRejected("FAIL_CLOSED: notional hesaplanamıyor "
                                    "(mark yok/stale)")
            # 5: toplam zarar sınırı
            if total_loss >= MAX_CANARY_NET_LOSS:
                self._halt(f"toplam net zarar {total_loss:.4f} >= "
                           f"{MAX_CANARY_NET_LOSS}")
                raise OrderRejected("CANARY_RISK_HALTED: toplam net zarar "
                                    f"-{total_loss:.4f} USDT (sınır -10)")
            if self.status == STATUS_HALTED:
                raise OrderRejected("CANARY_RISK_HALTED (kalıcı; yalnız "
                                    "operatör onayıyla sıfırlanır)")
            # 1: pozisyon sayısı
            opens = self.open_positions()
            existing = [p for p in opens if p["symbol"] == symbol]
            if not existing and len(opens) >= MAX_OPEN_POSITIONS:
                raise OrderRejected(f"pozisyon sınırı: {len(opens)} açık, "
                                    "üçüncü pozisyona izin YOK")
            # 2: pozisyon notional (100 USDT notional — marjin değil)
            if notional > MAX_POSITION_NOTIONAL + 1e-9:
                raise OrderRejected(f"pozisyon notional {notional:.2f} > "
                                    f"{MAX_POSITION_NOTIONAL} USDT")
            # 3: toplam notional — güncel fiyatla (yeni/açıcı notional
            # her durumda eklenir: yeni sembol VE mevcut pozisyon büyütme)
            proj = total_notional + notional
            if proj > MAX_TOTAL_NOTIONAL + 1e-9:
                raise OrderRejected(f"toplam notional {proj:.2f} > "
                                    f"{MAX_TOTAL_NOTIONAL} USDT")
            # 4: hedef pozisyonun kendi zarar sınırı (yeni risk alma yasağı)
            for p in existing:
                l = self.position_net_loss(p, now_ms)
                if l is None:
                    raise OrderRejected("FAIL_CLOSED: mevcut pozisyon zararı "
                                        "hesaplanamıyor")
                if l >= MAX_POSITION_NET_LOSS:
                    raise OrderRejected(f"{symbol} pozisyon net zararı "
                                        f"-{l:.4f} ≥ -5: bu pozisyon için "
                                        "yeni risk YOK (SAFE_EXIT)")
            # mark freshness bu sembol için de şart (fill fiyatı doğrulaması)
            if self._mark(symbol, now_ms) is None:
                raise OrderRejected(f"FAIL_CLOSED: {symbol} mark yok/stale "
                                    f">{MARK_STALE_S:.0f}s")
            return {"allowed": True, "open_positions": len(opens),
                    "total_notional": round(total_notional, 4),
                    "total_net_loss": round(total_loss, 4)}

    def submit_order(self, symbol, direction, notional, entry_px, now_ms=None,
                     entry_comm=0.0):
        """ATOMİK emir gönderimi (kural 10): check_order + pozisyon yazımı
        TEK kilitle. İki eşzamanlı submit yarışı üçüncü pozisyon
        oluşturamaz ve toplam notionalı aşamaz. Ret → OrderRejected."""
        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            self.check_order(symbol, direction, notional, now_ms)
            pos_id = self.open_position(symbol, direction, notional,
                                        entry_px, now_ms, entry_comm)
            self._event("ORDER_ACCEPTED",
                        f"{symbol} dir={direction} notional={notional:.2f} "
                        f"pos_id={pos_id}")
            return pos_id

    def _halt(self, reason):
        """Kural 5: kalıcı HALT — otomatik sıfırlama YOK."""
        self._set_state("status", STATUS_HALTED)
        self._set_state("halted_at", str(int(time.time() * 1000)))
        self._set_state("halt_reason", reason)
        self._event("CANARY_RISK_HALTED", reason)

    # ---- sürekli uygulama (döngüden çağrılır) --------------------------------
    def enforce(self, now_ms=None):
        """Pozisyon -5 ve toplam -10 sınırlarını izler; SAFE_EXIT işaretler
        ve gerekirse HALT eder. Risk azaltan kapanışa ENGEL OLMAZ."""
        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            actions = []
            for pos in self.open_positions():
                l = self.position_net_loss(pos, now_ms)
                if l is None:
                    actions.append({"pos_id": pos["pos_id"], "action":
                                    "FAIL_CLOSED_NO_DATA"})
                    continue
                if l >= MAX_POSITION_NET_LOSS and pos["status"] == "OPEN":
                    self.db.execute("UPDATE positions SET status='SAFE_EXIT' "
                                    "WHERE pos_id=?", (pos["pos_id"],))
                    self.db.commit()
                    self._event("POSITION_LOSS_LIMIT",
                                f"pos {pos['pos_id']} {pos['symbol']} net "
                                f"zarar -{l:.4f} → SAFE_EXIT (yeni risk yok, "
                                "güvenli çıkış uygulanır)")
                    actions.append({"pos_id": pos["pos_id"], "action":
                                    "SAFE_EXIT"})
            total = self.total_net_loss(now_ms)
            if total is not None and total >= MAX_CANARY_NET_LOSS and \
                    self.status != STATUS_HALTED:
                self._halt(f"toplam net zarar {total:.4f}")
                actions.append({"action": "CANARY_RISK_HALTED"})
            return actions

    # ---- mutabakat -----------------------------------------------------------
    def reconcile(self, live_positions, now_ms=None):
        """Salt-okunur canlı durumla karşılaştırma (shadow/reconcile aracı).
        live_positions: [{symbol, positionAmt, entryPrice, ...}] (signed GET).
        Dönüş: drift raporu — YENİ EMİR BU FONKSİYONLA AÇILMAZ."""
        now_ms = now_ms or int(time.time() * 1000)
        with self._lock:
            ours = {p["symbol"]: p for p in self.open_positions()}
        theirs = {p["symbol"]: p for p in live_positions
                  if float(p.get("positionAmt", 0)) != 0}
        drift = []
        for s in set(ours) | set(theirs):
            if s in ours and s not in theirs:
                drift.append({"symbol": s, "kind": "MISSING_ON_EXCHANGE"})
            elif s not in ours and s in theirs:
                drift.append({"symbol": s, "kind": "UNOWNED_POSITION"})
            else:
                if ours[s]["direction"] * ours[s]["units"] != \
                        float(theirs[s]["positionAmt"]):
                    drift.append({"symbol": s, "kind": "SIZE_MISMATCH"})
        return {"drift": drift, "ok": not drift}
