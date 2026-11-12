# Increment 11 — SOLUSDT Gerçek-Kod Çift-Kapatma Regresyonu

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · Başlangıç HEAD: `57c010a`
Dosya: `tests/test_inc11_sol_double_close.py` (YENİ, 12 test + 1 xfail). Üretim kodu
DEĞİŞMEDİ (regresyon kilidi + ayrı kök neden bulgusu).

## Amaç
Tarihsel SOLUSDT olayının (LONG → 1. SELL kapatır → 2. SELL flat pozisyonda dolup
ters SHORT açar) aynı koşullarda tekrar oluşmasını, **gerçek bot kodunun davranışıyla**
engellendiğini kanıtlamak. Temel kabul: ilk kapanıştan sonra yeni kapatma emri YOK.

## Gerçek çağrı zinciri (yalnızca dış sınır mock)
`CloseCoordinator → BinanceHBCloseAdapter → BinanceAPI / HBAPI` GERÇEK sınıflar.
Mock edilen tek şey modül düzeyli `_session` (HTTP sınırı). Sahte `_session`,
ONEWAY netting + **reduceOnly -2022 clamp** (reduceOnly emir sıfırı geçemez, flat
pozisyonda reddedilir) modelleyen stateful bir borsadır; böylece gerçek emir
gönderme yolu (`BinanceAPI._post` gerçek parametre üretimi, imzalama) çalışır.

## Çift-kapatma koruması (gerçek kodda, kanıtlı — VERIFIED)
Katmanlı savunma, hepsi gerçek kodda doğrulandı:
1. **Idempotency** (`_closed`): CLOSED sonrası her tekrar tetik → IGNORED, emir yok.
2. **K2 flat-confirm**: kapanıştan sonra sadece positionRisk==0 ise CLOSED; kısmi →
   PARTIAL (kör resend yok); ters işaret → REVERSED FAILED_SAFE.
3. **ONEWAY reduceOnly** (`close_reduce_only`): -2022 ters pozisyonu imkânsız kılar.
4. **HB handoff**: `stop_executor(keep_position=True)` otonom HB kapanışını kapatır.

## 10+ senaryo (hepsi GREEN, her biri emir sayısı/yön/miktar/son pozisyon doğrular)
1. LONG 0.3 → tek reduceOnly SELL 0.3 → flat, ters SHORT yok (posts=1).
2/5. İkinci/bağımsız tetik → IGNORED_ALREADY_CLOSED, posts=1.
3. İlk sonuç UNKNOWN (flat-confirm okuma hatası) → FLAT_CONFIRM_FAILED; retry (okuma
   düzelir, flat) → ALREADY_FLAT, ikinci emir YOK.
4. Kısmi dolum (0.2) → gerçek kalan 0.1 okunur → PARTIAL, kör resend yok.
6. Eski canlı HB close emri (cancel terminalize edemiyor) → UNRESOLVED_ORDER, posts=0.
7. Restart → yeni coordinator borsa gerçeğinden uzlaşır (flat) → ALREADY_FLAT, posts=0.
8. Borsa flat, yerel OPEN → ALREADY_FLAT, emir yok.
9. Borsa açık, yerel CLOSED → migration borsa gerçeğini kullanır, NOT_OPEN DEĞİL.
10. Non-reduceOnly SELL flat'te ters SHORT ÜRETİR (sahte modeli kanıtlar); coordinator
    reduceOnly yolu üretmez (ALREADY_FLAT, posts=0, amt=0).

## Davranışsal RED (gerçek, değer düzeyinde)
Coordinator idempotency guard'ı geçici devre dışı (`if False and ...`), stale-
positionRisk yarışı (replay tetiği eski 0.3 okur): **3 test düştü**; kritik olan
`test_duplicate_trigger_stale_race_single_close` → `CLOSE_SEND_FAILED` (yani İKİNCİ
reduceOnly SELL GÖNDERİLDİ/denendi, posts=2) ve yalnızca -2022 clamp ters SHORT'u
engelledi. Guard geri alındı → 12 passed + 1 xfail. **İki guard da yük taşıyor:**
idempotency ikinci denemeyi durdurur; reduceOnly ters SHORT'un son bariyeridir.

## AYRI KÖK NEDEN — RC-2 (raporlandı, bu increment'te DÜZELTİLMEDİ)
`ensure_sl_coverage()` (auto_trader.py:664):
`covered |= {o["symbol"] for o in binance.open_algo_orders_for(sym)}`
Yalnızca "sembole ait HERHANGİ bir algo emri var mı" kontrol eder; **STOP_MARKET mı,
side, positionSide, reduceOnly/closePosition, quantity (tam kapsama), emir durumu**
doğrulanmaz. İlgisiz / yanlış / terminal bir algo emri (ör. TAKE_PROFIT, iptal
edilmiş STOP, yanlış side, yetersiz quantity) pozisyonu "korumalı" gösterir →
UNPROTECTED pozisyona SL konmaz. Testte belgelendi:
- `test_nonstop_algo_order_should_not_count_as_coverage` — `@expectedFailure`
  (doğru davranış: SL konmalı; bugün konmuyor → beklenen başarısızlık).
- `test_documents_current_weak_behavior` — mevcut zayıf davranışı sabitler (placed=[]).
**Öneri (inc11.1, ayrı commit):** `ensure_sl_coverage` kapsama kontrolünü inc10.1
`sl_status` (mode-aware PRESENT/ABSENT/UNKNOWN, tam-kapsama) üzerinden geçir. HEDGE
SL oluşturma yolu gerçekten desteklenmediği için HEDGE **fail-closed** kalmalı.

## Statik / regresyon
tests/ **187 passed, 1 xfailed** (önce 175 → +12 +1xfail). Ruff `--select F,E9`
temiz. py_compile temiz. close_coordinator.py commit'li sürümle birebir aynı (RED
tamamen geri alındı). Üretim kodu değişmedi.

## Kalan riskler
- RC-2 düzeltilmedi (önerildi) → yanlış/ilgisiz algo emriyle pozisyon korunmasız
  sayılabilir. **PARTIAL** (mantık kanıtlı, canlı teyit yok).
- Tüm iddialar gerçek-kod + fake sınır düzeyinde **VERIFIED**; gerçek Binance'e karşı
  uçtan uca **NO EVIDENCE** (canlı emir yok). Tarihsel ONEWAY ters-SHORT arşivde
  **KISMEN** (test gerçek olarak sunmaz; mekanizmayı fake ile yeniden üretir).
