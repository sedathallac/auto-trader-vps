"""F3/F4 regression: a non-idempotent write must never be re-sent automatically.

Finding 2026-11-11 (findings/2026-11-11_duplicate_submission_risk.md): the shared
requests Session mounts urllib3 Retry with allowed_methods GET+POST+DELETE and
status_forcelist 5xx. If HB/Binance processes a POST and the response is a 5xx,
the same POST is re-sent -> a second executor = duplicate position.

A real local HTTP server is used (no mocks of requests/urllib3): it PROCESSES
every POST (counts it) but answers 503 to the first one.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
auto_trader = pytest.importorskip("auto_trader")


class _Server:
    def __init__(self, first_status=503):
        self.posts = []
        self.gets = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                if self.path.startswith("/executors/search"):
                    # report what was actually created so far
                    data = [{"executor_id": f"E{i}", "trading_pair": "SOL-USDT",
                             "status": "RUNNING"} for i, _ in enumerate(outer.posts)]
                    return self._reply(200, {"data": data})
                outer.posts.append(body)          # side effect happened
                if len(outer.posts) == 1:
                    return self._reply(first_status, {"detail": "gateway"})
                return self._reply(200, {"executor_id": f"E{len(outer.posts) - 1}"})

            def do_GET(self):
                outer.gets += 1
                if outer.gets == 1:
                    return self._reply(503, {})
                return self._reply(200, {"ok": True})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def srv():
    s = _Server()
    yield s
    s.close()


def test_create_executor_not_resent_after_5xx(srv):
    hb = auto_trader.HBAPI(srv.base, "u", "p")
    try:
        hb.create_executor("SOLUSDT", 0.3, 3, 0.01, 0.02, 3600, side=1)
    except Exception:
        pass  # an error / UNKNOWN outcome is acceptable; a resend is not
    assert len(srv.posts) == 1, f"executor POST sent {len(srv.posts)}x -> duplicate position"


def test_binance_write_not_resent_after_5xx(srv, monkeypatch):
    monkeypatch.setattr(auto_trader, "BINANCE_FAPI", srv.base)
    b = auto_trader.BinanceAPI("k", "s")
    try:
        b._post("/fapi/v1/order", {"symbol": "SOLUSDT", "side": "SELL",
                                   "type": "MARKET", "quantity": "0.3",
                                   "reduceOnly": "true"})
    except Exception:
        pass
    assert len(srv.posts) == 1, f"order POST sent {len(srv.posts)}x"


def test_reads_still_retry_on_5xx(srv, monkeypatch):
    """GET is idempotent: keeping retry for reads is intended."""
    monkeypatch.setattr(auto_trader, "BINANCE_FAPI", srv.base)
    r = auto_trader.BinanceAPI("k", "s")._get("/fapi/v1/ping")
    assert r == {"ok": True} and srv.gets == 2


def test_create_executor_unknown_outcome_resolved_by_search(srv):
    """After a 5xx the outcome is UNKNOWN: resolve via executor search instead of
    resending. The executor that WAS created must be returned (not lost)."""
    hb = auto_trader.HBAPI(srv.base, "u", "p")
    r = hb.create_executor("SOLUSDT", 0.3, 3, 0.01, 0.02, 3600, side=1)
    assert len(srv.posts) == 1
    assert isinstance(r, dict) and r.get("executor_id") == "E0", r
