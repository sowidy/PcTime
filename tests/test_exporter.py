"""测试 exporter.py 的自动导出逻辑：目录分层、文件名、增量补导。

和其它测试一样，这里用的是临时目录里的数据库和临时导出目录，
不会碰你本机那份 usage.db，也不会往项目的 data/ 里写东西。
"""
import csv
import os
import tempfile
import unittest
from datetime import date

from core import exporter, storage


class ExporterTests(unittest.TestCase):
    def setUp(self):
        # ── 临时数据库 ──
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_db_path = storage.DB_PATH
        self.addCleanup(setattr, storage, "DB_PATH", self.original_db_path)
        storage.DB_PATH = os.path.join(self.temp_dir.name, "usage.db")
        storage.init_db()

        # ── 临时导出目录 ──
        # exporter 里的 period_dir() 是调用 data_dir() 拿根目录的，
        # 而 data_dir 是从 paths 模块 import 进来的名字，所以直接替换
        # exporter 模块里的这个名字，就能把导出重定向到临时目录。
        self.out_dir = os.path.join(self.temp_dir.name, "data")
        self.original_data_dir = exporter.data_dir
        self.addCleanup(setattr, exporter, "data_dir", self.original_data_dir)
        exporter.data_dir = lambda: self.out_dir

    def _add_usage(self, day, app="example.exe", seconds=60.0):
        """往临时库里塞一条某天的使用记录。"""
        storage.record_fragment(
            app, "Example", f"C:/Example/{app}",
            f"{day}T10:00:00", f"{day}T10:0{int(seconds // 60) or 1}:00",
            seconds,
        )

    def _rel(self, path):
        """把绝对路径转成相对导出根目录的形式，方便断言目录结构。"""
        return os.path.relpath(path, self.out_dir).replace("\\", "/")

    # ── 目录分层与命名 ──

    def test_daily_export_uses_year_month_day_folders(self):
        """每日导出要放进 年/月/日 三层目录，文件名带上日期。"""
        self._add_usage("2026-10-14")
        path = exporter.export_daily(date(2026, 10, 14))
        self.assertEqual(self._rel(path), "2026/10/14/daily_2026-10-14.csv")

    def test_day_and_month_are_zero_padded(self):
        """月和日要补零，否则按名字排序时 10 会排在 9 前面，顺序就乱了。"""
        self._add_usage("2026-01-05")
        path = exporter.export_daily(date(2026, 1, 5))
        # 1 月 5 日 → 01/05，而不是 1/5
        self.assertEqual(self._rel(path), "2026/01/05/daily_2026-01-05.csv")

    def test_weekly_and_monthly_folders_use_period_start(self):
        """周/月的目录名取「周期起始日」，文件名里再写完整区间。"""
        self._add_usage("2026-10-14")   # 2026-10-14 是周三，所在周的周一是 10-12

        wp = exporter.export_weekly(date(2026, 10, 12))
        self.assertEqual(
            self._rel(wp), "2026/10/12/weekly_2026-10-12_2026-10-18.csv")

        mp = exporter.export_monthly(2026, 10)
        self.assertEqual(self._rel(mp), "2026/10/01/monthly_2026-10.csv")

    def test_empty_period_writes_nothing(self):
        """没有数据的日期不写空文件——否则会到处是不含内容的 CSV。"""
        self.assertIsNone(exporter.export_daily(date(2026, 10, 14)))

    # ── 日期工具 ──

    def test_week_start_of_returns_monday(self):
        """一周的起点是周一（和界面上的「周一/周二/…」标签保持一致）。"""
        # 2026-10-14 是周三
        self.assertEqual(exporter.week_start_of(date(2026, 10, 14)),
                         date(2026, 10, 12))
        # 周日应该归到「本周一」，而不是算成下一周的开头
        self.assertEqual(exporter.week_start_of(date(2026, 10, 18)),
                         date(2026, 10, 12))

    def test_months_spanning_crosses_year_boundary(self):
        """跨年时要正确列出 12 月 → 次年 1 月。"""
        months = exporter._months_spanning(date(2026, 12, 20), date(2027, 1, 5))
        self.assertEqual(months, [(2026, 12), (2027, 1)])

    def test_month_last_day_handles_leap_february(self):
        """2 月的最后一天要算对（闰年 29 号、平年 28 号）。"""
        self._add_usage("2024-02-29")
        path = exporter.export_monthly(2024, 2)
        self.assertIsNotNone(path)
        # 文件里应该能看到 2 月 29 日那天的数据，说明区间算到了月底
        with open(path, encoding="utf-8-sig", newline="") as fh:
            dates_in_file = {r[0] for r in list(csv.reader(fh))[1:]}
        self.assertEqual(dates_in_file, {"2024-02-29"})

    # ── 增量补导（水位线） ──

    def test_first_run_backfills_everything_then_only_new_days(self):
        """第一次运行要把历史全补出来；之后再跑只处理新的一天。

        这是「水位线」机制的核心行为：程序不是一直在后台跑，用户可能关几天
        再打开，中间那几天必须补上，否则会永久缺 CSV。
        """
        self._add_usage("2026-10-12")
        self._add_usage("2026-10-13")

        # 第一次：从最早的数据补到今天（2026-10-14），两天都该被导出来
        first = exporter.export_pending(today=date(2026, 10, 14))
        rels = sorted(self._rel(p) for p in first)
        self.assertIn("2026/10/12/daily_2026-10-12.csv", rels)
        self.assertIn("2026/10/13/daily_2026-10-13.csv", rels)

        # 第二次（同一天再跑）：只刷新今天相关的文件，不再重复导那两天
        second = exporter.export_pending(today=date(2026, 10, 14))
        daily_rels = [self._rel(p) for p in second if "daily_" in p]
        self.assertEqual(daily_rels, [])

    def test_gap_days_are_backfilled_on_next_run(self):
        """如果中间几天没开程序，下次启动要把空档补上，而不是直接跳过去。"""
        self._add_usage("2026-10-12")
        exporter.export_pending(today=date(2026, 10, 12))   # 水位线推到 10-12

        # 之后 10-13 也用了电脑，但程序直到 10-14 才再次启动
        self._add_usage("2026-10-13")
        written = exporter.export_pending(today=date(2026, 10, 14))
        rels = [self._rel(p) for p in written]
        self.assertIn("2026/10/13/daily_2026-10-13.csv", rels)

    def test_watermark_is_advanced_after_a_sweep(self):
        """跑完一轮后水位线要推到「今天」，否则下次会重复导一大堆。"""
        self._add_usage("2026-10-14")
        exporter.export_pending(today=date(2026, 10, 14))
        self.assertEqual(storage.get_setting("export_watermark"), "2026-10-14")

    def test_broken_watermark_falls_back_to_full_backfill(self):
        """水位线被写坏时（比如手工改过数据库）不应该崩，退回全量补导。"""
        self._add_usage("2026-10-14")
        storage.set_setting("export_watermark", "这不是日期")
        written = exporter.export_pending(today=date(2026, 10, 14))
        self.assertTrue(any("daily_2026-10-14" in p for p in written))

    # ── 输出内容 ──

    def test_csv_has_bom_and_expected_columns(self):
        """CSV 要带 BOM（Excel 打开不乱码）、列名和顺序固定。"""
        self._add_usage("2026-10-14", app="pycharm64.exe", seconds=120.0)
        path = exporter.export_daily(date(2026, 10, 14))

        with open(path, "rb") as fh:
            self.assertTrue(fh.read(3) == b"\xef\xbb\xbf")   # UTF-8 BOM

        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["日期", "应用名", "显示名", "使用时长(秒)", "使用时长", "使用次数"])
        self.assertEqual(rows[1][0], "2026-10-14")
        self.assertEqual(rows[1][3], "120.0")

    def test_disabled_scheduler_does_nothing(self):
        """关掉「自动导出」后，调度器跑一轮不应该写任何文件。"""
        self._add_usage("2026-10-14")
        storage.set_setting("export_enabled", "0")
        sched = exporter.ExportScheduler()
        self.assertEqual(sched.run_once(), [])

    def test_scheduler_swallows_errors(self):
        """后台线程里的异常必须被兜住，否则线程直接死掉、之后再也不导出了。"""
        storage.set_setting("export_enabled", "1")

        def boom(**kwargs):
            raise RuntimeError("模拟数据库炸了")

        original = exporter.export_pending
        self.addCleanup(setattr, exporter, "export_pending", original)
        exporter.export_pending = boom

        sched = exporter.ExportScheduler()
        # 不该往外抛异常，而是记在 last_error 里
        self.assertEqual(sched.run_once(), [])
        self.assertIn("模拟数据库炸了", sched.last_error)


if __name__ == "__main__":
    unittest.main()
