"""Test isolation for the K-3 cross-process SL-placement ledger.

ensure_sl_coverage uses a shared SQLite store (auto_trader.SL_LEDGER_DB, default
~/risk_ledger.db) for the SL-placement intent. Without isolation that real file would
leak intent state between tests (a PLACED intent -> BUSY -> placement skipped). This
autouse fixture points SL_LEDGER_DB at a fresh per-test temp db.

FAZ14w: aynı sorun tüm durum dosyalarında vardı. VPS'te pytest çalıştırmak
/root/.pending_fills.json içine test artığı (E1/SOLUSDT) yazıyor, canlı servis de o
kaydı "bekleyen dolum" sanıp slot işgal ediyordu. Artık FAZ9_STATE_DIR import'tan
ÖNCE geçici bir dizine ayarlanır; hiçbir test gerçek HOME durum dosyasına dokunamaz.
"""
import os
import sys
import tempfile

import pytest

# auto_trader import edilmeden ÖNCE: STATE_DIR modül yüklemesinde hesaplanır.
os.environ["FAZ9_STATE_DIR"] = tempfile.mkdtemp(prefix="faz9-test-state-")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader


@pytest.fixture(autouse=True)
def _isolate_validation_budget(tmp_path, monkeypatch):
    """BLOK11 §10.3: giriş artık bütçe KAPALI iken de fail-closed RED. Canlı
    open_coin yollarının testleri bundan böyle bütçe AÇIK + geçerli varsayılan
    limitlerle (2/50/100) ve HER TESTE TAZE bir SQLite deposuyla koşar.
    Modül-seviyesi tembel önbellek (_VB_CFG/_VB_BUDGET) testler arası sızmasın
    diye hem önce hem sonra sıfırlanır. Kapının kendisini test eden dosyalar
    monkeypatch ile kendi env'lerini kurar (test_blok11_budget_failclosed)."""
    monkeypatch.setenv("CONTROLLED_LIVE_VALIDATION", "true")
    monkeypatch.setenv("VALIDATION_BUDGET_DB",
                       str(tmp_path / "validation_budget.db"))
    auto_trader._VB_CFG = None
    auto_trader._VB_BUDGET = None
    try:
        yield
    finally:
        auto_trader._VB_CFG = None
        auto_trader._VB_BUDGET = None


@pytest.fixture(autouse=True)
def _isolate_entry_health():
    """BLOK12 §4: _ENTRY_HEALTH süreç-içi MODÜL sözlüğüdür; §2'nin
    validation_fill_cap kapısı (doluş tavanı aşımı) bir testte kurulursa
    sonraki testlerin merkezi risk kapısını SESSİZCE kapatıyordu (sıralamaya
    bağımlı kırmızılar). Diğer durum yalıtımlarıyla aynı desen: önce temizle,
    sonra geri yükle. Üretim davranışı değişmez — gerçek süreçte kapı
    yaşam döngüsü aynıdır; yalnız test izolasyonudur."""
    saved = dict(auto_trader._ENTRY_HEALTH)
    auto_trader._ENTRY_HEALTH.clear()
    try:
        yield
    finally:
        auto_trader._ENTRY_HEALTH.clear()
        auto_trader._ENTRY_HEALTH.update(saved)


@pytest.fixture(autouse=True)
def _isolate_api_guard():
    """BLOK12 devam-A: _get/_post artık api_guard cooldown'una bakıyor — bir
    test gerçek saatli cooldown bırakırsa SONRAKİ testlerin sahte transport'u
    susturulurdu (sessiz toplu kırmızı). Durumu testten önce temizle, sonra
    geri yükle."""
    import api_guard
    saved = dict(api_guard._STATE)
    saved_file = api_guard._STATE_FILE
    api_guard.reset()
    _wipe_guard_file()               # süreç-desti kanıt dosyası da izole
    try:
        yield
    finally:
        api_guard.reset()
        api_guard._STATE.update(saved)
        api_guard.set_state_file(saved_file)
        _wipe_guard_file()


