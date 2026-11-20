# BLOK8 İŞ1 — KÖTÜMSEN MALİYET KADEMESİ ÖN-KAYDI (DONDURULDU; 2026-11-20)

H5 SONUCU GÖRÜLMEDEN YAZILDI ve ayrı commit'lendi. Hesaplama betiği:
`faz8_cost_freeze.py` (tekrar koşulabilir); ham çıktı:
`faz9-audit/faz8_cost_freeze_20261120.{json,txt}`. Kaynak etiketleri: tümü
[EXCHANGE] ham arşiv; hiçbir HARNESS/BACKTEST sayısı maliyete girmedi.

## Kural (donduruldu)

`C_frozen_RT = 2 × C_commission + 2 × S_exit_p75`

- **C_commission = Σfee/Σnotional = %0,04983 /fill-tarafı** [EXCHANGE —
  binance_fills.csv, 220 tekil fill (id-dedupe: 220/220), Σfee 6,4635 USDT,
  Σnotional 12 971,92 USDT, pencere 2026-11-08→18; commissionAsset=USDT
  220/220, trades_fromid_fix ndjson ile çapraz-kontrol]. Gerçekleşen PnL /
  funding ile karıştırılmadı (komisyon kolonu ayrı).
- **S_exit_p75 = 3,505 bp = %0,0350** — 20/20 FINISHED STOP_MARKET
  tetiklemesinde triggerPrice↔actualPrice (borsa-ortalaması fill) eşleşmesi
  [EXCHANGE algo arşivi]. Advers işaret normalizasyonu: SELL(long kapat) →
  tp−ap; BUY(short kapat) → ap−tp. Yöntem: linear-interpolation (type-7);
  nearest-rank alternatifi 3,42 bp (fark <0,1 bp). Dağılım: mean 2,07;
  median 0,78; min −0,97; max 8,67 bp; aleyhte 12/20.
- **GİRİŞ SLİPJI: ASSUMPTION — ölçülmedi; çıkış p75'ine eşit varsayıldı.**
  Ölçülmüş gibi sunulamaz. Risk yönü: girişler LIMIT/maker ise muhtemel
  aşırı tahmin; taker-pazarlama ise makul; ölçüm eksikliği §sınırlarda.
- **DONDURULAN ORAN: C_frozen_RT = 2×0,04983 + 2×0,0350 = %0,1698 ≈ %0,170
  round-trip.**

Boyut-notu (dürüst kayıt): BLOK8 metnindeki `C_commission + 2×S_exit_p75`
ifadesinin harfi, komisyonu tek-taraf (0,0498) bırakıp slipaji çift sayar —
tur-başına maliyet boyut tutarsızlığı (komisyon da iki fill'dir). Dondurulan
oran RT-uyumlu 2×C_commission + 2×S_p75'tir; harfi-yorum (%0,1198) DAHA AZ
kötümser olduğu için H5'e bakılmadan önce reddedildi (anti-konservatif
seçim yasağı).

## Önceki iddiaların durumu

- "20 algo tetikleme" — **VERIFIED** (ham arşivde tam 20 kayıt).
- "≈3 bp/çıkış" — **PARTIAL** (ölçülen p75 3,505; mean 2,07).
- "Σ −0,7171 USDT / ort 108 USDT notional" — **NOT VERIFIED**: yerel rapor
  dosyalarında bulunamadı; PnL-tabanlı sayı slipaj değildir. Bizim ölçüm
  fiyat-farkı tabanlı ve bağımsızdır; −0,7171 hiçbir yere girmedi.

## Sonuç-hassasiyeti (ÖNCEKİ analizden taşınır; kapı değil)

| Maliyet kademesi | Ayna-gross | Hesap net |
|---|---:|---:|
| %0,15 | +%0,168 | +%0,018 |
| %0,20 | +%0,168 | −%0,032 |

Dondurulan %0,170 bu iki kademe ARASINDADIR: ayna-net ≈ %0,000 civarı
(büyüklük mertebesi; BLOK7 çıpalarıyla). Bu bir H5 tahmini DEĞİLDİR — yalnız
kapının sıfır-kenar bölgesine yerleştiğini, yani H5'in anlamının "eşdeğerlik
sınavı" olduğunu belirtir. Kademe sonuca göre SEÇİLMEDİ; kural tek başına
belirledi.

## Sınırlar / yapılmadı

- Giriş slipajı ölçülmedi (ASSUMPTION yukarıda). Ölçüm yolu: live userTrades
  ↔ emir referans fiyatı eşleşmesi (gelecek shadow döneminden gelir).
- Tetikleyen SL anındaki fill-ortalama (actualPrice) kullanıldı; çok-parçalı
  fill'ların zaman-içi dağılımı görünmez (bkz. trades_fromid ham verisi —
  eşleştirme yapılabilir, bu ön-kayıtta yapılmadı).
- n=20 → P75 kendi başına gürültülüdür (bootstrap aralığı geniş); kademe
  "büyüklük mertebesi" olarak anlaşılmalıdır.
- Komisyon penceresi (11-08→18) H5'in 2020-23 veri dönemini temsil etmez;
  fee rejimi sabit varsayımı (taker %0,05) — ASSUMPTION.
