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
from mail import charts, colors, report, sender, site_categories, site_names, tracking_report


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

    def test_stacked_bars_use_distinct_category_colors_and_dividers(self):
        model = charts.prepare(["Day"], [{"blue": 3600, "orange": 2400,
                                          "green": 1200, "other": 60}], [True], limit=3)
        self.assertEqual([model["colors"][key] for key in model["series"][:3]],
                         list(charts.STACK_PALETTE[:3]))
        self.assertEqual(model["colors"][charts.OTHER], charts.theme.OTHER)
        self.assertEqual(len(set(charts.STACK_PALETTE)), len(charts.STACK_PALETTE))
        tile = Image.new("RGB", (8, 8), "#FFFFFF")
        charts._bar_segment(tile, (0, 0, 8, 8), charts.STACK_PALETTE[0], separator=True)
        self.assertEqual(tile.getpixel((4, 0)), (255, 255, 255))
        self.assertNotEqual(tile.getpixel((4, 3)), (255, 255, 255))

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
        self.assertIn("蓝线：每日总时长", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("https://", body)
        self.assertEqual(len(charts.IMAGE_DATA.findall(body)), 1)

    def test_sixty_apps_forty_sites_keep_charts_without_old_timeline(self):
        bins = [{"day": row["day"], "bucket": i % 24 * 4, "app_name": row["app_name"], "seconds": 900}
                for i, row in enumerate(self.daily)]
        sites = [{"day": str(self.start), "host": f"site-{i:02d}.example", "seconds": 100} for i in range(41)]
        message = tracking_report.append_sections(report.build_weekly_message(self.end, self.summary),
            self.summary, bins, sites, self.start, self.end)
        self.assertEqual(message[2].count("data:image/png;base64,"), 5)
        self.assertIn("一周应用使用趋势", message[2])
        self.assertIn('data-chart-style="weekly-app-heatmap"', message[2])
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
        self.assertNotIn("10-20：6 小时", message[2])
        self.assertIn("分时缺失：", message[2])
        self.assertIn("10-22", message[2])

    def test_weekly_without_daily_detail_never_fabricates_daily_values(self):
        summary = {k: v for k, v in self.summary.items() if k != "daily_apps"}
        body = report.build_weekly_html(self.end, summary)
        self.assertIn("应用时长构成", body)
        self.assertIn("不推算每天/小时的分布", body)
        self.assertNotIn("10-20：", body)

    def test_complete_empty_day_is_zero_but_missing_day_is_not(self):
        ready = [{"day": str(self.start+timedelta(days=1)), "kind": "web_hourly", "complete": True}]
        body = charts.website_chart([{"day": str(self.start), "host": "test", "seconds": 60}],
                                    self.start, self.end, ready)
        days, hours = charts.website_hourly_model(
            [{"day": str(self.start), "host": "test", "seconds": 60}], [],
            self.start, self.end, ready)
        self.assertEqual(len(days), 7)
        self.assertIsNotNone(hours[1])
        self.assertEqual(sum(sum(bucket.values()) for bucket in hours[1]), 0)
        self.assertIsNone(hours[2])
        self.assertIn("分时缺失：", body)
        self.assertIn("10-21", body)

    def test_daily_app_gantt_replaces_bars_and_hides_tiny_legend_entries(self):
        apps = [{**app, "seconds": seconds} for app, seconds in zip(self.apps[:3], (900, 60, 30))]
        summary = {"apps": apps, "total_seconds": 990, "app_count": 3}
        intervals = [{"day": str(self.start), "app_name": app["app_name"],
                      "start_second": start, "end_second": start + app["seconds"]}
                     for app, start in zip(apps, (32400, 33300, 33360))]
        message = tracking_report.append_sections(report.build_message(self.start, summary),
            summary, [], [], self.start, self.start, app_intervals=intervals)
        body = message[2]
        self.assertNotIn('data-chart-style="daily-app-bars"', body)
        self.assertIn("软件使用时段和时长", body)
        self.assertIn("Application 00 · 15 分", body)
        self.assertIn("Application 01 · 1 分", body)
        self.assertNotIn("Application 02 ·", body)
        self.assertIn("另有 1 个软件", body)
        self.assertIn("已计入总时长", body)

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
        self.assertIn("柱顶：每小时总时长", daily)
        self.assertIn("网站类型占比", daily)
        self.assertEqual(daily.count("data:image/png;base64,"), 2)
        self.assertNotEqual(
            charts.png(charts.prepare([f"{hour:02d}" for hour in range(24)],
                                      [{"github.com": 600} if hour == 9 else {} for hour in range(24)],
                                      [True] * 24, 8), show_line=False),
            charts.png(charts.prepare([f"{hour:02d}" for hour in range(24)],
                                      [{"github.com": 600} if hour == 9 else {} for hour in range(24)],
                                      [True] * 24, 8), show_line=False, show_totals=True))
        self.assertIn('<td align="center" style="text-align:center"><img data-chart-style="website-category-pie"',
                      daily)
        self.assertIn("工作学习", daily)
        self.assertIn("视频娱乐", daily)
        weekly = charts.website_chart(rows, self.start, self.end, hourly_rows=hourly)
        self.assertIn('data-chart-style="weekly-web-heatmap"', weekly)
        self.assertIn("分时缺失：10-20", weekly)
        self.assertIsNone(charts.website_hourly_model(rows, hourly[:1], self.start, self.start)[1][0])

    def test_weekly_heatmap_has_readable_monotone_duration_scale(self):
        colors = [charts.website_heatmap_color(seconds, 600)
                  for seconds in (0, 1, 150, 300, 450, 600)]
        brightness = [sum(int(color[at:at + 2], 16) for at in (1, 3, 5)) for color in colors]
        self.assertEqual(len(set(colors)), len(colors))
        self.assertEqual(brightness, sorted(brightness, reverse=True))
        self.assertNotEqual(charts.HEATMAP_ZERO, charts.HEATMAP_MISSING)
        self.assertEqual(charts.website_heatmap_color(0, 0), charts.HEATMAP_ZERO)
        self.assertEqual(charts.website_heatmap_color(150, 600), "#B4FBDE")
        self.assertEqual(charts.website_heatmap_color(600, 600), "#122549")
        warm = [charts.application_heatmap_color(seconds, 600)
                for seconds in (0, 1, 150, 300, 450, 600)]
        warm_brightness = [sum(int(color[at:at + 2], 16) for at in (1, 3, 5)) for color in warm]
        self.assertEqual(len(set(warm)), len(warm))
        self.assertEqual(warm_brightness, sorted(warm_brightness, reverse=True))
        self.assertEqual(charts.application_heatmap_color(450, 600), "#F33D15")
        self.assertEqual(charts.application_heatmap_color(600, 600), "#77312F")
        self.assertNotEqual(charts.APP_HEATMAP_ZERO, charts.HEATMAP_MISSING)

    def test_weekly_application_heatmap_checks_daily_bins_and_missing_days(self):
        summary = {"daily_apps": [
            {"day": str(self.start), "app_name": "editor.exe", "seconds": 900},
            {"day": str(self.start + timedelta(days=1)), "app_name": "editor.exe", "seconds": 900}]}
        bins = [
            {"day": str(self.start), "bucket": 36, "app_name": "editor.exe", "seconds": 900},
            {"day": str(self.start + timedelta(days=1)), "bucket": 40, "app_name": "editor.exe", "seconds": 300}]
        ready = [{"day": str(self.start + timedelta(days=2)), "kind": kind, "complete": True}
                 for kind in ("apps", "timeline")]
        days, hours = charts.application_hourly_model(summary, bins, self.start, self.end, ready)
        self.assertEqual(len(days), 7)
        self.assertEqual(sum(hours[0][9].values()), 900)
        self.assertIsNone(hours[1])  # Incomplete bins must not pretend to cover the day.
        self.assertEqual(sum(sum(hour.values()) for hour in hours[2]), 0)
        self.assertIsNone(hours[3])
        body = charts.weekly_application_heatmap(summary, bins, self.start, self.end, ready)
        self.assertIn('data-chart-style="weekly-app-heatmap"', body)
        self.assertNotIn("10-19：15 分", body)
        self.assertIn("分时缺失：10-20", body)
        self.assertNotIn("10-21：0 秒", body)

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
        self.assertIn("蓝线：每日总时长", body)
        self.assertIn("叠加总时长折线", body)
        self.assertNotIn("10-19：15 分", body)
        self.assertNotIn("10-20：0 秒", body)
        self.assertIn("未收到：10-22", body)
        self.assertEqual(len(charts.IMAGE_DATA.findall(body)), 1)

    def test_website_stack_only_groups_a_short_small_tail(self):
        meaningful = [{"day": str(self.start), "host": f"site-{index:02d}.example", "seconds": 120}
                      for index in range(36)]
        tiny = [{"day": str(self.start), "host": f"tiny-{index:02d}.example", "seconds": 1}
                for index in range(10)]
        self.assertEqual(charts.website_series_limit(meaningful + tiny), 36)
        body = charts.weekly_website_stacked_chart(meaningful + tiny, self.start, self.end)
        self.assertIn("site-35.example", body)
        self.assertIn("其他（10 网站）", body)
        self.assertEqual(charts.website_series_limit(meaningful + [
            {"day": str(self.start), "host": f"more-{index:02d}.example", "seconds": 120}
            for index in range(14)]), 50)
        self.assertEqual(site_categories.classify("docs.qq.com"), "工作学习")
        self.assertEqual(site_categories.classify("21st.dev"), "工作学习")
        self.assertEqual(site_categories.classify("mail.proton.me"), "工具类")

    def test_category_pie_uses_separated_flat_slices_and_legend_above(self):
        hosts = ("x.com", "youtube.com", "github.com", "bbc.com",
                 "taobao.com", "google.com", "unknown.example")
        body = charts.website_category_chart(
            [{"host": host, "seconds": 600} for host in hosts])
        self.assertLess(body.index("社交媒体"), body.index('data-chart-style="website-category-pie"'))
        image = Image.open(BytesIO(base64.b64decode(charts.IMAGE_DATA.search(body).group(1))))
        self.assertEqual(image.size, (600, 400))
        raw = image.convert("RGB").tobytes()
        pixels = {raw[index:index + 3] for index in range(0, len(raw), 3)}
        for color in charts.PIE_PALETTE:
            self.assertIn(bytes.fromhex(color[1:]), pixels)
        self.assertIn(b"\xff\xff\xff", pixels)  # White gutters separate the slices.
        self.assertIn(bytes.fromhex(charts.theme.TEXT[1:]), pixels)  # Percentage text is in the pie.
        self.assertNotIn("社交媒体：", body)  # The legend only carries names and durations.

    def test_daily_gantt_uses_only_real_complete_intervals(self):
        sites = [{"day": str(self.start), "host": "github.com", "seconds": 900}]
        intervals = [{"day": str(self.start), "host": "github.com", "start_second": 32400,
                      "end_second": 33300}]
        body = charts.website_gantt_chart(sites, intervals, self.start)
        self.assertIn('data-chart-style="website-gantt"', body)
        image = base64.b64decode(charts.IMAGE_DATA.search(body).group(1), validate=True)
        Image.open(BytesIO(image)).verify()
        self.assertIn("GitHub · 15 分", body)
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

    def test_website_gantt_only_omits_hosts_below_one_minute(self):
        rows = [{"day": str(self.start), "host": f"regular-{index:02d}.example", "seconds": 60}
                for index in range(30)]
        rows += [{"day": str(self.start), "host": f"short-{index:02d}.example", "seconds": 59}
                 for index in range(10)]
        intervals = [{"day": str(self.start), "host": row["host"],
                      "start_second": 32400 + index * 65,
                      "end_second": 32400 + index * 65 + row["seconds"]}
                     for index, row in enumerate(rows[:30])]
        body = charts.website_gantt_chart(rows, intervals, self.start)
        self.assertIn('data-chart-style="website-gantt"', body)
        self.assertIn("regular-29.example", body)  # No former top-24 truncation.
        self.assertNotIn("short-00.example", body)
        self.assertIn("另有 10 个网站用时不足 1 分钟未展示", body)
        image = Image.open(BytesIO(base64.b64decode(charts.IMAGE_DATA.search(body).group(1))))
        self.assertEqual(image.size[0], 1200)
        only_short = charts.website_gantt_chart(rows[30:], [], self.start)
        self.assertNotIn('data-chart-style="website-gantt"', only_short)
        self.assertIn("均不足 1 分钟", only_short)

    def test_known_site_names_are_display_only_and_unknown_hosts_stay_distinct(self):
        self.assertEqual(site_names.names_for_hosts(["github.com", "www.douyin.com", "unknown.example"]),
                         {"github.com": "GitHub", "www.douyin.com": "抖音",
                          "unknown.example": "unknown.example"})
        self.assertEqual(site_names.names_for_hosts(["github.com", "www.github.com"]),
                         {"github.com": "GitHub (github.com)",
                          "www.github.com": "GitHub (www.github.com)"})
        self.assertEqual(site_names.names_for_hosts(["www.douyin.com"], chart=True),
                         {"www.douyin.com": "Douyin"})
        rows = [{"day": str(self.start), "host": "www.douyin.com", "seconds": 120},
                {"day": str(self.start), "host": "unknown.example", "seconds": 60}]
        intervals = [{"day": str(self.start), "host": row["host"],
                      "start_second": 32400 + index * 120,
                      "end_second": 32400 + index * 120 + row["seconds"]}
                     for index, row in enumerate(rows)]
        gantt = charts.website_gantt_chart(rows, intervals, self.start)
        weekly = charts.weekly_website_stacked_chart(rows, self.start, self.end)
        self.assertIn("抖音 · 2 分", gantt)
        self.assertIn("unknown.example · 1 分", gantt)
        self.assertIn("抖音", weekly)
        self.assertIn("unknown.example", weekly)

    def test_daily_application_gantt_uses_real_intervals_and_remains_optional(self):
        summary = {"total_seconds": 600, "app_count": 1,
                   "apps": [{"app_name": "editor.exe", "display_name": "<Editor>",
                             "seconds": 600, "launches": 1}]}
        intervals = [{"day": str(self.start), "app_name": "editor.exe",
                      "start_second": 33000, "end_second": 33600}]
        message = tracking_report.append_sections(report.build_message(self.start, summary),
            summary, [], [], self.start, self.start, app_intervals=intervals)
        self.assertIn('data-chart-style="application-gantt"', message[2])
        self.assertIn("软件使用时段和时长", message[2])
        self.assertIn("&lt;Editor&gt;", message[2])
        Image.open(BytesIO(base64.b64decode(charts.IMAGE_DATA.findall(message[2])[-1]))).verify()
        missing = tracking_report.append_sections(report.build_message(self.start, summary),
            summary, [], [], self.start, self.start)
        self.assertIn("尚未收到完整的软件起止时段", missing[2])
        self.assertNotIn('data-chart-style="application-gantt"', missing[2])
        weekly = tracking_report.append_sections(report.build_weekly_message(self.end, summary),
            summary, [], [], self.start, self.end, app_intervals=intervals)
        self.assertNotIn('data-chart-style="application-gantt"', weekly[2])

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
