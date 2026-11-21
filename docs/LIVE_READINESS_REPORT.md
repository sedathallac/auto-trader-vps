# LIVE_READINESS_REPORT — BLOK10 (FAZ9 bot onarımı, canlı teste hazırlık, kanıtlı teslimat)

Tarih: 2026-11-20 · Dal: `fix/deploy-candidate-security-20261120` · Aday SHA: **b2911da**
Üretim referansı: `release/faz9-faz14-20261119` @ **108002c** (dokunulmadı; worktree temiz, 0 değişiklik)
Aday = üretim + 16 commit (7 BLOK4/5/6 kapsam + 3 BLOK10 §3/§4/§6 + 6 BLOK10 §5 P1 onarımı)
Durum: **READY FOR OPERATOR REVIEW — NOT DEPLOYED**

> §0 gözetildi (kanıtlar §7'de): HALTED üretim değişmedi · `is_halted` sıfırlanmadı ·
> canlı hesaba tek API isteği/emir YOK · servis start/stop/restart YOK · üretime
> merge/deploy YOK · konfig/kimlik değişmedi · H5 koşmadı/eşdeğeri üretilmedi ·
> testler yalnız izole tmp/fixture · başarısız test gizlenmedi (§6 kırmızı koşumlar
> ham haliyle kayıtlı) · kullanıcı değişiklikleri korunmadı-değil, dokunulmadı ·
> terminal çıktısına/git'e credential YAZILMADI (§8.5 P2-C notu).

---

## 8.1 Teknik hazırlık durumu

| Bileşen | Durum | Kanıt |
|---|---|---|
| `validation_budget.py` — 2×50/100 sert tavanlı, bağımsız, kalıcı SQLite bütçe | UYGULANDI (e4eb9a8) | 28 birim test; §8.2 haritası |
| Giriş zinciri bağlantısı (reserve→submit→confirm/release) | UYGULANDI (e4387d7) | 7 gerçek `open_coin` entegrasyon testi |
| Bayat VPS kanıt scriptleri → `evidence/` + kök pytest yalıtımı | UYGULANDI (de1393a) | `evidence/README.md`; 8 kök FAILED'ın üretimde de var olduğu worktree ile kanıtlandı |
| 6 × P1 onarım (F-A…F-G) | UYGULANDI (2902de0…b2911da) | 34 yeni test; her commit'te kırmızı→yeşil kaydı |
| Testler | **1131 passed, 9 subtests, exit 0** | `/tmp/blok10_full_after_fixes.log` (110.83s) |
| Push | FF `e3506cb..b2911da`, remote HEAD doğrulandı | `git ls-remote` = b2911da |
| Deploy | **YAPILMADI** (yetki operatörde) | — |

### BLOK10 §5 onarımları (hepsi bağımsız kod-okumasıyla DOĞRULANDI; TDD)

| ID | Bulgu (kök neden) | Onarım | Commit | Test |
|---|---|---|---|---|
| F-A | `_faz6_confirm` istisna+compare yolunda `_LAST_CONFIRM`'a ÖNCEKİ sembolün `effective_side`'ını bırakıyordu → `open_coin:3480` canlı yön çevirme riski | `clear()` girişe taşındı; boş tüketici fail-closed | 2902de0 | 4 test (staleness) |
| F-C | `open_coin` ATR None → uydurma `%2` geometriyle CANLI emir (SL/TP+boyut girdisi hayalî) | canlıda RED; dry_run/compare fallback aynen | 9a40d6d | 3 test + harness `_get` onarımı |
| F-F | K-3 PLACED satırı exchange'te olmayan SL → sonsuz BUSY → **kalıcı SL korumasızlığı**, sessiz | PLACED+güvenilir ABSENT+re-map boş → release+yeniden yerleştir; PENDING BUSY kutsal | a42ca1f | 5 test (2'si muhafa çapası) |
| F-B | `CloseIntentStore.reconcile()` üretimde hiç çağrılmıyordu → flat sonrası eski ACTIVE intent yeni dönemi sonsuz STALE→FAILED_SAFE yapar (kapatma kilidi) | flat dalında `reconcile(exchange_flat=True)`; DB hatası fail-closed | 16b0007 | 7 test |
| F-D | orphan_killer: sorgu hatası "yetim yok" sayılıp sembol tracked'den DÜŞÜYORDU (kalıcı kör) + heartbeat bekçisinin dönüşü çöpe gidiyordu | `errored` kümesi: doğrulanmayan tracked'de kalır; bekçi sonucu CRITICAL kanıtlı, temizlik bilinçli sürer (gerekçe commit'te) | 9ecbd94 | 5 test |
| F-G | archiver watermark'ı fetch hatasında/error-dict'te/sayfa-limitinde ilerletiyordu → kanıt DB'sinde telafisiz boşluk | `sync_ok` kapısı; yalnız tam+güvenilir senkronda ilerler (idempotent yeniden tarama) | b2911da | 10 test |

Doğrulanıp **kod değişikliği gerektirmeyen** iddia: restart'ta bayat pending düşümü —
`_is_dangerous_record` (auto_trader.py:4357+) tüm finalizasyon/confirm belirsiz
kayıtlarını koruyor; kalan düşüm yalnız slot/SL taraması exchange gerçeğinden okunduğu
için riski düşük (P2, literatürde kaldı).

## 8.2 Limit uygulaması haritası (2 işlem / 50 USDT işlem başı / 100 USDT toplam)

Sert tavanlar koda gömülü; **env yalnız SIKILAŞTIRABİLİR**, gevşetemez/uyduramaz:

```
validation_budget.py:42-44   HARD_MAX_TRADES=2 · HARD_MAX_PER_TRADE_USDT=50.0 · HARD_MAX_TOTAL_USDT=100.0
validation_budget.py:69-103  Config.from_env(): bayrak kapalı→(enabled=False,valid=True);
                             geçersiz/aşırı override → valid=False → ÇAĞIRAN TIKLAR (fail-closed)
validation_budget.py:203-281 reserve(): BEGIN IMMEDIATE(220) içinde sırayla
                             BAD_NOTIONAL → idempotens(aynı anahtar tüketmez) →
                             PER_TRADE_CAP(236) → TRADE_COUNT_CAP(245) → TOTAL_NOTIONAL_CAP(249)
validation_budget.py:288     confirm(): RESERVED→CONSUMED (yalnız executor görüldükten sonra)
validation_budget.py:294-    release(): YALNIZ RESERVED→RELEASED; CONSUMED HİÇ iade edilmez
validation_budget.py:120-    WAL + synchronous=FULL + busy_timeout → restart-kalıcı sayaçlar
```

Giriş zincirindeki yerleşim (`auto_trader.py`, open_coin gövdesi):

```
auto_trader.py:24        import validation_budget
auto_trader.py:3326-3396 _validation_control/reserve/confirm/release yardımcıları
                         (lock'lu lazy kurulum; BudgetError→(False,...)=redder)
auto_trader.py:3405      HB_OFFLINE/global giriş kapısı → BÜTÇEDEN ÖNCE (kapalıysa sayacı dokunmaz)
auto_trader.py:3800      merkezi risk kapısı _risk_entry_gate → BÜTÇEDEN ÖNCE
auto_trader.py:3817      _validation_reserve(symbol, final_notional)  ← SON AND
auto_trader.py:3818-3822 ret → slot serbest ("validation_budget") + return False (POST YOK)
auto_trader.py:3824      hb.create_executor(...) — TEK gönderim noktası, bütçeden SONRA
auto_trader.py:3839      kesin-başarısızlıkta _validation_release (UNKNOWN'da ÇAĞRILMAZ→hak kalır)
auto_trader.py:3851      _validation_confirm — executor kimliği görüldü → CONSUMED kalıcı
```

Garantiler (test kilitleri): bayrak kapalıyken akış BİREBİR aynı (depo hiç oluşmaz);
3. işlem reddedilir; 50.01 reddedilir, 50.00 geçer; işlem-kapat-yeniden-açılış 2 hakkı
tüketmeye devam eder (iade YOK); retry aynı idem-anahtarı yeniden tüketmez; ACK-belirsiz
UNKNOWN hakkı korur; HALTED/risk-red bütçeye dokunmaz; eşzamanlı 3 thread'den yalnız 2'si
kazanır; bozuk DB dosyası BudgetError→giriş reddi.

**"İkinci işlem birinciyi asla otomatik izlemeyecek" çift katmanlı**: (1) kod — 2 işlem
tavanı üçüncüyü engine seviyesinde keser; (2) süreç — §8.4 runbook ikinci işlem için
AYRI operatör onayı ister; bütçe bunu otomatikleştirmez.

## 8.3 Ön koşullar ve geri alma (BELGE — hiçbir komut bu görevde çalıştırılmadı)

Ön koşullar (her biri ayrıca operatör onayı):
1. Operatör bu raporu + §6 ham logları okumuş olmalı.
2. Yedek: `docs/YEDEK_MANIFEST_20261120.md` + `faz9_raw_manifest.py` (BLOK9) — ham
   veri yedeği teyitli olmadan deploy adımı önerilmez.
3. Kimlik-hijyeni: `kill_spot_grid.py:10` ve `kill_spot_grid_c0.sh` içinde gömülü
   HB yönetici kimliği DURUYOR (tarihî, git'te; bu görev dosyaya dokunmadı — operatör
   aleti). Öneri: parola rotasyonu + env'e taşıma (değerler bu raporda YOK).
4. Servis durdurma/çalıştırma, `.env` düzenleme, VPS erişimi: tamamı operatörde.

Geri alma (yalnız dokümante; çalıştırılmadı):
- Kod geri alma = deploy edilmediği sürece no-op; edilmişse üretim dalı referansı
  `108002c`'ye `git checkout` + servis restart prosedürü `deploy/faz9_gridc0_deploy.sh`
  başlığındaki mevcut işlemdir (BU GÖREV KOŞTURMADI).
- Bütçe sıfırlama YOKTUR ve istenmez: `validation_budget.db` satırları denetim
  tarihidir; limitleri gevşetmenin yolu yoktur (sert tavan).
- Canlı pozisyon/plan acil kapatma: mevcut CloseCoordinator tek yetki; K-1 exchange-SL
  otoritesi değişmedi.

## 8.4 Kontrollü canlı test runbook'u (11 adım — operatör yürütür)

Test bütçesi: **2 işlem, ≤50 USDT/işlem, ≤100 USDT toplam.** Herhangi bir adım
beklenmedikse DUR; devam kararı yeni bir operatör onayıdır.

1. **Hazırlık onayı**: bu rapor + §6 logları kabul; yedek teyidi (§8.3/2).
2. **Dal/derleme kontrolü**: `git fetch && git rev-parse origin/fix/deploy-candidate-security-20261120` = b2911da beklenir.
3. **Test kapısı (VPS, izole HOME)**: `HOME=$(mktemp -d) <venv>/python -I -m pytest tests/ -q -p no:cacheprovider` → **1131 passed exit 0** beklenir.
   ⚠️ Deployment script kapısı (`deploy/faz9_gridc0_deploy.sh:100`) kök `pytest` çalıştırır ve 8 BAYAT testle kırılır (üretim 108002c'de de aynı 8 FAILED — worktree'de kanıtlı, BLOK10 §6); deploy'u script değil bu kapı + operatör yürütsün.
4. **Env (test penceresi için)**: `CONTROLLED_LIVE_VALIDATION=true`;
   `MAX_VALIDATION_TRADES=2`, `MAX_VALIDATION_POSITION_NOTIONAL_USDT=50`,
   `MAX_VALIDATION_TOTAL_NOTIONAL_USDT=100` (set edilse de edilmese de sert tavan
   2/50/100'dür; geçersiz değer = tüm girişler reddedilir).
5. **Bütçe deposu taze kontrol**: `validation_budget.db` YOKSA ilk test; VARSAYSA
   `trades_used` ≤2 ve geçmiş test hakları tükenmiş olabilir — sıfırlama aracı
   BİLİNÇLİ YOK; karar operatöründür (dosya silmek = geçmiş testi kapatır).
6. **HALTED/risk kapısı denetimi**: canlıya almadan önce `is_halted` ve giriş kapısı
   DURUMunu operatör kendisi doğrular (bu görev `is_halted`e DOKUNMADI; bütçe bu
   kapılardan SONRA konuşlanır — kapılar kapalıyken sayaç işlemez).
7. **Tek gözlem turu**: bot çalışır, ilk `doğrulama bütçesi` reserve logu izlenir.
   İlk executor oluştuysa → `CONSUMED` + `executor=` logu; bütçe durumundan teyit.
8. **DUR + İNCELE (birinci→ikinci işleme GEÇİŞ YOK)**: ilk işemin açılışı, SL
   yerleşimi (`sl_placements` PLACED + algoId doğrulaması) ve ilk kapanışa kadarki
   davranış İNSAN tarafından incelenir. İkinci aday, ilk kapandıktan VE operatör
   "ikinci işleme onay" dedikten SONRA değerlendirilir — kod bunu otomatikleştirmez.
9. **Üçüncü aday gözlemi (negatif kontrol)**: logda `atlandı: doğrulama bütçesi
   (TRADE_COUNT_CAP)` görülmeli; `create_executor` POST'u YOK.
10. **Kapanış kanıtları**: `validation_budget.db` durumu (trades_used=2,
    notional_used ≤100), `validation_reservations` satırları (hepsi CONSUMED),
    arşiv DB'sinde fill'ler (F-G kapısı sayesinde eksik-pencere UYARISI olmadan).
11. **Raporlama**: 11 maddelik BLOK10 §11 tablosu operatör kararına eklenir;
    limit aşımı/HALTED/beklenmedik durum → test DURDURULUR, bulgu BLOK11'e devir.

## 8.5 Fail-closed modları ve artık riskler

Fail-closed (kodda kanıtlı): geçersiz/eksiz limit → tüm girişler ret · bozuk bütçe DB →
ret · risk/HALTED/kapalı kapı → bütçeye dokunmadan ret · UNKNOWN create_executor →
hak KORUNUR (iade yok) · CONSUMED iade edilmez · PLACED-but-absent → yeniden
yerleştirme (PENDING BUSY hariç) · close-intent flat → CLOSED; DB hatası → FAILED_SAFE ·
archiver hata → watermark durur · orphan sorgu hatası → tracked kalır · ATR yok →
canlı giriş yok.

Artık riskler — **kod değişikliği YAPILMADI, gerekçeli**; durum etiketleri:
[V]=kod-okumasıyla doğrulandı, [U]=alt-ajan iddiası doğrulanmadı:

1. [V] 429/418 rate-limit dallanması yok (`grep -c "429" auto_trader.py` = 0). Piyasa
   verisi GET'leri hata-dict→kapı-lar fail-closed'dur; POST yeniden-gönderimi zaten yok.
2. [V] pidfile/tek-örnek muhafası yok; çift süreç koruması K-3/close-intent/C6
   rezervasyon defterlerinden gelir (paylaşımlı SQLite BEGIN IMMEDIATE).
3. [V] SIGTERM handler yok; dayanıklılık flock+fsync+os.replace+IMMEDIATE tx ile
   yazım-atomikliğinden gelir; POST anında kill → UNKNOWN yolları test-li.
4. [V] `_dedupe_algo_sl` "STOP" filtresi TRAILING_STOP_MARKET'i de sayar; bot hiç
   trailing üretmediği için pratik yol kapalı — elle-müdahale senaryosu.
5. [U→P2] stale-data (bayat kline) girişi için bar-yaşı kapısı yok; REST senkron
   çekimde risk düşük, FAZ4-ölçeğinde izleme önerisi.
6. [U→P2] ATR/volume hesapları kapanmamış son barı içerir (canlı≠backtest sapması —
   BLOK4/5 harness≠canlı bulgusuyla tutarlı); davranışsal değişiklik bu adayın
   dondurulmuş kapsamı DIŞINDA.
7. [V] kill_spot_grid* gömülü kimlik (§8.3/3) — rotasyon operatörde.
8. [V] `ensure_sl_coverage` koruyucu ATR fallback'i (%2) bilinçli korundu: açık
   pozisyona muhtemelen-iyi SL'nin alternatifi korumasızlık; giriş yolu F-C ile kapalı.

## §6 Test koşum günlüğü (tüm BLOK10)

| # | Komut | Ağaç | Sonuç | Süre/Exit | Log |
|---|---|---|---|---|---|
| 1 | `pytest tests/ -q` | e3506cb (taban) | 1063 passed, 9 subtests | exit 0 | `/tmp/blok10_pytest_tests_dir.log` |
| 2 | `pytest tests/ -q` | e4387d7 (bütçe+wire) | 1091 passed | exit 0 | `/tmp/blok10_pytest_wired.log` |
| 3 | `pytest tests/ -q` | de1393a+entegrasyon | 1098 passed, 9 subtests | exit 0 | log dosyası kalmadı; sayı e4387d7 commit mesajında |
| 4 | `HOME=$(mktemp -d) pytest . -q` (kök) | de1393a | 8 FAILED / 1299 passed | exit 1 | FAILED listesi commit/değişiklik notunda |
| 5 | aynı kök koşum | e3506cb worktree | 8 FAILED / 1264 | exit 1 | AYNI liste → önceden-var olan kanıtı |
| 6 | aynı kök koşum | 108002c worktree | 8 FAILED / 1224 | exit 1 | üretimde de var → deploy-kapı bulgusu |
| 7 | 6 yeni fix modülü (fix ÖNCESİ) | b2911da öncesi ağaç | **17 FAILED** / 16 passed | 84.6s | `/tmp/blok10_fixes_green.log` (kırmızı anıtlı) |
| 8 | 6 yeni fix modülü (fix SONRASI) | fix'li ağaç | 32 passed +1→10 arşiv rerun | exit 0 | `/tmp/blok10_arch_green.log` |
| 9 | hedef regresyon 17 modül | fix'li ağaç | 227 passed, 9 subtests | 5.08s exit 0 | `/tmp/blok10_targeted_reg.log` |
| 10 | `pytest tests/ -q` TAM | fix'li ağaç | **1131 passed, 9 subtests** | 110.83s exit 0 | `/tmp/blok10_full_after_fixes.log` |

Önceki §4 negatif senaryolarının ham çıktıları 28+7 testlik BLOK10-öncesi turlarda
(koşum 2-3 logları) canlı tutuldu; hiçbir başarısızlık atlanmadı/gizlenmedi — #4-6'daki
8 FAILED önceden-var olan bulgu olarak raporlandı ve düzeltme yerine §8.4/3'te doğru test
kapısıyla belgelendi (test silinmedi, `evidence/` arşivine `git mv` ile tarihçe korundu).

---

## 9. BLOK11 ekleri (2026-11-21) — semantik değişimler ve yeni operatör adımları

1. **BÜTÇE SEMANTİĞİ DEĞİŞTİ (N2, §10.3 — BLOK10 §8.3 satırı geçersiz):**
   `CONTROLLED_LIVE_VALIDATION` kapalı/bozuk iken artık GİRİŞ YOK (eski davranış:
   akış değişmezdi). Açılış sırası: bayrak doğru-`true` → limitler geçerli → DB
   açılabilir → rezerv. Kapalı ortamda canlı giriş denemesi logda
   `BÜTÇE-KAPALI:` ile RED edilir; doğrulama bütçesi DB yolu
   `VALIDATION_BUDGET_DB` ile taşınabilir (test/izolasyon; varsayılan STATE_DIR).
2. **YETİM-SL WATCHDOG PLANLANABİLİR (N1):** `tools/orphan_watchdog.py --once`
   (flock, exit 0/1/2; anahtar yoksa TEK İSTEK YOK). Kurulum ÖRNEKLERDEN
   (deploy/orphan_watchdog_faz9_live.{service,timer}.example veya cron) —
   OPERATÖR kararı; `Environment=HOME` canlı servisle BİREBİR aynı olmalı.
3. **KAPANIŞ CID (012200f):** her kapanış POST'u `fz9c<SYMBOL><ms>` taşır; ACK
   kaybı emir query ile telaf edilebilir kimlik verir. Retry aynı CID'yi
   üretmez; Binance red verirse FAILED_SAFE (bilinçli güvenli taraf).
4. **H5 ÖNSONUÇ:** koşucu/gard/motor BLOK11'de kuruldu (tools/h5_*); yalnız
   PRELIMINARY/PARTIAL etiketli ETHUSDT 2025 önsonucu üretildi — B.5 kapı hükmü
   DEĞİL (detaylar MISSION_STATE BLOK11 §3 ve audit `h5_runs/` özetleri).
   BCH/GALA verisi ve 2020-23 birincil OOS kararı OPERATÖR onayı bekliyor.
5. **TESTNET SMOKE:** `tools/testnet_smoke.py` hazır/koşulmadı — TESTNET_*
   anahtarı yok; üretim anahtarı ortamda iken kendini RED'ler (kanıtlı).

## HÜKÜM — BLOK10 (BLOK11 §9 ekleriyle geçerli)

**READY FOR OPERATOR REVIEW — NOT DEPLOYED.** Altı bağımsız doğrulanmış P1 onarıldı ve
testlendi; 2×50/100 bütçesi bağımsız fail-closed kontrol olarak üretim giriş zincirine
takılı; üretim `108002c` HALTED ve dokunulmamış; canlı hesapla tek etkileşim yok. Deploy,
HALTED kaldırma, H5 ve ikinci-işlem geçişi dahil her canlı adım AYRI operatör onayına
muhtaçtır. `Düzeltildi/geçti/güvenli` yükümlülüklerinin tümü yukarıdaki dosya:satır ve
log kanıtlarına bağlıdır.
