# MASTER INDEX — Algorithmic Trading System Engineering Program

Son güncelleme: 2026-11-12 · Uygulama ortamı: Zorin (`~/auto_trader`), `feature/futures-close-coordinator`.

> Bu bir program, tek oturumluk iş değil. Her oturum başında önce bunu oku.
> Kanıt olmadan "DOĞRULANDI" yazılmaz; test çıktısı olmadan "geçti" denmez.

## İş bölümü (ortam gerçeği)
- **VPS oturumu (burası):** salt okunur adli denetim + bilgi tabanı yazımı.
  `bash_guard` burada `python3`/`pytest`/`sqlite3`/`curl`/`docker exec` engelliyor →
  **bu oturumda test KOŞULAMAZ.** Kod okuma, log, arşiv (kb_*.py) yapılabilir.
- **Zorin makinesi:** araç zinciri kurulu (pytest/ruff/mypy/bandit/semgrep/…).
  Kod düzeltmeleri ve test KOŞUMU orada, ayrı branch'te yapılmalı. Canlıya deploy YOK.

## Üç bileşen — mevcut durum
| Bileşen | Canlı emir? | Olgunluk | En kritik sorun |
|---|---|---|---|
| Futures (`/root/auto_trader.py`) | **EVET (aktif)** | Düşük-orta; tek dosya, test yok | Çift-kapanış → ters SHORT (SOL kanıtı) |
| Spot Grid (`/opt/gridc0`) | **EVET (CANLI, düzeltildi)** | Yüksek; paket+30 test+Decimal | B1.8 LIVE; 16 açık emir (BTC+ETH); testler koşulmadı |
| Hummingbot (container) | Futures için aracı | dev-2.18.0 | ONEWAY reduceOnly'siz kapanış |

## Bütünleşik kabul
- `CLOSECOORDINATOR_ACCEPTANCE_REPORT.md` — CloseCoordinator + SL/migration (inc3–inc12)
  bütünleşik kabul raporu. HEAD `21cfd7f`, **229 passed**, üretime geçilmedi. **YAZILDI (2026-11-12)**

## Bağımsız denetimler
- `REVIEWS/REVIEW_release_blockers_3d48b18_2026-11-12.md` — release blocker fix'lerinin
  (C-1/B-1/F-1/E-1) salt-okunur bağımsız son denetimi (branch `feature/futures-release-
  blockers`, HEAD `3d48b18`). Dört fix kod+test düzeyinde **VERIFIED**; bağımsız
  regresyon **271 passed** (exit 0), ruff F/E9 + py_compile temiz. Kalan: C-1 canlı HB
  order_ids şekli NO EVIDENCE (fix güvenli), B-1 `coordinate_close` docstring güncel
  değil (minor), E-1 SL-only çıkış politikası strateji riski (kullanıcı kararı).
  Kritik/yüksek yeni açık bulgu YOK. **(Commit edilmedi — denetim salt-okunur.)**

## Dizin yapısı ve durum
- `findings/2026-11-11_sol_double_close.md` — SOL olayı forensic. **YAZILDI**
- `findings/INCREMENT_11_sol_double_close_regression.md` — SOL çift-kapatma GERÇEK-KOD
  regresyonu (CloseCoordinator→adapter→BinanceAPI, yalnızca `_session` mock).
  **YAZILDI (inc11, 2026-11-12)**; 12 test+1 xfail GREEN, davranışsal RED kanıtlı,
  tests/ **187 passed, 1 xfailed**. AYRI KÖK NEDEN RC-2: `ensure_sl_coverage` kapsama
  kontrolü zayıf (sadece algo emri varlığı) → inc11.1'de DÜZELTİLDİ.
- `design/INCREMENT_11_1_sl_coverage_fix.md` — RC-2 düzeltmesi: `ensure_sl_coverage`
  artık inc10.1 mode-aware `sl_status` kullanır (PRESENT/ABSENT/UNKNOWN, tam kapsama).
  **YAZILDI (inc11.1, 2026-11-12)**; 17 yeni test GREEN, davranışsal RED (12 yanlış-değer),
  tests/ **204 passed, 0 xfailed**.
