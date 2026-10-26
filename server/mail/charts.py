"""Mail-safe, data-faithful charts rendered as headless PNG.

Only ASCII axis labels are rasterized, so Linux needs neither Qt nor CJK fonts.
Names and accessible values remain escaped HTML / plain text outside the image.
"""
import base64
from collections import defaultdict
from datetime import timedelta
import html
from io import BytesIO
import math
import re

from PIL import Image, ImageDraw, ImageFont

import config
from mail.colors import color_map
from mail import chart_theme as theme
from mail.site_categories import CATEGORIES, totals as category_totals

IMAGE_DATA = re.compile(r'data:image/png;base64,([A-Za-z0-9+/=]+)')
OTHER = "\x00other"
LINE = theme.LINE


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
            "hidden": hidden, "colors": color_map(series)}


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


def _bar_segment(image, box, color, rounded=False):
    """Subtle vertical color lift and rounded top cap; heights remain exact."""
    x0, y0, x1, y1 = box
    width, height = x1-x0, y1-y0
    if width <= 0 or height <= 0:
        return
    piece = Image.new("RGB", (width, height), color)
    brush = ImageDraw.Draw(piece)
    base = [int(color[i:i+2], 16) for i in (1, 3, 5)]
    for row in range(height):
        lift = .16*(1-row/max(1, height-1))
        fill = tuple(round(value+(255-value)*lift) for value in base)
        brush.line((0, row, width, row), fill=fill)
    mask = Image.new("L", (width, height), 255)
    if rounded:
        mask.paste(0, (0, 0, width, height))
        radius = min(11, width//3, height//2)
        brush = ImageDraw.Draw(mask)
        brush.rounded_rectangle((0, 0, width-1, height-1), radius=radius, fill=255)
        brush.rectangle((0, min(radius, height-1), width-1, height-1), fill=255)
    image.paste(piece, (x0, y0), mask)


def png(model, show_line=True):
    """Render at 2x email size; gaps break the line rather than imply zero use."""
    image = Image.new("RGB", (1200, 510), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=23)
    small = ImageFont.load_default(size=20)
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
                             model["colors"][key], rounded=part == last_positive)
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
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render(labels, buckets, observed, names, title, note="", limit=8, noun="项", show_line=True,
           chart_style="echarts-inspired"):
    from mail.report import format_duration
    model = prepare(labels, buckets, observed, limit)
    # Match the timeline's summary-based application colors, even for ties.
    named_colors = color_map(names)
    for key in model["series"]:
        if key in named_colors:
            model["colors"][key] = named_colors[key]
    if OTHER in model["colors"]:
        model["colors"][OTHER] = theme.OTHER
    encoded = base64.b64encode(png(model, show_line=show_line)).decode("ascii")
    legend = []
    for key in model["series"]:
        seconds = (sum(model["totals"][key] for key in model["hidden"]) if key == OTHER
                   else model["totals"][key])
        name = f"其他（{len(model['hidden'])} {noun}）" if key == OTHER else names.get(key, key)
        legend.append(f'<td width="50%" valign="top" style="padding:5px 8px;font-size:12px;color:#536176;word-break:break-all">'
                      f'<span style="color:{model["colors"][key]}">●</span> {html.escape(name)}'
                      f' <span style="color:#8894a5">{format_duration(seconds)}</span></td>')
    rows = "".join('<tr>' + "".join(legend[i:i+2]) + '</tr>' for i in range(0, len(legend), 2))
    daily = " · ".join(f'{label}：{format_duration(sum(row)) if row is not None else "未收到"}'
                       for label, row in zip(model["labels"], model["values"])
                       if len(model["labels"]) <= 8 or row is None or sum(row) > 0)
    hidden_note = (f'另有 {len(model["hidden"])} 个{noun}合并为“其他”，合计 '
                   f'{format_duration(sum(model["totals"][k] for k in model["hidden"]))}，未丢弃时长。'
                   if model["hidden"] else "")
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">{html.escape(title)}</h2>'
            f'<p style="font-size:12px;color:#8592a3">柔色堆叠柱：{noun}时长构成 · '
            f'{"蓝色曲线：总时长 · " if show_line else ""}单位：小时</p>'
            f'<img data-chart-style="{html.escape(chart_style, quote=True)}" width="600" height="255" style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="{html.escape(title, quote=True)}：堆叠柱状图{ "叠加总时长折线" if show_line else ""}" src="data:image/png;base64,{encoded}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'
            f'<p style="font-size:11px;color:#8592a3;line-height:1.8">{html.escape(daily)}</p>'
            f'<p style="font-size:12px;color:#8592a3">{html.escape(note + hidden_note)}</p>')


