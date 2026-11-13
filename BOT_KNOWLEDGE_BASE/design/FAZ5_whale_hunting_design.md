# Faz 5 — Balina Avcılığı ve Fırsat Keşfi: Tasarım Planı (KOD YOK)

Durum: **ONAYLANDI (2026-11-13)** · Tarih: 2026-11-13
Not: Master Plan Bölüm 7 metni repoda bulunamadı; bu plan istenen 3 soruya ve mevcut kod
(`auto_trader.py`, `scan_engine.py`, `leverage_engine.py`) üzerinden yazıldı. Bölüm 7'deki ek
gereksinim varsa onay öncesi eklenmeli.

## 1. Mevcut order_book ve _volume_confirmation yeterli mi? — HAYIR

**Mevcut durum**
- `order_book(limit=20)` → `check_liquidity`: spread% + ilk 10 kademe toplam notional (bid+ask TOPLAMI).
- `_depth_liquidity`: mid ± 10 bps bandında toplam notional.
- `_volume_confirmation`: son 15m mum hacmi / önceki 20 mum ortalaması (tek skaler spike oranı).

**Boşluklar**
| İhtiyaç | Mevcut | Eksik |
|---|---|---|
| Bid/Ask dengesizliği | bid+ask toplanıyor, YÖN yok | taraf ayrımı |
| Likidite boşluğu | yok | kademeler arası fiyat atlaması |
| Büyük emir (duvar) | yok | tek seviyede anormal hacim |
| Agresör yönü | klines hacmi yönsüz | alıcı/satıcı ayrımı |
| Zaman boyutu | tek anlık görüntü | duvarın kalıcılığı (spoofing) |
| Pozisyon akışı | yok | OI, funding, tasfiye |

**Tespit yöntemleri (yeni saf modül `whale_engine.py`, borsaya yazmaz)**
1. **Order Book Imbalance (OBI)** — `limit=100`, mid ± B bps (B=10, 25):
   `OBI = (V_bid − V_ask) / (V_bid + V_ask)`, [−1,+1]. Tek anlık değer değil, son N örneğin
   (ör. 6×10s) medyanı kullanılır (titreşim/spoofing filtresi).
2. **Likidite boşluğu (gap)** — kademe i ile i+1 arası `|p_{i+1}−p_i|/mid`, kademe başına
   medyan aralığa göre z-skoru > 3 ise boşluk. Çıktı: hedef yönde ilk boşluğa mesafe
   (bps) → slipaj/stop-run riski ve TP hedefi.
3. **Duvar (wall)** — kademe notional'ı, ilk 50 kademe medyanının ≥ k× (k=8) ve ≥ mutlak
   eşik (ör. 250k USDT) ise duvar. Kalıcılık: ≥ M ardışık örnekte (≥ 30s) aynı fiyat bandında
   durmalı; kaybolan duvar = spoof işareti (skor düşürücü).
4. **Agresör akışı (CVD)** — `/fapi/v1/aggTrades` (son 1–5 dk): `m=false` alıcı agresör.
   `CVD_ratio = (taker_buy − taker_sell)/(toplam)`. Büyük işlem eşiği: notional ≥ P99
   (son 1 saat) → "balina işlemi" sayısı ve net yönü.
5. **Hacim teyidi v2** — mevcut spike oranı korunur; ek: (a) fiyat değişimi ile hacim
   uyumu (hacim↑ + fiyat yatay = emilim/absorption), (b) z-skor (ortalama yerine
   medyan/MAD, aykırı mumlara dayanıklı).
6. **Pozisyon akışı** — `/fapi/v1/openInterest` + `premiumIndex` (funding): OI↑+fiyat↑ =
   yeni long; OI↓+fiyat↑ = short squeeze (kalıcı değil). Aşırı funding = kalabalık pozisyon
   (ters yön riski). `forceOrders` salt-okunur tasfiye yoğunluğu (opsiyonel).

**Sinyal çıktısı:** `WhaleSignal{direction ∈ {−1,0,+1}, strength ∈ [0,1], components{obi, gap_bps,
wall, cvd, vol_conf, oi_delta}, quality_ok}`. Eksik veri → `direction=0, quality_ok=False`
(fail-closed: sinyal yoksa fırsat yok).

