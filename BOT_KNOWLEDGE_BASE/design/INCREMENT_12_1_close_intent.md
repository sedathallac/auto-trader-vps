# Increment 12.1 — Cross-Process Close Intent / Idempotency

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · Başlangıç HEAD: `2b71a17`
Değişen dosyalar: `close_intent.py` (YENİ), `close_coordinator.py` (opsiyonel intent
gate), `close_adapter.py` (`position_token`), `tests/test_inc121_close_intent.py`
(YENİ, 21 test). Önceki commit'ler değişmedi.

## Kök neden
inc12 `request_close`'u PROCESS İÇİNDE atomik yaptı (RLock), ama iki ayrı PROCESS
paylaşılan belleğe sahip değil → her biri handoff çalıştırıp bir kapanış POST'u
gönderebilir. inc12 borsanın reduceOnly (-2022) son bariyer olduğunu kanıtladı, ama
bu yine de gereksiz ikinci bir POST'a (reddedilir, CRITICAL, gürültü) izin verir.

## Çözüm — atomik close-intent (AYNI SQLite defteri)
`CloseIntentStore` PositionReserver ile AYNI `risk_ledger.db`'yi kullanır (ikinci db
YOK), `BEGIN IMMEDIATE`+WAL ile atomik. Intent anahtarı (symbol, token); token
exchange gerçeğinden türetilen pozisyon-DÖNEM id'sidir: `<side>:<entryPrice>`.
**Symbol tek başına güvensizdir:** ONEWAY'de yeni dönem eski (terminal) intent'i
devralmamalı; HEDGE'de aynı sembolde LONG ve SHORT ayrı pozisyonlardır → token
side taşır. Durumlar: PENDING→SENT→CLOSED(terminal). `acquire`:
- ACQUIRED: bu çağıran sahip; tek kapanışı o gönderir.
- BUSY: aynı token'da aktif intent → başka process uçuşta; **yeni POST yok**.
- CLOSED: aynı token terminal → zaten kapandı; IGNORE.
- STALE: farklı token'da aktif intent (önceki dönem, sahip çökmüş) → fail-closed;
  exchange gerçeğiyle reconcile gerekir.
UNKNOWN'da intent SİLİNMEZ/serbest bırakılmaz; `reconcile(exchange_flat)` güvenilir
flat teyidiyle terminal yapar.

## CloseCoordinator entegrasyonu (opsiyonel)
`CloseCoordinator(exchange, intent_store=None)`. None → eski davranış (tüm mevcut
testler korunur). Set → POST'tan ÖNCE `position_token` + `acquire`:
BUSY→IGNORED_IN_PROGRESS (POST yok), CLOSED→IGNORED_ALREADY_CLOSED, STALE→FAILED_SAFE
critical, ACQUIRED→tek kapanış. Flat-confirm→mark_closed(terminal); partial/UNKNOWN→
intent aktif kalır (kör re-POST yok).

## RED/GREEN
Davranışsal RED (`test_RED_without_intent_second_post_sent`): intent store olmadan
ikinci process, A'nın uçuştaki kapanışına rağmen İKİNCİ bir POST gönderir (posts=1).
GREEN (`test_second_process_in_progress_no_post`): intent ile ikinci process POST
göndermez (BUSY → IGNORED_IN_PROGRESS). Gerçek iki-process testi: paylaşılan SQLite
borsa + paylaşılan intent db → **tam 1 POST**, son pozisyon flat, asla ters. 3/3 kararlı.
21 test GREEN; tests/ **250 passed**; ruff F/E9 temiz; py_compile temiz.

## Bağımsız denetim bulguları
- **KRİTİK (çözülmedi, ürün entegrasyonu):** CloseCoordinator / CloseIntentStore /
  PositionMigrator üretim ana döngüsüne (auto_trader.py) **BAĞLANMAMIŞ** — yalnızca
  docstring'de geçer; `request_close` hiç çağrılmaz. Canlı kapanış/SL yolu hâlâ
  `open_coin` içindeki doğrudan market close + `place_algo_sl`. Bu iş paketindeki tek
  kapanış otoritesi RUNTIME'da ETKİN DEĞİL. (Deploy kararını değiştirir — bkz. kabul
  raporu ve deploy STOP notu.)
- **Orta (artık risk):** STALE intent, eski dönemin güvenilir flat teyidiyle
  reconcile edilene dek fail-closed bloklar; üretim döngüsü periyodik reconcile
  çağırmalı. Bu arada pozisyon borsa SL'i ile korunur (güvenli).
- **Düşük (mevcut tasarım):** `position_token`/`position_amt` ONEWAY-şekillidir (sembol
  başına tek satır varsayar); HEDGE desteklenmez, fail-closed kalır (K1=B ONEWAY).

## Kanıt etiketleri
- **VERIFIED:** intent atomikliği (thread + gerçek iki-process), gate mantığı
  (BUSY/CLOSED/STALE/ACQUIRED), terminal/partial/UNKNOWN davranışı, restart reconcile,
  yeni-dönem token, tüm regresyon, static.
- **PARTIAL:** gerçek Binance'e karşı koşulmadı.
- **NO EVIDENCE:** canlı uçtan uca; üretim entegrasyonu (yapılmadı).
