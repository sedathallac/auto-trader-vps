# FINAL_RELEASE_HANDOFF — FAZ9 kontrollü başlatma tek-akış devri (2026-11-21)

Durum: **BLOCKED — yalnız gerçek dışsal kapılar** (§8). Kod/kanıt işi bitti;
bu dört kapı operatör işlemidir ve bu görev yetki alanı değildir. Hüküm
§12 sözleşmesidir: READY_FOR_OPERATOR_START ancak 12 kriterin TAMAMI kanıtlı
olsa verilebilirdi.

## 0. Aday kimliği ve doğrulama

- Dal: `fix/deploy-candidate-security-20261120` (yalnız FF push; force yok).
- Nihai SHA: `git ls-remote origin fix/deploy-candidate-security-20261120`
  çıktısıdır; devir raporu (§13 formatı) bu SHA'yı ham-çıktı kanıtlarıyla
  (tam `tests/` koşumu, preflight, secret-scan; ham dosyalar `/tmp/…-<sha8>`
  adlarıyla) birlikte verir. BAYAT kayıt SHA'ları: b2911da, 265d433, cf3c32d.
- Commit'ler imzasızdır (imzalama anahtarı yok — dürüst bildirim); CI yoktur;
  kapı bu belge + operatördür.
- Üretim: `release/faz9-faz14-20261119` @ **108002c**, `is_halted=1`
  (sebep: daily_loss_pct %2.10 > %2 = 50.48 USDT eşiği) — bu görevde
  DOKUNULMADI; emir/istek/restart/HALTED-kaldırma YAPILMADI.

## 1. Bu zincirde ne bitti (tek paragraf)

BLOK10 P1 onarımları → BLOK11 bütçe/intent temeli → BLOK12 §1–§10 (tek
kullanımlık operatör onayı, intent-idempotency, api_guard, watchdog doğrulayıcı,
tek-süreç kilidi, devir belgeleri) → devam A–E (gönderim-anı gardları, SIGTERM
sıralaması, gerçek-fill denetimi, token izin denetimi, mutlak tavan + onay
sırası) → **devam F**: E1 gerçekleşen-doluş LIMIT pini (LONG matematiksel
tavan; SHORT fail-closed), E2 süreç-desti 418/429 gardı, E3 crash-penceresi
testi. Tam test/preflight sayıları yalnız §0'daki ham kanıtlardadır; bu
belgede "koşuldu ama sayısı burada" iddiası YOKTUR.

## 2. E1 sözleşmesi ve SINIRLARI (en kritik güvenlik maddesi)

