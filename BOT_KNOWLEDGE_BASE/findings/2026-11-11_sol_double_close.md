# SOLUSDT Çift-Kapanış / Ters SHORT Olayı — Forensic Kayıt

Tarih: 2026-11-11 · Durum: tarihsel kanıt tamam, canlı teyit YOK, düzeltme uygulanmadı.

## Zaman çizelgesi (Türkiye saati, UTC+3)
| İşlem | Saat | order_id | miktar | fiyat | realized_pnl |
|---|---|---|---|---|---|
| LONG açılış (BUY MARKET) | 07:44:36 | 257407377823 | 0.3 | 83.08 | 0.0 |
| Birinci SELL (MARKET) | 10:05:07 | 257439788691 | 0.3 (0.08+0.22) | 82.26/82.23 | −0.2526 |
| İkinci SELL (MARKET) | 10:05:08 | 257439806439 | 0.3 | 82.26 | 0.0 |

Kaynak: `.trading_archive.db` tabloları `trades` ve `orders` (salt okunur).
HB executor: `CWMinzu1YncW3Y9TPHXayCNGMwAXD3eXNYmNzjEqmwuh` (auto_trader.log:947).

## Komisyon dökümü (USDT)
| Grup | commission |
|---|---|
| İlk BUY (257407377823) | 0.012462 |
| Birinci SELL (257439788691) | 0.0032904 + 0.00904529 = 0.01233569 |
| İkinci SELL (257439806439) | 0.012339 |
| **Üç emir toplam** | **0.03713669** |
| HB −0.270801 hesabına giren iki komisyon (BUY + 2. SELL) | **0.024801** |

## −0.2708 mutabakatı (çok güçlü dolaylı kanıt)
HB risk defteri PnL = −0.2708 (auto_trader.log:1041; `net_pnl_quote`).
- 2. SELL varsayımı: (82.26−83.08)×0.3 − 0.012462 − 0.012339 = **−0.270801** ✓ birebir.
- 1. SELL varsayımı: (82.238−83.08)×0.3 − 0.012462 − 0.0123357 = −0.2774 ✗.
→ HB muhasebesi kapanış olarak **2. SELL'in fiyat+komisyonunu** kullanmış.
NOT: Bu, emir kimliği düzeyinde doğrudan eşleşme DEĞİLDİR; PostgreSQL
executor↔order eşleşmesi okunamadı (driver yok). Matematiksel parmak izidir.

## Sınıflandırma (denetim kararlarıyla)
| İddia | Sınıf |
|---|---|
| İki ayrı 0.3 SELL gerçekleşti (MARKET, FILLED) | **DOĞRULANDI** |
| İlk SELL LONG'u kapattı (realized_pnl −0.2526) | **GÜÇLÜ KANIT** |
| İkinci SELL, HB'nin PnL hesabında kapanış olarak kullanıldı | **ÇOK GÜÇLÜ DOLAYLI KANIT** (−0.270801 mutabakatı) |
| İlk SELL'i kesin olarak Binance algo SL gönderdi | **TAM KANIT YOK** (algo history + algoId/clientAlgoId eşleşmesi gerek) |
| HB kapanışı ONEWAY modda ters SHORT açtı | **KISMEN** — ONEWAY doğrudan teyit YOK (positionSide arşivde yok) |
| 19:00:34'teki −0.3, canlı Binance API'den | **DOĞRULANDI** (tarihsel canlı teyit; bugünkü durum değil) |
| orphan_killer açık pozisyonu SL kontrolü dışında bıraktı | **DOĞRULANDI** (kaynak kod) |
| "0.0182 = komisyon" | **ÇÜRÜTÜLDÜ** (iki farklı emrin ölçüleri arası fark) |
| SOL SHORT bugün hâlâ açık | **CANLI TEYİT YOK** |

## Kök neden (hipotez, kanıta dayalı)
Her pozisyonda iki bağımsız ~%1 stop-loss var:
- (a) HB PositionExecutor triple-barrier stop_loss (`position_executor.py:519-528`).
- (b) auto_trader'ın borsaya koyduğu reduceOnly STOP_MARKET algo SL
  (`auto_trader.py:215-222`, `place_algo_sl`; open_coin:808-812).
İkisi yakın seviyede → neredeyse aynı anda tetiklenir. Borsa reduceOnly SL long'u
kapatır (flat); 1 sn sonra HB kapanışı — ONEWAY'de `close_position_action` →
`PositionAction.OPEN`, yani reduceOnly'siz MARKET (`position_executor.py:221-225`) —
flat pozisyonda dolup **ters SHORT açar**. Sonra her iki SL-koruma yolu da
drift/erken-çıkış yüzünden bu short'a SL koyamaz.

## orphan_killer neden koruyamadı
- `orphan_killer.py:80`: `to_check = tracked - open_syms` → yalnızca KAPALI semboller.
  SOL açık → `open_syms` içinde → `to_check` dışı → satır 81-83 erken `return`.
  `ensure_sl_coverage` satır 120-121'de, yalnızca iptal edilecek yetim varsa çalışır.
- `auto_trader.py:954-956`: drift → `run_once` döner; `ensure_sl_coverage` (963) hiç çağrılmaz.
- 13:30'da (XRP yetim SL'i tam yolu tetikleyince) SOL'a SL denendi → borsa
  `-2021 "Order would immediately trigger"` (orphan_killer.log:4791). Short zaten zararda.

## Sistemik değerlendirme
Tüm arşivde (2026-11-09…11) net≠0 kalıntı **yalnızca SOLUSDT (−0.3)**.
AVAX normal aç/kapa döngüleri (net 0); ETH/XRP long-only, temiz.
Mekanizma sembolden bağımsızdır; kalıcı hasar bugün yalnızca SOL'da.
→ Düzeltme sembole özel değil, mimari olmalı (bkz. design/safe_close_architecture.md).

## Kapatılamayan kanıt ve gereken en dar izinler
1. 1. SELL = algo SL kesinliği → `GET /fapi/v1/allAlgoOrders`/`algoOrder` history
   ve algoId/clientAlgoId eşleşmesi (arşivlenmiyor).
2. Emir→bileşen doğrudan eşleşmesi → `hummingbot_api` PostgreSQL salt okunur SELECT.
3. Çalışan kod = disk kopyası → `docker inspect` veya `/proc/<pid>/root` okuma.
4. ONEWAY teyidi + canlı durum → imzalı GET `positionSide/dual`, `positionRisk`, `openAlgoOrders`.
