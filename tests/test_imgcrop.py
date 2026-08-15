"""封面双拼图的人像面判断。

源站封面常是「碟片封套 + 人像正片」的横版双拼图。图片完整存盘，只判断人像
在哪半边，前端据此设 object-position 让卡片露出人像那面。

判断靠启发式（边缘密度 + 饱和度分布），所以既要验证能判对，也要验证
分不出来时肯返回 NONE —— 非双拼图被偏到一边比居中显示更难看。
"""
from __future__ import annotations

import io
import random

import pytest

from app.utils import imgcrop

Image = pytest.importorskip("PIL.Image", reason="未安装 Pillow")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

PANEL_W, PANEL_H = 400, 538


def _sleeve(width=PANEL_W, height=PANEL_H):
    """碟片封套：纯色底 + 文字行 + 色块。

    边缘集中在文字行、饱和度只占几档，是启发式里的「低分」那一面。
    """
    img = Image.new("RGB", (width, height), (18, 30, 92))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, 60], fill=(210, 30, 40))
    for i in range(6):
        y = 110 + i * 34
        draw.rectangle([20, y, width - 30, y + 12], fill=(240, 240, 240))
    return img


def _portrait(width=PANEL_W, height=PANEL_H, seed=1):
    """人像照片：铺满噪点的连续肤色渐变。

    细节铺满整幅、饱和度散得开，是启发式里的「高分」那一面。
    """
    rnd = random.Random(seed)
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            noise = rnd.randint(-38, 38)
            px[x, y] = (
                max(0, min(255, 200 + int(30 * x / width) - int(20 * y / height) + noise)),
                max(0, min(255, 150 + int(40 * y / height) + noise)),
                max(0, min(255, 130 + int(35 * (x + y) / (width + height)) + noise)),
            )
    return img


def _join(left, right):
    """左右拼成一张双拼图。"""
    canvas = Image.new("RGB", (left.width + right.width, left.height))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width, 0))
    return canvas


def _encode(img) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _decode(data: bytes):
    return Image.open(io.BytesIO(data))


def test_人像在右时判为右():
    data = _encode(_join(_sleeve(), _portrait()))
    assert imgcrop.detect_portrait_side(data) == imgcrop.RIGHT


def test_人像在左时判为左():
    """左右哪面是人像没有规律，不能写死认右半。"""
    data = _encode(_join(_portrait(seed=2), _sleeve()))
    assert imgcrop.detect_portrait_side(data) == imgcrop.LEFT


def test_单张竖版封面判为none():
    """不是双拼图就没有偏移的必要。"""
    data = _encode(_portrait(400, 560, seed=3))
    assert imgcrop.detect_portrait_side(data) == imgcrop.NONE


def test_左右同质时判为none():
    """两面得分接近说明多半不是双拼图，宁可居中也别赌。"""
    data = _encode(_join(_portrait(seed=4), _portrait(seed=5)))
    assert imgcrop.detect_portrait_side(data) == imgcrop.NONE


def test_判断不修改图片():
    """图片要完整存盘，灯箱还要看原图，判断过程不能动它。"""
    data = _encode(_join(_sleeve(), _portrait()))
    before = _decode(data).size
    imgcrop.detect_portrait_side(data)
    assert _decode(data).size == before


def test_损坏数据判为none():
    """判断失败不该让整条封面缓存链路断掉。"""
    assert imgcrop.detect_portrait_side(b"definitely not an image" * 50) == imgcrop.NONE


def test_空数据判为none():
    assert imgcrop.detect_portrait_side(b"") == imgcrop.NONE


def test_带透明通道的图不报错():
    """源图可能是带 alpha 的 png/webp，直接送去 HSV 转换会抛错。"""
    canvas = Image.new("RGBA", (PANEL_W * 2, PANEL_H))
    canvas.paste(_sleeve().convert("RGBA"), (0, 0))
    canvas.paste(_portrait().convert("RGBA"), (PANEL_W, 0))

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")

    assert imgcrop.detect_portrait_side(buffer.getvalue()) == imgcrop.RIGHT


def test_从文件判断(tmp_path):
    """回填脚本走的是这条路径。"""
    path = tmp_path / "banner.jpg"
    path.write_bytes(_encode(_join(_sleeve(), _portrait())))
    assert imgcrop.detect_from_file(path) == imgcrop.RIGHT


def test_文件不存在判为none(tmp_path):
    assert imgcrop.detect_from_file(tmp_path / "nope.jpg") == imgcrop.NONE
