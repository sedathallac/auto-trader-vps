# MISSION STATE — FAZ9 Mission C (2026-11-19) — KALICI ONARIM SERISI

Kural seti (§0 DEĞİŞMEZ) yürürlükte: halt'a/ sayaçlara/ledger'a dokunulmadı;
canlı emir yok; production deploy yok; Health17 merge/deploy yok; BACKFILL=1 yok;
418/429 → anında dur; her yanıtta X-MBX-USED-WEIGHT-1M loglandı (stderr, W1M=);
sembolsüz ağır uç-nokt Çağrılmadı; tek geçiş/sembol-başına-tek-deneme;
kaynak etiketi her sayıda; zorla eşleştirme yok.

## KAYNAK ETİKETLERİ (bu dosya)
- [FILL] userTrades 220 fill (trades_fromid_fix, fromId-keyset) — TEK gerçekleşen-PnL
- [ALGO] GET allAlgoOrders/openAlgoOrders ham çekim `algo_orders_20261119.ndjson`
  (42 kanıtlı sembol × 5g×3 pencere + open; RC=0; 418 YOK; W1M son=342) — EXCHANGE defteri
- [ORD] allOrders 118 (yalnız REGULAR emirler — koşullular burada HİÇ görünmez)
- [JRN] VPS journalctl (salt-okur) — bot-yanı ackler
- [HB]/[LED] önceki bölümlerden (etiketsiz birleştirme yok)

---

# FAZ 0 — F20 YANLIŞLAMASI: **SONUÇ = REFUTED (F20 yanlış; ALGO-SL VAR ve ÇALIŞMIŞ)**

## F24 (F20'nin iptali — koşullu emirler Algo Service'te; 0.1–0.5 tamam)
Bağlam (doğrulandı): USDⓈ-M koşullu emirleri 2025-12-09'dan beri Algo Service'te;
/fapi/v1/order'a STOP_MARKET gönderimi −4120 STOP_ORDER_SWITCH_ALGO döner; bunlar
**GET allOrders'ta asla görünmez**. Mission B'nin "SL/TP yok" çıkarımı yalnız
[ORD]'ya dayalıydı → **kanıt yükü karşılanmamıştı**.

0.1 KOD: production 108002c (ve 01229ce, baseline 8434dfd 11-09'dan beri) SL'yi
`place_algo_sl` ile **POST /fapi/v1/algoOrder**'dan gönderiyor (algoType=CONDITIONAL,
STOP_MARKET, reduceOnly=true, quantity; workingType GÖNDERİLMİYOR → varsayılan
CONTRACT_PRICE). close_coordinator K1=B: "exchange reduceOnly STOP_MARKET algo
order tek SL"; HB'nin kendi kapatmaları devre dışı (8b0844b). Kurulu Hummingbot
**20260922** (konteyner /opt/conda/.../site-packages/hummingbot): connector
kaynaklarında `algoOrder` **HİÇ yok** → HB connector'ı SL gönderemez; gönderse
−4120 alırdı — [JRN]+HB loglarında **−4120 = 0, STOP_ORDER_SWITCH_ALGO = 0**.
mark_placed YALNIZ ack (algoId) ile yazılıyor → DB'deki PLACED, exchange ack'i demek.

