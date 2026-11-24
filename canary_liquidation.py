"""canary_liquidation.py — BLOK28.1 §6: bakım marjı katmanı tabanlı
likidasyon modeli (One-way mod; USDT-M).

Formül (Binance USDT-M resmî likidasyon koşulu):
  Margin Balance = Maintenance Margin anında likidasyon:
    MB = w_eff + dir·q·(m − e)        (w_eff: izolede pozisyon marjı,
                                      cross'ta hesap marj bakiyesi + diğer
                                      pozisyonların uPnL'si)
    MM(m) = q·m·MMR(bracket) − cum(bracket)
  Long (dir=+1) çözümü:  m_liq = (q·e − w_eff − cum) / (q·(1 − MMR))
  Short (dir=−1) çözümü: m_liq = (q·e + w_eff + cum) / (q·(1 + MMR))
  bracket, likidasyon anındaki notional'a (q·m_liq) göre seçilir → iterasyon.

Güvenlik sözleşmesi (§6.2): bracket / margin mode / leverage / pozisyon modu
eksik veya Hedge mod belirsizse → LIQUIDATION_RISK_UNVERIFIED → o sembolde
GERÇEK POZİSYON AÇILMASI ENGELLENİR (fail-closed). Tahmini tier ASLA
kullanılmaz — yalnız borsadan okunan gerçek leverageBracket verisi.
"""
import math

LIQUIDATION_RISK_UNVERIFIED = "LIQUIDATION_RISK_UNVERIFIED"
POSITION_MODE_ONEWAY = "ONEWAY"
POSITION_MODE_HEDGE = "HEDGE"


def parse_leverage_brackets(response):
    """GET /fapi/v1/leverageBracket yanıtı → {symbol: [bracket dict...]}.
    Bracket: floor/cap notional (USDT), ratio (MMR), cum (bakım tutarı)."""
    out = {}
    for row in response:
        sym = row.get("symbol")
        brackets = []
        for b in row.get("brackets", []):
            brackets.append({
                "bracket": int(b["bracket"]),
                "floor": float(b.get("notionalFloor", 0) or 0),
                "cap": float(b["notionalCap"]),
                "ratio": float(b["maintMarginRatio"]),
                "cum": float(b.get("cum", b.get("cummulativeMaintAmount", 0))
                             or 0),
                "max_leverage": int(b.get("initialLeverage", 0) or 0),
            })
        if brackets:
            out[sym] = brackets
    return out


def maintenance_for_notional(brackets, notional):
    """Notional'ın düştüğü bracket → (MMR, cum). Boşlukta None (fail-closed)."""
    for b in brackets:
        if b["floor"] <= notional < b["cap"]:
            return b["ratio"], b["cum"]
    return None


def liquidation_price(direction, qty, entry_px, wallet_eff, brackets,
                      max_iter=8):
    """One-way USDT-M likidasyon fiyatı. direction +1 long / −1 short.
    wallet_eff: izole → pozisyon izole marjı; cross → hesap marj bakiyesi +
    diğer pozisyonların uPnL'si. Bracket likidasyon notional'ına göre
    iteratif. Hesaplanamıyorsa None (fail-closed)."""
    if qty <= 0 or not brackets:
        return None
    m = entry_px                      # ilk bracket tahmini: entry notional
    liq = None
    for _ in range(max_iter):
        mm = maintenance_for_notional(brackets, qty * m)
        if mm is None:
            # notional en üst bracket cap'ini aşıyorsa en üst bracket kullan
            mm = (brackets[-1]["ratio"], brackets[-1]["cum"])
        ratio, cum = mm
        if direction > 0:
            cand = (qty * entry_px - wallet_eff - cum) / (qty * (1 - ratio))
        else:
            cand = (qty * entry_px + wallet_eff + cum) / (qty * (1 + ratio))
        if cand <= 0:
            return None               # marj pozisyonu zaten kapatamaz durumda
        if liq is not None and abs(cand - liq) < 1e-12:
            return cand               # bracket sabitlendi, yakınsadı
        liq, m = cand, cand
    return liq                        # sınırlı iterasyon — son değer


def liquidation_distance_bp(liq_px, mark_px):
    if liq_px is None or mark_px is None or mark_px <= 0:
        return None
    return abs(liq_px - mark_px) / mark_px * 1e4


