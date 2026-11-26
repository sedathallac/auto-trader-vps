# Archiver — Algo/Koşullu Emir Geçmişi Modeli (TASARIM, UYGULANMADI)

Sorun: `archiver.py` yalnızca `/fapi/v1/userTrades` (`sync_trades`) ve
`/fapi/v1/allOrders` (`sync_orders`) çekiyor. **Algo/koşullu emirler (algoId)
`allOrders`'a düşmez**, bu yüzden borsa SL'inin tetiklenmesi arşivde görünmez.
SOL olayında 1. SELL'in algo SL olduğu bu yüzden doğrudan kanıtlanamadı.

## İlgili resmî Binance USDⓈ-M Futures uçları (doğrulanacak)
- `GET /fapi/v1/openAlgoOrders` — açık algo emirleri (bot zaten `open_algo_orders_for` ile kullanıyor).
- `GET /fapi/v1/allAlgoOrders` / `GET /fapi/v1/algoOrder` (history) — algo emir geçmişi.
NOT: Uç adları ve alanları, uygulamadan önce güncel Binance API dokümanından
birebir doğrulanmalı (sürümle değişebilir). Bu tasarım alan adlarını Binance
yanıtına göre son hâline getirecek şekilde esnek bırakılmıştır.

## Yeni tablo (mevcut `orders`/`trades` DEĞİŞTİRİLMEZ)
```
CREATE TABLE algo_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    algo_id         INTEGER,              -- algoId
    client_algo_id  TEXT,                 -- clientAlgoId
    symbol          TEXT,
    side            TEXT,                 -- BUY/SELL
    position_side   TEXT,                 -- BOTH/LONG/SHORT (HEDGE için)
    order_type      TEXT,                 -- STOP_MARKET / TAKE_PROFIT_MARKET / ...
    reduce_only     INTEGER,              -- 0/1
    close_position  INTEGER,              -- 0/1
    trigger_price   REAL,                 -- stopPrice / triggerPrice
    trigger_time    INTEGER,              -- tetik zamanı (varsa)
    status          TEXT,                 -- NEW/TRIGGERED/CANCELLED/EXPIRED/FILLED
    resulting_order_id INTEGER,           -- tetik sonrası oluşan normal orderId
    orig_qty        REAL,
    executed_qty    REAL,
    book_time       INTEGER,              -- oluşturulma
    update_time     INTEGER,              -- son güncelleme
    recorded_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(algo_id)
);
CREATE INDEX ix_algo_symbol_time ON algo_orders(symbol, update_time);
```
`resulting_order_id` alanı, tetiklenen algo emrini `orders`/`trades` tablosundaki
normal `order_id` ile eşleştirmeyi sağlar — SOL olayında 1. SELL'i (788691) bir
algoId'ye bağlamak için kritik.

## Senkron fonksiyonu (sözde kod)
```
def sync_algo_orders(binance, conn):
    for sym in get_archive_symbols(conn, binance):
        hist = binance._get("/fapi/v1/allAlgoOrders",
                            {"symbol": sym, "startTime": watermark(sym)}, signed=True)
        for a in hist:                      # alan adları Binance yanıtına göre maplenir
            upsert algo_orders (algo_id=a["algoId"], client_algo_id=a.get("clientAlgoId"),
                position_side=a.get("positionSide"), order_type=a.get("type"/"origType"),
                reduce_only=a.get("reduceOnly"), close_position=a.get("closePosition"),
                trigger_price=a.get("stopPrice"/"triggerPrice"),
                resulting_order_id=a.get("orderId"),  # tetik sonrası
                status=a.get("status"), ...)
        update per-symbol watermark
```
- Idempotent: `UNIQUE(algo_id)` + `ON CONFLICT DO UPDATE` (status/executed/update_time).
- `allOrders` ile aynı sayfalama ve per-symbol watermark deseni.

## Resmî API davranış karşılaştırması (doğrulanacak)
- Hipotez: Tetiklenen STOP_MARKET algo emri, sonuçta bir **normal order** üretir ve
  bu `allOrders`'da görünür; ancak tetik-öncesi koşullu kayıt yalnızca algo
  uçlarında bulunur. `resulting_order_id` ile bu iki kaydı birleştireceğiz.
- SOL 1. SELL (orderId 788691, type=MARKET) bir algo emrin `resulting_order_id`'si
  olarak çıkarsa → "1. SELL = borsa algo SL" **TAM KANITLANIR**.

## Test planı (yazmadan önce)
1. Yeni tablo yalnızca CREATE IF NOT EXISTS; mevcut `orders`/`trades` okunur, dokunulmaz.
2. Tek sembolde (ör. XRP) dry-run: algo geçmişi çekilip satır sayısı loglanır, DB'ye
   yazmadan önce JSON örneği incelenir (gerçek alan adları teyidi).
3. `resulting_order_id` eşleşmesi: en az bir tetiklenmiş algo emir `trades.order_id`
   ile eşleşiyor mu?
4. Idempotency: iki kez çalıştır, satır sayısı artmamalı.
5. Geriye dönük: SOL algoId 2000001643598824 (open log'da) geçmişte görünüyor mu?

## Kısıt
Uygulama için yazma izni gerekli; şu an YOK. Önce yalnızca tasarım+test planı.