- Doğrulama modunda giriş = **SABİT fiyatlı LIMIT** (`open_order_type 2` +
  top-level `entry_price` — canlı Hummingbot imajında doğrulanmış alan).
  P_cap = floor_tick(plan_fiyat × (1+allow)); A × P_cap ≤ 50 Decimal BİREBİR;
  aşan plan RED (sessiz kırpma yok). BUY limit kendi fiyatı üstünde dolmaz →
  LONG gerçekleşen quote ≤ A×P_cap (kısmi dolum/iptal dahil; Borsa A'yı
  aşamaz). Kaldıraç notional DEĞİLDİR (boyut A×fiyat'tan gelir).
- **SHORT varsayılan RED** (`SHORT-GARANTİSİZ`): realized quote hiçbir emir
  türüyle yukarıdan tavanlanamaz. `VALIDATION_ALLOW_SHORT_ENTRIES=true` yalnız
  operatörün GARANTİSİZ drift-zarfı iznidir. Üçüncü işlem daima RED.
- Validation KAPALIYSANIZ E1 YOKTUR (eski MARKET yolu). FAZ9 başlatması
  `CONTROLLED_LIVE_VALIDATION=true` olmadan YAPILAMAZ (runbook emri, §3/4).
- Kalıntılar (dürüst): POST↔token-tüketimi crash penceresi (emir üretimini
  tetiklemez — §4 testi); watchdog runtime kurulumu §8/2.

## 3. TEK-AKIŞ: FAZ9 kontrollü başlatma sırası (her adım = TEK operasyon)

Başlatma YALNIZ §8'deki dört kapı sırayla açıldıktan ve ayrı bir operatör
komutundan sonra. Dur-kuralı: beklenmedik tek gözlem → DUR; devam yeni onaydır.

1. **Heartbeat rotasyonu** (kapı §8/1) — tek işlem:
   `docs/OPERATOR_ROTATION_HEARTBEAT_20261121.md` İŞ PLANI 1–6 (değerler
   sohbete/terminale BASILMAZ; `read -rs`). Kanıt: yeni `/etc/auto_trader/
   heartbeat.env` `root:root 0600`; `HB_ADMIN_ENV_FILE=... python -I
   kill_spot_grid.py` (argümansız) → usage/exit1 ve ESKİ kimlik HB'de RED.
2. **Watchdog kurulumu** (kapı §8/2) — tek işlem: `deploy/orphan-watchdog.*`
   birim/cron ÖRNEKLERİnden birini seç kur (systemd VEYA cron — İKİSİ BİRLİKTE
   DEĞİL), ardından ayrı tek onayla `python -I tools/orphan_watchdog.py --once`.
   ÖNCE parite: `python -I tools/verify_watchdog.py <repo>` (User/
   WorkingDirectory/EnvironmentFile/STATE_DIR/python/kil alanlarını gerçek
   servisle kıyaslar). Kanıt: exit 0 + tur logu + `api_guard.json` dosya
   gardının tanındığı log. 418/429 aktifken tur ATLANIR → exit 2 (bilerek,
   istek YOK — devam-F §3 dosya gardı).
3. **Testnet devre testi** (kapı §8/3) — tek işlem (güveneli kurulum, değerler
   sohbet DIŞINDA): VPS'te root-ait 0600 env dosyası `TESTNET_API_KEY/
   TESTNET_API_SECRET` + 0600 işaret dosyası `TESTNET_ORDER_APPROVAL_FILE`;
   `tools/testnet_smoke.py` yalnız `testnet.binancefuture.com`'a gider ve çıktı
   **"ÜRETİM KANITI DEĞİLDİR"** bandı taşır. Kanıt: imzalı GET zinciri OK +
   tek testnet emri (varsa) + BLOK11 §9 blokajının kalktığı.
4. **BCH/GALA OOS** (kapı §8/4) — tek işlem: ETHUSDT-2025 önsonucu OOS DEĞİLDİR.
   `tools/h5_runner.py` bilinçli ağ-kapalıdır; 2020–2023 BCHUSDT/GALAUSDT
   arşivlerinin indirilmesi OPERATÖR ONAYI gerektirir (üretim Binance veri
   hattına istek = bu görevde yasak). Kanıt: B.5 dört kapısı YALNIZ 2020–23
   holdout'unda; pozitif beklenti yoksa READY İLAN EDİLMEZ.
5. **Aday kapısı (VPS, taze)**: `git fetch && git rev-parse origin/<dal>` =
   nihai SHA; izole HOME tam `tests/` + `deploy/faz9_preflight.sh . HEAD` +
   `python -I tools/secret_scan.py` — hepsi exit 0 (bu görevde tek sefer
   koşulmuş ham çıktı §§0/13 ile kıyasla).
6. **Başlat**: üretim `is_halted=1` KALIRKEN yalnız FAZ9 doğrulama penceresi
   env ile (`CONTROLLED_LIVE_VALIDATION=true`, 2/50/100) ve §1 tek-akışının
   ayrı operatör komutuyla. İlk işlem runbook+bütçe; ikinci işlem YALNIZ
   kalıcı tek-kullanımlık token (`operator_approval.py` grant; dosya 0600 —
   kod denetler); üçüncüsü daima RED.

## 4. Restart/restart-öncesi mutabakat prosedürü (position↔SL)

Restart KARARI ÖNCESİ salt-okunur sıralı kontrol (hepsi OK değilse restart
YOK; CRITICAL uyarı + HALTED kalır):
1. `position_risk()` → pozisyon listesi (FLAT mı, hangi sembol).
2. Her açık pozisyon için `open_algo_orders_for()` → az bir doğrulanmış
   STOP_MARKET algo SL (yoksa ÖNCE SL yerleştir/tamamla — restart sonra).
3. `~/.pending_fills.json` (şema 1 envelope) → bekleyen dolum var mı; varsa
   reconcile'ın (tick'ler arası) kimlik hasadı tamamlanması beklenir.
4. Bütçe (`validation_budget` DB status) + onay kapısı (`operator_approval`
   state) + `api_guard.json` cooldown dosyası → beklenen değerler.
5. Eşleşmezse: `verify_watchdog.py` + `orphan_watchdog --once` tek turları ile
   yetim SL/çift SL denetimi (watchdog yalnız FLAT+tracked+algoId-belirli
   iptal eder; belirsizde ASLA otomatik iptal YOK).
Duruş: SIGTERM → tek-süreç kilidi çözülür, in-flight iptal EDİLMEZ; servis
dışında elle tek-tur §8 gereği exit 3. `is_halted` yalnız operatörün ayrı,
bilinçli kararıyla ve yeni kanıt setiyle sıfırlanır — bu görevde sıfırlanmadı.

## 5. Acil durum haritası (kavramlar KARIŞTIRILMAZ)

| Olay | Ne olur | Ne DEĞİLDİR |
|---|---|---|
| Yeni giriş kilidi (api_guard/418, E1, bütçe, kapı) | Emir POST'LANMAZ | Pozisyon kapatma değildir |
| Exchange Algo SL | Borsada reduceOnly STOP_MARKET; tek SL otoritesi | HB barrier'ı değildir (K1=B: bariyerler None) |
| `stop_executor(keep_position=True)` | HB executor durur = POSITION_HOLD | **Pozisyon kapatma DEĞİLDİR** |
| Gerçek kapatma | CloseCoordinator reduce-only; flat DOĞRULANIR | executor stop'u değildir |
| SL iptali | yalnız FLAT+tracked+algoId-belirli (watchdog) | belirsiz eşleşmede otomatik YOK |
| 418/429 | Tüm süreçler cooldown dosyasından susar | kör deneme/ban uzatma YOK |

## 6. Ortam gerçekleri (salt-okunur anlık görüntü, 2026-11-21)

- Bu makinede faz9/watchdog systemd birimi YOK; çalışan auto_trader süreci YOK.
- `~/risk_ledger.db`: is_halted=1 (yukarıdaki sebep); position_reservations 0,
  close_intents 0, trade_ledger 0, sl_placements 2 (2026-11-12 PLACED kayıtları
  — eski dönem; silinmedi/dokunulmadı).
- `~/.pending_fills.json`: TEK miras kayıt "E1" (SOLUSDT, target_notional
  **153.0** — BLOK12-öncesi dönemin 50-üstü boyutlandığının canlı kanıtı;
  deadline geçmişte). Dokunulmadı; §4/3 prosedürüyle değerlendirilecek.
- 6 kardeş worktree temiz (0 dirty/untracked) — worktree kazası sonrası
  birebir geri kurulum doğrulandı.
- `~/hb-credentials` ve `~/hummingbot-api-credentials`: **0775** (grup/diğer
  okuyabilir) — rotasyon (kapı §8/1) bunları 0600 root modeline taşır.
- TESTNET_* değişkeni 0; BCH/GALA arşivi YOK → kapılar §8/3–4.

## 7. Kalan riskler (kabul edilmiş, belgeli)

1. POST↔consume crash penceresi: token yakılmadan süreç ölebilir; emir
   tekrarı ÜRETİLEMEZ (testli), yeniden grant operatör işidir.
2. `VALIDATION_ALLOW_SHORT_ENTRIES=true` = garanti-üstü tercihtir; default
   bozulmadıkça SHORT giriş yok.
3. Validation kapalı çalıştırma E1'sizdir — runbook yasaklar (§3/5-6).
4. İzole koşumda 2 sıra-bağımlı test HEAD'den kalma kırmızıdır (LIVE_READINESS
   §10.4 notu); tam-süitte yeşildir.
5. Commit imzası ve CI yok; kapı insan + bu belgedir.

## 8. HÜKÜM: BLOCKED — dışsal kapılar (her biri TEK operasyon, §3'te)

| # | Kapı | Tek operatör işlemi | Beklenen kanıt |
|---|---|---|---|
| 1 | HB heartbeat rotasyonu | Rotation doc iş planı 1–6 | Yeni 0600 env; eski kimlik HB'de RED |
| 2 | Watchdog runtime | Orphan-watchdog kur + parite + tek `--once` onayı | exit 0 + tur logu |
| 3 | Testnet | TESTNET_* güvenli env + 0600 işaret + smoke | "ÜRETİM KANITI DEĞİLDİR" bandı + OK zincir |
| 4 | BCH/GALA OOS | 2020–23 arşiv indirme onayı/işlemi (veri hattı üretim isteğidir) | B.5 holdout hükmü (h5_runner, ağ-kapalı araç) |

Kapılar açıldığında tek sonraki komut §3/6'daki kontrollü başlatmadır; bu
belge kendi kendine canlı başlatma yetkisi DEĞİLDİR.
