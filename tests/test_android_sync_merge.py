"""Android sync merge: unlocks storage + per-mailbox merged mail.

Server-side only, mock DB, never a real database or real mail. Follows the
same patching discipline as tests/test_mailer.py and
tests/test_tracking_routes.py: patch the name where the code under test
actually looks it up.
"""
import unittest
from datetime import date, timedelta
from unittest import mock

from api import tracking as tracking_api
from api.tracking_schemas import TimelineDay, TimelineUpload
from db import mail_log, tracking, usage_records, users
from mail import merge, report, sender

U_PC = "11111111-1111-4111-8111-111111111111"
U_PHONE = "22222222-2222-4222-8222-222222222222"
MAIL = "User@Example.com "
DAY = date(2026, 11, 6)  # Friday: daily mail, not the weekly day
SUNDAY = date(2026, 10, 18)


def device(key, email=MAIL, hostname="PC", created="2026-01-01 00:00:00"):
    return {"user_key": key, "email": email, "hostname": hostname,
            "platform": "Windows", "created_at": created}


def summary(total, apps):
    return {"day": DAY.isoformat(), "total_seconds": total,
            "app_count": len(apps), "apps": apps}


def app_row(name, seconds, launches=1, display=""):
    return {"app_name": name, "display_name": display or name,
            "seconds": seconds, "launches": launches}


class TestNormalizeAndGroup(unittest.TestCase):
    def test_email_key_is_lowercase_and_stripped(self):
        self.assertEqual(merge.normalize_email("  User@Example.COM "), "user@example.com")
        self.assertEqual(merge.normalize_email(None), "")
        self.assertEqual(merge.normalize_email(""), "")

    def test_same_mailbox_different_case_and_space_is_one_group(self):
        rows = [device(U_PC, "User@Example.com", "PC", "2026-01-02 00:00:00"),
                device(U_PHONE, " user@example.com ", "Pixel", "2026-01-01 00:00:00")]
        groups = merge.group_devices_by_email(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["email"], "user@example.com")
        # Anchor is the earliest-created device, not the first row.
        self.assertEqual(groups[0]["devices"][0]["user_key"], U_PHONE)
        self.assertEqual(groups[0]["devices"][1]["user_key"], U_PC)

    def test_distinct_mailboxes_stay_separate_and_no_email_rows_drop(self):
        rows = [device(U_PC, "a@example.com", "PC"),
                device(U_PHONE, "b@example.com", "Pixel"),
                device("33333333-3333-4333-8333-333333333333", "", "NoMail"),
                device("44444444-4444-4334-8444-444444444444", None, "NoMail2")]
        groups = merge.group_devices_by_email(rows)
        self.assertEqual([g["email"] for g in groups], ["a@example.com", "b@example.com"])

    def test_single_device_group_keeps_send_day_path(self):
        groups = merge.group_devices_by_email([device(U_PC)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["devices"]), 1)


class TestMergeSummaries(unittest.TestCase):
    def test_totals_ranking_and_launches_add_up(self):
        pc = summary(3600, [app_row("wechat", 2000, 5), app_row("chrome.exe", 1600, 3)])
        phone = summary(1800, [app_row("wechat", 1200, 9), app_row("douyin", 600, 2)])
        merged = merge.merge_app_summaries([("PC", pc), ("Pixel", phone)])
        self.assertEqual(merged["total_seconds"], 5400)
        self.assertEqual(merged["app_count"], 3)
        self.assertEqual(merged["apps"][0]["app_name"], "wechat")
        self.assertEqual(merged["apps"][0]["seconds"], 3200)
        self.assertEqual(merged["apps"][0]["launches"], 14)
        self.assertEqual([d["total_seconds"] for d in merged["_devices"]], [3600, 1800])

    def test_all_none_is_none_and_partial_none_keeps_zero_device(self):
        self.assertIsNone(merge.merge_app_summaries([("PC", None), ("Pixel", None)]))
        merged = merge.merge_app_summaries([("PC", None), ("Pixel", summary(60, [app_row("a", 60)]))])
        self.assertEqual(merged["total_seconds"], 60)
        self.assertEqual(merged["_devices"][0]["total_seconds"], 0)

    def test_display_name_keeps_first_non_empty(self):
        pc = summary(10, [{"app_name": "wechat", "display_name": "", "seconds": 10, "launches": 1}])
        phone = summary(10, [{"app_name": "wechat", "display_name": "微信", "seconds": 10, "launches": 1}])
        merged = merge.merge_app_summaries([("PC", pc), ("Pixel", phone)])
        self.assertEqual(merged["apps"][0]["display_name"], "微信")


