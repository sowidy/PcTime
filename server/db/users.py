"""users 表：登记用户、查用户、取订阅名单。

为什么单独一张 users 表：光看 usage_records 只能知道「有哪些 user_key」，
但不知道这台机器是谁、邮箱是多少、上次什么时候活着。要发报告邮件就得靠这张表。

查询一律用参数化占位符（%s），不要用字符串拼 SQL —— 客户端传来的
user_key / email 都是外部输入，拼字符串就是注入漏洞。
"""
from datetime import datetime

from db import conn as db_conn


def upsert_user(cur, user_key, hostname, platform, client_version,
                email=None, now=None):
    """登记/更新一个用户。上传时先调它，再写 usage_records。

    几个刻意的行为：
        * email 只在「这次传了值」时才覆盖，传 None 就保持原样 ——
          否则客户端某次没带邮箱就会把已存的邮箱抹掉。
        * subscribed 只在第一次插入时按「有没有邮箱」定，之后不碰。
          这样用户主动退订之后，后续每天的上传不会又把他自动订回来。

    注意它收的 cur 是调用方（上传接口）自己那条连接上的游标 ——
    用户和明细必须在同一个事务里写，所以这里不开连接。
    """
    now = now or datetime.now()
    cur.execute(
        """
        INSERT INTO users
            (user_key, email, subscribed, hostname, platform,
             client_version, created_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            hostname       = VALUES(hostname),
            platform       = VALUES(platform),
            client_version = VALUES(client_version),
            last_seen_at   = VALUES(last_seen_at),
            email          = COALESCE(VALUES(email), email)
        """,
        # VALUES() 在 MySQL 8.0.20 之后被标记为「已废弃」，官方推荐用
        # 行别名语法（... AS new ON DUPLICATE KEY UPDATE x = new.x）。
        # 但别名语法要 8.0.19 以上才支持，而这个服务端可能部署在 5.7 上，
        # 所以这里故意用兼容性更好的 VALUES()：只多一条弃用警告，不会出错。
        (user_key, email, 1 if email else 0, hostname, platform,
         client_version, now, now),
    )


def set_subscription(user_key, email, subscribed):
    """设置某个用户的邮箱和订阅开关。

    返回 True 表示这个 user_key 存在（改到了），False 表示没这个用户。
    """
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            # 先单独确认这个人在不在，**不要**拿 UPDATE 的 affected rows 当判据。
            #
            # MySQL 的 rowcount 默认是「真正被改动的行数」，不是「匹配到的行数」
            # （要后者得开 CLIENT_FOUND_ROWS，pymysql 默认没开）。所以「保存的
            # 内容和库里一模一样」会返回 0 行 —— 那明明是一次成功的保存，却会被
            # 上层翻成 404「没有这个 user_key」。用户第二次点保存（什么都没改）
            # 就会看到一个莫名其妙的错误，而当天最典型的两次重复保存来自
            # 客户端补同步（见 client/net/uploader.py 的 flush_subscription）。
            cur.execute("SELECT 1 AS x FROM users WHERE user_key = %s", (user_key,))
            if cur.fetchone() is None:
                return False
            cur.execute(
                "UPDATE users SET email = %s, subscribed = %s WHERE user_key = %s",
                (email, 1 if subscribed else 0, user_key),
            )
            return True


def get_user(user_key):
    """按 user_key 查一个用户，没有返回 None。"""
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_key, email, subscribed, hostname, platform,"
                " client_version, created_at, last_seen_at"
                " FROM users WHERE user_key = %s",
                (user_key,),
            )
            row = cur.fetchone()
            return db_conn.row_to_json(row) if row else None


def list_users(limit=100, offset=0):
    """列出所有用户，最近活跃的排前面。管理接口用。"""
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_key, email, subscribed, hostname, platform,
                       client_version, created_at, last_seen_at
                FROM users
                ORDER BY last_seen_at DESC
                LIMIT %s OFFSET %s
                """,
                (int(limit), int(offset)),
            )
            return [db_conn.row_to_json(r) for r in cur.fetchall()]


def get_subscribers():
    """取出所有订阅了邮件的用户（邮箱 + user_key）。给 mail/sender.py 用。"""
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_key, email FROM users"
                " WHERE subscribed = 1 AND email IS NOT NULL AND email <> ''"
            )
            return cur.fetchall()
