# FAZ 3 ÖN-KAYDI (pre-registration) — 2026-11-19, yazar: claude

**Bu dosya HERHANGİ bir FAZ3 sonucu üretilmeden ve veri arşivi indirilmeden
ÖNCE commit edilmiştir (commit geçmişi kanıtı).** Sonuç gördükten sonra
tanım/metrik değişikliği YASAK; sapma gerekirse bu dosyaya "DEVIATIONS"
bölümü olarak, nedeni ve sonucu birlikte eklenir.

## 1. Soru ve hipotezler

S: Botun sinyal-jeneratörü (strategy_engine @ commit **1971059**,
`fix/exit-protection-faz18-20261119`) GERÇEKÇİ maliyet modeliyle, geçmişte
(görülmemiş OOS pencerelerde) pozitif net-ediş edge'i taşır mı?

- H1 (edge): sinyal-kapılı long/short girişler, bot geometrisi (ATR-SL/TP) ve
  ölçülen maliyetlerle net-ediş > 0.
- H0 (null-1): aynı geometri + aynı maliyetler, RASTGELE girişler (aynı
  sembol-gün yoğunluğu) — H1 bunu geçemezse edge YOK.
- Null-2: majelerde buy&hold; Null-3: yön-coin-flip aynı maliyetler.
- **Beklenen sonuç EDGE YOK'tur** (Mission B [CF]/[FWD] bulguları: seçim
  avantajsızlığı, n-yetersizliği). "EDGE YOK" başarısızlık DEĞİLDİR; kabul
  edilirse parametre araması DURDURULUR (Mission C emri).

## 2. Veri (kaynak etiketi: BACKTEST)

- data.binance.vision **USDT-M futures monthly klines**, 15m + 1h + 4h,
  zip + SHA256 `.CHECKSUM` DOĞRULAMASI zorunlu (uyumsuzluk → dosya reddi).
- Evren A (bot-evreni): Mission C'nin sabitlediği **42 sembol**
  (`raw/streams/algo_sembol_evreni.txt`) — gerçekleşen işlemlerden türediği
  için sonradan-seçilmiş-kazanan DEĞİL; ancak **listing-tarihi kısıtı
  zorunlu**: sembol yalnız kendi futures-listing'inden itibaren (çoğu alt
  2025'te listelendi; "çok-yıllık" onlar için fiziksel olarak yok). Bu
  SURVIVORSHIP-LISTING notu raporda ayrıca yazılacak.
- Evren B (referans): BTC, ETH, SOL, BNB, XRP, ADA, DOGE, LINK (2022-01'den).
- Delist edilmiş semboller evren-dışı BIRAKILMAZ (listeden düşme de bilgi).
- Test aralığı: 2024-01-01 → 2026-10-31 (ay-dosyası bütünlüğü; 2026-11 eksik
  ay olduğu için dışarıda).

## 3. Strateji tekrarı (code-as-spec)

Girişler strategy_engine'in KENDİ kodundan üretilir (yeniden-yazım yok):
offline sahte-boundary, arşiv CSV'lerinden klines döner; `1971059`'daki
dedektör kuralları (FAM_EXHAUSTION/YAPI/HACİM/TREND-veto, eşikler dâhil)
olduğu gibi çalışır. Uygulama protokolü:
- Sinyal kapalı-15m-bar üzerinde hesaplanır; giriş SONRAKİ barın açılışı
  (lookahead YOK); yön = sinyalin önerdiği yön (reverse_all dâhil mevcut kod).
- SL/TP geometrisi sizing_policy.atr_sl_multiplier/atr_tp_multiplier ile
  (grid aşağıda); tetikleme intrabar: LONG-SL düşük≤trig → trig'de çıkış.
- Eşzamanlılık: sembol başına 1 pozisyon, portföyde **en fazla 3**
  (canlı LIVE_MAX_OPEN_POSITIONS=3 gözlemi).
- Sizing: notional = min(0.20×equity, 0.015×equity/sl_pct); equity=1000 ile
  başlar, işlemler arası BİLEŞİK (compounding test-edilir, lineer de rapor).

## 4. Maliyet modeli (ölçülmüş, [FILL]-kalibrasyonlu)

- Komisyon: **%0.05/taraf taker** (220 fill'de ölçülen: örn. 0.09612650/
  192.253 = 0.0005). Tüm çıkışlar taker (canlı TP HB-market kapatışıydı).
- Slippage: SL/entry için 20 ölçülen algo-tetiklemesinin |actual−trig|/trig
  amprik dağılımından BLOCK-BOOTSTRAP ile örnekleme (medyan %0.0099, max
  %0.0867); TP/normal çıkış bar-kapanışında + aynı dağılım.
- Funding: 15m-bar sayısından aylık oran tahmini (evren-bazlı ortalama,
  sabit; belirsizlik raporda).

## 5. İstatistik protokolü

- Fold'lar: takvim-ay blokları; **purged & embargoed walk-forward** —
  embargo = SL/TP vade tavanı (max 96 saat = 96×15m bar) + 1 gün.
- Metrikler: işlembaşına net-ediş (%notional); günlük portföy-getisi;
  yıllık Sharpe; **Deflated Sharpe Ratio** deneme-cezasıyla; PBO
  (CSCV, 16 blok); block-bootstrap (%95 CI, blok=21 gün); t-istat.
- **Deneme bütçesi (DSR cezası):** grid = atr_sl_multiplier {1.0,1.5,2.0} ×
  atr_tp_multiplier {2.0,3.0,4.0} × confirm_min_families {1,2} ×
  min_coin_quality {0.45,0.55} = **36 config + 1 sabit-baz = 37 deneme.**
  Bundan FazLA deneme YAPILMAZ; yapılırsa DEVIATIONS'a yazılır ve DSR
  yeniden hesaplanır.
- Hybrid karar, TÜM-HEPSİ-BİRLİKTE sağlanacak (kabul-kapısı TASLAK — nihai
  eşik İNSAN kararıdır):
  1) En iyi config'in OOS toplam net'i > 0 VE block-bootstrap %95 alt-sınırı > 0;
  2) WF fold'larının ≥ %60'ında OOS net > 0;
  3) DSR > 0.95 (37 deneme cezasıyla);
  4) PBO < 0.50;
  5) Null-1 (rastgele giriş) OOS net'i anlamlı aşılmış (fark bootstrap CI > 0).
- Eşiklerden biri bile tutmazsa verdict **EDGE YOK**; veri yetersizse
  (sembol×ayCoverage < ön-kayıttaki minimum: Evren A'da toplam ≥ 480 sembol-ay
  15m verisi) verdict **YETERSİZ KANIT**.
- Çoklu-test disiplin: rapor EN İYİ config'i DEĞİL, grid'in tam dağılımını
  (heat-map) bildirir; "şanslı köşe" tartışması kanıtlı kapatılır.

## 6. Raporlama

`RESULTS_FAZ3.md` yalnız ön-kayıttan SONRA ve protokole uygun üretilir; her
sayı BACKTEST etiketi + veri-örneği-hacmi ile; BLOCKED/INSUFFICIENT ayrı
başlık; kârlılık iddiası ancak ve ancak kabul-kapısının İNSAN onayıyla ve
canlı-shadow doğrulamasıyla mümkün (FAZ4) — FAZ3 tek başına "kârlıdır"
iddiası İÇERMEZ.

## DEVIATIONS

(yok)
