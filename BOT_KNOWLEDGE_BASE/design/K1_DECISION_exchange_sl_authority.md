# K1 KARARI = B — Borsa SL Tek Kapanış Otoritesi

Tarih: 2026-11-11 · Karar veren: kullanıcı (kesin) · Durum: **TASARIM + feature branch yetkisi. Canlıya uygulama YOK.**

## Kararın zorunlu koşulları (kullanıcı)
1. HB stop_loss bariyeri bağımsız kapanış gönderemez.
2. HB take_profit / time_limit / trailing_stop bağımsız MARKET kapanış gönderemez; talepler CloseCoordinator'dan geçer.
3. Tüm kapanışlar (SL, TP, time-limit, acil) tek merkezi CloseCoordinator'a tabi.
4. Binance STOP_MARKET algo emri VPS/bottan bağımsız son güvenlik katmanı.
5. Borsa SL dolunca gerçek pozisyon Binance'ten doğrulanmadan executor CLOSED sayılmaz; executor yeni kapanış gönderemez.
6. SL sonrası executor + diğer tetikleyiciler güvenli terminal duruma (gecikmiş WS, eşzamanlı TP/time-limit, restart dahil).
7. Her kapanıştan önce pozisyon/miktar/yön/açık emir kontrolü; idempotent; timeout/UNKNOWN ikinci emre yol açmaz.
8–9. ONEWAY/HEDGE ayrı; reduceOnly/closePosition/positionSide/algo kuralları güncel dokümandan doğrulanmadan kod yok.
10. Ön koşul: K2/K3 flat teyidi, K6 drift'te koruma, SOL çift SELL regression.
11. Testler: SOL, AVAX/ETH/XRP, partial fill, gecikmiş fill, WS kopması, restart, eşzamanlı iki kapanış.
12. MAX_OPEN_POSITIONS=2 sabit; her girişten hemen önce.

## Doğrulanan Binance USDⓈ-M kuralları (resmi doküman, 2026-11-11 okundu)
Kaynak: developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api + change-log
| Kural | Alıntı | Sınıf |
|---|---|---|
| `/fapi/v1/order` positionSide | "Default BOTH for One-way Mode ; LONG or SHORT for Hedge Mode. It must be sent in Hedge Mode." | DOĞRULANDI |
| `/fapi/v1/order` reduceOnly | "Cannot be sent in Hedge Mode" | DOĞRULANDI |
| `/fapi/v1/order` closePosition | request parametresi DEĞİL (yalnız yanıt alanı) | DOĞRULANDI |
| MARKET zorunlu | `MARKET` → `quantity` | DOĞRULANDI |
| newClientOrderId | "A unique id among open orders" → dolmuş MARKET emrinde dedupe SAĞLAMAZ | DOĞRULANDI |
| Koşullu emirler | 2025-12-09'dan beri Algo Service; `/fapi/v1/order` -4120 STOP_ORDER_SWITCH_ALGO ile bloklar | DOĞRULANDI |
| Algo closePosition | "Close-All, used with STOP_MARKET or TAKE_PROFIT_MARKET"; "Cannot be used with quantity"; "Cannot be used with reduceOnly" | DOĞRULANDI |
| Algo reduceOnly | "Cannot be sent in Hedge Mode; cannot be sent with closePosition=true" | DOĞRULANDI |
| Algo HEDGE | closePosition: "cannot be used with BUY orders in LONG position side" / "SELL orders in SHORT" | DOĞRULANDI |
| Algo sorgu | GET/DELETE /fapi/v1/algoOrder: algoId veya clientAlgoId; `actualOrderId` "Empty string if not triggered" | DOĞRULANDI |
| Order/algo saklama | CANCELED/EXPIRED + fill yok + 3 gün → bulunamaz; 90 gün sınırı | DOĞRULANDI |
| positionSide/dual | dualSidePosition "true": Hedge; "false": One-way | DOĞRULANDI |
| GET /fapi/v1/openAlgoOrders parametre/yanıt | sayfa kesildi, okunamadı | KANIT YOK |

