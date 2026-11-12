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

## Dizin yapısı ve durum
- `findings/2026-11-11_sol_double_close.md` — SOL olayı forensic. **YAZILDI**
- `findings/INCREMENT_11_sol_double_close_regression.md` — SOL çift-kapatma GERÇEK-KOD
  regresyonu (CloseCoordinator→adapter→BinanceAPI, yalnızca `_session` mock).
  **YAZILDI (inc11, 2026-11-12)**; 12 test+1 xfail GREEN, davranışsal RED kanıtlı,
  tests/ **187 passed, 1 xfailed**. AYRI KÖK NEDEN RC-2: `ensure_sl_coverage` kapsama
  kontrolü zayıf (sadece algo emri varlığı) → inc11.1 önerildi.
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