class LiquidationModel:
    """Sembol başına likidasyon doğrulama kapısı (§6.2).

    Girdilerin TAMAMI gerçek (borsadan okunan) olmalıdır:
      brackets_by_symbol — GET /fapi/v1/leverageBracket (parse edilmiş)
      margin_modes       — {symbol: 'isolated'|'cross'} (positionRisk'den)
      leverages          — {symbol: int} (positionRisk'den)
      position_mode      — 'ONEWAY' | 'HEDGE' (GET positionSide/dual)
      wallet_usdt        — cross modeli için hesap marj bakiyesi
    Eksik/belirsiz → verify() False döner → girişler ENGELLENİR.
    """

    def __init__(self, brackets_by_symbol=None, margin_modes=None,
                 leverages=None, position_mode=None, wallet_usdt=None,
                 other_positions_upnl=0.0):
        self.brackets = brackets_by_symbol or {}
        self.margin_modes = margin_modes or {}
        self.leverages = leverages or {}
        self.position_mode = position_mode
        self.wallet_usdt = wallet_usdt
        self.other_upnl = other_positions_upnl

    # ---- sembol doğrulama ----------------------------------------------------
    def verify(self, symbol):
        """(ok, reason). ok=False → LIQUIDATION_RISK_UNVERIFIED + neden."""
        if self.position_mode is None:
            return False, f"{LIQUIDATION_RISK_UNVERIFIED}: pozisyon modu " \
                          "okunamadı (One-way/Hedge bilinmiyor)"
        if self.position_mode == POSITION_MODE_HEDGE:
            return False, f"{LIQUIDATION_RISK_UNVERIFIED}: Hedge mod — " \
                          "model One-way sözleşmesiyle sınırlı (operatör " \
                          "kararı gerekir)"
        brs = self.brackets.get(symbol)
        if not brs:
            return False, f"{LIQUIDATION_RISK_UNVERIFIED}: {symbol} bakım " \
                          "katmanı (leverageBracket) verisi YOK"
        mm = self.margin_modes.get(symbol)
        if mm not in ("isolated", "cross"):
            return False, f"{LIQUIDATION_RISK_UNVERIFIED}: {symbol} margin " \
                          f"modu bilinmiyor ({mm!r})"
        lev = self.leverages.get(symbol)
        if not lev or int(lev) < 1:
            return False, f"{LIQUIDATION_RISK_UNVERIFIED}: {symbol} kaldıraç " \
                          f"bilinmiyor ({lev!r})"
        if mm == "cross" and self.wallet_usdt is None:
            return False, f"{LIQUIDATION_RISK_UNVERIFIED}: cross mod için " \
                          "hesap marj bakiyesi okunamadı"
        return True, "ok"

    # ---- değerlendirme ---------------------------------------------------------
    def evaluate(self, symbol, direction, notional, entry_px, mark_px):
        """Olası/gerçek pozisyon için likidasyon analizi (§6.1 tablosu)."""
        ok, reason = self.verify(symbol)
        out = {"symbol": symbol, "verified": ok, "status":
               "OK" if ok else LIQUIDATION_RISK_UNVERIFIED, "reason": reason}
        if not ok:
            return out
        qty = notional / entry_px if entry_px else 0.0
        lev = int(self.leverages[symbol])
        mm_mode = self.margin_modes[symbol]
        if mm_mode == "isolated":
            wallet_eff = notional / lev          # izole başlangıç marjı
        else:
            # cross: hesap marj bakiyesi + diğer pozisyon uPnL (this pozisyon
            # kendi uPnL'si formülde ayrı çözülüyor)
            wallet_eff = (self.wallet_usdt or 0.0) + self.other_upnl
        liq = liquidation_price(direction, qty, entry_px, wallet_eff,
                                self.brackets[symbol])
        mmr_cum = maintenance_for_notional(self.brackets[symbol], notional)
        out.update({
            "direction": "LONG" if direction > 0 else "SHORT",
            "notional": round(notional, 4),
            "qty": qty,
            "entry_px": entry_px,
            "mark_px": mark_px,
            "leverage": lev,
            "margin_mode": mm_mode,
            "initial_margin": round(wallet_eff if mm_mode == "isolated"
                                    else notional / lev, 6),
            "maintenance_margin_ratio": mmr_cum[0] if mmr_cum else None,
            "maintenance_amount": mmr_cum[1] if mmr_cum else None,
            "maintenance_margin": (round(notional * mmr_cum[0] - mmr_cum[1], 6)
                                   if mmr_cum else None),
            "est_liquidation_px": liq,
            "liquidation_distance_bp": liquidation_distance_bp(liq, mark_px),
        })
        if liq is None:
            out["verified"] = False
            out["status"] = LIQUIDATION_RISK_UNVERIFIED
            out["reason"] = "likidasyon fiyatı çözülemedi (marj/bracket " \
                            "tutarsızlığı)"
        return out

    # ---- borsa raporuyla karşılaştırma (§6.1) -----------------------------------
    def compare_reported(self, symbol, direction, notional, entry_px,
                         mark_px, reported_liq_px, tol_bp=25.0):
        """Hesaplanan liq ile positionRisk.liquidationPrice karşılaştırması.
        Fark > tol_bp → açıklama adayları raporlanır (cross ortak bakiye,
        diğer pozisyonlar, funding/ücret, borsa formül farkı)."""
        ev = self.evaluate(symbol, direction, notional, entry_px, mark_px)
        out = {"evaluated": ev, "reported_liq_px": reported_liq_px}
        if reported_liq_px is None or ev.get("est_liquidation_px") is None:
            out["match"] = None
            out["note"] = "karşılaştırma yapılamadı (taraf yok)"
            return out
        diff_bp = abs(ev["est_liquidation_px"] - reported_liq_px) \
            / reported_liq_px * 1e4
        out["diff_bp"] = round(diff_bp, 3)
        out["match"] = diff_bp <= tol_bp
        if not out["match"]:
            out["note"] = ("fark olası nedenleri: cross ortak cüzdan "
                           "(diğer pozisyon uPnL), funding/komisyon tahakkuku, "
                           "borsa iç formül (ADL/insurance) farkı, tier "
                           "sınırına yakınlık")
        return out


def round_to_tick(price, tick, toward_entry, is_long):
    """Fiyatı tick'e yuvarla. toward_entry=True → tetik ERKEN olur (loss
    küçülür, güvenli yön); long için yukarı, short için aşağı yuvarla."""
    if tick is None or tick <= 0:
        return None
    n = price / tick
    if toward_entry:
        n = math.ceil(n) if is_long else math.floor(n)
    else:
        n = math.floor(n) if is_long else math.ceil(n)
    return round(n * tick, 12)
