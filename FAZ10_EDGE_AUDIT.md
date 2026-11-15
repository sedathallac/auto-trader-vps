# FAZ10 — Futures Bot Edge/ Risk Denetimi ve Düzeltme Kanıtı

Tarih: 2026-11-15 · Commit: `843a36a` · Kapsam: `/home/reisfurkan/faz9-live` (FAZ9 app)
Kural: yama yok — her bulgu kaynak kodda yapısal olarak düzeltildi ve testle kanıtlandı.

## 1. Bulgular ve düzeltmeler

| # | Bulgu (önce) | Kök neden | Düzeltme (sonra) | Kanıt testi |
|---|---|---|---|---|
| F1 | SL içeride tetiklenmişken ufuk sonunda kârda görünen işlem "real" sayılıyordu | `outcome_from_bars` yalnız kapanışa bakıyordu | `edge_engine.first_touch_outcome`: mum sırasına göre ilk temas; aynı mumda SL+TP → SL | `test_first_touch_stop_wins_when_stop_comes_first`, `test_first_touch_same_bar_is_conservatively_a_stop`, `test_outcome_from_bars_with_tp_is_path_dependent` |
| F2 | Boyutlandırma yalnız komisyonu sayıyor, slipajı saymıyordu | `effective_sl = sl + fee` | `risk_notional`: `sl + fee + slippage`; `net_reward_risk` aynı maliyet tabanı | `test_risk_notional_loss_at_stop_stays_inside_the_budget`, `test_net_reward_risk_requires_profit_after_costs` |
| F3 | `min_notional` yukarı çekilerek kayıp bütçesi aşılabiliyordu | `max(min_notional, …)` | `policy="refuse"`: bütçe minimumu karşılamıyorsa emir YOK | `test_risk_engine_refuses_instead_of_breaking_its_loss_budget`, `test_risk_notional_refuses_when_budget_cannot_fund_the_minimum` |
| F4 | Net R/R kapısı yoktu; TP/SL geometrisi maliyet sonrası test edilmiyordu | `approve_trade` TP almıyordu | `approve_trade(..., take_profit_pct=)` + `open_coin` içinde `min_net_reward_risk` | `test_risk_engine_refuses_low_net_reward_risk`, `test_risk_engine_approves_healthy_net_reward_risk` |
| F5 | BH-FDR, DSR, OOS, persistence fonksiyonları yazılmış ama karar yolunda kullanılmıyordu | ölü kod | `promotion_gate` + `_live_edge_gate` (canlıda fail-closed, veri yoksa shadow) | `test_promotion_gate_applies_fdr_across_cells`, `test_edge_gate_refuses_an_unproven_cell`, `test_edge_gate_allows_a_proven_cell` |
| F6 | Çoklu deneme (aile×rejim taraması) cezalandırılmıyordu | DSR yok | `deflated_sharpe_ratio` + `n_trials` | `test_dsr_penalises_more_trials`, `test_cell_decision_refuses_when_many_trials_deflate_the_sharpe` |
| F7 | Walk-forward sızıntılıydı (purge/embargo yok) | eğitim/test komşuluğu | `purged_walk_forward` | `test_purged_walk_forward_never_trains_on_the_future`, `test_purged_walk_forward_embargo_removes_adjacent_samples` |
| F8 | Kelly sabit 0.25 çarpanıydı; kenardan türetilmiyordu | formül eksikti | `fractional_kelly(p, b)` + cap | `test_fractional_kelly_matches_the_formula`, `test_fractional_kelly_returns_zero_without_edge` |
| F9 | Miktar `int(raw/step)` + `round(...,8)`; min notional altı sessizce yukarı çekiliyordu | float/floor hatası | `round_step` (Decimal) + `amount_for_notional` (ok=False → emir yok) | `test_round_step_has_no_float_artifacts`, `test_amount_for_notional_refuses_below_exchange_minimum` |
| F10 | Günlük zarar yalnız gerçekleşmiş PnL'e bakıyordu | realized-only muhasebe | `update_equity`: gün başı equity'den `max_daily_loss_pct` halt | `test_equity_drawdown_halts_even_before_realised_loss`, `test_equity_within_the_daily_limit_does_not_halt` |
| F11 | Kâr realizasyonu yoktu (break-even/trailing) | çıkış kuralı yok | `trailing_stop`: monoton, fiyatı geçmez, R≥1 → BE | `test_trailing_stop_moves_to_breakeven_after_one_r`, `test_trailing_stop_never_loosens_a_long` |
| F12 | Slipaj sabit varsayım | defter derinliği yok | `slippage_from_depth` (VWAP yürüyüşü, ince defter → 1.0) | `test_slippage_from_depth_walks_the_book`, `test_slippage_from_depth_fails_closed_on_thin_book` |
| F13 | Perp funding maliyeti EV'de yoktu | eksik maliyet kalemi | `funding_cost_pct` | `test_funding_long_pays_and_short_receives`, `test_funding_scaling` |
| F14 | `real` tautolojisi posterior'da hâlâ kullanılıyordu | tanım gereği gerçek | `SignalStore.resolved_rows` + `net_expectancy_report` (fwd−cost, CI, DSR) | `test_signal_store_resolved_rows_returns_only_scored_rows`, `test_net_expectancy_report_is_cost_aware` |

## 2. Test kanıtı (komut + sonuç)

```
python3 -m pytest tests/ -q --ignore=tests/test_c3.py --ignore=tests/test_fixes.py --ignore=tests/test_c6.py
445 passed in 11.95s

python3 -m pytest test_phase7_unified_sizing.py test_phase6_strategy_engine.py \
                 test_phase9_analyze_live_data.py test_phase3_leverage_engine.py -q
85 passed in 6.99s

python3 -m pytest tests/test_edge_engine.py -q      → 64 passed
python3 -m pytest tests/test_faz10_wiring.py -q     → 22 passed
```

Toplam: **530 test, 0 hata**. Yeni: 86 test (64 çekirdek + 22 bağlama).

## 3. Değişen dosyalar

```
 edge_engine.py                  | 555 ++++++++++++++++++++++++++++++++++++++
 auto_trader.py                  |  99 +++++++-
 risk_engine.py                  |  60 ++++-
 strategy_engine.py              |  42 +++-
 analyze_live_data.py            |  19 ++
 tests/test_edge_engine.py       | 371 ++++++++++++++++++++++++++++
 tests/test_faz10_wiring.py      | 248 +++++++++++++++++++
 test_phase7_unified_sizing.py   |   8 +-
```

## 4. Dürüst kalan boşluklar

- `trailing_stop` saf kural olarak kanıtlı; borsadaki algo SL'i güncelleyen döngüye bağlanması testnet koşusu gerektirir.
- Funding maliyeti hesabı hazır; canlı PnL muhasebesine eklenmesi bir sonraki adım.
- Korelasyon tavanı hâlâ statik sektör haritası kullanıyor (getiri-bazlı beta matrisi yok).
- Canlı testnet koşusu yapılmadı; bu commit yalnız kod + test kanıtıdır.
