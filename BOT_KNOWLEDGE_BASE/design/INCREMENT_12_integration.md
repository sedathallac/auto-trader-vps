# Increment 12 — Concurrency / Restart / Timeout / UNKNOWN Entegrasyonu

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · Başlangıç HEAD: `d30a749`
Değişen dosyalar: `close_coordinator.py` (RC-A lock), `auto_trader.py`
(`ensure_sl_coverage` RC-B temizlik), `tests/test_inc12_integration.py` (YENİ, 25 test).
Üretim: önceki commit'ler yeniden yazılmadı; gerçek Binance emri yok; HEDGE otomatik
emir YOK.

## Kök nedenler (inc12'de bulunup düzeltilen)
### RC-A — CloseCoordinator thread-safe değildi
`request_close` kilit kullanmıyordu. Aynı coordinator'a eşzamanlı iki tetik (iki
thread / iki executor) idempotency kontrolünü ikisi de geçip iki ayrı handoff
çalıştırabiliyordu (RED: iki thread de `SENT_REDUCE_ONLY`, hiçbiri IGNORED değil;
sıkışık zamanlamada ikinci bir POST -2022 ile reddedilir ama yine de GÖNDERİLİR).
Düzeltme: per-coordinator `threading.RLock`; `request_close` kilit altında
`_request_close_locked`'ı çağırır → ikinci tetik `_closed` görür → IGNORED, tek POST.
**Process sınırı Python kilidine DAYANMAZ** (paylaşılan bellek yok); gerçek atomiklik
BORSADAN gelir (reduceOnly sıfırı geçemez, -2022) — cross-process test bunu kanıtlar.

### RC-B — `_OPEN_UNPROTECTED` flat sonrası temizlenmiyordu
Bir -2021 (veya UNKNOWN) kaydı, pozisyon kapandıktan sonra haritada kalıp
`should_block_new_entries()`'i sonsuza dek True tutuyordu → yeni girişler kalıcı
bloklu (RED: kapanan `OLDSYM` haritada kaldı). Düzeltme: `ensure_sl_coverage`
GÜVENİLİR positionRisk okumasında (liste) `open_pos`'ta olmayan sembolleri haritadan
siler. Güvenilmez okuma (liste değil) zaten erken döner → temizlik YAPILMAZ (yanlış
flat varsayımı yok).

## Test yöntemi
Gerçek çağrı zinciri: `CloseCoordinator → BinanceHBCloseAdapter → BinanceAPI/HBAPI`,
`ensure_sl_coverage`, `PositionMigrator`, `PositionReserver`. Yalnızca dış sınır
mock: in-memory testlerde modül `_session`; cross-process'te **paylaşılan SQLite
borsa** (iki process tek atomik reduceOnly sınırına vurur).

## Senaryolar (25 test, hepsi GREEN)
- **Concurrency (thread):** single close baseline; iki thread yarışı → TEK POST, biri
  IGNORED, ters SHORT yok. 5/5 tekrar kararlı.
- **Cross-process:** iki PROCESS gerçek coordinator ile paylaşılan SQLite borsaya →
  son pozisyon flat (0), asla negatif; tek etkili kapanış (ikinci -2022). Python
  kilidi değil, borsa atomikliği. 5/5 kararlı.
- **Restart recovery:** yeni coordinator borsa gerçeğini sorgular — flat→ALREADY_FLAT
  (resend yok); açık→tek kapanış CLOSED; eski HB emri mapping kaybı→UNKNOWN, POST yok;
  kısmi→CLOSED değil (PARTIAL). Yerel state tek başına doğru sayılmaz; flat olmadan
  CLOSED yok.
- **Timeout/UNKNOWN:** positionRisk timeout→FAILED_SAFE POST yok; POST timeout→
  FAILED_SAFE; algo SL sorgu timeout→UNKNOWN, SL yok, blok; cancel timeout→
  UNRESOLVED_ORDER POST yok; positionRisk hata dict→fail-closed POST yok. Her UNKNOWN:
  fail-closed, yeni giriş blok, kontrolsüz tekrar POST yok, CRITICAL.
- **SL/migration entegre (inc10/10.1/11.1):** PRESENT→ikinci SL yok; ABSENT ONEWAY→
  mevcut kontrollü mekanizma; UNKNOWN→SL yok+CRITICAL; HEDGE geçerli→PRESENT; HEDGE
  eksik→fail-closed emir yok; ilgisiz algo→koruma değil; yanlış side→PRESENT değil.
  RC-B: flat'te OPEN_UNPROTECTED temizlenir; güvenilmez okumada KORUNUR.
- **MAX_OPEN_POSITIONS=2 (gerçek PositionReserver):** iki slot sonra cap; 6 paralel
  thread → en fazla 2; UNKNOWN rezervasyon serbest bırakılmaz (reconcile'a kadar);
  restart reconcile gerçek pozisyonu promote eder / stale'i release eder.

## RED/GREEN
Davranışsal RED (düzeltme ÖNCESİ gerçek kodda): `test_concurrent_close_single_post`
(iki SENT_REDUCE_ONLY, IGNORED yok) ve `test_open_unprotected_cleared_on_flat`
(OLDSYM haritada kaldı) → **2 failed**. İki kök neden düzeltildi → **25 passed**.
Tam `tests/`: **229 passed** (önce 204 → +25). Ruff `--select F,E9` temiz;
py_compile temiz.

## Kanıt etiketleri
- **VERIFIED:** RC-A lock (thread + cross-process), RC-B temizlik, restart recovery,
  timeout/UNKNOWN matrisi, SL/migration entegrasyonu, MAX_OPEN=2 (gerçek reserver),
  tüm regresyon, static. Cross-process atomiklik gerçek SQLite BEGIN IMMEDIATE ile.
- **PARTIAL:** gerçek Binance'e karşı KOŞULMADI (canlı emir yok); timeout/UNKNOWN
  gerçek HTTP yerine enjekte edildi. `open_algo_orders_raw` canlı davranışı PARTIAL.
- **NO EVIDENCE:** gerçek borsada uçtan uca; gerçek iki işletim süreci canlı Binance'e.

## Bilinen eksikler / açık riskler
- HEDGE SL **oluşturma** yolu üretimde yok → HEDGE pozisyonlar fail-closed bloklanır
  (tasarım gereği; otomatik emirle çözülmedi, talimat gereği).
- Cross-process close idempotency Python düzeyinde paylaşılmaz; güvenlik borsanın
  reduceOnly atomikliğine dayanır (kanıtlandı). Paylaşılan bir "kapanış niyeti"
  defteri (CloseCoordinator için reservation benzeri) ileride düşünülebilir.
- Canlı uçtan uca doğrulama ve anahtar rotasyonu hâlâ kullanıcı/ayrı iş.
