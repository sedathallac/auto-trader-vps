"""test_faz18_orphan_report_b6.py — BLOK6 İŞ2: E2 preflight KURAL TABLOSU.

Eski REFUSE koşulu (`enableWithdrawals True or ipRestrict not True`) yerini
açık bir bildirimsel tabloya bırakır; her satır ayrı test edilir:

  enableWithdrawals=True          → RED    (RET, exit 4)          — davranış aynı
  ipRestrict=False                → RED    (RET, exit 4)          — davranış aynı
  enableInternalTransfer=True     → UYARI  (bayrak isimli WARNING, devam)
  enableMargin=True               → UYARI  (aynı)
  enableSpotAndMarginTrading=True → UYARI  (aynı)
  enableFutures=True              → KABUL  (sessiz: ne uyarı ne RET)
  Bileşik: withdrawals=F ip=T futures=T internalTransfer=T → exit 0,
      TAM BİR WARNING, enableInternalTransfer adını taşır, RET'te SÖZ YOK.

Ağ YOK: b5'teki sahte-transport (FakeTransport) stili, dosya-içi kopya.
"""
import sqlite3

from tools import orphan_report as OR

# ---------------------------------------------------------------- yardımcılar
# (b5 dosyasındaki sahte-transport stilinin kendi-içeren kopyası)

LEDGER_COLS = ("symbol", "token", "state", "created_at", "updated_at", "gen",
               "algo_id", "client_algo_id")

_DB_SEQ = [0]


def make_db(tmp_path, columns=LEDGER_COLS, rows=()):
    _DB_SEQ[0] += 1
    p = tmp_path / f"ledger-{_DB_SEQ[0]}.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE sl_placements (%s)"
                % ", ".join(f"{c} TEXT" for c in columns))
    for r in rows:
        con.execute("INSERT INTO sl_placements VALUES (%s)"
                    % ",".join("?" * len(columns)), r)
    con.commit()
    con.close()
    return str(p)


class FakeTransport:
    """Orkestra taşıyıcı stub'ı (b5 stili): path→veri; hata raise edilir."""

    def __init__(self, positions=(), algos=(), orders=(),
                 api_restrictions=None):
        def _lst(v):
            return v if isinstance(v, Exception) else list(v)
        self.data = {
            "/fapi/v2/positionRisk": _lst(positions),
            "/fapi/v1/openAlgoOrders": _lst(algos),
            "/fapi/v1/openOrders": _lst(orders),
            "/sapi/v1/account/apiRestrictions": api_restrictions
            if api_restrictions is not None
            else {"enableWithdrawals": False, "ipRestrict": True},
        }
        self.calls = []
        self.w1m = 0

    def get(self, path, params=None, base=None):
        self.calls.append((path, params, base))
        v = self.data.get(path)
        if isinstance(v, Exception):
            if isinstance(v, (OR.QueryError, OR.BanError)):
                raise v
            raise OR.QueryError(f"{type(v).__name__}: {v}")
        if v is None:
            raise OR.QueryError(f"bilinmeyen yol {path}")
        return v


def LONG(amt="0.1", entry="90000", mark="91000", sym="BTCUSDT"):
    return {"symbol": sym, "positionAmt": amt, "entryPrice": entry,
            "markPrice": mark}


def SL(side="SELL", status="NEW", trig="89000", close=True, sym="BTCUSDT",
       aid="1", otype="STOP_MARKET"):
    return {"algoId": aid, "symbol": sym, "side": side, "algoStatus": status,
            "orderType": otype, "positionSide": "BOTH",
            "triggerPrice": trig, "closePosition": close}


def restr(**over):
    """TEMEL-tetiklenmeyen apiRestrictions sözlüğü + satır-üstü yazmalar."""
    d = {"enableWithdrawals": False, "ipRestrict": True,
         "enableInternalTransfer": False, "enableMargin": False,
         "enableSpotAndMarginTrading": False, "enableFutures": False}
    d.update(over)
    return d


def _warn_lines(cap):
    """stderr'deki WARNING/UYARI satırları (UYARI sözleşmesi)."""
    return [l for l in cap.err.splitlines() if "UYARI" in l]


