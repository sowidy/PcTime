"""Pure, email-safe visualizations; original application totals remain authoritative."""
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import html

PALETTE = ("#2563eb", "#0f766e", "#9333ea", "#c2410c", "#be185d", "#475569")


def clock(bucket):
    minutes = bucket * 15
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def analyze_day(rows):
    bins = [defaultdict(float) for _ in range(96)]
    for r in rows:
        bins[int(r["bucket"])][r["app_name"]] += float(r["seconds"])
    totals = [sum(b.values()) for b in bins]
    winners = [max(b, key=lambda app: (b[app], app)) if b else None for b in bins]
    nonempty = [i for i, seconds in enumerate(totals) if seconds > 0]
    if not nonempty:
        return None
    hourly = [sum(totals[h * 4:h * 4 + 4]) for h in range(24)]
    switches, last, last_index = 0, None, -100
    spans = []
    for i, winner in enumerate(winners):
        if winner:
            if last and i - last_index <= 3 and winner != last:
                switches += 1
            last, last_index = winner, i
            if totals[i] >= 450:
                if spans and spans[-1][2] == winner and spans[-1][1] == i:
                    spans[-1] = (spans[-1][0], i + 1, winner)
                else:
                    spans.append((i, i + 1, winner))
    focus = sorted((s for s in spans if s[1] - s[0] >= 2), key=lambda s: s[0] - s[1])[:3]
    return {"bins": bins, "hourly": hourly, "first": nonempty[0], "last": nonempty[-1] + 1,
            "peak": max(range(24), key=lambda h: hourly[h]), "switches": switches,
            "focus": focus, "seconds": sum(totals)}