def daily_application_bars(apps, limit=28):
    """A readable many-app daily chart; numbered bars map to an escaped HTML key."""
    from mail.report import format_duration
    ordered = sorted((a for a in apps if float(a.get("seconds") or 0) > 0),
                     key=lambda a: (-float(a["seconds"]), a["app_name"]))
    visible, hidden = ordered[:limit], ordered[limit:]
    if not visible:
        return '<p style="color:#8592a3">当天暂无应用使用记录。</p>'
    entries = [(a.get("display_name") or a["app_name"], float(a["seconds"])) for a in visible]
    if hidden:
        entries.append((f"其他（{len(hidden)} 个应用）", sum(float(a["seconds"]) for a in hidden)))
    height = 64 + len(entries) * 31
    image = Image.new("RGB", (1200, height), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    small = ImageFont.load_default(size=19)
    maximum = max(seconds for _, seconds in entries)
    palette = color_map(range(len(entries)))
    for index, (_, seconds) in enumerate(entries):
        y = 36 + index * 31
        draw.text((13, y + 11), f"{index + 1:02d}", fill=theme.MUTED, font=small, anchor="lm")
        draw.rounded_rectangle((54, y, 1030, y + 21), radius=8, fill=theme.GRID)
        width = max(3, round(seconds / maximum * 976))
        _bar_segment(image, (54, y, 54 + width, y + 21), palette[index], rounded=True)
        draw.text((1042, y + 11), f"{seconds / 3600:.1f}h", fill=theme.TEXT, font=small, anchor="lm")
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    legend = []
    for index, (name, seconds) in enumerate(entries):
        legend.append(f'<td width="50%" valign="top" style="padding:4px 8px;font-size:12px;color:#536176">'
                      f'<span style="color:{palette[index]}">●</span> {index + 1:02d} '
                      f'{html.escape(name)} · {format_duration(seconds)}</td>')
    rows = "".join("<tr>" + "".join(legend[i:i+2]) + "</tr>" for i in range(0, len(legend), 2))
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">当天软件使用时长</h2>'
            '<p style="font-size:12px;color:#8592a3">横条长度表示当天用时，序号对应下方应用名称。</p>'
            f'<img data-chart-style="daily-app-bars" width="600" height="{height // 2}" '
            f'style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="当天软件使用时长柱状图，共展示 {len(entries)} 类" src="data:image/png;base64,{encoded}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


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
    if start == end and summary.get("apps"):
        return daily_application_bars(apps, min(28, config.MAIL_MAX_APPS)
                                      if config.MAIL_MAX_APPS > 0 else 28)
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
                      names, "一周应用使用趋势", "未收到的日期留空并断开折线；有数据但未完成日终上传的日期按已收到值展示。", limit, "应用")
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


HEATMAP_ZERO = "#F6F9FC"
HEATMAP_MISSING = "#D5D9E1"
HEATMAP_STOPS = ("#E3F0FA", "#A5D2EE", "#69AEDA", "#418FC9", "#276DA8")


def website_heatmap_color(seconds, maximum):
    """Linear real-time scale with a wider light-to-dark range than the soft bars."""
    if seconds <= 0 or maximum <= 0:
        return HEATMAP_ZERO
    position = min(1.0, seconds / maximum) * (len(HEATMAP_STOPS) - 1)
    index = min(int(position), len(HEATMAP_STOPS) - 2)
    blend = position - index
    start, end = HEATMAP_STOPS[index:index + 2]
    rgb = [round(int(start[at:at + 2], 16) * (1 - blend) +
                 int(end[at:at + 2], 16) * blend) for at in (1, 3, 5)]
    return "#" + "".join(f"{value:02X}" for value in rgb)