**Rate-limit:** `depth(limit=100)` ağırlık 5, `aggTrades` 20. Yalnız Faz 4 **Hot katmanı**
adayları (≤ top 5–10, tier cache TTL 10–30s) için çağrılır; evren taraması yapılmaz.
Faz 4 `MetricCache` yeniden kullanılır.

## 2. E[kâr] > maliyet + risk primi — değişkenler

**Karar kuralı:** `E_net = E[kâr] − (C_işlem + RP) > 0` ve `E_net / S ≥ eşik` (min net kenar,
ör. 0.15%·S), aksi halde `WhaleSignal` yok sayılır. Tüm değerler **notional S'e oranla (kesir)**.

### 2.1 Beklenen getiri E[kâr]
`E[kâr] = p_win · G − (1 − p_win) · L` (S kesiri)
- **G (hedef kazanç):** TP mesafesi = `min(ATR-bazlı TP, ilk duvar/boşluğa mesafe × 0.8)`.
  Hedef yönde duvar varsa TP duvarın önünde (duvara sürmeyiz).
- **L (kayıp):** SL mesafesi (mevcut ATR-bazlı SL) + stop-run payı (karşı yönde yakın duvar yoksa ek yok).
- **p_win:** iki aşama.
  - *Cold-start (veri yok):* `p_win = 0.5 + k · (strength − 0.5)·edge_cap`, `edge_cap` küçük
    (en çok +0.08) → sinyal gücü büyük kenar vaat edemez.
  - *Kalibre (n ≥ min_sample, `reliable=True`):* `trade_ledger`'a yeni kolon `whale_strength_bucket`
    eklenir; p_win bucket bazlı rolling win-rate (Faz 3'teki `signal_stats_from_ledger` mantığı),
    bucket n < min_sample ise cold-start formülüne düşer. Faz 3 ile aynı reliable bayrağı.

### 2.2 İşlem maliyeti C_işlem (komisyon + slipaj)
`C_işlem = fee_rt + slip_in + slip_out + funding_hold`
- **fee_rt:** `RISK_CONFIG.fee_pct_round_trip` (0.0008 = 4+4 bps taker). Maker/taker gerçek ücret `commissionRate` endpoint'inden okunup config'e düşer (salt-okunur).
- **slip_in/out:** order book üzerinden **gerçek VWAP slipajı**: S notional'ını yürüyerek
  `slip = |VWAP(S) − mid| / mid` (bugünkü `get_real_slippage` yalnız spread/2 + sabit baz
  kullanıyor — Faz 5'te depth-walk ile değiştirilir/ek metrik olur). Çıkış için stop-market
  tarafında 1.5× çarpan (stop kaymasının tipik kötü durumu).
- **funding_hold:** `funding_rate × (beklenen_tutma_süresi/8h)`, yön işaretli (long öder/short alır).

### 2.3 Risk primi RP
`RP = λ_σ·σ_ATR_kesir + λ_dd·dd_cezası + λ_u·belirsizlik`
- **λ_σ·σ:** volatilite primi (Faz 3'teki σ ile aynı kaynak: ATR%). Başlangıç λ_σ = 0.1.
- **dd_cezası:** `risk_engine.current_drawdown` (d); `RP *= (1 + d)` — drawdown'da eşik yükselir (Faz 3'teki 1+d paydasıyla tutarlı).
- **Belirsizlik:** `reliable=False` iken sabit ek prim (ör. +10 bps); spoof/duvar-kalıcılık şüphesinde +; veri eksikse sinyal zaten fail-closed.
- Tümü ⚠️ CALIBRATION-REQUIRED; `compare` loglarından ayarlanır.

**Örnek (BTC, kavramsal):** G=1.5%, L=1.0%, p_win=0.54 → E=0.54·1.5−0.46·1.0=0.35%;
C=0.08%+0.04%+0.06%+0.01%=0.19%; RP=0.10%·... → E_net≈0.06% < 0.15% eşik → **işlem yok**.
Yani kural çoğu zayıf sinyali eler (istenen davranış).

## 3. WHALE_HUNTING = off | compare | on entegrasyonu

Faz 3/4 ile **birebir aynı desen** (varsayılan `off`, env bayrağı, saf modül, hata→eski davranış).

| Mod | Davranış |
|---|---|
| `off` (varsayılan) | Blok tamamen atlanır; canlı davranış DEĞİŞMEZ. Hiçbir ek API çağrısı yok. |
| `compare` | Faz 4 Hot adayları için WhaleSignal + E_net hesaplanır, **loglanır** (`🐋 Faz5[compare]: …`), ek olarak gerçekleşen sonuç ledger'a `whale_*` alanlarıyla yazılır (kalibrasyon verisi). Karar ESKİ mantıkla verilir. |
| `on` | `decide_side` yönü ile WhaleSignal yönü çelişirse veya `E_net ≤ eşik` ise giriş **reddedilir** (yalnızca FİLTRE); yön/boyut/SL kararını bu faz DEĞİŞTİRMEZ. Boyut Faz 3, aday sıralaması Faz 4 sorumluluğunda kalır. |

**Entegrasyon noktası:** `open_coin` içinde `decide_side` sonrası, Faz 3 bloğundan ÖNCE tek bir
`try/except` bloğu (hata → log + eski akış, `fail-open` DEĞİL: `on` modunda veri hatası = giriş yok,
çünkü filtre yalnızca daha az işlem açar → güvenli yön).

**Güvenlik/kapsam**
- Modül **emir göndermez**, SL/close yolu (K1, CloseCoordinator) **dokunulmaz**.
- `HB_OFFLINE=1` ile `compare` modu çalışabilir (yalnız Binance public GET; emir yok).
- Rate-limit: yalnız Hot katmanı adayları; cache TTL; `429` → o tick'te Faz 5 atlanır.
- `on`'a geçiş şartı: ≥ 200 `compare` örneği, `E_net>0` olan sinyallerin gerçekleşen win-rate'i
  ile tahmin edilen p_win farkı kabul bandında (ör. ±5 puan), yönetici onayı.

## 4. Test planı (RED/GREEN, Faz 3/4 stiliyle)
- OBI/gap/wall/CVD saf fonksiyonları: sentetik order book ile deterministik testler.
- Duvar kalıcılığı: kaybolan duvar spoof sayılır.
- E_net: maliyet↑ → E_net↓; drawdown↑ → eşik↑; veri eksik → sinyal yok (fail-closed).
- Bayrak: varsayılan `off`; `off` iken `whale_engine` import/çağrı yok; `on` yalnız reddedebilir (asla giriş eklemez).
- `HB_OFFLINE` + `compare` → emir yok.

## 5. Artımlı uygulama sırası
1. `whale_engine.py` saf fonksiyonlar + testler (bayrak yok, entegrasyon yok).
2. `compare` entegrasyonu + ledger `whale_*` kolonları (migration, schema_validator uyumu).
3. HB_OFFLINE ile kalibrasyon logu toplama.
4. Kalibrasyon → yönetici onayı → `on` (yalnız filtre).

## 6. Açık sorular (onay için)
1. `on` modu yalnız FİLTRE mi (önerilen), yoksa balina sinyali yön de belirleyebilsin mi?
2. Minimum net kenar eşiği (öneri: 0.15% S) ve duvar eşiği (öneri: 250k USDT, 8× medyan).
3. Tasfiye (`forceOrders`) ve OI verisi ilk sürümde zorunlu mu, yoksa Faz 5.1'e mi bırakılsın?
4. `ledger` şema değişikliği (`whale_*` kolonları) migration onayı gerekiyor (K1/schema_validator ile uyum).

## 7. Yönetici Kararları (2026-11-13)
1. `on` = **yalnız FİLTRE** (sahte/kalitesiz sinyal eler); balina sinyali işlem TETİKLEMEZ. Risk yönetimi önceliklidir.
2. Eşikler `RISK_CONFIG` içinde, muhafazakâr başlangıç değerleriyle; canlı veriyle kalibre edilecek.
3. OI/tasfiye: rate-limit yemiyorsa dahil. **Uygulama notu:** OI `/fapi/v1/openInterest` (ağırlık 1) ile dahil. Tasfiye için Binance'te herkese açık REST yok (`allForceOrders` kaldırıldı, `forceOrders` yalnız kendi hesabın) → tasfiye haritası REST ile alınamaz; ilk sürümde order book + taker akışı + OI/funding ile devam.
4. Ledger: mevcut şema (trade_ledger, risk_state) DOKUNULMAZ; yalnız yeni `whale_metrics` tablosu, ayrı SQLite dosyasında.
