"""判断封面该往哪半边偏。

源站给的封面多是一张横版双拼图：一半是碟片封套（条码、厂牌 logo、大段
文字），另一半是人像正片。前端卡片是 80x112 的竖版框，整张塞进去只能看到
中间接缝那一条，两边都被 object-cover 切掉。

这里只做判断、不动图片：图片完整存盘，前端拿判断结果设 object-position，
让卡片只露人像那半边；点开灯箱时仍然显示完整原图。

判断靠番号分类而不是看图。曾经试过看图的启发式（边缘密度 + 饱和度分布），
在真实封面上翻车了：以 REBD-971 为例，左边封套是贴满十几张小照片的写真集
排版，边缘密度 50.0 对人像面的 28.0，饱和度也更宽，两个指标同时指向错误
答案。「细节多 = 人像」这个前提在双拼封面上根本不成立 —— 封套那面永远是
拼贴加文字，细节必然更碎。

而常规有码 JAV 的封面版式是固定的：缩略图为光碟封面，人像恒在右侧。按番号
分类直接套规则，这一类就是 100% 准确，压根不用猜。版式不固定的类型（素人、
FC2、无码）尺寸五花八门，没有可靠规律可循，一律不偏、居中显示。
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 没装 Pillow 时整个功能降级为不偏移
    Image = None

# 判断结果。存进库里、发给前端的就是这三个值
LEFT = "left"
RIGHT = "right"
NONE = "none"

# 宽高比低于这个值就不是双拼图，整张就是海报本身，没有可偏的另一半。
# 正片封面单张接近 2:3，双拼后大于 1.3
MIN_PANEL_RATIO = 1.3

# 无码站的日期型番号（032416_267）。整个番号纯数字，版式不固定
_DATE_CODE = re.compile(r"^\d{6}[-_]\d{2,4}$")

# 素人系：番号以三位数字厂牌前缀开头（200GANA-3282、300MIUM-xxx）。
# 这类图尺寸不规则，16:9 的、方的都有，右侧未必是人像
_AMATEUR_CODE = re.compile(r"^\d{3}[A-Z]")

# 无码的纯数字/n 打头番号（n1234、1234）
_NUMERIC_CODE = re.compile(r"^N?\d+$")

# 常规有码 JAV：字母打头的厂牌前缀 + 横杠 + 数字，版式固定，人像恒在右侧。
# 前缀允许夹数字（T28-544、SDMU-001 都要认），但必须以字母开头 ——
# 数字开头的已经在上面被素人系那条规则拦走了
_STANDARD_CODE = re.compile(r"^[A-Z][A-Z0-9]{1,7}-\d{2,5}")


def classify(code: str) -> str:
    """按番号判定作品类型，决定用哪条偏移规则。

    返回 standard / amateur / fc2 / uncensored / unknown。
    """
    c = (code or "").strip().upper()
    if not c:
        return "unknown"
    if c.startswith("FC2"):
        return "fc2"
    if _DATE_CODE.match(c):
        return "uncensored"
    if _AMATEUR_CODE.match(c):
        return "amateur"
    if _NUMERIC_CODE.match(c):
        return "uncensored"
    if _STANDARD_CODE.match(c):
        return "standard"
    return "unknown"


def detect_portrait_side(data: bytes, code: str = "") -> str:
    """判断封面该往哪半边偏，返回 LEFT / RIGHT / NONE。

    只读图不改图 —— 图片完整存盘，灯箱还要看原图。
    读不出尺寸时返回 NONE，前端按普通封面居中显示。
    """
    if not data or Image is None:
        return NONE

    # 版式不固定的类型没有可靠规律，不猜
    if classify(code) != "standard":
        return NONE

    try:
        from io import BytesIO

        with Image.open(BytesIO(data)) as image:
            width, height = image.size
    except Exception as exc:
        logger.debug(f"读取封面尺寸失败: {exc}")
        return NONE

    if not height or width / height < MIN_PANEL_RATIO:
        # 已经是竖版海报，整张就是人像，没有另一半可偏
        return NONE
    return RIGHT


def detect_from_file(path, code: str = "") -> str:
    """从磁盘上的图片判断偏移方向。

    番号可以不传，默认从文件所在目录名取 —— 缓存布局就是 pics/<番号>/banner.jpg。
    """
    target = Path(path)
    if not code:
        code = target.parent.name

    if classify(code) != "standard":
        return NONE
    if Image is None:
        return NONE

    try:
        with Image.open(target) as image:
            width, height = image.size
    except Exception as exc:
        logger.debug(f"读取封面失败 {target}: {exc}")
        return NONE

    if not height or width / height < MIN_PANEL_RATIO:
        return NONE
    return RIGHT
