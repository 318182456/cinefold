"""判断封面双拼图的人像在哪半边。

源站给的封面多是一张横版双拼图：一半是碟片封套（带条码、厂牌 logo、大段
文字），另一半是人像正片。前端卡片是 80x112 的竖版框，整张塞进去只能看到
中间接缝那一条，两边都被 object-cover 切掉。

这里只做判断、不动图片：图片完整存盘，前端拿判断结果设 object-position，
让卡片只露人像那半边；点开灯箱时仍然显示完整原图。

哪半边是人像没有元数据可查，只能看图判断：封套那面是印刷排版，成片的纯色
块多、边缘集中在文字行；人像那面是照片，细节铺满整幅、肤色让饱和度分布
更宽。两个指标分别打分再投票。

判断不出来时返回 NONE，前端退回居中显示 —— 非双拼的整幅封面被偏到一边，
比不动它更难看。
"""
from __future__ import annotations

import io
from pathlib import Path

from loguru import logger

try:
    from PIL import Image, ImageFilter
except ImportError:  # pragma: no cover - 没装 Pillow 时整个功能降级为不判断
    Image = None
    ImageFilter = None

# 判断结果。存进库里、发给前端的就是这三个值
LEFT = "left"
RIGHT = "right"
NONE = "none"

# 宽高比低于这个值就不是双拼图。正片封面单张接近 2:3，双拼后大于 1.3；
# 留一点余量，避免把本来就偏方的封面当成双拼
MIN_PANEL_RATIO = 1.3

# 两半的得分差不到这个比例就认为分不出来，返回 NONE。
# 双拼图两面的差异通常很明显，差距小往往说明这压根不是双拼图
MIN_SCORE_MARGIN = 0.12

# 每半边采样时缩到这个宽度再算指标。原图半边动辄 400 宽，逐像素统计没必要
# 那么精确，缩图后快一个数量级。
#
# 必须切开之后各自缩放，不能先把整幅缩小再切：整幅缩到这个宽度时每半边只剩
# 一百多像素，FIND_EDGES 会把照片的细节一起抹平，两半的得分被拉到同一水平，
# 双拼图反而判不出来
SAMPLE_WIDTH = 240

# 缩放用最近邻。双线性会把噪点平均掉，正好抹掉我们要测的那点细节差异
RESAMPLE = Image.NEAREST if Image is not None else None


def _sample(image):
    """把半边图缩到统一宽度，保持长宽比。"""
    width, height = image.size
    if width <= SAMPLE_WIDTH:
        return image
    return image.resize(
        (SAMPLE_WIDTH, max(1, round(SAMPLE_WIDTH * height / width))), RESAMPLE
    )


def _edge_density(image) -> float:
    """轮廓强度的平均值，衡量画面细节有多密。

    照片的细节铺满整幅，印刷封套除了文字行大多是平的。
    """
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    total = sum(histogram)
    if not total:
        return 0.0
    weighted = sum(level * count for level, count in enumerate(histogram))
    return weighted / total


def _saturation_spread(image) -> float:
    """饱和度的分布宽度。

    肤色和布料让照片的饱和度散得开；封套的底色和色块集中在少数几档。
    用非空档位的占比来量化，比算方差更抗大面积纯色背景的干扰。
    """
    saturation = image.convert("HSV").getchannel("S")
    histogram = saturation.histogram()
    total = sum(histogram)
    if not total:
        return 0.0
    # 占比低于千分之一的档位算噪声，不计入宽度
    floor = total / 1000
    return sum(1 for count in histogram if count > floor) / len(histogram)


def _panel_score(image) -> float:
    """给半边图打分，越高越像人像面。"""
    # 边缘密度的量纲比饱和度占比大一个数量级，先压到同一量级再相加，
    # 否则饱和度那一项等于没参与投票
    return _edge_density(image) / 64 + _saturation_spread(image)


def detect_portrait_side(data: bytes) -> str:
    """判断人像在双拼封面的哪半边，返回 LEFT / RIGHT / NONE。

    只读图不改图。任何异常都退回 NONE —— 判断失败不该让整条封面缓存链路断掉，
    前端拿到 NONE 就按普通封面居中显示。
    """
    if not data or Image is None:
        return NONE

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if not height or width / height < MIN_PANEL_RATIO:
                return NONE

            # 统一转 RGB：源图可能是带调色板的 PNG 或带透明通道的 webp，
            # 直接送进 HSV 转换会抛错
            rgb = image.convert("RGB")
            mid = width // 2

            left_score = _panel_score(_sample(rgb.crop((0, 0, mid, height))))
            right_score = _panel_score(_sample(rgb.crop((mid, 0, width, height))))

            total = left_score + right_score
            if total <= 0:
                return NONE
            if abs(left_score - right_score) / total < MIN_SCORE_MARGIN:
                # 两面差不多，多半不是双拼图，别赌
                return NONE

            return RIGHT if right_score > left_score else LEFT
    except Exception as exc:
        logger.debug(f"封面人像面判断失败: {exc}")
        return NONE


def detect_from_file(path) -> str:
    """从磁盘上的图片判断人像面，读不出来就返回 NONE。"""
    try:
        return detect_portrait_side(Path(path).read_bytes())
    except OSError as exc:
        logger.debug(f"读取封面失败 {path}: {exc}")
        return NONE
