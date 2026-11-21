#!/usr/bin/env python3
"""BLOK12 §6 — YALNIZ-OKUNUR watchdog parite doğrulayıcı.

Örnek systemd birim pair'i (orphan_watchdog_faz9_live.service/.timer.example)
+ sarmalayıcı (tools/orphan_watchdog.py) + orphan_killer.py + cron örneği ile
ÜRETİM birimini (deploy/auto_trader_faz9_live.service) makinece karşılaştırır.

KESİNLİKLE YAPMAZ: systemctl/enable/start, dosya yazma, ağ isteği, emir.
Yalnız dosya okur ve rapor basar. ÇIKIŞ: 0=PARİTE OK (WARN olabilir),
1=FAIL (canlıya alınmadan önce kapatılmalı), 2=doğrulanamaz (dosya yok/bozuk).

Kök neden hafızası: watchdog, canlı servisle AYNI $HOME/'u görmezse
TRACKED/HEARTBEAT dosyalarını yanlış yerde arar ve SESSİZ KÖRLÜK üretir
(faz9-deploycand docs/LIVE_READINESS_REPORT.md:201). Bu araç o sınıfı makine
kapısına çevirir.
"""
from __future__ import annotations

import ast
import os
import re
import sys

EXIT_OK, EXIT_FAIL, EXIT_UNVERIFIABLE = 0, 1, 2

# Sözleşme sabitleri (tests/test_blok11_watchdog.py ile AYNI tutulur).
BANNED_ORDER_TOKENS = ("place_algo", "new_order", "create_executor",
                       "market_close", "close_reduce_only", "cancel_order(")


def parse_unit(path):
    """systemd birimini section → [(key, value), ...] sözlüğüne çözer
    (yinelenen anahtarlar korunur — Environment/EnvironmentFile çok satırlı)."""
    out, section = {}, None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                out.setdefault(section, [])
                continue
            if section is None or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[section].append((k.strip(), v.strip()))
    return out


def vals(unit, section, key):
    return [v for k, v in unit.get(section, []) if k == key]


def first(unit, section, key):
    vs = vals(unit, section, key)
    return vs[0] if vs else None


class Report:
    def __init__(self):
        self.lines = []

    def add(self, level, check, msg):
        self.lines.append((level, check, msg))
        print(f"{level:4s} {check}: {msg}")

    ok = lambda self, c, m: self.add("OK", c, m)        # noqa: E731
    warn = lambda self, c, m: self.add("WARN", c, m)    # noqa: E731
    fail = lambda self, c, m: self.add("FAIL", c, m)    # noqa: E731
    info = lambda self, c, m: self.add("INFO", c, m)    # noqa: E731

    @property
    def n_fail(self):
        return sum(1 for l, _, _ in self.lines if l == "FAIL")


