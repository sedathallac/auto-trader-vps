# Faz 6 — İndikatör ve Strateji Teyit Katmanı: Tasarım Planı (KOD YOK)

Durum: **ONAYLANDI (2026-11-13)** · Tarih: 2026-11-13
Not: Master Plan Bölüm 8 repoda yok (yönetici dokümanı); plan istenen 4 soruya ve mevcut koda göre yazıldı.

## 0. Özet (yönetici için)
1. **Bugün `decide_side` yalnız TEK girdi kullanıyor: 24 saatlik `priceChangePercent`.** Golden cross, supertrend, destek/direnç, RSI, hacim — hiçbiri YOK. (`auto_trader.py:1122`)
2. Strateji **mean-reversion (kontra-trend)**: ≤ −%3 → LONG, ≥ +%3 → SHORT. Golden cross/supertrend ise **trend-takip** göstergeleridir. Bunları "onay" diye doğrudan eklemek sistemi kendi yönüne **ters** bir sinyalle teyit ettirir (çelişki). Bu yüzden indikatörler **iki role** ayrılıyor: (a) *tükenme teyidi* (dönüş için), (b) *rejim/trend filtresi* (güçlü trende karşı mean-reversion = en sık sahte sinyal).
3. Mimari: sinyal aileleri → her sinyal için istatistiksel `p_fake` → aile-bazlı birleşik skor → **yalnız FİLTRE** (Faz 5 ile aynı ilke).
4. Sahte/gerçek ayrımı **olay-çalışması (event study)** ile: sinyal sonrası yönün kalma süresi, taban çizgisine karşı anlamlılık testi, çoklu-test düzeltmesi, walk-forward doğrulama.
5. Bayrak: `STRATEGY_CONFIRMATION = off | compare | on`, varsayılan `off`.

## 1. Mevcut Durum

```
decide_side(coin):            # tek girdi: coin["priceChangePercent"] (24s)
    chg <= LONG_ON_DIP_PCT(-3.0)         -> LONG  (düşeni al)
    USE_SHORTS and chg >= +3.0           -> SHORT (yükseleni sat)
    else                                 -> None
```
Aday havuzu `scan_top`/Faz 4: hacim ≥ 200M, 3% ≤ |chg| ≤ 15%. Kodda hesaplanan tek teknik değer **ATR** (SL/TP için) ve Faz 5'te hacim/OBI. Trend, momentum, destek/direnç, golden cross, supertrend: **yok**.

**Sonuçlar / riskler**
- 24s değişim **gecikmeli ve kaba**: düşüş çoktan bitmiş (dönüş gerçekleşmiş) ya da yeni başlıyor olabilir; ayırt edilmiyor.
- **Düşen bıçak (falling knife):** güçlü trend gününde %−5 → LONG, trendin devamında stop olur. Rejim bilgisi yok.
- Teyit yok → her aday eşit güvenilir sayılıyor.

## 2. Çoklu Teyit Mimarisi

### 2.1 Sinyal aileleri (korelasyonlu sinyalleri çift saymamak için)
| Aile | Rol (mean-reversion bağlamı) | Sinyaller (kapanmış mumlar, 15m/1h/4h) |
|---|---|---|
| **TREND/REJİM** | **VETO filtresi** | EMA(50/200) golden/death cross durumu, Supertrend yönü, ADX(14) |
| **TÜKENME** | **Teyit** | RSI(14) aşırı bölge + diverjans, Bollinger/fiyat z-skoru, ardışık aynı yönlü mum sayısı |
| **YAPI** | **Teyit** | Destek/direnç (pivot/swing + hacim profili) yakınlığı, fitil/reddedilme mumu |
| **HACİM** | **Teyit** | Hacim klimaksı (spike sonrası sönme), Faz 5 CVD/OBI (yeniden kullanım), `_volume_confirmation` |

> Golden cross/supertrend burada "LONG'u onaylama" için değil; **trend, LONG'a karşıysa (ör. supertrend aşağı + ADX>25 + death-cross) mean-reversion girişini veto etmek** için kullanılır. İstenirse ikinci mod (`trend_follow`) sonraki fazda ayrı strateji olarak eklenir; Faz 6'da strateji DEĞİŞMEZ.

