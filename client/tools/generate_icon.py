"""生成 app.ico（Windows 可执行文件用的图标）。

这个脚本只在打包 exe 之前跑一次（见 build.bat），运行结果是一个
app.ico 文件，供 PyInstaller 当作 exe 的图标。

.ico 和 .png 的区别：.ico 可以同时包含多个尺寸（16x16、32x32、256x256……），
Windows 会按用途挑合适的那张——任务栏用大的，文件列表用小的。
所以下面会把一张图缩放出 6 种尺寸，一起塞进同一个 ico 文件里。

另外，源图「图标.png」是个不带透明通道的正方形，直接拿来当图标四个角会是
生硬的直角。所以下面加了一步「圆角处理」，见 _round_corners()。
"""
import os
from PIL import Image, ImageDraw, ImageChops

# 圆角半径占边长的比例。0.22 这个值画出来的实际圆角约占边长的 1/5，
# 和 Windows 11 / iOS 应用图标的圆角程度相当（Pillow 的 rounded_rectangle
# 画出的圆弧会比标称半径略小一点，所以不用去纠结传进去的原始数字）。
# 想更圆就调大（0.35 就很圆了），想更方就调小。
CORNER_RADIUS_RATIO = 0.22

# 本文件在 client/tools/ 里，所以往上退两级是 client/。
# 这里不 import core.paths 是有意的：这个脚本要能直接
# `python client/tools/generate_icon.py` 跑起来，而那样启动时 client/ 并不在
# 模块搜索路径上，import core.paths 会失败。所以路径自己算。
_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSETS_DIR = os.path.join(_CLIENT_DIR, "assets")

# app.ico 固定生成到 client/ 下（app.spec 里的 icon='app.ico' 就是按这个位置
# 找的）。这里写成绝对路径而不是相对的 "app.ico"，是因为相对路径会跟着
# 当前工作目录跑——从仓库根执行脚本时就会把 ico 落到仓库根，和 spec 对不上。
_OUT_ICO = os.path.join(_CLIENT_DIR, "app.ico")


def _round_corners(img, radius_ratio=CORNER_RADIUS_RATIO, ss=4):
    """把一张正方形图片的四个直角磨圆，返回新的 RGBA 图片（尺寸不变）。

    参数：
        img：源图，PIL Image。没有 alpha 通道的（如 RGB）会自动补上。
        radius_ratio：圆角半径相对边长的比例，见上面的 CORNER_RADIUS_RATIO。
        ss：超采样倍数（supersampling）。
    返回值：磨好圆角的 RGBA 图片。

    为什么需要它：图标本质上是「把图片四角变成透明」。做法是先造一张蒙版
    （圆角矩形内为白 255、圆角外为黑 0），再拿它当图片的 alpha 通道。
    细节在于：
      * 先在 ss 倍大的画布上画蒙版、再缩回原尺寸 —— 这叫超采样。缩放时
        边缘像素会被算成 0~255 之间的灰阶，圆角因此有平滑的过渡，不会
        出现难看的锯齿台阶（如果直接在原尺寸上二值画蒙版就会锯齿）。
      * 蒙版和源图「原有的」alpha 逐像素取较小值，而不是直接覆盖。这样
        万一以后换成带透明背景的源图，它本身的透明区域也不会被抹掉。
    """
    img = img.convert("RGBA")          # 保证有 alpha 通道可写
    w, h = img.size
    radius = max(1, int(min(w, h) * radius_ratio))

    # 超采样画蒙版：Image.L 是「单通道 8 位灰度」模式，正好当 alpha 用
    mask = Image.new("L", (w * ss, h * ss), 0)   # 0 = 全黑 = 全透明
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w * ss - 1, h * ss - 1], radius=radius * ss, fill=255
    )
    mask = mask.resize((w, h), Image.LANCZOS)    # 缩回原尺寸，边缘变平滑

    out = img.copy()
    # ImageChops.darker 是逐像素取较小值（min），等效于「两者中更透明的那档」
    out.putalpha(ImageChops.darker(img.getchannel("A"), mask))
    return out


