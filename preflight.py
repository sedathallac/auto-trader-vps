"""preflight.py — FAZ11 Katman 4: emir öncesi 12 adımlı uçuş öncesi kontrol (SAF).

Her adım bağımsız çalışır, sonucu kaydedilir ve İLK hatada zincir durur
(fail-fast). Hiçbir adım istisna fırlatmaz: beklenmeyen hata `error` olarak
raporlanır ve emir reddedilir (fail-closed).

Adımlar (sıra sabittir, denetim izi bu sıraya dayanır):
   1 config        — canlı yapılandırma geçerli mi
   2 offline_flag  — HB_OFFLINE=0 (canlı) / 1 (compare)
   3 account       — bakiye > 0 ve canTrade
   4 exchange_info — sembol borsada listeli mi
   5 filters       — LOT_SIZE / MIN_NOTIONAL alanları okunabilir mi
   6 quality       — coin kalite skoru ve vetoları
   7 leverage      — kaldıraç tasfiye tavanının altında mı
   8 sizing        — kayıp bütçesi minimumu karşılıyor mu
   9 geometry      — SL/TP net R/R kapısı
  10 risk          — RiskEngine onayı (halt / günlük limit / ardışık zarar)
  11 exposure      — açık pozisyon sayısı ve marjin kullanımı
  12 edge          — kanıtlanmış edge hücresi (yoksa shadow)

Tasarım kuralı: adım sayısı ve adları sabittir; testler bunu doğrular.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STEP_NAMES = (
    "config", "offline_flag", "account", "exchange_info", "filters", "quality",
    "leverage", "sizing", "geometry", "risk", "exposure", "edge",
)


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self):
        return {"step": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class PreflightResult:
    ok: bool
    steps: list = field(default_factory=list)
    failed_at: str | None = None
    reason: str = ""

    def as_dict(self):
        return {"ok": self.ok, "failed_at": self.failed_at, "reason": self.reason,
                "steps": [s.as_dict() for s in self.steps]}

    def step(self, name):
        for s in self.steps:
            if s.name == name:
                return s
        return None


def _run(steps, name, fn):
    """Tek adımı çalıştırır; istisna → ok=False (fail-closed)."""
    try:
        ok, detail = fn()
    except Exception as e:  # adım hatası zinciri durdurur, süreç çökmez
        ok, detail = False, f"error: {type(e).__name__}: {e}"
    steps.append(Step(name, bool(ok), str(detail)))
    return bool(ok)


def pre_flight_check(*, cfg=None, hb_offline=None, account=None, exchange_info=None,
                     symbol=None, filters=None, quality=None, leverage=None, sl_pct=None,
                     size=None, geometry=None, risk=None, exposure=None, edge=None):
    """12 adımı sırayla çalıştırır. Her argüman bir "sağlayıcı"dır:

    - `cfg`: LiveConfig benzeri nesne (`.is_live`, `.validate()`), ya da None (atlanır)
    - `hb_offline`: bool
    - `account`: dict (assets/canTrade) ya da callable
    - `exchange_info`: dict (symbols) ya da callable
    - `symbol`: sembol adı
    - `filters`: dict (LOT_SIZE/MIN_NOTIONAL) ya da callable
    - `quality`: dict (CQ/quality_vetoes) ya da callable
    - `leverage`: int ya da callable
    - `sl_pct`: float
    - `size`: SizeDecision benzeri (`.ok`, `.reason`) ya da callable
    - `geometry`: Geometry benzeri (`.ok`, `.reason`) ya da callable
    - `risk`: (notional|None, reason) ya da callable
    - `exposure`: dict (open_positions/max_positions/margin_usage) ya da callable
    - `edge`: (allow, reason) ya da callable

    Sağlayıcı None ise adım "skipped" olarak GEÇMEZ; `not_provided` ile reddeder.
    Bu bilinçli: eksik bilgiyle emir açmak, emri açmamaktan daha pahalıdır.
    """
    steps = []
    result = PreflightResult(True, steps)

    def _val(x):
        return x() if callable(x) else x

    def _fail(name, detail):
        result.ok = False
        result.failed_at = name
        result.reason = detail
        return False

    # 1 config
    def s_config():
        c = _val(cfg)
        if c is None:
            return False, "not_provided"
        if hasattr(c, "validate"):
            c.validate()
        return True, f"is_live={getattr(c, 'is_live', None)}"

    # 2 offline_flag
    def s_offline():
        hb = _val(hb_offline)
        c = _val(cfg)
        if hb is None:
            return False, "not_provided"
        live = bool(getattr(c, "is_live", False)) if c is not None else False
        want = 0 if live else 1
        if int(hb) != want:
            return False, f"HB_OFFLINE={int(hb)} beklenen={want}"
        return True, f"HB_OFFLINE={int(hb)}"

    # 3 account
    def s_account():
        a = _val(account)
        if not isinstance(a, dict) or "assets" not in a:
            return False, "unusable_payload"
        usdt = 0.0
        for asset in a.get("assets") or []:
            if asset.get("asset") == "USDT":
                usdt = float(asset.get("availableBalance")
                             or asset.get("walletBalance") or 0.0)
                break
        if usdt <= 0:
            return False, f"balance={usdt:.2f}"
        if a.get("canTrade") is False:
            return False, "canTrade=false"
        return True, f"balance={usdt:.2f}"

    # 4 exchange_info
    def s_exchange():
        info = _val(exchange_info)
        syms = info.get("symbols") if isinstance(info, dict) else None
        if not syms:
            return False, "no_symbols"
        name = _val(symbol)
        if name and not any(s.get("symbol") == name for s in syms):
            return False, f"{name} not listed"
        return True, f"symbols={len(syms)}"

    # 5 filters
    def s_filters():
        f = _val(filters)
        if not isinstance(f, dict):
            return False, "not_provided"
        lot = f.get("LOT_SIZE") or {}
        if not lot.get("stepSize"):
            return False, "LOT_SIZE.stepSize missing"
        if float(lot["stepSize"]) <= 0:
            return False, "stepSize<=0"
        min_n = (f.get("MIN_NOTIONAL") or {}).get("notional")
        if min_n is None or float(min_n) <= 0:
            return False, "MIN_NOTIONAL missing"
        return True, f"step={lot['stepSize']} minNotional={min_n}"

    # 6 quality
    def s_quality():
        q = _val(quality)
        if not isinstance(q, dict):
            return False, "not_provided"
        if q.get("quality_vetoes"):
            return False, "veto:" + ",".join(q["quality_vetoes"])
        cq = q.get("CQ")
        if cq is None:
            return False, "no_quality_data"
        return True, f"cq={float(cq):.3f}"

    # 7 leverage
    def s_leverage():
        lev = _val(leverage)
        if lev is None:
            return False, "not_provided"
        import leverage_policy
        ok, detail = leverage_policy.leverage_ok(lev, _val(sl_pct))
        return ok, detail

    # 8 sizing
    def s_sizing():
        s = _val(size)
        if s is None:
            return False, "not_provided"
        ok = bool(getattr(s, "ok", False))
        return ok, str(getattr(s, "reason", ""))

    # 9 geometry
    def s_geometry():
        g = _val(geometry)
        if g is None:
            return False, "not_provided"
        ok = bool(getattr(g, "ok", False))
        return ok, str(getattr(g, "reason", ""))

    # 10 risk
    def s_risk():
        r = _val(risk)
        if r is None:
            return False, "not_provided"
        notional, reason = r
        return notional is not None, str(reason)

    # 11 exposure
    def s_exposure():
        e = _val(exposure)
        if not isinstance(e, dict):
            return False, "not_provided"
        opened = int(e.get("open_positions") or 0)
        cap = int(e.get("max_positions") or 0)
        if cap <= 0:
            return False, "max_positions<=0"
        if opened >= cap:
            return False, f"open={opened}>={cap}"
        usage = float(e.get("margin_usage") or 0.0)
        limit = float(e.get("margin_limit") or 0.4)
        if usage > limit:
            return False, f"margin={usage:.2f}>{limit:.2f}"
        return True, f"open={opened}/{cap} margin={usage:.2f}"

    # 12 edge
    def s_edge():
        ed = _val(edge)
        if ed is None:
            return False, "not_provided"
        allow, reason = ed
        return bool(allow), str(reason)

    for name, fn in zip(STEP_NAMES, (s_config, s_offline, s_account, s_exchange,
                                     s_filters, s_quality, s_leverage, s_sizing,
                                     s_geometry, s_risk, s_exposure, s_edge)):
        if not _run(steps, name, fn):
            _fail(name, steps[-1].detail)
            return result
    return result
