# Increment 10 — Açık Pozisyonların Güvenli Migration ve Handoff

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · Başlangıç HEAD: `72b7ad4`
Dosyalar: `migration.py` (YENİ), `close_adapter.py` (`sl_status` eklendi),
`tests/test_inc10_migration.py` (YENİ, 17 test / 15+ senaryo).

## Problem
Sistem güncellenip yeniden başlatıldığında borsada ZATEN açık pozisyonlar olabilir.
Bunlar yeni SL/kapanış otoritesine (K1=B: borsa reduceOnly STOP_MARKET algo SL)
devredilirken:
- yanlışlıkla KAPATILMAMALI (çift-kapanış → ters pozisyon riski — SOL olayı),
- korumasız (SL'siz) BIRAKILMAMALI,
- çift-kapatılmamalı.

## Çözüm — `PositionMigrator.migrate(symbol)`
Yalnızca **sınıflandırır + çözer**; asla kapanış emri göndermez, asla otomatik SL
yerleştirmez (SL yerleştirme ayrı adım / normal `ensure_sl_coverage` döngüsü).
Yalnızca **borsa gerçeği**; yerel duruma asla güvenilmez.

Durumlar: `NOT_OPEN` · `MIGRATED` · `UNPROTECTED` · `UNKNOWN` (fail-closed).

6 adım:
1. `position_amt` oku (hata → UNKNOWN; 0 → NOT_OPEN). Side işaretten türetilir.
2. `position_mode` ONEWAY/HEDGE olmalı; değilse → UNKNOWN (fail-closed).
3. `stop_executor` → HB `keep_position=True` (POSITION_HOLD); HB otonom kapanış
   yapamasın. Hata → UNKNOWN.
4. `protective_orders` (pre-sent close/TP/ENTRY) — her biri terminal değilse
   `cancel_order` ile çözülür; teyitli çözülemeyen tek emir → UNKNOWN (halt).
5. **Pozisyonu YENİDEN OKU** (kısmi dolum / çözüm sırasında dolan bir kapanış).
   Artık 0 → NOT_OPEN. Kalan miktar/side güncellenir.
6. `sl_status` gerçek durumla doğrula: UNKNOWN→UNKNOWN(halt),
   "PROTECTED"→MIGRATED (**ikinci SL YOK**), aksi→UNPROTECTED (CRITICAL).

`migrate_all(exchange, open_symbols)` → `{symbol: MigrationResult}`.
`block_new_entries(results)` → herhangi biri UNKNOWN/UNPROTECTED ise True
(MAX_OPEN_POSITIONS=2 korunur; güvensiz devir sırasında yeni giriş açılmaz).

## 10 güvenlik kuralı — nasıl karşılandı
1. Yerel duruma güvenme → sadece `position_amt`/`position_mode` (borsa). **VERIFIED (test)**
2. Belirsizlikte kapatma → hiçbir yolda kapanış emri yok; UNKNOWN=halt. **VERIFIED**
3. HB otonom kapanışı sustur → `stop_executor(keep_position=True)`. **VERIFIED**
4. Pre-sent emirleri çöz, teyit et → cancel + confirm; teyitsiz→UNKNOWN. **VERIFIED**
5. Kısmi dolum → adım-5 re-read; kalan miktar raporlanır. **VERIFIED**
6. Çözüm sırasında kapanırsa → re-read 0 → NOT_OPEN. **VERIFIED**
7. SL belirsizse koru → sl_status UNKNOWN=halt, asla "korunuyor" varsayma. **VERIFIED**
8. SL yoksa → UNPROTECTED + CRITICAL + yeni giriş blok. **VERIFIED**
9. SL varsa ikinci SL gönderme → PROTECTED→MIGRATED, placement yok. **VERIFIED**
10. Idempotent yeniden-migration → tekrar çağrı aynı sınıf, kapanış yok. **VERIFIED**

## RED/GREEN kanıtı (gerçek davranışsal RED)
GREEN: `17 passed`.
Davranışsal RED (import/collection değil): 3 güvenlik kuralı geçici olarak
zayıflatıldı → **4 test yanlış-DEĞER ile düştü**:
- SL=UNKNOWN "güvenli" sayıldı → `test_open_sl_unknown_halts`: MIGRATED beklenmedi,
  UNKNOWN olmalı (çıplak pozisyon güvenli sanıldı).
- re-read atlandı → `test_partial_fill_reread`: amt `-0.3` (eski) ≠ `-0.2` (kalan);
  `test_closed_during_resolution_not_open`: MIGRATED, NOT_OPEN olmalı.
- cancel teyidi yok sayıldı → `test_old_order_unresolved_unknown`: MIGRATED,
  UNKNOWN olmalı (çözülmemiş emir devri bitmiş sanıldı).
Zayıflatma geri alındı → tekrar `17 passed`.

## Regresyon / statik
- `tests/` tümü: **146 passed** (inc10 öncesi 129 → +17). exit 0.
- `py_compile` migration.py + test + close_adapter.py: exit 0.
- `ruff --select F,E9` (correctness) üç dosya: **All checks passed** (0 F-serisi).
- `ruff` (config'siz, tüm kurallar): yalnızca kozmetik — I001 (import sırası) ve
  FURB157 (`Decimal(str(x))` idiomu, float için KASITLI ve doğru). Bu bulgular
  depodaki mevcut commit'li dosyalarda da var (repo'da ruff config yok). **PARTIAL**
  (stil tabanı repo geneliyle tutarlı; düzeltme gerektiren correctness yok).

## SL STATUS TRI-STATE SERTLEŞTİRME (inc10 ek commit, 2026-11-12)
Kök neden: `BinanceAPI.open_algo_orders_for()` API hatasında `[]` döndürüp hatayı
yutuyordu → "canlı API hatası" ile "gerçekten SL yok" ayırt edilemiyordu (outage,
"SL yok" sanılıp UNPROTECTED'a düşebilirdi). Düzeltme (migration mimarisi korunur,
`86564b5` değiştirilmez; yalnızca sl_status API belirsizliği giderilir):

- YENİ `BinanceAPI.open_algo_orders_raw(symbol)` — ham cevap; hata YUTULMAZ
  (timeout/exception propagate, hata dict'i olduğu gibi döner).
- `close_adapter.sl_status(symbol, position_side=None)` artık tri-state döner:
  - `SL_PRESENT` — sorgu başarılı + geçerli + bu sembole (ve verildiyse bu side'a;
    BOTH/ONEWAY eşleşir) ait canlı koruyucu reduceOnly STOP algo emri var.
  - `SL_ABSENT` — sorgu başarılı + geçerli + gerçekten SL yok → UNPROTECTED.
  - `SL_UNKNOWN` — timeout / exception / API hata dict'i / bozuk payload / YANLIŞ
    sembol (doğrulanamayan eşleşme) → HALT. Hiç emir göndermez, stop fiyatı değişmez.
- `migration.py` eşlemesi: PRESENT→MIGRATED, ABSENT→UNPROTECTED(CRITICAL),
  UNKNOWN/beklenmeyen→UNKNOWN(HALT). HEDGE için `side` sl_status'a geçirilir.
- UNKNOWN → `block_new_entries` True (yeni giriş engellenir), uçtan uca test edildi.

RED/GREEN: 16 yeni test GREEN. Davranışsal RED (üç sertleştirme guard'ı geçici
zayıflatıldı → hata-dict/invalid "ABSENT", timeout/exception RAISE, yanlış sembol
"PRESENT") → **6 test yanlış-DEĞER ile düştü**; geri alındı → GREEN.

Kalan sınır — **PARTIAL (canlı)**: mantık fake'lerle tam test edildi (VERIFIED);
gerçek Binance'e karşı KOŞULMADI (canlı emir yok). `open_algo_orders_raw` `_get`'e
dayanır; `_get` istisnayı yakalamaz, dolayısıyla timeout/exception sl_status'un
try/except'ine ulaşır → canlı belirsizlik kök nedeni (yutma) kaldırıldı.

## MODE-AWARE SL VALIDATION (inc10.1 ek commit, 2026-11-12)
Korunan HEAD: `569c5ec`. Amaç: tri-state sınıflandırmayı gerçek Binance
ONEWAY/HEDGE emir sözleşmesine uydurmak ve PRESENT'i **tam koruma** şartına bağlamak.

Kaynak koddan çıkarılan gerçek sözleşme (tahmin değil):
- `place_algo_sl()` tek SL yerleştirme metodu → `POST /fapi/v1/algoOrder`,
  `algoType=CONDITIONAL`, `type=STOP_MARKET`, `side=SELL`(long)/`BUY`(short),
  `triggerPrice`, **`quantity=abs(positionAmt)` (tam pozisyon)**, `reduceOnly=true`.
  `positionSide`/`closePosition` GÖNDERİLMİYOR → üretim **ONEWAY reduceOnly STOP_MARKET**.
- `ensure_sl_coverage()` kapsama tespiti şu an zayıf (sadece "sembole ait herhangi
  bir algo emri var mı"); tip/side/quantity/reduceOnly doğrulamıyor. inc10.1 bu
  boşluğu `sl_status` düzeyinde kapatır.

`sl_status(symbol, position_side, mode, position_amt)` artık mode-aware; PRESENT
yalnızca pozisyonun TAMAMI geçerli biçimde korunuyorsa:
- **ONEWAY**: positionSide BOTH/yok, kapanış yönünde `side`, VE (`closePosition=true`
  YA DA `reduceOnly=true` + `quantity >= tam pozisyon`). Kısmi quantity → tam değil.
- **HEDGE**: positionSide == bu side, kapanış yönünde `side`, `STOP_MARKET`,
  `closePosition=true`, `reduceOnly` true DEĞİL (hedge'de reduceOnly Binance
  tarafından reddedilir → geçersiz kombinasyon kabul edilmez).
Yanlış sembol→UNKNOWN; yanlış side/positionSide→PRESENT değil; yetersiz/eksik
quantity→tam koruma yok; terminal algo→PRESENT değil; API hata/timeout→UNKNOWN;
geçerli sorgu+uygun SL yok→ABSENT. Migrator step-6 `mode` ve `abs(amt2)` geçirir.

RED/GREEN: 12 zorunlu senaryo (+1 eksik-quantity) GREEN (13 test). Davranışsal RED:
mode-aware blok pre-10.1 gevşek kontrole ("canlı reduceOnly STOP = PRESENT")
düşürüldü → **6 test yanlış-DEĞER ile düştü** (HEDGE reduceOnly kombinasyonu, yanlış
side, yanlış positionSide, yetersiz/eksik quantity → yanlış PRESENT; geçersiz HEDGE
SL → yanlış MIGRATED). Geri alındı → GREEN. tests/ **175 passed** (162→+13).
Ruff F,E9 temiz; py_compile temiz.

NOT (PARTIAL): `place_algo_sl` üretimde yalnızca ONEWAY reduceOnly biçimini GÖNDERİR;
sl_status HEDGE closePosition biçimini de DOĞRULAR (ör. elle/dışarıdan konan SL),
ama HEDGE SL GÖNDERME yolu üretimde yok (ayrı iş). Canlı Binance'e karşı koşulmadı.

## Kapsam dışı (bu increment değil)
Gerçek SL yerleştirme, SOLUSDT gerçek-kod regresyon fikstürü (inc11),
eşzamanlılık/restart entegrasyonu (inc12). Bunlar ayrı onay bekler.
