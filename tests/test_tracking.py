"""Trust boundaries, durable receipts, time binning, API contracts and mail fallbacks."""
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from uuid import uuid4

from browser.coordinator import BrowserCoordinator
from browser.protocol import parse_message, segment
from browser.sanitizer import normalize_host
from core import storage, site_storage, timeline, config as client_config
from api.tracking_schemas import SiteDay, TimelineDay
from mail import tracking_report


class LocalTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(storage, "DB_PATH", os.path.join(self.temp.name, "usage.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.init_db()

    def event(self, start, end):
        return {"event_id": str(uuid4()), "session_id": str(uuid4()), "browser": "chrome",
                "host": "github.com", "start": start, "end": end}

    def test_receipt_is_durable_and_midnight_is_split(self):
        start = datetime(2026, 10, 21, 23, 59, 55).timestamp()
        event = self.event(start, start + 10)
        self.assertTrue(site_storage.record_segment(event, [(start, start + 10)]))
        self.assertFalse(site_storage.record_segment(event, [(start, start + 10)]))
        rows = site_storage.get_site_summary_range("2026-10-21", "2026-10-22")
        self.assertEqual([r["total_seconds"] for r in rows], [5, 5])
        self.assertEqual(len(site_storage.get_site_fragments_for_date("2026-10-21")), 1)
        self.assertEqual(site_storage.get_site_hourly_bins_for_date("2026-10-21"),
                         [{"hour": 23, "host": "github.com", "seconds": 5}])
        self.assertEqual(site_storage.get_site_hourly_bins_for_date("2026-10-22"),
                         [{"hour": 0, "host": "github.com", "seconds": 5}])
        self.assertEqual(site_storage.get_site_intervals_for_date("2026-10-21"),
                         [{"host": "github.com", "start_second": 86395, "end_second": 86400}])
        self.assertEqual(site_storage.get_site_intervals_for_date("2026-10-22"),
                         [{"host": "github.com", "start_second": 0, "end_second": 5}])

    def test_site_hourly_upload_is_derived_from_real_intervals(self):
        start = datetime(2026, 10, 21, 9, 59, 40).timestamp()
        site_storage.record_segment(self.event(start, start + 50), [(start, start + 50)])
        self.assertEqual(site_storage.get_site_hourly_bins_for_date("2026-10-21"),
                         [{"hour": 9, "host": "github.com", "seconds": 20},
                          {"hour": 10, "host": "github.com", "seconds": 30}])
        self.assertEqual(site_storage.get_site_intervals_for_date("2026-10-21"),
                         [{"host": "github.com", "start_second": 35980, "end_second": 36030}])

    def test_ignored_event_acknowledged_without_data(self):
        site_storage.set_ignored_site("github.com")
        site_storage.record_segment(self.event(100, 110), [(100, 110)])
        self.assertEqual(site_storage.get_site_stats("1970-01-01"), [])

    def test_failed_insert_rolls_back_receipt(self):
        event = self.event(100, 110)
        event.pop("session_id")
        with self.assertRaises(KeyError):
            site_storage.record_segment(event, [(100, 110)])
        event["session_id"] = str(uuid4())
        self.assertTrue(site_storage.record_segment(event, [(100, 110)]))

    def test_clear_cannot_be_undone_by_pending_replay(self):
        event = self.event(100, 110)
        site_storage.record_segment(event, [(100, 110)])
        site_storage.clear_site_data()
        self.assertFalse(site_storage.record_segment(event, [(100, 110)]))

    def test_timeline_buckets_split_exactly(self):
        storage.record_fragment("example.exe", "secret title", "C:/secret/example.exe",
                                "2026-10-21T09:10:00", "2026-10-21T09:20:00", 600)
        bins = timeline.build_bins("2026-10-21")
        self.assertEqual(bins, [{"bucket": 36, "app_name": "example.exe", "seconds": 300},
                               {"bucket": 37, "app_name": "example.exe", "seconds": 300}])
        self.assertNotIn("secret", json.dumps(bins))

    def test_overlap_never_double_counts_and_large_error_rejected(self):
        for app in ("first.exe", "second.exe"):
            storage.record_fragment(app, "", "", "2026-10-21T09:00:00", "2026-10-21T09:10:00", 600)
        with self.assertRaises(ValueError):
            timeline.build_bins("2026-10-21")

    def test_csv_only_contains_host_summary(self):
        start = datetime(2026, 10, 21, 9).timestamp()
        site_storage.record_segment(self.event(start, start + 10), [(start, start + 10)])
        target = os.path.join(self.temp.name, "website.csv")
        site_storage.export_sites("2026-10-21", "2026-10-21", target)
        with open(target, encoding="utf-8-sig") as file:
            contents = file.read()
        self.assertIn("github.com", contents)
        self.assertNotIn("09:00", contents)


class TrustTests(unittest.TestCase):
    def test_host_normalization_and_rejection(self):
        self.assertEqual(normalize_host("WWW.Example.COM.:443"), "www.example.com")
        self.assertEqual(normalize_host("例子.测试"), "xn--fsqu00a.xn--0zwm56d")
        self.assertEqual(normalize_host("[::1]:8443"), "[::1]:8443")
        for host in ("user@example.com", "example.com/path", "example.com?token=x", "bad host", "a..com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                normalize_host(host)

    def test_oversized_message_rejected(self):
        with self.assertRaises(ValueError):
            parse_message('"' + "x" * 17000 + '"')

    def test_segment_rejects_nan_future_and_stale(self):
        event = {"type": "segment", "protocol": 1, "event_id": str(uuid4()),
                 "session_id": str(uuid4()), "profile_instance_id": str(uuid4()),
                 "browser": "chrome", "host": "example.com", "started_at_ms": 990000, "ended_at_ms": 1000000}
        self.assertEqual(segment(event, 1000)["end"], 1000)
        for start, end in ((float("nan"), 1000000), (990000, 1100000), (1, 1000)):
            with self.subTest(start=start), self.assertRaises(ValueError):
                segment({**event, "started_at_ms": start, "ended_at_ms": end}, 1000)

    def test_desktop_idle_and_duplicate_interval_protection(self):
        coordinator = BrowserCoordinator()
        peers = [{"profile": "one", "focused": True, "browser": "chrome"}]
        state = {"app_name": "chrome.exe", "is_idle": False}
        coordinator.observe(state, peers, 100)
        for t in range(101, 111):
            coordinator.observe(state, peers, t)
        coordinator.observe({**state, "is_idle": True}, peers, 110)
        accepted = coordinator.intersections("one", 90, 120)
        self.assertEqual(accepted, [(100, 110)])
        coordinator.committed(accepted)
        self.assertEqual(coordinator.intersections("one", 90, 120), [])

    def test_sleep_gap_does_not_become_active_time(self):
        c = BrowserCoordinator()
        peers = [{"profile": "one", "focused": True, "browser": "chrome"}]
        state = {"app_name": "chrome.exe"}
        c.observe(state, peers, 100)
        c.observe(state, peers, 500)
        self.assertEqual(c.intersections("one", 100, 500), [(100, 101)])

    def test_only_one_browser_profile_granted(self):
        c = BrowserCoordinator()
        peers = [{"profile": "one", "focused": True, "browser": "chrome", "focus_at": 1},
                 {"profile": "two", "focused": True, "browser": "chrome", "focus_at": 2}]
        self.assertEqual(c.observe({"app_name": "chrome.exe"}, peers, 100), "two")
        c.observe({"app_name": "chrome.exe"}, peers, 101)
        self.assertEqual(c.intersections("one", 100, 101), [])

    def test_url_configuration(self):
        with mock.patch.object(client_config, "SERVER_URL", "https://example.com/prefix/api/v1/"):
            self.assertEqual(client_config.upload_url(), "https://example.com/prefix/api/v1/upload")
            self.assertTrue(client_config.secure_server())
        for value in ("file:///x", "https://user:password@example.com", "https://example.com?token=x"):
            with mock.patch.object(client_config, "SERVER_URL", value), self.assertRaises(ValueError):
                client_config.base_url()
        with mock.patch.object(client_config, "SERVER_URL", "http://203.0.113.2:8000"):
            self.assertFalse(client_config.secure_server())


class ContractTests(unittest.TestCase):
    def test_hourly_site_contract_rejects_mismatch_and_duplicates(self):
        valid = SiteDay(day="2026-10-21", sites=[{"host": "github.com", "seconds": 50}],
                        hourly=[{"hour": 9, "host": "github.com", "seconds": 20},
                                {"hour": 10, "host": "github.com", "seconds": 30}])
        self.assertEqual(sum(row.seconds for row in valid.hourly), 50)
        with self.assertRaises(ValueError):
            SiteDay(day="2026-10-21", sites=[{"host": "github.com", "seconds": 50}],
                    hourly=[{"hour": 9, "host": "github.com", "seconds": 20}])
        with self.assertRaises(ValueError):
            SiteDay(day="2026-10-21", sites=[{"host": "github.com", "seconds": 50}],
                    hourly=[{"hour": 9, "host": "github.com", "seconds": 20},
                            {"hour": 9, "host": "github.com", "seconds": 30}])

    def test_private_fields_and_nonfinite_seconds_rejected(self):
        for seconds in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                SiteDay(day="2026-10-21", sites=[{"host": "example.com", "seconds": seconds}])
        for interval in ({"host": "example.com", "start_second": 0, "end_second": 20,
                          "url": "https://example.com/private"},
                         {"host": "example.com", "start_second": 20, "end_second": 10},
                         {"host": "example.com", "start_second": 0, "end_second": float("nan")}):
            with self.assertRaises(ValueError):
                SiteDay(day="2026-10-21", sites=[{"host": "example.com", "seconds": 20}],
                        intervals=[interval])
        with self.assertRaises(ValueError):
            SiteDay(day="2026-10-21", sites=[{"host": "example.com", "seconds": 20}],
                    intervals=[{"host": "example.com", "start_second": 0, "end_second": 10}])
        with self.assertRaises(ValueError):
            TimelineDay(day="2026-10-21", bins=[{"bucket": 0, "app_name": "a.exe", "seconds": 1,
                                                 "window_title": "secret"}])

    def test_duplicate_hosts_and_overfull_bucket_rejected(self):
        with self.assertRaises(ValueError):
            SiteDay(day="2026-10-21", sites=[{"host": "EXAMPLE.com", "seconds": 1},
                                             {"host": "example.com", "seconds": 1}])
        with self.assertRaises(ValueError):
            TimelineDay(day="2026-10-21", bins=[{"bucket": 0, "app_name": "a.exe", "seconds": 900},
                                                 {"bucket": 0, "app_name": "b.exe", "seconds": 20}])

    def test_missing_bearer_refused_before_database_access(self):
        from api.tracking import authenticate
        from fastapi import HTTPException
        from db import conn
        with mock.patch.object(conn, "connect") as connect, self.assertRaises(HTTPException):
            authenticate(str(uuid4()), "")
        connect.assert_not_called()

    def test_empty_snapshot_deletes_and_marks_readiness(self):
        from db import tracking
        cur = mock.Mock()
        self.assertEqual(tracking.replace_day(cur, "web", str(uuid4()), date(2026, 10, 21), [], True), 0)
        cur.executemany.assert_not_called()
        self.assertIn("DELETE", cur.execute.call_args_list[0].args[0])
        self.assertIn("tracking_uploads", cur.execute.call_args_list[1].args[0])


class TrackingMailTests(unittest.TestCase):
    def test_chart_does_not_modify_total_and_escapes_labels(self):
        day = date(2026, 10, 21)
        summary = {"total_seconds": 1800, "apps": [{"app_name": "evil.exe", "display_name": "<script>"}]}
        bins = [{"day": str(day), "bucket": 36 + i, "app_name": "evil.exe", "seconds": 900} for i in range(2)]
        text, body = tracking_report.render(summary, bins, [{"host": "example.com", "seconds": 500}], day, day)
        self.assertNotIn("<script>", body)
        self.assertNotIn("应用时间线", body)
        self.assertIn("网站浏览明细", text)
        self.assertEqual(summary["total_seconds"], 1800)
        self.assertLess(len(body.encode()), 80000)

    def test_timeline_only_does_not_create_mail_section(self):
        day = date(2026, 10, 21)
        text, body = tracking_report.render({"total_seconds": 20000, "apps": []},
            [{"day": str(day), "bucket": 36, "app_name": "a.exe", "seconds": 20}], [], day, day)
        self.assertEqual((text, body), ("", ""))

    def test_no_data_keeps_original_message(self):
        message = ("subject", "text", "<body>existing</body>")
        self.assertEqual(tracking_report.append_sections(message, {"total_seconds": 0}, [], [],
                         date(2026, 10, 21), date(2026, 10, 21)), message)

    def test_tracking_sections_precede_footer(self):
        from mail import report
        day = date(2026, 10, 21)
        summary = {"total_seconds": 0, "app_count": 0, "apps": []}
        message = tracking_report.append_sections(report.build_message(day, summary), summary, [],
                    [{"day": str(day), "host": "example.com", "seconds": 30}], day, day)
        self.assertLess(message[2].index("网站浏览明细"), message[2].index(report.FOOTER_TEXT[:12]))

    def test_missing_application_summary_is_not_presented_as_zero(self):
        from mail import report
        message = report.build_missing_app_message(date(2026, 10, 21))
        self.assertIn("应用汇总未收到", message[0])
        self.assertNotIn("共 0 秒", message[0])

    def test_weekly_mail_ignores_legacy_timeline_rows(self):
        start = date(2026, 10, 12)
        bins = [{"day": str(start + timedelta(days=i)), "bucket": 36,
                 "app_name": "a.exe", "seconds": 900} for i in (0, 2)]
        text, body = tracking_report.render({"total_seconds": 1800, "apps": []}, bins, [], start, start + timedelta(days=6))
        self.assertIn("本周尚未收到网站记录", text)
        self.assertNotIn("应用时间线", text + body)
        self.assertNotIn('data-timeline="itinerary"', body)
