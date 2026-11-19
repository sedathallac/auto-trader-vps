# C.1 — 418/−1003 IP-BAN kronolojisi (kaynak: journalctl + denetim mtime'ları + ban_escalation_log.txt)

Dönüşüm: journalctl +03:00 (TRT); audit log mtime'ları yerel +03; escalation
log UTC. Tablodaki TÜM saatler **UTC**'ye çevrilmiştir (−3h).

| UTC | Olay | Kaynak |
|---|---|---|
| 18-Nov 22:18:53 | audit discovery.ndjson (discovery çağrıları) | mtime |
| 18-Nov 22:34:49 | audit income.ndjson (89-gün income taraması) | mtime |
| 19-Nov 01:22:43 | audit snapshot.ndjson | mtime |
| 19-Nov 01:27:12–18 | audit ledger.ndjson | mtime |
| 19-Nov **01:27:22** | audit **asyn_list** (trade/asyn+income/asyn keşfi) | mtime |
| 19-Nov **01:27:29** | BOTA İLK −1003 (positionRisk) — **7 sn sonra** | journalctl |
| 19-Nov 01:34:49 | audit income denemesi son | mtime |
| 19-Nov 01:39:07 | paper equity sentinel 0.0 başlar (get_balance hata→0.0) | equity CSV |
| 19-Nov 02:35:02 | live sentinel (canlı birim) | equity CSV |
| 19-Nov 05:45–46 | audit asyn_list **r2** gözlemi → INCOME/ASYN ban → bitiş **07:03:12Z** | mtime + escal. log |
| 19-Nov 07:17 | audit asyn_list **r3** → TRADE/ASYN ban → bitiş **07:23:47Z** | mtime + escal. log |
| 19-Nov 09:48–50 | audit asyn_list **r4** → INCOME/ASYN ban → bitiş **11:30:08Z** (eskale ~1s42m) | mtime + escal. log |
| 19-Nov 10:38:26 (=14:38:26+03) | bot sentinel son 0.0; ilk başarılı okuma | equity CSV |
| 19-Nov ~10:51 | signal_outcomes/whale collector'ları (ban sonrası, temiz) | mtime |

**Yasağı kim tetikledi: denetim araçları.** Kanıt zinciri:
1. İlk −1003, asyn_list collector'ının bitişinden **7 saniye** sonra.
2. Sonraki üç ban tam olarak üç asyn_list gözlemiyle (r2/r3/r4) başlar;
   escalation log'da kendi kaydimiz: "benim r4 gözlemim tetiklemiş olabilir".
3. Collector VPS IP'sinden (179.61.207.234) ve **canlı botun anahtarıyla**
   (/proc/<pid>/environ) koştu → ağırlık havuzu ve IP ortak; ban botları vurdu.
4. Ban boyunca botun tüm private çağrıları −1003 aldı; get_balance 0.0
   döndürdü → F25 halt (bkz. equity_series_halt_20261119.csv).

Botların kendi isteğiyle yediği başka bir ban kanıtı YOK (ban öncesi
bot loglarında −1003 yok). Mağdur: botlar; tetikleyen: audit.
