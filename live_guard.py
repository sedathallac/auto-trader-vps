"""FAZ9 live-mode guard: staged activation, winning-family gate, fail-closed startup checks.

The compare instance (`auto_trader_faz9.service`, HB_OFFLINE=1) keeps collecting signal_outcomes and never sends an
order. This module is what the live instance (`auto_trader_faz9_live.service`, HB_OFFLINE=0) uses to decide whether it
may trade at all, and how much.

Every check here is fail-closed: an unset, unparsable or out-of-range value refuses the start instead of falling back to
a permissive default. Nothing in this module places an order or talks to the network by itself — `startup_check` takes
an injected client so it can be tested without a key.
"""
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

FUTURES_MAINNET = "https://fapi.binance.com"
FUTURES_TESTNET = "https://testnet.binancefuture.com"

# GÖREV 2026-11-27 §9.3 — URL güvenliği: imzalı Binance isteklerinin (API key
# başlıkta + HMAC imzası sorguda) gittiği taban adres kontrolden çıkamaz.
# Canlı modda host bu allowlist dışında olamaz; özel adres (yerel test rigı)
# yalnız operatörün açık opt-in'iyle (LIVE_ALLOW_CUSTOM_BASE_URL=1) geçer.
ALLOWED_FUTURES_HOSTS = ("fapi.binance.com", "testnet.binancefuture.com")

# Stage caps. A stage may run BELOW its cap but never above it: the env file cannot talk itself into more risk than the
# stage it declares. Stage 4 is the only one that reaches the full 2500 USDT book, and it needs an explicit operator
# confirmation flag on top of the stage number.
STAGE_CAPS = {
    1: {"capital_usdt": 0.0, "max_notional": 200.0, "daily_loss": 20.0, "leverage": 5, "max_positions": 2},
    2: {"capital_usdt": 100.0, "max_notional": 200.0, "daily_loss": 20.0, "leverage": 20, "max_positions": 5},
    3: {"capital_usdt": 500.0, "max_notional": 400.0, "daily_loss": 50.0, "leverage": 5, "max_positions": 5},
    4: {"capital_usdt": 2500.0, "max_notional": 840.0, "daily_loss": 150.0, "leverage": 5, "max_positions": 5},
}

# Deterministic winner rule, fixed before looking at the data so the answer cannot be fitted to it.
MIN_SAMPLE = 100
MIN_WIN_PCT = 50.0
MIN_AVG_RET = 0.0


DEFAULT_WINNING_REGIMES = ("range", "mid")


def _env_set(env, name, default):
    """Comma separated set. Unset -> default; explicitly empty -> empty set (which refuses every order)."""
    raw = env.get(name)
    if raw is None:
        return frozenset(default)
    return frozenset(p.strip().lower() for p in str(raw).split(",") if p.strip())


class LiveConfigError(Exception):
    """Raised when the live configuration is not safe to start. The service must not come up."""


def _safe_base_repr(base):
    """Hata mesajlarında base'in güvenli temsili — userinfo maskelenir
    (adrese gömülü kimlik bilgisi loga/çıktıya sızamaz)."""
    try:
        u = urllib.parse.urlsplit(str(base))
    except ValueError:
        return "<ayrıştırılamadı>"
    if u.username or u.password:
        return f"{u.scheme}://***@{u.hostname or ''}"
    return str(base)


