# Release Blocker Remediation — C-1 / B-1 / F-1 / E-1

Tarih: 2026-11-12 · Branch: `feature/futures-release-blockers` (from `bb10108`)
Bağımsız release denetiminde (`REVIEWS/REVIEW_inc13_bb10108_2026-11-12.md`) kanıtlanan
açık bulguların düzeltmeleri. Her biri ayrı mantıksal commit; TDD RED/GREEN.

## C-1 — HB order_ids None slotları (commit `8d00a91`)
Kök neden: K1=B executor close/TP barrier'larını kapatır → `get_custom_info().order_ids`
= `[open_id, None, None]`. `_hb_order_ids` `ids |= set(oids)` ile None'u da alıyordu;
None → stub `{symbol, clientOrderId: None}` → `order_terminal`/`cancel_order` ikisi de
`oid=None,coid=None` guard'ında False → **UNRESOLVED_ORDER** → `request_close` kapanışı
reddediyordu. inc13 canlı wiring'i bunu her acil kapanışta aktive ederdi.
Düzeltme: None/boş slotları `order_ids`'ten ele (devre dışı barrier = emir yok); her
GERÇEK id hâlâ çözülür (bilinmeyen/canlı gerçek emir — ENTRY/CLOSE/TP NEW, ambiguous id,
eksik executor — fail-closed bloklar). None asla gerçek emir gibi sorgulanmaz.
RED/GREEN: `tests/test_c1_order_ids.py` 8 test (gerçek CloseCoordinator+adapter).

## B-1 — Coordinator bypass (commit `0b43e55`)
Kök neden: `coordinate_close(None)` doğrudan reduceOnly MARKET POST gönderiyordu;
`run_once` coordinator'ı try/except'te kurup başarısızlıkta yine aday döngüsüne giriyordu
→ kurulum hatasında sessiz bypass (idempotency/intent/handoff yok) + yeni pozisyon.
Düzeltme: `coordinate_close(None)` HİÇBİR emir göndermez, fail-closed `CloseResult`
(NO_COORDINATOR, critical) + CRITICAL alarm döner; `run_once` coordinator None iken YENİ
girişleri bloklar (protect_then_gate sonrası döner → ensure_sl_coverage açık pozisyonlar
için çalıştı). RED/GREEN: `tests/test_b1_bypass.py` 2 test (coordinate_close 0 POST;
run_once open_coin çağırmaz). inc13 eski fallback testi fail-closed'a güncellendi.

## F-1 — Inline SL doğrulama (commit `7505698`)
Kök neden: open_coin inline doğrulaması `open_algo_orders_for`+qty≥%95; side/positionSide/
reduceOnly/closePosition/status doğrulamıyordu → gerçekte korumayan bir SL (ör. yanlış
side) "doğrulanmış" sayılıp pozisyon korumasız kalıyordu.
Düzeltme: inc10.1 mode-aware tri-state `sl_status` ile doğrula. PRESENT = pozisyonun
TAMAMINI geçerli biçimde koruyan canlı STOP_MARKET; ABSENT/UNKNOWN → acil kapanış
(coordinator). Mod `_verify_oneway`; yalnızca ONEWAY yerleştirme, HEDGE upstream gate'li.
RED/GREEN: `tests/test_f1_sl_verify.py` 3 test (gerçek open_coin: yanlış-side/UNKNOWN →
acil kapanış; geçerli → PRESENT).

## E-1 — TP/time-limit (commit `c83461b`)
Bulgu: `request_take_profit/request_time_limit` üretimde çağrılmıyor. Gerçek config:
`build_executor_config` tüm barrier'ları None (K1=B); tp/time-limit `create_executor`'a
geçip yok sayılıyor. Çözüm: **yeni strateji icat edilmedi** — canlı çıkış politikasının
yalnızca borsa SL (+ koordineli acil kapanış) olduğu belgelendi; docstring'ler netleşti;
invariant testi eklendi (barrier'lar None; TP/time-limit arayüzleri tek coordinator'dan
geçer; üretimde tetikleyici yok). `tests/test_e1_exit_policy.py` 3 test.

## Doğrulama (bağımsız koşum, bu uygulayıcı oturumunda)
Tam `tests/`: **271 passed** (255 → +16: C-1 8, B-1 2, F-1 3, E-1 3). Ruff `--select
F,E9` temiz; py_compile temiz. MAX_OPEN_POSITIONS=2 değişmedi; HB otonom kapanışlar kapalı;
gerçek emir/push/deploy/restart yok; önceki commit'ler yeniden yazılmadı.

## Bağımsız denetim — BEKLİYOR (PARTIAL)
reisfurkan-53 çevrimdışı; uygulayıcının kendi raporu bağımsız sayılmaz. ZORIN-FURKAN'ın
FARKLI, salt-okunur bir oturumda gerçek diff'i ve testleri bağımsız incelemesi gerekir.
Bu düzeltmelerin release öncesi bağımsız doğrulaması **NO EVIDENCE** (yapılmadı).
