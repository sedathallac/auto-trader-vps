# BOT_KNOWLEDGE_BASE — Teknik Hafıza

Bu depo, trading altyapısının kalıcı teknik hafızasıdır. **Her oturumda sistemi
sıfırdan keşfetme — önce buradaki dokümanları oku, sonra yeni bulgularla güncelle.**

## Dizin
- `findings/` — kanıta dayalı olay incelemeleri (forensic).
  - `2026-11-11_sol_double_close.md` — SOLUSDT çift-kapanış / ters SHORT olayı.
- `design/` — henüz uygulanmamış mimari tasarımlar ve test planları.
  - `safe_close_architecture.md` — tek kapanış otoritesi, çift-SL koordinasyonu.
  - `archiver_algo_order_model.md` — algo/koşullu emir geçmişi arşiv modeli.
- `architecture/` — sistem haritaları.
  - `system_overview.md` — üç bileşen: Futures (auto_trader), Spot Grid (GridC0), Hummingbot.
- `EVIDENCE/tools/` — salt okunur inceleme araçları (`kb_*.py`).

## Değişmez güvenlik kuralları (bu oturum serisinde dayatıldı)
- Emir açma/iptal YOK. Pozisyon kapatma YOK. SL ekleme/değiştirme YOK.
- Servis restart YOK. Kaynak kodu değiştirme YOK. Veritabanına yazma YOK.
- Reconciliation drift kontrolünü atlatma YOK.
- Futures botu şu an **yeni işlem açmamalı**; drift kontrolü devrede kalmalı.
- Yalnızca `EVIDENCE/tools/kb_*.py` altında, salt okunur araçlarla inceleme yapılır.

## Açık güvenlik notları (düzeltme bekliyor, uygulama yok)
- `auto_trader.py` `run_once`: HB kullanıcı/parolası varsayılan olarak düz metin.
- `kill_spot_grid.py:10`: HB admin parolası düz metin. Rotasyon gerekli.
- Her iki bot aynı `BINANCE_KEY/SECRET` ortam değişkenlerini kullanıyor.

## Durum (2026-11-11)
- SOLUSDT: arşivde −0.3 SHORT kalıntısı. **Canlı borsa durumu teyit edilmedi**
  (imzalı Binance sorgusu bu ortamda mümkün değil). Panelden doğrulanmalı.
- PostgreSQL (hummingbot_api) salt okunur erişimi: driver yok → okunamıyor.
