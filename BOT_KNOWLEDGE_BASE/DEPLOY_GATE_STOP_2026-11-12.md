# Üretime Geçiş — GÜVENLİK KAPISI DURDURMA KAYDI

Tarih: 2026-11-12 · Branch: `feature/futures-close-coordinator` · HEAD: `61bef7f`
Karar: **DEPLOY/RESTART YAPILMADI.** Aşağıdaki hard STOP koşulları nedeniyle durduruldu.
Hiçbir canlı emir gönderilmedi, VPS'e bağlanılmadı, dosya değiştirilmedi.

Kullanıcı canlıya alma onayı vermiştir; ancak onay "güvenlik kapıları başarıyla
geçildiğinde" şartına bağlıdır. Kapılar geçilmedi.

## STOP koşulları ve kanıt

### 1) KRİTİK — Tek kapanış otoritesi RUNTIME'da etkin değil (bağımsız denetim)
`grep request_close auto_trader.py` → yalnızca satır 284 (docstring). CloseCoordinator,
CloseIntentStore ve PositionMigrator üretim ana döngüsünde **hiç çağrılmıyor**. Canlı
kapanış/SL yolu hâlâ `open_coin` içindeki doğrudan market close + `place_algo_sl`
(auto_trader.py ~1173, ~1199). Bu iş paketinin çekirdeği (K1=B tek otorite, çift-kapanış
koruması, cross-process intent, migration) commit'li ama **inert**. Deploy, var olmayan
bir korumayı "aktif" sanmaya yol açar → **sahte güvenlik**.
→ DURMA: "Test veya bağımsız denetimde kritik açık var."

### 2) Ortam — bu oturum ÜRETİM VPS'i DEĞİL
`whoami`=reisfurkan · `hostname`=FURKAN · `HOME`=/home/reisfurkan (Zorin geliştirme
makinesi). Üretim VPS: `root@179.61.207.234`. AŞAMA 3 canlı salt-okunur envanter
(servisler, Binance position mode, açık pozisyonlar, emirler, SL, HB executor, DB)
bu oturumdan **yapılamaz** → tüm canlı envanter UNKNOWN.
→ DURMA: "Canlı pozisyon/emir/SL envanterinde UNKNOWN var."

### 3) Release aktarımı güvenilir değil
`git remote -v` → `origin = root@179.61.207.234:/root` (üretim VPS /root). Daimi kural +
AŞAMA 2: VPS /root'a doğrudan push YASAK. Bu oturumdan doğrulanmış alternatif güvenli
aktarım kanalı yok.
→ DURMA: "Git hedefi veya release aktarım yöntemi güvenilir değil."

### 4) Yedek / rollback hazırlanamıyor
VPS erişimi olmadan üretim dosya yedeği ve rollback prosedürü gerçek yollarla
hazırlanıp DOĞRULANAMAZ.
→ DURMA: "DB yedeği veya rollback başarısız."

### 5) Anahtar rotasyonu doğrulanamıyor + HEDGE
Daha önce ifşa olmuş Binance/VPS erişim bilgilerinin rotasyon durumu bu oturumdan
doğrulanamaz (kullanıcı işi). HEDGE SL oluşturma yolu desteklenmiyor; canlı HEDGE
pozisyonu varsa (bkz. #2, UNKNOWN) güvenli geçiş imkânsız.

## Sonuç
En az 4 bağımsız hard STOP koşulu aktif. Deploy/restart yapılmadı. Çözülmesi gereken
ÇEKİRDEK teknik karar: CloseCoordinator/intent/migration canlı döngüye (run_once/
open_coin) entegre edilmeden deploy anlamlı koruma sağlamaz. Bu, ayrı bir entegrasyon
increment'i (gerçek kapanış/SL yollarının CloseCoordinator'a yönlendirilmesi) ve
ardından kapıların yeniden değerlendirilmesini gerektirir — sermayeyi etkileyen bir
karar olduğundan kullanıcı yönlendirmesi gerekir.

Kod tarafı (inc12.1 dahil) tamam: tests/ **250 passed**, ruff F/E9 temiz, py_compile
temiz. Açık pozisyonların mevcut koruması bu karardan ETKİLENMEZ (dağıtım yapılmadı;
üretim olduğu gibi kalır).