### 2.2 Her sinyal standart çıktı verir
```
SignalReading { family, name, direction ∈ {-1,0,+1}, strength ∈ [0,1],
                age_bars (olay kaç mum önce), p_fake (§3), ok(veri tam mı) }
```

### 2.3 Birleştirme (psödokod)
```
def confirm(side_dir, readings, regime, cfg):
    # 1) Veri yoksa fail-closed
    if not all_required_ok(readings): return Verdict(allow=False, reason="veri eksik")

    # 2) REJİM VETO (trend ailesi)
    trend = [r for r in readings if r.family == TREND]
    against = trend_direction_score(trend)        # side_dir'e KARŞI trend gücü [0,1]
    if against >= cfg.veto_trend_strength and adx >= cfg.adx_trend:
        return Verdict(False, "güçlü trende karşı mean-reversion (düşen bıçak)")

    # 3) TEYİT: en az K FARKLI aileden uyumlu sinyal (aynı aileden 3 sinyal = 1 oy)
    support = {}                                   # aile -> en iyi (1 - p_fake) * strength
    for r in readings where r.family in (EXHAUSTION, STRUCTURE, VOLUME):
        if r.direction == side_dir and r.ok:
            support[r.family] = max(support.get(r.family, 0), (1 - r.p_fake) * r.strength)
    if len(support) < cfg.min_families:            # öneri: 2
        return Verdict(False, f"teyit yetersiz ({len(support)}/{cfg.min_families} aile)")

    # 4) Birleşik güven: log-olabilirlik oranı (LLR) toplamı, aile başına TEK terim
    #    (aileler arası bağımsızlık varsayımı; aile içi çift sayım yok)
    llr = sum(log((1-p_fake_f)/p_fake_f) for f in support)   # p_fake_f: aile en iyi sinyalinin p_fake'i
    p_real = sigmoid(llr_to_logit(prior=cfg.prior_real) + llr)
    return Verdict(allow = p_real >= cfg.min_p_real, p_real=p_real, reason=...)
```
`p_real`, Faz 3'te `q` için bir girdi olabilir (kalibre olduktan sonra, ayrı onayla); Faz 6'da YALNIZ filtre.

### 2.4 Sinyal örnekleri (hepsi saf fonksiyon; yalnız KAPANMIŞ mum; look-ahead yok)
```
ema(closes, n);  supertrend(h,l,c, atr_n=10, mult=3.0) -> (dir, line)
adx(h,l,c,14);   rsi(c,14);  bollinger_z(c, 20)
swing_levels(h,l, left=5, right=5) -> [(price, touches)]    # destek/direnç
near_level(price, levels, tol=0.5*ATR, side)                 # LONG için destek, SHORT için direnç
climax(vol, lookback=20): vol[-2] > 3*median ve vol[-1] < vol[-2]   # tükenme
rsi_divergence(price_lows, rsi_lows)                         # fiyat LL, RSI HL
```

## 3. Gerçek / Fake Ayrımı — İstatistiksel Mekanizma

**Tanım.** Bir sinyal olayı t anında tetiklenir. *Gerçek* = yön, test penceresinde maliyeti aşan hareketle korundu. *Fake* = değil.
```
outcome(event, H, cost):
    # H = ufuk (mum), cost = fee_rt + slipaj (Faz 5 maliyet modeli)
    fwd_ret   = dir * (close[t+H] - close[t]) / close[t]
    mfe, mae  = dir*max/min excursion within H
    real      = (fwd_ret > cost) AND (mae > -SL_pct)      # SL'ye çarpmadan yönde kazandı
    persist_k = ilk k ki sonrasında yön tersine döndü (kalıcılık süresi)
```

