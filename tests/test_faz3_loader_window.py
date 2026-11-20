"""BLOK6 İŞ1 — SymbolData._load_tf pencere süzgeci [from, to) (ÜST DIŞLANIR).

Kök neden (BLOK4 İŞ2'de bulundu): _load_tf ym_from/ym_to'ya hiç bakmıyordu;
yayın koşusundan SONRA dize eklenen 15m/2026-11.csv FAZ3 tekrarını bozdu
(56340≠56334). Kalıcı çözüm: yükleyici pencereye saysın; kırpılmış veriye
manuel bağımlılık (_repub_state) kaldırılsın.

KIRMIZI→YEŞİL sözleşmesi: dizine bir AYLIKK dosya eklemek (pencere dışı)
yüklenen seriyi DEĞİŞTİRMEMELİ. Aynı ilke entegrasyonda: 2026-11 dosyaları
dursun, kapılar 56334/56334 ve 35829/35829 kalmalı (faz4_ci_refresh koşusu).
"""
import datetime as dtm
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import faz3_backtest as FB


def _month_csv(path, ym, n=12):
    """Sadece o ayın ilk n 15m barını yazan sahte CSV (13 kolonlu arşiv biçimi)."""
    y, m = int(ym[:4]), int(ym[5:7])
    start = int(dtm.datetime(y, m, 1, tzinfo=dtm.timezone.utc).timestamp() * 1000)
    lines = []
    for k in range(n):
        t = start + k * 900_000
        px = 100.0 + k
        lines.append(f"{t}," + ",".join(f"{px:.8f}" for _ in range(10)) +
                     f",{t + 899999},1234.5,0,0,0")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _mk_root(tmp_path, months, tf="15m", sym="TESTUSDT"):
    d = os.path.join(str(tmp_path), sym, tf)
    os.makedirs(d, exist_ok=True)
    for ym in months:
        _month_csv(os.path.join(d, f"{ym}.csv"), ym)
    return d


def test_loader_keeps_only_window_months(tmp_path):
    _mk_root(tmp_path, ["2024-01", "2024-02", "2024-03", "2024-04"])
    sd = FB.SymbolData("TESTUSDT", str(tmp_path), "2024-01", "2024-03")
    yms = {FB._ym(r[0]) for r in sd.r15}
    assert yms == {"2024-01", "2024-02", "2024-03"}, \
        f"ÜST sınır dışlanmalı ([from,to)): {sorted(yms)}"


def test_loader_keeps_pre_window_warmup(tmp_path):
    """BİLİNÇLİ SAPMA: alt sınır trimlenmez — ısıtma önceki aylardan gelir
    ve yayın koşusu 8 majörde 2022-23 geçmişiyle koşulmuştu (kabul kriteri
    56334'ün değişmemesi alt-trim'i kendisiyle çelişik kılar)."""
    _mk_root(tmp_path, ["2024-01", "2024-02"])
    sd = FB.SymbolData("TESTUSDT", str(tmp_path), "2024-02", "2024-02")
    assert {FB._ym(r[0]) for r in sd.r15} == {"2024-01", "2024-02"}


def test_late_extra_month_file_changes_nothing(tmp_path):
    """İŞ1 kabul testi (kırmızı→yeşilin birimi): sonra eklenen pencere-dışı
    ay dosyası yüklenen seriyi değiştirmez."""
    d = _mk_root(tmp_path, ["2024-01", "2024-02", "2024-03"])
    before = FB.SymbolData("TESTUSDT", str(tmp_path), "2024-01", "2024-03").r15
    _month_csv(os.path.join(d, "2024-04.csv"), "2024-04")   # parite-tuzak senaryosu
    after = FB.SymbolData("TESTUSDT", str(tmp_path), "2024-01", "2024-03").r15
    assert after == before, "Sonradan eklenen pencere-dışı ay seriye girdi!"


def test_in_window_extra_month_still_loads(tmp_path):
    d = _mk_root(tmp_path, ["2024-01", "2024-02"])
    _month_csv(os.path.join(d, "2024-03.csv"), "2024-03")
    sd = FB.SymbolData("TESTUSDT", str(tmp_path), "2024-01", "2024-03")
    assert {FB._ym(r[0]) for r in sd.r15} == {"2024-01", "2024-02", "2024-03"}, \
        "Pencere İÇİ ay süzülmüş olamaz (to kapsayıcı: to_excl = to+1ay)"