class TestOverlap(unittest.TestCase):
    def test_disjoint_is_zero_and_touching_is_zero(self):
        self.assertEqual(merge.compute_overlap_seconds([[(0, 100)], [(100, 200)]]), 0)
        self.assertEqual(merge.compute_overlap_seconds([[(0, 100)], [(200, 300)]]), 0)

    def test_partial_and_nested_overlap(self):
        self.assertEqual(merge.compute_overlap_seconds([[(0, 100)], [(50, 150)]]), 50)
        self.assertEqual(merge.compute_overlap_seconds([[(0, 1000)], [(100, 200)]]), 100)

    def test_three_devices_count_once_per_second(self):
        # 0-50 covered by 2, 50-100 by 3, 100-150 by 2, 150-200 by 1:
        # overlap is 150, not the sum of pairwise overlaps.
        intervals = [[(0, 100)], [(0, 200)], [(50, 150)]]
        self.assertEqual(merge.compute_overlap_seconds(intervals), 150)

    def test_invalid_spans_ignored(self):
        self.assertEqual(merge.compute_overlap_seconds([[(100, 100)], [(0, 10)]]), 0)


class TestUnionIntervalsAndBins(unittest.TestCase):
    def test_same_app_overlapping_spans_collapse(self):
        rows = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 600},
                {"day": str(DAY), "app_name": "wechat", "start_second": 300, "end_second": 900},
                {"day": str(DAY), "app_name": "douyin", "start_second": 0, "end_second": 300}]
        union = merge.union_intervals_by_app(rows)
        self.assertEqual(len(union), 2)
        wechat = [r for r in union if r["app_name"] == "wechat"][0]
        self.assertEqual((wechat["start_second"], wechat["end_second"]), (0, 900))

    def test_union_bins_split_at_15_minute_boundaries(self):
        union = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 1800}]
        bins = merge.union_bins_from_intervals(union)
        self.assertEqual([(b["bucket"], b["seconds"]) for b in bins], [(0, 900), (1, 900)])
        self.assertEqual(sum(b["seconds"] for b in bins), 1800)

    def test_union_total_equals_phone_plus_pc_minus_same_app_overlap(self):
        rows = [{"day": str(DAY), "app_name": "wechat", "start_second": 0, "end_second": 600},
                {"day": str(DAY), "app_name": "wechat", "start_second": 300, "end_second": 900}]
        bins = merge.union_bins_from_intervals(merge.union_intervals_by_app(rows))
        self.assertEqual(sum(b["seconds"] for b in bins), 900)  # 600+600-300

    def test_daily_apps_from_bins(self):
        bins = [{"day": str(DAY), "bucket": 0, "app_name": "wechat", "seconds": 900},
                {"day": str(DAY), "bucket": 1, "app_name": "wechat", "seconds": 900}]
        daily = merge.daily_apps_from_bins(bins)
        self.assertEqual(daily, [{"day": str(DAY), "app_name": "wechat", "seconds": 1800}])


