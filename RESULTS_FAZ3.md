# FAZ 3 SONUÇLARI — OOS EDGE TESTİ (BACKTEST)

**VERDICT: EDGE YOK.** (Ön-kayıt §1 beklentisiyle UYUMLU; "EDGE YOK"
başarısızlık değildir — §5 emriyle parametre araması DURDURULDU.)

- Protokol: `PREREGISTRATION_FAZ3.md` @93d58d7 + DEVIATIONS D-1..D-4
  @7698892/@dcc23cb — TÜMÜ sonuç üretiminden ÖNCE commit'li.
- Koşum: `research/faz3-oos-prereg-20261119` @dcc23cb; ham sonuç:
  `faz9-audit/raw/streams/faz3_results.json` + `faz3_nulls.json`
  (Git-dışı audit alanı).
- Etiket: aşağıdaki HİÇBİR sayı canlı/ledger verisiyle birleştirilmez —
  kaynak **BACKTEST**; maliyet-girdi kaynakları [FILL]/[ALGO]/[INC] ayrıca
  belirtilir.

## 0. Kanıt-hacmi (YETERSİZ KANIT eşiği geçildi)

- Evren A 15m kapsamı: **641 sembol-ay** (eşik 480 → GEÇTİ); majörler
  +272 sembol-ay. 42/42 sembol tarandı, 0 tarama-hatası; 2.308 düz-bar
  §D-4 guard'ıyla atlandı (PUMPUSDT 97.562 karar üretti).
- Pencere 2024-01→2026-10; 1.034 işlem-günü; config başına 49.0–56.3k
  portföy-işlemi (≤3 eşzamanlı, sembol başına 1, bileşik).

## 1. H1 ana sonucu — 18 config'ın TAMAMI kaybediyor [BACKTEST]

İşlem-başı net-ediş (komisyon 0.001 RT [FILL] + amprik slipaj×2 [ALGO] +
funding −4.18bp/gün [INC] düşülmüş):

| Metrik | Değer |
|---|---|
| 18 config mean_net aralığı | **−%0.357 … −%0.301** (medyan −%0.329) |
| En-iyi config (Sharpe'a göre) | sl1.0/tp2.0/fam1 → mean −%0.301, yıllık Sharpe **−12.2** |
| Block-bootstrap %95 CI (en-iyi) | **[−%0.318, −%0.284]** — ÜST sınır bile < 0 |
| DSR (N=19, §D-2) | **0.0** |
| PBO (CSCV 16 blok, grid+null) | 0.0 (bkz. §4 uyarı — burada anlamsız) |
| WF 9 fold OOS | **0/9 pozitif** (fold ort. −%0.19…−%0.42) |

Heat-map dürüstlüğü: hiçbir "şanslı köşe" yok — 3×3×2'nin 18 hücresinin
18'i de negatif ve aralık dar (±%0.03): kayıp sinyal-jeneratörünün değil,
yapının (maliyet + ters-seçim) ürettiği bir düzey.

## 2. Null modeller [BACKTEST]

| Model | n | mean_net | %95 CI |
|---|---|---|---|
| Sinyal (en-iyi config) | 56.334 | −%0.301 | [−%0.318, −%0.284] |
| **null-1** rastgele bar + rastgele yön (kapı kapalı) | 15.961 | −%0.143 | [−%0.166, −%0.120] |

> null-1 ticaret-kümesi, tohumlar korunarak bağımsız yeniden-üretildi
> (`faz3_null1_recount.py`, n=15.961, mean −%0.1427): CI ana koşunun CI'ı
> ile BİREBİR aynı → determinizm kanıtı. Sembol başına 600 olayla
> sınırlıdır (ön-kayıt null-yoğunluğu tanımı).
| **null-3** AYNI girişler, yön yazı-tura | 35.829 | −%0.119 | [−%0.134, −%0.104] |
| null-2 buy&hold (2024-01→2026-10, majör) | — | BTC +%83 BNB +%126 XRP +%103 ETH −%1 ADA −%68 | — |

Yorum (BACKTEST içi tutarlı, tek cümle): null-1/null-3'ün −%0.12…−%0.14'ü
≈ saf maliyet-erdirme; sinyalin −%0.30'u bundan **CI'ları ayrık biçimde
kötüdür** → giriş ZAMANLAMASI ve YÖNÜ, paranın rastgele el değiştirmesine
göre ek −%0.16/işlem değer KAYBETTİRİYOR (ters-seçim imzası). Bu, canlı
[LED]/[FILL] −%28.26 kaybın [FWD] bulgularıyla AYNI yöndedir ama
onlarla BİRLEŞTİRİLMEZ (etiketler ayrı).

## 3. Kabul kapısı (§5, TÜMÜ-birlikte) — 1/5

| # | Ölçüt | Sonuç |
|---|---|---|
| 1 | OOS net > 0 VE bootstrap ALT CI > 0 | ❌ CI'ın ikisi de negatif |
| 2 | WF fold'larının ≥%60'ı pozitif | ❌ 0/9 |
| 3 | DSR > 0.95 (N=19) | ❌ 0.0 |
| 4 | PBO < 0.50 | ✅ 0.0 — ama bkz. §4 |
| 5 | null-1 ANLAMLI aşılmış | ❌ TAM TERSİ: signal < null-1 (CI ayrık) |

→ **EDGE YOK.** §1 emri: "kabul edilirse parametre araması DURDURULUR."
FAZ4 (shadow/canary) için ÖN-KOŞUL SAĞLANMADI — FAZ4 yalnız İNSAN
onayıyla ve ancak farklı bir tezle gündeme alınabilir.

## 4. Uyarılar / dürüstlük payları

- **PBO burada yanıltıcı:** CSCV rank-stabilitesi ölçer; 18 config'ın hepsi
  istikrarlı biçimde kaybettiğinde "IS-en-iyi OOS'ta da en-iyi" kolaydır →
  PBO≈0. Bu kapı-paso'su edge kanıtı DEĞİLDİR (koşul AND; diğer 4'ü çöktü).
- **WF seçim-istikrarsızlığı:** fold seçimleri sl2.0_tp4.0 ailesi etrafında
  salınıyor (fam1/fam2 değişimi) — IS-şampiyonu bile tutarlı değil.
- **DSR N=19 (§D-2):** orijinal 37'ye göre daha az muhafazakâr; sonuç yine
  de 0.0 — ceza büyüse değişmezdi.
- **Funding** tek 11-günlük [INC] gözlemden sabit yaklaşımdır; işlemlerin
  RT net'i üzerinde payı ~−2bp ve verdict'i değiştirmez.
- **SURVIVORSHIP-LISTING:** alt-coin'lerin çoğu 2025'te listelendi; "çok
  yıllık" onların var olmadığı dönemlerde test edilemezdi (§2 notu). Majör
  2022-öncesi veri test edilmedi (pencere ön-kayıtlı).
- **Kod eşdeğeri:** strateji @1971059 code-as-spec; p_fake nötr (§D-1);
  sıfır-ATR barları canlı gibi atlandı (§D-4, 2.308 bar).

## 5. Faaliyet durdurma maddesi

Mission C emri uyarınca: **H1 çürütüldü; grid/genişletme/yeni parametre
araması YAPILMAYACAKTIR.** Bundan sonra aynı jeneratörle kârlılık iddiası,
yalnız yeni bir tez + yeni ön-kayıt + İNSAN onayı ile mümkündür.
