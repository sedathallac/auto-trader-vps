# Increment 13 — CloseCoordinator Gerçek İşlem Döngüsü Entegrasyonu

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · Başlangıç HEAD: `bc4ffc7`
Değişen dosyalar: `auto_trader.py` (+49/−14), `tests/test_inc13_live_wiring.py` (YENİ, 5 test).
Yeni strateji/indikatör/kaldıraç geliştirilmedi.

## Kök neden (inc12.1 denetiminde bulundu)
CloseCoordinator/CloseIntentStore/PositionMigrator commit'liydi ama canlı döngüye
BAĞLI DEĞİLDİ; `request_close` hiç çağrılmıyordu. Canlı kapanış hâlâ `open_coin`
içindeki iki doğrudan market-close idi → tek kapanış otoritesi runtime'da inert.

## Eski vs yeni POST çağrı haritası
Tüm emir-POST yolları kaynaktan çıkarıldı. Pozisyon KAPATAN tek yollar:
| Site | ESKİ | YENİ |
|---|---|---|
| `open_coin` SL doğrulanamadı | `binance._post(/fapi/v1/order, MARKET reduceOnly)` | `coordinate_close(coordinator,…, "SL_UNVERIFIED")` → `request_close` |
| `open_coin` likidasyona yakın | `binance._post(/fapi/v1/order, MARKET reduceOnly)` | `coordinate_close(coordinator,…, "LIQ_TOO_CLOSE")` → `request_close` |
| Borsa algo SL (place_algo_sl) | K1=B otorite (değişmedi) | K1=B otorite (değişmedi) |
| AUTO-HEAL stop_executor | `keep_position=True` (pozisyon KAPATMAZ) | aynı (kapanış değil) |

Kalan tek doğrudan `MARKET` close POST'u `coordinate_close`'un fallback dalındadır
(yalnızca `coordinator is None` iken; canlı `run_once` her zaman koordinatör kurar →
canlı yolda bypass YOK). Mekanik denetim (grep): `"type":"MARKET"` yalnızca satır
1043 (fallback); her iki `open_coin` sitesi `coordinate_close` çağırır; `request_close`
üretimde yalnızca satır 1033'ten (coordinate_close içinde) çağrılır.

## Merkezi kapanış mimarisi (uygulandı)
- `run_once`: `CloseIntentStore(risk.db_file)` (AYNI ledger, ikinci db yok) +
  `BinanceHBCloseAdapter(binance, hb)` + `CloseCoordinator(adapter, intent_store=…)`
  kurulur ve `open_coin`'e geçirilir.
- `coordinate_close(coordinator, …)`: tüm kapanışları `request_close`'a yönlendirir →
  tek idempotent, cross-process-korumalı, flat-confirmed reduceOnly kapanış; HB
  executor `keep_position=True` durdurulur; pre-sent emirler çözülür; UNKNOWN'da kör
  re-POST yok. Koordinatör yoksa (legacy/dry) doğrudan reduceOnly fallback (kapanış
  asla sessizce atlanmaz).
- Exchange SL fill otonomdur (K1=B); ikinci kapatma gönderilmez (idempotency + intent).
- TP/time-limit tetik mantığı YOK (yeni strateji değil); `request_take_profit/
  request_time_limit` mevcut, tetiklendiğinde aynı otoriteden geçer.

## Gerçek davranışsal RED/GREEN (canlı open_coin/run_once zinciri)
GREEN (5 test): GERÇEK `open_coin`, SL yerleştirme -2021 → acil kapanış dalı →
`request_close` ULAŞIR. Kanıt: HB `("E1", True)` durduruldu, TAM 1 reduceOnly POST,
pozisyon flat, cross-process intent `CLOSED`; ikinci tetik `IGNORED_ALREADY_CLOSED`
(idempotent, ikinci POST yok); fallback yalnızca koordinatörsüz; `run_once`
`CloseCoordinator` kurup `open_coin`'e geçirir.
Davranışsal RED: `coordinate_close` koordinatörü atlayacak şekilde geçici
zayıflatıldı → **3 test düştü** (log: "eski doğrudan reduceOnly kapanış"; HB
durdurulmadı; intent yok). Geri alındı → 5 passed. Testler yalnızca fonksiyon
varlığını değil, canlı zincirin `request_close`'a ULAŞTIĞINI kanıtlar.

tests/ **255 passed** (önce 250 → +5). Ruff `--select F,E9` temiz; py_compile temiz.

## Bağımsız denetim — PARTIAL (bağımsız DEĞİL)
reisfurkan-53 çevrimdışı; bu oturumda tek ajan uygulayıcıdır. Gerçek bağımsız
denetim yapılamadı. Mekanik/kaynak-temelli öz-denetim yapıldı (yukarıdaki grep
kanıtları + RED ispatı). **Bağımsız denetçi doğrulaması NO EVIDENCE** — kullanıcı
reisfurkan-53/ayrı bir denetçiyle bağımsız diff+çağrı-zinciri+regresyon incelemesi
yaptırmalı.

## Bilinen eksikler / açık riskler
- **Orta (artık risk):** Acil kapanış `request_close` ile yürür; teorik bir kenar
  durumda (yeni executor'ın giriş emri henüz terminal değilse) handoff UNRESOLVED_
  ORDER döndürüp kapanışı reddedebilir → pozisyon açık+korumasız kalır (ama site-1'de
  `risk.is_halted` set edilir, yeni giriş durur; fail-closed). Pratikte giriş 8s
  dolum+filled kontrolünden sonra FILLED (terminal) olduğundan bu dal nadirdir.
- HEDGE: open_coin yalnızca ONEWAY altında çalışır (run_once `_verify_oneway` ile
  gate); HEDGE SL oluşturma desteklenmez, fail-closed (değişmedi).
- Canlı Binance'e karşı KOŞULMADI (NO EVIDENCE). Üretim entegrasyonu kod düzeyinde
  tamam; canlı doğrulama ayrı.

## Kanıt etiketleri
- **VERIFIED:** canlı open_coin→coordinate_close→request_close zinciri; run_once
  wiring; tek-POST/HB-stop/intent-CLOSED/idempotency; bypass yokluğu (grep+RED);
  tüm regresyon; static.
- **PARTIAL:** bağımsız denetim (öz-denetim); canlı-yol iki-process (bileşen inc12.1'de
  kanıtlı, uçtan uca open_coin ile yeniden koşulmadı).
- **NO EVIDENCE:** gerçek Binance'e karşı uçtan uca; bağımsız denetçi doğrulaması.
