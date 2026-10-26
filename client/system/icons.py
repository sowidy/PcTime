"""应用图标获取模块：拿到每个应用真实的 exe 图标，显示在统计列表里。

难点在于：数据库里存的是「进程名」和 exe 路径，但要显示图标得拿到真正的
图标资源。这里做了多级尝试，从最可靠的来源一路退到兜底方案：

    1. 记录过的、文件名与规范名一致的 exe  → 最可信
    2. 桌面/开始菜单里同名快捷方式的图标    → 次优
    3. 记录过的其它 exe（可能是辅助进程）
    4. 当前正在运行的、同名进程的 exe
    5. 系统目录下的同名程序（C:\\Windows\\System32\\...）
    6. 全都失败 → 用代码画一个「彩色圆圈 + 首字母」的占位图标

另外还需要调用 Windows 的 COM 接口和 GDI 接口来把图标「抠」成图片，
这部分代码看起来比较吓人，但套路是固定的：调用 API → 拿到位图数据 →
把像素格式从 BGRA 转成 RGBA（Python 的图像库习惯用 RGBA）。
"""
import ctypes
import ctypes.wintypes as wintypes
import struct
import win32gui
import win32ui
from PIL import Image, ImageDraw, ImageFont
import os
import glob as globmod

# ── Windows API 常量与结构体 ──────────────────────────────────
# 这些常量是 Windows 头文件里定义好的固定数值，含义由系统约定，
# 不能随便改。名字后面是它们在 Windows SDK 里的原始名称含义。
SHGFI_ICON = 0x00000100          # SHGetFileInfo 的选项：请返回图标句柄
SHGFI_SYSICONINDEX = 0x00004000  # SHGetFileInfo 的选项：请返回系统图标列表里的下标
SHIL_EXTRALARGE = 0x2            # 要「超大」尺寸（通常 48x48 或 256x256）的图标
SHIL_LARGE = 0x0                 # 要「大」尺寸（通常 32x32）的图标

# 预先加载好两个系统 DLL，后面直接调用它们的函数
shell32 = ctypes.windll.shell32    # 外壳接口：取文件图标、快捷方式信息
comctl32 = ctypes.windll.comctl32  # 通用控件：操作系统的图标列表

# IImageList 接口的 GUID（全局唯一标识符）。
# COM 接口不能按名字调用，必须先把这段 16 字节的 ID 转成二进制再传进去。
# COM 规定 GUID 的前三部分是小端序，所以这串十六进制要按原样拼成 bytes 而不是反过来。
_IID_IImageList = struct.pack('16s', bytes.fromhex('46EB5926582E4017AF9E428BACE4B2A8'))


class SHFILEINFO(ctypes.Structure):
    """对应 Windows 的 SHFILEINFO 结构体，用来接收 SHGetFileInfo 的返回结果。"""
    _fields_ = [
        ("hIcon", wintypes.HICON),          # 图标句柄
        ("iIcon", ctypes.c_int),            # 在系统图标列表中的下标
        ("dwAttributes", ctypes.c_uint),    # 文件属性标志
        ("szDisplayName", ctypes.c_wchar * 260),  # 显示名（C 里是定长字符数组）
        ("szTypeName", ctypes.c_wchar * 80),      # 类型名，如「应用程序」
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    """对应 Windows 的 BITMAPINFOHEADER 结构体，描述一块位图内存的格式。

    GetBitmapBits 拿到的是裸字节，光有字节不知道宽高和每像素几个字节，
    必须靠这个结构体来「翻译」。
    """
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),   # 每像素多少位，32 表示 BGRA 各 8 位
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]

# ── 缓存 ──────────────────────────────────────────────────────
# 取图标要读文件、调 API，开销不小；界面每次刷新都会请求同一批图标，
# 所以结果全部缓存起来。

_cache = {}          # {(应用名, exe路径, 尺寸): PIL 图片}
_exe_path_cache = {} # {应用名: exe 路径}，见 _find_exe_path
_shortcut_cache = None  # {exe_name_lower: {display_name, icon_path, icon_index}}

# 这些是「通用宿主程序」——桌面上的快捷方式常常指向它们，
# 但快捷方式的名字跟实际应用毫无关系（比如某个 .lnk 指向 python.exe）。
# 如果按快捷方式给它们命名，会得到非常离谱的名字，所以直接跳过。
_GENERIC_SHORTCUT_TARGETS = {
    "cmd.exe", "explorer.exe", "java.exe", "javaw.exe",
    "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe",
    "rundll32.exe", "wscript.exe",
}


