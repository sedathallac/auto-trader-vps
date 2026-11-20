# Deploy ADAY DALI — fix/deploy-candidate-security-20261120 (UYGULAMA YOK)

Taban: üretim `108002c`. Dal yalnız GÜVENLIK SETI; arastirma kodu
(faz3*/faz4*/analiz betikleri, orphan_report araci) HARIC.
Suite: 1063 passed + 9 subtests (tam tests/, bu agac). Diff: 19 dosya,
+1452/−43.

## SHA esleme tablosu (TBD YOK)
| Adim | Madde | Kaynak SHA | Dal-üstü SHA | Not |
|---|---|---|---|---|
| 1 | F25 sentinel fail-safe + oz-test + M-2 | `7f84149` | `865d2e3` | health17 test-dosyasi DU cakismasi DUSURULDU (üretim tabaninda yok; Health17 merge YASAK) |
| 2 | C.2 get_balance→None | `0857650` | `e4a6dd7` | kaynak-sentinel; F25 streak derinlemesine korunur |
| 3 | A-10 ATR=0 tanimi + kayip-logu | `f7c99d4` | `3538fad` | SE + guard testi |
| 4 | A-10 sertlestirme: mutlak giris vetosu (BLOK4 IS3.2) | `1a62f3f` | `76b06bb` | fail-closed veto; backtest D-4 hiza |
| 5 | A-2 + A-3 + A-9 (tek commit, ATOMIK) | `1971059` | `dfe25a5` | eksen: A-2 close_adapter _SL_LIVE PARTIALLY_FILLED; A-3 find_algo/re-map watchdog; A-9 conditional_margin_gate (%20 buffer, fail-closed). C.4'teki "üretimde zaten var" VARSAYIMI YANLISCIKI: 1971059 üretimde YOK — icin dalda. |

Düzeltme (C.4'e not): C.4 tablosundaki A-2/A-3/A-9 "production ancestor"
iddiasi 2026-11-20'de IÇERIK-ARAMAYLA refute edildi (`git grep
find_algo_by_client_id 108002c` → bos; `merge-base --is-ancestor 1971059
108002c` → false). Ucte uca deploy sirasi degismez; adim-5 tek commit'tir.

## Deploy sirasi (her adim AYRI deploy; insan onayi + C.4 kontrolleri)
ONEKOSUL: operatör onayi; C.4 "her adim oncesi" (orphan_report yalniz-oku
tek canli-okuma, DB/config salt-kopya yedegi, rev-parse dogrulamasI);
her adim sonrasi smoke (F25 oz-testi satiri, is_halted=1 KALIR, −1003/418 yok).

Adim sirasi (C.4 BAGLAYICI: F25 → A-2 → A-3 → A-9 → A-10):
  1) F25 `865d2e3` + C.2 `e4a6dd7` (C.4 F25 satiri C.2'yi bu adima baglar)
  2) A-2+A-3+A-9 `dfe25a5` (ATOMIK — uc madde tek commit, ayrilmaz)
  3) A-10 `3538fad` + veto `76b06bb` (A-10 adiminin iki parcasI)
Commit sirasi ile deploy sirasi FARKLIDIR (dalda veto erken gelir; deploy'da
A-10 son adimdir — C.4'ten sapma yok). Her adim ayri release/servis starti.

## Tek-komut rollback (her adimdan sonra gecerli)
    sudo systemctl stop auto-trader_faz9_live && \
    sudo ln -sfn /opt/auto_trader_faz9.releases/<ONCEKI-REL> /opt/auto_trader_faz9 && \
    sudo systemctl start auto-trader_faz9_live
Rollback tetikleyicileri (C.4): oz-test FAIL, is_halted degisimi, 418, yeni
equity sentinel'i. HALTED korundugu icin pazarlik gerekmez.
<ONCEKI-REL> her adim ONCESI not edilir; symlink yoksa `git -C
/opt/auto_trader_faz9/app checkout <onceki-sha>` + servis restart (operatör
kurulumuna gore C.4'te TAM yaziyla sabitlenir).

## Bu belgenin kapsami
Deploy YOK, servis dokunusu YOK, canli hesap istegi YOK. Dal push'landi;
merge/merge-değil kararı ve sıfır-ATR vetosunun canlı davranışa etkisi
operatör/onayinda. is_halted'a DOKUNULMADI (HALTED devam).
