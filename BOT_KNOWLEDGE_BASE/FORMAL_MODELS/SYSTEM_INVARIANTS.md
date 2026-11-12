# Sistem Invariant'ları ve Mevcut Durum

Son inceleme: 2026-11-11. Kaynak: `/root/auto_trader.py`, `/root/orphan_killer.py`,
Hummingbot `position_executor.py` (disk kopyası dev-2.18.0). Durum etiketleri bu
oturumdaki **kod okumasına** dayanır; "VIOLATED" tespitleri testle teyit edilmeli.
Test KOŞULMADI (bu ortamda mümkün değil) → kesinleştirme Zorin'de.

## Futures invariant'ları (auto_trader)
| # | Invariant | Durum | Kanıt / not |
|---|---|---|---|
| F1 | Borsa teyidi olmadan pozisyon CLOSED sayılmaz | **İHLAL** | `sync_risk_engine` executor TERMINATED'da PnL yazıyor, flat teyidi yok (auto_trader.py:286-312) |
| F2 | Tek pozisyonda iki habersiz kapanış otoritesi olamaz | **İHLAL** | HB triple-barrier SL + borsa algo SL; SOL kanıtı (findings/2026-11-11) |
| F3 | Duplicate submission ikinci pozisyon açmamalı | **BELİRSİZ** | clientOrderId idempotency auto_trader'da yok; HB executor'a bağlı — UNVERIFIED |
| F4 | Timeout→UNKNOWN'da körlemesine resend yok | **İHLAL (risk)** | SL doğrulanamazsa market close gönderiliyor, UNKNOWN ele alınmıyor (open_coin:835-852) |
| F5 | Unknown sonuç önce order history ile çözülür | **İHLAL** | Eylemden önce order history reconciliation yok |
| F6 | Kapatma emri ters pozisyon açamaz | **İHLAL** | ONEWAY reduceOnly'siz kapanış; SOL −0.3 SHORT |
| F7 | ONEWAY/HEDGE kapanış ayrı doğrulanmalı | **YOK** | Bot position mode'u hiç kontrol/ayar etmiyor |
| F8 | Koruyucu emirler borsa durumuyla doğrulanmalı | **KISMEN** | `ensure_sl_coverage` borsayı sorguluyor (iyi) ama drift'te atlanıyor |
| F9 | SL doğrulaması drift'te devre dışı kalmamalı | **İHLAL** | drift → `run_once` 954-956'da return; ensure_sl_coverage (963) atlanıyor |
| F10 | Risk motoru stratejiden bağımsız çalışabilmeli | **KISMEN** | `RiskEngine` ayrı sınıf ama aynı süreç/döngü; bağımsız koşamaz |
| F11 | MAX_OPEN_POSITIONS=2 korunur | **GEÇERLİ** | Runtime env=2 (loglarda "limit doldu (2/2)") — değiştirilmeyecek |
| F12 | Limit her emir öncesi atomik kontrol | **KISMEN** | C6-fix her open_coin öncesi canlı sayım (1009-1015) ama TOCTOU penceresi kalıyor; atomik değil |
| F13 | Drift/unknown/tutarsızlıkta fail-closed giriş | **KISMEN** | drift yeni girişi durduruyor; unknown order outcome fail-closed değil |
| F14 | Koruma + alarm + acil risk çalışmaya devam | **İHLAL** | drift'te ensure_sl_coverage atlanıyor (bkz F9) |
| F15 | Kapatma tek yetkili mekanizmayla koordine | **İHLAL** | iki bağımsız kapatıcı, koordinasyon yok (bkz F2) |

## Spot Grid invariant'ları (GridC0)
GridC0 read-only ve 30 testli. Aşağıdakiler için **ilgili test dosyası mevcut**
ama bu oturumda **KOŞULMADI** → hepsi UNVERIFIED (tasarlanmış görünüyor).
| # | Invariant | İlgili test (var) | Durum |
|---|---|---|---|
| G1 | Her seviye tekil emir+lifecycle | test_level_lifecycle | UNVERIFIED (test var) |
| G2 | Aynı seviyeye duplicate emir engeli | test_om_blocking, test_client_order_id | UNVERIFIED (test var) |
| G3 | free/locked bakiye ayrı | test_inventory | UNVERIFIED (test var) |
| G4 | Partial fill kalan miktar doğru | test_fill_sync, test_om_sell | UNVERIFIED (test var) |
| G5 | Seviye yalnız gerçek fill+mutabakatla güncellenir | test_reconcile, test_fill_sync | UNVERIFIED (test var) |
| G6 | Gerçekleşmeyen emir fill sayılmaz | test_order_sync | UNVERIFIED (test var) |
| G7 | Açık seviye limiti emir öncesi kontrol | test_om_blocking, test_risk | UNVERIFIED (test var) |
| G8 | Yetersiz envanter/bakiyede güvenli engel | test_risk, test_inventory | UNVERIFIED (test var) |
| G9 | Ekonomik olmayan spread'de emir yok | test_grid_strategy, test_backtest | UNVERIFIED (test var) |
| G10 | Reconcile hatası emri durdurur, gözlem sürer | test_reconcile_loop | UNVERIFIED (test var) |

## Doğrulama planı
- Futures: her İHLAL için Zorin'de kırmızı regresyon testi yaz → düzelt → yeşil.
  İlk hedef F2/F6/F9/F15 (SOL sınıfı). Model testi: `TESTING/tests/test_close_authority_model.py`.
- Grid: 30 testi Zorin'de koş; geçen/düşen listesi + coverage kaydet; sonra
  G1–G10'u gerçek koşum çıktısıyla DOĞRULANDI/BAŞARISIZ etiketle.
