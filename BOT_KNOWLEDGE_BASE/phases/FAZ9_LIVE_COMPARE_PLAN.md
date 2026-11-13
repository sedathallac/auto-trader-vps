# Faz 9 — Canlı Veri Toplama ve İzleme Planı (compare modu)

Tarih: 2026-11-13 · Dal: feature/futures-release-blockers

## 1. Faz 8 sonucu (180 gün, 8 sembol, 4397 olay, 4 fold walk-forward)
- OOS n_accepted = 0, verdict = INSUFFICIENT_DATA. Taban kazanma oranı %42.4, taban beklenti −%0.137/işlem (maliyet sonrası).
- Whale p_win (taker-akış vekili) OOS Brier skill = −0.0027 → vekilin tahmin gücü yok.
- Eşik taraması (p_real × min_events × fdr × min_families, 32 kombinasyon): p_real 0.65/0.60/0.55 hepsinde n_accepted = 0.
  p_real = 0.50'de n = 347 (min_fam=2) / 1329 (min_fam=1), kazanma %43.5 / %43.2, beklenti −%0.136, p = 0.34 / 0.22 → NO_EDGE.
  0.50 nötr prior olduğundan bu "filtreyi kapatmak" demek; kenar yok.
- "n_accepted > 30 ve kazanma > %50" sağlayan hiçbir eşik YOK. Eşik gevşetmek bunu çözmez; varsayılanlar (0.65 / %0.15) değiştirilmemeli.
- E_net eşiği (%0.15) backtest'te ölçülemez: whale_engine OBI/duvar/OI verisine ihtiyaç duyar, tarihsel REST'te yok.

## 2. Hedef
Backtest'te olmayan gerçek veriyi (OBI, duvar kalıcılığı, OI Δ, canlı sinyal sonuçları) GERÇEK EMİR OLMADAN toplamak; sonra
whale p_win kalibrasyonu ve sinyal tablosunu bu veriyle yeniden değerlendirmek.

## 3. Konfigürasyon
```
HB_OFFLINE=1                 # open_coin dry-run'a zorlanır, HB API'ye gidilmez
SCIENTIFIC_SIZING=compare    # Faz 3
HIERARCHICAL_SCAN=compare    # Faz 4
WHALE_HUNTING=compare        # Faz 5 -> ~/whale_metrics.db
STRATEGY_CONFIRMATION=compare# Faz 6 -> ~/signal_outcomes.db
# MAX_OPEN_POSITIONS dokunulmaz (0 = acil durum, yeni giriş yok)
python auto_trader.py --loop
```
Hiçbir bayrak `on` olmaz. risk_ledger.db'ye yazma yolu değişmez; compare modları ayrı DB'lere yazar.

## 4. Başlamadan önce kontrol (go/no-go)
1. `HB_OFFLINE` gerçekten gerçek emri engelliyor mu: log'da "🔌 HB_OFFLINE ... dry-run" satırı + HB'de emir yok.
2. Tüm testler yeşil (pytest), flag'ler yalnız ortam değişkeniyle.
3. API anahtarı yalnız okuma yetkili mi (mümkünse) — emir yetkisi gereksiz.
4. Rate-limit: Faz 4/5 ek REST çağrıları Binance limitinin altında kalıyor mu (ilk saat izlenir).

## 5. Süre ve örneklem hedefleri
| Veri | Hedef | Tahmini süre |
|---|---|---|
| whale_metrics satırı (OBI ile) | ≥ 2000, ≥ 3 farklı rejim günü | 2–4 hafta |
| signal_outcomes çözülmüş satır / (sinyal×rejim) | ≥ 100 (confirm_min_events) | 4+ hafta |
| Çözülmüş compare kararı (Faz 5/6 allow vs gerçek sonuç) | ≥ 300 | 4 hafta |
Süre dolmadan karar verilmez; ara bakış yalnız sağlık içindir (peeking = overfit riski).

## 6. İzleme (günlük)
- Süreç canlı mı, log'da `Faz3/4/5/6[compare]` satırları geliyor mu, hata/Traceback sayısı.
- whale_metrics.db: günlük satır sayısı, `quality_ok` oranı, `spoof` oranı, OBI boş oranı (veri-yok = fail-closed oranı).
- signal_outcomes.db: yeni/çözülen satır, sinyal×rejim dağılımı.
- API: 429/418 sayısı. Herhangi biri → süreç durdurulur, tekrar denenmez.
- Disk: DB büyümesi.

## 7. Kabul ölçütleri (4 hafta sonunda, önceden sabit)
Faz 5 (whale) `on`a ancak şunlar birlikte sağlanırsa aday:
- OBI tabanlı p_win OOS Brier skill > 0 (zaman-bölünmüş), kabul edilen n ≥ 30,
- kabul edilenlerde kazanma oranı taban + Wilson alt sınırı > taban, maliyet sonrası beklenti > 0,
- permütasyon p < 0.10.
Faz 6 (strateji) için aynı kriterler signal_outcomes çözülmüş verisiyle (canlı + offline ayrı raporlanır).
Sağlanmazsa: filtreler `off` kalır, sonuç "kenar yok" olarak kayda geçer; eşik gevşetme ile kurtarma denenmez.
Faz 3/4: compare logları sağlık/tutarlılık açısından gözden geçirilir (boyut ve kaldıraç farkları risk sınırları içinde mi).

## 8. Durdurma koşulları
Gerçek emir izi, tekrarlayan rate-limit, DB bozulması, bellek/disk sorunu, beklenmeyen Traceback döngüsü.

## 9. Sonrası
Veri toplandıktan sonra `confirm_backtest` whale kalibrasyonu OBI verisiyle genişletilir; karar raporu yazılır; `on` geçişi ayrı onay ister.
