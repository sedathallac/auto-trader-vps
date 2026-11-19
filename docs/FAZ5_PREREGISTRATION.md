# FAZ 5 ÖN-KAYDI — H5: TERS KURAL (durum: TASLAK, insan onayı bekliyor)

Bu dosyanın commit'i HERHANGİ bir FAZ 5 sonucu üretilmeden ÖNCE atılmıştır
(B.6). Öneri yasağı: bu dosya tek bir hipotez + tek bir kabul kapısı tanımlar;
parametre araması/grid/optimizasyon TANIMLI DEĞİLDİR ve kapı geçilemezse
arama da başlamaz ("EDGE YOK" son deneydir).

## B.1 Tek hipotez
**H5:** Mevcut sinyalin **tersi** (aynı giriş zamanları/boyutları, ters yön,
aynalı bariyerler) **kötümser maliyet senaryosunda** net > 0 verir.
Başka hipotez test edilmez. Deneme sayacı: bu deney N=19; FAZ3 WF ailesinden
18 config + bu 1 = **N=20** — DSR (deflated Sharpe) N=20 düzeltmeli hesaplanır;
bu sayıya koşan başka deney eklenirse N buraya YAZILARAK güncellenir ve
güncelleme sonucu değiştiremez (sadece daha aleyhe çevrilebilir).

## B.2 Veri
- **Birincil OOS (tek karar verici):** dokunulmamış 2020–2023 aylık
  verisi, data.binance.vision public arşivi (imzasız).
  - **Sembol-varlık / survivorship notu:** 2026'nın 28 sembolünün bir
    kısmı 2020–23'te mevcut DEĞİL. Kural: yalnız o dönem listede olan
    semboller koşulur; sonuç "2020–23'te mevcut olan alt kümede" verilir;
    mevcut olmayanlar için sıfır/getiri-yok uydurulmaz. survivorship yanlılığı
    raporda ayrı satır olarak belirtilir ve H5 yorumunda aleyhe varsayılır.
  - **Rejim farkı notu:** 2020–23 (boğa/ayı döngüleri, düşük funding rejimi)
    2026 rejiminden farklıdır; bu fark hüküm cümlesinden düşülemez.
- **2024-01→2026-10:** KEŞİF VERİSİDİR (İŞ A bu dönemden beslendi) — yalnız
  tutarlılık kontrolü; karar verici değildir; sonuçları B.5 kapısına giremez.
- **İleri-shadow:** tamamen ayrıdır; bu ön-kaydın parçası değildir, ayrı
  ön-kayıt gerektirir.

## B.3 Deney tasarımı
1. **Sinyal-düzeyi eşleştirilmiş test (ANA):** her sinyal olayında iki kol
   (LONG/SHORT) birlikte simüle edilir; portfoy filtreleri/elemeler KAPALI;
   fark saf yön etkisidir. Sinyal kaynağı: FAZ3'te kullanılan üretici kuralların
   AYNISI (yeniden yazılmaz, tek kaynak).
2. **Portfoy-filtreli sürüm (ikincil):** canlıdaki eşzamanlı pozisyon/limit
   filtreleriyle; sapma raporlanır, kapı ana testten hesaplanır.
3. **Bariyer aynalama kuralı (bağlayıcı):** TP_long(E+k) → TP_short(E−k);
   SL_long(E−m) → SL_short(E+m); trailing yönü ters çevrilir; E aynen korunur.
   Funding işaretleri ters dönmez — gerçekte olduğu gibi uygulanır (short
   pozitif funding'de ÖDER), verideki gerçek funding oranları kullanılır.
4. **Maliyet senaryoları (cm-v1-20261119 üzerine):**
   - iyimser: fee 0.05%/side, slippage 0;
   - baz: fee 0.05%/side, maker/latency ÖLÇÜLEMEDİ etiketi korunur;
   - **kötümser (kapının senaryosu):** fee 0.05%/side + likidite-kademeli
     slippage: USDT-hacim kuyruğuna göre bps cezası (kademeler ve bps değerleri
     İLK GERÇEK KOŞUDAN ÖNCE bu dosyaya yazılır ve dondurulur; small-cap
     kademesi ayrı ve daha yüksek), funding tam tahakkuk. Kötümser senaryo
     DEĞERLERİ ÖLÇÜM DEĞİL VARSAYIMDIR — çıktıdaki model_id "cm-v1-pess-v1"
     olarak ayrıca işaretlenir.

## B.4 Harness kontrolleri (koşulmadan gerçek veriye dokunulmaz)
(i) sentetik düz rastgele yürüyüş → gross ≈ 0 ve net ≈ −(maliyet) olmalı;
(ii) enjekte momentum → LONG-takipçi kol, enjekte mean-reversion → ters kol
işaretini doğru geri vermeli (sign-recovery). Bunlar `faz5_harness.py`
testleridir (sentetik; gerçek veri yok).

## B.5 KABUL KAPISI TASLAĞI (insan onaylamadan uygulanmaz)
H5 KABUL için **dördü birden** (hepsi KÖTÜMSEN senaryoda, ana tasarımda):
1. sembol-kümeli bootstrap CI alt sınırı > 0 (B=10000, sabit seed);
2. zaman-bloklu (çeyrek-varlık blokları) bootstrap CI alt sınırı > 0;
3. DSR(N=20) ≥ 0.95;
4. yıl-bazında işaret tutarlılığı (2020,2021,2022,2023 net'leri hepsi > 0)
   VE en büyük 3 sembol çıkarıldığında toplam > 0.
Hiçbiri tek başına yeterli değil; biri düşerse → **"ters kural: EDGE YOK"**,
bu son deneydir; parametre araması BAŞLAMAZ; sonuç BÖLÜM-3'e yazılır.

## B.6 Sıra
Bu dosya + harness commit'i → (insan onayı: kapı + kötümser bps değerleri) →
fetch (public arşiv, rate-doktrini) → harness kırmızı/yeşil → TEK koşu → rapor.
Ön-kayıt commit'inden önce hiçbir FAZ 5 sayısı üretilmez; üretim olursa
sayılar ÇÖP sayılır ve kayıt altına alınır.

## B.7 BLOK 3 İŞ A bu gerekçeyi nasıl değiştirdi (dürüst durum)
İŞ A düzeltilmiş veride TERM2(yön/sembol seçimi) = −21.36 **ancak CI[−60.4,+15.5]
sıfır içeriyor, perm p=0.216, MDE ±55.8 USDT (n=60 güçsüz)** → eski "(b) yön
zararı kesin" dayanağı düştü; hükmün (d). H5'in yön-temeli artık NİTELİKSEL
(TERM2 negatifi + WF 18/18 negatifi), nicel-kanıtlı değil. Bu, H5'i yasaklamaz
ama ön-kayıttaki tek gerekçe buysa **kapıya girmeden operatör "değer mi?"
sorusunu tartışmalıdır** — kayıt gereği dürüstçe buraya yazılmıştır.
