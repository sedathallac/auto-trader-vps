# BLOK12 §6 — Yetim-SL Watchdog: parite doğrulama + operatör kurulum planı (2026-11-21)

**Bu belge kurulum YAPMAZ.** BLOK12 görevinde üretime birim kurulmadı, timer
enable edilmedi, canlı Binance isteği atılmadı. Kurulum adımlarının tamamı
**OPERATÖR ONAYI GEREKTİRİR**.

## 1) Makine-parite doğrulaması (yalnız-okunur, kurumsuz)

```bash
cd /opt/auto_trader_faz9/app   # ya da aday worktree
/opt/auto_trader_faz9/venv/bin/python -I tools/verify_watchdog.py
# EXIT 0 = PASS (WARN olabilir) · 1 = FAIL (kurulumdan ÖNCE kapatılmalı) · 2 = doğrulanamaz
```

Kontrol sınıfları (bkz. `tools/verify_watchdog.py`):
- `parite:User|WorkingDirectory|HOME|EnvironmentFile|python-yolu` — örnek
  watchdog birimi ile canlı `auto_trader_faz9_live.service` birebir aynı
  kimliği/çevreyi görmeli. **Farklı `HOME` = TRACKED/HEARTBEAT körlüğü**
  (watchdog çalışır ama hiçbir yetim görmez; sessiz hata sınıfı).
- `env:STATE_DIR-tuzağı` — `EnvironmentFile` içinde `FAZ9_STATE_DIR` AYARLIYSA
  kırmızı: `orphan_killer.py` dosyaları `$HOME`'da arar, bot `$FAZ9_STATE_DIR`'e
  yazar → körlük. (Bugün `/etc/auto_trader/live.env` bu değişkeni taşımıyor;
  taşımamalı.)
- `kilit:*` — systemd `--lock /run/lock/orphan_watchdog_faz9_live.lock`;
  **cron örneğinin kilidi AYRI dosyadır** (`...faz9.cron.lock`): ikisini
  birlikte kurmayın, birini seçin.
- `sarmalayici:*` — `tools/orphan_watchdog.py` emir yerleştirme tokenları
  taşımaz; anahtar kontrolü kilit/isteden ÖNCE; exit sözleşmesi 0/1/2.
- `av:*` — `orphan_killer.py` gerçekten `positionRisk` okur ve iptal yanıtında
  `code==200` teydi ister (200 değilse tracked kalır; sonraki tur dener).

Ayrıca sözleşme testleri: `tests/test_blok11_watchdog.py` (sarmalayıcıexit
kodları, kilit, anahtar kapısı, emir-yasak) ve
`tests/test_blok12_watchdog_verify.py` (doğrulayıcının kendisi; yalnız-okunur
olduğu hash kanıtıyla test edilir).

## 2) Çıkış kodları (tek doğruluk kaynağı: sarmalayıcı sabitleri)

| Kod | Anlam | Kim okur |
|---|---|---|
| 0 | Tur tamam (yetim bulunmuş ve **200 ile** kapatılmış olabilir) | timer/journal |
| 1 | Scan istisnası — sonraki tur denenir; NextElapse korunur | journal |
| 2 | Kilit meşgul VEYA BINANCE_KEY/SECRET yok → **tek istek bile yapılmadı** | journal (göz ardı etme!) |

Önemli: bir yetmin **kapatılamaması** (HTTP 200 değil/istisna) süreç kodunu
değiştirmez (exit 0 + WARNING log). Kapanış başarısı EXIT'TEN DEĞİL,
journal/`$HOME/orphan_killer.log` satırlarından izlenir.

## 3) KURULUM — OPERATÖR ONAYI GEREKTİRİR (bu görevde YAPILMADI)

Ön koşul sırası (hepsi operatör tarafından, canlı servise dokunmadan):
1. `tools/verify_watchdog.py` exit 0 (FAIL varsa önce kapat).
2. `/etc/auto_trader/live.env` denetimi: `FAZ9_STATE_DIR` YOK; anahtarlar var.
3. Karar: systemd timer **veya** cron (ikisi birden YOK — kilitler ayrı).
4. Birimlerin kurulması (yalnız `.example` → `/etc/systemd/system`):
   ```bash
   sudo cp deploy/orphan_watchdog_faz9_live.service.example \
        /etc/systemd/system/orphan_watchdog_faz9_live.service
   sudo cp deploy/orphan_watchdog_faz9_live.timer.example \
        /etc/systemd/system/orphan_watchdog_faz9_live.timer
   sudo systemctl daemon-reload
   sudo systemctl enable --now orphan_watchdog_faz9_live.timer
   ```
5. Doğrulama (salt-okunur):
   ```bash
   systemctl list-timers | grep orphan
   journalctl -u orphan_watchdog_faz9_live -n 20   # exit 2/1 analizi burada
   ```
6. **GERÇEK API'li tek-tur denemesi (`--once`) ayrı operatör onayı ister** —
   borsa anahtarlarıyla gerçek istek atar; emir göndermez ama iptal
   yapabilir (yetim SL avı doğası gereği).

## 4) Bilinen eksikler / tuzaklar (dürüstlük payı)

- `docs/OPS_C3_C4.md:37` `tools/orphan_report.py`'dan söz eder; **bu dosya
  ağaçta YOK** — o komutu çalıştırmaya çalışmayın. Salt-durum raporu için
  `verify_watchdog.py` + journal kullanın.
- Timer'da `Persistent=` yok (varsayılan false): makine kapalıyken tur
  KAÇAR, açılınca telafi koşusu YOK — 1 dakikalık periyotta kabul edilebilir;
  istenirse operatör `Persistent=true` ekleyebilir (yalnız watchdog, bot
  birimine dokunmayın).
- `ExecStopPost`/`TimeoutStopSec` ne watchdog'ta ne canlı birimde tanımlı —
  servis seviyesinde kapanış garantisi YOK (BLOK12 §8 kapsamı dışında).
- Watchdog'un kendisi `ProtectHome/ProtectSystem` taşımıyor; canlı birimin
  sertleştirmesi kopyalanacaksa `ReadWritePaths` `/var/lib/auto_trader_faz9_live`
  + `/run/lock`'u kapsamalı, yoksa flock/kayıt yazımı kırılır.
- Kalıcı `HOME` bağı: timer `Environment=HOME` birim içinden gelir; `/root`
  veya başka HOME ile elle `--once` koşmak FARKLI tracked kümesi görür.