### 3.1 Olay-çalışması (offline, kalibrasyon)
```
for symbol in universe (Faz 4 evreni, geçmiş klines, Binance public):
    for signal in registry:
        events  = signal.detect(klines)                 # yalnız kapanmış mum
        events  = decluster(events, min_gap=H)           # örtüşen olayları ele (bağımsızlık)
        record outcome(event, H) for H in {4, 8, 16, 32 mum}

fake_rate(signal)      = 1 - mean(real)
CI                     = Wilson aralığı (n küçükken güvenli)
baseline(signal)       = AYNI rejimde rastgele zaman noktalarının real oranı (n_perm=1000, permütasyon/block-bootstrap)
lift                   = real_rate - baseline_real_rate
p_value                = permütasyon testi (H0: sinyal baseline'dan iyi değil)
survival_curve(signal) = Kaplan–Meier: "yönü koruma süresi" (golden cross sonrası kaç mum yön korunur)
half_life              = medyan persist_k
```
**Kabul kuralları (muhafazakâr):** n ≥ 100 (declustered), Benjamini–Hochberg FDR ≤ %10 (çok sayıda sinyal×ufuk testi → yanlış keşif kontrolü), lift > 0 ve Wilson alt sınırı baseline'ın üstünde. **Walk-forward:** zaman sıralı bölme (purged/embargo), seçim eğitimde, rapor test diliminde; parametre taraması test verisine ASLA bakmadan. Geçemeyen sinyal `p_fake = prior` (nötr) ile işaretlenir, oy hakkı kısıtlanır.

