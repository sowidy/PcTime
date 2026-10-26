"""Plain-language timelines keep real data and remain readable without images."""
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mail import timeline, tracking_report, report, charts


class MailTimelineTests(unittest.TestCase):
    day = date(2026, 10, 19)

    def rows(self, *records):
        return [{"day": str(self.day), "bucket": bucket, "app_name": app, "seconds": seconds}
                for bucket, app, seconds in records]

    def test_adjacent_records_merge_but_gaps_never_do(self):
        rows = self.rows((36, "editor", 120), (37, "editor", 40), (39, "editor", 20), (40, "browser", 300))
        spans = timeline.build_spans(rows)
        self.assertEqual([(s["start"], s["end"], s["app_name"]) for s in spans],
                         [(36, 38, "editor"), (39, 40, "editor"), (40, 41, "browser")])
        self.assertEqual([s["seconds"] for s in spans], [160, 20, 300])
        text, body = timeline.render({"total_seconds": 480}, rows, self.day, self.day)
        self.assertIn("09:30–09:45", body)
        self.assertIn("未记录时段", body)
        self.assertIn("记录 20 秒", body)  # Short use must not disappear.
        self.assertIn("不代表未使用", text)

    def test_multi_app_bin_keeps_secondary_apps_and_does_not_invent_switch_times(self):
        rows = self.rows((36, "editor", 420), (36, "browser", 100), (37, "editor", 30))
        summary = {"total_seconds": 550, "apps": [{"app_name": "editor", "display_name": "编辑器"}]}
        text, body = timeline.render(summary, rows, self.day, self.day)
        self.assertIn("09:00–09:30", body)
        self.assertIn("编辑器", body)
        self.assertIn("browser 1 分 40 秒", body)
        self.assertIn("时段共记录 9 分 10 秒", body)
        self.assertIn("编辑器 7 分 30 秒", text)
        self.assertIn("起止时间为近似值", body)
        self.assertNotIn("09:07", body)

    def test_midnight_and_ties_are_deterministic(self):
        rows = self.rows((95, "b", 60), (95, "a", 60))
        self.assertEqual(timeline.build_spans(rows)[0]["app_name"], "a")
        self.assertEqual(timeline.build_spans(list(reversed(rows)))[0]["app_name"], "a")
        _, body = timeline.render({"total_seconds": 120}, rows, self.day, self.day)
        self.assertIn("23:45–24:00", body)

    def test_missing_day_and_completed_empty_day_are_different(self):
        rows = self.rows((36, "editor", 60))
        ready = [{"day": str(self.day+timedelta(days=1)), "kind": "timeline", "complete": True}]
        text, body = timeline.render({"total_seconds": 60}, rows, self.day, self.day+timedelta(days=2), ready)
        self.assertIn("当天无使用记录", text)
        self.assertIn("无时间线数据（未收到）", text)
        self.assertIn("10-20 周二", body)
        self.assertIn("10-21 周三", body)

    def test_html_escapes_names_without_relying_on_hover_or_image(self):
        rows = self.rows((36, "evil", 1))
        summary = {"total_seconds": 1, "apps": [{"app_name": "evil", "display_name": '<img src="x">'}]}
        _, body = timeline.render(summary, rows, self.day, self.day)
        self.assertIn("&lt;img", body)
        self.assertNotIn('<img src="x">', body)
        self.assertNotIn("title=", body)
        self.assertNotIn("data:image", body)
        self.assertNotIn("同色浅", body)
        self.assertNotIn("%", body.replace('width="100%"', ""))

    def test_long_legacy_timeline_is_not_added_to_weekly_mail(self):
        end = self.day+timedelta(days=6)
        rows = [{"day": str(self.day+timedelta(days=day)), "bucket": i, "app_name": f"app-{i:02d}", "seconds": 120}
                for day in range(7) for i in range(96)]
        summary = {"total_seconds": len(rows)*120, "apps": [], "app_count": 96}
        message = tracking_report.append_sections(report.build_weekly_message(end, summary), summary,
                                                  rows, [], self.day, end)
        self.assertNotIn("应用时间线", message[2])
        self.assertNotIn('data-timeline="itinerary"', message[2])
        self.assertNotIn("app-47", message[1])
        self.assertLess(charts.html_size(message[2]), 80000)

    def test_ignores_zero_invalid_and_out_of_period_data(self):
        rows = self.rows((36, "zero", 0), (37, "bad", float("nan")), (-1, "bad", 20), (96, "bad", 20))
        rows.append({"day": "2000-01-01", "bucket": 40, "app_name": "old", "seconds": 300})
        _, body = timeline.render({"total_seconds": 0}, rows, self.day, self.day)
        self.assertIn("当天尚未收到应用时间线", body)
        self.assertNotIn("old", body)


if __name__ == "__main__":
    unittest.main()
