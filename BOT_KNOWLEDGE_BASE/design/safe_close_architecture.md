# Güvenli Kapanış Mimarisi — Tasarım (UYGULANMADI)

Amaç: Tek pozisyon üzerinde birden fazla bağımsız kapanış mekanizmasının
birbirini tetikleyip ters pozisyon açmasını engellemek. Kaynak kanıt:
`findings/2026-11-11_sol_double_close.md`.

## Zorunlu koşullar (kullanıcı kararı)
1. **Tek kapanış otoritesi.**
2. Borsada gerçek pozisyon sıfırlanmadan executor **kapanmış sayılmamalı**.
3. Pozisyon kapanınca bekleyen diğer koruma emirleri güvenli yönetilmeli.
4. ONEWAY ve HEDGE modları ayrı değerlendirilmeli.
5. Koruyucu emir doğrulaması reconciliation drift durumunda da çalışmalı.
6. Açık ve SL'siz pozisyon için kritik alarm.
7. `MAX_OPEN_POSITIONS=2` her emirden hemen önce denetlenmeli.

## Tasarım kararları

### K1 — Tek kapanış otoritesi (iki seçenek)
- **A (önerilen): HB tek otorite.** Borsa reduceOnly STOP_MARKET algo SL kaldırılır;
  SL/TP/time-limit yalnızca HB triple-barrier'da. auto_trader yalnızca giriş + izleme.
  Risk: HB/connector down olursa borsa-tarafı failsafe kalmaz → K5 ile telafi.
- **B: Borsa tek otorite.** HB triple-barrier stop_loss kapatılır (TP/time-limit kalır
  ya da onlar da borsaya taşınır); kapatıcı tek emir = reduceOnly STOP_MARKET.
  Risk: HB'nin pozisyon muhasebesi borsa SL'ini görmeli (K2).
- Her iki seçenekte de **aynı anda iki aktif kapatıcı asla bulunmaz.**

### K2 — Kapanış = borsada flat teyidi
- Executor TERMINATED → risk defterine yazmadan önce `positionRisk` ile
  `positionAmt==0` doğrula. Değilse: "executor kapandı ama borsa flat değil" →
  kritik alarm, yeni işlem açma, otomatik ikinci satış GÖNDERME (ters pozisyon riski).
- `sync_risk_engine` ve `reconcile_positions` arasına bu teyit kapısı eklenir.

### K3 — Bekleyen koruma emirlerinin yaşam döngüsü
- Pozisyon flat olunca: o sembolün tüm açık algo/koşullu emirleri **önce iptal**,
  sonra executor TERMINATED işaretlenir (iptal→flat→kapat sıralaması).
- Kalan reduceOnly emir flat'te tetiklenirse borsa reddeder; yine de aktif iptal
  edilmeli ki yanlış tetik/again-fill olmasın.

### K4 — ONEWAY vs HEDGE
- **ONEWAY:** netleşen tek pozisyon. Kapanış mutlaka `reduceOnly=true` gitmeli.
  HB dev-2.18.0 ONEWAY'de reduceOnly'siz kapatıyor (`position_executor.py:221-225`) →
  bu davranış seçenek A'da zaten devre dışı; seçenek B'de HB kapanışı kullanılmıyor.
  Alternatif: hesabı HEDGE moda almak ve positionSide ile kapatmak.
- **HEDGE:** long/short ayrı; kapanış doğru bacağı hedeflemeli (positionSide LONG/SHORT).
  Kapanış emirlerinde `reduceOnly` yerine positionSide + closePosition kullanılır.
- Karar: Pozisyon modu açıkça tespit edilmeli (`positionSide/dual`) ve bot, modu
  kod başlangıcında doğrulayıp uyumsuzsa fail-closed olmalı. Şu an bot modu hiç kontrol etmiyor.

### K5 — Drift'te de koruma doğrulaması
- `ensure_sl_coverage` drift'ten BAĞIMSIZ çalışmalı (SL eklemek yeni işlem açmak değil).
  Mevcut akış drift'te 954-956'da döner, SL kontrolü (963) atlanır → değişmeli:
  reconcile False dönse bile "açık + SL'siz pozisyon" taraması yapılmalı.
- orphan_killer: açık pozisyonlar için SL kontrolü, `to_check` boş-erken-çıkıştan
  (satır 81-83) ÖNCE yapılmalı; şu an yalnızca kapalı semboller taranıyor.

### K6 — Açık + SL'siz pozisyon alarmı
- Her döngüde: açık her pozisyon için en az bir aktif koruyucu emir var mı?
  Yoksa CRITICAL alarm + (seçilen otoriteye göre) tek SL yeniden kur.
- `-2021 Order would immediately trigger` artık sessiz geçilmez → CRITICAL
  (korumasız, zararda pozisyon demek).

### K7 — Pozisyon limiti
- `MAX_OPEN_POSITIONS` her `open_coin` çağrısından hemen önce yeniden sayılır
  (zaten C6-fix mevcut: auto_trader.py:1009-1015). Tasarım bunu korur; kapanış
  otoritesi değişince sayım kaynağı tek (borsa `positionRisk`) olmalı.

## Test senaryoları (uygulamadan önce koşulacak, canlı emir YOK)
1. **Çift tetik simülasyonu:** İki kapatıcı aynı anda tetiklenirse yalnızca biri
   emir göndermeli; ikincisi flat teyidiyle iptal olmalı. (birim test, mock borsa)
2. **Flat teyidi kapısı:** Executor TERMINATED ama positionAmt≠0 → yeni işlem yok,
   alarm var, ikinci satış yok. 
3. **reduceOnly dolum:** ONEWAY'de kapanış reduceOnly ile gönderiliyor; flat'te
   reddi beklenen; ters pozisyon AÇILMADIĞI doğrulanmalı.
4. **Drift'te SL:** reconcile False iken açık+SL'siz pozisyona SL kuruluyor mu.
5. **orphan_killer sırası:** açık pozisyon SL kontrolünün erken-çıkıştan önce.
6. **-2021 yolu:** immediately-trigger → CRITICAL alarm üretiliyor mu.
7. **HEDGE modu:** positionSide doğru bacağı kapatıyor mu (ayrı ortamda).
8. **Limit yarışı:** iki aday paralel → MAX_OPEN_POSITIONS aşılmıyor (regression).

## Sıralı uygulama planı (onay sonrası)
1. Önce K2 + K6 (tespit/alarm; davranışı değiştirmez, güvenlik artırır).
2. Sonra K1 (tek otorite) seçimi — A veya B kullanıcı kararı.
3. K3/K4/K5 ilgili otoriteye göre.
4. Tüm test senaryoları yeşil olmadan canlıya alınmaz.
