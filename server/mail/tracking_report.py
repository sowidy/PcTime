"""Pure email sections for website charts and text-only fallbacks."""
from collections import defaultdict
from datetime import timedelta
import html
import config
from mail.charts import (application_gantt_chart, daily_winner_timeline,
                         timeline_segments, weekly_application_heatmap,
                         website_chart, website_gantt_chart, html_size)
from mail.site_names import names_for_hosts


def _site_text(sites, text):
    """Text-only fallback detail; HTML shows stacked columns instead of a rank list."""
    from mail.report import format_duration
    ordered = sorted(sites.items(), key=lambda item: -item[1])
    shown = ordered[:config.MAIL_MAX_SITES] if config.MAIL_MAX_SITES > 0 else ordered
    total = sum(sites.values())
    labels = names_for_hosts(host for host, _ in ordered)
    for host, seconds in shown:
        share = 100 * seconds / total if total else 0
        text.append(f"{labels[host]}: {format_duration(seconds)} ({share:.1f}%)")
    if len(ordered) > len(shown):
        text.append(f"另有 {len(ordered)-len(shown)} 个网站未逐项列出，总时长仍包含这些网站。")


def render(summary, timeline_rows, site_rows, start, end, readiness=(), site_hours=(),
           site_intervals=(), app_intervals=()):
    """Return daily application spans and website sections."""
    from mail.report import format_duration
    weekly = start != end
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    dates = {str(day) for day in days}
    site_rows = [r for r in site_rows if str(r.get("day", start)) in dates]
    site_hours = [r for r in site_hours if str(r.get("day", start)) in dates]
    site_intervals = [r for r in site_intervals if str(r.get("day", start)) in dates]
    complete = {(str(r["day"]), r["kind"]) for r in readiness if r["complete"]}
    text, markup = [], []
    if not weekly:
        # The winner timeline opens the mail, ahead of the Gantt charts.
        app_segments, _, _ = timeline_segments(
            summary, app_intervals, site_intervals, start,
            sources=summary.get("_app_sources"))
        if app_segments:
            markup.append(daily_winner_timeline(
                summary, app_intervals, site_intervals, start,
                sources=summary.get("_app_sources")))

            def _hhmm(seconds):
                return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}"

            parts = []
            for seg_start, seg_end, _, label, subs in app_segments:
                part = f"{_hhmm(seg_start)}-{_hhmm(seg_end)} {label}"
                if subs:
                    part += "（" + "、".join(
                        f"{site} {_hhmm(sub_start)}-{_hhmm(sub_end)}"
                        for sub_start, sub_end, _, site in subs) + "）"
                parts.append(part)
            text.append("时间线：" + "；".join(parts))
    if not weekly and summary.get("apps"):
        markup.append(application_gantt_chart(
            summary, app_intervals, start,
            overlap_by_app=summary.get("_app_overlap_seconds"),
            sources=summary.get("_app_sources")))
    elif weekly and summary.get("apps"):
        markup.append(weekly_application_heatmap(summary, timeline_rows, start, end, readiness))
    sites = defaultdict(float)
    for row in site_rows:
        sites[row["host"]] += float(row["seconds"])
    if sites:
        title = "一周网站使用时长" if weekly else "网站浏览明细"
        line = f"网站：{format_duration(sum(sites.values()))} · {len(sites)} 个（已计入浏览器用时）"
        text.extend(["", title, line])
        markup.append(f'<p style="font-size:13px;color:#64748b;margin:20px 0 4px">{html.escape(line)}</p>')
        _site_text(sites, text)
        if not weekly:
            markup.append(website_gantt_chart(site_rows, site_intervals, start,
                                              sources=summary.get("_site_sources")))
        markup.append(website_chart(site_rows, start, end, readiness, site_hours))
        if weekly:
            for day in days:
                records = [r for r in site_rows if str(r.get("day", start)) == str(day)]
                value = (format_duration(sum(float(r["seconds"]) for r in records)) if records else
                         "0 秒" if (str(day), "web") in complete else "未收到")
                text.append(f"{day} 网站: {value}")
    elif weekly:
        title = "一周网站使用时长"
        note = ("本周无网站使用记录。" if all((str(d), "web") in complete for d in days) else
                "本周未收到网站记录。请检查客户端网站统计和上传设置。")
        text.extend(["", title, note])
        markup.append(f'<h2 style="font-size:18px;color:#334e68;margin-top:28px">{title}</h2>'
                      f'<p style="font-size:13px;color:#64748b">{note}</p>')
    if site_rows:
        kinds = ("apps", "web")
        if any((str(day), kind) not in complete for day in days for kind in kinds):
            note = "部分日期未确认上传；留空不代表未使用。"
            text.append(note)
            markup.append(f'<p style="color:#946432;font-size:12px">{note}</p>')
    return "\n".join(text), "".join(markup)


def append_sections(message, summary, timeline_rows, site_rows, start, end,
                    readiness=(), site_hours=(), site_intervals=(), app_intervals=()):
    subject, text, body = message
    extra_text, extra_html = render(summary, timeline_rows, site_rows, start, end,
                                   readiness, site_hours, site_intervals, app_intervals)
    if not extra_html and not extra_text:
        return message
    if html_size(body + extra_html) > 80000:
        # Keep website charts when a long site list exceeds the HTML budget.
        # The timeline stays first: it is the opening section of the mail.
        extra_html = ((daily_winner_timeline(summary, app_intervals, site_intervals, start,
                                             sources=summary.get("_app_sources"))
                       if start == end else "") +
                      (application_gantt_chart(summary, app_intervals, start,
                                               overlap_by_app=summary.get("_app_overlap_seconds"),
                                               sources=summary.get("_app_sources"))
                       if start == end and summary.get("apps") else
                       weekly_application_heatmap(summary, timeline_rows, start, end, readiness)
                       if start != end and summary.get("apps") else "") +
                      (website_gantt_chart(site_rows, site_intervals, start,
                                           sources=summary.get("_site_sources")) if start == end and site_rows else "") +
                      (website_chart(site_rows, start, end, readiness, site_hours) if site_rows else ""))
    if html_size(body + extra_html) <= 80000:
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