def weekly_website_heatmap(days, buckets):
    """Date × hour heatmap. Missing snapshots use grey, not false zeroes."""
    from mail.report import format_duration
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
            fill = HEATMAP_MISSING if seconds is None else website_heatmap_color(seconds, maximum)
            x, y = left + hour * cell_w, top + row * cell_h
            draw.rounded_rectangle((x, y, x + 36, y + 34), radius=6, fill=fill)
    draw.text((left, 399), "USE / HOUR", fill=theme.MUTED, font=font)
    for index, fraction in enumerate((0, .25, .5, .75, 1)):
        x = left + index * 112
        draw.rounded_rectangle((x, 424, x + 102, 444), radius=5,
                               fill=website_heatmap_color(maximum * fraction, maximum))
        value = round(maximum * fraction)
        label = (f"{value // 3600}h{value % 3600 // 60:02d}m" if value >= 3600 else
                 f"{value // 60}m{value % 60:02d}s" if value >= 60 else f"{value}s")
        draw.text((x + 51, 450), label, fill=theme.MUTED, font=font, anchor="mt")
    draw.rounded_rectangle((900, 424, 930, 448), radius=4, fill=HEATMAP_MISSING)
    draw.text((940, 427), "missing", fill=theme.MUTED, font=font)
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    daily = " · ".join(f"{day:%m-%d}：{format_duration(sum(sum(h.values()) for h in row)) if row is not None else '未收到分时记录'}"
                       for day, row in zip(days, buckets))
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">一周网站使用热力图</h2>'
            '<p style="font-size:12px;color:#8592a3">纵轴日期 · 横轴小时 · 蓝色由浅到深对应每小时使用时长，色阶按本周最高小时用量标定；浅白为零使用，灰色为未收到分时记录。</p>'
            f'<img data-chart-style="weekly-web-heatmap" width="600" height="240" '
            f'style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="一周网站使用热力图：日期与小时" src="data:image/png;base64,{encoded}">'
            f'<p style="font-size:11px;color:#8592a3;line-height:1.8">{html.escape(daily)}</p>')


def website_category_chart(rows):
    """Soft pie from local host rules, with a readable HTML legend."""
    from mail.report import format_duration
    totals = category_totals(rows, "seconds")
    if not totals:
        return ""
    total = sum(totals.values())
    image = Image.new("RGB", (600, 400), theme.BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=23)
    angle = -90.0
    colors = dict(CATEGORIES)
    labels, boundaries = [], []
    for name, seconds in totals.items():
        sweep = seconds / total * 360
        color = colors[name]
        base = tuple(int(color[index:index+2], 16) for index in (1, 3, 5))
        draw.pieslice((115, 15, 485, 385), angle, angle + sweep, fill=color)
        for layer in range(1, 19):
            radius = 185 * (1 - layer / 19)
            lift = .19 * layer / 19
            fill = tuple(round(value + (255 - value) * lift) for value in base)
            draw.pieslice((300-radius, 200-radius, 300+radius, 200+radius),
                          angle, angle + sweep, fill=fill)
        boundaries.append(angle)
        if sweep >= 22:
            middle = math.radians(angle + sweep / 2)
            x = 300 + math.cos(middle) * 118
            y = 200 + math.sin(middle) * 118
            labels.append((x, y, f"{seconds / total:.0%}"))
        angle += sweep
    for boundary in boundaries:
        direction = math.radians(boundary)
        draw.line((300, 200, 300 + 185 * math.cos(direction),
                   200 + 185 * math.sin(direction)), fill=theme.BACKGROUND, width=4)
    draw.ellipse((115, 15, 485, 385), outline=theme.BACKGROUND, width=4)
    for x, y, label in labels:
        draw.text((x, y), label, fill="#283A52", font=font, anchor="mm")
    encoded = base64.b64encode(_encode_png(image)).decode("ascii")
    legend = [
        f'<td width="50%" valign="top" style="padding:4px 8px;font-size:12px;color:#536176">'
        f'<span style="color:{colors[name]}">●</span> {html.escape(name)}：{seconds / total:.0%} · '
        f'{format_duration(seconds)}</td>' for name, seconds in totals.items()]
    legend_rows = "".join('<tr>' + "".join(legend[i:i+2]) + '</tr>'
                          for i in range(0, len(legend), 2))
    # Each category stays listed even when a narrow mail client wraps the table.
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">网站类型占比</h2>'
            '<p style="font-size:12px;color:#8592a3">按域名本地规则归类；未知网站列为“其他”，不读取页面内容。</p>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            '<tr><td align="center" style="text-align:center">'
            f'<img data-chart-style="website-category-pie" width="300" height="200" '
            f'style="display:block;width:300px;max-width:100%;height:auto;margin:0 auto" '
            f'alt="网站类型使用时长占比饼图" src="data:image/png;base64,{encoded}">'
            '</td></tr></table>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{legend_rows}</table>')


