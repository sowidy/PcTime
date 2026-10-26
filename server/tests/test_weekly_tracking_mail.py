"""Week-report integration: real rendering, fake data boundaries, no SMTP or user data."""
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from db import tracking
from mail import report, sender, tracking_report


class WeeklyTrackingTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 10, 19)
        self.end = self.start + timedelta(days=6)
        self.summary = {"total_seconds": 1800, "app_count": 1, "days_with_data": 2,
                        "apps": [{"app_name": "editor.exe", "display_name": "<Editor>",
                                  "seconds": 1800, "launches": 2}]}
        self.bins = [{"day": str(self.start + timedelta(days=i)), "bucket": 36,
                      "app_name": "editor.exe", "seconds": 900} for i in (0, 2)]
        self.sites = [{"day": str(self.start + timedelta(days=i)), "host": "example.com", "seconds": 300}
                      for i in (0, 2)]
        self.ready = [{"day": str(self.start + timedelta(days=i)), "kind": kind, "complete": True}
                      for i in range(7) for kind in ("apps", "timeline", "web")]

    def test_weekly_body_has_sites_daily_totals_without_application_timeline(self):
        message = tracking_report.append_sections(report.build_weekly_message(self.end, self.summary),
            self.summary, self.bins, self.sites, self.start, self.end, self.ready)
        self.assertNotIn("一周应用时间线", message[2])
        self.assertIn("堆叠柱状图叠加总时长折线", message[2])
        self.assertIn("一周网站使用热力图", message[2])
        self.assertIn("一周网站每日使用时长", message[2])
        self.assertIn("example.com: " + report.format_duration(600), message[1])
        self.assertIn("2026-10-20 网站: 0 秒", message[1])
        self.assertNotIn('data-timeline="itinerary"', message[2])
        self.assertIn("&lt;Editor&gt;", message[2])
        self.assertNotIn("<Editor>", message[2])
        self.assertEqual(self.summary["total_seconds"], 1800)
        self.assertIn(report.format_duration(1800), message[0])
        self.assertLess(message[2].index("一周网站使用时长"), message[2].index(report.FOOTER_TEXT[:12]))
        self.assertLess(len(message[2].encode()), 80000)

    def test_partial_week_does_not_fill_missing_website_days(self):
        self.summary["total_seconds"] = 20000
        text, body = tracking_report.render(self.summary, self.bins, self.sites, self.start, self.end)
        self.assertIn("部分日期未确认", body)
        self.assertNotIn('data-timeline="itinerary"', body)
        self.assertIn("2026-10-20 网站: 未收到", text)
        self.assertNotIn("2026-10-20 网站: 0 秒", text)

    def test_empty_week_shows_upload_instructions_without_fabricating_zeros(self):
        text, body = tracking_report.render(self.summary, [], [], self.start, self.end)
        self.assertNotIn("一周应用时间线", body)
        self.assertIn("一周网站使用时长", body)
        self.assertIn("本周尚未收到网站记录", text)
        self.assertIn("HTTPS", text)
        self.assertNotIn("网站浏览明细：0", text)

    def _send(self, sites=None, bins=None, summary=True, readiness=None):
        def read(kind, *_):
            if kind == "web" and isinstance(sites, Exception):
                raise sites
            return (self.sites if sites is None else sites) if kind == "web" else []
        with mock.patch.multiple(config, MAIL_TRACKING_ENABLED=True, MAIL_WEEKLY_DAY=6), \
                mock.patch.object(sender.mail_log, "was_mail_sent", return_value=False), \
                mock.patch.object(sender.mail_log, "record_mail"), \
                mock.patch.object(sender.usage_records, "get_range_summary", return_value=self.summary if summary else None), \
                mock.patch.object(sender.users, "get_user", return_value={"hostname": "synthetic"}), \
                mock.patch.object(tracking, "read_range", side_effect=read) as reads, \
                mock.patch.object(tracking, "readiness", return_value=self.ready if readiness is None else readiness), \
                mock.patch.object(sender, "send_mail") as send:
            result = sender.send_day("test-device", self.end, email="sink@example.invalid")
        self.assertEqual(result["status"], "sent")
        reads.assert_any_call("web", "test-device", self.start, self.end)
        self.assertFalse(any(call.args[0] == "timeline" for call in reads.call_args_list))
        return send.call_args.args[1:]

    def test_sender_includes_placeholders_when_no_extra_data_has_arrived(self):
        message = self._send(sites=[], bins=[], readiness=[])
        self.assertIn("本周尚未收到网站记录", message[2])
        self.assertNotIn("应用时间线", message[2])

    def test_website_query_failure_keeps_application_chart(self):
        with self.assertLogs("pctime.mail.sender", level="WARNING"):
            message = self._send(sites=RuntimeError("test-only database failure"))
        self.assertIn("应用时长构成", message[2])
        self.assertNotIn('data-timeline="itinerary"', message[2])

    def test_website_can_be_reported_before_application_summary(self):
        message = self._send(summary=False)
        self.assertIn("应用汇总未收到", message[0])
        self.assertNotIn("共 0 秒", message[0])
        self.assertIn("一周网站使用时长", message[2])
        self.assertNotIn('data-timeline="itinerary"', message[2])


if __name__ == "__main__":
    unittest.main()
