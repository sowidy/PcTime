"""把「会卡界面的活」丢到后台线程去干，再把结果送回界面线程。

单独一个文件的原因：界面这边有两处需要它（仪表盘设置页的「立即上传」按钮、
托盘右键菜单的「立即上传数据」）。写在任意一边，另一边就得反过来 import 它，
而 tray 和 dashboard 之间是有单向依赖的（tray 懒加载 dashboard，见 tray.py
里的说明），反过来引会把关系搞乱。所以抽出来放中间。

这个模块只用到 QtCore（QObject / Signal），**不碰任何控件**，因此可以在
QApplication 创建之前被 import —— 这一点很重要：tray.py 是在 main.py 顶部
就被导入的，那时候 QApplication 还没建出来。
"""

from PySide6.QtCore import QObject, Signal


class BgWorker(QObject):
    """在后台线程里跑一个函数，完成后发信号把结果送到界面线程。

    为什么需要它：上传是网络操作，最坏情况要等「超时若干秒 × 重试几次」，
    直接在按钮的槽函数里调用会把界面冻住 —— 窗口变成一片白，用户只会
    以为程序死了。所以丢到 Python 线程里跑。

    为什么是 QObject + Signal 而不是裸的 threading.Thread：
        Qt 的控件只能在界面线程里改。信号是线程安全的 —— 这个对象在界面
        线程里创建，从别的线程 emit 时 Qt 会自动改用「队列连接」，把槽函数
        排进界面线程的事件循环执行。所以在槽函数里直接改标签、弹气泡都是
        安全的。如果反过来在子线程里直接 lbl.setText()，表现是偶尔崩溃或者
        文字永远不刷新，而且很难复现。

    用法：
        self._worker = BgWorker()
        self._worker.done.connect(self._on_done)   # 槽函数跑在界面线程
        threading.Thread(target=self._worker.run,
                         args=("upload", fn), daemon=True).start()
    """

    # 参数：(任务名, 结果)。结果统一是字典，这样多种任务可以共用一个槽函数，
    # 接收端按任务名分支即可。
    done = Signal(str, object)

    def run(self, name, fn):
        """在子线程里执行 fn() 并 emit 结果。异常一律兜住。

        后台线程绝不能因为异常静悄悄死掉 —— 那样按钮点下去会永远没有反应，
        而用户完全看不到原因（本程序没有控制台窗口，异常信息无处可去）。
        所以任何异常都转成 {"ok": False, "reason": "error", "error": ...}
        照常发出去。带 reason 是为了和 uploader 自己返回的失败形状保持一致，
        界面的 describe_result() 两种都能认。
        """
        try:
            result = fn()
        except Exception as e:                   # noqa: BLE001
            result = {"ok": False, "reason": "error", "days": 0, "apps": 0,
                      "error": f"{type(e).__name__}: {e}"}
        self.done.emit(name, result)
