# CloseCoordinator — Bütünleşik Kabul Raporu (K1=B)

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · HEAD: `21cfd7f`
Durum: **branch'te tamamlandı; ÜRETİME GEÇİLMEDİ** (deploy/restart/push yok, canlı emir yok).
Tam regresyon: **229 passed** (18 test dosyası). Ruff F/E9 temiz; py_compile temiz.

> Bu rapor tek kapanış otoritesi (CloseCoordinator) + SL/migration iş paketinin
> (inc3–inc12) bütünleşik kabulünü özetler. Her iddia VERIFIED / PARTIAL / NO EVIDENCE.

## 1. Mimari karar — K1=B (kalıcı)
Borsa reduceOnly STOP_MARKET algo emri **tek stop-loss otoritesidir**. Diğer tüm
kapanışlar (TP, time-limit, manuel, reconcile) TEK `CloseCoordinator`'dan geçer.
HB'nin otonom kapanışları `build_executor_config` (tüm triple-barrier None) +
`stop_executor(keep_position=True)` ile kapatılır. ONEWAY'de kapanış reduceOnly'dir
(-2022 sıfırı geçemez → ters pozisyon imkânsız).

## 2. Commit soy zinciri (bu iş paketi)
| Commit | İçerik |
|---|---|
| `…6d342ee, a590271, b61d16e, 1410569, 72b7ad4` | inc3–inc9 + inc5: core, adapter, HANDOFF, HB-barrier kapatma, -2021, K2, K6, orphan_killer, pre-sent emir çözümü, multi-executor |
| `86564b5` | inc10 — açık pozisyon güvenli migration/handoff |
| `569c5ec` | inc10 — `sl_status` tri-state (PRESENT/ABSENT/UNKNOWN) |
| `57c010a` | inc10.1 — mode-aware SL doğrulama (ONEWAY/HEDGE tam kapsama) |
| `48a8513` | inc11 — SOLUSDT gerçek-kod çift-kapatma regresyonu + RC-2 bulgusu |
| `d30a749` | inc11.1 — RC-2 düzeltmesi (ensure_sl_coverage → sl_status) |
| `21cfd7f` | inc12 — concurrency/restart/timeout/UNKNOWN + RC-A (lock) + RC-B (temizlik) |

## 3. Çözülen kök nedenler
| Kod | Kök neden | Düzeltme | Durum |
|---|---|---|---|
| SOL çift-kapanış | İkinci (non-reduceOnly) kapanış flat'te ters SHORT | Tek otorite + idempotency + ONEWAY reduceOnly + K2 flat-confirm | **VERIFIED** (gerçek-kod regresyon) |
| -2021 naked | SL -2021'de konamazken sessiz korumasızlık | OPEN_UNPROTECTED + CRITICAL + giriş blok + kontrolsüz-retry yok | **VERIFIED** |
| RC-2 | `ensure_sl_coverage` "herhangi algo emri = korunuyor" | mode-aware `sl_status` tam-kapsama | **VERIFIED** |
| RC-A | `request_close` thread-safe değil → çift POST | per-coordinator RLock | **VERIFIED** (thread+process) |
| RC-B | `_OPEN_UNPROTECTED` flat sonrası temizlenmiyor → kalıcı blok | güvenilir flat teyidiyle temizlik | **VERIFIED** |
| TOCTOU/MAX_OPEN | Eş zamanlı açılış cap'i aşabilir | atomik SQLite rezervasyon (BEGIN IMMEDIATE) | **VERIFIED** (thread+process) |

## 4. Değişmezler (invariant) — kapsam
- **Tek etkili kapanış / idempotency**: CLOSED sonrası tekrar tetik IGNORED; eşzamanlı
  tetikler kilitle serileşir. **VERIFIED**
