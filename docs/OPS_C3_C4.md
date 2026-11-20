# C.3 — Denetim araçları için ayrı anahtar gereksinimi (GEREKSİNİM; uygulama YOK)
# C.4 — Deploy hazırlık listesi (HAZIRLIK; uygulama YOK)

## C.3 Gereksinimler (kanıt: faz9-audit/ban_timeline_c1_20261120.md)
2026-11-19 IP-BAN zincirinde yasağı denetim collector'ları tetikledi (ilk
−1003, asyn_list bitişinden 7 sn sonra; üç asyn gözlemi üç ban). Neden
şiddetlendi: collector'lar (a) aynı VPS IP'sinden, (b) canlı botun anahtarıyla
(/proc/<pid>/environ okuması), (c) botun ağırlık havuzunu tüketerek koştu.

ZORUNLU GEREKSİNİMLER (uygulanırsa bu belge güncellenir):
1. **Ayrı API anahtarı**: denetim için üretim anahtarından TAMAMEN ayrı;
   izinler yalnız-oku (spot/margin/futures READ; TRADE/ORDER PERMS KAPALI);
   Binance konsolda **IP-beyazlistesi** yalnız denetim makinesinin sabit IP'si.
   Not: 418 IP-ban ağırlık-havuzunu izole etmez — anahtar ayrımı hasarı
   sınırlandırır, **asıl izolasyon ayrı çıkış IP'si/makinesinden koşmaktır**.
2. **/proc/<pid>/environ anahtar okuma yolu kaldırılsın** (öneri; UYGULANMADI):
   collector'ların canlı sürecin ortamından anahtar çalması yerine ayrı
   `/etc/audit_api_key` (0600, audit kullanıcısı) veya interaktif giriş.
   Bu yol hem F25-benzeri ortak-IP felaketini hem yetki genişlemesini keser.
3. Ağırlık bütçesi: collector toplam W1M ≤ bot-payı bırakacak eşik (ör. 1200);
   429/418'de retry YOK — DUR (mevcut doktrin korundu, escalation bunu kanıtladı).

## C.4 Deploy hazırlık listesi (UYGULAMA YOK — operatör onayı + insan kararı)
Sıra (bağlayıcı): **F25 → A-2 → A-3 → A-9 → A-10**. Her adım AYRI deploy,
her birinden önce ve sonra aşağıdaki kontroller. Production'a alma kararı
insana aittir; bu belge yalnız hazırlıktır.

| Adım | İçerik | Kanıt/SHA |
|---|---|---|
| F25 | sentinel fail-safe + M-2 + açılış öz-testi + C.2 (None) | 7f84149 + bu blok C.2 commit'i (FAZ18 dalı) |
| A-2 | (eşleme bu bloğun kapsamı dışında — operatör ana denetim listesinden SHA'yı buraya yazacak; **doğrulanmadan deploy YOK**) | TBD |
| A-3 |同上 TBD | TBD |
| A-9 |同上 TBD | TBD |
| A-10 | ATR=0 tanımı + görünür sinyal-kaybı logu | f7c99d4 |

**Her adım öncesi:**
- `python3 tools/orphan_report.py` (yalnız-oku; canlı pozisyon↔algo-SL
  eşlemesi) — **tek canlı-okuma olarak İNSAN ONAYIYLA** koşılır; SL_YOK/YETİM
  bulgusu varsa deploy ÖNCESİ operatöre raporlanır.
- `git rev-parse HEAD` = beklenen SHA; venv `pytest -q tests/` yeşil.
- Yedek: `/var/lib/auto_trader_faz9_live/` DB + config kopyası (salt-kopya).
**Her adım sonrası smoke (dakikalar içinde):**
- Servis logunda `[RISK] F25 öz-testi:` satırı GÖRÜNÜR ve BAŞARILI;
- `risk_state` satırında `is_halted` **1 KALIR** (HALTED durumunu hiç kimse
  sıfırlamaz — sıfırlanırsa DERHAL rollback);
- Son 500 log satırında −1003/418 YOK; `💰 Equity=` satırları None-değil;
- Tek-komut rollback: `sudo systemctl stop auto-trader && sudo ln -sfn <önceki-release> /opt/auto_trader_faz9 && sudo systemctl start auto-trader`
  (symlink-sürüm yönlendirmesi varsa; komut sürüm-kurulumuna göre deploy
  listesinde TÜMÜYLE yazılı teslim edilir — burada UYGULAMA yok).
Rollback tetikleyicileri: öz-test FAIL, is_halted değişimi, 418, yeni equity
sentinel'i. Rollback sonrası HALTED korunduğu için pazarlık gerekmez.

## E5 — FAZ18-B5 notu: tools/orphan_report.py sertleştirme (UYGULANDI — kod, deploy DEĞİL)
C.3/2'deki "**/proc/<pid>/environ anahtar okuma yolu kaldırılsın** (öneri;
UYGULANMADI)" maddesi BU blokta UYGULANDI: pid-environ okuma yolu tamamen
silindi; anahtar artık YALNIZ `--env-file` (systemd EnvironmentFile) ya da
süreç ortamından alınır — botun gerçek adları `BINANCE_KEY`/`BINANCE_SECRET`
(kanıt: auto_trader.py:5240), anahtar argv'de/yazdırmada asla görünmez.
Aynı blokta araç sözleşmesi: **E1** sembolsüz tekil tarama (GET
openAlgoOrders W=40 + GET openOrders W=40 + positionRisk W=5; sembol
parametresi yalnız `--symbol` derinleşmesinde); **E2** GET
/sapi/v1/account/apiRestrictions ön-kontrolü — `enableWithdrawals=true` ya
da `ipRestrict=false` ise Araç DURUR (exit 4; `--skip-preflight` yalnız mock
test); **E3** final satır `VERDICT: OK|FINDINGS|INCOMPLETE` → exit 0/1/2,
exit 3 = 418/429 (Retry-After loglanır, retry YOK), "N/N sorgu başarılı" +
 sorgu başına `X-MBX-USED-WEIGHT-1M` logu, `--max-weight` (varsayılan 300)
bütçe aşımında kalan sorgular koşmaz → INCOMPLETE; **D2/D5** TP≠SL ve
SL-geçerlilik denetimi (algoStatus NEW/PARTIALLY_FILLED dışı canlı sayılmaz
— F20 muhasebe körlüğü kapandı). Salt-GET garantisi ve ledger mode=ro +
PRAGMA şema doğrulaması testlerle kilitli (tests/test_faz18_orphan_report*.py).
C.4 "Her adım öncesi" maddesindeki tek-canlı-okuma komutu yeni bayraklarla
koşulur: `python3 -m tools.orphan_report --env-file /etc/audit_api_key`.

E5 NOTU (olduğu gibi eklendi): Binance FUTURES tarafında salt-okunur API
izin ayrımı sınırlıdır — futures anahtarında "yalnız-oku" izni fiilen
çalışmayabilir (trade izinleri kaldırılsa bile uç nokta erişimi aynıdır;
para çekme zaten futures anahtarında yoktur). Bu durumda araç, anahtarı
kendi taşımadan salt-GET sözleşmesiyle sınırlı kalmalıdır; alternatif:
BOT'UN KENDİ watchdog çıktısını (pozisyon↔algo-SL eşleme durumu) periyodik
olarak DOSYAYA YAZMASI ve denetimin o dosyayı okuması — canlı hesaba hiçbir
istek atılmadan aynı hüküm üretilir. (Not eklenir; araç bu amaçla
DEĞİŞTİRİLMEDİ.)