def ensure_futures_base_url(base, *, strict_host=True, env=None):
    """GÖREV 2026-11-27 §9.3 (BÖLÜM 18) — URL doğrulamasının TEK boğum
    noktası. BinanceAPI.__init__ env'den base okuduğunda LiveConfig
    doğrulaması atlanabiliyordu (orphan_killer/archiver yolu) — imzalı
    isteklerin gittiği adres burada DA kontrol edilir.

    Kurallar: scheme her zaman https; strict_host=True iken host
    ALLOWED_FUTURES_HOSTS içinde olmalı. Operatör opt-in'i
    (LIVE_ALLOW_CUSTOM_BASE_URL=1) allowlist'i YALNIZ canlı olmayan
    kurulumlarda (LIVE_MODE kapalı) aşabilir — LIVE_MODE=1 iken
    allowlist MUTLAKTIR, opt-in geçersizdir.

    GÖREV 2026-11-27 (hedefli güvenlik düzeltmesi): yapısal denetim
    eklendi — port, kullanıcı bilgisi (userinfo), path, query ve fragment
    reddi. hostname urlsplit tarafından zaten normalize edilir (küçük
    harf). Böylece imzalı istek hedefi allowlist'teki hosta ek bir
    yol/path/port üzerinden kaydırılamaz."""
    env = os.environ if env is None else env
    _u = urllib.parse.urlsplit(base)
    if _u.scheme != "https" or not _u.hostname:
        raise LiveConfigError(
            f"BINANCE_FUTURES_BASE_URL https olmalı ve host içermeli: "
            f"{_safe_base_repr(base)}")
    if _u.username or _u.password:
        raise LiveConfigError(
            "BINANCE_FUTURES_BASE_URL kullanıcı bilgisi (userinfo) "
            f"içeremez: {_u.scheme}://***@{_u.hostname}")
    try:
        _port = _u.port
    except ValueError:
        _port = -1  # aralık dışı port → aşağıda reddedilir
    if _port is not None:
        raise LiveConfigError(
            f"Binance taban adresi port içeremez: port={_port} "
            f"(host={_u.hostname!r})")
    if _u.path not in ("", "/"):
        raise LiveConfigError(
            f"Binance taban adresi path içeremez: {_u.path!r} "
            f"(host={_u.hostname!r})")
    if _u.query:
        raise LiveConfigError("Binance taban adresi query içeremez")
    if _u.fragment:
        raise LiveConfigError("Binance taban adresi fragment içeremez")
    if strict_host and _u.hostname not in ALLOWED_FUTURES_HOSTS:
        # GÖREV 2026-11-27 (devam — hedefli güvenlik düzeltmesi): GERÇEK
        # canlı imzalı isteklerde allowlist MUTLAKTIR. LIVE_MODE=1 iken
        # operatör opt-in'i (LIVE_ALLOW_CUSTOM_BASE_URL) allowlist'i ASLA
        # aşamaz — opt-in yalnız canlı OLMAYAN kurulumlar (yerel test
        # rigı, LIVE_MODE kapalı) için geçerlidir. Mainnet/testnet
        # davranışı değişmez.
        if _env_bool(env, "LIVE_MODE", False):
            raise LiveConfigError(
                f"Binance taban adresi allowlist dışı: {_u.hostname!r} "
                f"(izinli: {', '.join(ALLOWED_FUTURES_HOSTS)}) — CANLI "
                "modda (LIVE_MODE=1) LIVE_ALLOW_CUSTOM_BASE_URL opt-in'i "
                "allowlist'i AŞAMAZ; mainnet/testnet dışı adres kesinlikle "
                "reddedilir")
        if not _env_bool(env, "LIVE_ALLOW_CUSTOM_BASE_URL", False):
            raise LiveConfigError(
                f"Binance taban adresi allowlist dışı: {_u.hostname!r} "
                f"(izinli: {', '.join(ALLOWED_FUTURES_HOSTS)}); özel adres "
                "yalnız operatör açık opt-in'iyle "
                "(LIVE_ALLOW_CUSTOM_BASE_URL=1) ve CANLI MOD KAPALIYKEN")
    return base


def _env_bool(env, name, default=False):
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(env, name, default):
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise LiveConfigError(f"{name} is not a number: {raw!r}")


def _env_int(env, name, default):
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise LiveConfigError(f"{name} is not an integer: {raw!r}")