- **F6 ters çevirmeme**: kapanış işareti çeviremez (reduceOnly + REVERSED guard). **VERIFIED**
- **K2 flat-confirm**: yalnızca positionRisk==0 → CLOSED; kısmi→PARTIAL; kör resend yok. **VERIFIED**
- **Fail-closed**: her API hatası/timeout/UNKNOWN → yeni emir yok, giriş blok, CRITICAL. **VERIFIED**
- **SL tam kapsama**: PRESENT yalnızca pozisyonun tamamı geçerli STOP_MARKET ile. **VERIFIED**
- **MAX_OPEN_POSITIONS=2**: değişmedi; cross-process atomik. **VERIFIED**

## 5. Test envanteri (229)
close_coordinator 11 · close_adapter 13 · inc3_inc9 14 · inc5_review 15 ·
inc5_old_orders 16 · inc5_multiexec 5 · inc6_k2 11 · inc7_k6 10 · inc8_orphan 7 ·
oneway_guard 7 · write_idempotency 4 · position_reservation 16 · inc10_migration 17 ·
inc10_sl_status 16 · inc101_mode_aware_sl 13 · inc11_sol_double_close 12 ·
inc111_sl_coverage 17 · inc12_integration 25. **Toplam 229 passed.**

Her increment için davranışsal RED→GREEN kaydı ilgili KB dosyalarında (import/collection
RED değil; gerçek değer düzeyi; stash/geçici-zayıflatma + geri alma).

## 6. Concurrency / restart / timeout kanıtı (inc12)
- Thread: iki eşzamanlı kapanış → tek POST, biri IGNORED, ters yok (5/5 kararlı).
- Process: iki process + paylaşılan SQLite borsa → son pozisyon flat, asla negatif;
  güvenlik borsa reduceOnly atomikliğinden (Python kilidi değil). (5/5 kararlı).
- Restart: yeni coordinator borsa gerçeğini sorgular; flat olmadan CLOSED yok;
  mapping kaybı→UNKNOWN; kısmi→PARTIAL.
- Timeout/UNKNOWN matrisi: positionRisk/POST/algo/cancel/hata-dict → fail-closed.

## 7. Bilinen eksikler / açık riskler
1. **HEDGE SL oluşturma yok** → HEDGE pozisyonlar üretimde korunamaz, fail-closed
   bloklanır (tasarım gereği; otomatik emirle çözülmedi). **Açık iş.**
2. **Canlı doğrulama yok** → tüm kanıt gerçek-kod + mock sınır düzeyinde. Gerçek
   Binance'e karşı uçtan uca **NO EVIDENCE**. `open_algo_orders_raw` canlı davranışı
   **PARTIAL** (API hatası ile '[]' ayrımı inc10'da giderildi ama canlı koşulmadı).
3. **Cross-process kapanış niyeti defteri yok** → güvenlik borsanın reduceOnly
   atomikliğine dayanır (kanıtlı); ileride rezervasyon-benzeri bir defter düşünülebilir.
4. **Tarihsel SOL ONEWAY ters-SHORT** arşivde **KISMEN** (positionSide arşivlenmemiş);
   mekanizma gerçek-kodla yeniden üretildi, gerçek olarak sunulmadı.
5. **Dağıtım yapılmadı** → üretim hâlâ eski (korumasız) kodda. Düzeltmeler yalnızca
   branch'te. **En kritik açık risk.**

## 8. Üretime geçiş için ÖN KOŞULLAR (öneri, bu iş paketi dışında)
- Yeni/IP-kısıtlı salt-okunur anahtarla canlı exchange-truth teyidi (kullanıcı).
- İfşa anahtarların rotasyonu (kullanıcı).
- Kontrollü canary ile gerçek reduceOnly kapanış + SL yerleştirme doğrulaması.
- HEDGE kullanılacaksa HEDGE SL oluşturma yolunun ayrı eklenmesi (yoksa ONEWAY zorunlu).
- Deploy/restart yalnızca kullanıcı onayıyla; migration (inc10) ile açık pozisyon devri.

## 9. Sonuç
CloseCoordinator + SL/migration iş paketi branch'te **bütünleşik olarak kabul edilir**
(229 passed, fail-closed, idempotent, concurrency/restart/timeout dayanıklı, tek SL
otoritesi). **Üretim kabulü** için bölüm 8 ön koşulları ve canlı doğrulama gerekir.
Üretime geçilmedi.
