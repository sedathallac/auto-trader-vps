# FAZ11 — Beş Katmanlı Genişletme: Bulgu, Düzeltme ve Test Kanıtı

Tarih: 2026-11-15 · Repo: `/home/reisfurkan/faz9-live`
Kural: yama yok — her katman kaynak kodda yapısal olarak uygulandı ve testle kanıtlandı.
Ağırlıklar kullanıcı tarafından onaylandı: `0.15/0.15/0.15/0.15/0.10/0.10/0.10/0.10`.

## Katman 1 — Coin kalite motoru (`coin_quality.py`, 324 satır)

Sekiz metrik, her biri kendi bandına göre [0,1] normalize; set-max normalizasyonu yok.

| Metrik | Puanlama | Ağırlık |
|---|---|---|
| spread_bps | ≤1 bps → 1.0, ≥20 bps → 0.0 | 0.15 |
| depth_usdt_1pct | doyum eğrisi (50k → 0.5) | 0.15 |
| quote_volume_24h | doyum eğrisi (50M → 0.5) | 0.15 |
| atr_pct | bant %0.3–2.0 → 1.0 | 0.15 |
| funding_rate | ≤%0.01/8h → 1.0, ≥%0.05 → 0.0 | 0.10 |
| open_interest_usdt | doyum eğrisi (20M → 0.5) | 0.10 |
| taker_buy_sell_ratio | [0.80,1.25] → 1.0 | 0.10 |
| price_change_24h_pct | ≤%10 → 1.0, ≥%20 → 0.0 | 0.10 |

Sert veto: spread >25 bps · depth <5k USDT · OI <500k USDT · |24s| >%25 · ATR >%6.
Fail-closed: spread veya depth eksikse coin reddedilir. Kanıt: 58 test.

## Katman 2 — Kaldıraç politikası (`leverage_policy.py`, 190 satır)

`compute_leverage` beş tavanın en küçüğünü alır ve **aşağı** yuvarlar:

```
L = floor( min( c/σ/(1+d),  kelly_cap(f*),  1/(buffer·SL+maint),  stage_cap,  exchange_max ) )
```

Geçersiz girdide (σ≤0, SL≤0) en düşük kaldıraç döner. `open_coin` içinde
`set_leverage` çağrısından hemen önce `leverage_ok` kapısı vardır: tasfiye tavanını
aşan kaldıraçla emir açılmaz. Kanıt: 23 test.

## Katman 3 — Boyutlandırma ve geometri (`sizing_policy.py`, 175 satır)

`size_position`: kayıp bütçesi / (SL + fee + slip + funding) → Kelly çarpanı →
kalite çarpanı (floor'da 0, 1.0'da 1.0) → max_notional tavanı → min_notional altı
**reddedilir** (upsize yok).

`suggest_sl_tp`: SL = 1.5·ATR, TP = 2.5·ATR; bantlara kırpılır (`clipped` ile
raporlanır) ve net R/R ≥ 1.20 kapısından geçirilir. `ok` alanı kapı sonucudur.

`calculate_position_size_v2` artık bu politikaya delege eder (eski
`max(notional, min_notional)` bütçe taşması kaldırıldı). Kanıt: 18 test.

## Katman 4 — Uçuş öncesi kontrol (`preflight.py`, 210 satır)

12 adım, sabit sıra, fail-fast, istisna yakalama:

```
config → offline_flag → account → exchange_info → filters → quality
      → leverage → sizing → geometry → risk → exposure → edge
```

Sağlayıcı `None` ise adım **atlanmaz**, `not_provided` ile reddeder.
Kanıt: 24 test.

## Katman 5 — Benchmark ve doğruluk (`benchmark.py`, 250 satır)

`benchmark_equity`: toplam getiri, CAGR, maks. drawdown, Sharpe, Sortino, Calmar,
kazanma oranı. `compare_to_baseline`: nakit ve buy&hold tabanına karşı fark.
`accuracy_report`: isabet, Brier, log-loss, ECE. `skill_score`: Brier skill.
Kanıt: 30 test.

## Test kanıtı

```
pytest tests/ -q (c3/fixes/c6 hariç)                        → 598 passed
pytest test_phase7 + test_phase6 + test_phase9 + test_phase3 → 85 passed
pytest tests/test_coin_quality.py                           → 58 passed
pytest tests/test_leverage_policy.py                        → 23 passed
pytest tests/test_sizing_policy.py                          → 18 passed
pytest tests/test_preflight.py                              → 24 passed
pytest tests/test_benchmark.py                              → 30 passed
```

Toplam **683 test, 0 hata**. FAZ11 yeni test: 153 (L1 58 + L2 23 + L3 18 + L4 24 + L5 30).

## Dürüst kalan boşluklar

- `preflight.pre_flight_check` saf ve testli; `open_coin` içine tam zincir olarak
  bağlanması bir sonraki tur (şu an kalite/kaldıraç/R-R kapıları ayrı ayrı bağlı).
- `benchmark` modülü canlı PnL akışına bağlı değil; raporlama katmanı bekliyor.
- Bantlar ve ağırlıklar `CALIBRATION-REQUIRED`; compare loglarıyla ince ayar gerekir.
- VPS'e yazılmadı; production hâlâ `598b4c0`, PID 716356.
