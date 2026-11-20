# evidence/ — TARİHİ DOĞRULAMA SCRIPTLERİ (pytest kapsamı DIŞINDA)

BLOK10 (§6) kapsamında kök dizinden buraya taşındılar. Üçü de FAZ14/FAZ16
döneminin **tek seferlik VPS doğrulama kanıtlarıdır**; üretime veya bakım
testlerine etkileri YOKTUR ve bugünkü ağaçta koşulamazlar:

| Dosya | Dönem | Neden artık koşulamaz | Güncel muadili |
|---|---|---|---|
| `test_fixes.py` | FAZ14 C1 | `RISK_CONFIG['max_notional'] == 25.0` assert'i FAZ14h'de **bilinçli kaldırılan** sabit 25-USDT tavanını doğrular (auto_trader.py:240 "sabit USDT tavanı kaldırıldı; bağlayıcı tavan equity %20"); ayrıca `/root/.risk_engine_state.json` (JSON-era) okur | `tests/test_risk_engine_integrity.py`, `tests/test_faz14x_risk_daily.py` (halt kalıcılığı + günlük sayaçlar) |
| `test_c3.py` | FAZ16 C3 | `/root/risk_engine.py.c3.new` (göç-öncesi yedek modül) ve `/root` yazma izni ister; göç tamamlandı, dosya artık yok | `tests/test_faz16*`, `tests/test_risk_engine_integrity.py` (SQLite risk defteri) |
| `test_c6.py` | FAZ14 C6 | `auto_trader`'a hiç dokunmaz; eski C6 slot-Sayım döngüsünü **kendi elle taklit eder** (kanıt değeri simülasyondur) | `tests/test_faz15_c6_integrity.py`, `tests/test_faz14v_slots.py` (gerçek open_coin + atomik rezervasyon) |

Bakım altındaki tek test paketi: `tests/` (bkz. `pytest tests/`).
Kök `pytest` artık bu üç dosyayı TOPLAMAZ; `deploy/faz9_gridc0_deploy.sh`
içindeki `--ignore=test_c3.py --ignore=test_fixes.py --ignore=test_c6.py`
bayrakları zararsız no-op'tur (yol yoksa --ignore sessizdir).

Hiçbir test SİLİNMEDİ veya "geçirilmiş" GÖSTERİLMEDİ; dosyalar `git mv` ile
tarihçe korunarak arşivlendi (BLOK10 dürüstlük kuralı).
