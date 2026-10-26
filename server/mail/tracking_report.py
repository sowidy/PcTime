"""Pure email sections for website charts and text-only fallbacks."""
from collections import defaultdict
from datetime import timedelta
import html
import config
from mail.charts import website_chart, website_gantt_chart, html_size


def _site_text(sites, text):
    """Text-only fallback detail; HTML shows stacked columns instead of a rank list."""
    from mail.report import format_duration
    ordered = sorted(sites.items(), key=lambda item: -item[1])
    shown = ordered[:config.MAIL_MAX_SITES] if config.MAIL_MAX_SITES > 0 else ordered
    total = sum(sites.values())
    for host, seconds in shown:
        share = 100 * seconds / total if total else 0
        text.append(f"{host}: {format_duration(seconds)} ({share:.1f}%)")
    if len(ordered) > len(shown):
        text.append(f"另有 {len(ordered)-len(shown)} 个网站未逐项列出，总时长仍包含这些网站。")


def render(summary, timeline_rows, site_rows, start, end, readiness=(), site_hours=(),
           site_intervals=()):
    """Return website sections; legacy timeline_rows is accepted but not rendered."""
    from mail.report import format_duration
    weekly = start != end
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    dates = {str(day) for day in days}
    site_rows = [r for r in site_rows if str(r.get("day", start)) in dates]
    site_hours = [r for r in site_hours if str(r.get("day", start)) in dates]
    site_intervals = [r for r in site_intervals if str(r.get("day", start)) in dates]
    complete = {(str(r["day"]), r["kind"]) for r in readiness if r["complete"]}
    text, markup = [], []
    sites = defaultdict(float)
    for row in site_rows:
        sites[row["host"]] += float(row["seconds"])
    if sites:
        title = "一周网站使用时长" if weekly else "网站浏览明细"
        line = f"网站浏览明细：{format_duration(sum(sites.values()))} · {len(sites)} 个网站（已包含在浏览器应用时长中，不再加入总计）"
        text.extend(["", title, line])
        markup.append(f'<h2 style="font-size:18px;color:#334e68;margin-top:28px">{title}</h2>'
                      f'<p style="font-size:13px">{html.escape(line)}</p>')
        _site_text(sites, text)
        if not weekly:
            markup.append(website_gantt_chart(site_rows, site_intervals, start))
        markup.append(website_chart(site_rows, start, end, readiness, site_hours))
        if weekly:
            for day in days:
                records = [r for r in site_rows if str(r.get("day", start)) == str(day)]
                value = (format_duration(sum(float(r["seconds"]) for r in records)) if records else
                         "0 秒" if (str(day), "web") in complete else "未收到")
                text.append(f"{day} 网站: {value}")
    elif weekly:
        title = "一周网站使用时长"
        note = ("本周已完成网站上传，没有记录到网站浏览。" if all((str(d), "web") in complete for d in days) else
                "本周尚未收到网站记录。请在客户端启用网站统计、连接浏览器扩展，开启“邮件中加入网站图表”，"
                "并确认自动上传和远程 HTTPS 连接可用。")
        text.extend(["", title, note])
        markup.append(f'<h2 style="font-size:18px;color:#334e68;margin-top:28px">{title}</h2>'
                      f'<p style="font-size:13px;color:#64748b">{note}</p>')
    if site_rows:
        kinds = ("apps", "web")
        if any((str(day), kind) not in complete for day in days for kind in kinds):
            note = "部分日期未确认日终上传完成，按最后收到的数据展示；未收到不等于未使用。"
            text.append(note)
            markup.append(f'<p style="color:#946432;font-size:12px">{note}</p>')
    return "\n".join(text), "".join(markup)


def append_sections(message, summary, timeline_rows, site_rows, start, end,
                    readiness=(), site_hours=(), site_intervals=()):
    subject, text, body = message
    extra_text, extra_html = render(summary, timeline_rows, site_rows, start, end,
                                   readiness, site_hours, site_intervals)
    if not extra_html and not extra_text:
        return message
    if html_size(body + extra_html) > 80000:
        # Keep website charts when a long site list exceeds the HTML budget.
        extra_html = ((website_gantt_chart(site_rows, site_intervals, start) if start == end else "") +
                      website_chart(site_rows, start, end, readiness, site_hours) if site_rows else "")
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
