"""Mail-safe, data-faithful charts rendered as headless PNG.

Axis labels are rasterized with a CJK-capable font when the server has one
(Windows ships Microsoft YaHei; many Linux boxes ship Noto CJK), so chart
rows can show Chinese display names; without one they fall back to the ASCII
keys, as before. Names and accessible values remain escaped HTML / plain
text outside the image.
"""
import base64
from collections import defaultdict
from datetime import timedelta
import html
from io import BytesIO
import math
import os
import re

from PIL import Image, ImageDraw, ImageFont

import config
from mail.colors import color_map
from mail import chart_theme as theme
from mail.site_categories import CATEGORIES, totals as category_totals
from mail.site_names import names_for_hosts, names_for_rows as site_names_for_rows

IMAGE_DATA = re.compile(r'data:image/png;base64,([A-Za-z0-9+/=]+)')
OTHER = "\x00other"
LINE = theme.LINE

# CJK-capable fonts for rasterized chart labels, first match wins.
# PCTIME_MAIL_CHART_CJK_FONT can point at an explicit font file.
_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\deng.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)
_cjk_font_cache = {}


def cjk_font(size):
    """A CJK-capable TrueType font for chart labels, or None.

    Pillow's bundled font has no CJK glyphs, which is why chart rows used
    to show ASCII keys while the HTML legend showed Chinese names. When the
    server has a CJK font, rows use the Chinese display names; otherwise
    they fall back to the ASCII keys, exactly as before.
    """
    if size in _cjk_font_cache:
        return _cjk_font_cache[size]
    configured = (getattr(config, "MAIL_CHART_CJK_FONT", "") or "").strip()
    paths = ([configured] if configured else []) + list(_CJK_FONT_CANDIDATES)
    for path in paths:
        try:
            if path and os.path.isfile(path):
                font = ImageFont.truetype(path, size)
                _cjk_font_cache[size] = font
                return font
        except Exception:
            continue
    _cjk_font_cache[size] = None
    return None
# Muted, medium-depth category colors for stacked bars only. Cool and warm
# neighbors remain distinct without the neon effect of fully saturated hues.
STACK_PALETTE = ("#5D7FB0", "#D18B56", "#589C7B", "#9C78AF",
                 "#CB6F75", "#4C9DAD", "#B79A4B", "#7089AE",
                 "#C07D9E", "#7C9E59", "#B87C64", "#678DA5")
# The reference pie uses separated, flat slices. These tones keep that look
# legible on the report's white background instead of copying its black canvas.
PIE_PALETTE = ("#6FA8E8", "#E9BA50", "#48B9A9", "#A88BDC",
               "#D4869D", "#72AEC4", "#9AA8BA")
SITE_CHART_MAX_SERIES = 80


def stack_color_map(keys):
    return {key: theme.OTHER if key == OTHER else STACK_PALETTE[index % len(STACK_PALETTE)]
            for index, key in enumerate(keys)}


def html_size(body):
    """CID images aren't part of the HTML body after SMTP packaging."""
    return len(IMAGE_DATA.sub("cid:chart", body).encode("utf-8"))


def prepare(labels, buckets, observed, limit=8):
    """Group small categories without dropping any seconds. None means missing."""
    totals = defaultdict(float)
    for bucket in buckets:
        for key, seconds in bucket.items():
            totals[key] += max(0, float(seconds))
    ordered = sorted(totals, key=lambda key: (-totals[key], key))
    visible = ordered[:limit]
    hidden = ordered[limit:]
    series = visible + ([OTHER] if hidden else [])
    values = []
    for bucket, present in zip(buckets, observed):
        values.append(([max(0, float(bucket.get(key, 0))) for key in visible] +
                       ([sum(max(0, float(bucket.get(key, 0))) for key in hidden)] if hidden else []))
                      if present else None)
    return {"labels": labels, "series": series, "values": values, "totals": totals,
            "hidden": hidden, "colors": stack_color_map(series)}


def website_series_limit(rows, *, weekly=False):
    """Keep meaningful hosts visible; aggregate only a small short-duration tail.

    The app chart keeps MAIL_CHART_SERIES as its cap. For website charts that
    number is a minimum, otherwise a fixed top-eight can hide most browsing.
    """
    totals = defaultdict(float)
    for row in rows:
        totals[str(row["host"])] += max(0.0, float(row["seconds"]))
    durations = sorted((value for value in totals.values() if value > 0), reverse=True)
    total = sum(durations)
    remaining = total
    minimum = max(1, config.MAIL_CHART_SERIES)
    tiny_seconds = 120 if weekly else 60
    for index, seconds in enumerate(durations):
        if (index >= minimum and remaining <= total * .05 and seconds < tiny_seconds):
            return index
        remaining -= seconds
        if index + 1 >= SITE_CHART_MAX_SERIES:
            return SITE_CHART_MAX_SERIES
    return max(1, len(durations))


