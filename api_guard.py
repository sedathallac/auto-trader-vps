#!/usr/bin/env python3
"""BLOK12 §4 — Binance rate-limit gardı (429/418 cooldown/backoff).

Durum makinesi (bellek ∨ STATE_DIR/api_guard.json — muhasebe değil, AKIŞ
gardıdır; devam-F §3: dosya katmanı restart ve watchdog süreçlerinin ban
sırasında istek göndermesini engeller):
  * 429 → Retry-After başlığı varsa O, yoksa DEFAULT_429_S; artan grevlerle
    üstel backoff (tavan MAX_COOLDOWN_S).
  * 418 (IP ban) → DEFAULT_418_S (uzun) cooldown — asla kör deneme yok.
  * Cooldown süresince `blocked()` neden döner → çağıran YENİ GİRİŞİ
    fail-closed kapatır. Salt-okuma yoluna dokunmaz; Emir POST'u zaten
    urllib3 tarafından retry EDİLMEZ (allowed_methods=["GET"]) — bu modül
    POST'u asla yeniden göndermez, yalnız yeni giriş kapısını kapatır.
  * UNKNOWN emir durumu bu modülün kapsamı dışındadır ve KORUNUR (bilerek
    hiçbir şey iptal/tekrar etmeyiz).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

DEFAULT_429_S = 60.0
DEFAULT_418_S = 900.0
MAX_COOLDOWN_S = 3600.0

_LOCK = threading.Lock()
_STATE = {"cooldown_until": 0.0, "reason": None, "strikes": 0}

# BLOK12 devam-F §3 (E2): cooldown'un SÜREÇ-DESTİ kanıtı — yalnız bitiş
# zamanı + neden YAZILIR (sır taşımaz). Amaç: (a) restart'ın ban'ı
# sıfırlamasını, (b) ayrı süreçteki watchdog'un ban sırasında istek
# göndermesini engellemek. Akış gardıdır, muhasebe DEĞİL: bozuk/eksik dosya
# bloklama üretmez (fail-open), bellek-içi ban ise dosyadan bağımsız sürer.
_STATE_FILE = None


def set_state_file(path) -> None:
    """Cool down dosyası yolu (None → dosya katmanı kapalı, eski davranış)."""
    global _STATE_FILE
    with _LOCK:
        _STATE_FILE = str(path) if path else None


def file_blocked(path, now=None):
    """Dosyada aktif cooldown varsa nedeni döner; yok/bozuk/süresi geçmiş →
    None. Salt-okunur, istisna fırlatmaz."""
    now = time.time() if now is None else float(now)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        until = float(data.get("cooldown_until") or 0.0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if until > now:
        return (f"{data.get('reason') or 'rate-limit-cooldown'}"
                f"(dosya:{until - now:.0f}s kaldı)")
    return None


def _persist_locked(now) -> None:
    """_LOCK altında çağrılır; best-effort atomik yazım (tmp+rename)."""
    if not _STATE_FILE:
        return
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".api_guard_", dir=os.path.dirname(
            _STATE_FILE) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"cooldown_until": _STATE["cooldown_until"],
                       "reason": _STATE["reason"]}, fh)
        os.replace(tmp, _STATE_FILE)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _retry_after(headers) -> float | None:
    if not headers:
        return None
    try:
        v = headers.get("Retry-After", headers.get("retry-after"))
    except AttributeError:
        return None
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, f)


def note_http_status(status_code, headers=None, now=None) -> float:
    """Her Binance REST yanıtından sonra çağrılır. 429/418 hariç no-op.
    Dönen: uygulanan cooldown saniyesi (0.0 ise bir şey yapılmadı)."""
    if status_code not in (429, 418):
        return 0.0
    now = time.time() if now is None else float(now)
    ra = _retry_after(headers)
    with _LOCK:
        if status_code == 418:
            _STATE["strikes"] += 1
            base = ra if ra is not None else DEFAULT_418_S
            delay = min(base, MAX_COOLDOWN_S)
            reason = "418-ip-ban"
        else:
            _STATE["strikes"] += 1
            base = ra if ra is not None else DEFAULT_429_S
            delay = min(base * (2 ** (_STATE["strikes"] - 1)), MAX_COOLDOWN_S)
            reason = f"429-rate-limit(strikes={_STATE['strikes']})"
        # Kalıcı tavan: kalan cooldown HİÇBİR zaman MAX_COOLDOWN_S'u aşamaz —
        # grevler birikerek süreyi sonsuza uzatamaz (yalnız uzatır, çakmaz).
        _STATE["cooldown_until"] = max(_STATE["cooldown_until"], now + delay)
        _STATE["reason"] = reason
        _persist_locked(now)
        return delay


def blocked(now=None):
    """Cooldown sürüyorsa nedeni, değilse None.

    BLOK12 devam-F §3: bellek ∨ dosya — yeniden başlayan süreç (bellek boş)
    ve dosya yolunu bilen ayrı süreçler ban'ı GÖRÜR. Dosya okuma kilidi
    dışarıda tutuldu (kısa, atomik rename ile tutarlı)."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        if _STATE["cooldown_until"] > now:
            return _STATE["reason"] or "rate-limit-cooldown"
        fpath = _STATE_FILE
    if fpath:
        return file_blocked(fpath, now)
    return None


def cooldown_remaining(now=None) -> float:
    now = time.time() if now is None else float(now)
    with _LOCK:
        return max(0.0, _STATE["cooldown_until"] - now)


def success(now=None) -> None:
    """Başarılı yanıt grevleri sıfırlar (cooldown sürüyorsa dokunmaz)."""
    with _LOCK:
        _STATE["strikes"] = 0


def reset() -> None:
    with _LOCK:
        _STATE.update(cooldown_until=0.0, reason=None, strikes=0)
