#!/usr/bin/env python3
"""canary_account_probe.py — AŞAMA B gerçek veri probu (salt-okunur).

KULLANIM:
  python3 scripts/canary_account_probe.py --rest-baseline [--uds-check]
      [--uds-duration SANIYE] [--json ÇIKTI_DOSYASI]

NE YAPAR:
  --rest-baseline : 7 salt-okunur imzalı GET (accountConfig, v2/v3
                    account, v3 balance, positionRisk filtresiz,
                    openOrders, openAlgoOrders) + ağırlık/zaman kaydı.
  --uds-check     : listenKey POST + PUT (DELETE YOK) + süre boyunca WS
                    gözlem; olay yoksa dürüstçe
                    CONNECTED_NO_ACCOUNT_EVENT_OBSERVED raporlar.

GÜVENLİK:
  - EMİR YOK; listenKey DELETE YOK; DB yazımı YOK; servis dokunulmaz.
  - API anahtarı/secret/listenKey/imza HİÇBİR çıktıya yazılmaz: yalnız
    ortam değişkeni VARLIK kontrolü yapılır, değerleri ASLA yazdırılmaz.
  - Çıktı redakte edilmiştir (redact() + to_safe_dict sözleşmeleri).
  - Bağlantılar sonunda kapanır (ws.close, session.close).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canary_account_adapter import BinanceHttpTransport, AccountAdapter
from canary_account_reconcile import reconcile
from canary_account_uds import UdsClient


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Salt-okunur hesap veri probu (AŞAMA B)")
    ap.add_argument("--rest-baseline", action="store_true",
                    help="7 imzalı salt-okunur GET baseline'ı topla")
    ap.add_argument("--uds-check", action="store_true",
                    help="listenKey + WS gözlem (DELETE yok)")
    ap.add_argument("--uds-duration", type=float, default=15.0,
                    help="UDS gözlem süresi (saniye, varsayılan 15)")
    ap.add_argument("--json", metavar="DOSYA", default=None,
                    help="redakte JSON raporu dosyaya yaz (değerler girmez)")
    args = ap.parse_args()

    if not (args.rest_baseline or args.uds_check):
        ap.error("en az bir mod gerekli: --rest-baseline ve/veya --uds-check")

    api_key = os.environ.get("BINANCE_KEY")
    api_secret = os.environ.get("BINANCE_SECRET")
    # Yalnız VARLIK kontrolü — değerler ASLA yazdırılmaz.
    print(f"BINANCE_KEY ortamda {'SET' if api_key else 'UNSET'}")
    print(f"BINANCE_SECRET ortamda {'SET' if api_secret else 'UNSET'}")
    if not api_key or not api_secret:
        print("HATA: kimlik bilgileri yok — prob çalıştırılamaz.")
        return 2

    report = {"probe_started_utc": None, "rest_baseline": None,
              "uds": None, "reconciliation": None, "verdict": None}

    transport = BinanceHttpTransport(api_key, api_secret)
    try:
        baseline = uds_result = verdict = None

        if args.rest_baseline:
            print("\n=== REST BASELINE (salt-okunur, 7 endpoint) ===")
            baseline = AccountAdapter(transport).collect_baseline()
            print(json.dumps(baseline.to_safe_dict(), indent=2,
                             ensure_ascii=False))
            report["rest_baseline"] = baseline.to_safe_dict()

        if args.uds_check:
            print(f"\n=== UDS GÖZLEM ({args.uds_duration:.0f}s; DELETE yok) ===")
            uds_result = UdsClient(
                transport, duration_s=args.uds_duration).run_probe()
            print(json.dumps(uds_result.to_safe_dict(), indent=2,
                             ensure_ascii=False))
            report["uds"] = uds_result.to_safe_dict()

        if baseline is not None:
            verdict = reconcile(baseline, uds_result)
            print("\n=== MUTABAKAT ===")
            print(json.dumps(verdict.to_safe_dict(), indent=2,
                             ensure_ascii=False))
            report["reconciliation"] = verdict.to_safe_dict()

        # Tek cümlelik sonuç (dürüst; başarı VARSAŞI YOK)
        if verdict is None:
            report["verdict"] = ("UDS gözlemi tamamlandı; REST baseline "
                                 "toplanmadı.")
        elif verdict.state == "RECONCILED":
            report["verdict"] = "VERİ KATMANI DOĞRULANDI (bu çalışmada)."
        else:
            report["verdict"] = (
                f"VERİ KATMANI TAM DOĞRULANAMADI — durum: {verdict.state}; "
                "eksikler yukarıdaki kayıtlarda dürüstçe listelendi.")
        print("\nSONUÇ:", report["verdict"])
    finally:
        try:
            transport.session.close()
        except Exception:
            pass

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"Redakte JSON rapor: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