def smooth_line(points, steps=16):
    """Monotone cubic interpolation: soft curves without fabricated extrema.

    The caller passes one observed run at a time, so missing dates stay gaps.
    Curves are visual interpolation only; original points and labels are kept.
    """
    if len(points) < 2:
        return list(points)
    widths = [b[0]-a[0] for a, b in zip(points, points[1:])]
    slopes = [(b[1]-a[1])/width for a, b, width in zip(points, points[1:], widths)]
    tangents = [slopes[0]] + [(a+b)/2 if a*b > 0 else 0 for a, b in zip(slopes, slopes[1:])] + [slopes[-1]]
    for i, slope in enumerate(slopes):
        if slope == 0:
            tangents[i] = tangents[i+1] = 0
        else:
            ratio = math.hypot(tangents[i]/slope, tangents[i+1]/slope)
            if ratio > 3:
                factor = 3/ratio
                tangents[i] *= factor
                tangents[i+1] *= factor
    sampled = [points[0]]
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(points, points[1:])):
        width = widths[i]
        for step in range(1, steps+1):
            t = step/steps
            height = ((2*t**3-3*t**2+1)*y0 + (t**3-2*t**2+t)*width*tangents[i] +
                      (-2*t**3+3*t**2)*y1 + (t**3-t**2)*width*tangents[i+1])
            sampled.append((x0+t*width, max(min(y0, y1), min(max(y0, y1), height))))
    return sampled


def line_runs(points):
    """None is missing data, not a zero or a point to connect across."""
    runs, current = [], []
    for point in points:
        if point is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(point)
    if current:
        runs.append(current)
    return runs


def _bar_segment(image, box, color, rounded=False, separator=False):
    """Subtle vertical color lift and rounded top cap; heights remain exact."""
    x0, y0, x1, y1 = box
    width, height = x1-x0, y1-y0
    if width <= 0 or height <= 0:
        return
    piece = Image.new("RGB", (width, height), color)
    brush = ImageDraw.Draw(piece)
    base = [int(color[i:i+2], 16) for i in (1, 3, 5)]
    for row in range(height):
        lift = .10*(1-row/max(1, height-1))
        fill = tuple(round(value+(255-value)*lift) for value in base)
        brush.line((0, row, width, row), fill=fill)
    if separator and height >= 5:
        brush.line((0, 0, width - 1, 0), fill=theme.BACKGROUND, width=2)
    mask = Image.new("L", (width, height), 255)
    if rounded:
        mask.paste(0, (0, 0, width, height))
        radius = min(11, width//3, height//2)
        brush = ImageDraw.Draw(mask)
        brush.rounded_rectangle((0, 0, width-1, height-1), radius=radius, fill=255)
        brush.rectangle((0, min(radius, height-1), width-1, height-1), fill=255)
    image.paste(piece, (x0, y0), mask)


def png(model, show_line=True, show_totals=False):
    """Render at 2x email size; gaps break the line rather than imply zero use."""
    image = Image.new("RGB", (1200, 510), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=23)
    small = ImageFont.load_default(size=20)
    total_font = ImageFont.load_default(size=16)
    left, right, top, bottom = 82, 1160, 48, 426
    values = model["values"]
    maximum = max((sum(row) / 3600 for row in values if row is not None), default=0)
    raw_step = max(maximum / 4, .1)
    power = 10 ** math.floor(math.log10(raw_step))
    step = next(v * power for v in (1, 2, 2.5, 5, 10) if v * power >= raw_step)
    ceiling = max(step * 4, math.ceil(maximum * 1.18 / step) * step)
    ticks = int(round(ceiling / step))
    def y(hours):
        return bottom - hours / ceiling * (bottom - top)
    for tick in range(ticks + 1):
        hours = tick * step
        height = round(y(hours))
        for x in range(left, right, 14):
            draw.line((x, height, min(x+6, right), height), fill=theme.GRID, width=2)
        draw.text((left - 14, height), f"{hours:g}", fill=theme.AXIS, font=small, anchor="rm")
    draw.text((left, 15), "h", fill=theme.AXIS, font=small)
    count = max(1, len(values))
    stride = (right - left) / count
    width = min(90, stride * .56)
    points = []
    labels = []
    for index, (label, row) in enumerate(zip(model["labels"], values)):
        x = left + stride * (index + .5)
        if count <= 8 or index % 3 == 0:
            draw.text((x, bottom + 23), label if count <= 8 else label[:2],
                      fill=theme.AXIS, font=small, anchor="mt")
        if row is None:
            draw.text((x, bottom - 18), "--", fill=theme.AXIS, font=font, anchor="mm")
            points.append(None)
            continue
        base = 0
        last_positive = max((i for i, seconds in enumerate(row) if seconds > 0), default=-1)
        for part, (key, seconds) in enumerate(zip(model["series"], row)):
            if seconds > 0:
                _bar_segment(image, (round(x-width/2), round(y((base+seconds)/3600)),
                                     round(x+width/2), round(y(base/3600))),
                             model["colors"][key], rounded=part == last_positive,
                             separator=part != last_positive)
            base += seconds
        point = (round(x), round(y(base/3600)))
        points.append(point)
        if count <= 8:
            labels.append(((x, point[1] - 21), f"{base/3600:.1f}h"))
    if show_line:
        # Draw trend last so stacked bars never hide the smooth curve.
        draw = ImageDraw.Draw(image)
        for run in line_runs(points):
            if len(run) > 1:
                draw.line(smooth_line(run), fill=LINE, width=4, joint="curve")
            for x, height in run:
                draw.ellipse((x-7, height-7, x+7, height+7), fill=theme.BACKGROUND, outline=LINE, width=3)
        for position, label in labels:
            draw.text(position, label, fill=LINE, font=small, anchor="mb")
    if show_totals:
        draw = ImageDraw.Draw(image)
        for point, row in zip(points, values):
            if point is None or not row or sum(row) <= 0:
                continue
            seconds = sum(row)
            label = (f"{seconds / 3600:.1f}h" if seconds >= 3600 else
                     f"{seconds / 60:.0f}m" if seconds >= 60 else f"{seconds:.0f}s")
            draw.text((point[0], max(top + 18, point[1] - 8)), label,
                      fill=theme.TEXT, font=total_font, anchor="mb")
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render(labels, buckets, observed, names, title, note="", limit=8, noun="项", show_line=True,
           chart_style="echarts-inspired", show_totals=False):
    from mail.report import format_duration
    model = prepare(labels, buckets, observed, limit)
    encoded = base64.b64encode(png(model, show_line=show_line, show_totals=show_totals)).decode("ascii")
    legend = []
    for key in model["series"]:
        seconds = (sum(model["totals"][key] for key in model["hidden"]) if key == OTHER
                   else model["totals"][key])
        name = f"其他（{len(model['hidden'])} {noun}）" if key == OTHER else names.get(key, key)
        legend.append(f'<td width="50%" valign="top" style="padding:5px 8px;font-size:12px;color:#536176;word-break:break-all">'
                      f'<span style="color:{model["colors"][key]}">●</span> {html.escape(name)}'
                      f' <span style="color:#8894a5">{format_duration(seconds)}</span></td>')
    rows = "".join('<tr>' + "".join(legend[i:i+2]) + '</tr>' for i in range(0, len(legend), 2))
    missing = [label for label, row in zip(model["labels"], model["values"]) if row is None]
    details = [part for part in (note.strip(), f'未收到：{"、".join(missing)}' if missing else "") if part]
    hint = ("蓝线：每日总时长" if show_line else
            "柱顶：每小时总时长" if show_totals else "")
    hint_html = f'<p style="font-size:12px;color:#8592a3">{hint}</p>' if hint else ""
    details_html = (f'<p style="font-size:12px;color:#8592a3">{html.escape(" · ".join(details))}</p>'
                    if details else "")
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">{html.escape(title)}</h2>'
            f'{hint_html}'
            f'<img data-chart-style="{html.escape(chart_style, quote=True)}" width="600" height="255" style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="{html.escape(title, quote=True)}：堆叠柱状图{ "叠加总时长折线" if show_line else ""}" src="data:image/png;base64,{encoded}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
            f'{details_html}')


