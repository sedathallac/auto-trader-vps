# BLOK12 §5 — Heartbeat Yönetici Kimliği: TEMİZLEME + ROTASYON PLANI (OPERATÖR)

Tarih: 2026-11-21 · Durum: **kod temiz, ROTASYON YAPILMADI (operatör onayı/işlemi gerektirir)**

## Bulgu (VERIFIED, bu oturumda)

- `kill_spot_grid.py` içinde düz metin `HB_AUTH = (user, pass)` tuple'ı gömülüydü.
- `kill_spot_grid_c0.sh` içinde aynı kullanıcı `HB_USER`/`HB_PASS` kabuk değişkeni gömülüydü.
- **Git geçmişinde yaşıyor:** pass ataması 2 commit (`68d0f59` 2026-11-11,
  `94d97a3` 2026-11-15), `HB_AUTH` dizesi 4 commit. Güncel dosyadan silmek
  geçmişi temizlemez ve kimliği **geçersiz kılmaz** — tek gerçek çözüm rotasyondur.
- Değerler bu rapora/commit'lere/test çıktılarına YAZILMADI; tarayıcı
  `tools/secret_scan.py` yalnız sha256 imzalarını içerir.
- Erişim yüzeyi: `http://localhost:8000` (Hummingbot REST, makine-içi). Yine de
  bu kimlik HB API üzerinden executor durdurma yetkisi verir → acil durum
  sabotajı/yanlış operasyon riski gerçek yetkidir.

## Yeni kod sözleşmesi (uygulandı, testli)

1. `kill_spot_grid.py` → `load_hb_auth()`: kimlik yalnız
   `HB_ADMIN_USER`/`HB_ADMIN_PASS` ortam değişkenlerinden veya
   `HB_ADMIN_ENV_FILE` ile gösterilen **0600** izinli dosyadan okunur.
2. Eksik/boş kimlik → SystemExit(2), HB adımına hiç gidilmez (fail-closed).
3. Değer hiçbir stdout/stderr/log satırına basılmaz (HB yanıt gövdesi de
   basılmıyor — yalnız HTTP durumu).
4. `kill_spot_grid_c0.sh` artık tek uygulama çağrısıdır (py'ye delege).
5. `tools/secret_scan.py` izlenen ağacı tarar; sabit-atama kalıplarını ve
   sızmış kimlik imzalarını bulursa exit 1 (deploy preflight'in parçası).

## Rotasyon İŞ PLANI — tümü OPERATÖR işlemi, bu görevde YAPILMADI

Sıra önemlidir; değerleri sohbete/terminale loglamadan yapın (`read -rs` kullanın):

1. **Yeni kimlik üretin** (rastgele, ≥32 chars): `openssl rand -base64 32`
   çıktısını doğrudan 2. adımdaki dosyaya yönlendirin, ekrana basmayın.
2. **Yeni credential dosyası** (VPS, kök):
   `/etc/auto_trader/heartbeat.env` — içerik:
   `HB_ADMIN_USER=<yeni-kullanıcı>` / `HB_ADMIN_PASS=<yeni-parola>`;
   `chmod 600`, `chown root:root`.
3. **Hummingbot tarafında** yönetim kimliğini yeni değerlerle güncelleyin
   (HB API config/yönetim ekranı — değerleri paylaşmadan).
4. **Eski kimliği devre dışı bırakın** (HB üzerinde eski kullanıcıyı silin veya
   parolasını değiştirin). Eski değer artık hiçbir yerde çalışmamalı.
5. **Doğrulama (read-only):**
   `HB_ADMIN_ENV_FILE=/etc/auto_trader/heartbeat.env python kill_spot_grid.py`
   argümansız çağrı → usage/exit1 (kimlik okundu, emir gitmedi); ardından gerçek
   executor ile `--dry` yoksa tek durdurma denemesi.
6. **Eski env kalıntılarını temizleyin:** VPS'te eski değeri içeren kabuk
   geçmişini (`history -d`/`~/.bash_history`) ve /root/.env benzeri dosyalarda
   kopya varsa silin.
7. **Servis unit'leri** heartbeat kullanan birimlere
   `EnvironmentFile=/etc/auto_trader/heartbeat.env` eklenmesi (operatör,
   restart GEREKTİRİR — bu görevde yapılmaz).

## Kalıcı risk kaydı

Git geçmişi silinmedikçe eski kimlik imzası depoda okunabilir durumda kalır;
imza kamuya açık değildir ancak depo sızıntısı durumunda offline kırma
denenebilir. Bu nedenle 4. adım (eski kimliğin devre dışı bırakılması)
**opsiyonel değildir**. Rotasyon tamamlanmadan aday dal üretim kapısından
geçmemelidir (operatör kabul maddesi).