### 3.2 Çevrimiçi güncelleme (canlı/compare)
```
posterior[signal, regime] = Beta(a0 + wins, b0 + losses)      # a0,b0 = offline lift'ten (informative prior)
p_fake = 1 - E[Beta]  ;  güven = Beta aralığı genişliği
# Dönemsel sürüklenme (drift) tespiti: son N sonuçta CUSUM; sapma → p_fake prior'a çekilir, uyarı logu
```
Sonuçlar `signal_outcomes` tablosuna (ayrı SQLite; Faz 5'teki `whale_metrics` deseni, `risk_ledger.db` şemasına DOKUNULMAZ) yazılır: sinyal adı, rejim, yön, giriş zamanı, H sonrası getiri, real/fake. `compare` modunda *hipotetik* sonuçlar (işlem açılmasa bile) sonradan klines'tan doldurulur → işlem sayısından bağımsız hızlı kalibrasyon.

### 3.3 Rejim ayrımı
Aynı sinyal rejime göre farklı çalışır (golden cross trendde güvenilir, yatayda sahte). Rejim: `ADX<20 → range`, `ADX≥25 → trend`, arası belirsiz (+ ATR yüzdelik). `p_fake` **sinyal × rejim** bazında tutulur.

## 4. Entegrasyon — `STRATEGY_CONFIRMATION = off | compare | on`

| Mod | Davranış |
|---|---|
| `off` (varsayılan) | Blok atlanır; ek API çağrısı YOK; canlı davranış DEĞİŞMEZ. |
| `compare` | Sinyaller, `p_fake`, `p_real`, veto/teyit kararı loglanır ve `signal_outcomes`'a yazılır; karar ESKİ mantıkla (yalnız `decide_side`) verilir. |
| `on` | **Yalnız FİLTRE:** veto, teyit yetersiz, `p_real < eşik` veya veri eksik → giriş REDDEDİLİR. Yön/boyut/SL değişmez; asla yeni giriş tetiklemez. |

**Akış (open_coin içinde, sırayla ucuzdan pahalıya):**
```
decide_side(coin)                       # değişmez
  -> [Faz 6] strategy_confirmation      # klines (cache'li), saf hesap, ek ağırlık düşük
  -> [Faz 5] whale filter               # depth/aggTrades (daha pahalı) — yalnız Faz 6'yı geçenler
  -> Faz 3 boyut/kaldıraç               # değişmez
```
Ucuz filtre önce → pahalı Faz 5 çağrıları azalır (rate-limit kazancı).

**Yeni modül:** `strategy_engine.py` (saf: göstergeler, dedektörler, `confirm()`, `outcome()`, `SignalStore`), `confirm_backtest.py` (offline olay-çalışması, KOD ÇALIŞTIRMA yalnız public klines; canlıya dokunmaz). `auto_trader.py`: bayrak + tek `try/except` bloğu (Faz 5 ile aynı desen; `on`'da hata = fail-closed/giriş yok, `compare`'de karar etkilenmez).

**Config (`RISK_CONFIG["confirm_*"]`, muhafazakâr, ⚠️ CALIBRATION-REQUIRED):**
`confirm_min_families=2`, `confirm_min_p_real=0.65`, `confirm_veto_trend_strength=0.6`, `confirm_adx_trend=25`, `confirm_horizons=(4,8,16,32)`, `confirm_min_events=100`, `confirm_fdr=0.10`, `confirm_prior_real=0.5`, `confirm_tf=("15m","1h","4h")`.

**Rate-limit:** klines (limit≤200, ağırlık 2) × 3 zaman dilimi × aday; **mum kapanış zamanına göre cache** (aynı kapanmış mum için tekrar çağrı yok). Faz 4 `MetricCache` yeniden kullanılır; yalnız Hot aday seti için.

## 5. Test planı (RED/GREEN)
- **RED:** eski kod, güçlü aşağı trendde (supertrend aşağı, ADX>25) LONG girişini veto ETMEZ; teyit yok → giriş devam.
- **GREEN:** her gösterge bilinen vektörlerle (Wilder RSI/ATR referans değerleri); dedektörler sentetik serilerle; **look-ahead testi** (t+1 verisi değişince t kararı değişmez); aynı aileden 3 sinyal = 1 oy; veto; fail-closed; `off` iken modül çağrılmaz; `on` yalnız reddeder; Wilson/permütasyon/BH fonksiyonları bilinen örneklerle; Beta güncelleme; sinyal_outcomes ayrı DB.

## 6. Artımlı uygulama sırası
1. `strategy_confirm.py` göstergeler + dedektörler + testler (entegrasyon yok).
2. `confirm_backtest.py` olay-çalışması → **rapor** (hangi sinyaller gerçekten lift veriyor; yönetici onayı).
3. `compare` entegrasyonu + `signal_outcomes`.
4. Yeterli `compare` verisi → eşik kalibrasyonu → onay → `on` (yalnız filtre).

## 7. Yönetici Kararı Gereken Açık Sorular
1. **Strateji tutarlılığı:** Mean-reversion çekirdek korunup trend göstergeleri **veto** olarak mı kullanılsın (öneri), yoksa golden-cross/supertrend ile **trend-takip** ikinci bir strateji mi istiyorsunuz? (İkincisi Faz 6 kapsamı dışı, ayrı faz.)
2. Teyit eşiği: `min_families=2` ve `min_p_real=0.65` yeterince muhafazakâr mı? (Daha sıkı → daha az ama daha kaliteli işlem.)
3. **Offline backtest** için public Binance klines geçmişi çekmeye (yalnız GET, emir yok) izin var mı? Çekim hacmi: evren × 3 TF × ~90 gün.
4. Zaman dilimleri 15m/1h/4h uygun mu (bot 5–15 dk döngüde)?
5. `signal_outcomes` ayrı SQLite dosyası (`~/signal_outcomes.db`) onayı (Faz 5'teki `whale_metrics` ile aynı yaklaşım).

## 8. Yönetici Kararları (2026-11-13)
1. Mean-reversion çekirdek KORUNUR; trend göstergeleri yalnız **VETO**. Trend-takip ayrı faz (7/8).
2. `min_families=2`; **`min_p_real=0.65`** (0.55 çok düşük bulundu). Canlı veriyle kalibre edilecek.
3. Offline backtest: yalnız public `/fapi/v1/klines` (anahtarsız, salt-okunur), rate-limit'e dikkat.
4. **15m ana zaman dilimi** (bot 15m ATR kullanıyor); **1h ve 4h yalnız trend filtresi (veto)**.
5. `~/signal_outcomes.db` ayrı SQLite; `risk_ledger.db` şeması DOKUNULMAZ.

**Mimari kararı:** modül adı `strategy_engine.py` (whale_engine genişletilmedi: sorumluluklar ayrı, her biri kendi bayrağı/DB'si ile bağımsız kapatılabilir). Cold-start notu: posterior verisi (n < `confirm_min_events`) yokken `p_fake=0.5` nötr → `p_real≈prior=0.5 < 0.65`, yani `on` modu kalibrasyon (offline/compare) olmadan çoğu girişi reddeder (muhafazakâr, istenen).
