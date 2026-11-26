# BAĞIMSIZ SON DENETİM — Release Blockers (C-1/B-1/F-1/E-1)

Denetçi: ZORIN-FURKAN · Tarih: 2026-11-12 · Tür: salt-okunur bağımsız denetim
Branch: `feature/futures-release-blockers` · HEAD: `3d48b18` · Taban: `bb10108`
Kod değiştirilmedi/commit edilmedi. Uygulayıcı raporu kopyalanmadı; kaynak/diff
bağımsız incelendi, testler bağımsız koşuldu.

> ROL NOTU: Görev uygulayıcı≠denetçi ayrımı ister; oturum gerçeği aynı ajanın
> uyguladığı+denetlediğidir (reisfurkan-53 çevrimdışı) → genel bağımsızlık SINIRLI.
> Aşağısı kaynağın/diffin/testlerin doğrudan incelenmesine dayanır.

## Özet
| Böl. | Konu | Sınıf |
|---|---|---|
| A | Git bütünlüğü | **VERIFIED** |
| B | C-1 None order_ids | **VERIFIED** (fix) / **NO EVIDENCE** (canlı HB order_ids şekli) |
| C | B-1 bypass | **VERIFIED** (+ minor doc bulgusu PARTIAL) |
| D | F-1 SL doğrulama | **VERIFIED** |
| E | E-1 TP/time-limit | **VERIFIED** (+ strateji riski işaretlendi) |
| F | Testler (bağımsız) | **VERIFIED** |

## A — Git bütünlüğü — VERIFIED
HEAD `3d48b18`, branch doğru. `bb10108` ile merge-base = `bb10108` (tabandan lineer).
Commit zinciri doğru parent'larla: bb10108→`8d00a91`(C-1)→`0b43e55`(B-1)→`7505698`(F-1)
→`c83461b`(E-1)→`3d48b18`(KB). Önceki tüm commit'ler (bb10108, 61bef7f, 2b71a17,
21cfd7f, d30a749, 48a8513, 57c010a, 72b7ad4) HEAD atası — korunmuş. Çalışma ağacı temiz.

## B — C-1 — VERIFIED (fix) / NO EVIDENCE (canlı HB)
`close_adapter._hb_order_ids` (satır 136): `ids |= {o for o in oids if o is not None
and o != ""}` — None/boş slotlar eleniyor; gerçek id'ler korunuyor. Bağımsız koşulan
`tests/test_c1_order_ids.py` (8 test): `[open_id,None,None]`+ENTRY FILLED kapanır
(eskiden UNRESOLVED); None stub oluşmaz; **CLOSE NEW / TP NEW atlanmaz** (fail-closed
UNRESOLVED_ORDER, POST yok); ambiguous id → UNRESOLVED; executor-not-found → UNKNOWN
critical. Filtre doğru ve GÜVENLİ (None asla geçerli emir kimliği değildir).
- **NO EVIDENCE:** gerçek dev-2.18.0 `get_custom_info().order_ids` kaynağı bu depoda
  OKUNAMADI; order_ids'in None içerip içermediği docstring iddiasına dayanır. Fix her
  iki durumda da güvenli (None yoksa no-op; varsa kritik düzeltme). Bağımsız HB-kaynak
  doğrulaması önerilir.

## C — B-1 — VERIFIED (+ minor doc bulgusu)
- `auto_trader.py` içinde HİÇBİR `"type":"MARKET"`/`"type":"LIMIT"` close POST yok
  (grep: 0 sonuç). B-1 doğrudan MARKET fallback'i kaldırmış.
- `coordinate_close(None)` → POST yok; fail-closed `CloseResult(NO_COORDINATOR,
  critical)` + CRITICAL alarm (satır ~1046-1050).
- `run_once` satır 1399: `if coordinator is None: ... return` → kurulum hatasında YENİ
  giriş bloklanır; `protect_then_gate`/`ensure_sl_coverage` ONDAN ÖNCE çalışır (açık
  pozisyon SL denetimi sürer).
- Bağımsız `tests/test_b1_bypass.py` (2 test): coordinate_close(None) 0 POST; run_once
  başarısız coordinator kurulumunda open_coin çağırmaz. inc13 eski fallback testi
  fail-closed'a güncellenmiş.
- **PARTIAL (minor doc bulgusu):** `coordinate_close` docstring'i (satır 1028-1031)
  hâlâ "The direct reduce-only fallback is used ONLY when no coordinator is wired ...
  never silently skipped" diyor — B-1 bu fallback'i KALDIRDI (artık fail-closed, POST
  yok). Kod doğru; docstring güncellenmemiş → yanıltıcı. Fonksiyonel etki yok.

