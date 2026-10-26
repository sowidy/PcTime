"""usage_records 表：写入某天的明细，以及按天 / 按区间读出来。

模块名叫 usage_records 而不是 usage，是为了和 api/usage.py（那是对外的
两个读接口）在文件里同时出现时不至于看混 —— 一个是表，一个是路由。
"""
from datetime import datetime

from db import conn as db_conn


def replace_day(cur, user_key, day, apps, now=None):
    """用这次上传的内容整体替换某一天的记录。

    为什么是「先删后插」而不是逐行 upsert：
        客户端上传的是当天的**汇总**，而这个汇总会随时间变化 ——
        比如多进程归并规则改了，原来分开记的 A.exe / B.exe 会合并成
        一条 A.exe。如果只做 upsert，库里会同时留着旧的两行和新的一行，
        当天总时长就凭空多出来了。
        先按 (user_key, day) 删干净再插，能保证服务端这份和客户端说的
        完全一致。整个过程在一个事务里，不会出现删了没插进去的空窗。

    注意它收的 cur 是调用方（上传接口）自己那条连接上的游标：
    同一次上传的多天必须在一个事务里，所以这里不开连接。

    参数：
        apps：可迭代对象，每项含 app_name / display_name / seconds / launches。
    """
    now = now or datetime.now()
    cur.execute(
        "DELETE FROM usage_records WHERE user_key = %s AND day = %s",
        (user_key, day),
    )
    rows = [
        (user_key, day, a["app_name"], a["display_name"],
         float(a["seconds"]), int(a["launches"]), now)
        for a in apps
    ]
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO usage_records
            (user_key, day, app_name, display_name, seconds, launches, uploaded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def get_usage(user_key, start, end):
    """取某个用户在 [start, end] 区间内的明细，按日期、时长排序。

    参数：
        start / end：datetime.date。
    返回：字典列表，日期字段已转成 ISO 字符串（直接就能进 JSON）。
    """
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, app_name, display_name, seconds, launches, uploaded_at
                FROM usage_records
                WHERE user_key = %s AND day BETWEEN %s AND %s
                ORDER BY day ASC, seconds DESC
                """,
                (user_key, start, end),
            )
            return [db_conn.row_to_json(r) for r in cur.fetchall()]


def get_daily_summary(user_key, day):
    """取某个用户某天的汇总：总时长 + 分应用明细。

    这就是每日邮件正文的数据源（mail/report.py 用它渲染），所以单独留一个
    函数，免得邮件逻辑自己去拼 SQL —— 两边对「总时长怎么算」理解不一致就麻烦了。
    返回 None 表示这天没有任何记录。
    """
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT app_name, display_name, seconds, launches
                FROM usage_records
                WHERE user_key = %s AND day = %s
                ORDER BY seconds DESC
                """,
                (user_key, day),
            )
            apps = cur.fetchall()

    if not apps:
        return None

    total = sum(float(a["seconds"]) for a in apps)
    return {
        "day": day.isoformat(),
        "total_seconds": round(total, 1),
        "app_count": len(apps),
        "apps": [
            {
                "app_name": a["app_name"],
                "display_name": a["display_name"],
                "seconds": round(float(a["seconds"]), 1),
                "launches": int(a["launches"]),
            }
            for a in apps
        ],
    }


def get_range_summary(user_key, start, end):
    """取某个用户在 [start, end] 区间内的汇总：总时长 + 分应用明细。

    这是**周报**正文的数据源（周报 = 周日那一封，区间是周一..周日）。
    形状刻意和 get_daily_summary 做成一样（total_seconds / app_count / apps），
    这样 mail/report.py 可以共用汇总头部和纯文本明细。
    两处不同：
        * apps 是**跨天聚合**过的（按 app_name 分组求和），不是某一天的；
        * 多一个 days_with_data：区间里有几天真的有数据；
        * daily_apps 保留真实每天各应用的时长，用于堆叠柱状图。
    返回 None 表示区间内一行记录都没有 —— 调用方据此跳过（no_data）。

    参数：
        start / end：datetime.date，闭区间。调用方保证 start <= end。

    关于那条 SQL 里两个容易改坏的地方：
        * display_name 外面必须套 MAX()。同一天一个应用只有一行（唯一键
          uk_user_day_app），所以 get_daily_summary 可以直接取；跨了天，同一个
          app_name 就可能有好几行、显示名还不一样（客户端能改名）。裸写
          display_name 会违反 MySQL 5.7+ 默认的 ONLY_FULL_GROUP_BY 直接报错。
          列定义是 NOT NULL DEFAULT ''，MAX() 会优先挑到非空的那个显示名 ——
          正是想要的（report._app_label 只在显示名为空时才退回进程名）。
        * SUM(seconds) AS seconds 这些别名是**承重的**：渲染层按 app["seconds"]
          取值，少个别名就是渲染时的 KeyError，而不是查询时报错。
    """
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT app_name,
                       MAX(display_name)   AS display_name,
                       SUM(seconds)        AS seconds,
                       SUM(launches)       AS launches,
                       COUNT(DISTINCT day) AS days_with_data
                FROM usage_records
                WHERE user_key = %s AND day BETWEEN %s AND %s
                GROUP BY app_name
                ORDER BY seconds DESC
                """,
                (user_key, start, end),
            )
            apps = cur.fetchall()

            # Genuine daily composition for the weekly stacked chart. Never
            # distribute the weekly totals evenly across days.
            cur.execute(
                "SELECT day, app_name, seconds FROM usage_records "
                "WHERE user_key = %s AND day BETWEEN %s AND %s ORDER BY day, app_name",
                (user_key, start, end),
            )
            daily_apps = [db_conn.row_to_json(r) for r in cur.fetchall()]

    if not apps:
        return None

    total = sum(float(a["seconds"]) for a in apps)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_with_data": len({str(row["day"]) for row in daily_apps}),
        "daily_apps": daily_apps,
        "total_seconds": round(total, 1),
        "app_count": len(apps),
        "apps": [
            {
                "app_name": a["app_name"],
                "display_name": a["display_name"],
                "seconds": round(float(a["seconds"]), 1),
                "launches": int(a["launches"]),
            }
            for a in apps
        ],
    }
