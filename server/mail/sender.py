"""发信：一封（SMTP）、一天（一个用户）、一轮（所有订阅用户）。

三层是套着的：run() 按天按人循环调 send_day()，send_day() 渲染完调
send_mail()。同模块内的这两次调用写**裸名**（`send_day(...)` / `send_mail(...)`），
测试是靠 patch 本模块的同名属性来换实现的，写成别的引用就 patch 不到了。

「一天」那一层发的是日报还是周报，由那一天是星期几决定（is_weekly_day，
配置项 config.MAIL_WEEKLY_DAY）。默认周日发周报 —— 覆盖「以该天结尾的 7 天」
的聚合；其余六天各发当天的日报。一周里每天恒定一封，所以 mail_log 的
UNIQUE KEY (user_key, day) 不需要动，详见 mail/__init__.py。
"""
import logging
# smtplib 必须是模块级 import（而不是 from smtplib import SMTP_SSL）：
# 测试是拿 mock.patch.object(sender.smtplib, "SMTP_SSL", ...) 顶掉它的，
# 这就要求本模块里存在一个叫 smtplib 的属性。
import smtplib
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import config
from db import mail_log, usage_records, users
from mail import report

log = logging.getLogger("usagetime.mail.sender")


def send_mail(to, subject, text, html_body):
    """通过 SMTP 发一封信。失败直接抛异常，由调用方决定怎么记录。

    multipart/alternative + 纯文本在前：邮件客户端能显示 HTML 就用 HTML，
    不能就用纯文本。先纯文本是约定 —— 越靠后的部分越「高级」，客户端
    从后往前挑自己认识的。
    """
    msg = MIMEMultipart("alternative")
    # 主题里有中文，必须显式编码，否则会出现乱码或直接被拒
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(config.MAIL_FROM_NAME, "utf-8")),
                              config.MAIL_FROM))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    domain = config.MAIL_FROM.split("@")[-1] if "@" in config.MAIL_FROM else None
    msg["Message-ID"] = make_msgid(domain=domain)

    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if config.SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                  timeout=config.SMTP_TIMEOUT)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                              timeout=config.SMTP_TIMEOUT)

    # with 退出时会 quit()，异常路径下也会尽量把连接关掉，不留半开连接
    with server:
        if not config.SMTP_USE_SSL:
            server.ehlo()
            server.starttls()
            # STARTTLS 之后必须再 ehlo 一次：加密层建立后，服务器宣告的
            # 能力（尤其 AUTH 支持哪些方式）可能变了，用的是握手前那份
            # 旧信息的话，login 会以奇怪的方式失败。
            server.ehlo()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)


def is_weekly_day(day):
    """这一天该发周报吗。大小写/编号都跟着 date.weekday()：0=周一 … 6=周日。

    报告种类**只由这一天的星期几决定** —— 不看数据库，也不看「是什么时候跑的」。
    所以半夜补发某天时，补出来的还是那一天该有的那种报告；也因此 mail_log 的
    UNIQUE KEY (user_key, day) 依然成立（见 mail/__init__.py 的论证）。

    刻意留成模块级的裸名函数（而不是把比较内联进 send_day），这样测试可以
    mock.patch.object(sender, "is_weekly_day") 直接指定种类，不必真的等到某个周日。
    """
    return day.weekday() == config.MAIL_WEEKLY_DAY


