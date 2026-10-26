"""测试包。

有这个文件，tests 目录才会被 Python 当成一个「包」，
测试之间就可以用 `from tests.xxx import ...` 的方式互相引用。

运行全部测试的方式（在项目根目录执行）：
    .venv/Scripts/python.exe -m unittest discover -s tests -t .

下面那段 sys.path 的作用：
    被测试的模块（storage、monitor、icons……）都放在 client/ 目录里，
    而这些测试文件自己位于 tests/ 目录，两边不在同一个文件夹。
    测试里写的是简单的 `import storage`，所以必须先把 client/ 加进模块搜索
    路径，否则会报 ModuleNotFoundError。

    为什么要放在 __init__.py 里：unittest discover 在加载任何测试模块之前
    会先导入 tests 这个包，所以这段代码一定是最先执行的，只写一处就够，
    不用在每个 test_*.py 顶部重复。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 本文件在 tests/ 里，往上一级是项目根，再进去就是 client/
_CLIENT_DIR = os.path.join(_ROOT, "client")
# 放 0 号位置，保证优先用项目里的模块，而不是环境里可能存在的同名包
if _CLIENT_DIR not in sys.path:
    sys.path.insert(0, _CLIENT_DIR)

# 服务端的代码也在自己那一层做扁平导入：server/ 是一整棵「导入树」的根，
# 它下面的 api/ db/ mail/ 都是包，包内互相引用写 `from db import users`
# 这种以 server/ 为根的绝对导入（不是 `from server.db import users`）。
# 所以测试要导入它们的话，server/ 本身也得在搜索路径上。
#
# 追加到**末尾**而不是插到前面：client/ 那边优先。两边目前没有重名的模块
# （client 下面是 core/system/ui/net/tools 这几个包），但这个顺序是安全兜底 ——
# 万一以后撞名了，坏掉的只是服务端测试，不会连累客户端那一堆。
_SERVER_DIR = os.path.join(_ROOT, "server")
if _SERVER_DIR not in sys.path:
    sys.path.append(_SERVER_DIR)