0.2 LOG/MEKANİZMA [JRN]: "SL gönderildi … algoId=… → OPEN_PROTECTED" satırları;
OPEN_UNPROTECTED=0, "konulamadı"=0. Günlük 11-15 19:29'dan önceki app-logları
retansiyon nedeniyle yok → 30 satır görünüyor. **journal algoId ↔ [ALGO] kesişimi
65/65** (tüm ack'ler exchange geçmişinde; 11 kayıt retansiyon-öncesi).

0.3 ÇEKİM [ALGO] (tek geçiş, 42 sembol, 0 hata): **76 CONDITIONAL STOP_MARKET**
(06:50Z 11-19 itibarı): **20 FINISHED** (hepsinde actualOrderId+actualPrice+actualQty),
56 CANCELED (0'ında actualOrderId var), algoType ALGO-TP **YOK**, openAlgoOrders=0/42
(0 pozisyonla tutarlı); reduceOnly 76/76; closePosition=true yalnız 3;
workingType=CONTRACT_PRICE 76/76; positionSide=BOTH (ONEWAY); quantity==0 olan 3 kayıt
(rounding artefaktı — erken dönem). [ALGO] dedupe anahtarı algoId; ham saklandı.

0.4 SINIFLANDIRMA (220 fill → orderId join; UNKNOWN=0, LIQUIDATION/ADL=0 — force
orderId izi yok):
- GİRİŞ 113 fill: 110 ORDER-MARKET + 3 ORDER-LIMIT [ORD] (HB girişleri)
- ÇIKIŞ 107 fill: **51 = ALGO-SL** (20 FINISHED algo'nun actualOrderId'si;
  **Σrp=−55.0972** [FILL]) + **56 = ORDER-MARKET** reduceOnly HB-kapanışları
  (Σrp=+33.9863 [FILL]). Mutabakat: −55.0972+33.9863 = **−21.1109** ≡ income RP ✓
- SL slippage |actual−trigger|/trigger: medyan %0.0099, max %0.0867 — tetikleme
  hassas; kayıp slippage'tan değil, stop MESAFESİNDEN geliyor.
- ALGO-SL kapanış listesi (sembol tetik@zaman rp): SOL 11-11 −0.25 | BTC 11-13 −0.75 |
  SOL 11-13 −0.31 | WLD 11-15 −2.85 | NEAR 11-15 −2.47 | KAVA 11-17 −4.34 |
  ICP −2.67 | RARE −7.07 | INJ −3.03 | ASTER −2.73 | TAO −2.11 | UNI −2.13 |
  AAVE −1.94 | LINK −1.94 | AVAX −2.08 | ETH −2.08 | DOGE −1.72 | TRUMP −3.99 |
  PENGU −6.65 | ADA 11-18T22:48:26Z −3.975 (F21'deki ledger'a-yazılmamış küme).
- Korumasız giriş kümeleri: 60 giriş-kümesinin 6'sı [-1h,+2h] penceresinde algo'sız:
  XRP 11-08, SOL+JCT+BTC 11-09 (SL kodu ilk algo 11-09 10:27'den ÖNCE), XRP 11-15,
  SOL 11-11 (SL tetiklemesiyle aynı-dk yeniden giriş; sonraki SL geç) → koruma
  54/60; boşluklar yoğunlukla 11-08/09 ilk günlerinde.

0.5 YENİ YORUM — "exit saatler önce, HB neden bilmiyordu": pozisyonu **exchange'teki
algo SL kapattı** (ALGO-SL fill'leri); HB executor'ı kapanış yolunu ALGO_UPDATE
tüketmediği için görmedi → executor açık kaldı, geç "SYSTEM_CLEANUP" kaydı net=0.00
ile yazıldı. Matched SYSTEM_CLEANUP kümelerinin **16/26'sı [ALGO]-SL tetiklemesi**,
9'u HB-MARKET, 1'i APPROX HB-MARKET; öksüz kümelerde 3 ALGO Σ−11.79. Yani kök-neden
"bot düşünce korumasız kaldı" DEĞİL; **"koruma çalıştı ama HB/ledger kapanışı hiç
görmedi"** (F21 ile birleşince muhasebe-körlüğü). 20 SL tetiklemesinin tamamı
birbirinden bağımsız pozisyon; eşzamanlı crash yok.

## F20 KARARI (resmi)
**REFUTED-ALGO-EXISTED.** Exchange-tarafı SL koruması VARDI (76 algo emir, 30+ ack,
20 gerçek tetiklenme, −55.10 realize), TP ise exchange'te HİÇ var olmadı (0 algo-TP;
0 HB-LIMIT-TP çıkışı — TP tamamen HB-market kapanışı; 3 LIMIT yalnız girişte).
Mission B düzeltmeleri: "16 PLACED hayalet" YANLIŞ (hepsi ack'li gerçek algo);
"tüm risk yazılım-içi/pozisyonlar sahipsiz" YANLIŞ (54/60 giriş exchange-SL'liydi);
"SYSTEM_CLEANUP zararının mekanik nedeni korumasızlık" YANLIŞ (neden: kapanışın
HB'ye görünmemesi + SL mesafesi). DOĞRU KALANLAR: TP exchange'te yok; 6/60 koruma
boşluğu (ilk günler + bir yeniden-giriş); workingType=CONTRACT_PRICE (mark değil);
qty=0 algo'lar; PLACED satırlarında algoId/clientAlgoId SAKLANMIYOR (FAZ1.3'ün
gerekçesi); ALGO_UPDATE akışı yok (FAZ1.5).

DÜZELTİLEN BELGELER: FINAL_EVIDENCE_REPORT_FAZ9B_20261119.md (S2/S6/S8/S10 + Top-5
#1 + §0 etiket tablosu), MISSION_STATE_20261119B.md F20/F23 düzeltme notları.
Faaliyet kod-değişikliği yalnız audit tooling'inde (faz9_audit_collector.py:
algo_orders fazı + W1M logu) — audit alanı Git-DIŞI kuralı gereği commit yok;
FAZ1 kodu yeni dalda commit'lenecek.

SONRAKİ: FAZ 1 — fix/exit-protection-faz18-20261119 (algo client sertleştirme:
MARK_PRICE, closePosition/quantity doktrin-uyumu, ack+openAlgoOrders ikili doğrulama,
fail-closed, watchdog, ALGO_UPDATE, testnet-only). Operatör onayı gereken hiçbir
adım atılmadı.

---

# FAZ 1 RAPORU — FAZ18 çıkış koruması (2026-11-19, claude)

Dal: `fix/exit-protection-faz18-20261119` @ **1971059** (base 5134453 Health17),
worktree `/home/reisfurkan/faz9-faz18`. PUSHED; MERGE/DEPLOY YOK; üretim kodu
(108002c) ve canlı DB/hesap DEĞİŞMEDİ. Kanıt etiketi: BOT-SUITE.

**1.1 Wire doktrini:** `place_algo_sl` closePosition=true ⇒ quantity/reduceOnly
BİRLİKTE GÖNDERİLMEZ (ValueError kilidi, istek gitmeden); `workingType=MARK_PRICE`
(F24 kalan kuslunu 76/76 CONTRACT_PRICE'i bu yolla düzeltir — canlıya sürülünce);
`newClientAlgoId` ≤36; `get_algo_order` (GET /fapi/v1/algoOrder, algoId VE
clientAlgoId). `place_algo_tp` TAKE_PROFIT_MARKET+closePosition+MARK_PRICE olarak
yazıldı ANCAK `RISK_CONFIG["faz18_tp_algo_enabled"]` **varsayılan OFF** — TP
çıkış yolunu değiştirir; açma kararı operatörde (politika kapısı testli).

**1.2 Fail-closed:** `verify_sl_ack` üç-durum (OK/MISSING/UNKNOWN). Yıkıcı
kapanış (reduceOnly MARKET) + `sl_protection_events` PROTECTION_FAILED
(yalnız-ekleme tablo) + giriş kilidi YALNIZ **kanıtlı MISSING**'de. −4120:
UNPROTECTED, PLACED satırı YOK, KAPATMA YOK (borsa emri açık reddetti). GET
taşımayan boundary → UNKNOWN → koşulsuz kapatma imkânsız.

**1.3/1.4 Kalıcı kimlik + watchdog:** `sl_placements` additive kolonlar
algo_id/client_algo_id/working_type; `mark_placed` yalnız verify-OK sonrası;
`remap_placed_algos` her `ensure_sl_coverage` turunda (run_once 900s +
orphan_killer 60s'nin ortak kapısı) restart re-map yapar (clientAlgoId GET →
openAlgoOrders benimsenme), PRESENT'te idempotent, gen'i oynatmaz, çift SL
üretmez.

**1.8 Ack-kaybı + partial-fill (bu turda bulunan iki GERÇEK kusur):**
(i) `close_adapter._SL_LIVE` kümesinde PARTIALLY_FILLED YOKTU — kısmen dolmuş
SL ABSENT sayılıp İKİNCİ SL gönderilebilirdi; düzeltildi, `_SL_LIVE_STATUS` ile
tutarlı. (ii) Place istisnası (ack-kaybı) epoch'u PENDING bırakıp sessiz kılıyordu;
artık `find_algo_by_client_id` ile araştırılır: FOUND→PLACED benimsenir /
kesin ABSENT→epoch serbest (retry güvenli) / UNKNOWN→PENDING KALIR (çift-SL
guard'ı retry'dan önce; kör release/tekrar-gönderme yok).

**1.6 Marjin tamponu:** `conditional_margin_gate` — available ≥
notional/kaldıraç × (1+%buf), varsayılan %20, okunamayan girdi fail-closed;
open_coin'de kaldıraç kapısından hemen sonra, emirden ÖNCE. SCOPE NOT:
closePosition SL yerleşimde marjin kilitlemez (borsa doktrini); tampon
qty-modu düşüşü ve tetikleme-anı hesap riskine muhafazakâr sigortadır —
ölçülmüş fayda iddiası YOK.

**1.7:** `/fapi/v1/countdownCancelAll` kaynakta hiç geçmez (guard testi uç-nokta
dizgisini kontrol eder; docstring yasağı-notu serbest).

**1.5 ALGO_UPDATE:** WS user-data akışı EKLENMEDİ — scope notu: yeni runtime
bağımlılığı + canlıya thread riski; eşdeğer algılama polling ile var (her tur
sl_status + re-map + verify). Gerçek akış FAZ2+ kararına bırakıldı.

**Testler [BOT-SUITE]:** `tests/test_faz18_exit_protection.py` 20/20 GREEN —
wire doktrini (closePosition/qty/TP/hedge/countdown), −4120, MISSING fail-closed,
GET'siz-UNKNOWN, PLACED kimlik kolonları, restart re-map (901 benimsenme, çift SL
yok), idempotent watchdog, ack-kaybı üç-durum, partial-fill, olay tablosu
additive, marjin kapısı. 7 eski fake yalnız `**_faz18kw` aldı (savunmalar
korundu). **TAM SUITE: 1078 passed + 16 subtests, EXIT=0** (Health17 tabanı
1058+16 + 20 yeni).

**1.9 Testnet doğrulaması — BLOCKED:** bu ortamda testnet anahtarı YOK (env'ler
VPS'te, buradan okunamaz; canlı anahtar testnet'e ASLA sürülmez). Wire doktrini
ağısız/anahtarsız ama GERÇEK `BinanceAPI` kod yolunda doğrulandı (WireFakeClient,
testnet base + sahte anahtarlar). Gerçek testnet canlı testi anahtar
verildiğinde yapılabilir.

**ŞU AN İDDİA EDİLEMEYECEKLER:** "FAZ18 testnet'te çalıştırıldı" (çalıştırılmadı
— anahtar yok); "canlıda exchange-SL koruması artık doğrulamalı çalışıyor"
(dal deploy edilmedi; üretim hâlâ 108002c eski yolda); "marjin tamponu SL
yerleşim hatalarını önler" (ölçülmedi); "ALGO_UPDATE tüketiliyor" (polling
eşdeğeri var, WS yok).

SONRAKİ: FAZ 2 (fill/income-kaynaklı ledger writer staging + salt-okur günlük
mutabakat + exchange-türevli risk-gate girdileri) — ve Health17/FAZ18 deploy
KARARI operatörde.

---

# FAZ 2 RAPORU — exchange-kaynaklı ledger + salt-okur mutabakat (2026-11-19, claude)

Dal: `fix/exchange-ledger-faz2-20261119` @ **70e2a6a** (base 1971059 FAZ18; Health17
zinciriyle birlikte staging-toplu test tabanı). Worktree `/home/reisfurkan/faz9-faz2`.
PUSHED; MERGE/DEPLOY YOK; üretim DB'si/ağı/emri DOKUNULMADI (staging = tmp DB).

**2.1 Writer [FILL/INC]:** `exchange_ledger.build_close_events` userTrades'ten
pozisyon-delta kapanış kümeleri üretir; **net = Σrp − Σkomisyon + funding**
(komisyon maliyettir; Mission B finansal tanımı). FUNDING_FEE gelir-etiketi
gerçek veriden bulundu (API dokümanı FUNDING der — ikisi de kabul). Kapanmayan
küme EVENT ÜRETMEZ (tail raporu; uydurma kapanış yok). Writer gerçek
`RiskEngine.record_trade` yolunu kullanır: `source="binance_fill_verified"`,
`affect_daily=False` — bugünün risk sayacına yazım YOK; idempotency
executor_id PRIMARY KEY ile.

**GERÇEK-veri doğrulaması (salt-okur, [FILL]/[INC]/[LED]):**
- 220 fill → **60 kapanış eventi = 60 giriş [FILL]**, 0 tail, 0 unassigned funding.
- Σrp **−21.11092998** ≡ income REALIZED_PNL [INC] (R1 bulgu YOK).
- Σkomisyon **6.46352885** ≡ income COMMISSION [INC] (R2 bulgu YOK).
- Σfunding **−0.68915844** [INC] — 23/23 olay pencere-içi kümeye atandı.
- Σnet **−28.2636** ≡ Mission B NET TRADING ZARARI [FILL] — modül düzeltilmiş
  finansal temeli BİREBİR üretiyor. SYSTEM_CLEANUP 0.00'ları yerine gerçek
  rp+komisyon+funding yazılabilir durumda.
- [LED] ↔ [FILL] gap **15 sembol** listelendi (ADA −3.78 = F21 yazılmamış kayıt
  dâhil) — ZORLA EŞLEŞTİRME YOK; gap raporun parçası.

**2.2 Mutabakat (read-only):** `reconcile()` + CLI — R1/R2 Σ-karşılaştırması
(tol 0.01) + sembol-bazlı ledger↔exchange gap; girdiler mutasyona uğramaz
(testli); CLI yalnız REALIZED_PNL/COMMISSION uyuşmazlığında exit 1. Canlı
işleyişte veri kaynağı collector ndjson'ları ya da `FillKeysetFetcher`
(F22 sözleşmesi testli: fromId ASLA startTime/endTime ile birlikte; kısa
sayfada dur; id dedupe; güvensiz sayfa → RuntimeError fail-closed).

**2.3 Gate girdisi:** `exchange_day_pnl` — gün-neti YALNIZ exchange verisinden;
ledger-türevli daily_pnl (FAZ16 standardı) ile çapraz-kontrol girdisi; F21-tipo
eksik-yazım bu farktan yakalanır. Writer akışı exchange-doğrulamalı satırlar
ürettiği için gate'in PnL girdisi exchange-temelli olur (deploy sonrası).

**Testler [BOT-SUITE]:** `tests/test_faz2_exchange_ledger.py` 15/15 — küme
inşası + sıfır-geçişi bölünmesi, funding penceresi + uydurma-atama yasağı,
staging writer provenance/idempotency/sayaç-dokunmazlık, reconcile bulgu +
gap + mutasyon-yok sözleşmesi, keyset F22 kilidi, CLI exit kodları.
**TAM SUITE 1093 passed + 16 subtests, EXIT=0.**

**ŞU AN İDDİA EDİLEMEYECEKLER:** "mutabakat canlıda günlük çalışıyor" (job
kurulmadı — deploy/daemon kararı operatörde); "gap 15 = 15 defekt" (gap
içinde tanımı-farkı satırları da var; tek tek sınıflandırma FAZ2-deploy
sonrası iş); "writer üretim ledger'ına yazar" (YAZMAZ — staging tmp; canlı
yazım deploy + operatör onayı ister); "funding gün-ataması mutlak doğru"
(pencere-ataması yaklaşıktır; unassigned=0 gözlemi tek veri-seti için).

SONRAKİ: FAZ 3 — veri-arşivi (data.binance.vision, checksum, delisted/
survivorship notu), maliyet modeli (gerçek commissionRate + ölçülen slippage),
ÖN-KAYIT (pre-registration) commit'i SONUÇTAN ÖNCE, null modeller, purged/
embargoed WF + Deflated Sharpe + PBO + block bootstrap, kabul-kapısı TASLAĞI
(karar insanda), verdict EDGE YOK | EDGE VAR | YETERSİZ KANIT.

---

# FAZ 3 RAPORU — OOS edge testi (2026-11-19, claude) — VERDICT: **EDGE YOK**

## Yapılan (önce-kanıt-sonra-hüküm zinciri)
1. **ÖN-KAYIT @93d58d7** (sonuçtan önce) → **DEVIATIONS @7698892** (D-1
   store=None nötr prior; D-2 grid 18+1=19 — order-book arşivi yok; D-3
   fiili-4h vade + 1g embargo) → **@dcc23cb D-4** (sıfır-ATR barı canlı-
   eşdeğeri atlama) → kanonik koşu. Zincirin her halkası commit-li/pushlu.
2. **Veri:** data.binance.vision USDT-M monthly klines, SHA256 .CHECKSUM
   doğrulamalı: OK 3234 / MISSING 1545 (listing-öncesi) / **0 hata**.
   Kapsama 641 sembol-ay ≥ 480 eşik → YETERSİZ-KANIT değil.
3. **Koşum:** strategy_engine @1971059 code-as-spec, 18-config grid,
   ölçülen maliyetler [FILL][ALGO][INC], portföy ≤3 eşzamanlı bileşik,
   expanding-WF 9 fold, PBO-CSCV, DSR(N=19), block-bootstrap(21,2000).
4. Sonuç dosyaları (Git-dışı): raw/streams/faz3_results.json,
   faz3_nulls.json; protokol raporu: RESULTS_FAZ3.md (research dalında).

## Sayılar [BACKTEST] — etiket-hiyerarşisi korunur, canlıyla birleştirilmez
- 18/18 config NEGATİF: mean_net −%0.357…−%0.301/işlem (medyan −%0.329);
  en-iyi config CI'ı [−%0.318, −%0.284] — ÜST sınır < 0; Sharpe −12.2.
- WF 0/9 pozitif (tüm OOS fold'lar negatif); DSR 0.0; PBO 0.0 (not: PBO
  hepsi-kaybeden gridde anlamsız-paso — RESULTS §4 uyarısı).
- **null-1** (rastgele giriş+yön; n=15.961, bağımsız yeniden-üretimle
  determinizmi doğrulandı): −%0.143 [−%0.166, −%0.120]; **null-3**
  (aynı girişler, yön yazı-tura): −%0.119 [−%0.134, −%0.104] — sinyal
  ikisinden de CI-ayrık KÖTÜ → zamanlama+yön ters-seçim üretiyor.
- null-2 buy&hold: BTC +%83 / BNB +%126 / ADA −%68 (2024-01→2026-10).
- Kabul kapısı 1/5 (yalnız PBO) → **EDGE YOK** = ön-kayıt beklentisiyle
  uyumlu. §5/§1 emri: PARAMETRE ARAMASI DURDURULDU.

## Bu fazda bulunup düzeltilen gerçek kusurlar
- ÜRETİM (latent): structure_readings ATR=0'da ZeroDivisionError
  (canlıda geniş except barı sessiz atlıyor → görünmez sinyal kaybı;
  öneri: `if a:` guard'ı — FAZ18 dalına taşımak operatör kararı).
- Harness: portföy ≤3/sembol-kilidi ters-predicate ile hiç işlemiyordu;
  scan testleri 210-çubuk eşiğine ulaşmadan boşlukta geçiyordu; hash()
  determinizmi; µs-ts; CHECKSUM biçimi — tümü düzeltildi+sabitlendi.
- Testler: FAZ3 dalı 1114 passed + 16 subtests, EXIT=0.

## İDDİA EDİLEMEYECEKLER
- "Backtestte kayıp = canlıda da kesin kaybeder" (liste-survivorship,
  funding-sabit-yaklaşımı, §D-2). Edge YOK hükmü FAZ3 protokolünün
  sınırları içindedir; FAZ4 için ZEMİN DEĞİLDİR.
- FAZ4 (shadow/canary) ön-koşulu SAĞLANMADI — ancak yeni tez + yeni
  ön-kayıt + İNSAN onayı ile mümkündür.

## DURUM / SONRAKİ
- Üretim: dokunulmadı (deploy YOK; is_halted, sayaçlar, ledger aynı).
- Operatör kararları bekliyor: (1) FAZ18 @1971059 ve FAZ2 @70e2a6a
  dallarının devreye alma sırası; (2) FAZ2 günlük mutabakat cron bağlama;
  (3) structure_readings ATR-guard'ının FAZ18 dalına alınması.
- Mission C FAZ0–FAZ3 TAMAMLANDI. Birleşik hata/düzeltme raporu:
  TAM_RAPOR_20261119.md.

---

# DEVİR-TESLİM — 2026-11-20 (BLOK 4–8 sonrası; yeni oturum önce bunu okur)

Bu bölüm 2026-11-20'de EKLENDİ; yukarıdaki hiçbir satır silinmedi/değişmedi.
Git kaydı: bu dosyanın BLOK8-anı kopyası `fix/exchange-ledger-faz2-20261119`
dalında `docs/MISSION_STATE_20261120.md` olarak commit'lendi (SHA: git log'da
"devir" başlıklı commit; bu dizin Git-DIŞIDIR — commit yoksa SHA uydurulmaz).

## 5.1 DEĞİŞMEZ KURALLAR (yeni oturum için)

- Bot **HALTED** kalacak; `is_halted` sıfırlanmayacak; halt nedeni değişmeyecek.
- Bot doğrulanmadan canlı işlem AÇILMAYACAK; test/deneme/manuel dahil canlı
  hesaba EMİR GÖNDERİLMEYECEK; canlı hesap API isteği yapılmayacak.
- Deploy / restart / üretim-merge / üretim-push YOK; canlı DB'ye YAZMA yok.
- H5 sonucu operatör izni ("H5 koş") olmadan ÜRETİLMEYECEK. **H5'in geçmesi
  canlı işlem onayı DEĞİLDİR**; en fazla shadow değerlendirmesi konuşulur.
- Shadow ve canlı geçiş AYRI kararlardır; canlı işlem kapısı ayrıca açık
  onay ister (kritik kusurlar kapalı + testler + operasyonel doğrulama kanıtı).
- Kaynak etiketi doktrini: [FILL]/[ALGO]/[LED]/[HB]/[INC]/[EXCHANGE]/
  [LOCAL_LEDGER]/[HARNESS]/[BACKTEST]/[SHADOW]/[ASSUMPTION]/
  [OPERATOR_REPORTED] — etiketler arası birleştirme yok; kanıt yoksa
  INSUFFICIENT EVIDENCE / NOT VERIFIED yazılır, sayı uydurulmaz.
- 429/418 → Retry-After dolana kadar DUR; her yanıtta W1M logla.
- Alt-ajan çıktıları BAĞIMSIZ doğrulanmadan kapanış satırına giremez (BLOK6
  İŞ2 fabriciation olayı; BLOK7 İŞ4 kuralı).

## 5.2 AÇIK KARAR KAPILARI (operatörde; hiçbiri bu oturumlarda açılmadı)

- **Kapı A — H5 koş:** ONAY BEKLİYOR. Ön-kayıt eksikleri BLOK8'de
  kapatıldı: maliyet kademesi DONDURULDU (aşağıda), güç analizi birleşik
  kapıya göre yeniden üretildi. Kapı A açılmadan hiçbir H5 sayısı yok.
- **Kapı B — Birleşik aday kur (aday+Health17+FAZ2):** Operatör kararı
  **ŞİMDİ HAYIR** (BLOK8 talimatı): bot HALTED ve yeniden açma niyeti yok →
  iş ertelendi; OTOMATİK YENİDEN AÇILMAZ; niyet doğarsa ayrıca değerlendirilir.
  Kapsam tablosu: docs/DEPLOY_CANDIDATE_SCOPE_20261120.md (deploycand @e3506cb).
- **Canlı işlem kapısı:** KAPALI (yukarıdaki şartlar sağlansa bile ayrıca
  açık operatör onayı gerekir).

## 5.3 ENVANTER (2026-11-20 gerçek çıktılarıyla)

| Alan | Değer | Kanıt |
|---|---|---|
| Üretim sürümü | 108002c | OPERATOR_REPORTED (bu oturumda canlıya bakılmadı; lokalde release dalı 108002c mevcut) |
| Üretim durumu | HALTED | OPERATOR_REPORTED |
| faz9-faz2 | fix/exchange-ledger-faz2-20261119 @ bu dosyayı commit'leyen SHA (öncesi: bdf32c9 İŞ1-B7, d38094f B8-İŞ1, c162c19 B8-İŞ3, 5be266d B8-İŞ2) | git log (VERIFIED lokal) + remote pushlu |
| faz9-faz3 | research/faz3-oos-prereg-20261119 @ d357aed | VERIFIED lokal |
| faz9-faz18 | fix/exit-protection-faz18-20261119 @ 10545eb | VERIFIED lokal |
| faz9-deploycand | fix/deploy-candidate-security-20261120 @ e3506cb | VERIFIED lokal |
| Çalışma ağaçları | 5 dalın hepsi temiz (status boş) | git status (VERIFIED) |
| faz9-audit | Git DEĞİL | git rev-parse hatası (VERIFIED) |
| Maliyet ön-kayıt commit | d38094f (docs/FAZ8_COST_FREEZE_20261120.md + faz8_cost_freeze.py) | git show --stat (rapor) |
| Hüküm-dili commit | c162c19 (docs/FAZ7_H5_POWER_20261120.md +DÜZELTMELER) | git show --stat |
| Güç v2 commit | 5be266d (faz8_power_v2.py + docs/FAZ8_POWER_V2_20261120.md + FAZ7 madde-4 retraction) | git show --stat |
| MISSION_STATE commit | bu kopyanın commit'i (git log "devir") | commit sonrası git log |

## 5.4 TEK SAYFA — ÖNEMLİ BULGULAR

1. **Muhasebe:** NET −28,2636 USDT [FILL] = Σrp −21,1109 + Σkomisyon −6,4635
   + Σfunding −0,6892; 220 tekil fill (id-dedupe, fromId-keyset), pencere
   2026-11-08→18, kaynak raw/streams (trades_fromid_fix + income). Bu, MEVCUT
   kuralın canlı defteridir; harness değildir.
2. **Harness ≠ canlı:** BLOK4 paritesi: canlı 60 girişin harness'te recall'ı
   %72-82, olay yoğunluğu 525×, 29/60 tur scale-in — harness canlı botun
   temsilcisi DEĞİL. Harness/backtest sayıları canlı execution kanıtı
   olarak SUNULAMAZ.
3. **Parite neyi doğruladı:** FAZ3 hükmü yalnız "harness varyantı için"
   geçerli; neyi doğrulamadı: canlı botun eşdeğer davranışı.
4. **Güç (BLOK8 v2 — BLOK7 v1 BİRİM HATASIYLA GEÇERSİZDİR):** Dört koşul
   BİRLİKTE kapı; NSIM=300/μ, B=2000 (hedefler karşılandı), t-jackknife
   düzeltmeli. Birleşik MDE80 ≈ **%0,017 (A) / %0,019 (B) → ≈%0,02
   mertebesi** (bağlayıcı bileşen DSR; %0,01'de geçme %12-28, %0,02'de
   %92-97). μ=0'da birleşik 0/300 → tip-I %95 üst-sınırı ≤%1 ("sıfır"
   DENMEZ). 2,49M olay bağımsız örneklem değil: gün-kümeli ICC 0,009 →
   N_eff ≈ 152k (A). g1 bileşeni 18-kümede tek-başına anti-konservatif
   (%14-23) — tek-başına kullanılamaz. v1'in "MDE=%0,016" ve 0/40
   sayıları ×100 birim hatası + UNUSDT yazım hatası nedeniyle OKUNAMAZ
   (docs/FAZ8_POWER_V2_20261120.md §4).
5. **F25 (VERIFIED kök-neden, FAZ18 kayıtları):** equity okumasındaki
   0,0-sentinel/kötü-değer DD hesabına zehir saçıyordu; düzeltme
   `_record_bad_equity` + `dd_selftest()` (865d2e3, aday dalda; test
   test_faz18_f25_dd_gate.py). ÜRETİME SÜRÜLMEDİ.
6. **BLOK7 saflık (VERIFIED):** yayın karar/ticaretleri ⊂ [2024-01-01,
   2026-10-31]; 2022-23 yalnız lookback (ısıtma sınır ≤2024-02-11; %3,61
   kapalı işlem ısınma kuşağında). Holdout iddiası: keşif-dönemi verisi
   yayın kararlarına sızmamış.
7. **Maliyet DONDURULDU (BLOK8 İŞ1, H5-öncesi):** C_frozen_RT = %0,1698
   = 2×%0,04983 [EXCHANGE komisyon] + 2×3,505bp [EXCHANGE 20/20 tetikleme
   p75]. Giriş slipajı ASSUMPTION (= çıkış p75). "−0,7171 USDT" iddiası
   NOT VERIFIED (yerel kaynak yok; PnL≠slipaj). Ayna-gross çıpası +0,168 →
   dondurulmuş kademe kapıyı sıfır-kenar bölgesine koyar.
8. **Hüküm dili (BLOK8 İŞ3):** İzinli tek başarısızlık cümlesi: "2020–23
   büyük-cap evreninde, dondurulmuş maliyetle, ters kural net pozitif
   üretmedi." İki-yönlü/evrenler-arası genellemeler GERİ ALINDI. Evren B
   (2024-26 küçük-cap) keşifle aynı rejim+günleri paylaştığından bağımsız
   doğrulama gücü zayıftır; A başarısızlığı küçük-cap'de etki yokluğunu
   kanıtlamaz.

## 5.5 YAPILMADI / ÖLÇÜLEMEDİ (dürüstlük listesi)

- Giriş slipajı ÖLÇÜLMEDİ — çıkış p75'ine eşit ASSUMPTION kullanıldı.
- Tetikleme-fill eşleşmesi çok-parçalı fill dağılımını görmez (actualPrice
  borsa ortalaması); parça-bazlı eşleştirme yapılmadı.
- H5 SONUCU üretilmedi; H5 harness'i (faz5_harness.py B.4 kontrolleri)
  henüz YAZILMADI/kırmızı-yeşil edilmedi — Kapı A açılırsa önce B.4.
- Canlı hesap/servis/DB kontrolü BLOK8'de YAPILMADI (görev offline; VPS
  yalnız BLOK7'de salt-okur ziyaret edildi: faz9-tests.timer 03:30 →
  /root/risk_ledger.db yazar bulgusu [JRN/systemctl, BLOK7]).
- Canlı SL/pozisyon kontrolü, deploy, restart, emir, canlı DB yazımı: YOK.
- Birleşik aday (Kapı B) kurulmadı (operatör HAYIR).
- Canlıda yürürlükte onaylı aday YOKTUR; üretim 108002c HALTED
  [OPERATOR_REPORTED].

## 5.6 DRILL/ROLLBACK (yalnız yerel doküman commit'leri için prosedür)

- BLOK8 commit'leri ve dosyaları: d38094f (faz8_cost_freeze.py,
  docs/FAZ8_COST_FREEZE_20261120.md); c162c19 (docs/FAZ7_H5_POWER...md);
  BLOK8-İŞ2 commit'i (faz8_power_v2.py + docs); bu devir commit'i
  (docs/MISSION_STATE_20261120.md). Hiçbiri üretim dosyasına dokunmaz.
- Geri alma ÖRNEK prosedürü (AÇIKLAMA — çalıştırılmadı):
  `git status --short` boş olmalı → `git revert <SHA>` → `git log -1`.
- Üretime checkout / deploy / restart / canlı rollback TATBİKATI yapılmaz.
  Offline prova (BLOK6 drill) mevcuttur: faz9-drill/drill.sh + run_tick.py
  (kopya DB + sahte transport; üretim değil).
- Bu belgede yazılı her komut ya gerçek çıktıya sahiptir ya "örnek/
  AÇIKLAMA" etiketlidir.


# KAPANIŞ — 2026-11-20 (BLOK 9) — YENİ OTURUM TEK BU BÖLÜMÜ OKUYARAK DURUMU ANLAR

1. **Bot HALTED [OPERATOR_REPORTED]; canlı işlem kapısı KAPALI.** Bu
   oturumlarda canlıya hiçbir yazma/emir/durum-değişikliği dokunuşu olmadı.
2. Bot düzeltilmeden ve düzeltmeler test+bağımsız kanıtla doğrulanmadan
   canlı işlem AÇILMAZ (§0 değişmezleri yürürlükte).
3. **H5 sonucu üretilmedi** — B.4 harness hiç koşmadı; H5'e eşdeğer sonuç
   üreten hiçbir simülasyon da koşulmadı (BLOK7-9 boyunca yalnız sentetik
   GÜÇ analizleri koştu; bunlar H5 çıktısı değildir).
4. **H5: KOŞMAMA ÖNERİSİ — OPERATÖR KARARI BEKLİYOR.** Gerekçe ve karar
   ağacı: docs/H5_KARAR_20261120.md. Operatör "H5 koş" da "H5 koşma" da
   demedi; "OPERATÖR KARARIYLA KOŞULMADI" satırı henüz YAZILAMAZ (iki
   durum karıştırılmaz).
5. Birleşik aday (Kapı B): **ŞİMDİ HAYIR** — otomatik yeniden açılmaz.
6. Shadow programı BAŞLATILMADI (H5'ten bağımsız ayrı kapıdır).
7. Ekonomik ön-eleme kuralı: TASLAK, onay bekliyor — beklenen brüt edge ≥
   2×C_RT = **%0,3396**; küçük-cap payı S_smallcap_extra NOT-MEASURED
   (eşik onaylanmış sayılmaz). docs/ÖN_ELEME_KURALI.md.
8. Dondurulmuş maliyet **C_frozen_RT = %0,1698**; giriş slipajı = çıkış p75
   ASSUMPTION (ölçülmedi). Dondurma H5-öncesi ama çıpa+%0,15/%0,20
   duyarlılıklarına KÖR DEĞİLDİ (izinli ifade BLOK9 İŞ2.1; "tamamen bağımsız"
   dili yasak).
9. Beklenen **in-sample mekanik net −%0,0014/tur** (ayna-gross %0,1684 −
   C_frozen); bu OOS BEKLENTİSİ DEĞİLDİR; OOS daralma miktarı ÖLÇÜLMEDİ.
10. **Ham delil yedeği: BLOCKED — OPERATÖR TEYİDİ BEKLİYOR**; manifest
    üretilmedi (araç commit'li: 9d7a8b2 `faz9_raw_manifest.py`; kuru-koşum
    kapsamı 3787+60 dosya, kimlik-dosyası 0). Gerçek ham ağaç: faz9-audit/raw
    (streams 35M + klines 469M + klines1m 54M + kök JSON'lar) + üst-düzey 60
    dosya — görev mektubundaki `raw/streams` listesi eksikti, doğrulandı.
11. Binance `GET /fapi/v1/userTrades` geçmiş aralığı 2026-08-26'da 6→3 ay
    (resmî changelog referansı; offline modda bağımsız doğrulanmadı
    [PARTIAL]). 8–18 Kas 2026 verileri ≈ Şubat 2027'de erişim-dışı olabilir →
    **yedek erkenden alınır, kesim zamanına güvenilmez**; "Query Algo Order"
    belgesi userTrades saklama kanıtı DEĞİLDİR. Kaynak:
    https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log
12. **KAPI BAĞIMSIZLIĞI: H5 geçmesi ≠ canlı izin ≠ shadow başlangıcı ≠ aday
    onayı.** Dört kapı ayrı operatör kararlarıdır; hiçbiri diğerinden
    otomatik SONUÇ üretmez.

**Güç hükmü (BLOK9 koşullu dili):** "Güç, yalnızca belirtilen alternatif etki
bandında (+%0,06…%0,20) yeterli; maliyet sonrası beklenen net etki için bu
sonuç yeterlilik kanıtı değildir." Birleşik MDE80 ≈ %0,02 mertebesi (v2,
analitik envelope %0,015-0,02 ile tutarlı — docs/FAZ9_MDE_UYUMLULUK_20261120.md).

**Git zinciri:** BLOK9 commit'leri 61e5c38 (MDE tutarlılık) → e2f5bcd (H5
karar) → 74cdc8f (ön-eleme) → 9d7a8b2 (yedek/manifest) → bu commit (kapanış),
fix/exchange-ledger-faz2-20261119'da; **BLOK9 PUSH YAPMADI** (görev yasağı) —
origin hâlâ 85e5768'de; push kararı operatörde.

---

## BLOK10 DEVİR — FAZ9 bot onarımı, canlı teste hazırlık, kanıtlı teslimat (2026-11-20)

**Dal/SHA:** `fix/deploy-candidate-security-20261120` @ **b2911da** (FF-push edildi:
origin e3506cb → b2911da, `git ls-remote` doğrulandı). Üretim `release/faz9-faz14-20261119`
@ 108002c DOKUNULMADI (worktree temiz, HALTED); üretime merge/deploy/restart YOK;
`is_halted` değişmedi; canlı Binance tek API isteği/emir YOK; canlı DB yazma YOK;
H5 KOŞMADI ve eşdeğeri üretilmedi; yeni birleşik strateji adayı YOK.

**BLOK10 commit zinciri (9):** e4eb9a8 (validation_budget modülü + 28 test) →
e4387d7 (giriş zinciri bağlantısı + 7 entegrasyon testi; siyahıdan 1 regresiyon
GÖRÜLDÜ ve gizlenmeden siyahlandı) → de1393a (kök test hijyeni: 3 ölü VPS scripti
evidence/'e `git mv`; pytest.ini; kök conftest STATE_DIR kalkanı) → 2902de0 F-A
(bayat `_LAST_CONFIRM` → yön çevirme riski; giriş-temizliği) → 9a40d6d F-C (ATR
None → canlı giriş RED; harness `_get` onarımı) → a42ca1f F-F (K-3 bayat PLACED →
kalıcı SL korumasızlığı kilidi) → 16b0007 F-B (close-intent deadlock; flat→reconcile
CLOSED) → 9ecbd94 F-D (orphan_killer error≠empty + bekçi dürüstlüğü; iptalin
bayat-HB'de sürmesi BİLİNÇLİ KARAR) → b2911da F-G (archiver watermark hata kapısı).

**Bağımsız limitler (BLOK10 §3):** kontrol varsayılan KAPALI
(`CONTROLLED_LIVE_VALIDATION=false`); sert tavanlar koda gömülü ve yalnız sıkılaştırılır:
2 işlem / 50 USDT işlem başı / 100 USDT toplam (validation_budget.py:42-44). Kalıcı
SQLite (WAL, FULL, BEGIN IMMEDIATE); retry idempotent; ACK-belirsiz hak iade ETMEZ;
CONSUMED iade edilmez; bayrak kapalı = akış BİREBİR değişmez; geçersiz limit = fail-closed.
Bağlantı noktası: risk kapılarından SONRA, create_executor'tan HEMEN ÖNCE
(auto_trader.py:3817→3824→3851). "2. işlem 1.'yi otomatik izlememek zorunda" çift
katman: kod tavanı + runbook insan kapısı (docs/LIVE_READINESS_REPORT.md §8.4 adım 8).

**Test durumu:** full `tests/` **1131 passed, 9 subtests, exit 0**
(/tmp/blok10_full_after_fixes.log). Kırmızı anıtlar korundu: fix-öncesi 17 FAILED
(#7 koşumu), kök pytest'ta 8 ÖNCEDEN-VAR FAILED üretim 108002c worktree'sinde de
kanıtlı → deploy-script test kapısı kırık; doğru kapı `pytest tests/` (rapor §6).

**Açık kalanlar (kod değişmedi, rapor §8.5 etiketli):** 429/pidfile/SIGTERM
[mitigasyonlu, doğrulanmış yokluk], TRAILING dedupe [doğrulanmış, pratik yol kapalı],
stale-bar kapısı ve ATR/volume kapanmamış-bar [alt-ajan iddiası, doğrulanmadı, P2],
kill_spot_grid* gömülü HB kimliği [doğrulandı; rotasyon OPERATÖR eylemi — değer hiçbir
çıktıya/commite yazılmadı]. `ensure_sl_coverage` koruyucu %2 ATR fallback'i bilinçli
korundu (açık pozisyonda alternatif korumasızlık; giriş yolu F-C ile kapalı).

**Sıradaki tek yasal adım:** operatör incelemesi. Deploy, HALTED kaldırma, H5, canlı
bütçe açılması (`CONTROLLED_LIVE_VALIDATION=true` seti dahil) — HER BİRİ AYRI onay.
Hüküm: **READY FOR OPERATOR REVIEW — NOT DEPLOYED.**

---

# BLOK 11 DEVİR (2026-11-21) — §2 doğrulama + N1/N2/§6.2 onarımları + H5 ÖNSONUÇ altyapısı

**Dal/SHA:** `fix/deploy-candidate-security-20261120`; zincir fde51d8 → **81a8c88** (N2) →
**b226590** (N1) → **012200f** (§6.2 close CID) → BLOK11-H5/§9 commit'i (bu dosyayla aynı).
Üretim `108002c` DOKUNULMADI; HALTED; canlıya tek istek/emir YOK (koşum günlükleri rapor
§6). Üç BLOK11 commit'i PUSH EDİLMEDİ (operatör inceleme kapısı BLOK10 ile aynı).

## 1) §2 — ÜÇ REVIEW'UN BAĞIMSIZ DOĞRULAMASI (yanlışlama-önce)

Merkez bulgu: reviewer'ların dosya satır referansları (runner.py, heartbeat_writer.py,
watchdog.py, h5/h5_runner.py, config_h5_50.json, ENV_DB validation_budget.py:15...)
HİÇBİR ağaçta/git geçmişinde YOK — reviewer'lar FAZ18-öncesi/hayalî bir ağaç okumuş.

| ID | Bulgu (iddia) | Kök neden | Doğrulama | Karar |
|----|---|---|---|---|
| R1-F1 | "SL girişi TAKİP EDER; pencere açık" | FAZ18 `_finalize_open` SL'yi AYNI finalizasyonda clientAlgoId+3×3s doğrulama ile koyar; doğrulanamazsa acil koordineli kapanış + `is_halted=True` | auto_trader.py `_finalize_open`/`ensure_sl_coverage` okundu; pre-entry SL fiziken imkânsız (reduceOnly pozisyonsuz -2022) | **REFUTED** |
| R2-F1 | "ACK kaybı telafisi yok" | FAZ18 `find_algo_by_client_id` FOUND/ABSENT/UNKNOWN; UNKNOWN→PENDING korunur, kör retry yok | verify_sl_ack sözleşmesi + testler | **REFUTED** |
| R2-F2 | "restart'ta tehlikeli kayıt silinir" | `_recover_pending_records` (FAZ15b v5) tehlikeli kayıtları KORUR; ilk tick exchange ile reconcile | kod okundu | **REFUTED** |
| R2-F3 | "kapanış ACK'siz kör re-POST" | CloseCoordinator: mark_sent(POST öncesi)→FLAT-confirm→mark_closed; REVERSED/PARTIAL muhafızları | close_coordinator.py tam okundu | **REFUTED** |
| R3 | "client order id hiç yok" | SL algo FAZ18'den beri clientAlgoId; query/cancel orig_client_order_id destekler | kod okundu | **KISMEN YANLIŞ** → gerçek boşluk = kapanış POST'u (§6.2, onarıldı) |
| N1 | "orphan_killer hiç planlanmamış" | crontab boş, unit yok, üretimde çağıran grep=0, süreç yok | üç bağımsız yokluk kanıtı | **CONFIRMED P1 → onarıldı (b226590)** |
| N2 | "bütçe kapalıyken pass-through" | §10.3 "kontrol kapalıysa hiçbir işlem açılmasın" der; kod bayrak-kapalıyken serbest bırakıyordu | kod + test | **CONFIRMED P1 → onarıldı (81a8c88)** |
| §6.2 | "kapanış POST'unda newClientOrderId yok" | close_adapter.close_reduce_only parametresizdi | kod okundu | **CONFIRMED → onarıldı (012200f)** |

## 2) Onarımların kanıt özeti

- **N2 (81a8c88):** `_validation_reserve` DISABLED→RED ("BÜTÇE-KAPALI…"); DB yolu
  `VALIDATION_BUDGET_DB` env override (test yalıtımı); conftest autouse her teste taze
  bütçe+DB. Pozitif/negatif kontrol çifti AYNI harneste: bütçe açık→`create_executor`
  1 çağrı; kapalı→False + 0 çağrı + DB dosyası yok. Eski "bayrak kapalı = akış değişmez"
  BLOK10 satırı ARTIK GEÇERSİZDİR (bu bölüm onu RESMEN İPTAL eder); §10.3 semantiği:
  bayrak-kapalı = GİRİŞ YOK. 2 test bilinçli güncellendi (mod-off sözleşmesi; c6 200 USDT
  harness bypass'ı + per-trade 50.01 RED testi eklendi).
- **N1 (b226590):** `tools/orphan_watchdog.py` (flock+exit-code: 0 tur/1 scan istisnası/
  2 kilit-anahtar yok; anahtar yokluğunda TEK İSTEK YOK), 8 test (kaynak taraması dahil:
  emir token'ı yasağı), `deploy/orphan_watchdog_faz9_live.{service,timer}.example` +
  cron alternatifi — **KURULMADI** (operatör kararı; HOME'un live servisle birebir
  aynı olması şart — farklı HOME = sessiz körlük).
- **§6.2 (012200f):** `fz9c<SYMBOL><ms>` ≤36 karakter newClientOrderId; ONEWAY+HEDGE
  dalları; ms tabanlı tekil; red→FAILED_SAFE (bilinçli güvenli taraf).

## 3) §3 H5 — altyapı KURULDU; ÖNSONUÇ (PRELIMINARY) koşuldu; ana karar OPERATörde

Motor `tools/h5_engine.py` (faz5_harness cc0f1010 + faz7 c8877b45 BİREBİR kopyalar +
run_round_ex kimlik-testli genişletme) + `tools/h5_runner.py` (10 gard, 23 test
GREEN). Sinyal makinesi = FAZ3'ün kendisi (`faz3_backtest.SymbolData/scan_symbol_decisions`,
cfg_base={}, geçiren = veto-değil ∧ fams≥1 — canlı `CONFIRM_MIN_FAMILIES=1`);
bariyerler `sizing_policy.SIZING_DEFAULTS` (canlı ile aynı: 1.5/3.0×ATR bantlı).
Etiketler (dürüstlük): **PRELIMINARY + PARTIAL + üreticiye-in-sample** — B.2'nin birincil
OOS'u 2020-23'tür; 2025 FAZ3 keşif penceresi İÇİNDE; B.5 dört-kapısı 2025'e UYGULANMAZ
(B.8); BCH/GALA 2025 verisi YOK (indirme OPERATÖR ONAYI gerektirir, §3.1).

BLOK12 §9 ETİKET HÜKMÜ (bağlayıcı): aşağıdaki ETHUSDT 2025 sayıları **yalnız
OOS-OLMAYAN ÖN-GÖZLEM**dir. (i) B.5 OOS kapısı hükmü yerine GEÇİRİLEMEZ;
(ii) kârlılık/kârsızlık KANITI olarak alıntılamaz — yönsel gözlemdir;
(iii) H5 bu sayılar için YENİDEN KOŞULMAZ (koşum maliyeti/blokaj gerekçesiyle
etiket gevşetilemez). Birincil OOS kararı yalnız OPERATÖR onaylı BCH/GALA
indirmesi + 2020-23 penceresiyle verilir.

H5 koşum sayıları (ETHUSDT 15m; işlem başına net %, cm-v1-pess-v1 kötümser):
FULL2025 (n=33 981 olay): ileri −0,3657 / **TERS −0,3390**; iid boot lo5(ters)
−0,3469; çeyrek-lo5 −0,3545; DSR(N=20)≈0. EXACT KAS-ARA (n=5 854): ileri
−0,3669 / **TERS −0,3758**; iid lo5 −0,3939 (çeyrek-lo5 2 blokte tanımsız).
base senaryoda da iki pencerede de ortalamalar negatif (−0,09…−0,13).
SONUÇ (ÖNSONUÇ): iki kol × iki senaryo × iki pencerenin TAMAMINDA net<0 —
2025 ETHUSDT'de ters kural maliyeti karşılayacak kenar ÜRET MİYOR; FAZ3
"EDGE YOK" ile yönsel tutarlı; üretici in-sample olduğundan KEŞİF DEĞİL,
teyit önsonucu. 2026 girişi YOK (1 sınır olayı elendi+sayıldı; G-runtime
sıfır ihlal); çıkış sızması ≤15 işlemdir (≤4 h). Kaçış dağılımı kötümserde
ağırlıklı SL (~%67) — bariyerler SL tarafında çalışıyor.
Yöntem notu (B.5-1 sapması): sembol-kümeleri tanımsız olduğundan tek-sembol
koşusunda küme-iid uygulanır; alt-uç barındırma-korelasyonuna KÖR kalır —
sayı yalnız PARTIAL bağlamda anlam taşır.
Sonuç dizini: /home/reisfurkan/faz9-audit/h5_runs/ (Git-dışı; trades.csv+summary.json).

## 4) §9 testnet smoke — ARAÇ HAZIR, KOŞULMADI (blokaj)

`tools/testnet_smoke.py`: base URL KODDA SABİT testnet; üretim anahtarı ortamda varsa
RED; TESTNET_* anahtarı yoksa RED (exit 2). 7 test GREEN, ağa tek istek YOK.
**TESTNET_API_KEY/TESTNET_API_SECRET bu makinede YOK → §13 blokajı: smoke yapılamadı.**
Not: G09 (h5_runner) ilk denemede ÜRETİM ANAHTARI ortamda görüldüğü için koşuyu RED'ledi —
gard çalıştı; koşular yalnız kimlik-temiz alt-ortamda (`env -u BINANCE_KEY -u BINANCE_SECRET`)
koşar; kimlik değerleri hiçbir çıktıya yazılmadı.

## 5) Test/gardi durumu

Full `tests/`: H5+smoke modülleriyle **1186 passed, 9 subtests, exit 0**
(son tam koşum, BLOK11 commit'iyle aynı ağaç). Root pytest: 8 ÖNCEDEN-VAR
FAILED (BLOK10 §6 ile aynı küme; yeni dosya etkisi yok). Canlı Binance'e TEK
İSTEK YOK (tüm BLOK11 boyunca; H5 turları yalnız yerel CSV okuması).

**BLOK10 satırıyla çelişki DÜRÜSTÇE KAYITLI:** "bayrak kapalı = akış BİREBİR
değişmez" (yukarıda, BLOK10 §Bağımsız limitler) N2 ile GEÇERSİZDİR; yeni
sözleşme: bayrak kapalı/bozuk → tüm canlı girişler RED (`BÜTÇE-KAPALI:`).
Eski semantiğe dayanan iki test bilinçli olarak yeni sözleşmeye güncellendi.

## 6) Sıradaki yasal adımlar (HER BİRİ AYRI OPERATÖR ONAYI)

1. BCH/GALA 2025 kline indirme onayı (production/vision arşiv = "production kline
   download") → tam-üniversite H5; 2. B.2 2020-23 birincil OOS kararı (asıl kapı);
3. watchdog kurulumu (unit örnekleri hazır); 4. testnet anahtarları verilirse smoke;
5. push/deploy/HALTED — BLOK10 kapıları AYNEN duruyor.
Hüküm: **READY FOR OPERATOR REVIEW — NOT DEPLOYED; HALTED KORUNDU.**

---

# BLOK 12 DEVİR (2026-11-21) — operasyonel engellerin kapatılması + canlı test kapıları

Dal: `fix/deploy-candidate-security-20261120` (üretim `108002c` KORUNDU, dokunulmadı).
Tabandaki 9 commit (eski→yeni): 96e6406(§5) → 4808854(§2) → b07cf40(§1) →
c6e97c2(§4) → 15f678f(§3) → e7df422(§6) → 752e61e(§7) → bf2001c(§8) →
87007cf(§9). Her commit mesajı kök-neden + kırmızı→yeşil kanıtını taşır.

## 1) Onarım tablosu (kök neden → kanıt → commit)

| § | Kök neden (özet) | Ana dosyalar | Kanıt | Commit |
|---|---|---|---|---|
| 1 | İkinci canlı işlem onayı süreç-içi/bulaşık; restart'ta kayboluyordu | operator_approval.py (YENİ), auto_trader entegrasyonu | 24 test; üçüncü işlem HER ZAMAN RED; restart-safe | b07cf40 |
| 2 | Aynı intent yeniden POST edilebiliyordu; fill tavanı AŞILINCA raporlanıyordu (yetersiz) | auto_trader (intent/payload bağı, worst-fill ÖN gard) | test_blok12_budget_intent.py; UNKNOWN korunur | 4808854 |
| 3 | 8 kök test kırmızısı fixture çürümesiydi; preflight eski ignore listesiyle SAHTE YEŞİL üretti | 7 test fixture + tools/secret_scan.py + deploy/faz9_preflight.sh (YENİ) | bare tam pytest exit 0; ignore YOK | 15f678f |
| 4 | 429/418 umursanmıyordu; açık mum ATR/hacme girebiliyordu | api_guard.py, auto_trader (_api_response, closed_bars, kapılar) | 30 test (test_blok12_api_guard.py); POST kör retry YOK makine değişmezi | c6e97c2 |
| 5 | Heartbeat yönetici kimliği kill_spot_grid.py/.sh içinde GÖMÜLÜYDÜ | kill_spot_grid.py/.sh, tools/secret_scan.py, docs rotasyon planı | dış config + fail-closed; GEÇMİŞTE ESKİ KİMLİK VAR (bkz. §5 notu) | 96e6406 |
| 6 | Watchdog kurulumu sessiz-körlük sınıfıyla (HOME/STATE_DIR farkı) denetlenmiyordu | tools/verify_watchdog.py (YENİ), docs/OPS_WATCHDOG_20261121.md | 8 test + hash-kanıtlı yalnız-okunurluk; gerçek ağaç 17 OK/1 WARN | e7df422 |
| 7 | --place-order ile salt-okunur aynı kapıdaydı; ayrı kalıcı onay yoktu | tools/testnet_smoke.py (order_approval) | 8+8 test; üretim-anahtarı sırası ÖNCE; banner "ÜRETİM KANITI DEĞİLDİR" | 752e61e |
| 8 | Tek-süreç kilidi YOKTU (çift süreç = çift giriş riski); SIGTERM'siz döngü | auto_trader (acquire_process_lock, _shutdown_handler, interruptible_sleep) | RED 15→GREEN 15 + gerçek SIGTERM alt-süreç; main exit 3 | bf2001c |
| 9 | ETHUSDT 2025 sayılarının alıntı yasağı bağlayıcı değildi | docs (yalnız belge) | H5 YENİDEN KOŞULMADI | 87007cf |

Test toplamı (BLOK12 sonu): `tests/` tam koşum **1294 passed + 9 subtests,
exit 0** (bf2001c üzerinde; §9 belge-only). Deploy preflight (worktree@HEAD,
bare pytest + secret scan) §10'da ayrıca doğrulandı.

## 2) YAPILMADI (dürüstlük listesi — BLOK12)

Production merge YOK · deploy/restart YOK · HALTED KORUNDU · canlı Binance
isteği YOK · canlı emir YOK · watchdog birimi KURULMADI/timer enable
EDİLMEDİ · testnet gerçek turu KOŞMADI (TESTNET_* anahtarı makinede yok —
blokaj) · H5 yeniden KOŞULMADI · heartbeat kimliği ROTASYON YAPMADI (yalnız
plan) · force push YOK. §5 DÜRÜST NOTU: eski heartbeat kimlik DEĞERLERİ git
geçmişinde yaşamaya devam ediyor (silinmek geçersiz kılmaz); yalnız sha256
digest'ler secret_scan KNOWN_CRED_DIGESTS'te; değerler hiçbir çıktıya
yazılmadı.

## 3) OPERATÖR ONAYI BEKLEYEN KAPILAR (hiçbiri bu görevde açılmadı)

1. Watchdog: `deploy/orphan_watchdog_faz9_live.{service,timer}.example`
   kurulumu + `enable --now` + (ayrı onay) gerçek API'li `--once` denemesi —
   docs/OPS_WATCHDOG_20261121.md.
2. §1 onay token'ının ÜRETİLMESİ ve kullanımı (ikinci canlı işlem).
3. Testnet: TESTNET_* anahtarlarının korumalı env/secret store ile verilmesi
   (SOHBETTE İSTENMEZ) + emir adımı için 0600 işaret dosyası
   (TESTNET_ORDER_APPROVAL_FILE).
4. Heartbeat kimlik rotasyonu (plan: BLOK12 §5 commit mesajı + docs).
5. BCH/GALA indirme + 2020-23 birincil OOS (BLOK11 kapısı AYNEN duruyor).
6. Push sonrası deploy/HALTED — BLOK10 runbook'u AYNEN geçerli.

## 4) §8 DAVRANIŞ DEĞİŞİKLİĞİ UYARISI (operatör)

Servis ayaktayken elle tek-tur `python auto_trader.py …` artık **exit 3**
verir (tek-süreç kilidi; bilinçli fail-closed). SIGTERM en geç ~1s içinde
döngüden çıkarır; in-flight emir sorgulanmadan iptal EDİLMEZ, pending/bütçe/
onay diskte korunur. systemd `TimeoutStopSec` verili değilse varsayılan
90s >> 1s — risk yok.

Hüküm: **READY FOR FINAL OPERATOR REVIEW — NOT DEPLOYED; HALTED KORUNDU.** (Devam A–D maddeleri 182a0bd ile kapandı; son push noktası bu bölümdedir.)

## 5) BLOK12 DEVAM A–D (2026-11-21, operatör takviye maddeleri) — commit 182a0bd

| Madde | Kök neden | Kızıl→Yeşil |
|---|---|---|
| A | 429/418 gardı yalnız open_coin başında; tur içinde beliren cooldown'da istekler gidiyordu | test_blok12_sendtime_gates.py: sentry transport (tek ağ denemesi patlar) — GET/POST/DELETE local_block; tur-içi 429 → POST YOK + hak iade (trades_used 0'a döner); _reservation_should_keep(local_block)=False |
| B | SIGTERM yalnız fonksiyon başında; POST öncesi pencere yarışı | TestShutdownRaceAtCreateExecutor: sinyal dikişi onay kapısı↔POST arasında; hb.created==[], iade, "create_executor ÖNCESİ shutdown"; negatif kontrol POST koşar |
| C | worst-fill zarfı pending'e taşınmıyordu; tavan-içi/en-kötü-üstü doluş "temiz"di | test_blok12_fill_guarantee.py (8): zarf (cap,drift,worst) kayda yazılır; GERÇEK filled_amount_quote hem tavan hem en-kötüyle kıyas; GARANTİ-SINIRI aşımı / TAVAN İHLALİ / bozuk zarf fail-closed → giriş kilidi |
| D | OPERATOR_APPROVAL_TOKEN_FILE 0644 okunabiliyordu | TestTokenFilePermissions: fstat (TOCTOU'suz) mod&0o077 → RED, token HARCANMAZ (state APPROVED_AWAITING_USE); 0600 geçer |

C'nin dürüst sınırı: tavan ancak gerçek doluş kayması ≤ drift (worst zarfı)
ise garanti; garanti-dışı kalan tek şey tespit gecikmesi — en fazla bir
dolum-sonraki tick, sonra yeni girişler kilitli.

Kanıt: RED 14 failed → GREEN 51/51; tam tests/ **1315 passed + 9 subtests
exit 0**; secret scan TEMİZ; canlı istek/emir YOK.
