"""测试 main.py 里的「单实例」逻辑。

要测的原函数会直接调用 Windows API，测试机不一定能真实地造出
「已经有一个实例在跑」这种情况，所以用 unittest.mock 把 Windows API
替换成假的（mock），让我们能随意控制返回值。

mock 的基本套路：
    @patch("main.ctypes.windll.kernel32")  → 把该模块里的 kernel32 换成一个假对象
    函数签名里多出来的参数 kernel32 就是那个假对象，由 patch 自动传进来
    然后设置它的返回值，最后用 assert_xxx 检查被调用的情况
"""
import unittest
from unittest.mock import MagicMock, patch

import main


class SingleInstanceTests(unittest.TestCase):
    """测试 _acquire_single_instance() 能否正确判断「是否已有实例在运行」。"""

    @patch("main.ctypes.windll.kernel32")
    def test_existing_instance_is_rejected(self, kernel32):
        """已经有一个实例在运行时，应该返回 None，并且关掉自己刚创建的句柄。

        通过让 GetLastError() 返回 183（Windows 的 ERROR_ALREADY_EXISTS）
        来模拟「互斥量已被别人抢占」。
        """
        kernel32.CreateMutexW = MagicMock(return_value=123)
        kernel32.GetLastError.return_value = 183

        handle = main._acquire_single_instance()

        self.assertIsNone(handle)  # 返回值应为 None，表示不该继续启动
        # 抢失败时那把句柄必须关掉，否则会泄漏系统资源
        kernel32.CloseHandle.assert_called_once_with(123)

    @patch("main.ctypes.windll.kernel32")
    def test_first_instance_keeps_mutex_handle(self, kernel32):
        """第一个实例启动时应该拿到句柄并保留它（不能关掉）。

        GetLastError() 返回 0 表示「创建成功，之前没人占这个名字」。
        """
        kernel32.CreateMutexW = MagicMock(side_effect=[123, 456])
        kernel32.GetLastError.return_value = 0

        handle = main._acquire_single_instance()

        self.assertEqual(handle, (123, 456))
        self.assertEqual([call.args[2] for call in kernel32.CreateMutexW.call_args_list],
                         list(main._INSTANCE_MUTEX_NAMES))
        # 句柄要一直持有到程序退出，所以这里绝不能调用 CloseHandle
        kernel32.CloseHandle.assert_not_called()

    @patch("main.ctypes.windll.kernel32")
    def test_old_instance_prevents_start_and_releases_new_mutex(self, kernel32):
        kernel32.CreateMutexW = MagicMock(side_effect=[123, 456])
        kernel32.GetLastError.side_effect = [0, 183]
        self.assertIsNone(main._acquire_single_instance())
        self.assertEqual([call.args[0] for call in kernel32.CloseHandle.call_args_list],
                         [456, 123])


if __name__ == "__main__":
    unittest.main()
