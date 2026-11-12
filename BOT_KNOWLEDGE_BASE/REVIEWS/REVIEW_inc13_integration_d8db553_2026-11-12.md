# BAĞIMSIZ DENETİM — inc13 Entegrasyon (commit `d8db553`)

Denetçi: ZORIN-FURKAN · Tarih: 2026-11-12 · Tür: salt-okunur bağımsız denetim
Branch: `feature/futures-release-blockers` · HEAD: `d8db553`
Kod/commit/VPS/Binance/DB değiştirilmedi. 5 nokta bağımsız doğrulandı; testler bağımsız
koşuldu. (ROL NOTU: reisfurkan-53 çevrimdışı; aynı ajan uygulayıp denetliyor → genel
bağımsızlık SINIRLI. Aşağısı kaynağın/diffin/testlerin doğrudan incelenmesine dayanır.)

## Beş nokta — sonuç
| # | Nokta | Sınıf |
|---|---|---|
| 1 | run_once→open_coin→coordinate_close→request_close zinciri | **VERIFIED** |
| 2 | Coordinator yoksa doğrudan MARKET fallback kesinlikle kapalı | **VERIFIED** |
| 3 | ENTRY/CLOSE/TP UNKNOWN ve None ID doğru ele alınıyor | **VERIFIED** |
| 4 | CloseIntentStore aynı ledger'da cross-process atomik; 2. process 2. POST | **VERIFIED** (request_close) / **PARTIAL** (open_coin uçtan uca) |
| 5 | SOL çift-kapanış, SL PRESENT/ABSENT/UNKNOWN, MAX_OPEN=2 bozulmamış | **VERIFIED** |

## Kanıt (gerçek çağrı zinciri + kaynak)
**1 — Zincir:** `coordinate_close(coordinator, …)` iki open_coin sitesinde (satır 1248,
1274) → `coordinator.request_close` (1044). `run_once` `open_coin`'e `coordinator=
coordinator` geçirir. `test_inc13_live_wiring` gerçek open_coin'in request_close'a
ULAŞTIĞINI kanıtlar (HB stop keep_position, tek reduceOnly POST, intent CLOSED,
idempotent). `test_inc13_acceptance` 10 koşulu gerçek zincirde doğrular.

**2 — Fallback kapalı:** `grep '"type": "MARKET"' auto_trader.py` → **0 sonuç** (hiç
doğrudan MARKET close POST yok). `coordinate_close(None)` → `CloseResult(FAILED_SAFE,
NO_COORDINATOR, critical)`, POST yok (satır 1059); `run_once` coordinator None iken
yeni girişi bloklar (1399). `test_b1_bypass` doğrular.

**3 — UNKNOWN/None:** `_hb_order_ids` satır 136 `{o for o in oids if o is not None and
o != ""}` — None/boş elenir; gerçek id'ler çözülür. CLOSE/TP NEW, ambiguous-id,
executor-yok → UNRESOLVED/UNKNOWN fail-closed, POST yok. `test_c1_order_ids` +
`test_inc5_old_orders`/`multiexec`.

**4 — Cross-process:** `CloseIntentStore(risk.db_file)` = `PositionReserver(risk.db_
file)` ile AYNI ledger (1332/1358); `close_intent.py` BEGIN IMMEDIATE (6 kullanım).
`test_inc121_close_intent` GERÇEK iki process + paylaşılan SQLite → **tam 1 POST**,
flat, asla ters; BUSY/CLOSED/STALE gate; UNKNOWN serbest bırakılmaz. **İkinci process
gerçek canlı kapanış yolundan ikinci POST GÖNDEREMEZ** (owner değilse request_close
BUSY/CLOSED → POST yok). **PARTIAL:** bu garanti `request_close` düzeyinde iki-process
testiyle kanıtlı; canlı yol (`open_coin`) request_close'u çağırdığından KOMPOZİSYONLA
kapsanır, ama 2-process'i UÇTAN UCA `open_coin`'den geçiren özel bir test YOK.

**5 — SOL/SL/MAX_OPEN:** `test_inc11_sol_double_close` (çift-kapanış/ters-SHORT
regresyonu), `test_inc10_sl_status`+`test_inc101_mode_aware_sl`+`test_inc111_sl_coverage`
(PRESENT/ABSENT/UNKNOWN mode-aware), `test_position_reservation`+`test_f1_sl_verify`
(MAX_OPEN=2 atomik, SL doğrulama) → hepsi geçti; bozulma yok.

## Bağımsız test koşumu (gerçek exit kodları)
- Acceptance + live wiring: **12 passed**, exit 0.
- B-1 bypass: **2 passed**, exit 0.
- C-1 + inc5 order resolution: **29 passed**, exit 0.
- Intent cross-process: **21 passed**, exit 0.
- SOL + sl_status + reservation + F-1: **77 passed**, exit 0.
- (Önceki tam suite sonucu korunuyor: **278 passed**; gereksiz tam döngü tekrarlanmadı.)

## Kritik/yüksek bulgular
**YOK.** Tek PARTIAL (düşük önem): nokta-4 için 2-process'i uçtan uca `open_coin`'den
geçiren özel test yok (request_close düzeyinde kanıtlı, kompozisyonla kapsanır). İsteğe
bağlı iyileştirme: open_coin üzerinden iki-process kapanış testi eklenebilir.

## Release kararı
- **inc13 entegrasyon 5 noktası: PASS** (kod düzeyinde; kritik/yüksek bulgu yok).
- **Üretim release'i: BLOCKED** (bu denetimin 5 noktası DIŞINDAKİ canlı kapılar):
  canlı ortamda **korumasız açık SOLUSDT −0.3 SHORT (SL yok)** mevcut; deploy öncesi
  ele alınmalı. Ayrıca anahtar rotasyon provenansı PARTIAL (yetkiler VERIFIED:
  ipRestrict=true, withdraw=false). Bu kapılar tamamlanmadan deploy/restart yapılmaz.

## Değişmezlere uyum (denetim sırasında)
Üretim VPS/Binance/DB/servislere emir/yazma yapılmadı. Kod yazılmadı/commit edilmedi.
Yalnızca git okuma, grep, kaynak okuma, yerel venv test koşumu.