def _run(tmp_path, restrictions):
    """Temiz-pozisyon senaryosuyla main koşar; (rc, capsys çıktısı) döner."""
    tr = FakeTransport(positions=[LONG()], algos=[SL()],
                       api_restrictions=restrictions)
    rc = OR.main(["--db", make_db(tmp_path)], transport=tr)
    return rc, tr


# ================================================================ kural satırları


class TestE2B6RuleTable:
    # --- SATIR 1: enableWithdrawals=True → RED (exit 4) — davranış değişmez.
    def test_row_withdrawals_red(self, tmp_path, capsys):
        rc, _ = _run(tmp_path, restr(enableWithdrawals=True))
        assert rc == 4
        cap = capsys.readouterr()
        assert "RET" in cap.out + cap.err
        assert "enableWithdrawals" in cap.out + cap.err

    # --- SATIR 2: ipRestrict=False → RED (exit 4) — davranış değişmez.
    def test_row_ip_restrict_red(self, tmp_path, capsys):
        rc, _ = _run(tmp_path, restr(ipRestrict=False))
        assert rc == 4
        cap = capsys.readouterr()
        assert "RET" in cap.out + cap.err
        assert "ipRestrict" in cap.out + cap.err

    # --- SATIR 3: enableInternalTransfer=True → UYARI only, koşmaya devam.
    def test_row_internal_transfer_warn_only(self, tmp_path, capsys):
        rc, _ = _run(tmp_path, restr(enableInternalTransfer=True))
        assert rc == 0, "UYARI satırı RED olmamalı — araç koşmaya devam eder"
        cap = capsys.readouterr()
        warns = _warn_lines(cap)
        assert len(warns) == 1, f"tam bir UYARI beklenirdi: {warns}"
        assert "enableInternalTransfer" in warns[0]
        assert "RET" not in cap.out + cap.err

    # --- SATIR 4: enableMargin=True → UYARI only.
    def test_row_margin_warn_only(self, tmp_path, capsys):
        rc, _ = _run(tmp_path, restr(enableMargin=True))
        assert rc == 0
        cap = capsys.readouterr()
        warns = _warn_lines(cap)
        assert len(warns) == 1, f"tam bir UYARI beklenirdi: {warns}"
        assert "enableMargin" in warns[0]
        assert "RET" not in cap.out + cap.err

    # --- SATIR 5: enableSpotAndMarginTrading=True → UYARI only.
    def test_row_spot_margin_warn_only(self, tmp_path, capsys):
        rc, _ = _run(tmp_path, restr(enableSpotAndMarginTrading=True))
        assert rc == 0
        cap = capsys.readouterr()
        warns = _warn_lines(cap)
        assert len(warns) == 1, f"tam bir UYARI beklenirdi: {warns}"
        assert "enableSpotAndMarginTrading" in warns[0]
        assert "RET" not in cap.out + cap.err

    # --- SATIR 6: enableFutures=True → KABUL: ne uyarı ne RET, sessiz.
    def test_row_futures_accepted_silently(self, tmp_path, capsys):
        rc, _ = _run(tmp_path, restr(enableFutures=True))
        assert rc == 0
        cap = capsys.readouterr()
        assert _warn_lines(cap) == [], "KABUL satırı uyarı ÜRETEMEZ"
        assert "RET" not in cap.out + cap.err

    # --- BİLEŞİK: futures+internalTransfer aynı anda → tek UYARI (transfer),
    #     futures sessiz; hüküm satırı koşar; reddetme sözü YOK.
    def test_combined_futures_plus_internal_transfer_single_warning(
            self, tmp_path, capsys):
        rc, tr = _run(tmp_path, restr(enableFutures=True,
                                      enableInternalTransfer=True))
        assert rc == 0
        cap = capsys.readouterr()
        warns = _warn_lines(cap)
        assert len(warns) == 1, f"TAM BİR WARNING beklenirdi: {warns}"
        assert "enableInternalTransfer" in warns[0]
        # Reddetme sözü geçmeyecek (RET anahtarı + eski RET cümle izleri):
        allc = cap.out + cap.err
        assert "RET:" not in allc
        assert "DURUYORUM" not in allc
        # Reddedilmedi — taramanın kendisi de koştu (4 sorgu: preflight+3).
        assert "4/4 sorgu başarılı" in cap.out
        assert tr.calls[0][0] == "/sapi/v1/account/apiRestrictions"