def _save_ico(images, sizes, path=_OUT_ICO):
    """把多个尺寸的图片打包成一个 .ico 文件。

    参数：
        images：PIL Image 列表，各张尺寸不同。
        sizes：尺寸列表，如 [16, 32, ...]，告诉 Pillow 每张图对应哪个尺寸。
        path：输出文件路径。

    这里有个特别容易踩的坑，务必注意：
        Pillow 保存 ico 时，会拿「主图」（也就是调用 save() 的那个对象）的
        尺寸当作上限，凡是比它大的尺寸都会被静默跳过（见 Pillow 源码
        IcoImagePlugin._save 里那句 `if size[0] > width ... continue`）。
        所以主图必须选最大的那张。如果图省事拿 images[0]（16x16）当主图，
        32/48/64/128/256 会全部被丢掉，ico 里只剩一张 16x16 —— 文件名看着
        没问题，但 Windows 只能把这个小图放大去糊任务栏，看起来又糊又方。

    另外用 bitmap_format="bmp"：让每张帧以未压缩的 BMP 存储。PNG 压缩帧
    只有在 Vista 以后的系统才被支持，而 BMP 帧在所有 Windows 版本上都能
    正常显示，体积代价（几十 KB）相对于 exe 本身可以忽略，图省心就用它。
    """
    images = sorted(images, key=lambda im: im.size[0])   # 从小到大排，确保最后一张最大
    images[-1].save(
        path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[:-1],      # 其余尺寸作为附加帧
        bitmap_format="bmp",
    )


def generate_icon():
    """生成 app.ico 并保存到 client/ 目录下（见 _OUT_ICO）。

    优先用项目自带的「图标.png」；如果图片不存在，就用代码画一个时钟图标。
    """
    icon_path = os.path.join(_ASSETS_DIR, "图标.png")

    if os.path.exists(icon_path):
        # convert("RGBA") 保证有 alpha 通道，否则后面缩放出 ico 可能出问题
        src = Image.open(icon_path).convert("RGBA")
        sizes = [16, 32, 48, 64, 128, 256]
        images = []
        for size in sizes:
            # LANCZOS 是缩小时质量较好的重采样算法
            img = src.resize((size, size), Image.LANCZOS)
            # 每个尺寸都单独磨一次圆角：越小的尺寸圆角半径也越小，
            # 视觉上才和系统里其它图标保持一致。
            img = _round_corners(img)
            images.append(img)
        _save_ico(images, sizes)
        print("Generated app.ico from 图标.png")
        return

    # Fallback: draw a clock icon
    # 没有图片资源时的兜底：程序化画一个双指针时钟
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        # 每个尺寸都单独画一遍（而不是缩放小图），这样小图标也足够清晰
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # 下面所有尺寸参数都按 size 比例算出来，所以 16px 和 256px 看起来一致
        pad = max(1, size // 16)
        # 蓝色圆底；outline 是边框色，width 是边框粗细
        draw.ellipse([pad, pad, size - pad, size - pad],
                     fill=(10, 132, 255), outline=(64, 156, 255),
                     width=max(1, size // 32))
        cx, cy = size // 2, size // 2  # 圆心坐标
        hand_top = size // 5
        draw.line([cx, cy, cx, hand_top], fill="white",
                  width=max(1, size // 20))
        import math
        # 时针指向「10 点 10 分」方向（约 -50°）——这是钟表图标常用的经典角度，
        # 视觉上左右对称、比较好看。数学角度逆时针为正，但屏幕 y 轴朝下，故取负值。
        angle = -math.radians(50)
        hx = cx + int(size // 4 * math.cos(angle))
        hy = cy + int(size // 4 * math.sin(angle))
        draw.line([cx, cy, hx, hy], fill="white",
                  width=max(1, size // 20))
        dot_r = max(1, size // 20)
        draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
                     fill="white")  # 圆心轴点
        images.append(img)
    # 兜底的时钟图标本身就是圆形，四周已经是透明的，不用再磨圆角
    _save_ico(images, sizes)
    print("Generated app.ico (fallback)")


if __name__ == "__main__":
    generate_icon()