def _scan_shortcuts():
    """扫描桌面和开始菜单的快捷方式，建立 exe → 名称/图标映射。

    为什么要扫快捷方式：
        桌面上的 .lnk 文件名往往就是应用的「人话名字」（比如「微信」），
        比进程名 weixin.exe 友好得多，而且快捷方式里通常还指定了图标位置。

    扫描顺序是桌面 → 公共桌面 → 用户开始菜单 → 全局开始菜单。
    同一进程名先扫到的优先，不覆盖（见下面 if exe_name not in _shortcut_cache），
    这样「桌面」的优先级就高于「开始菜单」。

    结果缓存在模块级的 _shortcut_cache 里，只真正扫一次。
    """
    global _shortcut_cache
    if _shortcut_cache is not None:
        return _shortcut_cache

    _shortcut_cache = {}
    try:
        import win32com.client
        # WScript.Shell 是 Windows 脚本宿主提供的 COM 对象，
        # 它能解析 .lnk 文件，帮我们读出「目标路径」「图标位置」等信息
        shell = win32com.client.Dispatch("WScript.Shell")
    except Exception:
        # COM 初始化失败（少见）就直接返回空表，后续逻辑会走其它兜底方案
        return _shortcut_cache

    # 扫描目录
    # 用环境变量拼路径，这样在中文/英文系统、或者用户改过桌面位置时都能找到
    dirs_to_scan = []
    for env_var in ("USERPROFILE", "PUBLIC", "APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env_var)
        if not base:
            continue
        if env_var == "USERPROFILE":
            dirs_to_scan.append(os.path.join(base, "Desktop"))  # 当前用户桌面
        elif env_var == "PUBLIC":
            dirs_to_scan.append(os.path.join(base, "Desktop"))  # 公共桌面
        elif env_var == "APPDATA":
            # 当前用户的开始菜单（用户自己装的软件大多在这里）
            dirs_to_scan.append(os.path.join(base, r"Microsoft\Windows\Start Menu\Programs"))
        elif env_var == "PROGRAMDATA":
            # 全局开始菜单（系统装的和为所有用户安装的软件在这里）
            dirs_to_scan.append(os.path.join(base, r"Microsoft\Windows\Start Menu\Programs"))

    lnk_files = []
    for d in dirs_to_scan:
        if os.path.isdir(d):
            # os.walk 会递归遍历子目录，因为开始菜单是按文件夹分类的
            for root, _dirs, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        lnk_files.append(os.path.join(root, f))

    for lnk_path in lnk_files:
        try:
            sc = shell.CreateShortCut(lnk_path)
            target = sc.TargetPath  # 快捷方式指向的 exe
            if not target:
                continue  # 指向网址或文件夹的快捷方式，跳过
            exe_name = os.path.basename(target).lower()
            if exe_name in _GENERIC_SHORTCUT_TARGETS:
                continue
            # 显示名 = 快捷方式文件名（去掉 .lnk）
            display_name = os.path.splitext(os.path.basename(lnk_path))[0]
            icon_path = sc.IconLocation  # 形如 "C:\\App\\app.exe,0"
            icon_idx = 0
            if icon_path:
                # 从右边找最后一个逗号来切分「路径」和「图标序号」。
                # 用 rsplit(",", 1) 而不是 split(",") 是因为路径本身可能含逗号。
                parts = icon_path.rsplit(",", 1)
                if len(parts) == 2:
                    icon_path = parts[0].strip()
                    try:
                        icon_idx = int(parts[1].strip())
                    except ValueError:
                        icon_idx = 0

            entry = {
                "display_name": display_name,
                "icon_path": icon_path if icon_path and os.path.exists(icon_path) else target,
                "icon_index": icon_idx,
            }
            # 不覆盖已有的（优先桌面 > 开始菜单）
            if exe_name not in _shortcut_cache:
                _shortcut_cache[exe_name] = entry
        except Exception:
            continue  # 个别快捷方式损坏/无权限，跳过即可，不影响其它

    return _shortcut_cache


def get_shortcut_info(exe_name):
    """根据 exe 名称获取快捷方式中的显示名和图标路径。

    找不到对应快捷方式时返回 None。
    """
    shortcuts = _scan_shortcuts()
    return shortcuts.get(exe_name.lower())

# 兜底图标用的配色表：认识的应用用品牌色，圆圈里放名字首字母。
# （想加新应用就在这里补一行：关键字 → 颜色）
_FALLBACK_COLORS = {
    "Chrome": "#4285F4", "Edge": "#0078D7", "Firefox": "#FF7139",
    "微信": "#07C160", "QQ": "#12B7F5", "钉钉": "#0089FF",
    "Slack": "#4A154B", "Telegram": "#0088CC", "飞书": "#3370FF",
    "VS Code": "#007ACC", "Python": "#3776AB", "终端": "#0C0C0C",
    "Spotify": "#1DB954", "Steam": "#1B2838", "UU远程": "#FF6600",
    "文件资源管理器": "#FFB900", "记事本": "#6B5B95",
}


def _extract_icon_iimagelist(exe_path, size=32):
    """首选方案：通过系统的 ImageList 接口取高清图标。

    流程（这是 Windows 取图标的经典套路，照着写就行）：
        1. SHGetFileInfoW 问系统：这个 exe 的图标在系统图标列表里排第几号？
        2. SHGetImageList 拿到那个「超大图标列表」的 COM 接口
        3. 按刚才的编号把图标取出来（得到一个 HICON 句柄）
        4. 建一块内存画布，把图标画上去
        5. 读出画布的像素字节，把 BGRA 格式转成 RGBA

    失败时返回 None，由上层退到 _extract_icon_legacy。
    """
    sfi = SHFILEINFO()
    flags = SHGFI_SYSICONINDEX | SHGFI_ICON
    result = shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(sfi),ctypes.sizeof(sfi), flags)

    if not result:
        return None  # 系统认不出这个文件

    try:
        # ctypes.POINTER(ctypes.c_void_p) 相当于 C 里的 void**
        # COM 方法的第一个参数就是这种「输出参数」，调用后 pml 里就装着接口指针
        pml = ctypes.POINTER(ctypes.c_void_p)()
        hr = shell32.SHGetImageList(
            SHIL_EXTRALARGE, _IID_IImageList, ctypes.byref(pml))
        if hr != 0 or not pml:
            # hr 是 COM 的返回码，0（S_OK）才表示成功
            return None

        # 0x0001 = ILD_TRANSPARENT，意思是保留图标的透明通道
        hicon = comctl32.ImageList_GetIcon(pml, sfi.iIcon, 0x0001)
        if not hicon:
            return None

        # 下面开始准备画布：GetDC(0) 拿整个屏幕的设备上下文，
        # 再用它作为模板创建兼容的内存 DC 和位图（这是 GDI 画图的标准步骤）
        hdc = win32gui.GetDC(0)
        hbmp = win32ui.CreateBitmap()
        hbmp.CreateCompatibleBitmap(win32ui.CreateDCFromHandle(hdc), size, size)

        dc_mem = win32ui.CreateDCFromHandle(win32gui.CreateCompatibleDC(hdc))
        dc_mem.SelectObject(hbmp)  # 把位图选进内存 DC，之后画图就画在位图上

        # 黑色背景填充（处理 alpha 通道）
        # 之所以填黑：后面靠「像素是不是纯黑」来判断透明区域
        dc_mem.FillSolidRect(0, 0, size, size, 0x000000)

        # 0x0003 = DI_NORMAL，即同时画图标和它的掩码，保证透明区域正确
        win32gui.DrawIconEx(dc_mem.GetSafeHdc(), 0, 0, hicon,
                            size, size, 0, None, 0x0003)

        # True 表示按 RGBA 顺序取，但 Windows 内部是 BGRA，下面要手工换回来
        bits = hbmp.GetBitmapBits(True)
        if len(bits) < size * size * 4:
            return None  # 数据长度不对，说明取图失败

        # BGRA → RGBA，用 alpha 通道保留透明度
        # PIL 要求内存里的顺序是 R,G,B,A，而 Windows 给的是 B,G,R,A，
        # 所以要按像素逐个换位置（每 4 个字节是一个像素）
        raw = bits
        pixels = bytearray(size * size * 4)
        for i in range(size * size):
            idx = i * 4
            b, g, r = raw[idx], raw[idx + 1], raw[idx + 2]
            pixels[idx] = r
            pixels[idx + 1] = g
            pixels[idx + 2] = b
            # 如果像素不是纯黑（填充色），说明有内容
            if r or g or b:
                pixels[idx + 3] = 255  # 不透明
            else:
                pixels[idx + 3] = 0    # 完全透明
        # frombuffer 直接复用这段字节，不做拷贝；
        # 最后一个参数 1 表示数据是自下而上排列的（Windows 位图就是这样），
        # 让 PIL 帮忙上下翻转
        img = Image.frombuffer('RGBA', (size, size), bytes(pixels),
                               'raw', 'RGBA', 0, 1)

        # 手工创建的 GDI 资源必须显式释放，否则会持续占用系统资源
        dc_mem.DeleteDC()
        win32gui.ReleaseDC(0, hdc)
        win32gui.DestroyIcon(hicon)
        return img

    except Exception:
        return None


