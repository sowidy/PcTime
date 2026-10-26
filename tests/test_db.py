"""测试 server/db/ 里几处容易踩坑的 SQL 逻辑。

**不连真数据库**：用一个假的连接和游标顶上去。单测不该依赖公网那台 MySQL
今天开不开机。真连库的那条链路（本机 FastAPI + 云端 MySQL）是另外手工验的。
"""
import contextlib
import unittest
from unittest import mock

# 数据层是按表拆开的包。这里只用到两个子模块：
#   db_conn.connect      —— 要被换成假连接
#   db_users.set_subscription —— 被测的函数
#
# 注意 patch 的目标必须是 **db_conn** 这个子模块，不能是包 db。
# 包 db 上没有 connect 这个属性（它没有做 re-export 门面），patch 它只会
# AttributeError；而就算硬在包上挂一个 connect，users.py 内部查的也是
# 自己模块里的 db_conn.connect，patch 会「成功但无效」，测试就会真的去连
# 线上那台 MySQL（config.DB_HOST）。
from db import conn as db_conn
from db import users as db_users


class FakeCursor:
    """够用的假游标：记下执行过的 SQL，按脚本返回行 / rowcount。"""

    def __init__(self, rows=None, rowcount=0):
        self.executed = []          # [(sql, args), ...]
        self.rowcount = rowcount
        self._rows = list(rows or [])

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        return self.rowcount

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # ── 断言辅助 ──

    def sql_list(self):
        return [sql for sql, _ in self.executed]

    def ran(self, prefix):
        return any(s.strip().upper().startswith(prefix) for s in self.sql_list())


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def fake_connect(cursor):
    """把 db_conn.connect 换掉：它是个生成器函数，用 contextmanager 等价顶替。"""
    @contextlib.contextmanager
    def _cm(with_db=True):
        yield FakeConn(cursor)
    return _cm


class TestSetSubscription(unittest.TestCase):
    """set_subscription 的存在性判断。

    这里挡的是一个真踩过的坑：MySQL 的 rowcount 默认是「真正被改动的行数」，
    不是「匹配到的行数」（要后者得开 CLIENT_FOUND_ROWS，pymysql 没开）。
    拿它当「用户存不存在」的判据，就会出现「保存的内容和库里一模一样 → 0 行
    被改动 → 被判成没有这个用户 → 接口回 404」。用户第二次点保存（什么都没改）
    看到一个莫名其妙的错误，而这正是客户端补同步的典型场景
    （见 client/net/uploader.py 的 flush_subscription）。
    """

    def test_identical_values_are_still_a_success(self):
        """内容没变化也必须返回 True。"""
        cur = FakeCursor(rows=[{"x": 1}], rowcount=0)   # 人在，但 0 行被改动
        with mock.patch.object(db_conn, "connect", fake_connect(cur)):
            self.assertTrue(db_users.set_subscription("k", "a@b.com", True))
        self.assertTrue(cur.ran("UPDATE"), "确认真的写了，不是查一下就返回")

    def test_missing_user_returns_false(self):
        """人不在时才返回 False，而且不该白写一次 UPDATE。"""
        cur = FakeCursor(rows=[])
        with mock.patch.object(db_conn, "connect", fake_connect(cur)):
            self.assertFalse(db_users.set_subscription("k", "a@b.com", True))
        self.assertFalse(cur.ran("UPDATE"), "人都不在，不该发 UPDATE")

    def test_unsubscribe_writes_a_zero_not_false(self):
        """subscribed 要写成 0/1，不能把 Python 的 False 直接塞进去。

        塞 False 在 MySQL 里恰好也能用（False → 0），但语义上是在赌
        驱动和列类型，写明白更好读，也免得以后换列类型时出问题。
        """
        cur = FakeCursor(rows=[{"x": 1}])
        with mock.patch.object(db_conn, "connect", fake_connect(cur)):
            db_users.set_subscription("k", None, False)
        update_args = [args for sql, args in cur.executed if "UPDATE" in sql]
        self.assertEqual(update_args, [(None, 0, "k")])


if __name__ == "__main__":
    unittest.main()
