# Faz 4 — Hiyerarşik Çok-Katmanlı Tarama Mimarisi (Master Plan Bölüm 6)

Durum: **ONAYLANDI** (yönetici kararları §5'te) · Uygulama: `scan_engine.py` + `auto_trader.py` (flag arkasında)

## 1. Mevcut Durum (`scan_top`, auto_trader.py)
- `tickers()` → `GET /fapi/v1/ticker/24hr` (symbol'süz) → **TÜM ~400 USDT-M coin tek istekte** (ağırlık ~40). Per-symbol döngü YOK → mevcut tarama rate-limit sorunu yaratmıyor.
- Filtre: endswith USDT, blacklist dışı, `quoteVolume ≥ 200M`, `3% ≤ |chg| ≤ 15%`.
- Skor: `_score = 0.6·(vol/max) + 0.4·(|chg|/max)` — yalnız 2 faktör (hacim + momentum).
- Döngü: `run_once` her `--interval` (varsayılan 900s). ATR yalnız final adaylar için `open_coin`'de; likidite hiç kullanılmıyor.
- **Eksik:** likidite, volatilite, hacim-teyidi yok; tek katman/kadans.
- **Rate-limit riski** ancak Q-skoru için tüm evrene per-symbol metrik eklersek doğar → tiering + cache bunu çözer.

## 2. Hiyerarşik Katmanlar
| Katman | Kadans | Evren | Likidite | Volatilite/Hacim |
|---|---|---|---|---|
| **Hot** | 5 dk | İlk 100 | **gerçek depth** (`/fapi/v1/depth`) | ATR% + klines hacim-teyidi (cache'li) |
| **Warm** | 15 dk | İlk 250 | `quoteVolume` proxy | ATR% (cache'li) |
| **Cold** | 1 saat | İlk 500 | `quoteVolume` proxy | ucuz (ticker) |

- **Scheduler:** ana döngü **taban tick = 300s**; her tick `due_tiers(now, last_run)` ile vadesi gelen katmanları çalıştırır.
- **Cache (`MetricCache`, TTL'li):** ticker snapshot (60s, paylaşılır), ATR (`atr:<sym>`, 300s), depth (`liq:<sym>`, 300s), hacim-teyidi (`vc:<sym>`, 300s). Per-symbol çağrılar katman evreniyle + cache ile sınırlı → rate-limit güvenli.

## 3. Kalite Skoru (Q)
`Q = α·Likidite + β·Momentum + γ·Volatilite + δ·Hacim-Teyidi`
- Likidite/Momentum/Hacim-Teyidi: set-max'a göre [0,1] normalize.
- **Volatilite: orta-bandı ödüllendiren eğri** (mean-reversion): ATR% **[1.5%, 4%] → 1.0**, altında lineer yüksel, üstünde lineer azal (~8%'de 0). (yönetici kararı)
- **Katsayılar (⚠️ CALIBRATION-REQUIRED):** `α=0.35, β=0.30, γ=0.15, δ=0.20` (toplam 1.0).

## 4. Yönetici Kararları (onaylı)
1. **Volatilite şekli:** orta-bant eğrisi (ATR %1.5–%4 tam puan, uçlarda düşer).
2. **Likidite:** Hot=gerçek depth; Warm/Cold=quoteVolume proxy.
3. **Taban tick:** `--interval` 900→**300s**; her tick due-check.
4. **Katsayılar:** α=0.35, β=0.30, γ=0.15, δ=0.20.

## 5. Güvenlik / Kapsam İzolasyonu
- **Flag:** `HIERARCHICAL_SCAN = off | compare | on` (env), **varsayılan `off`** → mevcut `scan_top` aynen; production değişmez.
- **compare:** yeni `scan_tiered` Q-sıralaması loglanır, eski `scan_top` kullanılır (kalibrasyon için eski↔yeni gözlemi).
- **on:** tiered tarama kullanılır (kalibrasyon sonrası).
- Strateji sinyali (`decide_side`), leverage, sizing **değişmez** — yalnız aday seçimi/sıralaması.

## 6. Uygulama Haritası
- `scan_engine.py` (saf, test edilebilir): `MetricCache`, `Tier`/`TIERS`, `volatility_score`, `Q_WEIGHTS`, `due_tiers`, `compute_quality_scores`.
- `auto_trader.py` (ince adaptör): `HIERARCHICAL_SCAN` flag, `_SCAN_CACHE`/`_SCAN_LAST_RUN`, `run_tiered_scan` (binance + cache ile ham metrik toplar → `compute_quality_scores`), `run_once` içinde compare/on entegrasyonu.
- Testler: cache hit/miss/TTL, tier due/not-due, Q monotonluğu + normalize sınırları, volatilite orta-bant, `off`'ta davranış değişmezliği.

## 6. Q-Skoru Onayı ve Production Durumu (2026-11-13)
- Q-skoru sıralaması yönetici tarafından ONAYLANDI.
- Production'da `HIERARCHICAL_SCAN=off` KALIR (eski `scan_top` devam eder); kod varsayılanı `off`, testle kilitli (`test_default_flag_off`).
