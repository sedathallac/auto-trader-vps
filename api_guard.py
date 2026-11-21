#!/usr/bin/env python3
"""BLOK12 §4 — Binance rate-limit gardı (429/418 cooldown/backoff).

Durum makinesi (process-içi, restart'ta sıfırlanır — muhasebe değil, AKIŞ
gardıdır):
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

import threading
import time

DEFAULT_429_S = 60.0
DEFAULT_418_S = 900.0
MAX_COOLDOWN_S = 3600.0

_LOCK = threading.Lock()
_STATE = {"cooldown_until": 0.0, "reason": None, "strikes": 0}


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
        return delay


def blocked(now=None):
    """Cooldown sürüyorsa nedeni, değilse None."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        if _STATE["cooldown_until"] > now:
            return _STATE["reason"] or "rate-limit-cooldown"
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