def render(summary, timeline_rows, site_rows, start, end, readiness=()):
    """Return (text, html), bounded in size; never issue network/database calls."""
    names = {r["app_name"]: r.get("display_name") or r["app_name"] for r in summary.get("apps", [])}
    grouped = defaultdict(list)
    for r in timeline_rows:
        grouped[str(r["day"])].append(r)
    total = float(summary["total_seconds"])
    coverage = sum(float(r["seconds"]) for r in timeline_rows)
    text, markup = [], []
    from mail.report import format_duration
    if timeline_rows:
        text.append("应用使用节奏（15 分钟粒度估算）")
        markup.append('<h2 style="font-size:18px;color:#334155;margin-top:28px">应用使用节奏</h2>')
        if abs(total - coverage) > max(60, total * .01):
            warning = "时间线数据不完整，保留应用汇总与排行。"
            text.append(warning)
            markup.append(f"<p>{warning}</p>")
        elif start == end:
            analysis = analyze_day(timeline_rows)
            if analysis:
                peak = analysis["peak"]
                line = (f"活跃约 {clock(analysis['first'])}–{clock(analysis['last'])} · "
                        f"峰值 {peak:02d}:00–{peak + 1:02d}:00 · 主导应用变化 {analysis['switches']} 次")
                text.append(line)
                markup.append(f'<p style="font-size:13px;color:#475569">{line}</p>')
                apps = [r["app_name"] for r in summary.get("apps", [])[:6]]
                colors, occupied = {}, set()
                for app in apps:
                    index = int(hashlib.sha256(app.encode()).hexdigest()[:8], 16) % len(PALETTE)
                    while index in occupied:
                        index = (index + 1) % len(PALETTE)
                    occupied.add(index)
                    colors[app] = PALETTE[index]
                cells = []
                spans = []
                for i in range(48):
                    combined = defaultdict(float)
                    for b in analysis["bins"][i * 2:i * 2 + 2]:
                        for app, seconds in b.items():
                            combined[app] += seconds
                    winner = max(combined, key=combined.get) if combined else None
                    label = names.get(winner, winner or "未记录")
                    color = colors.get(winner, "#94a3b8" if winner else "#f1f5f9")
                    cells.append(f'<td width="7%" height="18" bgcolor="{color}" '
                                 f'title="{clock(i*2)} {html.escape(label, quote=True)}" '
                                 'style="border:1px solid #fff;font-size:1px">&nbsp;</td>')
                    if winner:
                        if spans and spans[-1][2] == winner and spans[-1][1] == i:
                            spans[-1] = (spans[-1][0], i + 1, winner)
                        else:
                            spans.append((i, i + 1, winner))
                lines = []
                for row in range(4):
                    lines.append(f'<tr><td style="font-size:11px;white-space:nowrap">{row*6:02d}–{row*6+6:02d}</td>'
                                 + "".join(cells[row * 12:row * 12 + 12]) + "</tr>")
                markup.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
                              + "".join(lines) + "</table>")
                markup.append('<p style="font-size:12px;line-height:2">' + " · ".join(
                    f'<span style="color:{color}">■</span> {html.escape(names.get(app, app))}'
                    for app, color in colors.items()) + ' · 灰色：其他/未记录</p>')
                for a, b, app in spans[:8]:
                    text.append(f"{clock(a*2)}–{clock(b*2)} {names.get(app, app)}")
                if len(spans) > 8:
                    text.append(f"另有 {len(spans)-8} 个时段")
                for a, b, app in analysis["focus"]:
                    line = f"连续主导时段（估算）：{clock(a)}–{clock(b)} {names.get(app, app)}"
                    text.append(line)
                    markup.append(f'<p style="font-size:12px">{html.escape(line)}</p>')
        else:
            markup.append('<p style="font-size:12px">小时热力分布（空白表示无时间线数据）</p>')
            rows, analyses = [], []
            cursor = start
            while cursor <= end:
                analysis = analyze_day(grouped[str(cursor)])
                if analysis:
                    analyses.append(analysis)
                cells = []
                for h in range(24):
                    ratio = min(1, analysis["hourly"][h] / 3600) if analysis else 0
                    shade = ("#f1f5f9", "#bfdbfe", "#60a5fa", "#2563eb")[min(3, int(ratio * 3.99))]
                    cells.append(f'<td height="15" bgcolor="{shade}" style="border:1px solid #fff;font-size:1px">&nbsp;</td>')
                rows.append(f'<tr><td style="font-size:11px;white-space:nowrap">{cursor:%m-%d}</td>' + "".join(cells) + '</tr>')
                text.append(f"{cursor}: {format_duration(analysis['seconds']) if analysis else '无时间线数据'}")
                cursor += timedelta(days=1)
            markup.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
                          '<tr><td></td><td colspan="6">00</td><td colspan="6">06</td>'
                          '<td colspan="6">12</td><td colspan="6">18</td></tr>' + "".join(rows) + '</table>')
            if analyses:
                hourly = [sum(a["hourly"][h] for a in analyses) for h in range(24)]
                peak = max(range(24), key=lambda h: hourly[h])
                line = f"一周最活跃小时：{peak:02d}:00–{peak+1:02d}:00"
                text.append(line)
                markup.append(f'<p style="font-size:12px">{line}</p>')
                if len(analyses) > 1:
                    first = round(sum(a["first"] for a in analyses) / len(analyses))
                    last = round(sum(a["last"] for a in analyses) / len(analyses))
                    line = f"有记录 {len(analyses)} 天 · 平均活跃跨度约 {clock(first)}–{clock(last)}（按自然日估算）"
                    text.append(line)
                    markup.append(f'<p style="font-size:12px">{line}</p>')
                # Per-day bars show the same timeline distribution, without adding another total.
                highest = max(a["seconds"] for a in analyses)
                bars = []
                for day, data in sorted(grouped.items()):
                    seconds = sum(float(r["seconds"]) for r in data)
                    width = max(1, round(seconds * 100 / highest))
                    bars.append(f'<tr><td style="font-size:11px;white-space:nowrap">{day[5:]}</td><td width="65%">'
                        f'<table role="presentation" width="{width}%" cellspacing="0"><tr>'
                        '<td height="8" bgcolor="#2563eb" style="font-size:1px">&nbsp;</td></tr></table></td>'
                        f'<td style="font-size:11px">{format_duration(seconds)}</td></tr>')
                markup.append('<table role="presentation" width="100%">' + "".join(bars) + '</table>')
    if site_rows:
        sites = defaultdict(float)
        for r in site_rows:
            sites[r["host"]] += float(r["seconds"])
        site_total = sum(sites.values())
        label = f"网站浏览明细：{format_duration(site_total)}（已包含在浏览器应用时长中）"
        text.extend(["", label])
        markup.append(f'<h2 style="font-size:18px;margin-top:28px">网站浏览明细</h2><p>{html.escape(label)}</p>')
        rows = []
        for host, seconds in sorted(sites.items(), key=lambda pair: -pair[1])[:15]:
            share = 100 * seconds / site_total if site_total else 0
            text.append(f"{host}: {format_duration(seconds)} ({share:.1f}%)")
            rows.append(f'<tr><td style="padding:6px;word-break:break-all">{html.escape(host)}</td>'
                        f'<td>{format_duration(seconds)}</td><td>{share:.1f}%</td></tr>')
        markup.append('<table width="100%" style="font-size:13px">' + "".join(rows) + '</table>')
    if timeline_rows or site_rows:
        complete = {(str(r["day"]), r["kind"]) for r in readiness if r["complete"]}
        if any((str(d), kind) not in complete for kind, data in (("apps", [True]), ("timeline", timeline_rows), ("web", site_rows))
               if data for d in (start + timedelta(days=i) for i in range((end-start).days + 1))):
            note = "部分日期未确认日终上传完成，按最后收到的数据展示。"
            text.append(note)
            markup.append(f'<p style="color:#92400e;font-size:12px">{note}</p>')
    return "\n".join(text), "".join(markup)


def append_sections(message, summary, timeline_rows, site_rows, start, end, readiness=()):
    subject, text, body = message
    extra_text, extra_html = render(summary, timeline_rows, site_rows, start, end, readiness)
    if not extra_html and not extra_text:
        return message
    if len((body + extra_html).encode("utf-8")) <= 80000:
        if "<!-- tracking-sections -->" in body:
            body = body.replace("<!-- tracking-sections -->", extra_html)
        else:
            body = body.replace("</body>", extra_html + "</body>")
    if extra_text:
        from mail.report import FOOTER_TEXT
        if FOOTER_TEXT in text:
            text = text.replace(FOOTER_TEXT, extra_text + "\n\n" + FOOTER_TEXT)
        else:
            text += "\n\n" + extra_text
    return subject, text, body
