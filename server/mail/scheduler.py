"""进程内的每日定时器：算下一次该在什么时候发，然后睡到那个点。

只看这一个文件时，记住两条：
    * 「下一次几点」是一个**时刻**（带日期），不是「还有多少秒」——
      这样睡过头跨过午夜时，发的还是被跳过的那一天（见包 docstring 的 ②）；
    * 异常必须被吃掉，绝不能逃出线程（见 DailyMailScheduler._guarded_run）。

这里**不区分日报和周报** —— 它只管「到点跑一轮」，跑哪一天、那一天该发哪种
报告，都是 mail/sender.py 按日期算出来的。所以定时点仍然是每天 23:59:59 一个，
周日那天自动变成周报（见包 docstring 的 ⑤）。
"""
import logging
import threading
from datetime import datetime, time, timedelta

import config
from mail.sender import run

log = logging.getLogger("usagetime.mail.scheduler")

DEFAULT_SEND_TIME = time(23, 59, 59)


# ── 定时点解析 ────────────────────────────────────────────────

def parse_send_time(raw):
    """把 USAGETIME_MAIL_TIME 解析成 datetime.time，解析不了就用默认值。

    这里不抛异常：配置写错了就不该让整个服务起不来 —— 大不了按默认时间发，
    日志里有告警，比服务直接崩掉好。
    """
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).time()
        except ValueError:
            continue
    log.warning("USAGETIME_MAIL_TIME=%r 解析不了，改用默认的 23:59:59", raw)
    return DEFAULT_SEND_TIME


SEND_TIME = parse_send_time(config.MAIL_SEND_TIME)


# ── 定时器 ────────────────────────────────────────────────────

class DailyMailScheduler:
    """进程内的每日定时器。跑在一个 daemon 线程里。

    线程是 daemon：服务端退出时不指望它自己收尾，别为了等它而卡住关机。
    """

    def __init__(self, send_time=None, catchup_days=None, runner=None):
        self.send_time = send_time or SEND_TIME
        self.catchup_days = (config.MAIL_CATCHUP_DAYS
                             if catchup_days is None else catchup_days)
        # runner 只是为了测试能塞一个假的进来，生产里就是上面的 run()
        self._runner = runner or run
        self._thread = None
        self._stop = threading.Event()
        self.last_result = None

    def next_fire(self, now=None):
        """下一次该发的**时刻**（不是「还有多少秒」）。

        时刻里带着「属于哪一天」的信息，这正是修复「睡过头跨过午夜」
        那个坑的关键：睡醒之后要发哪一天由它决定，不看现在几点。
        """
        now = now or datetime.now()
        fire = datetime.combine(now.date(), self.send_time)
        if fire <= now:
            fire += timedelta(days=1)
        return fire

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="usagetime-mail-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # 大概率是卡在某次 SMTP 上（有 timeout= 兜着，总会回来）。
                # 不强杀线程 —— Python 里没有安全的杀线程手段。
                log.warning("邮件定时器没能在 %.0f 秒内退出，先不管它", timeout)
            self._thread = None

    def _loop(self):
        # 开机先补一次：服务器如果是半夜重启的，昨天那一封就没人发了。
        # 只补「已经过完的」日子，所以从昨天开始往前数，不碰今天 ——
        # 今天的报告归 23:59:59 那一枪，提前发出去的话，到点反而会因为
        # 「已经发过」而被跳过，用户拿到的就是一份残缺的当日总结。
        if self.catchup_days > 0:
            self._guarded_run(day=datetime.now().date() - timedelta(days=1),
                              catchup_days=self.catchup_days - 1,
                              note="启动补发")

        while not self._stop.is_set():
            now = datetime.now()
            fire = self.next_fire(now)
            wait = (fire - now).total_seconds()
            log.info("下一次邮件：%s（%.0f 秒后）",
                     fire.isoformat(sep=" ", timespec="seconds"), wait)
            if self._stop.wait(max(0.0, wait)):
                return                       # 收到停止信号
            # 用 fire.date() 而不是 date.today()：睡过头跨过了午夜时，
            # 前者仍然是「被跳过的那一天」，后者已经变成新的一天了。
            self._guarded_run(day=fire.date(), note="定时")

    def _guarded_run(self, day, catchup_days=None, note=""):
        """跑一轮，并且保证异常不逃出去。

        定时器线程里逃出去的异常会静默地杀掉这个线程，之后每天都不发邮件，
        而且没有任何迹象 —— 这是最难查的一类故障。
        """
        try:
            result = self._runner(
                day=day,
                catchup_days=(self.catchup_days if catchup_days is None
                              else catchup_days),
            )
        except Exception as e:                   # noqa: BLE001
            log.exception("%s：报告任务异常：%s", note, e)
            return None
        self.last_result = result
        log.info("%s完成：day=%s 成功=%d 失败=%d",
                 note, result.get("day"), result.get("sent", 0),
                 result.get("failed", 0))
        return result


# 进程内只允许有一个定时器。lifespan 每次启动调一次 start_scheduler()，
# 重复调用不该起出第二个线程。
_scheduler = None
_scheduler_lock = threading.Lock()


def start_scheduler():
    """按配置启动进程内定时器。没启用就返回 None，调用方不用管分支。"""
    global _scheduler

    if not config.MAIL_ENABLED:
        log.info("每日邮件未启用（没配 USAGETIME_SMTP_HOST），跳过定时器")
        return None
    if not config.MAIL_SCHEDULER_ENABLED:
        log.info("进程内定时器已关闭，请用外部 cron 调 "
                 "POST /api/v1/admin/daily-mail")
        return None

    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = DailyMailScheduler()
            _scheduler.start()
            log.info("每日邮件定时器已启动，每天 %s 发送",
                     _scheduler.send_time.strftime("%H:%M:%S"))
        return _scheduler