def _extract_icon_legacy(exe_path, size=28):
    """兜底方案：用 SHGetFileInfo + DrawIconEx 取图标。

    比首选方案简单（不依赖 COM 的 ImageList），但拿到的是小尺寸图标，
    放大后比较模糊，所以只在上一个方案失败时使用。
    """
    sfi = SHFILEINFO()
    # 这次只传 SHGFI_ICON，直接要一个图标句柄，不管它在列表里排第几
    shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(sfi),
                           ctypes.sizeof(sfi), SHGFI_ICON)
    if not sfi.hIcon:
        return None

    try:
        hdc = win32gui.GetDC(0)
        hbmp = win32ui.CreateBitmap()
        hbmp.CreateCompatibleBitmap(win32ui.CreateDCFromHandle(hdc), size, size)

        dc_mem = win32ui.CreateDCFromHandle(win32gui.CreateCompatibleDC(hdc))
        dc_mem.SelectObject(hbmp)

        win32gui.DrawIconEx(dc_mem.GetSafeHdc(), 0, 0, sfi.hIcon,
                            size, size, 0, None, 0x0003)

        bits = hbmp.GetBitmapBits(True)
        if len(bits) < size * size * 4:
            return None

        # 和上面的区别：这里不判断黑色，直接把 alpha 全设为 255（不透明）。
        # 因为没有填充黑底，纯黑像素可能是图标本身的一部分，判断会出错。
        raw = bits
        pixels = bytearray(size * size * 4)
        for i in range(size * size):
            idx = i * 4
            pixels[idx] = raw[idx + 2]      # R ← B
            pixels[idx + 1] = raw[idx + 1]  # G 不变
            pixels[idx + 2] = raw[idx]      # B ← R
            pixels[idx + 3] = 255

        img = Image.frombuffer('RGBA', (size, size), bytes(pixels),
                               'raw', 'RGBA', 0, 1)

        dc_mem.DeleteDC()
        win32gui.ReleaseDC(0, hdc)
        return img

    except Exception:
        return None
    finally:
        # finally 保证无论成功、失败还是抛异常，图标句柄都会被释放
        win32gui.DestroyIcon(sfi.hIcon)