## D — F-1 — VERIFIED
open_coin inline doğrulaması artık `_sl_adapter.sl_status(symbol, side, mode, position_
amt)` kullanıyor (satır 1232); `sl_state == SL_PRESENT` değilse (ABSENT/UNKNOWN) acil
kapanış `coordinate_close` → `request_close` (kör MARKET POST DEĞİL; koordineli,
idempotent). İkinci bir `place_algo_sl` YOK (gereksiz SL POST yok). Bağımsız
`tests/test_f1_sl_verify.py` (3 test): yanlış-side SL → acil kapanış (eskiden yanlışça
doğrulanıyordu); UNKNOWN → acil kapanış; geçerli SL → PRESENT. Mod `_verify_oneway`;
yalnızca ONEWAY yerleştirme; HEDGE upstream gate'li ve ONEWAY-stili SL'yi HEDGE'de
sl_status reddeder → fail-closed. 
- Not (kabul edilebilir fail-closed): geçici API hatasıyla sl_status UNKNOWN olursa
  (SL aslında mevcutken) 3 denemeden sonra yeni pozisyon gereksiz kapatılabilir; eski
  kodda da aynı retry-sonra-kapat davranışı vardı (regresyon değil).

## E — E-1 — VERIFIED (+ strateji riski)
`build_executor_config` triple_barrier: stop_loss/take_profit/time_limit/trailing_stop
= **None** (satır 303-306) → HB otonom çıkışları kapalı. `.request_take_profit(` /
`.request_time_limit(` üretimde ÇAĞRILMIYOR (grep: 0). Bağımsız
`tests/test_e1_exit_policy.py` (3 test) bunu sabitler. Canlı çıkış politikası =
**yalnızca borsa reduceOnly STOP_MARKET algo SL + koordineli acil kapanış**.
- **AYRI STRATEJİ RİSKİ (işaretlendi):** TP/time-limit yokluğu bir politika seçimidir;
  pozisyonlar yalnızca SL dolana ya da acil kapanışa kadar tutulur — kâr-al yok
  (kazançlar geri dönebilir), süre-limiti yok (pozisyon süresiz açık kalabilir). Bu
  beklenen K1=B politikasıysa kabul; değilse ayrı bir strateji increment'i gerekir.
  Denetçi bunu teknik olarak doğru ama **finansal/strateji açısından dikkat edilmesi
  gereken** bir risk olarak işaretler (karar kullanıcıya aittir).

## F — Testler (bağımsız koşum) — VERIFIED
Bağımsız çalıştırıldı (uygulayıcı sayıları kopyalanmadı), gerçek exit kodları:
- 4 blocker grubu (`test_c1/ b1/ f1/ e1`): **16 passed**, exit 0.
- İlgili regresyon (close_coordinator/adapter/intent/inc13/inc5_old/reservation):
  **82 passed**, exit 0.
- Tam `tests/`: **271 passed**, exit 0.
- `ruff --select F,E9` (auto_trader, close_adapter, close_coordinator, close_intent):
  **All checks passed**, exit 0.
- `py_compile` (çekirdek + 4 test): exit 0.

## Genel değerlendirme
Dört blocker düzeltmesi de kod+test düzeyinde **VERIFIED**; filtreleme/bypass/SL-
doğrulama/çıkış-politikası doğru kurgulanmış, bağımsız regresyon temiz. Kalan noktalar:
(1) C-1 canlı HB order_ids şekli **NO EVIDENCE** (HB kaynağı okunamadı; fix yine de
güvenli), (2) B-1 `coordinate_close` docstring'i güncellenmemiş (minor, fonksiyonel
etki yok), (3) E-1 SL-only çıkış politikası ayrı bir **strateji riski** (kullanıcı
kararı). Kritik/yüksek seviyede yeni açık bulgu YOK.
Release deploy kapıları (VPS erişimi, transfer kanalı, canlı envanter, anahtar
rotasyonu) bu denetimin kapsamı DIŞINDA ve ayrıca hâlâ bloklu
(bkz. `RELEASE_STOP_bb10108_2026-11-12.md` — farklı branch).

## Değişmezlere uyum (denetim sırasında)
Üretim VPS/Binance/DB/servislere dokunulmadı. Kod yazılmadı/commit edilmedi. Yalnızca
git okuma, grep, kaynak okuma, yerel venv test koşumu.
