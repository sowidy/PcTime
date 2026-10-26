"""测试 monitor.py 的空闲时间判定和片段结算逻辑。

这些是项目里最容易出错、也最值得测试的地方：
    * tick 计数回绕（Windows 的 GetTickCount 约 49.7 天归零一次）
    * 电脑休眠/长时间无操作后，不能把整段睡眠时间都算成「在用电脑」

测试完全不需要真的去操作键鼠：所有跟系统打交道的函数都用 mock 替换掉了。
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from system.monitor import (
    WindowMonitor,
    _active_time_cutoff,
    _idle_seconds_from_ticks,
)


class IdleTimeTests(unittest.TestCase):
    def test_tick_counter_wrap_keeps_idle_time_positive(self):
        """tick 计数回绕后，空闲时长仍然应该是正的小数值。

        current_tick = 0x10（16）、last_input_tick = 0xFFFFFFF0（很大），
        如果直接相减会得到负数；按 32 位无符号回绕处理，
        实际经过的时间是 16 - 0xFFFFFFF0 绕一圈 = 32 毫秒 = 0.032 秒。
        """
        self.assertAlmostEqual(
            _idle_seconds_from_ticks(0x00000010, 0xFFFFFFF0),
            0.032,
        )

    def test_active_time_is_capped_after_a_long_sleep(self):
        """长时间空闲后，计时终点要被「往回拨」到合理的位置。

        现在 = 7200 秒，已空闲 3600 秒，阈值 300 秒：
        只应该算到 7200 - 3600 + 300 = 3900 秒为止，
        也就是「最后一次操作后 300 秒」，多出来的 3300 秒不该计入。
        """
        now = 7_200.0
        idle_seconds = 3_600.0
        threshold = 300

        self.assertEqual(
            _active_time_cutoff(now, idle_seconds, threshold),
            3_900.0,
        )

    def test_recent_activity_uses_current_time(self):
        """空闲时长没超过阈值时，终点就是「现在」，不做任何削减。"""
        self.assertEqual(_active_time_cutoff(1_000.0, 30.0, 300), 1_000.0)

    @patch("system.monitor.get_ignored_apps", return_value=set())
    def test_flush_current_accepts_an_idle_cutoff(self, _ignored_apps):
        """传入截断时间后，结算出的时长和结束时间都要用这个值。

        开始于 100.0 秒，传入的结束时间 160.0 秒 → 时长应为 60 秒。
        patch 的参数名 _ignored_apps 以下划线开头，表示这个假对象用不到，
        只是为了接住 patch 传进来的位置参数。
        """
        monitor = WindowMonitor()
        # 手工摆出「正在计时」的状态，这样不用真的启动监控线程
        monitor._last_app = "example.exe"
        monitor._last_title = "Example"
        monitor._last_exe = "C:/Example/example.exe"
        monitor._last_start = 100.0

        monitor._flush_current(160.0)

        self.assertEqual(len(monitor._buffer), 1)
        self.assertEqual(monitor._buffer[0]["duration"], 60.0)
        self.assertEqual(
            datetime.fromisoformat(monitor._buffer[0]["end_time"]),
            datetime.fromtimestamp(160.0),
        )

    @patch("system.monitor.record_fragments_batch")
    @patch("system.monitor.get_ignored_apps", return_value=set())
    @patch("system.monitor.get_setting", return_value="300")
    @patch("system.monitor._get_idle_seconds", return_value=3_600.0)
    @patch("system.monitor.time.time", return_value=7_200.0)
    def test_stop_after_sleep_does_not_count_the_whole_sleep(
        self,
        _time,
        _idle_seconds,
        _setting,
        _ignored_apps,
        record_batch,
    ):
        """核心回归测试：睡了一觉之后停止监控，睡眠时间不能被算进使用时长。

        场景：3500 秒时开始用某应用，之后电脑空闲了 3600 秒，
        在 7200 秒时程序退出。
        正确结果：只记到 3500 + 400 = 3900 秒为止，时长 400 秒。
        （3900 = 7200 - 3600 + 300，即「最后操作时刻 + 阈值」）

        patch 装饰器是自上而下写、自下而上生效的，所以函数参数要按
        由下往上的顺序接收这些假对象（除 self 外）。
        """
        monitor = WindowMonitor()
        monitor._last_app = "example.exe"
        monitor._last_title = "Example"
        monitor._last_exe = "C:/Example/example.exe"
        monitor._last_start = 3_500.0

        monitor.stop()

        # call_args.args[0] 取出被 mock 的 record_fragments_batch 收到的第一个参数，
        # 也就是准备写库的那批片段
        fragments = record_batch.call_args.args[0]
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0]["duration"], 400.0)


if __name__ == "__main__":
    # 直接运行本文件时执行测试（也可以从项目根目录用 unittest discover 跑）
    unittest.main()
