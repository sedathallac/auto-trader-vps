# Increment 11.1 — SL Coverage Root-Cause Fix (RC-2)

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · Başlangıç HEAD: `48a8513`
Değişen dosyalar: `auto_trader.py` (`ensure_sl_coverage` kapsama tespiti),
`tests/test_inc111_sl_coverage.py` (YENİ, 17 test), `tests/test_inc11_sol_double_close.py`
(RC-2 testleri "fixed" olarak güncellendi).

## Kök neden (RC-2)
`ensure_sl_coverage()` eski kapsama tespiti:
`covered |= {o["symbol"] for o in binance.open_algo_orders_for(sym)}`
Yalnızca "sembole ait HERHANGİ bir algo emri var mı" bakıyordu; STOP_MARKET mı,
side, positionSide, reduceOnly/closePosition, quantity (tam kapsama), emir durumu
doğrulanmıyordu. İlgisiz/yanlış/terminal bir algo emri (TAKE_PROFIT, iptal STOP,
yanlış side, yetersiz quantity) UNPROTECTED bir pozisyonu "korumalı" gösteriyor →
SL konmuyordu.

## Düzeltme (tek doğrulama kaynağı)
Her açık pozisyon inc10.1 `sl_status` üzerinden değerlendiriliyor (gerçek
positionRisk miktarı + ONEWAY/HEDGE mod + side + sembol + emir türü + side +
positionSide + reduceOnly|closePosition + quantity tam kapsama + emir durumu
BİRLİKTE). `BinanceHBCloseAdapter(binance, None).sl_status(sym, side, mode=mode,
position_amt=abs(amt))` — close_adapter'daki doğrulanmış metodun TEK kaynağı; mod
`_verify_oneway(binance)` ile alınır. Davranış:
- **PRESENT** → tam korunuyor; ikinci SL GÖNDERİLMEZ.
- **ABSENT (ONEWAY)** → yalnızca MEVCUT `place_algo_sl` mekanizmasıyla konur;
  -2021 / OPEN_UNPROTECTED / CRITICAL / kontrolsüz-retry-yok davranışı KORUNUR.
- **UNKNOWN** (timeout/exception/hata dict/belirsiz eşleşme) → kör SL YOK;
  `_OPEN_UNPROTECTED`'e eklenir, CRITICAL, yeni giriş bloklanır (fail-closed).
- **HEDGE geçerli SL yok** → ONEWAY `place_algo_sl` HEDGE'i desteklemediği için
  desteklenmeyen emir GÖNDERİLMEZ; fail-closed, CRITICAL, yeni giriş bloklanır.
- Mod doğrulanamazsa (UNKNOWN) → tüm pozisyonlar fail-closed.
PRESENT olduğunda sembol `_OPEN_UNPROTECTED`'ten temizlenir (dış kaynakla çözülen SL).

## RED/GREEN (gerçek ensure_sl_coverage, yalnızca Binance sınırı mock)
GREEN: `test_inc111_sl_coverage.py` **17 passed** (15+ senaryo):
1 ONEWAY reduceOnly PRESENT · 2 ONEWAY closePosition PRESENT · 3 TAKE_PROFIT→SL konur ·
4 terminal STOP→konur · 5 yanlış side→konur · 6 yanlış positionSide→konur ·
7 yetersiz quantity→konur · 8 timeout→UNKNOWN blok · 9 exception→UNKNOWN · 10 hata
dict→UNKNOWN · 11 PRESENT→duplicate yok · 12 ABSENT→konur+alarm temizlenir · 12b
-2021→blok · 13 HEDGE geçerli→PRESENT · 14 HEDGE eksik→fail-closed emir yok · 15 çoklu
pozisyon ayrı değerlendirilir · (+ mod doğrulanamaz→fail-closed).

Davranışsal RED: tespit satırı RC-2 orijinal hatasına döndürüldü
(`SL_PRESENT if binance.open_algo_orders_for(sym) else SL_ABSENT`) → **12 test
yanlış davranışla düştü** (TAKE_PROFIT/terminal/yanlış-side/yanlış-positionSide/
yetersiz-quantity "korumalı" sayıldı, SL konmadı; UNKNOWN'lar yutuldu; HEDGE/çoklu).
Geri alındı → 17 passed.

## Increment 11 regresyonu
`test_inc11_sol_double_close.py` SOLUSDT fixture: **12 passed** (RC-2 testleri artık
"fixed" olarak geçiyor). Tam `tests/`: **204 passed, 0 xfailed** (önce 187+1xfail).
Ruff `--select F,E9` temiz; py_compile temiz. RED artığı yok.

## HEDGE / sınırlar
HEDGE SL **oluşturma** yolu üretimde yok; HEDGE yalnızca gerçek sözleşme
doğrulamasıyla (closePosition, doğru positionSide/side, STOP_MARKET, reduceOnly
değil) PRESENT kabul edilir; aksi hâlde fail-closed. Yeni/kontrolsüz retry
mekanizması oluşturulmadı; mevcut mekanizma korundu. Gerçek Binance emri yok.

## Kanıt etiketleri
- **VERIFIED:** düzeltme mantığı + 15+ senaryo + inc11 regresyon + tüm suite + static.
- **PARTIAL:** canlı — gerçek Binance'e karşı KOŞULMADI; `open_algo_orders_raw`
  canlı davranışı inc10'dan beri PARTIAL. HEDGE SL oluşturma desteklenmiyor
  (fail-closed tasarım gereği).
- **NO EVIDENCE:** gerçek borsada uçtan uca.

## Kalan riskler
- HEDGE pozisyonlar üretimde korunamaz (fail-closed bloklanır) → HEDGE SL oluşturma
  ayrı iş. Canlı uçtan uca doğrulama ve anahtar rotasyonu hâlâ kullanıcı/ayrı iş.
