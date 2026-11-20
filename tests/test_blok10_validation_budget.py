"""BLOK10 §3/§4 — Kontrollü canlı doğrulama bütçesi: birim testleri.

Kapsanan senaryolar (görev mektubu §4 ile eşleşir):
  S1  50.01 USDT notional reddi (işlem-başı tavan)
  S2/S3 üçüncü işlem reddi; kapanıştan sonra yeniden giriş HAK TÜKETİR
  S4  Retry aynı idempotency-anahtarı ile HAK YENİDEN TÜKETMEZ
  S5  ACK belirsiz → rezerv iade edilmez (korunur) — release YOLU KAPALI
  S7  İki worker yarışı: yalnız bir rezerv kazanır (atomik)
  S8  Restart kalıcılığı: sayaçlar yeni bağlantıda ayakta
  S10 DB hatası → BudgetError (çağıran fail-closed bloklar)
Toplam-tavan bağı Defense-in-depth: doğrular doğrudan Limits ile denenir
(ortam-override hard-cap'e asla üstünlük kuramaz — §3 bağımsız kontrol).
"""
import os
import sqlite3
import threading

import pytest

import validation_budget as vb


def _limits(trades=2, per=50.0, total=100.0):
    return vb.Limits(max_trades=trades, max_per_trade_usdt=per,
                     max_total_usdt=total)


def _budget(tmp_path, limits=None, name="v.db"):
    return vb.ValidationBudget(str(tmp_path / name), limits or _limits())


# ---------------------------------------------------------------- bayrak/limit
def test_bayrak_varsayilan_kapali(monkeypatch):
    monkeypatch.delenv("CONTROLLED_LIVE_VALIDATION", raising=False)
    cfg = vb.Config.from_env()
    assert cfg.enabled is False
    assert cfg.valid is True          # kapalıyken limit hatası aranamaz


def test_bayrak_acik_ama_limitler_gecersizse_fail_closed(monkeypatch):
    monkeypatch.setenv("CONTROLLED_LIVE_VALIDATION", "true")
    monkeypatch.delenv("MAX_VALIDATION_TRADES", raising=False)
    monkeypatch.delenv("MAX_VALIDATION_POSITION_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_VALIDATION_TOTAL_NOTIONAL_USDT", raising=False)
    cfg = vb.Config.from_env()
    assert cfg.enabled is True
    assert cfg.valid is True          # tanımsız → sert varsayılan 2/50/100
    assert (cfg.limits.max_trades, cfg.limits.max_per_trade_usdt,
            cfg.limits.max_total_usdt) == (2, 50.0, 100.0)


