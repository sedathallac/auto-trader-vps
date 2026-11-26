# GridC0 (Spot Grid) — Mimari (salt okunur inceleme, 2026-11-11)

Konum: `/opt/gridc0` (kendi git deposu). Servis: `gridc0-live.service`
(active, enabled; `ExecStart=/root/venv/bin/python /opt/gridc0/run_engine.py`,
EnvironmentFile=`/etc/gridc0/gridc0.env`, config `/etc/gridc0/config.json`).

## EN ÖNEMLİ BULGU: CANLI (düzeltildi 2026-11-11)
DÜZELTME: Önceki not "read-only" diyordu — YANLIŞTI (run_engine.py docstring'ine
güvenilmişti). Gerçek durum:
- `gridc0/real_transport.py:28`: `POST_DELETE_NEVER_ENABLED = False`
  (yorum: "B1.8: LIVE MODE UNLOCKED (user-approved)"). POST/DELETE artık hard-block DEĞİL.
- `POST_PATHS = DELETE_PATHS = {'/api/v3/order'}` → emir gönderme/iptal açık.
- Kanıt `/opt/gridc0/data/gridc0.db` (salt okunur): 16 FILLED (borsa order_id'li),
  **16 NEW = şu an borsada açık emir**, 6 CANCELED, 17 REJECTED. Semboller
  **BTCUSDT + ETHUSDT**, LIMIT_MAKER BUY; son gönderim 2026-11-11T17:19Z.
- `gridc0.db-wal` aktif yazılıyor; servis `active`.
Sonuç: Spot Grid **canlı ticaret yapıyor** (Binance SPOT, BTC+ETH). Futures ile
birlikte **iki canlı sistem** var. Teyit edilemedi (guard): `/etc/gridc0/config.json`
`trade_enabled` değeri ve borsadaki gerçek açık emir sayısı (canlı sorgu yok).

## Olgunluk — Futures'tan belirgin ileri
- Düzgün paket: `gridc0/` altında live_client, risk, reconcile, filters,
  decimal_utils, inventory, level_lifecycle, order_manager, models, state_store,
  grid_manager, engine, reconcile_loop, real_transport.
- **Decimal aritmetiği** (`decimal_utils.py`) — finansal hesapta float değil.
- **30 test dosyası** (`tests/test_*.py`), aralarında doğrudan invariant'lara
  karşılık gelenler: `test_level_lifecycle`, `test_om_blocking`, `test_inventory`,
  `test_reconcile`, `test_reconcile_loop`, `test_client_order_id`, `test_stop_loss`,
  `test_kill_switch`, `test_risk`, `test_fill_sync`, `test_order_sync`, `test_filters`,
  `test_backtest`, `test_real_transport(_v2)`, `test_state_store`, `test_decimal_utils`.
- Bütçe sabiti `MAX_LEVEL_BUDGET` (`grid_manager.py`), per-level budget, fee reserve,
  take_profit_pct config alanları (`run_engine.py:_symbol_configs`).
- Instance id (`/etc/gridc0/instance_id`, 0600), structured log, "keys/secrets asla loglanmaz".

## Durum: KISMEN İNCELENDİ
Bu tur yalnızca `run_engine.py` + dosya envanteri okundu. Henüz doğrulanmadı:
- Grid matematiği (geometrik/aritmetik, spacing, recenter) — `grid_manager.py`/`engine.py`.
- Emir yaşam döngüsü ve duplicate-level engeli — `order_manager.py`/`level_lifecycle.py`.
- Bakiye rezervasyonu (locked/free) — `inventory.py`.
- Reconciliation — `reconcile.py`/`reconcile_loop.py`.
- 30 testin gerçekten geçip geçmediği — **KOŞULMADI** (Zorin'de koşulmalı).

## Sıradaki GridC0 görevleri
1. `gridc0/` modüllerini tek tek oku; grid invariant'larını koda bağla.
2. Mevcut 30 testi Zorin'de koş, yeşil taban + coverage kaydet.
3. "would block" tarzı yalnız-uyarı kontrollerinin gerçek emir noktasında
   engellediğini testle kanıtla (invariant Grid-7, Grid-8).
4. Canlı açılış (Gate D) için ayrı yayın hazırlık raporu — bu görevin dışında.