- `design/INCREMENT_12_integration.md` — concurrency/restart/timeout/UNKNOWN entegrasyonu.
  **YAZILDI (inc12, 2026-11-12)**; RC-A CloseCoordinator lock (thread + cross-process
  SQLite), RC-B `_OPEN_UNPROTECTED` flat-temizliği. 25 yeni test GREEN, davranışsal RED
  (2 kök neden), tests/ **229 passed**.
- `design/INCREMENT_12_1_close_intent.md` — cross-process close-intent/idempotency
  (`close_intent.py`, aynı SQLite defteri). **YAZILDI (inc12.1, 2026-11-12)**; 21 yeni
  test GREEN (gerçek iki-process tek POST), tests/ **250 passed**. DENETİM: CloseCoordinator
  üretim döngüsüne BAĞLI DEĞİL (kritik entegrasyon açığı — inc13'te giderildi).
- `design/INCREMENT_13_live_wiring.md` — CloseCoordinator'ın GERÇEK open_coin/run_once
  döngüsüne bağlanması; iki doğrudan market-close `coordinate_close`→`request_close`'a
  yönlendirildi. **YAZILDI (inc13, 2026-11-12)**; 5 yeni test GREEN (canlı zincir
  request_close'a ulaşır), davranışsal RED kanıtlı, tests/ **255 passed**. Bağımsız
  denetim PARTIAL (öz-denetim; reisfurkan-53 çevrimdışı).
- `design/safe_close_architecture.md` — tek kapanış otoritesi. **YAZILDI**
- `design/archiver_algo_order_model.md` — algo emir arşivi. **YAZILDI (tasarım)**
- `design/K1_DECISION_exchange_sl_authority.md` — K1=B kararı (borsa SL tek otorite). **YAZILDI**
- `design/INCREMENT_10_position_migration.md` — açık pozisyon güvenli migration/handoff
  + `sl_status` tri-state (PRESENT/ABSENT/UNKNOWN) sertleştirme.
  **YAZILDI (inc10 + inc10.1, 2026-11-12)**; `migration.py` + `open_algo_orders_raw` +
  mode-aware (ONEWAY/HEDGE) tam-koruma doğrulaması. 17+16+13 test GREEN, davranışsal
  RED kanıtlı (4+6+6 yanlış-değer), tests/ **175 passed**.
- `architecture/system_overview.md` — üç bileşen. **YAZILDI**
- `architecture/zorin_toolchain.md` — Zorin araçları. **DOĞRULANDI**
- `architecture/SPOT_GRID_ARCHITECTURE.md` — GridC0. **YAZILDI (bu tur)**
- `FORMAL_MODELS/SYSTEM_INVARIANTS.md` — 15+10 invariant + durum. **YAZILDI (bu tur)**
- `TESTING/tests/test_close_authority_model.py` — SOL regresyon modeli. **YAZILDI, KOŞULMADI (Zorin'de koş)**
- Aşağıdakiler PLANLANDI, henüz yazılmadı (ungrounded stub yazmıyorum):
  REFERENCES/, FORMAL_MODELS/ORDER_STATE_MACHINE, ENGINEERING/*, TESTING/* kalanı,
  SECURITY/*, OBSERVABILITY/*, RELEASES/*.

## Sıradaki önerilen adımlar (öncelik)
1. Futures çift-kapanış invariant'ını Zorin'de model testiyle kırmızı/yeşil doğrula.
2. Futures execution/reconciliation düzeltme tasarımını koda dök (ayrı branch, Zorin).
3. GridC0 mevcut 30 testi Zorin'de koş, yeşil tabanı kaydet (henüz çalıştırılmadı).
4. Hummingbot postgres + container digest için dar salt-okunur izinler (kullanıcı açar).
5. Dış kaynak araştırması → REFERENCES/ (Binance USDⓈ-M, HB, NIST SSDF 1.1, OWASP).

## Değişmez kullanıcı politikaları
MAX_OPEN_POSITIONS=2 (değiştirme) · canlı emir/pozisyon/SL/servis/DB-yazma YOK ·
strateji/risk/sermaye parametreleri izinsiz değişmez · ifşa kimlik bilgileri raporlanmaz.