Tasarım sonucu:
- **SL (ONEWAY):** algo STOP_MARKET `closePosition=true`, quantity/reduceOnly YOK (partial fill/ekleme sonrası tüm pozisyonu kapsar, aşma yapamaz). Mevcut kod quantity+reduceOnly kullanıyor (ONEWAY'de geçerli, HEDGE'de geçersiz).
- **SL (HEDGE):** `closePosition=true` + `positionSide=LONG/SHORT`, reduceOnly YOK.
- **Koordinatör MARKET kapanışı ONEWAY:** `reduceOnly=true` + `quantity=|positionAmt|`; HEDGE: `positionSide` + quantity, reduceOnly YOK.
- Dedupe newClientOrderId'e DAYANMAZ; UNKNOWN sonuç `GET /fapi/v1/order?origClientOrderId=` ile çözülür, sonra pozisyon yeniden okunur.

## Doğrulanan Hummingbot davranışı (çalışan container, salt okunur)
Sürüm: hummingbot `20260922`, hummingbot-api image `26.7.1-1` (sha256:c2406cc…).
| Bulgu | Kanıt | Sınıf |
|---|---|---|
| ONEWAY'de HB kapanışı `PositionAction.OPEN` (reduceOnly YOK) | position_executor.py:208-225 | DOĞRULANDI (SOL ters short mekanizması çalışan sürümde mevcut) |
| Tüm bariyerler Optional; None → control_* atlanır | data_types.py TripleBarrierConfig; :526,:539,:285,:783 | DOĞRULANDI |
| `early_stop(keep_position=True)` → POSITION_HOLD → kapanış emri gönderilmez | :618-624, :333-347, :496 | DOĞRULANDI |
| `keep_position=False` (API varsayılanı!) → MARKET kapanış (ONEWAY'de reduceOnly'siz) | routers/executors.py:330, models/executors.py:311 default=False | DOĞRULANDI |
| HB-api restart/shutdown'da executor'lar ne yapar | services/executor_service.py:306 `stop()` okunmadı | KANIT YOK |

Tasarım sonucu (B uygulaması):
- `create_executor` payload: stop_loss/take_profit/time_limit/trailing_stop = **None** → HB yalnızca giriş yapar, asla kapatmaz.
- Executor durdurma her zaman `POST /executors/{id}/stop {"keep_position": true}`; `false` kod tabanında yasak (test ile).
- TP ve time-limit CloseCoordinator'da (bot döngüsü) değerlendirilir; kapanış yalnız koordinatörden.

## Uygulanan (feature branch, canlıya alınmadı)
- `~/auto_trader` branch `feature/futures-idempotency`, commit 2cd296b: urllib3 retry yalnız GET; create_executor UNKNOWN sonucu executor diff ile çözülür, yeniden gönderim yok. tests/test_write_idempotency.py 4/4 GREEN (RED: gerçek HTTP sunucuda POST 2x).
- GridC0 `~/gridc0` branch `feature/risk-gate-preorder`: e56fc0a risk kapısı (RED 3 POST→GREEN), 4cc1522 overfill guard + sahte fill onarım betiği; tam takım 274/274 (py3.12 ve py3.14).

## Kalan iş (sıralı)
1. HB-api `stop()`/restart davranışını oku (KANIT YOK satırı).
2. CloseCoordinator modülü + Binance ONEWAY/HEDGE simülatörü; RED testler: SOL çift SELL, AVAX/ETH/XRP, partial/gecikmiş fill, WS kopması, restart, eşzamanlı iki kapanış.
3. K2 flat teyidi (sync_risk_engine), K6 drift'te ensure_sl_coverage, orphan_killer sıralaması.
4. create_executor barrier=None, SL closePosition=true, keep_position=true zorunluluğu; MAX_OPEN_POSITIONS=2 sayımı borsa positionRisk ∪ HB RUNNING.
5. Tüm testler yeşil olduktan sonra canlı geçiş için ayrı kullanıcı onayı.