def _wipe_guard_file():
    """devam-F §3: api_guard cooldown dosyası oturum-GENİŞİ tmp STATE_DIR'de
    yaşar; bir testin bıraktığı 418 kaydı SONRAKİ testlerin sahte transport'unu
    susturmasın diye önce/sonra silinir (bellek izolasyonunun dosya versiyonu).
    """
    try:
        os.unlink(auto_trader.API_GUARD_STATE_FILE)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _isolate_shutdown_flag():
    """BLOK12 §8: _SHUTTING_DOWN süreç-içi bayraktır; bir §8 testi bayrağı
    True bırakırsa SONRAKİ tüm canlı-yol testleri open_coin kapısından
    geçemezdi (sıralamaya bağımlı toplu kırmızı). Temizle-koşut-geri yükle."""
    saved = list(auto_trader._SHUTTING_DOWN)
    auto_trader._SHUTTING_DOWN[0] = False
    try:
        yield
    finally:
        auto_trader._SHUTTING_DOWN[:] = saved


@pytest.fixture(autouse=True)
def _isolate_sl_ledger(tmp_path):
    _orig = auto_trader.SL_LEDGER_DB
    auto_trader.SL_LEDGER_DB = str(tmp_path / "risk_ledger.db")
    try:
        yield
    finally:
        auto_trader.SL_LEDGER_DB = _orig


@pytest.fixture(autouse=True)
def _isolate_pending_store(tmp_path):
    """FAZ15b v5: her test taze ve TUTARLI bir pending store ile başlar.

    Transaction artık mutator'dan önce diski okuyup bellekle uzlaştırıyor. Bir test
    belleği (`_PENDING_FILLS.clear()`) diskteki kayıtla uyumsuz bırakırsa üretim
    kuralı gereği MALFORMED_STATE oluşur — bu doğru davranış, ama testler arası
    sızıntı olur. Bu fixture dosyayı, revision'ı ve sağlık/gate durumunu sıfırlar.
    """
    saved = {
        "path": auto_trader.PENDING_FILLS_JSON,
        "flock": auto_trader.PENDING_FLOCK_PATH,
        "loaded": list(auto_trader._PENDING_LOADED),
        "fills": dict(auto_trader._PENDING_FILLS),
        "rev": auto_trader._PENDING_REVISION[0],
        "healthy": auto_trader._PENDING_STORE_HEALTHY[0],
        "err": auto_trader._PENDING_STORE_ERROR[0],
        "blocked": auto_trader._GLOBAL_ENTRY_BLOCKED[0],
        "reason": auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0],
        "wip": auto_trader._PENDING_WRITE_IN_PROGRESS[0],
    }
    path = tmp_path / "pending_fills.json"
    auto_trader.PENDING_FILLS_JSON = path
    auto_trader.PENDING_FLOCK_PATH = path.with_suffix(".lock")
    auto_trader._PENDING_FILLS = {}
    auto_trader._PENDING_REVISION[0] = 0
    auto_trader._PENDING_LOADED[0] = True
    auto_trader._PENDING_STORE_HEALTHY[0] = True
    auto_trader._PENDING_STORE_ERROR[0] = None
    auto_trader._GLOBAL_ENTRY_BLOCKED[0] = False
    auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = None
    auto_trader._PENDING_WRITE_IN_PROGRESS[0] = False
    auto_trader._PENDING_TX_LOCAL.active = False
    try:
        yield
    finally:
        auto_trader.PENDING_FILLS_JSON = saved["path"]
        auto_trader.PENDING_FLOCK_PATH = saved["flock"]
        auto_trader._PENDING_LOADED[0] = saved["loaded"][0]
        auto_trader._PENDING_FILLS = saved["fills"]
        auto_trader._PENDING_REVISION[0] = saved["rev"]
        auto_trader._PENDING_STORE_HEALTHY[0] = saved["healthy"]
        auto_trader._PENDING_STORE_ERROR[0] = saved["err"]
        auto_trader._GLOBAL_ENTRY_BLOCKED[0] = saved["blocked"]
        auto_trader._GLOBAL_ENTRY_BLOCK_REASON[0] = saved["reason"]
        auto_trader._PENDING_WRITE_IN_PROGRESS[0] = saved["wip"]
        auto_trader._PENDING_TX_LOCAL.active = False