@pytest.mark.parametrize("env,val", [
    ("MAX_VALIDATION_TRADES", "0"),
    ("MAX_VALIDATION_TRADES", "-1"),
    ("MAX_VALIDATION_TRADES", "3"),      # hard cap 2 aşılamaz
    ("MAX_VALIDATION_TRADES", "abc"),
    ("MAX_VALIDATION_TRADES", ""),
    ("MAX_VALIDATION_POSITION_NOTIONAL_USDT", "50.01"),  # hard cap 50
    ("MAX_VALIDATION_POSITION_NOTIONAL_USDT", "0"),
    ("MAX_VALIDATION_POSITION_NOTIONAL_USDT", "x"),
    ("MAX_VALIDATION_TOTAL_NOTIONAL_USDT", "100.01"),
    ("MAX_VALIDATION_TOTAL_NOTIONAL_USDT", "-10"),
])
def test_gecersiz_override_fail_closed(monkeypatch, env, val):
    monkeypatch.setenv("CONTROLLED_LIVE_VALIDATION", "true")
    for k in ("MAX_VALIDATION_TRADES", "MAX_VALIDATION_POSITION_NOTIONAL_USDT",
              "MAX_VALIDATION_TOTAL_NOTIONAL_USDT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(env, val)
    cfg = vb.Config.from_env()
    assert cfg.enabled is True
    assert cfg.valid is False, f"{env}={val!r} fail-closed olmalıydı"


def test_siki_sayilabilir_gecersiz_acik_deger_kabul(monkeypatch):
    # Daha sıkı değerler meşrudur (sadece SIKILAŞTIRMA).
    monkeypatch.setenv("CONTROLLED_LIVE_VALIDATION", "on")
    monkeypatch.setenv("MAX_VALIDATION_TRADES", "1")
    monkeypatch.setenv("MAX_VALIDATION_POSITION_NOTIONAL_USDT", "25")
    monkeypatch.setenv("MAX_VALIDATION_TOTAL_NOTIONAL_USDT", "50")
    cfg = vb.Config.from_env()
    assert cfg.valid is True
    assert cfg.limits.max_trades == 1


# ---------------------------------------------------------------- S1: 50.01
def test_S1_islem_basi_tavan_50_01_reddi(tmp_path):
    b = _budget(tmp_path)
    r = b.reserve("BTCUSDT", 50.01, idem_key="k1")
    assert r.ok is False
    assert r.reason == "PER_TRADE_CAP"
    assert b.status()["trades_used"] == 0     # ret hak tüketmedi


def test_tam_50_kabul(tmp_path):
    b = _budget(tmp_path)
    r = b.reserve("BTCUSDT", 50.0, idem_key="k1")
    assert r.ok is True


# ------------------------------------------------------- S2/S3: işlem sayısı
def test_S2_ucuncu_islem_reddi(tmp_path):
    b = _budget(tmp_path)
    assert b.reserve("BTCUSDT", 40, idem_key="a").ok
    assert b.reserve("ETHUSDT", 40, idem_key="b").ok
    r = b.reserve("SOLUSDT", 10, idem_key="c")
    assert r.ok is False and r.reason == "TRADE_COUNT_CAP"


def test_S3_kapanis_sonrasi_yeniden_giris_hak_tuketir(tmp_path):
    b = _budget(tmp_path)
    r1 = b.reserve("BTCUSDT", 40, idem_key="a")
    b.confirm(r1.rid)                        # pozisyon açıldı (POST başarılı)
    r2 = b.reserve("ETHUSDT", 40, idem_key="b")
    b.confirm(r2.rid)
    # pozisyonlar kapansa bile (CONSUMED iade edilmez) 3. giriş reddedilir
    assert b.reserve("SOLUSDT", 5, idem_key="c").reason == "TRADE_COUNT_CAP"


# ------------------------------------------------------------- S4: idempotency
def test_S4_retry_hak_yeniden_tuketzmez(tmp_path):
    b = _budget(tmp_path)
    r1 = b.reserve("BTCUSDT", 40, idem_key="ayni")
    r2 = b.reserve("BTCUSDT", 40, idem_key="ayni")   # ACK şüpheli retry
    assert r1.ok and r2.ok
    assert r1.rid == r2.rid                  # aynı rezervasyon döner
    st = b.status()
    assert st["trades_used"] == 1            # TEK tüketim
    assert st["notional_used"] == 40.0


def test_idempotency_iade_edilmis_anahtar_yeniden_hak_tuketir(tmp_path):
    # Güvenlik yönü: RELEASED satırla aynı anahtarla gelen reserve YENİDEN
    # hak tüketir — Aksi hâlde (refund görmeyip ok=True dönmek) sonraki POST
    # bütçesiz emre dönüşürdü. CONSUMED/RESERVED ise tekrar tüketMEZ (S4).
    b = _budget(tmp_path)
    r1 = b.reserve("BTCUSDT", 40, idem_key="ayni")
    b.release(r1.rid, reason="confirmed_failure")
    assert b.status()["trades_used"] == 0
    r2 = b.reserve("BTCUSDT", 40, idem_key="ayni")
    assert r2.ok is True and r2.rid == r1.rid
    st = b.status()
    assert st["trades_used"] == 1            # yeniden tüketildi (güvenli taraf)
    assert st["notional_used"] == 40.0
    # ...ve CONFIRM'dan sonra aynı anahtar yine tek tüketim kalır:
    b.confirm(r2.rid)
    r3 = b.reserve("BTCUSDT", 40, idem_key="ayni")
    assert r3.ok and b.status()["trades_used"] == 1


# ------------------------------------------------- S5: UNKNOWN → rezerv kalmalı
def test_S5_release_yalniz_RESERVED_iade_eder(tmp_path):
    b = _budget(tmp_path)
    r = b.reserve("BTCUSDT", 40, idem_key="a")
    b.confirm(r.rid)
    # ACK belirsiz yolunda release ÇAĞRILSA bile CONSUMED geri alınamaz:
    assert b.release(r.rid, reason="gec") is False
    assert b.status()["notional_used"] == 40.0


def test_double_release_tek_iade(tmp_path):
    b = _budget(tmp_path)
    r = b.reserve("BTCUSDT", 40, idem_key="a")
    assert b.release(r.rid, reason="f1") is True
    assert b.release(r.rid, reason="f2") is False   # ikinci kez iade YOK
    st = b.status()
    assert st["trades_used"] == 0 and st["notional_used"] == 0.0


def test_bilinmeyen_rid_release_false(tmp_path):
    b = _budget(tmp_path)
    assert b.release("yok-boyle-bir-id", reason="x") is False


# ------------------------------------------------------- S8: restart kalıcılığı
def test_S8_restart_kaliciligi(tmp_path):
    b1 = _budget(tmp_path)
    r = b1.reserve("BTCUSDT", 50, idem_key="a")
    b1.confirm(r.rid)
    del b1                                   # süreç sonu simülasyonu
    b2 = _budget(tmp_path)                   # taze bağlantı, aynı dosya
    st = b2.status()
    assert st["trades_used"] == 1 and st["notional_used"] == 50.0
    r2 = b2.reserve("BTCUSDT", 50, idem_key="b")
    assert r2.ok is True                     # 2. hak duruyor
    assert b2.reserve("ETHUSDT", 1, idem_key="c").reason == "TRADE_COUNT_CAP"


# ------------------------------------------------------------------ S7: yarış
def test_S7_iki_worker_yarisi_atomik(tmp_path):
    b = vb.ValidationBudget(str(tmp_path / "race.db"), _limits(trades=2))
    sonuc = []
    barrier = threading.Barrier(3)

    def worker(i):
        w = vb.ValidationBudget(str(tmp_path / "race.db"),
                                _limits(trades=2))
        barrier.wait()
        try:
            sonuc.append(w.reserve(f"SYM{i}USDT", 40, idem_key=f"w{i}"))
        except Exception as e:                     # fail-closed: hata da sonuç
            sonuc.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join(timeout=30) for t in ts]
    oks = [r for r in sonuc if getattr(r, "ok", False)]
    assert len(oks) == 2, f"tam olarak 2 kazanmalıydı: {sonuc}"
    assert b.status()["trades_used"] == 2