def _encode_png(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def application_chart(summary, start, end):
    apps = summary.get("apps") or []
    names = {a["app_name"]: a.get("display_name") or a["app_name"] for a in apps}
    limit = max(1, min(12, config.MAIL_CHART_SERIES))
    # Keep old row caps useful for text-only readers and explicit small settings.
    if config.MAIL_MAX_APPS > 0:
        limit = min(limit, config.MAIL_MAX_APPS)
    daily = summary.get("daily_apps")
    if start != end and daily is not None:
        days = [start + timedelta(days=i) for i in range((end-start).days+1)]
        buckets = [defaultdict(float) for _ in days]
        index = {str(day): i for i, day in enumerate(days)}
        present = {str(r["day"]) for r in daily}
        present.update(str(r["day"]) for r in summary.get("_readiness", ())
                       if r["kind"] == "apps" and r["complete"])
        for row in daily:
            if str(row["day"]) in index:
                buckets[index[str(row["day"])]][row["app_name"]] += float(row["seconds"])
        return render([d.strftime("%m-%d") for d in days], buckets, [str(d) in present for d in days],
                      names, "一周应用使用趋势", limit=limit, noun="应用")
    timeline = summary.get("_timeline_rows") or []
    total = float(summary.get("total_seconds") or 0)
    if start == end and timeline and abs(sum(float(r["seconds"]) for r in timeline)-total) <= max(60, total*.01):
        buckets = [defaultdict(float) for _ in range(24)]
        for row in timeline:
            buckets[int(row["bucket"])//4][row["app_name"]] += float(row["seconds"])
        return render([f"{h:02d}:00" for h in range(24)], buckets, [True]*24,
                      names, "当天应用使用趋势", "横轴为小时，依据已收到的 15 分钟时间线汇总；空白时段仅表示没有收到记录。", limit, "应用")
    totals = {a["app_name"]: float(a["seconds"]) for a in apps}
    return render(["Week" if start != end else start.strftime("%m-%d")], [totals], [True], names,
                  "应用时长构成", "仅收到周期汇总，展示单柱构成和总时长点，不推算每天/小时的分布。", limit, "应用")


# Browsers open websites, so a browser winning an hour earns a second lane
# showing that hour's most-used site.
BROWSER_APPS = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "arc.exe", "vivaldi.exe", "chromium.exe", "safari.exe", "360se.exe",
    "qqbrowser.exe", "sogouexplorer.exe", "2345explorer.exe", "liebao.exe",
})