def send_day(user_key, day, email=None, dry_run=False, now=None):
    """给一个用户发某一天的报告。

    发的是哪一种，由 day 是星期几决定（见 is_weekly_day）：**周报日发周报**
    （覆盖「以这一天结尾的 7 天」的聚合），其余的日子发当天日报。
    除了「取哪份数据」「用哪个渲染」这两步，别的检查两种报告完全一样 ——
    跳过和失败的规则不是各写一遍。

    返回一个 dict，不抛异常 —— 一个地址发失败不该让整轮发送中断。
        {"status": "sent"/"skipped"/"failed", "reason": ..., "day": ...,
         "user_key": ..., "email": ..., "subject": ..., "weekly": bool}

    跳过（skipped）的几种情况，reason 分别是：
        already_sent  这天已经成功发过了（看 mail_log）
        no_email      这个用户没有可用邮箱
        no_data       服务端没有这天的记录 —— 这天机器没开机，或者没上传；
                      周报则是整个区间一行记录都没有
        dry_run       只渲染不发送
    """
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    weekly = is_weekly_day(day)
    out = {"user_key": user_key, "day": day.isoformat(),
           "email": email or "", "subject": "", "weekly": weekly}

    if mail_log.was_mail_sent(user_key, day):
        return {**out, "status": "skipped", "reason": "already_sent"}
    if not email:
        return {**out, "status": "skipped", "reason": "no_email"}

    if weekly:
        # 周报的数据源是**区间聚合**，不是某一天。start/end 由 report.week_range
        # 算，这里只是照着用 —— 「一周是哪 7 天」只该有一个定义处。
        start, end = report.week_range(day)
        summary = usage_records.get_range_summary(user_key, start, end)
    else:
        summary = usage_records.get_daily_summary(user_key, day)
    if summary is None:
        return {**out, "status": "skipped", "reason": "no_data"}

    user = users.get_user(user_key)
    if weekly:
        subject, text, html_body = report.build_weekly_message(day, summary, user)
    else:
        subject, text, html_body = report.build_message(day, summary, user)
    out["subject"] = subject

    if dry_run:
        return {**out, "status": "skipped", "reason": "dry_run"}

    try:
        send_mail(email, subject, text, html_body)
    except Exception as e:                       # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        # 失败也留痕：用户来问「怎么没收到」时，这张表就是答案
        try:
            mail_log.record_mail(user_key, day, email, mail_log.MAIL_FAILED,
                                 detail, now=now)
        except Exception as log_err:             # noqa: BLE001
            log.warning("连失败记录都写不进去：%s", log_err)
        log.warning("%s发送失败 user=%s day=%s to=%s：%s",
                    "周报" if weekly else "日报", user_key, day, email, detail)
        return {**out, "status": "failed", "reason": detail}

    try:
        mail_log.record_mail(user_key, day, email, mail_log.MAIL_SENT, subject,
                             now=now)
    except Exception as e:                       # noqa: BLE001
        # 信已经发出去了，记录没写上。最坏结果是下次运行重发一封，
        # 比「明明发了却因为写记录失败而报错」要好。
        log.warning("邮件已发出但记录失败 user=%s day=%s：%s", user_key, day, e)

    log.info("%s已发送 user=%s day=%s to=%s",
             "周报" if weekly else "日报", user_key, day, email)
    return {**out, "status": "sent", "reason": ""}


def run(day=None, catchup_days=0, dry_run=False, user_key=None, now=None):
    """给所有订阅用户发某一天的报告。

    发的是日报还是周报，由每一天自己决定（见 is_weekly_day）—— 所以传进来的
    日子里有周报日时，那一封就是周报，调用方不用区分。外部 cron 因此也完全
    不用改：照旧调 POST /api/v1/admin/daily-mail，周日那次自然变成周报。

    参数：
        day          发哪一天，默认今天。注意「今天」只有在 23:59:59 发才
                     是完整的一天 —— 手工触发时请自己确认这一点。
        catchup_days 顺带补发前几天（默认 0）。定时器和启动补发会带上它，
                     这样某天因为重启没发出去时能自愈。已经发过的会被
                     mail_log 挡掉，不会重复发。补发出来的报告种类也是那一天
                     该有的那种（种类只看日期，不看「什么时候跑的」）。
        user_key     只给这一个设备发，并且**忽略订阅开关** —— 这是手工
                     排查用的入口，用户主动要求重发时不该被订阅状态拦住。
        dry_run      只渲染不发送。

    返回：
        {"ok": bool, "day": ..., "days": [...],
         "sent"/"skipped"/"failed": 计数, "results": [每次尝试的明细],
         "reason": 整轮没跑成时的原因}
    """
    now = now or datetime.now()
    target = day or now.date()
    if not isinstance(target, date):
        target = date.fromisoformat(str(target))

    result = {"ok": False, "day": target.isoformat(), "days": [],
              "sent": 0, "skipped": 0, "failed": 0, "results": []}

    if not config.MAIL_ENABLED:
        result["reason"] = "disabled"
        return result

    # 补发的日子只取「已经过完的」。哪怕调用方手滑把 day 传成了明天，
    # 也不会把还没结束的一天当报告发出去。
    days = [target] + [target - timedelta(days=i)
                       for i in range(1, max(0, int(catchup_days)) + 1)]
    days = [d for d in days if d <= now.date()]
    result["days"] = [d.isoformat() for d in days]

    if user_key:
        user = users.get_user(user_key)
        if user is None:
            result["reason"] = "no_such_user"
            return result
        # 身份用调用方传进来的那个，不从查出来的行里再取一遍：少一个
        # 「行里恰好没有 user_key 这一列就 KeyError」的隐患。
        targets = [{**user, "user_key": user_key}]
    else:
        targets = users.get_subscribers()
        if not targets:
            result["ok"] = True
            result["reason"] = "no_subscribers"
            return result

    for d in days:
        for user in targets:
            outcome = send_day(
                user["user_key"], d,
                email=user.get("email"), dry_run=dry_run, now=now,
            )
            result[outcome["status"]] += 1
            result["results"].append(outcome)

    result["ok"] = result["failed"] == 0
    log.info("报告发送完成 day=%s 天=%d 成功=%d 跳过=%d 失败=%d",
             result["day"], len(days), result["sent"],
             result["skipped"], result["failed"])
    return result
