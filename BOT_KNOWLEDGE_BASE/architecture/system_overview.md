# Sistem Mimarisi — Genel Bakış (2026-11-11)

Üç bağımsız bileşen, tek VPS'te, aynı Binance hesabı ve aynı Hummingbot-API.
İşaret: ✅ doğrulandı · �ðŸŸ¡ kısmen · ❓ eksik (sonraki oturumda haritalanacak).

## 1) Futures botu — `auto_trader.py` (✅ büyük ölçüde haritalandı)
- Servis: `auto_trader.service` (systemd, `ExecStart=/root/venv/bin/python /root/auto_trader.py --loop`, Restart=always, EnvironmentFile=/root/.env).
- Döngü: 900s tarama, 60s heartbeat (`.auto_trader.heartbeat`).
- Borsa: Binance **USDⓈ-M Futures** (`fapi.binance.com`), imzalı HMAC.
- İşlem açılışı HB executor üzerinden (`position_executor`, `binance_perpetual`);
  SL ek olarak borsaya reduceOnly STOP_MARKET algo emriyle de konur → **çift SL** (risk).
- Risk: `risk_engine.py` + SQLite `risk_ledger.db` (C3 migrasyonu, transactional).
- Güvenlik kapıları: reconcile drift (954-956), stale-data (958), SL coverage (963),
  korelasyon/sektör limiti, C6 fail-closed pozisyon limiti (`MAX_OPEN_POSITIONS`, env, şu an bilinçli düşük).
- Yardımcılar: `orphan_killer.py` (yetim SL avcısı, 60s), `archiver.py` (trades/orders → `.trading_archive.db`).
- AÇIK SORUN: SOLUSDT çift-kapanış/ters-short (bkz. findings) + çift-SL koordinasyon riski.

## 2) Spot Grid — "GridC0" (🟡 kısmen haritalandı) — **CANLI**
- DÜZELTME (2026-11-11): Önce "read-only" sanıldı; YANLIŞ. `real_transport.py:28`
  `POST_DELETE_NEVER_ENABLED=False` (B1.8 LIVE). gridc0.db: 16 FILLED + 16 açık NEW
  emir, BTCUSDT+ETHUSDT LIMIT_MAKER. **Canlı emir gönderiyor.**
- Borsa: Binance **SPOT** (`api.binance.com /api/v3`), semboller **BTCUSDT + ETHUSDT**.
- Kendi transport/order_manager/state_store'u (Hummingbot'tan BAĞIMSIZ); `kill_spot_grid*`
  scriptleri HB executor stop + Binance openOrders iptali yapıyor (ayrı acil-durdurma).
- Kill switch'ler: `kill_spot_grid.py`, `kill_spot_grid_c0.sh` (yalnızca verilen
  executor'ı durdurur + BTCUSDT açık emirlerini iptal eder; auto_trader'a dokunmaz).
- Futures'tan **bağımsız** (ayrı borsa API tabanı, ayrı sembol, ayrı kill yolu).
- ❓ EKSİK: Grid'i OLUŞTURAN başlatıcı/controller nerede? Muhtemelen HB controller
  (`hummingbot-api/bots/controllers/{generic,market_making}`) veya `bots/scripts`.
  Grid aralığı, adım, sermaye, muhasebe henüz incelenmedi → sonraki görev.
- ❓ Standing systemd servisi görünmüyor; manuel/HB-içi başlatma olabilir.

## 3) Hummingbot — altyapı bileşeni (🟡)
- Container'lar: `hummingbot-api` (uvicorn, port 8000), `hummingbot-postgres`
  (postgres:16, db `hummingbot_api`, user `hbot`, 127.0.0.1:5432), `hummingbot-broker` (emqx:5).
- Sürüm: `dev-2.18.0` (disk kopyası; çalışan container'a hash düzeyinde bağlanamadı).
- Executor modeli: `PositionExecutor` triple-barrier (SL/TP/time-limit).
- KRİTİK DAVRANIŞ: ONEWAY modda kapanış `PositionAction.OPEN` → reduceOnly'siz
  MARKET (`position_executor.py:221-225`). Flat pozisyonda ters pozisyon açabilir.
- Veri: executor/order kayıtları PostgreSQL'de (diskte SQLite yok). Salt okunur
  erişim şu an YOK (python postgres driver yok; docker exec/psql kapalı).
- Host dizini: `/root/hummingbot-api` (compose, credentials `bots/credentials/master_account`).

## Ortak noktalar / riskler
- Her iki bot aynı `BINANCE_KEY/SECRET` (ortam) kullanıyor.
- Düz metin kimlik bilgileri: `auto_trader.py` HB varsayılanları; `kill_spot_grid*.{py,sh}`
  HB admin parolası. Rotasyon önerilir (değerler burada saklanmaz).

## Sonraki haritalama görevleri (öncelik sırası)
1. Futures güvenlik mimarisi düzeltme tasarımı → `design/safe_close_architecture.md` (hazır).
2. Spot Grid tam haritası: controller/script kaynağı, grid parametreleri, risk, muhasebe.
3. Hummingbot entegrasyonu: PostgreSQL şeması, executor↔order ilişkisi, connector sürüm teyidi.
4. Her bulgu bu depoya yazılır; oturum başında önce burası okunur.