def _merged_spans(spans):
    """Collapse overlapping or touching (start, end) spans, sorted."""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _hourly_winners(rows, day, key):
    """Per-hour winner from interval rows: {hour: (name, seconds)}.

    Same-name spans union first, so one app or site running on two devices
    at once counts once per wall-clock second: the winner is what owned
    the most of that hour, not what summed the most across devices.
    """
    by_name = defaultdict(list)
    for row in rows or []:
        if str(row.get("day", day)) != str(day):
            continue
        try:
            start, end = float(row["start_second"]), float(row["end_second"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            by_name[str(row[key])].append((start, end))
    winners = {}
    for hour in range(24):
        low, high = hour * 3600, (hour + 1) * 3600
        best, best_seconds = None, 0.0
        for name in sorted(by_name):
            seconds = sum(max(0.0, min(end, high) - max(start, low))
                          for start, end in _merged_spans(by_name[name]))
            if seconds > best_seconds:
                best, best_seconds = name, seconds
        if best is not None:
            winners[hour] = (best, best_seconds)
    return winners


def _winner_segments(winners):
    """Merge consecutive hours with the same winner into (start, end, name)."""
    segments = []
    for hour in sorted(winners):
        name = winners[hour][0]
        if segments and segments[-1][2] == name and segments[-1][1] == hour:
            segments[-1][1] = hour + 1
        else:
            segments.append([hour, hour + 1, name])
    return [(start, end, name) for start, end, name in segments]


def timeline_segments(summary, app_intervals, site_intervals, day, sources=None,
                      site_sources=None):
    """(app_segments, site_segments) for the daily winner timeline.

    Each segment is (start_hour, end_hour, key, label): the most-used app
    per hour, consecutive same-winner hours merged; the site lane covers
    only hours the winning app was a browser, showing that hour's
    most-used site. Labels are Chinese display names with the P/C/M
    marker, e.g. '抖音 (M)'. Empty when the day has no app intervals.
    """
    app_winners = _hourly_winners(app_intervals, day, "app_name")
    if not app_winners:
        return [], []
    names = {app["app_name"]: app.get("display_name") or app["app_name"]
             for app in summary.get("apps") or []}

    def marked(key, display, source_map):
        marker = (source_map or {}).get(key)
        return f"{display} ({marker})" if marker else display

    app_segments = [(start, end, key, marked(key, names.get(key, key), sources))
                    for start, end, key in _winner_segments(app_winners)]
    browser_hours = {hour for hour, (name, _) in app_winners.items()
                     if name.lower() in BROWSER_APPS}
    site_segments = []
    if browser_hours and site_intervals:
        site_winners = _hourly_winners(site_intervals, day, "host")
        site_winners = {hour: site_winners[hour] for hour in browser_hours
                        if hour in site_winners}
        site_names = names_for_hosts([name for _, (name, _) in
                                      sorted(site_winners.items())])
        site_segments = [(start, end, key,
                          marked(key, site_names.get(key, key), site_sources))
                         for start, end, key in _winner_segments(site_winners)]
    return app_segments, site_segments


def daily_winner_timeline(summary, app_intervals, site_intervals, day, sources=None,
                          site_sources=None):
    """One horizontal timeline for the top of the daily mail: each hour's
    most-used app, with a second lane showing each browser hour's
    most-used site. Segment names alternate above/below the bar so
    neighboring labels never collide. '' when the day has no app intervals.
    """
    app_segments, site_segments = timeline_segments(
        summary, app_intervals, site_intervals, day, sources, site_sources)
    if not app_segments:
        return ""
    label_font = cjk_font(14) or ImageFont.load_default(size=14)
    lane_font = cjk_font(16) or ImageFont.load_default(size=16)
    small = ImageFont.load_default(size=14)
    left, right = 90, 1160  # 24 hours at ~44.6px each; 14px "HH:00" is 35px, so labels clear.

    def x(hour):
        return left + (right - left) * hour / 24

    # One lane per row: (lane name, segments, bar top, bar bottom).
    lanes = [("软件", app_segments, 48, 84)]
    if site_segments:
        lanes.append(("网站", site_segments, 150, 186))
    height = 258 if site_segments else 156
    axis_y = height - 26
    image = Image.new("RGB", (1200, height), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    palette = color_map([key for _, _, key, _ in app_segments] +
                        [key for _, _, key, _ in site_segments])
    for hour in range(25):
        pos = round(x(hour))
        draw.line((pos, 40, pos, axis_y - 4), fill=theme.GRID, width=2)
    for lane_name, segments, top, bottom in lanes:
        draw.rounded_rectangle((left, top, right, bottom), radius=6, fill="#EDF0F3")
        for start, end, key, _ in segments:
            draw.rounded_rectangle((round(x(start)) + 1, top, round(x(end)) - 1, bottom),
                                   radius=6, fill=palette[key])
        draw.text((12, (top + bottom) // 2), lane_name, fill=theme.TEXT,
                  font=lane_font, anchor="lm")

        def fit_label(label):
            max_width = 150
            if label_font.getlength(label) <= max_width:
                return label
            if " (" in label:
                base, marker = label.rsplit(" (", 1)
                suffix = " (" + marker
            else:
                base, suffix = label, ""
            while len(base) > 1 and label_font.getlength(base + "..." + suffix) > max_width:
                base = base[:-1]
            return base + "..." + suffix

        for index, (start, end, _, label) in enumerate(segments):
            center = (x(start) + x(end)) / 2
            above = index % 2 == 0
            cy = top - 18 if above else bottom + 18
            draw.text((center, cy), fit_label(label), fill=theme.TEXT,
                      font=label_font, anchor="mm")
    for hour in range(25):
        draw.text((round(x(hour)), axis_y), f"{hour:02d}:00", fill=theme.AXIS,
                  font=small, anchor="mt")
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">今日时间线</h2>'
            f'<p style="font-size:12px;color:#8592a3">每小时使用最多的软件；浏览器时段另显示使用最多的网站</p>'
            f'<img src="data:image/png;base64,{encoded}" alt="今日时间线" '
            f'style="width:100%;height:auto;display:block" />')


def website_hourly_model(rows, hourly_rows, start, end, readiness=()):
    """Return 24 real hourly values per day; None means unavailable, never zero."""
    days = [start + timedelta(days=i) for i in range((end-start).days + 1)]
    daily_totals = defaultdict(float)
    for row in rows:
        daily_totals[str(row.get("day", start))] += max(0.0, float(row["seconds"]))
    buckets = {str(day): [defaultdict(float) for _ in range(24)] for day in days}
    observed = {str(r["day"]) for r in readiness if r["kind"] == "web_hourly" and r["complete"]}
    for row in hourly_rows:
        day = str(row.get("day", start))
        try:
            hour = int(row["hour"])
            seconds = float(row["seconds"])
            host = str(row["host"])
        except (KeyError, TypeError, ValueError):
            continue
        if day in buckets and 0 <= hour < 24 and seconds > 0 and host:
            buckets[day][hour][host] += seconds
            observed.add(day)
    result = []
    for day in days:
        key = str(day)
        hour_total = sum(sum(bucket.values()) for bucket in buckets[key])
        expected = daily_totals[key]
        if key not in observed or abs(hour_total - expected) > max(2, expected * .001):
            result.append(None)
        else:
            result.append(buckets[key])
    return days, result


# Inspired by the supplied visx palette, adapted to the report's light theme.
HEATMAP_ZERO = "#F4FBF7"
APP_HEATMAP_ZERO = "#FFF8F5"
HEATMAP_MISSING = "#D5D9E1"
HEATMAP_STOPS = ("#E5FAF0", "#B4FBDE", "#74CFC4", "#387A91", "#122549")
APP_HEATMAP_STOPS = ("#FFF0EB", "#F6B6A5", "#F67D5B", "#F33D15", "#77312F")


def _heatmap_color(seconds, maximum, stops, zero):
    """Interpolate a light-to-dark scale while keeping zero distinct from missing."""
    if seconds <= 0 or maximum <= 0:
        return zero
    position = min(1.0, seconds / maximum) * (len(stops) - 1)
    index = min(int(position), len(stops) - 2)
    blend = position - index
    start, end = stops[index:index + 2]
    rgb = [round(int(start[at:at + 2], 16) * (1 - blend) +
                 int(end[at:at + 2], 16) * blend) for at in (1, 3, 5)]
    return "#" + "".join(f"{value:02X}" for value in rgb)


def website_heatmap_color(seconds, maximum):
    return _heatmap_color(seconds, maximum, HEATMAP_STOPS, HEATMAP_ZERO)


def application_heatmap_color(seconds, maximum):
    return _heatmap_color(seconds, maximum, APP_HEATMAP_STOPS, APP_HEATMAP_ZERO)


def _weekly_hour_heatmap(days, buckets, title, chart_style, noun, color_scale):
    """Date × hour heatmap shared by real website and application snapshots."""
    image = Image.new("RGB", (1200, 480), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=19)
    left, top, cell_w, cell_h = 103, 76, 43, 45
    values = [sum(hour.values()) for day in buckets if day is not None for hour in day]
    maximum = max(values, default=0)
    for hour in range(24):
        if hour % 2 == 0:
            draw.text((left + hour * cell_w + 17, 41), f"{hour:02d}", fill=theme.AXIS,
                      font=font, anchor="mm")
    for row, day in enumerate(days):
        draw.text((4, top + row * cell_h + 16), day.strftime("%m-%d"), fill=theme.TEXT,
                  font=font, anchor="lm")
        for hour in range(24):
            seconds = None if buckets[row] is None else sum(buckets[row][hour].values())
            fill = HEATMAP_MISSING if seconds is None else color_scale(seconds, maximum)
            x, y = left + hour * cell_w, top + row * cell_h
            draw.rounded_rectangle((x, y, x + 36, y + 34), radius=6, fill=fill)
    draw.text((left, 399), "USE / HOUR", fill=theme.MUTED, font=font)
    for index, fraction in enumerate((0, .25, .5, .75, 1)):
        x = left + index * 112
        draw.rounded_rectangle((x, 424, x + 102, 444), radius=5,
                               fill=color_scale(maximum * fraction, maximum))
        value = round(maximum * fraction)
        label = (f"{value // 3600}h{value % 3600 // 60:02d}m" if value >= 3600 else
                 f"{value // 60}m{value % 60:02d}s" if value >= 60 else f"{value}s")
        draw.text((x + 51, 450), label, fill=theme.MUTED, font=font, anchor="mt")
    draw.rounded_rectangle((900, 424, 930, 448), radius=4, fill=HEATMAP_MISSING)
    draw.text((940, 427), "missing", fill=theme.MUTED, font=font)
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    missing = [day.strftime("%m-%d") for day, row in zip(days, buckets) if row is None]
    missing_html = (f'<p style="font-size:12px;color:#8592a3">分时缺失：{html.escape("、".join(missing))}</p>'
                    if missing else "")
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">{title}</h2>'
            '<p style="font-size:12px;color:#8592a3">日期 × 小时 · 深色用时更长 · 浅白为零 · 灰色未收到</p>'
            f'<img data-chart-style="{chart_style}" width="600" height="240" '
            f'style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="一周{noun}使用热力图：日期与小时" src="data:image/png;base64,{encoded}">'
            f'{missing_html}')


def weekly_website_heatmap(days, buckets):
    """Website snapshots: missing days are grey, never false zeroes."""
    return _weekly_hour_heatmap(days, buckets, "一周网站使用热力图", "weekly-web-heatmap", "网站",
                                website_heatmap_color)


def application_hourly_model(summary, timeline_rows, start, end, readiness=()):
    """Aggregate real 15-minute app bins into hours, validating each daily total."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    daily_apps = summary.get("daily_apps")
    if daily_apps is None:
        return days, [None] * len(days)
    expected = defaultdict(float)
    for row in daily_apps:
        expected[str(row["day"])] += max(0.0, float(row["seconds"]))
    buckets = {str(day): [defaultdict(float) for _ in range(24)] for day in days}
    present = set()
    for row in timeline_rows:
        day = str(row.get("day", start))
        try:
            bucket = int(row["bucket"])
            seconds = float(row["seconds"])
            app_name = str(row["app_name"])
        except (KeyError, TypeError, ValueError):
            continue
        if day in buckets and 0 <= bucket < 96 and math.isfinite(seconds) and seconds > 0 and app_name:
            buckets[day][bucket // 4][app_name] += seconds
            present.add(day)
    complete = {(str(row["day"]), row["kind"]) for row in readiness if row["complete"]}
    result = []
    for day in days:
        key = str(day)
        actual = sum(sum(hour.values()) for hour in buckets[key])
        total = expected[key]
        empty_complete = total == 0 and (key, "apps") in complete and (key, "timeline") in complete
        if not (key in present or empty_complete) or abs(actual - total) > max(2, total * .001):
            result.append(None)
        else:
            result.append(buckets[key])
    return days, result


def weekly_application_heatmap(summary, timeline_rows, start, end, readiness=()):
    """Use uploaded app bins only; never spread daily totals across hours."""
    if summary.get("daily_apps") is None:
        return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">一周软件使用热力图</h2>'
                '<p style="font-size:12px;color:#8592a3">尚未收到每日软件汇总，无法校验分时记录；不推算每小时用量。</p>')
    days, buckets = application_hourly_model(summary, timeline_rows, start, end, readiness)
    return _weekly_hour_heatmap(days, buckets, "一周软件使用热力图", "weekly-app-heatmap", "软件",
                                application_heatmap_color)


def website_category_chart(rows):
    """Centered, gently exploded category pie with a readable HTML legend."""
    from mail.report import format_duration
    totals = category_totals(rows, "seconds")
    if not totals:
        return ""
    total = sum(totals.values())
    scale = 3  # Supersample the curved edges before embedding in email.
    image = Image.new("RGB", (600 * scale, 400 * scale), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    angle = -90.0
    colors = {name: PIE_PALETTE[index] for index, (name, _) in enumerate(CATEGORIES)}
    separated = len(totals) > 1
    inside_labels, outside_labels = [], []
    for name, seconds in totals.items():
        sweep = seconds / total * 360
        middle = math.radians(angle + sweep / 2)
        offset = 8 if separated else 0
        center_x = 300 + offset * math.cos(middle)
        center_y = 200 + offset * math.sin(middle)
        radius = 170
        inset = min(1.1, sweep * .1) if separated else 0
        draw.pieslice(tuple(round(value * scale) for value in
                            (center_x-radius, center_y-radius,
                             center_x+radius, center_y+radius)),
                      angle + inset, angle + sweep - inset, fill=colors[name])
        share = seconds / total
        label = f"{share:.0%}" if share >= .01 else f"{share:.1%}"
        if share >= .08:
            inside_labels.append((center_x + radius * .62 * math.cos(middle),
                                  center_y + radius * .62 * math.sin(middle), label))
        else:
            side = 1 if math.cos(middle) >= 0 else -1
            outside_labels.append((side, center_x + radius * .98 * math.cos(middle),
                                   center_y + radius * .98 * math.sin(middle),
                                   max(24, min(376, 200 + 198 * math.sin(middle))),
                                   label, colors[name]))
        angle += sweep
    font = ImageFont.load_default(size=24 * scale)
    for x, y, label in inside_labels:
        draw.text((round(x * scale), round(y * scale)), label,
                  fill=theme.TEXT, font=font, anchor="mm")
    for side in (-1, 1):
        placed = 0
        for _, edge_x, edge_y, wanted_y, label, color in sorted(
                (entry for entry in outside_labels if entry[0] == side),
                key=lambda entry: entry[3]):
            y = max(wanted_y, placed + 26) if placed else wanted_y
            y = min(376, y)
            placed = y
            x = 82 if side < 0 else 518
            draw.line(((round(edge_x * scale), round(edge_y * scale)),
                       (round((x - side * 26) * scale), round(y * scale))),
                      fill=color, width=2 * scale)
            draw.text((x * scale, round(y * scale)), label,
                      fill=theme.TEXT, font=font, anchor="mm")
    image = image.resize((600, 400), Image.Resampling.LANCZOS)
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    legend = [
        f'<td width="50%" valign="top" style="padding:4px 8px;font-size:12px;color:#536176">'
        f'<span style="color:{colors[name]}">●</span> {html.escape(name)} · '
        f'{format_duration(seconds)}</td>' for name, seconds in totals.items()]
    legend_rows = "".join('<tr>' + "".join(legend[i:i+2]) + '</tr>'
                          for i in range(0, len(legend), 2))
    # Each category stays listed even when a narrow mail client wraps the table.
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">网站类型占比</h2>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{legend_rows}</table>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            '<tr><td align="center" style="text-align:center">'
            f'<img data-chart-style="website-category-pie" width="300" height="200" '
            f'style="display:block;width:300px;max-width:100%;height:auto;margin:0 auto" '
            f'alt="网站类型使用时长占比饼图" src="data:image/png;base64,{encoded}">'
            '</td></tr></table>')


def website_gantt_chart(rows, interval_rows, day, *, key="host", title="网站使用时段",
                        noun="网站", chart_style="website-gantt", display_names=None,
                        min_seconds=60, max_visible=None, allowed_gap=None, sources=None):
    """Daily real intervals on a time axis; never infer spans from totals.

    ``allowed_gap`` maps a host/app name to seconds the interval total may
    legitimately fall short of the listed total by. Merged mail passes the
    same-app cross-device overlap here: the listed per-app total sums both
    devices, the chart draws union spans, and the shortfall is exactly that
    overlap, not missing data. Without it the chart refuses to draw for any
    merged day. Single-device mail passes nothing, so its strict check is
    unchanged.

    ``sources`` maps a host/app name to 'P' (phone), 'C' (computer) or 'M'
    (both). Row labels and legend entries carry the marker, e.g. '抖音 (M)'.
    """
    from mail.report import format_duration
    display_names = display_names or (site_names_for_rows(rows) if key == "host" else {})
    chart_names = site_names_for_rows(rows, chart=True) if key == "host" else display_names
    totals = defaultdict(float)
    for row in rows:
        if str(row.get("day", day)) == str(day):
            totals[str(row[key])] += float(row["seconds"])
    if not totals:
        return ""
    ordered = sorted(totals, key=lambda name: (-totals[name], name))
    eligible = [name for name in ordered if totals[name] >= min_seconds]
    if key == "host" and not eligible:
        return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">{title}</h2>'
                '<p style="font-size:12px;color:#8592a3">当天网站用时均不足 1 分钟，'
                '已计入网站总时长，时段图不展示。</p>')
    # Website rows are not capped: only hosts below one minute are omitted.
    visible = eligible if key == "host" else (eligible[:max_visible] or ordered[:1])
    intervals = defaultdict(list)
    for row in interval_rows:
        if str(row.get("day", day)) != str(day):
            continue
        try:
            host = str(row[key])
            start, end = float(row["start_second"]), float(row["end_second"])
        except (KeyError, TypeError, ValueError):
            continue
        if host in totals and math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= 86400:
            intervals[host].append((start, end))
    required = visible if key == "host" else ordered
    allowed_gap = allowed_gap or {}
    if (key != "host" and set(intervals) != set(totals)) or any(
            host not in intervals or
            abs(sum(end - start for start, end in intervals[host]) - totals[host]) >
            max(2, totals[host] * .001) + float(allowed_gap.get(host, 0) or 0)
            for host in required):
        missing_note = ("尚未收到完整的网站起止时段；不根据每日总量或每小时汇总推造访问时间。"
                        if key == "host" else
                        "尚未收到完整的软件起止时段；不根据每日总量或每 15 分钟汇总推造使用时间。")
        return (f'<h2 style="font-size:18px;color:#34425c;margin:24px 0 8px">{title}</h2>'
                f'<p style="font-size:12px;color:#8592a3">{missing_note}</p>')

    visible_set = set(visible)
    hidden = [name for name in ordered if name not in visible_set]
    earliest = min(start for host in visible for start, _ in intervals[host])
    latest = max(end for host in visible for _, end in intervals[host])
    left_hour = max(0, math.floor(earliest / 3600))
    right_hour = min(24, max(left_hour + 4, math.ceil(latest / 3600)))
    if right_hour - left_hour < 4:
        left_hour = max(0, right_hour - 4)
    left, right, top, stride = 255, 1140, 74, 37
    bottom = top + len(visible) * stride
    height = bottom + 66
    image = Image.new("RGB", (1200, height), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    label_font = cjk_font(19)
    font = label_font or ImageFont.load_default(size=19)
    small = ImageFont.load_default(size=17)
    def x(seconds):
        return left + (seconds / 3600 - left_hour) * (right - left) / (right_hour - left_hour)
    for index in range(len(visible)):
        y = top + index * stride
        if index % 2 == 0:
            draw.rounded_rectangle((4, y - 3, 1182, y + 29), radius=6, fill="#F5F8FC")
    step = 1  # One tick per hour; the label font shrinks when a full day
    # leaves only ~37px per hour, so adjacent "HH:00" labels never overlap.
    hour_font = small if (right - left) / max(1, right_hour - left_hour) >= 46 \
        else ImageFont.load_default(size=14)
    for hour in range(left_hour, right_hour + 1):
        if hour == left_hour or hour == right_hour or hour % step == 0:
            pos = round(x(hour * 3600))
            draw.line((pos, top - 12, pos, bottom + 4), fill=theme.GRID, width=2)
            draw.text((pos, bottom + 16), f"{hour:02d}:00", fill=theme.AXIS, font=hour_font, anchor="mt")
    palette = color_map(visible)

    def label_parts(host):
        """(base name, ' (P)'/' (C)'/' (M)' suffix) for one row.

        The PNG label uses the Chinese name only when a CJK font is
        available; otherwise it falls back to the ASCII key, as before.
        """
        base = display_names.get(host, host) if label_font is not None \
            else chart_names.get(host, host)
        if label_font is None and any(ord(char) > 127 for char in base):
            base = host
        marker = (sources or {}).get(host)
        return base, f" ({marker})" if marker else ""

    for index, host in enumerate(visible):
        y = top + index * stride
        base, suffix = label_parts(host)
        label = base + suffix
        if font.getlength(label) > 230:
            # Shorten the name, keep the marker visible.
            while len(base) > 1 and font.getlength(base + "..." + suffix) > 230:
                base = base[:-1]
            label = base + "..." + suffix
        draw.text((12, y + 13), label, fill=theme.TEXT, font=font, anchor="lm")
        seconds = totals[host]
        duration = (f"{seconds / 3600:.1f}h" if seconds >= 3600 else
                    f"{seconds / 60:.0f}m" if seconds >= 60 else f"{seconds:.0f}s")
        draw.text((right + 10, y + 13), duration, fill=theme.MUTED,
                  font=small, anchor="lm")
        color = palette[host]
        for start, end in intervals[host]:
            a, b = round(x(start)), round(x(end))
            b = max(a + 2, b)  # Preserve a visible mark for very short real visits.
            draw.rounded_rectangle((a, y + 4, b, y + 23), radius=5, fill=color)
            draw.line((a + 2, y + 6, max(a + 2, b - 2), y + 6), fill="#F8FBFF", width=2)
    draw.text((left, 29), "TIME OF DAY", fill=theme.MUTED, font=small)
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    def legend_label(host):
        base = display_names.get(host, host)
        marker = (sources or {}).get(host)
        return f"{base} ({marker})" if marker else base

    legend = [f'<td width="50%" valign="top" style="padding:4px 8px;font-size:12px;color:#536176;word-break:break-all">'
              f'<span style="color:{palette[host]}">●</span> {html.escape(legend_label(host))} · '
              f'{format_duration(totals[host])}</td>' for host in visible]
    legend_rows = "".join("<tr>" + "".join(legend[i:i+2]) + "</tr>"
                          for i in range(0, len(legend), 2))
    hidden_note = (f'另有 {len(hidden)} 个{noun}'
                   f'{"用时不足 1 分钟" if key == "host" else ""}未展示 · '
                   f'合计 {format_duration(sum(totals[h] for h in hidden))}（已计入总时长）'
                   if hidden else "")
    hidden_html = f'<p style="font-size:12px;color:#8592a3">{hidden_note}</p>' if hidden_note else ""
    merged_note = ""
    if key != "host" and any(float(v or 0) > 0 for v in (allowed_gap or {}).values()):
        merged_note = ('<p style="font-size:12px;color:#8592a3">同一应用在多台设备'
                       '同时使用的时段已合并显示一次；图例时长为各设备合计。</p>')
    source_note = "（P 手机，C 电脑，M 两台都有）" if sources else ""
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">{title}</h2>'
            f'<p style="font-size:12px;color:#8592a3">时间 × {noun} · 图例为当天总时长{source_note}</p>'
            f'<img data-chart-style="{chart_style}" width="600" height="{height // 2}" '
            f'style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="{noun}使用甘特图，按名称展示真实使用时段" src="data:image/png;base64,{encoded}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{legend_rows}</table>'
            f'{hidden_html}{merged_note}')


def application_gantt_chart(summary, interval_rows, day, overlap_by_app=None, sources=None):
    """Render exact foreground app spans; 15-minute bins are never used as visits.

    ``overlap_by_app`` is the merged-mail allowance from
    mail.merge.per_app_overlap_seconds: same-app seconds two devices ran at
    once. The chart draws union spans, so without it any merged day with a
    same-app overlap fails the completeness check and loses its chart.
    ``sources`` maps each app to 'P' (phone), 'C' (computer) or 'M' (both).
    """
    apps = summary.get("apps") or []
    rows = [{"day": str(day), "app_name": app["app_name"], "seconds": app["seconds"]}
            for app in apps if float(app.get("seconds") or 0) > 0]
    names = {app["app_name"]: app.get("display_name") or app["app_name"] for app in apps}
    max_visible = min(40, config.MAIL_MAX_APPS) if config.MAIL_MAX_APPS > 0 else 40
    return website_gantt_chart(rows, interval_rows, day, key="app_name", title="软件使用时段和时长",
                               noun="软件", chart_style="application-gantt", display_names=names,
                               min_seconds=60, max_visible=max_visible,
                               allowed_gap=overlap_by_app, sources=sources)


def weekly_website_daily_model(rows, start, end, readiness=()):
    """Daily host totals; a complete empty day is zero, an absent day stays missing."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    index = {str(day): i for i, day in enumerate(days)}
    buckets = [defaultdict(float) for _ in days]
    observed = {str(row["day"]) for row in readiness
                if row["kind"] == "web" and row["complete"]}
    for row in rows:
        day = str(row.get("day", start))
        if day in index:
            buckets[index[day]][row["host"]] += max(0.0, float(row["seconds"]))
            observed.add(day)
    return days, buckets, [str(day) in observed for day in days]


def weekly_website_stacked_chart(rows, start, end, readiness=()):
    days, buckets, observed = weekly_website_daily_model(rows, start, end, readiness)
    limit = website_series_limit(rows, weekly=True)
    return render([day.strftime("%m-%d") for day in days], buckets, observed,
                  site_names_for_rows(rows),
                  "一周网站每日使用时长", "",
                  limit, "网站", show_line=True, chart_style="weekly-site-stacked")


def website_chart(rows, start, end, readiness=(), hourly_rows=()):
    days, hours = website_hourly_model(rows, hourly_rows, start, end, readiness)
    if start != end:
        chart = (weekly_website_stacked_chart(rows, start, end, readiness) +
                 weekly_website_heatmap(days, hours))
    elif hours[0] is None:
        chart = ('<h2 style="font-size:18px;color:#34425c;margin:24px 0 8px">每小时网站使用</h2>'
                 '<p style="font-size:12px;color:#8592a3">分时记录未收到，不推算每小时用量。</p>')
    else:
        buckets = hours[0]
        limit = website_series_limit(rows)
        chart = render([f"{hour:02d}:00" for hour in range(24)], buckets, [True] * 24,
                       site_names_for_rows(rows),
                       "每小时网站使用", "",
                       limit, "网站", show_line=False, show_totals=True)
    return chart + website_category_chart(rows)
