# OPERATÖR DOKÜMANI — ENTRY PAUSE (canary_control.py)

**Sürüm:** BLOK30 (2026-11-25) · **Araç:** `canary_control.py` ·
**Canlı DB:** `/var/lib/futures-canary/canary_live.db` (varsayılan; `$CANARY_DB`
ile veya `--db` ile değiştirilebilir)

## Ne yapar, ne yapmaz

ENTRY PAUSE, **yalnızca yeni giriş (pozisyon açma) emirlerini** durduran,
kalıcı ve operatör kontrollü bir bayraktır. Global HALT mekanizmasından
**tamamen bağımsızdır** ve onu yeniden kullanmaz.

**PAUSE açıkken ÇALIŞMAYA DEVAM EDENLER (testlerle kanıtlı):**
- 5 saniyelik Mark Price risk döngüsü (`risk_tick`)
- `rm.enforce()`; pozisyon başına **−5 USDT** → SAFE_EXIT (reduce-only çıkış)
- toplam **−10 USDT** → global HALT zinciri (PAUSE bunu BASTIRMAZ/GECİKTİRMEZ)
- mevcut açık pozisyonların yönetimi (PAUSE yalnız başına pozisyon KAPATMAZ)
- STOP_MARKET koruma emirleri: iptal edilmez; doğrulama/kaybolursa yeniden
  yerleştirme döngüsü çalışır
- H1 ters sinyalin risk azaltan ÇIKIŞ bacağı (flip çıkışları)
- funding senkronizasyonu, drift/mutabakat kontrolleri
- HALT state machine (hiçbir değişiklik yok)

**PAUSE açıkken ENGELLENEN tek şey:** yeni giriş MARKET emirlerinin
gönderilmesi.

## Komutlar

### Durum görüntüleme (salt-okunur)

```bash
python canary_control.py status
# veya açık yol ile:
python canary_control.py status --db /var/lib/futures-canary/canary_live.db
```

Örnek çıktı (gerçek koşudan):

```
DB                       : /var/lib/futures-canary/canary_live.db (exists=True)
entry-gate kilit dosyası : /var/lib/futures-canary/canary_live.db.entry-gate.lock  ← uygulama ve bu araç aynı kilidi paylaşır
entry_paused             : 0
halt_state (global HALT) : CANARY_ACTIVE
rm_status (risk yönetici): ACTIVE
worker_pid (çalışan uygulama): 2328754
bekleyen giriş emirleri  : 0 (INTENT/SUBMITTED/UNKNOWN)
RESULT: OK (salt-okunur — borsa API'si çağrılmadı)
```

`status` **hiçbir şey yazmaz** (DB `mode=ro` ile açılır) ve **borsa emir/iptal
API'sini çağırmaz**. PAUSE durumunu, global HALT durumunu, risk yöneticisi
durumunu ve bekleyen/sonucu bilinmeyen giriş emirlerini ayrı ayrı gösterir.
`worker_pid`, çalışan canary sürecinin PID'idir — bu DB'yi okuyan uygulamanın
kimliğini kanıtlar.

### Yeni girişleri durdurma

```bash
python canary_control.py pause --reason "Operatör kontrollü yeni giriş duraklatması"
```

Örnek çıktı:

```
PAUSE isteği: yeni girişler durdurulacak. Mevcut pozisyonlar, koruma emirleri, risk döngüsü ve çıkışlar DOKUNULMAZ.
önceki durum             : entry_paused=0
yazıldı                  : entry_paused=1 @ 2026-11-25 00:58:08 UTC by=cli:pid=455875@FURKAN
audit                    : risk_events → ENTRY_PAUSED (reason='Operatör kontrollü yeni giriş duraklatması')
RESULT: OK — yeni giriş MARKET emirleri artık engellendi (kalıcı; restart'a dayanıklı). Resume: python canary_control.py resume --confirm
```

