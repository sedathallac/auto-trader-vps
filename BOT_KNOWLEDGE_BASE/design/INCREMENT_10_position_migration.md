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

## Canlı doğrulama sınırı — PARTIAL
`close_adapter.sl_status` canlıda `BinanceAPI.open_algo_orders_for()` kullanır; bu
API hatasında `[]` döndürdüğü için "canlı API hatası" ile "gerçekten SL yok" her
zaman ayırt edilemez → canlı davranış **PARTIAL**. Migrator mantığı mock ile tam
test edilmiştir (VERIFIED); canlı algo-emir okuma yolu ayrı bir sertleştirme işi.

## Kapsam dışı (bu increment değil)
Gerçek SL yerleştirme, SOLUSDT gerçek-kod regresyon fikstürü (inc11),
eşzamanlılık/restart entegrasyonu (inc12). Bunlar ayrı onay bekler.
