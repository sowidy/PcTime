"""Exercise HTTP validation/auth before reaching mocked MySQL boundaries."""
from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock
from uuid import uuid4
import asyncio
import json
from types import SimpleNamespace

import app
from api import tracking
from db import conn as db_conn


class ASGIClient:
    """Small HTTP ASGI harness; no external HTTP client or lifespan side effects."""
    def request(self, method, path, payload=None, headers=None):
        async def call():
            sent, received = [], False
            async def receive():
                nonlocal received
                if received:
                    return {"type": "http.disconnect"}
                received = True
                return {"type": "http.request", "body": json.dumps(payload).encode() if payload is not None else b"",
                        "more_body": False}
            async def send(message):
                sent.append(message)
            await app.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "root_path": "", "server": ("localhost", 80),
                "client": ("127.0.0.1", 12345),
                "headers": [(b"content-type", b"application/json")] +
                           [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}, receive, send)
            body = b"".join(m.get("body", b"") for m in sent)
            return SimpleNamespace(status_code=sent[0]["status"], json=lambda: json.loads(body))
        return asyncio.run(call())

    def post(self, path, json=None, headers=None):
        return self.request("POST", path, json, headers)

    def delete(self, path):
        return self.request("DELETE", path)


class TrackingRouteTests(TestCase):
    def setUp(self):
        self.client = ASGIClient()
        self.key = str(uuid4())
        self.meta = {"user_key": self.key, "hostname": "test", "platform": "test", "client_version": "test"}

    def test_unauthorized_write_does_not_touch_mysql(self):
        with mock.patch.object(db_conn, "connect") as connect:
            response = self.client.post("/api/v1/web-usage/upload", json={**self.meta,
                "days": [{"day": "2026-10-21", "sites": []}]})
        self.assertEqual(response.status_code, 401)
        connect.assert_not_called()

    def test_title_and_url_fields_rejected_at_boundary(self):
        with mock.patch.object(db_conn, "connect") as connect:
            response = self.client.post("/api/v1/app-timeline/upload", json={**self.meta,
                "days": [{"day": "2026-10-21", "bins": [{"bucket": 0, "app_name": "a.exe",
                "seconds": 10, "title": "private"}]}]})
        self.assertEqual(response.status_code, 422)
        connect.assert_not_called()

    def test_duplicate_days_and_unfinished_complete_refused(self):
        day = {"day": str(date.today()), "sites": [], "complete": True}
        with mock.patch.object(tracking, "authenticate"), mock.patch.object(db_conn, "connect") as connect:
            response = self.client.post("/api/v1/web-usage/upload", json={**self.meta, "days": [day]})
            self.assertEqual(response.status_code, 422)
            day["complete"] = False
            response = self.client.post("/api/v1/web-usage/upload", json={**self.meta, "days": [day, day]})
            self.assertEqual(response.status_code, 422)
            connect.assert_not_called()

    def test_empty_snapshot_is_committed_and_date_scoped(self):
        cur = mock.MagicMock()
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cur
        @contextmanager
        def connect():
            yield connection
        with mock.patch.object(tracking, "authenticate"), mock.patch.object(db_conn, "connect", connect):
            response = self.client.post("/api/v1/web-usage/upload", json={**self.meta,
                "days": [{"day": "2026-10-21", "sites": [], "complete": True}]})
        self.assertEqual(response.status_code, 200)
        delete = [(sql, params) for sql, params in (c.args for c in cur.execute.call_args_list)
                  if sql.startswith("DELETE")]
        self.assertEqual(len(delete), 5)
        self.assertTrue(all(params[:2] == (self.key, date(2026, 10, 21))
                            for _, params in delete))

    def test_web_upload_persists_real_hourly_bins_atomically(self):
        cur = mock.MagicMock()
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cur
        @contextmanager
        def connect():
            yield connection
        payload = {**self.meta, "days": [{"day": "2026-10-21", "complete": True,
            "sites": [{"host": "github.com", "seconds": 50}],
            "hourly": [{"hour": 9, "host": "github.com", "seconds": 20},
                       {"hour": 10, "host": "github.com", "seconds": 30}],
            "intervals": [{"host": "github.com", "start_second": 32400, "end_second": 32420},
                          {"host": "github.com", "start_second": 36000, "end_second": 36030}]}]}
        with mock.patch.object(tracking, "authenticate"), mock.patch.object(db_conn, "connect", connect):
            response = self.client.post("/api/v1/web-usage/upload", json=payload)
        self.assertEqual(response.status_code, 200)
        inserts = [sql for sql, _ in (c.args for c in cur.executemany.call_args_list)]
        self.assertTrue(any("website_usage_records" in sql for sql in inserts))
        self.assertTrue(any("website_hourly_usage" in sql for sql in inserts))
        self.assertTrue(any("website_intervals" in sql for sql in inserts))

    def test_different_token_cannot_reclaim_existing_device(self):
        cur = mock.MagicMock()
        cur.fetchone.side_effect = [{"user_key": self.key}, {"token_hash": tracking.token_hash("a" * 43)}]
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cur
        with mock.patch.object(db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value = connection
            response = self.client.post("/api/v1/device/register", json=self.meta,
                                        headers={"Authorization": "Bearer " + "b" * 43})
        self.assertEqual(response.status_code, 409)

    def test_admin_reset_requires_admin_token(self):
        with mock.patch.object(db_conn, "connect") as connect:
            response = self.client.delete("/api/v1/admin/device-token/" + self.key)
        self.assertIn(response.status_code, (401, 403))
        connect.assert_not_called()
