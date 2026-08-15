"""封面双拼图裁剪。

源站给的封面多是一张横版双拼图：一半是碟片封套（带条码、厂牌 logo、大段
文字），另一半是人像正片。前端卡片是 80x112 的竖版框，整张塞进去只能看到
中间接缝那一条，两边都被 object-cover 切掉。

所以落盘前先把人像那半边裁出来。哪半边是人像没有元数据可查，只能看图判断：
封套那面是印刷排版，成片的纯色块多、边缘集中在文字行；人像那面是照片，
细节铺满整幅、肤色让饱和度分布更宽。两个指标分别打分再投票。

判断不可靠时一律原样返回 —— 非双拼的整幅封面被误裁一半，比不裁难看得多。
"""
from __future__ import annotations

import io

from loguru import logger

try:
    from PIL import Image, ImageFilter
except ImportError:  # pragma: no cover - 没装 Pillow 时整个功能降级为不裁
    Image = None
    ImageFilter = None

# 宽高比低于这个值就不是双拼图。正片封面单张接近 2:3，双拼后大于 1.3；
# 留一点余量，避免把本来就偏方的封面当成双拼切了
MIN_PANEL_RATIO = 1.3

# 两半的得分差不到这个比例就认为分不出来，保持原图。
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


def pick_portrait_half(data: bytes) -> bytes:
    """把双拼封面裁成人像那半边，判断不了就原样返回。

    返回的永远是可以直接落盘的图片字节；任何异常都退回原图，
    图片处理失败不该让整条封面缓存链路断掉。
    """
    if not data or Image is None:
        return data

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if not height or width / height < MIN_PANEL_RATIO:
                return data

            # 统一转 RGB：源图可能是带调色板的 PNG 或带透明通道的 webp，
            # 直接送进 HSV 转换会抛错
            rgb = image.convert("RGB")
            mid = width // 2

            left_score = _panel_score(_sample(rgb.crop((0, 0, mid, height))))
            right_score = _panel_score(_sample(rgb.crop((mid, 0, width, height))))

            total = left_score + right_score
            if total <= 0:
                return data
            if abs(left_score - right_score) / total < MIN_SCORE_MARGIN:
                # 两面差不多，多半不是双拼图，别赌
                return data

            box = (mid, 0, width, height) if right_score > left_score else (0, 0, mid, height)
            cropped = rgb.crop(box)

            buffer = io.BytesIO()
            # 统一存 JPEG：缓存层按 URL 后缀决定文件名，而裁完的图重新编码后
            # 原格式已无意义，JPEG 对照片体积最省
            cropped.save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()
    except Exception as exc:
        logger.debug(f"封面裁剪失败，保留原图: {exc}")
        return data
