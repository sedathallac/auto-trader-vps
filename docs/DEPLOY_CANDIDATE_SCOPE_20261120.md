# Deploy ADAYI KAPSAM TABLOSU — fix/deploy-candidate-security-20261120 @84e6356 (BLOK6 İŞ3)

Taban üretim `108002c`. Aday = üretim + tam 6 commit (F25, C.2, A-2/3/9 atomik,
A-10 guard, A-10 veto, bu dalın dokümanları). Diff: 20 dosya +1499/−43
(DEPLOY_CANDIDATE_20261120.md kendi +47 satırı sayılınca 19→20; kod diff'i
108002c..76b06bb = 9 dosya +551/−7 + dfe25a5 yeni dosyalar).

İÇERİDE/DIŞARIDA kanıtları 2026-11-20'de content-grep + ancestor-test ile
doğrulandı (komutlar satır sonunda).

## Kapsam tablosu

| Madde | Durum | Kaynak SHA | Aday SHA | Kanıt / Not |
|---|---|---|---|---|
| Health17 — HB /executors/search cursor | **DIŞARIDA** | `feadf83` | — | `git grep 'payload["cursor"]' HEAD` boş; aday auto_trader.py:1249 hâlâ `offset` sayfalama |
| Health17 — closed_at reconcile (FAZ17_CLOSED_AT_BACKFILL) | **DIŞARIDA** | `7c65c49` + `d2f650f` + `5134453` | — | sembol `FAZ17_CLOSED_AT_BACKFILL` adayda yok; test dosyası tree'de yok |
| Health17 — equity-DD sentinel regresyon kilidi | **DIŞARIDA** (dosya) | `2a0d715`, `0ef1c66` | — | tests/test_health17_equity_dd.py yok; SADECE test kilidi — davranış F25 `865d2e3` içinde adayda mevcut (Health17 merge YASAK kuralı gereği dosya düşürüldü) |
| FAZ2 — exchange_ledger writer + salt-okur mutabakat | **DIŞARIDA** | `70e2a6a` (+ araştırma uyduları `d8c84c5`, `d65fb38`) | — | exchange_ledger.py tree'de yok; merge-base ancestor false |
| FAZ18 — A-2 partial-fill çift-SL | **İÇERİDE** | `1971059` | `dfe25a5` | close_adapter.py:34 `_SL_LIVE` PARTIALLY_FILLED |
| FAZ18 — A-3 ack-kaybı PENDING sızıntısı | **İÇERİDE** | `1971059` | `dfe25a5` | `find_algo_by_client_id` HEAD:auto_trader.py'de var |
| FAZ18 — A-7 K-3 re-map spurious | **İÇERİDE** (DÜZELTME) | `1971059` (parçası) | `dfe25a5` | `remap_algo_ids` sl_placement.py:351+ — A-2/3/9 ATOMİK commit'iyle geldi; eski SHA tablosunda SATIRI YOKTU (doküman-eksikliği, içerik-var) |
| FAZ18 — A-9 conditional_margin_gate | **İÇERİDE** | `1971059` | `dfe25a5` | `conditional_margin_gate` ×2 |
| FAZ18 — A-10 ATR=0 guard + veto | **İÇERİDE** | `f7c99d4` + `1a62f3f` | `3538fad` + `76b06bb` | _ATR_ZERO_BARS + veto; 2 test dosyası tree'de |
| FAZ18 — F25 DD kapısı / 0.0-sentinel | **İÇERİDE** | `7f84149` | `865d2e3` | `_record_bad_equity`, `dd_selftest()` |
| FAZ18 — C.2 get_balance→None | **İÇERİDE** | `0857650` | `e4a6dd7` | auto_trader.py:521 None-sentinel |
| tools/orphan_report.py (BLOK5 aracı) | **DIŞARIDA** (bilinçli) | `0e3e055` + `0387685` (+`10545eb` B6 tablosu) | — | adayda yok (root'taki orphan_killer.py ÜRETİMİN kendi dosyası, karıştırılmasın); C.4 preflight adımı faz18 checkout'undan koşulur |
| Adaya sızmış başka bir şey | YOK | — | — | Health17 grep tek vuruş = F25 dd_selftest scratch-string; 6 test dosyasındaki `**_faz18kw` shim'i dfe25a5'in test-uyumu (üretim davranışı değil) |

## DIŞARIDA kalanların üretimdeki SÜREN sonucu

1. **HB cursor (feadf83 DIŞARIDA)** → üretim `/executors/search` sayfalama
   kalıcı-PARTIAL (2026-11-17T20:21:30Z'de takıldı; MISSION_STATE_20261119B.md:185)
   → kapanış-ledger yazımları kaçmaya DEVAM EDER.
2. **ADA −4.07 (F21) — EVET, tip sürüyor**: ADAUSDT SYSTEM_CLEANUP kapanışı
   (exit fill 2026-11-18 22:48:26Z; −3.975 + 0.098 com ≈ −4.07 net) ledger'a
   HİÇ yazılmadı — ayrı bir kod hatası değil, 1. maddeded cursor kusurunun
   CANLI HASARI (51 ledger satırında ADA yok; 4 executor-kayıtsız kayıt).
   Kanıt: FINAL_EVIDENCE_REPORT_FAZ9B_20261119.md:38-43,92-94;
   close_match_20261119.csv. [LED] −23.96 ↔ [FILL] −28.26 boşluğu = −4.07 ADA
   + ~+0.2 UNMATCHED; ZORLAMA-EŞLEŞTİRME YOK.
3. **closed_at backfill (DIŞARIDA)** → 27 zaman-damgalı kapanışta closed_at
   naive/çözülmemiş kalır; mutabakat timestamp'leri kanıtlanamaz.
4. **equity-DD test kilidi (DIŞARIDA)** → DD-halt bütünlüğü yalnız
   test_faz18_f25_dd_gate.py'e dayanır (bilinçli; Health17 merge YASAK).
5. **exchange_ledger/mutabakat (DIŞARIDA)** → ledger kendini-kesmeye devam
   eder; exchange-doğrulamalı günlük salt-okur mutabakat yok → ADA sınıfı
   boşluklar üretilmeye devam ederse FARK EDİLMEZ (tespit aracı eksik).
6. **orphan_report (DIŞARIDA, bilinçli)** → C.4 "her adım öncesi tek canlı
   okuma" kapısının aracı adayda yok; operatör faz18 checkout'undan çalıştırır.

## Düzeltmeler (önceki belgeye ek)
- A-7 eski eşleme tablosunda satır olarak YOKTU: içerik dfe25a5 ile İÇERİDE
  (atomik commit'i bölmek yasak). Bu tablo A-7 satırını resmileştirir.
- Diff-stat: 108002c..HEAD = 20 dosya +1499/−43 (kendi doküman dahil).
