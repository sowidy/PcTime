"""Chart data, PNG rendering and actual MIME structure; never contacts SMTP/DB."""
import base64
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from db import usage_records
from mail import charts, colors, report, sender, tracking_report


class MailChartTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 10, 19)
        self.end = self.start + timedelta(days=6)
        self.apps = [{"app_name": f"app-{i:02d}.exe", "display_name": f"Application {i:02d}",
                      "seconds": 900, "launches": 1} for i in range(61)]
        self.daily = [{"day": str(self.start + timedelta(days=i//24)), "app_name": a["app_name"],
                       "seconds": a["seconds"]} for i, a in enumerate(self.apps)]
        self.summary = {"total_seconds": 54900, "app_count": 61, "apps": self.apps, "daily_apps": self.daily}

    def test_grouping_preserves_every_second_and_missing_breaks_line(self):
        model = charts.prepare(["19", "20", "21"], [{"a": 3600, "b": 1800}, {}, {"b": 900}],
                               [True, False, True], limit=1)
        self.assertEqual(model["series"], ["a", charts.OTHER])
        self.assertEqual(model["values"], [[3600, 1800], None, [0, 900]])
        self.assertEqual(sum(sum(row) for row in model["values"] if row is not None), 6300)
        image = Image.open(BytesIO(charts.png(model)))
        self.assertEqual(image.size, (1200, 510))

    def test_smoothing_keeps_actual_points_without_overshoot_or_negative_usage(self):
        points = [(0, 1), (1, 0), (2, 4), (3, 4), (4, 2)]
        samples = charts.smooth_line(points)
        self.assertEqual(samples[0], points[0])
        self.assertEqual(samples[-1], points[-1])
        for i, (a, b) in enumerate(zip(points, points[1:])):
            segment = samples[i*16:(i+1)*16+1]
            self.assertEqual(segment[-1], b)
            self.assertTrue(all(min(a[1], b[1]) <= p[1] <= max(a[1], b[1]) for p in segment))
        self.assertEqual(charts.smooth_line([]), [])
        self.assertEqual(charts.smooth_line([(1, 0)]), [(1, 0)])

    def test_missing_dates_break_smoothed_runs_but_real_zeros_are_kept(self):
        points = [(0, 0), (1, 2), None, (3, 0), None, None, (6, 1)]
        self.assertEqual(charts.line_runs(points), [[(0, 0), (1, 2)], [(3, 0)], [(6, 1)]])
        self.assertEqual(charts.line_runs([None, None]), [])

    def test_echarts_inspired_theme_is_static_and_server_only(self):
        body = charts.render(["A", "B"], [{"app": 60}, {"app": 120}], [True, True], {}, "example")
        self.assertIn('data-chart-style="echarts-inspired"', body)
        self.assertIn("蓝色曲线", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("https://", body)
        self.assertEqual(len(charts.IMAGE_DATA.findall(body)), 1)

    def test_sixty_apps_forty_sites_keep_charts_without_old_timeline(self):
        bins = [{"day": row["day"], "bucket": i % 24 * 4, "app_name": row["app_name"], "seconds": 900}
                for i, row in enumerate(self.daily)]
        sites = [{"day": str(self.start), "host": f"site-{i:02d}.example", "seconds": 100} for i in range(41)]
        message = tracking_report.append_sections(report.build_weekly_message(self.end, self.summary),
            self.summary, bins, sites, self.start, self.end)
        self.assertEqual(message[2].count("data:image/png;base64,"), 4)
        self.assertIn("一周应用使用趋势", message[2])
        self.assertIn("一周网站每日使用时长", message[2])
        self.assertIn('data-chart-style="weekly-site-stacked"', message[2])
        self.assertIn("一周网站使用热力图", message[2])
        self.assertIn("网站类型占比", message[2])
        self.assertIn("其他（53 应用）", message[2])
        self.assertNotIn("应用排行", message[2])
        self.assertNotIn("网站周排行", message[2])
        self.assertNotIn("linear-gradient", message[2])
        self.assertNotIn('data-timeline="itinerary"', message[2])
        self.assertLess(charts.html_size(message[2]), 80000)
        self.assertIn("10-20：6 小时", message[2])
        self.assertIn("10-22：未收到", message[2])

    def test_weekly_without_daily_detail_never_fabricates_daily_values(self):
        summary = {k: v for k, v in self.summary.items() if k != "daily_apps"}
        body = report.build_weekly_html(self.end, summary)
        self.assertIn("Week：15 小时 15 分", body)
        self.assertIn("不推算每天/小时的分布", body)
        self.assertNotIn("10-20：", body)

    def test_complete_empty_day_is_zero_but_missing_day_is_not(self):
        ready = [{"day": str(self.start+timedelta(days=1)), "kind": "web_hourly", "complete": True}]
        body = charts.website_chart([{"day": str(self.start), "host": "test", "seconds": 60}],
                                    self.start, self.end, ready)
        self.assertIn("10-20：0 秒", body)
        self.assertIn("10-21：未收到分时记录", body)

    def test_daily_app_usage_shows_many_readable_bars(self):
        summary = {"apps": self.apps[:1], "total_seconds": 900,
                   "_timeline_rows": [{"app_name": self.apps[0]["app_name"], "bucket": 36, "seconds": 900}]}
        self.assertIn('data-chart-style="daily-app-bars"', report.build_html(self.start, summary))
        summary["total_seconds"] = 5000
        self.assertIn("当天软件使用时长", report.build_html(self.start, summary))

    def test_real_hourly_site_bins_drive_daily_stack_and_weekly_heatmap(self):
        rows = [{"day": str(self.start), "host": "github.com", "seconds": 600},
                {"day": str(self.start), "host": "youtube.com", "seconds": 300}]
        hourly = [{"day": str(self.start), "hour": 9, "host": "github.com", "seconds": 600},
                  {"day": str(self.start), "hour": 10, "host": "youtube.com", "seconds": 300}]
        days, buckets = charts.website_hourly_model(rows, hourly, self.start, self.start)
        self.assertEqual(sum(sum(bucket.values()) for bucket in buckets[0]), 900)
        self.assertEqual(buckets[0][9]["github.com"], 600)
        daily = charts.website_chart(rows, self.start, self.start, hourly_rows=hourly)
        self.assertIn("每小时网站使用", daily)
        self.assertIn("网站类型占比", daily)
        self.assertEqual(daily.count("data:image/png;base64,"), 2)
        self.assertIn('<td align="center" style="text-align:center"><img data-chart-style="website-category-pie"',
                      daily)
        self.assertIn("工作学习", daily)
        self.assertIn("视频娱乐", daily)
        weekly = charts.website_chart(rows, self.start, self.end, hourly_rows=hourly)
        self.assertIn('data-chart-style="weekly-web-heatmap"', weekly)
        self.assertIn("10-20：未收到分时记录", weekly)
        self.assertIsNone(charts.website_hourly_model(rows, hourly[:1], self.start, self.start)[1][0])

    def test_weekly_heatmap_has_readable_monotone_duration_scale(self):
        colors = [charts.website_heatmap_color(seconds, 600)
                  for seconds in (0, 1, 150, 300, 450, 600)]
        brightness = [sum(int(color[at:at + 2], 16) for at in (1, 3, 5)) for color in colors]
        self.assertEqual(len(set(colors)), len(colors))
        self.assertEqual(brightness, sorted(brightness, reverse=True))
        self.assertNotEqual(charts.HEATMAP_ZERO, charts.HEATMAP_MISSING)
        self.assertEqual(charts.website_heatmap_color(0, 0), charts.HEATMAP_ZERO)

    def test_weekly_site_stacks_use_daily_hosts_and_preserve_missing_days(self):
        rows = [{"day": str(self.start), "host": "github.com", "seconds": 600},
                {"day": str(self.start), "host": "youtube.com", "seconds": 300},
                {"day": str(self.start + timedelta(days=2)), "host": "github.com", "seconds": 1800}]
        ready = [{"day": str(self.start + timedelta(days=1)), "kind": "web", "complete": True}]
        days, buckets, observed = charts.weekly_website_daily_model(rows, self.start, self.end, ready)
        self.assertEqual(len(days), 7)
        self.assertEqual(dict(buckets[0]), {"github.com": 600, "youtube.com": 300})
        self.assertEqual(dict(buckets[1]), {})
        self.assertEqual(dict(buckets[2]), {"github.com": 1800})
        self.assertEqual(observed, [True, True, True, False, False, False, False])
        body = charts.weekly_website_stacked_chart(rows, self.start, self.end, ready)
        self.assertIn("蓝色曲线：总时长", body)
        self.assertIn("叠加总时长折线", body)
        self.assertIn("10-19：15 分", body)
        self.assertIn("10-20：0 秒", body)
        self.assertIn("10-21：30 分", body)
        self.assertIn("10-22：未收到", body)
        self.assertEqual(len(charts.IMAGE_DATA.findall(body)), 1)

    def test_daily_gantt_uses_only_real_complete_intervals(self):
        sites = [{"day": str(self.start), "host": "github.com", "seconds": 900}]
        intervals = [{"day": str(self.start), "host": "github.com", "start_second": 32400,
                      "end_second": 33300}]
        body = charts.website_gantt_chart(sites, intervals, self.start)
        self.assertIn('data-chart-style="website-gantt"', body)
        image = base64.b64decode(charts.IMAGE_DATA.search(body).group(1), validate=True)
        Image.open(BytesIO(image)).verify()
        self.assertIn("github.com", body)
        self.assertNotIn('data-chart-style="website-gantt"',
                         charts.website_gantt_chart(sites, intervals[:0], self.start))
        self.assertIn("不根据每日总量", charts.website_gantt_chart(sites, [], self.start))
        _, daily = tracking_report.render({"total_seconds": 900, "apps": []}, [], sites,
                                          self.start, self.start, site_intervals=intervals)
        self.assertIn('data-chart-style="website-gantt"', daily)
        self.assertNotIn('data-timeline="itinerary"', daily)
        _, weekly = tracking_report.render({"total_seconds": 900, "apps": []}, [], sites,
                                           self.start, self.end, site_intervals=intervals)
        self.assertNotIn('data-chart-style="website-gantt"', weekly)

    def test_old_timeline_rows_do_not_reappear_in_mail(self):
        bins = [{"day": str(self.start), "bucket": i, "app_name": "a.exe", "seconds": 900}
                for i in (32, 36, 37, 38, 39)]
        summary = {"total_seconds": 4500, "apps": [{"app_name": "a.exe"}]}
        _, body = tracking_report.render(summary, bins, [], self.start, self.end)
        self.assertNotIn("08:00–08:15", body)
        self.assertNotIn("应用时间线", body)
        self.assertNotIn('data-timeline="itinerary"', body)
        self.assertNotIn("同色浅", body)
        self.assertNotIn('height="15"', body)
        self.assertTrue(all(min(int(c[i:i+2], 16) for i in (1, 3, 5)) > 150 for c in colors.PALETTE))

    def test_legend_is_escaped_and_chart_image_is_valid(self):
        body = charts.render(["Day"], [{"evil": 60}], [True], {"evil": '<script>"&'}, "test")
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        image = base64.b64decode(charts.IMAGE_DATA.search(body).group(1), validate=True)
        Image.open(BytesIO(image)).verify()

    def test_smtp_embeds_png_with_cid_inside_related_html_alternative(self):
        body = charts.website_chart([{"host": "example.com", "seconds": 30}], self.start, self.start)
        with mock.patch.multiple(config, SMTP_USE_SSL=True, SMTP_HOST="invalid", SMTP_PORT=465, SMTP_USER=""), \
                mock.patch.object(sender.smtplib, "SMTP_SSL") as smtp:
            sender.send_mail("sink@example.invalid", "test", "plain", body)
        message = smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual(message.get_payload(0).get_content_type(), "text/plain")
        related = message.get_payload(1)
        self.assertEqual(related.get_content_type(), "multipart/related")
        html_part, image = related.get_payload()
        decoded = html_part.get_payload(decode=True).decode("utf-8")
        self.assertNotIn("data:image", decoded)
        self.assertIn("cid:" + image["Content-ID"][1:-1], decoded)
        self.assertEqual(image.get_content_type(), "image/png")
        self.assertEqual(image.get_content_disposition(), "inline")
        Image.open(BytesIO(image.get_payload(decode=True))).verify()

    def test_weekly_query_counts_union_of_days_not_first_application_days(self):
        # Two apps used on disjoint days: first application's count is NOT the week count.
        with mock.patch.object(usage_records.db_conn, "connect") as connect:
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [
                [{**a, "days_with_data": 1} for a in self.apps[:2]],
                [{"day": str(self.start+timedelta(days=i)), "app_name": a["app_name"], "seconds": 900}
                 for i, a in enumerate(self.apps[:2])]]
            summary = usage_records.get_range_summary("synthetic", self.start, self.end)
        self.assertEqual(summary["days_with_data"], 2)
        self.assertEqual(len(summary["daily_apps"]), 2)


if __name__ == "__main__":
    unittest.main()