@dataclass
class LiveConfig:
    live_mode: bool
    hb_offline: bool
    testnet: bool
    dry_run: bool
    compare_mode: bool
    stage: int
    max_notional: float
    daily_loss_limit: float
    leverage: int
    max_open_positions: int
    strategy_family: str
    winning_regimes: frozenset
    allow_unproven_family: bool
    base_url: str
    stage_confirmed: bool = False
    # FAZ14: sabit USDT tavanları yerine equity yüzdeleri. 0 = tavan yok.
    max_position_pct: float = 20.0
    daily_loss_pct: float = 2.0
    max_leverage: int = 5
    notes: list = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        """True when this process is allowed to reach the exchange with real orders.

        LIVE_MODE must be set explicitly. Without it this is not a live instance, so every gate stays open and the
        compare behaviour is byte-for-byte what it was before this module existed.
        """
        return self.live_mode and not self.hb_offline and not self.dry_run and not self.compare_mode

    @classmethod
    def from_env(cls, env=None) -> "LiveConfig":
        env = os.environ if env is None else env
        live_mode = _env_bool(env, "LIVE_MODE", False)
        hb_offline = _env_bool(env, "HB_OFFLINE", False)
        testnet = _env_bool(env, "BINANCE_TESTNET", False)
        stage = _env_int(env, "LIVE_STAGE", 1)
        caps = STAGE_CAPS.get(stage)
        if caps is None:
            raise LiveConfigError(f"LIVE_STAGE must be 1..4, got {stage}")
        base = (env.get("BINANCE_FUTURES_BASE_URL") or "").strip()
        if not base:
            base = FUTURES_TESTNET if testnet else FUTURES_MAINNET
        # GÖREV 2026-11-27 §9.3 — taban adres doğrulaması (fail-closed,
        # tek implementasyon: ensure_futures_base_url). Canlı modda host
        # allowlist'i de zorunlu; compare'da yalnız https şartı.
        ensure_futures_base_url(base, strict_host=live_mode, env=env)
        return cls(
            live_mode=live_mode,
            hb_offline=hb_offline,
            testnet=testnet,
            dry_run=_env_bool(env, "LIVE_DRY_RUN", True),
            compare_mode=_env_bool(env, "COMPARE_MODE", False),
            stage=stage,
            max_notional=_env_float(env, "LIVE_MAX_NOTIONAL_USDT", caps["max_notional"]),
            daily_loss_limit=_env_float(env, "LIVE_DAILY_LOSS_LIMIT_USDT", caps["daily_loss"]),
            leverage=_env_int(env, "LIVE_LEVERAGE", caps["leverage"]),
            max_open_positions=_env_int(env, "LIVE_MAX_OPEN_POSITIONS", caps["max_positions"]),
            strategy_family=(env.get("LIVE_STRATEGY_FAMILY") or "").strip().lower(),
            winning_regimes=_env_set(env, "LIVE_WINNING_REGIMES", DEFAULT_WINNING_REGIMES),
            allow_unproven_family=_env_bool(env, "LIVE_ALLOW_UNPROVEN_FAMILY", False),
            base_url=base.rstrip("/"),
            stage_confirmed=_env_bool(env, "LIVE_STAGE_CONFIRMED", False),
            max_position_pct=_env_float(env, "LIVE_MAX_POSITION_PCT", 20.0),
            daily_loss_pct=_env_float(env, "LIVE_DAILY_LOSS_PCT", 2.0),
            max_leverage=_env_int(env, "LIVE_MAX_LEVERAGE", caps["leverage"]),
        )

    def validate(self) -> "LiveConfig":
        """Refuse anything that would trade more than its stage allows, or trade an unproven family with real money."""
        caps = STAGE_CAPS[self.stage]
        if not self.live_mode:
            # Not a live instance (compare, tests, ad-hoc runs): nothing to validate, no order is gated.
            self.notes.append("LIVE_MODE unset: not a live instance")
            return self
        if self.hb_offline:
            raise LiveConfigError("LIVE_MODE=1 and HB_OFFLINE=1 contradict each other")
        if self.compare_mode:
            raise LiveConfigError("LIVE_MODE=1 and COMPARE_MODE=1 contradict each other")
        if self.stage == 1 and not self.testnet:
            raise LiveConfigError("stage 1 is testnet only: set BINANCE_TESTNET=1")
        if self.stage >= 2 and self.testnet:
            raise LiveConfigError(f"stage {self.stage} is mainnet: BINANCE_TESTNET must be 0")
        if self.stage >= 2 and not self.stage_confirmed:
            raise LiveConfigError(f"stage {self.stage} needs LIVE_STAGE_CONFIRMED=1 (operator sign-off)")
        if self.max_notional > 0 and self.max_notional > caps["max_notional"]:
            raise LiveConfigError(
                f"LIVE_MAX_NOTIONAL_USDT={self.max_notional} exceeds stage {self.stage} cap {caps['max_notional']}")
        if self.daily_loss_limit > 0 and self.daily_loss_limit > caps["daily_loss"]:
            raise LiveConfigError(
                f"LIVE_DAILY_LOSS_LIMIT_USDT={self.daily_loss_limit} exceeds stage {self.stage} cap {caps['daily_loss']}")
        if self.leverage > caps["leverage"]:
            raise LiveConfigError(f"LIVE_LEVERAGE={self.leverage} exceeds stage {self.stage} cap {caps['leverage']}")
        if self.max_leverage > caps["leverage"]:
            raise LiveConfigError(
                f"LIVE_MAX_LEVERAGE={self.max_leverage} exceeds stage {self.stage} cap {caps['leverage']}")
        if self.max_open_positions > caps["max_positions"]:
            raise LiveConfigError(
                f"LIVE_MAX_OPEN_POSITIONS={self.max_open_positions} exceeds stage {self.stage} cap "
                f"{caps['max_positions']}")
        if not (0 < self.max_position_pct <= 100):
            raise LiveConfigError(f"LIVE_MAX_POSITION_PCT must be in (0, 100], got {self.max_position_pct}")
        if not (0 < self.daily_loss_pct <= 100):
            raise LiveConfigError(f"LIVE_DAILY_LOSS_PCT must be in (0, 100], got {self.daily_loss_pct}")
        if self.leverage < 1 or self.max_open_positions < 1:
            raise LiveConfigError("live limits must be positive")
        if not self.strategy_family and not self.winning_regimes:
            raise LiveConfigError(
                "neither LIVE_STRATEGY_FAMILY nor LIVE_WINNING_REGIMES is set: no gate would stop an order")
        if not self.strategy_family:
            # Regime filter only. Allowed on purpose: the regime gate is the narrower of the two, and an empty family
            # must not silently mean "every family".
            self.notes.append("LIVE_STRATEGY_FAMILY empty: regime gate only")
        if self.allow_unproven_family and not self.testnet:
            raise LiveConfigError("LIVE_ALLOW_UNPROVEN_FAMILY is testnet-only")
        if self.testnet:
            self.notes.append("testnet: real orders, fake money")
        return self

    def risk_overrides(self) -> dict:
        """The RiskEngine keys this stage pins. Everything else keeps the shared RISK_CONFIG value.

        FAZ14: sabit USDT tavanları yerine equity yüzdeleri. `max_notional`/`max_daily_loss_usdt`
        yalnız operatör açıkça sabit bir tavan verdiyse döner; verilmediyse (0) RiskEngine
        yüzde tavanlarını kullanır.
        """
        out = {
            "max_position_pct": self.max_position_pct,
            "max_daily_loss_pct": self.daily_loss_pct / 100.0,
            "max_leverage": self.max_leverage,
            "min_leverage": 1,
        }
        if self.max_notional > 0:
            out["max_notional"] = self.max_notional
            out["min_notional"] = min(5.5, self.max_notional)
        if self.daily_loss_limit > 0:
            out["max_daily_loss_usdt"] = self.daily_loss_limit
        return out