def _extract_icon(exe_path, size=28):
    """取图标的统一入口：先试高清方案，失败就退回旧方案。"""
    img = _extract_icon_iimagelist(exe_path, size)
    if img:
        return img
    return _extract_icon_legacy(exe_path, size)


def _find_exe_path(app_name):
    """从当前运行的进程里找出这个应用的 exe 路径（结果会缓存）。

    会连带查找「别名进程」：比如要找 weixin.exe，wechat.exe 也算，
    因为合并规则显示它们属于同一个应用。

    注意只能找到「当前正在运行」的进程。如果应用没开着，就拿不到路径，
    返回 None。
    """
    app_lower = app_name.lower()
    if app_lower in _exe_path_cache:
        cached_path = _exe_path_cache[app_lower]
        # 缓存的 exe 还在磁盘上才能用；文件被卸载/移动了就删掉缓存重新找
        if cached_path and os.path.exists(cached_path):
            return cached_path
        del _exe_path_cache[app_lower]

    import psutil
    from core.storage import canonical_app_name, get_app_rules  # 局部导入，避免循环依赖

    rules = get_app_rules()

    # 收集所有同类进程名
    # 比如 app_name 是 weixin.exe，那么 wechat.exe、wechatappex.exe 也算「同类」，
    # 因为它们合并后都是同一个应用，随便哪个的 exe 都能拿来取图标
    aliases = {app_lower}
    for child, parent in rules.items():
        if canonical_app_name(child, rules) == app_lower:
            aliases.add(child)

    result = None
    for proc in psutil.process_iter(['name', 'exe']):
        try:
            if proc.info['name'] and proc.info['name'].lower() in aliases:
                result = proc.info['exe']
                break  # 找到一个就够了
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # 进程在遍历途中退出了，跳过

    _exe_path_cache[app_lower] = result
    return result