# ------------------------------------------------------------- top-tavan hesabı
def test_toplam_notional_tavan_defans(tmp_path):
    # Defense-in-depth doğrudan Limits ile (env bu kombinasyona izin vermez).
    b = _budget(tmp_path, limits=_limits(trades=10, per=30.0, total=50.0))
    assert b.reserve("AUSDT", 30, idem_key="a").ok
    assert b.reserve("BUSDT", 20, idem_key="b").ok     # 30+20 = 50 tam sınır
    r = b.reserve("CUSDT", 5, idem_key="c")            # 55 > 50
    assert r.ok is False and r.reason == "TOTAL_NOTIONAL_CAP"


# ---------------------------------------------------------------- S10: DB hatası
def test_S10_db_hatasi_BudgetError(tmp_path):
    bad = tmp_path / "not-a-db.db"
    bad.write_bytes(b"bu sqlite degil")
    b = vb.ValidationBudget(str(bad), _limits())
    with pytest.raises(vb.BudgetError):
        b.status()
    b2 = vb.ValidationBudget(str(bad), _limits())
    with pytest.raises(vb.BudgetError):
        b2.reserve("BTCUSDT", 10, idem_key="a")


def test_schema_tek_kurulum_idempotent(tmp_path):
    # Şema TEMBEL kurulur: ilk KULLANIMDA. İkiörnek kullanımı çökmemeli.
    p = tmp_path / "s.db"
    b1 = vb.ValidationBudget(str(p), _limits())
    b1.status()                                    # ilk kullanım → şema kurulur
    b2 = vb.ValidationBudget(str(p), _limits())
    st = b2.status()                               # ikinci init çökmemeli
    assert st["trades_used"] == 0
    con = sqlite3.connect(str(p))
    n = con.execute("SELECT count(*) FROM validation_reservations").fetchone()[0]
    con.close()
    assert n == 0


def test_sifir_negatif_notional_reddi(tmp_path):
    b = _budget(tmp_path)
    assert b.reserve("BTCUSDT", 0, idem_key="a").reason == "BAD_NOTIONAL"
    assert b.reserve("BTCUSDT", -5, idem_key="b").reason == "BAD_NOTIONAL"