def family_stats(db_path, min_sample=MIN_SAMPLE):
    """Every family's resolved sample, win rate and mean forward return, worst first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT family, COUNT(*) AS n, SUM(resolved) AS res,"
            " 100.0 * SUM(CASE WHEN resolved=1 AND fwd_ret>0 THEN 1 ELSE 0 END) / NULLIF(SUM(resolved),0) AS win_pct,"
            " AVG(CASE WHEN resolved=1 THEN fwd_ret END) AS avg_ret"
            " FROM signal_outcomes GROUP BY family ORDER BY avg_ret DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for family, n, res, win_pct, avg_ret in rows:
        out.append({
            "family": family,
            "n": int(n or 0),
            "resolved": int(res or 0),
            "win_pct": float(win_pct) if win_pct is not None else 0.0,
            "avg_ret": float(avg_ret) if avg_ret is not None else 0.0,
            "eligible": int(res or 0) >= min_sample,
        })
    return out


def select_winning_family(db_path, min_sample=MIN_SAMPLE, min_win_pct=MIN_WIN_PCT, min_avg_ret=MIN_AVG_RET):
    """The one family allowed to trade, or None.

    Deterministic and fixed in advance: resolved sample >= min_sample AND win rate > min_win_pct AND mean forward
    return > min_avg_ret. When several qualify the highest mean return wins; ties break on the larger sample, then on
    the name, so the same database always yields the same answer.
    """
    qualified = [s for s in family_stats(db_path, min_sample)
                 if s["resolved"] >= min_sample and s["win_pct"] > min_win_pct and s["avg_ret"] > min_avg_ret]
    if not qualified:
        return None
    qualified.sort(key=lambda s: (-s["avg_ret"], -s["resolved"], s["family"]))
    return qualified[0]["family"]


def family_gate(family, cfg: LiveConfig):
    """(allowed, reason) for one signal family about to place an order."""
    if not cfg.is_live:
        return True, "not live: gate open"
    if not cfg.strategy_family:
        # Regime-only configuration: the regime gate decides, the family gate stays open.
        return True, "no family filter: regime gate only"
    if (family or "").strip().lower() != cfg.strategy_family:
        return False, f"family {family!r} is not the live family {cfg.strategy_family!r}"
    return True, "live family"


def regime_gate(regime, cfg: LiveConfig):
    """(allowed, reason) for one signal's regime about to place an order.

    The signal is already in signal_outcomes by the time this runs, so a refusal costs no data: it only stops the order.
    An empty winning set refuses everything, which is the fail-closed reading of "no regime is proven".
    """
    if not cfg.is_live:
        return True, "not live: gate open"
    if not cfg.winning_regimes:
        return False, "LIVE_WINNING_REGIMES is empty: no regime is proven"
    r = (regime or "").strip().lower()
    if r not in cfg.winning_regimes:
        return False, f"regime {regime!r} is not in winning set {sorted(cfg.winning_regimes)}"
    return True, "winning regime"


def startup_check(client, cfg: LiveConfig):
    """One account() + exchangeInfo() call before the first tick. Any failure refuses the start.

    Returns a dict of what was verified. Raises LiveConfigError on anything unexpected, including a zero balance: a live
    instance with no money cannot trade, and starting it anyway only hides the misconfiguration.
    """
    try:
        account = client.account()
    except Exception as e:
        raise LiveConfigError(f"account() failed: {type(e).__name__}: {e}")
    if not isinstance(account, dict) or "assets" not in account:
        raise LiveConfigError(f"account() returned an unusable payload: {str(account)[:200]}")
    usdt = 0.0
    for a in account.get("assets") or []:
        if a.get("asset") == "USDT":
            usdt = float(a.get("availableBalance") or a.get("walletBalance") or 0.0)
            break
    if usdt <= 0:
        raise LiveConfigError(f"USDT balance is {usdt}: refusing to start a live instance with no funds")
    try:
        info = client.exchange_info()
    except Exception as e:
        raise LiveConfigError(f"exchangeInfo() failed: {type(e).__name__}: {e}")
    symbols = info.get("symbols") if isinstance(info, dict) else None
    if not symbols:
        raise LiveConfigError("exchangeInfo() returned no symbols")
    if account.get("canTrade") is False:
        raise LiveConfigError("account reports canTrade=false: the key may not trade futures")
    return {
        "balance_usdt": usdt,
        "symbols": len(symbols),
        "can_trade": account.get("canTrade"),
        "testnet": cfg.testnet,
        "base_url": cfg.base_url,
    }