def _make_fallback_icon(display_name, size=28):
    """兜底图标：画一个「彩色圆圈 + 名字首字母」。

    当所有办法都拿不到真实图标时用它充数，总比列表里空一块好看。
    """
    color = "#64748B"  # 默认灰蓝色
    for key, c in _FALLBACK_COLORS.items():
        # 用「关键字包含」来判断，比如名字里有 "Chrome" 就用 Chrome 的蓝色
        if key in display_name:
            color = c
            break

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 2
    draw.ellipse([pad, pad, size - pad, size - pad], fill=color)

    letter = (display_name[0] if display_name else "?").upper()  # 首字母；名字为空就用问号
    try:
        # 依次尝试几个常见的中文字体，谁先加载成功就用谁。
        # 如果能找到微软雅黑之类的中文字体，中文字符也能正常显示。
        for font_name in ["msyh.ttc", "msyhbd.ttc", "arial.ttf", "segoeui.ttf"]:
            try:
                font = ImageFont.truetype(font_name, size // 2)
                break
            except (IOError, OSError):
                continue
        else:
            # for-else：循环正常结束（一次都没 break）时执行，
            # 也就是「所有字体都没找到」，退回 Pillow 自带的位图字体
            font = ImageFont.load_default()

        # textbbox 返回文字外框，用它算出的宽高把字母居中
        bbox = draw.textbbox((0, 0), letter, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (size - tw) // 2
        y = (size - th) // 2 - 1  # 视觉上略微上移一点，更居中
        draw.text((x, y), letter, fill="white", font=font)
    except Exception:
        pass  # 画字失败也无所谓，至少还有个彩色圆圈

    return img


def get_icon(app_name, display_name="", size=28, exe_path=""):
    """获取应用图标，优先使用「使用时记录下来的 exe 路径」。

    这是本模块对外的唯一入口。会按优先级依次尝试多种来源，
    第一个成功的直接返回，全失败就用兜底图标。

    参数：
        app_name     数据库里的规范进程名，如 "weixin.exe"
        display_name 中文显示名，用于兜底图标的首字母和配色
        size         图标边长（像素）
        exe_path     使用该应用时记录下来的 exe 路径，可为空
    返回：PIL 的 Image 对象。
    """
    # 同一个 exe 在 Windows 上大小写可能不一致，用 normcase 统一，
    # 否则 "C:\\App.exe" 和 "c:\\app.exe" 会占两份缓存
    normalized_path = os.path.normcase(exe_path) if exe_path else ""
    cache_key = f"{app_name}_{normalized_path}_{size}"
    if cache_key in _cache:
        return _cache[cache_key]

    # 文件名和规范名一致的 exe 是最可信的来源，比如我们要 weixin.exe 的图标，
    # 记录下来的路径恰好就是 weixin.exe，那就直接用它
    is_canonical_path = (
        exe_path and
        os.path.basename(exe_path).lower() == app_name.lower()
    )
    if is_canonical_path and os.path.exists(exe_path):
        img = _extract_icon(exe_path, size)
        if img:
            _cache[cache_key] = img
            return img

    # 对于被合并的应用，快捷方式里的图标通常属于真正的那个主程序，
    # 比辅助进程的图标更贴切，所以这里优先用快捷方式
    sc_info = get_shortcut_info(app_name)
    if sc_info:
        icon_path = sc_info["icon_path"]
        if icon_path and os.path.exists(icon_path):
            img = _extract_icon(icon_path, size)
            if img:
                _cache[cache_key] = img
                return img

    # 历史数据里如果只记录到辅助进程也没关系——
    # 有真实图标总比用兜底占位图强
    if exe_path and os.path.exists(exe_path):
        img = _extract_icon(exe_path, size)
        if img:
            _cache[cache_key] = img
            return img

    # 换个思路：看看这个应用现在是不是正开着，从运行中的进程里问路径
    exe_path = _find_exe_path(app_name)
    if exe_path:
        img = _extract_icon(exe_path, size)
        if img:
            _cache[cache_key] = img
            return img

    # 有些系统自带程序的路径是固定的，直接去 Windows 目录和 System32 里找
    sys_paths = [
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), app_name),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", app_name),
    ]
    for path in sys_paths:
        if os.path.exists(path):
            img = _extract_icon(path, size)
            if img:
                _cache[cache_key] = img
                return img

    # 全部失败，只能用程序画的占位图标兜底。
    # 注意这里也写进了缓存——
    # 因为走到这一步说明确实没有更好的来源了，没必要每次刷新都重试一遍。
    img = _make_fallback_icon(display_name or app_name, size)
    _cache[cache_key] = img
    return img