Davranış notları:
- O anda bir giriş emri **gönderim aşamasındaysa**, `pause` komutu o isteğin
  **sonucunu bekler** (ortak `entry-gate` flock kilidi); sonuçlanmadan
  "etkinleşti" bildirmez. Bekleyen zincir 120 sn içinde bitmezse
  `RESULT: FAILED ... (durum DEĞİŞMEDİ)` ile çıkar (çıkış kodu 2) — tekrar
  deneyin.
- Bekleyen/`UNKNOWN` giriş emri varsa pause ÖNCE uyarı ile listeler. Pause
  bunları **iptal etmez** — çözümleri mevcut risk döngüsündeki izleme
  mekanizmasına bırakır.
- Pause, mevcut pozisyonları kapatmaz, koruma emirlerini iptal etmez, risk
  döngüsünü durdurmaz.
- Durum DB'ye kalıcı yazılır: servis **yeniden başlasa bile girişler PAUSED
  kalır**.
- PAUSE sırasında engellenen sinyal resume sonrası **otomatik oynatılmaz**
  (sinyal dedup kaydı korunur); yalnız gelecekteki yeni ve geçerli sinyaller
  değerlendirilir.

### Devam ettirme (resume)

```bash
python canary_control.py resume --confirm
# neden de kaydedilebilir:
python canary_control.py resume --confirm --reason "operatör izin verdi"
```

- `--confirm` **zorunludur**; onsuz deneme çıkış kodu 3 ile reddedilir ve
  durum değişmez.
- Resume **yalnızca PAUSE bayrağını kaldırır**: global HALT sıfırlanmaz,
  `operator_reset()`/`operator_release()` çağrılmaz, risk limitleri ve diğer
  güvenlik blokları (HALT, mutabakat, feed, koruma, sahiplik...) değişmez.
  Bu bloklardan herhangi biri aktifse yeni giriş yine engellenir — bilinçli
  tasarımdır.

```
RESUME isteği: yalnızca ENTRY PAUSE bayrağı kaldırılacak. Global HALT sıfırlanMAZ; operator_reset/operator_release çağrılMAZ; diğer güvenlik blokları geçerliliğini korur.
önceki durum             : entry_paused=1
yazıldı                  : entry_paused=0 @ 2026-11-25 00:58:08 UTC by=cli:pid=455878@FURKAN
audit                    : risk_events → ENTRY_RESUMED
RESULT: OK — yeni girişler normal güvenlik kontrollerine tabi olarak değerlendirilecek.
```

## Çıkış kodları

| Kod | Anlam |
|---|---|
| 0 | Başarı |
| 2 | Giriş kapısı kilidi timeout — devam eden giriş zinciri var; durum DEĞİŞMEDİ, tekrar deneyin |
| 3 | Kullanım hatası (ör. `resume` without `--confirm`) |

## Audit

Her PAUSE/RESUME, `risk_events` tablosuna zaman damgasıyla
(`ENTRY_PAUSED` / `ENTRY_RESUMED`) ve `live_state` tablosuna
`entry_pause_ts` / `entry_pause_by` / `entry_pause_reason` /
`entry_pause_last_action` anahtarlarıyla yazılır. `status` çıktısı bu
bilgilerin son halini gösterir.

## Eşzamanlılık güvencesi (özet)

Uygulama süreci ve CLI ayrı OS süreçleridir; iki taraf da aynı
`<db>.entry-gate.lock` dosyasında `fcntl.flock` tutar. Giriş zinciri
(INTENT kaydı + Binance MARKET emri + sonuç yazımı) bu kilidi tutarken
işler; pause/resume yazımı da kilidi almadan durum değiştirmez. Böylece
"kontrol geçildi, emir gönderiliyor" anında pause açılsa bile gönderilen
emirden SONRA kalıcı olur ve bir sonraki emir kesinlikle gönderilmez.
Gerçek ayrı süreçlerle yapılan yarış testi:
`tests/test_blok30_entry_pause.py::TestRaceConditions::test_29_cross_process_race_real_os_processes`.
