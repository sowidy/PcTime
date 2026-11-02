"""Opt-in, transport and independent-upload failure behavior."""
import os
import tempfile
from datetime import date, datetime, timedelta
from unittest import TestCase, mock
from uuid import uuid4

from core import storage, config, site_storage
from net import tracking_upload, uploader


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

    def test_enabled_website_upload_supports_configured_remote_http(self):
        storage.set_setting("web_upload_enabled", "1")
        with mock.patch.object(config, "SERVER_URL", "http://203.0.113.1"), \
                mock.patch.object(tracking_upload, "post") as post:
            self.assertEqual(tracking_upload.run_once(), {"web": "ok"})
        paths = [call.args[0] for call in post.call_args_list]
        self.assertIn("/device/register", paths)
        self.assertIn("/web-usage/upload", paths)
        self.assertNotIn("/app-timeline/upload", paths)
        self.assertEqual(storage.get_setting("web_upload_status"), "ok")

    def test_manual_upload_preserves_consent_and_recovers_old_website_snapshots(self):
        storage.set_setting("upload_enabled", "0")
        storage.set_setting("web_upload_enabled", "1")
        storage.set_setting("web_hourly_backfill_v1", "1")
        storage.set_setting("web_intervals_backfill_v1", "1")
        storage.set_setting("web_upload_watermark", str(date.today()))
        day = date.today() - timedelta(days=20)
        start = datetime.combine(day, datetime.min.time()).replace(hour=9).timestamp()
        site_storage.record_segment({"event_id": str(uuid4()), "session_id": str(uuid4()),
                                     "browser": "chrome", "host": "example.com"}, [(start, start + 600)])
        with mock.patch.object(tracking_upload, "post") as post:
            self.assertEqual(tracking_upload.run_once(), {"reason": "disabled"})
            post.assert_not_called()
            self.assertEqual(tracking_upload.run_once(force=True), {"web": "ok"})
        snapshots = [block for call in post.call_args_list if call.args[0] == "/web-usage/upload"
                     for block in call.args[1]["days"]]
        historical = next(block for block in snapshots if block["day"] == str(day))
        self.assertEqual(historical["sites"], [{"host": "example.com", "display_name": "", "seconds": 600}])
        self.assertEqual(sum(row["seconds"] for row in historical["hourly"]), 600)
        self.assertEqual(historical["intervals"], [{"host": "example.com", "start_second": 32400,
                                                  "end_second": 33000}])
        self.assertEqual(storage.get_setting("upload_enabled"), "0")
        self.assertNotIn("/app-timeline/upload", [call.args[0] for call in post.call_args_list])

    def test_manual_upload_never_enables_unapproved_details(self):
        with mock.patch.object(tracking_upload, "post") as post:
            self.assertEqual(tracking_upload.run_once(force=True), {"reason": "disabled"})
        post.assert_not_called()

    def test_registration_failure_is_reported_without_marking_success(self):
        storage.set_setting("web_upload_enabled", "1")
        with mock.patch.object(tracking_upload, "post", side_effect=tracking_upload.UploadFailure(409)):
            self.assertEqual(tracking_upload.run_once(), {"web": "HTTP 409"})
        self.assertEqual(storage.get_setting("web_upload_status"), "HTTP 409")
        self.assertFalse(storage.get_setting("web_upload_watermark"))
        self.assertFalse(storage.get_setting("web_upload_last_success"))

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
                sent.extend(payload["days"])
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
                sent.extend(payload["days"])
            return {"ok": True}
        with mock.patch.object(config, "SERVER_URL", "https://example.com"), \
                mock.patch.object(tracking_upload, "post", side_effect=post):
            self.assertEqual(tracking_upload.run_once()["web"], "ok")
        self.assertEqual(len(sent), 14)
        self.assertEqual(storage.get_setting("web_intervals_backfill_v1"), "1")

    def test_redirects_are_not_followed_with_bearer(self):
        self.assertIsNone(tracking_upload.NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.test"))

    def test_failed_batch_preserves_retry_progress_until_all_days_are_accepted(self):
        storage.set_setting("web_upload_enabled", "1")
        def post(path, payload, token):
            if path == "/web-usage/upload" and payload["days"][-1]["day"] == str(date.today()):
                raise tracking_upload.UploadFailure(503)
            return {"ok": True}
        with mock.patch.object(tracking_upload, "post", side_effect=post):
            self.assertEqual(tracking_upload.run_once()["web"], "HTTP 503")
        self.assertEqual(storage.get_setting("web_upload_watermark"), str(date.today() - timedelta(days=7)))
        self.assertFalse(storage.get_setting("web_upload_last_success"))
        self.assertFalse(storage.get_setting("web_intervals_backfill_v1"))
        with mock.patch.object(tracking_upload, "post") as post:
            self.assertEqual(tracking_upload.run_once()["web"], "ok")
        self.assertEqual(storage.get_setting("web_upload_watermark"), str(date.today()))
        self.assertTrue(all(len(call.args[1]["days"]) <= 7 for call in post.call_args_list
                            if call.args[0] == "/web-usage/upload"))


class CombinedUploadTests(TestCase):
    def setUp(self):
        self.app_result = {"ok": True, "reason": "ok", "days": 7, "apps": 61, "error": None}

    def test_manual_upload_dispatches_both_paths(self):
        with mock.patch.object(uploader, "upload_pending", return_value=self.app_result) as apps, \
                mock.patch.object(tracking_upload, "run_once", return_value={"web": "ok"}) as details:
            result = uploader.upload_all(force=True)
        apps.assert_called_once_with(force=True)
        details.assert_called_once_with(force=True)
        self.assertTrue(result["ok"])
        self.assertIn("网站：上传成功", uploader.describe_result(result))

    def test_website_failure_is_not_reported_as_complete_success(self):
        with mock.patch.object(uploader, "upload_pending", return_value=self.app_result), \
                mock.patch.object(tracking_upload, "run_once", return_value={"web": "server_unsupported"}):
            result = uploader.upload_all(force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "partial_upload")
        self.assertTrue(result["apps_ok"])
        message = uploader.describe_result(result)
        self.assertIn("应用：上传成功", message)
        self.assertIn("网站：服务端不支持", message)

    def test_application_failure_does_not_block_website_sync(self):
        failed = {**self.app_result, "ok": False, "reason": "error", "error": "connection refused"}
        with mock.patch.object(uploader, "upload_pending", return_value=failed), \
                mock.patch.object(tracking_upload, "run_once", return_value={"web": "ok"}) as details:
            result = uploader.upload_all()
        details.assert_called_once_with(force=False)
        self.assertFalse(result["ok"])
        self.assertIn("网站：上传成功", uploader.describe_result(result))

    def test_disabled_optional_details_do_not_fail_application_upload(self):
        with mock.patch.object(uploader, "upload_pending", return_value=self.app_result), \
                mock.patch.object(tracking_upload, "run_once", return_value={"reason": "disabled"}):
            self.assertTrue(uploader.upload_all()["ok"])
