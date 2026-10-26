"""测试 icons.py 里图标来源的优先级。

核心问题是「同一个应用有好几个候选 exe 时，该用哪个的图标」。
测试通过 mock 掉 _extract_icon（真正去调 Windows API 的部分），
只关注「调用者选了哪条路径」，不关心图标本身长什么样。

mock 技巧：让 _extract_icon 返回一个独特的对象（expected = object()），
然后断言返回的就是这一个对象（assertIs），就能证明走的是预期的分支。
"""
import unittest
from unittest.mock import patch

from system import icons


class IconSourceTests(unittest.TestCase):
    def setUp(self):
        # 清空模块级缓存。不清的话，上一个测试的结果会被缓存下来，
        # 导致后面的测试根本没走到 _extract_icon，断言就失效了
        icons._cache.clear()

    @patch("system.icons.get_shortcut_info")
    @patch("system.icons._extract_icon")
    @patch("system.icons.os.path.exists", return_value=True)
    def test_recorded_canonical_executable_has_first_priority(
        self, _exists, extract_icon, shortcut_info
    ):
        """记录下来的 exe 文件名和规范名一致时，应该优先用它，且不再往下尝试。

        传入 exe_path="C:/Apps/Codex.exe"，而 app_name 是 "codex.exe"，
        两者一致 → 直接用这个路径取图标，快捷方式那条路根本不该走。

        patch 参数顺序：装饰器自下往上生效，所以参数由左到右
        对应 _exists（os.path.exists）、extract_icon、shortcut_info。
        用不到的假对象加下划线前缀，只是为了让名字和顺序对应清楚。
        """
        expected = object()  # 用一个独一无二的对象当「图标」，方便后面认出来
        extract_icon.return_value = expected

        actual = icons.get_icon(
            "codex.exe",
            "Codex",
            size=20,
            exe_path="C:/Apps/Codex.exe",
        )

        self.assertIs(actual, expected)
        # 确认是用「传入的路径 + 传入的尺寸」去取的图标
        extract_icon.assert_called_once_with("C:/Apps/Codex.exe", 20)
        # 既然第一优先级就命中了，就不该再去查快捷方式
        shortcut_info.assert_not_called()

    @patch("system.icons._extract_icon")
    @patch("system.icons.os.path.exists", return_value=True)
    @patch("system.icons.get_shortcut_info")
    def test_canonical_shortcut_beats_a_merged_helper_process(
        self, shortcut_info, _exists, extract_icon
    ):
        """对于合并过的应用，快捷方式的图标要优先于辅助进程的图标。

        场景：百度网盘主程序是 BaiduNetdisk.exe，但它实际上是由
        BaiduNetdiskUnite.exe 这个辅助进程在跑（数据库里记的是后者）。
        此时应该用快捷方式指向的主程序 BaiduNetdisk.exe 去取图标。

        注意这里的 patch 顺序和上一个测试相反：
        因为调用顺序是「先查快捷方式」，所以决定权交给快捷方式这条分支。
        """
        shortcut_info.return_value = {
            "display_name": "百度网盘",
            "icon_path": "C:/Apps/BaiduNetdisk.exe",
            "icon_index": 0,
        }
        expected = object()
        extract_icon.return_value = expected

        actual = icons.get_icon(
            "baidunetdisk.exe",
            "百度网盘",
            size=20,
            exe_path="C:/Apps/BrowserEngine/BaiduNetdiskUnite.exe",
        )

        self.assertIs(actual, expected)
        # 断言用的是快捷方式给的路径，而不是记录下来的辅助进程路径
        extract_icon.assert_called_once_with("C:/Apps/BaiduNetdisk.exe", 20)


if __name__ == "__main__":
    unittest.main()
