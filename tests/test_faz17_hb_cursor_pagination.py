"""FAZ17 — HB /executors/search GERÇEK cursor sözleşmesi (canlı probe kilidi).

Kök neden (doğrudan probe kanıtı 2026-11-19 07:35–07:40Z, localhost:8000):
  * `offset`, `page`, `is_active` istek parametreleri HB tarafından YOK SAYILIR
    (offset=0/55/200 → aynı 55 kayıt).
  * `status` filtresi çalışır.
  * Gerçek sözleşme: yanıtta `pagination` = {limit, has_more, next_cursor,
    total_count}; `next_cursor` keyset "<created_at>:<executor_id>"; istek
    parametresi `cursor`. Tam tur: limit=10 → 6 sayfa → 55 tekil id,
    has_more=False, toplam == total_count.

79bed1d'nin offset tabanlı tamamlanma kanıtı bu HB'de HİÇ tamamlanamıyordu →
canlı sync kalıcı PARTIAL (flat-sweep/pnl-recovery ölü). Bu test gerçek HB
semantiğini birebir taklit eden sunucuyla sözleşmeyi sabitler:
  tamamlanma YALNIZ has_more=False + total_count mutabakatı; cursor/has_more/
  pagination bozuksa KISMİ (fail-closed).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_trader as at


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _HB:
    auth = ("u", "p")


def _rows(n, start=0):
    return [{"executor_id": f"E{i:04d}", "status": "TERMINATED"}
            for i in range(start, start + n)]


class _RealHBServer:
    """Canlı probe'un birebir taklidi: offset/page yok sayar, cursor işler."""

    def __init__(self, total=55, break_cursor=False, break_total=False,
                 drop_pagination=False, empty_with_more=False, dup_once=False):
        self.all = _rows(total)
        self.total = total
        self.break_cursor = break_cursor
        self.break_total = break_total
        self.drop_pagination = drop_pagination
        self.empty_with_more = empty_with_more
        self.dup_once = dup_once
        self.calls = []

    def post(self, url, json=None, **k):
        payload = json or {}
        self.calls.append(dict(payload))
        # offset/page YOK SAYILIR (canlı HB gibi): yalnız limit + cursor.
        limit = int(payload.get("limit") or 100)
        cur = payload.get("cursor")
        start = 0
        if cur:
            if not cur.startswith("ts:"):
                return _Resp({"data": [], "pagination": {
                    "limit": limit, "has_more": False, "next_cursor": None,
                    "total_count": self.total}})
            start = int(cur.split(":", 1)[1])
        page = self.all[start:start + limit]
        if self.dup_once and start >= limit and page:
            page = [self.all[0]] + page  # keyset'te beklenmeyen çift kayıt
        nxt = start + len(page)
        has_more = nxt < self.total
        body = {"data": page, "pagination": {
            "limit": limit,
            "has_more": has_more,
            "next_cursor": f"ts:{nxt}" if has_more else None,
            "total_count": self.total,
        }}
        if self.empty_with_more and not page:
            body["pagination"]["has_more"] = True
            body["pagination"]["next_cursor"] = "ts:9999"
        if self.break_cursor and has_more:
            body["pagination"]["next_cursor"] = "ts:0"  # ilerlemeyen cursor
        if self.break_total:
            body["pagination"]["total_count"] = self.total + 1  # asla tutmaz
        if self.drop_pagination:
            body.pop("pagination")
        return _Resp(body)


class TestCursorContract(unittest.TestCase):
    def setUp(self):
        self._orig = at.requests.post
        self.addCleanup(setattr, at.requests, "post", self._orig)

    def _run(self, server, **kw):
        at.requests.post = server.post
        return at._hb_executors_all(_HB(), **kw)

    def test_single_page_completion(self):
        srv = _RealHBServer(total=55)
        out, err = self._run(srv, page_limit=200)
        self.assertIsNone(err, err)
        self.assertEqual(len(out), 55)
        self.assertEqual(len(srv.calls), 1, "tek sayfada cursor denenmemeli")
        self.assertNotIn("offset", srv.calls[0], "offset gönderilmemeli (ignored)")

    def test_cursor_walk_completes_with_proof(self):
        srv = _RealHBServer(total=55)
        out, err = self._run(srv, page_limit=10)
        self.assertIsNone(err, err)
        self.assertEqual(len(out), 55)
        self.assertEqual(len({e["executor_id"] for e in out}), 55)
        self.assertGreaterEqual(len(srv.calls), 6, "cursor turu başlamadı")
        # İlk çağrıda cursor yok, sonrakilerde sunucunun next_cursor'ı kullanılmalı.
        self.assertNotIn("cursor", srv.calls[0])
        self.assertIn("cursor", srv.calls[1])

    def test_empty_account_completes(self):
        srv = _RealHBServer(total=0)
        out, err = self._run(srv, page_limit=200)
        self.assertIsNone(err, err)
        self.assertEqual(out, [])

    def test_missing_pagination_meta_is_incomplete(self):
        srv = _RealHBServer(total=3, drop_pagination=True)
        out, err = self._run(srv, page_limit=200)
        self.assertTrue(err and err.startswith("incomplete:"), err)
        self.assertEqual(len(out), 3, "toplanan kayıplar korunur")

    def test_stuck_cursor_is_incomplete(self):
        srv = _RealHBServer(total=30, break_cursor=True)
        out, err = self._run(srv, page_limit=10)
        self.assertTrue(err and err.startswith("incomplete:"), err)
        self.assertIn("cursor", err)

    def test_total_count_mismatch_is_incomplete(self):
        srv = _RealHBServer(total=12, break_total=True)
        out, err = self._run(srv, page_limit=10)
        self.assertTrue(err and err.startswith("incomplete:"), err)
        self.assertIn("total_count", err)

    def test_empty_page_with_has_more_true_is_incomplete(self):
        srv = _RealHBServer(total=5, empty_with_more=True)
        out, err = self._run(srv, page_limit=10)
        # İlk sayfa 5 kaydı döner, has_more False olur → normal tamamlanma.
        # empty_with_more yalnız BOŞ sayfada devreye girer; burada boş sayfa
        # hiç görülmez. Ayrı senaryo: all boş + has_more True.
        self.assertIsNone(err, err)
        srv2 = _RealHBServer(total=0, empty_with_more=True)
        out2, err2 = self._run(srv2, page_limit=10)
        self.assertTrue(err2 and err2.startswith("incomplete:"), err2)

    def test_duplicate_executor_id_is_incomplete(self):
        srv = _RealHBServer(total=12, dup_once=True)
        out, err = self._run(srv, page_limit=10)
        self.assertTrue(err and err.startswith("incomplete:"), err)
        self.assertIn("yinelenen", err)

    def test_invalid_executor_id_kept_and_incomplete(self):
        # 79bed1d değişmezi: çözümlenmemiş kayıt atılmaz, liste KISMİ döner.
        class _S:
            def post(self, url, json=None, **k):
                return _Resp({"data": [{"status": "X"}], "pagination": {
                    "limit": 200, "has_more": False, "next_cursor": None,
                    "total_count": 1}})
        at.requests.post = _S().post
        out, err = at._hb_executors_all(_HB(), page_limit=200)
        self.assertTrue(err and err.startswith("incomplete:"), err)
        self.assertEqual(len(out), 1)

    def test_max_pages_guard(self):
        srv = _RealHBServer(total=10_000)
        out, err = self._run(srv, page_limit=10, max_pages=5)
        self.assertTrue(err and err.startswith("incomplete:"), err)
        self.assertEqual(len(out), 50)


if __name__ == "__main__":
    unittest.main()