def check_pairity(root, rep):
    p = lambda *a: os.path.join(root, *a)  # noqa: E731
    W_SVC = p("deploy/orphan_watchdog_faz9_live.service.example")
    W_TM = p("deploy/orphan_watchdog_faz9_live.timer.example")
    W_CRON = p("deploy/orphan_watchdog_cron.example")
    PROD = p("deploy/auto_trader_faz9_live.service")
    WRAP = p("tools/orphan_watchdog.py")
    KILLER = p("orphan_killer.py")
    AT = p("auto_trader.py")

    missing = [f for f in (W_SVC, W_TM, W_CRON, PROD, WRAP, KILLER, AT)
               if not os.path.isfile(f)]
    if missing:
        for f in missing:
            rep.fail("dosya-yok", f)
        return EXIT_UNVERIFIABLE

    wsvc = parse_unit(W_SVC)
    wtm = parse_unit(W_TM)
    prod = parse_unit(PROD)
    wrap_src = open(WRAP, encoding="utf-8").read()
    killer_src = open(KILLER, encoding="utf-8").read()
    at_src = open(AT, encoding="utf-8").read()

    # 1) Kimlik/çevre paritesi (sessiz körlük sınıfı) -----------------------
    for key in ("User", "WorkingDirectory"):
        wv, pv = first(wsvc, "Service", key), first(prod, "Service", key)
        if wv is None or wv != pv:
            rep.fail(f"parite:{key}", f"watchdog={wv!r} prod={pv!r}")
        else:
            rep.ok(f"parite:{key}", wv)

    w_home = [v for v in vals(wsvc, "Service", "Environment")
              if v.startswith("HOME=")]
    p_home = [v for v in vals(prod, "Service", "Environment")
              if v.startswith("HOME=")]
    if not w_home or not p_home:
        rep.fail("parite:HOME", f"watchdog={w_home} prod={p_home} — eksik")
    elif w_home != p_home:
        rep.fail("parite:HOME", f"watchdog={w_home} prod={p_home} — "
                                "FARKLI HOME = TRACKED/HEARTBEAT körlüğü")
    else:
        rep.ok("parite:HOME", w_home[0])

    w_ef = vals(wsvc, "Service", "EnvironmentFile")
    p_ef = vals(prod, "Service", "EnvironmentFile")
    if not w_ef or w_ef != p_ef:
        rep.fail("parite:EnvironmentFile", f"watchdog={w_ef} prod={p_ef} — "
                                           "anahtar kümesi ayrı dosyalardan "
                                           "gelmemeli")
    else:
        rep.ok("parite:EnvironmentFile", ",".join(w_ef))

    # 2) Yorumlayıcı yolu ----------------------------------------------------
    w_exec = first(wsvc, "Service", "ExecStart") or ""
    p_exec = first(prod, "Service", "ExecStart") or ""
    w_py = w_exec.split() [0] if w_exec else ""
    p_py = p_exec.split()[1] if p_exec.startswith("/bin/sh") else \
        (p_exec.split()[0] if p_exec else "")
    if w_py.endswith("python") and w_py == p_py:
        rep.ok("parite:python-yolu", w_py)
    else:
        rep.fail("parite:python-yolu", f"watchdog={w_py!r} prod={p_py!r}")
    if " -I " in (" " + w_exec):
        rep.ok("sarmalayici:-I", "yalıtılmış mod (PYTHONPATH/user-site yok)")
    else:
        rep.warn("sarmalayici:-I", "ExecStart -I taşımıyor — sys.path "
                                   "sızıntısı denetlenmeli")

    # 3) Zamanlayıcı ---------------------------------------------------------
    timer_vals = vals(wtm, "Timer", "OnUnitActiveSec")
    if timer_vals:
        rep.ok("timer:periyot", ",".join(timer_vals))
    else:
        rep.fail("timer:periyot", "OnUnitActiveSec yok — watchdog hiç döngüye "
                                  "girmez")
    if vals(wtm, "Install", "WantedBy"):
        rep.ok("timer:install", ",".join(vals(wtm, "Install", "WantedBy")))
    else:
        rep.warn("timer:install", "WantedBy yok — enable edilemez")

    # 4) Kilit dosyaları ------------------------------------------------------
    m = re.search(r"--lock\s+(\S+)", w_exec)
    w_lock = m.group(1) if m else None
    if w_lock:
        rep.ok("kilit:systemd", w_lock)
    else:
        rep.fail("kilit:systemd", "ExecStart --lock taşımıyor — eşzamanlı "
                                  "tur riski")
    cron_src = open(W_CRON, encoding="utf-8").read()
    c_lock = (re.search(r"--lock\s+(\S+)", cron_src) or [None, None])[1] \
        if "flock" in cron_src else None
    if c_lock and w_lock and c_lock != w_lock:
        rep.warn("kilit:cron-vs-systemd",
                 f"cron={c_lock} systemd={w_lock} — iki zamanlayıcı birlikte "
                 "kurulursa BİRBİRİNİ DIŞLAMAZ (birini seçin)")

    # 5) Sarmalayıcı sözleşmesi (ast + sıralama) -----------------------------
    tree = ast.parse(wrap_src)
    consts = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith("EXIT_"):
                    consts[t.id] = node.value.value
    if consts.get("EXIT_OK") == 0 and consts.get("EXIT_SCAN_ERROR") == 1 \
            and consts.get("EXIT_CONFIG") == 2:
        rep.ok("sarmalayici:exit-kodlari", "0 tur / 1 scan istisnası / "
                                           "2 kilit-anahtar-yok")
    else:
        rep.fail("sarmalayici:exit-kodlari", f"beklenmeyen sabitler {consts}")
    i_keys = wrap_src.find("BINANCE_KEY")
    i_flock = wrap_src.find("flock(")
    if 0 <= i_keys and 0 <= i_flock and i_keys < i_flock:
        rep.ok("sarmalayici:anahtar-on-kontrol", "kilit/istek ÖNCESİ "
                                                 "anahtar kontrolü (exit 2)")
    else:
        rep.fail("sarmalayici:anahtar-on-kontrol",
                 "anahtar kontrolü flock/istek sırasından ÖNCE değil")
    banned = [t for t in BANNED_ORDER_TOKENS if t in wrap_src]
    if banned:
        rep.fail("sarmalayici:emir-yasak", f"yasaklı tokenlar {banned}")
    else:
        rep.ok("sarmalayici:emir-yasak", "sarmalayıcı emir yerleştiremez "
                                         "(salt iptal/avcı akışı)")
    if "scan_and_kill" in wrap_src:
        rep.ok("sarmalayici:tara", "orphan_killer.scan_and_kill çağrısı var")
    else:
        rep.fail("sarmalayici:tara", "scan_and_kill çağrısı YOK — "
                                     "watchdog hiçbir şey yapmaz")

    # 6) Avcının baktığı dosyalar = canlı servisin yazdıkları ($HOME) --------
    tracked = re.search(r'TRACKED\s*=\s*Path\.home\(\)\s*/\s*"([^"]+)"',
                        killer_src)
    hb = re.search(r'HEARTBEAT_FILE\s*=\s*Path\.home\(\)\s*/\s*"([^"]+)"',
                   killer_src)
    if tracked and hb:
        rep.ok("av:durum-dosyalari",
               f"~/{tracked.group(1)} ~/{hb.group(1)} (HOME paritesine "
               "bağımlı — bkz. parite:HOME)")
    else:
        rep.fail("av:durum-dosyalari",
                 "TRACKED/HEARTBEAT $HOME sabitleri çözümlenemedi")
    if "position_risk" in killer_src or "positionRisk" in killer_src:
        rep.ok("av:pozisyon-okuma", "positionRisk okuması var")
    else:
        rep.fail("av:pozisyon-okuma", "pozisyon okuma YOK")
    if '"code"' in killer_src and "200" in killer_src:
        rep.ok("av:iptal-teyidi", "cancel sonucu code==200 teyidi var")
    else:
        rep.warn("av:iptal-teyidi", "200 teyidi çözümlenemedi — journal "
                                    "takibi zorunlu")

    # 7) FAZ9_STATE_DIR tuzağı: env dosyası erişilebilirse doğrula -----------
    for ef in (w_ef or []):
        if not os.path.isfile(ef):
            rep.info("env:erisim", f"{ef} bu makinede yok — operatör "
                                   "sunucusunda MANUAL doğrulanmalı")
            continue
        try:
            env_txt = open(ef, encoding="utf-8").read()
        except OSError as e:
            rep.warn("env:erisim", f"{ef} okunamadı: {e}")
            continue
        if re.search(r"^\s*FAZ9_STATE_DIR\s*=", env_txt, re.M):
            rep.fail("env:STATE_DIR-tuzağı", f"{ef} FAZ9_STATE_DIR AYARLIOR — "
                                             "avcı $HOME'da kör kalır "
                                             "(ya kaldır ya avcıyı güncelle)")
        else:
            rep.ok("env:STATE_DIR-tuzağı", "FAZ9_STATE_DIR yok → "
                                           "STATE_DIR==$HOME (tutarlı)")
        if re.search(r"^\s*BINANCE_(KEY|SECRET)\s*=\s*\S+", env_txt, re.M):
            rep.ok("env:anahtarlar", "BINANCE_KEY/SECRET tanımlı "
                                     "(DEĞERLER BURADA YAZILMAZ)")
        else:
            rep.warn("env:anahtarlar", "anahtarlar bulunamadı — sarmalayıcı "
                                       "her tur exit 2 verir (fail-closed, "
                                       "sessiz değil)")
    # 8) Bot tarafı: STATE_DIR = FAZ9_STATE_DIR or home -----------------------
    if re.search(r'STATE_DIR\s*=\s*Path\(os\.getenv\("FAZ9_STATE_DIR"\)\s*or\s*Path\.home\(\)\)',
                 at_src):
        rep.ok("bot:STATE_DIR", "auto_trader STATE_DIR = env or $HOME — "
                                "parite HOME+env kontrolüyle garanti")
    else:
        rep.warn("bot:STATE_DIR", "STATE_DIR tanımı beklenmeyen biçimde — "
                                  "elle incele")
    return EXIT_FAIL if rep.n_fail else EXIT_OK


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if len(argv) > 1:
        root = argv[1]
    rep = Report()
    rc = check_pairity(root, rep)
    n = {"OK": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
    for lvl, _, _ in rep.lines:
        n[lvl] += 1
    print(f"--- verify_watchdog: OK={n['OK']} WARN={n['WARN']} "
          f"FAIL={n['FAIL']} INFO={n['INFO']} → {'PASS' if rc == 0 else 'RED'}")
    print("NOT:Bu araç kurulum YAPMAZ; timer enable/daemon-reload operatör "
          "onaylı adımdır (docs/OPS_WATCHDOG_20261121.md).")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
