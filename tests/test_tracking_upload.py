"""Opt-in, transport and independent-upload failure behavior."""
import os
import tempfile
from unittest import TestCase, mock

from core import storage, config
from net import tracking_upload


class TrackingUploadTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.object(storage, "DB_PATH", os.path.join(temp.name, "usage.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.init_db()

    def test_default_never_connects(self):
        with mock.patch.object(tracking_upload, "post") as post:
            self.assertEqual(tracking_upload.run_once()["reason"], "disabled")
        post.assert_not_called()

    def test_remote_plaintext_never_sends_token(self):
        storage.set_setting("timeline_upload_enabled", "1")
        with mock.patch.object(config, "SERVER_URL", "http://203.0.113.1"), \
                mock.patch.object(tracking_upload, "post") as post:
            self.assertEqual(tracking_upload.run_once()["reason"], "https_required")
        post.assert_not_called()

    def test_one_dataset_failure_does_not_advance_its_watermark_or_block_other(self):
        storage.set_setting("timeline_upload_enabled", "1")
        storage.set_setting("web_upload_enabled", "1")
        def post(path, payload, token):
            self.assertEqual(storage.get_setting("device_token_" + __import__("hashlib").sha256(
                config.base_url().encode()).hexdigest()[:16]), token)
            if path == "/web-usage/upload":
                raise tracking_upload.UploadFailure(404)
            return {"ok": True}
        with mock.patch.object(config, "SERVER_URL", "https://example.com"), \
                mock.patch.object(tracking_upload, "post", side_effect=post):
            result = tracking_upload.run_once()
        self.assertEqual(result["timeline"], "ok")
        self.assertEqual(result["web"], "server_unsupported")
        self.assertFalse(storage.get_setting("web_upload_watermark"))
        self.assertIsNotNone(storage.get_setting("timeline_upload_watermark"))

    def test_first_upgrade_backfills_real_hourly_sites_for_recent_days_once(self):
        storage.set_setting("web_upload_enabled", "1")
        storage.set_setting("web_upload_watermark", str(__import__("datetime").date.today()))
        sent = []
        def post(path, payload, token):
            if path == "/web-usage/upload":
                sent.append(payload["days"][0])
            return {"ok": True}
        with mock.patch.object(config, "SERVER_URL", "https://example.com"), \
                mock.patch.object(tracking_upload, "post", side_effect=post):
            self.assertEqual(tracking_upload.run_once()["web"], "ok")
            self.assertEqual(len(sent), 14)
            self.assertTrue(all(day["hourly"] == [] for day in sent))
            self.assertTrue(all(day["intervals"] == [] for day in sent))
            self.assertEqual(storage.get_setting("web_hourly_backfill_v1"), "1")
            self.assertEqual(storage.get_setting("web_intervals_backfill_v1"), "1")
            sent.clear()
            self.assertEqual(tracking_upload.run_once()["web"], "ok")
            self.assertEqual(len(sent), 3)

    def test_existing_hourly_installation_backfills_intervals_once(self):
        storage.set_setting("web_upload_enabled", "1")
        storage.set_setting("web_hourly_backfill_v1", "1")
        storage.set_setting("web_upload_watermark", str(__import__("datetime").date.today()))
        sent = []
        def post(path, payload, token):
            if path == "/web-usage/upload":
                sent.append(payload["days"][0])
            return {"ok": True}
        with mock.patch.object(config, "SERVER_URL", "https://example.com"), \
                mock.patch.object(tracking_upload, "post", side_effect=post):
            self.assertEqual(tracking_upload.run_once()["web"], "ok")
        self.assertEqual(len(sent), 14)
        self.assertEqual(storage.get_setting("web_intervals_backfill_v1"), "1")

    def test_redirects_are_not_followed_with_bearer(self):
        self.assertIsNone(tracking_upload.NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.test"))