class TestTimelineUnlocksSchema(unittest.TestCase):
    def test_old_payload_without_unlocks_still_parses_as_none(self):
        day = TimelineDay.model_validate(
            {"day": "2026-11-06",
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]})
        self.assertIsNone(day.unlocks)

    def test_unlocks_accepted(self):
        day = TimelineDay.model_validate(
            {"day": "2026-11-06", "unlocks": 42,
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]})
        self.assertEqual(day.unlocks, 42)

    def test_unlocks_rejects_negative_and_non_integer(self):
        for bad in (-1, 1.5, "42", True):
            with self.assertRaises(Exception, msg=repr(bad)):
                TimelineDay.model_validate(
                    {"day": "2026-11-06", "unlocks": bad,
                     "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]})

    def test_extra_fields_still_forbidden(self):
        with self.assertRaises(Exception):
            TimelineUpload.model_validate(
                {"user_key": U_PC, "days": [
                    {"day": "2026-11-06", "unlock": 42,
                     "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]})


class TestUnlocksStorage(unittest.TestCase):
    def test_save_unlocks_upserts_instead_of_accumulating(self):
        cur = mock.MagicMock()
        tracking.save_unlocks(cur, U_PHONE, DAY, 42)
        sql, params = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("android_daily_stats", sql)
        self.assertEqual(params, (U_PHONE, DAY, 42, mock.ANY))

    def test_read_unlocks_filters_by_user_and_range(self):
        fake_conn = mock.MagicMock()
        fake_cur = fake_conn.cursor.return_value.__enter__.return_value
        fake_cur.fetchall.return_value = [
            {"day": DAY, "unlocks": 42, "uploaded_at": "2026-11-06 00:00:00"}]
        with mock.patch.object(tracking.db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value = fake_conn
            rows = tracking.read_unlocks(U_PHONE, DAY, DAY)
        self.assertEqual(rows[0]["unlocks"], 42)
        self.assertEqual(rows[0]["day"], "2026-11-06")
        sql = fake_cur.execute.call_args[0][0]
        self.assertIn("android_daily_stats", sql)


class TestTimelineUploadWritesUnlocks(unittest.TestCase):
    def _upload(self, unlocks):
        req = TimelineUpload.model_validate(
            {"user_key": U_PHONE, "hostname": "Pixel", "platform": "Android 14",
             "client_version": "1.8.0", "bucket_minutes": 15,
             "days": [{"day": "2026-11-06",
                       **({"unlocks": unlocks} if unlocks is not None else {}),
                       "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]})
        cur = mock.MagicMock()
        store = mock.MagicMock()
        with mock.patch.object(tracking_api, "authenticate"), \
                mock.patch.object(tracking_api.users, "upsert_user"), \
                mock.patch.object(tracking_api, "store", store), \
                mock.patch.object(tracking_api.db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur
            result = tracking_api.upload_dataset("timeline", req, "Bearer " + "t" * 40)
        return result, store, cur

    def test_upload_with_unlocks_calls_save_unlocks(self):
        result, store, _ = self._upload(42)
        self.assertTrue(result["ok"])
        store.save_unlocks.assert_called_once()
        args = store.save_unlocks.call_args[0]
        self.assertEqual((args[1], args[2], args[3]), (U_PHONE, date(2026, 11, 6), 42))

    def test_upload_without_unlocks_does_not_touch_the_table(self):
        result, store, _ = self._upload(None)
        self.assertTrue(result["ok"])
        store.save_unlocks.assert_not_called()

    def test_reupload_overwrites_unlocks_instead_of_adding(self):
        # Same day uploaded twice: the second value replaces the first.
        _, store1, _ = self._upload(42)
        _, store2, _ = self._upload(7)
        self.assertEqual(store1.save_unlocks.call_args[0][3], 42)
        self.assertEqual(store2.save_unlocks.call_args[0][3], 7)


class TestValidationIs4xx(unittest.TestCase):
    """Validation failures are 4xx, so a client that retries only on
    network/429/5xx (see client/net/tracking_upload.py) never retries them."""

    def setUp(self):
        import app as app_module
        from tests.test_tracking_routes import ASGIClient
        self.client = ASGIClient()
        self.app = app_module
        self.meta = {"user_key": U_PHONE, "hostname": "Pixel",
                     "platform": "Android 14", "client_version": "1.8.0"}

    def test_unknown_field_is_422_without_touching_db(self):
        from db import conn as db_conn
        payload = {**self.meta, "days": [
            {"day": "2026-11-06", "unlock": 1,
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]}
        with mock.patch.object(db_conn, "connect") as connect:
            response = self.client.post("/api/v1/app-timeline/upload", json=payload)
        self.assertEqual(response.status_code, 422)
        connect.assert_not_called()

    def test_bad_bucket_is_422(self):
        payload = {**self.meta, "days": [
            {"day": "2026-11-06",
             "bins": [{"bucket": 96, "app_name": "wechat", "seconds": 60}]}]}
        response = self.client.post("/api/v1/app-timeline/upload", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_valid_unlocks_payload_is_accepted(self):
        cur = mock.MagicMock()
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cur
        payload = {**self.meta, "days": [
            {"day": "2026-11-06", "unlocks": 42,
             "bins": [{"bucket": 0, "app_name": "wechat", "seconds": 60}]}]}
        from db import conn as db_conn
        with mock.patch.object(tracking_api, "authenticate"), \
                mock.patch.object(db_conn, "connect") as connect:
            connect.return_value.__enter__.return_value = connection
            response = self.client.post("/api/v1/app-timeline/upload", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


class MergedMailTestCase(unittest.TestCase):
    def setUp(self):
        self._patchers = []
        self.send_mail = self._patch(sender, "send_mail")
        self.was_sent = self._patch(mail_log, "was_mail_sent", return_value=False)
        self.record_mail = self._patch(mail_log, "record_mail")
        self.get_user = self._patch(
            users, "get_user",
            return_value={"user_key": U_PC, "email": "user@example.com",
                          "hostname": "PC", "subscribed": 1})
        self.get_summary = self._patch(usage_records, "get_daily_summary")
        self.get_range = self._patch(usage_records, "get_range_summary")
        self.read_range = self._patch(tracking, "read_range")
        self.readiness = self._patch(tracking, "readiness", return_value=[])
        self._patch_config = mock.patch.object(sender.config, "MAIL_TRACKING_ENABLED", True)
        self._patch_config.start()
        self.addCleanup(self._patch_config.stop)

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _summaries(self, pc_total=3600, phone_total=1800):
        def daily(key, day):
            if key == U_PC:
                return summary(pc_total, [app_row("wechat", 2000, 5),
                                          app_row("chrome.exe", 1600, 3)])
            return summary(phone_total, [app_row("wechat", 1200, 9),
                                         app_row("douyin", 600, 2)])
        self.get_summary.side_effect = daily

    def _intervals(self, pc_spans, phone_spans):
        """app_intervals per device; timeline bins derived from the same spans."""
        def read_range(kind, key, start, end):
            if kind == "app_intervals":
                spans = pc_spans if key == U_PC else phone_spans
                return [{"day": str(DAY), "app_name": "wechat",
                         "start_second": s, "end_second": e} for s, e in spans]
            if kind == "timeline":
                spans = pc_spans if key == U_PC else phone_spans
                return [{"day": str(DAY), "bucket": int(s // 900), "app_name": "wechat",
                         "seconds": e - s} for s, e in spans]
            return []
        self.read_range.side_effect = read_range


class TestSendMergedDay(MergedMailTestCase):
    def test_one_mail_for_two_devices_with_overlap_line(self):
        self._summaries()
        # PC 0-3600, phone 1800-3600: 1800s overlap; merged = 3600+1800-1800.
        self._intervals([(0, 3600)], [(1800, 3600)])
        devices = [device(U_PHONE, created="2026-01-01 00:00:00"),
                   device(U_PC, created="2026-01-02 00:00:00")]
        outcome = sender.send_merged_day(devices, DAY, MAIL)
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(outcome["merged_devices"], 2)
        self.send_mail.assert_called_once()
        to, subject, text, html_body = self.send_mail.call_args[0]
        self.assertEqual(to, "user@example.com")
        self.assertIn("1 小时", subject)  # merged 3600s, not 5400s
        self.assertIn("分设备", text)
        self.assertIn("双设备重叠：30 分", text)
        self.assertIn("双设备重叠", html_body)
        # mail_log keyed by the earliest-created device, detail names the merge.
        self.record_mail.assert_called_once()
        args = self.record_mail.call_args[0]
        self.assertEqual(args[0], U_PHONE)
        self.assertIn("合并 2 台设备", args[4])

    def test_no_overlap_means_plain_sum(self):
        self._summaries()
        self._intervals([(0, 1800)], [(3600, 5400)])
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE)], DAY, MAIL)
        _, _, text, _ = self.send_mail.call_args[0]
        self.assertEqual(outcome["status"], "sent")
        self.assertNotIn("双设备重叠", text)

    def test_dry_run_renders_but_sends_nothing(self):
        self._summaries()
        self._intervals([(0, 3600)], [(1800, 3600)])
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE)], DAY, MAIL, dry_run=True)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "dry_run")
        self.send_mail.assert_not_called()
        self.record_mail.assert_not_called()

    def test_already_sent_anchor_skips_the_whole_mailbox(self):
        self._summaries()
        self.was_sent.return_value = True
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE)], DAY, MAIL)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "already_sent")
        self.send_mail.assert_not_called()

    def test_no_data_on_any_device_skips(self):
        self.get_summary.return_value = None
        outcome = sender.send_merged_day(
            [device(U_PC), device(U_PHONE)], DAY, MAIL)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "no_data")
        self.send_mail.assert_not_called()


class TestRunGroupsByMailbox(MergedMailTestCase):
    def setUp(self):
        super().setUp()
        self.get_subscribers = self._patch(users, "get_subscribers")
        self._patch_config_mail = mock.patch.object(sender.config, "MAIL_ENABLED", True)
        self._patch_config_mail.start()
        self.addCleanup(self._patch_config_mail.stop)

    def test_same_mailbox_sends_once(self):
        self._summaries()
        self._intervals([(0, 3600)], [(1800, 3600)])
        self.get_subscribers.return_value = [
            device(U_PC, created="2026-01-02 00:00:00"),
            device(U_PHONE, created="2026-01-01 00:00:00")]
        result = sender.run(day=DAY)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["merged_devices"], 2)
        self.send_mail.assert_called_once()

    def test_distinct_mailboxes_still_send_separately(self):
        self._summaries()
        self._intervals([(0, 3600)], [(1800, 3600)])
        self.get_subscribers.return_value = [
            device(U_PC, "a@example.com"),
            device(U_PHONE, "b@example.com")]
        result = sender.run(day=DAY)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertNotIn("merged_devices", result["results"][0])

    def test_weekly_merge_does_not_count_cross_day_overlap(self):
        # PC used 0-3600 on Monday, phone 0-3600 on Tuesday: same seconds,
        # different days, so overlap must be 0 and merged total the plain sum.
        monday = SUNDAY - timedelta(days=6)
        tuesday = monday + timedelta(days=1)

        def weekly(key, start, end):
            apps = [app_row("wechat", 3600, 1)]
            out = {"start": str(start), "end": str(end), "days_with_data": 1,
                   "total_seconds": 3600, "app_count": 1, "apps": apps,
                   "daily_apps": [{"day": str(monday if key == U_PC else tuesday),
                                   "app_name": "wechat", "seconds": 3600}]}
            return out
        self.get_range.side_effect = weekly

        def read_range(kind, key, start, end):
            if kind == "app_intervals":
                day = monday if key == U_PC else tuesday
                return [{"day": str(day), "app_name": "wechat",
                         "start_second": 0, "end_second": 3600}]
            if kind == "timeline":
                day = monday if key == U_PC else tuesday
                return [{"day": str(day), "bucket": 0, "app_name": "wechat",
                         "seconds": 3600}]
            return []
        self.read_range.side_effect = read_range
        self.get_subscribers.return_value = [
            device(U_PC, created="2026-01-02 00:00:00"),
            device(U_PHONE, created="2026-01-01 00:00:00")]
        result = sender.run(day=SUNDAY)
        self.assertEqual(result["sent"], 1)
        _, subject, text, _ = self.send_mail.call_args[0]
        self.assertIn("2 小时", subject)  # 3600 + 3600, no overlap deducted
        self.assertNotIn("双设备重叠", text)


class TestMergedReportRendering(unittest.TestCase):
    def test_merged_breakdown_lines_only_appear_for_merged_summaries(self):
        plain = summary(3600, [app_row("wechat", 3600)])
        self.assertNotIn("分设备", report.build_text(DAY, plain))
        merged = dict(plain)
        merged["_devices"] = [{"label": "PC", "total_seconds": 3600, "app_count": 1},
                              {"label": "Pixel", "total_seconds": 1800, "app_count": 1}]
        merged["_overlap_seconds"] = 1800
        text = report.build_text(DAY, merged)
        self.assertIn("分设备", text)
        self.assertIn("双设备重叠：30 分", text)


if __name__ == "__main__":
    unittest.main()