def website_gantt_chart(rows, interval_rows, day):
    """Daily website sessions on a real time axis; never infer visits from totals."""
    from mail.report import format_duration
    totals = defaultdict(float)
    for row in rows:
        if str(row.get("day", day)) == str(day):
            totals[str(row["host"])] += float(row["seconds"])
    if not totals:
        return ""
    intervals = defaultdict(list)
    for row in interval_rows:
        if str(row.get("day", day)) != str(day):
            continue
        try:
            host = str(row["host"])
            start, end = float(row["start_second"]), float(row["end_second"])
        except (KeyError, TypeError, ValueError):
            continue
        if host in totals and math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= 86400:
            intervals[host].append((start, end))
    if set(intervals) != set(totals) or any(
            abs(sum(end - start for start, end in intervals[host]) - seconds) > max(2, seconds * .001)
            for host, seconds in totals.items()):
        return ('<h2 style="font-size:18px;color:#34425c;margin:24px 0 8px">网站使用时段</h2>'
                '<p style="font-size:12px;color:#8592a3">尚未收到完整的网站起止时段；不根据每日总量或每小时汇总推造访问时间。</p>')

    ordered = sorted(totals, key=lambda host: (-totals[host], host))
    visible, hidden = ordered[:24], ordered[24:]
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
    font = ImageFont.load_default(size=19)
    small = ImageFont.load_default(size=17)
    def x(seconds):
        return left + (seconds / 3600 - left_hour) * (right - left) / (right_hour - left_hour)
    for index in range(len(visible)):
        y = top + index * stride
        if index % 2 == 0:
            draw.rounded_rectangle((4, y - 3, 1182, y + 29), radius=6, fill="#F5F8FC")
    step = 1 if right_hour - left_hour <= 10 else 2 if right_hour - left_hour <= 18 else 3
    for hour in range(left_hour, right_hour + 1):
        if hour == left_hour or hour == right_hour or hour % step == 0:
            pos = round(x(hour * 3600))
            draw.line((pos, top - 12, pos, bottom + 4), fill=theme.GRID, width=2)
            draw.text((pos, bottom + 16), f"{hour:02d}:00", fill=theme.AXIS, font=small, anchor="mt")
    palette = color_map(visible)
    for index, host in enumerate(visible):
        y = top + index * stride
        label = host if len(host) <= 20 else host[:17] + "..."
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
    legend = [f'<td width="50%" valign="top" style="padding:4px 8px;font-size:12px;color:#536176;word-break:break-all">'
              f'<span style="color:{palette[host]}">●</span> {html.escape(host)} · '
              f'{format_duration(totals[host])}</td>' for host in visible]
    legend_rows = "".join("<tr>" + "".join(legend[i:i+2]) + "</tr>"
                          for i in range(0, len(legend), 2))
    hidden_note = (f'另有 {len(hidden)} 个网站未在图中逐项显示，共 {format_duration(sum(totals[h] for h in hidden))}。'
                   if hidden else "")
    return (f'<h2 style="font-size:18px;color:{theme.TEXT};margin:24px 0 8px">网站使用时段</h2>'
            '<p style="font-size:12px;color:#8592a3">横轴为当天时间，纵轴为网站；每段柔色横条对应一次真实使用时段。</p>'
            f'<img data-chart-style="website-gantt" width="600" height="{height // 2}" '
            f'style="display:block;width:100%;max-width:600px;height:auto" '
            f'alt="网站使用甘特图，按域名展示真实使用时段" src="data:image/png;base64,{encoded}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{legend_rows}</table>'
            f'<p style="font-size:12px;color:#8592a3">{hidden_note}</p>')


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
    limit = max(1, min(12, config.MAIL_CHART_SERIES))
    if config.MAIL_MAX_SITES > 0:
        limit = min(limit, config.MAIL_MAX_SITES)
    return render([day.strftime("%m-%d") for day in days], buckets, observed, {},
                  "一周网站每日使用时长",
                  "每根柱代表一天，柔色块为各网站时长，蓝色折线为当天网站总用时；"
                  "未收到的日期留空并断开折线，确认完成的空日期显示零。"
                  "网站时长已包含在浏览器应用中，不与应用总时长相加。",
                  limit, "网站", show_line=True, chart_style="weekly-site-stacked")


def website_chart(rows, start, end, readiness=(), hourly_rows=()):
    days, hours = website_hourly_model(rows, hourly_rows, start, end, readiness)
    if start != end:
        chart = (weekly_website_stacked_chart(rows, start, end, readiness) +
                 weekly_website_heatmap(days, hours))
    elif hours[0] is None:
        chart = ('<h2 style="font-size:18px;color:#34425c;margin:24px 0 8px">每小时网站使用</h2>'
                 '<p style="font-size:12px;color:#8592a3">尚未收到这一天的每小时记录；旧客户端仅上传每日汇总，不推算分时用量。</p>')
    else:
        buckets = hours[0]
        limit = max(1, min(12, config.MAIL_CHART_SERIES))
        if config.MAIL_MAX_SITES > 0:
            limit = min(limit, config.MAIL_MAX_SITES)
        chart = render([f"{hour:02d}:00" for hour in range(24)], buckets, [True] * 24, {},
                       "每小时网站使用", "依据网站真实使用片段汇总，网站时长已包含在浏览器应用中，不与应用总时长相加。",
                       limit, "网站", show_line=False)
    return chart + website_category_chart(rows)
