"""封面双拼图裁剪。

源站封面常是「碟片封套 + 人像正片」的横版双拼图，落盘前要把人像那半边裁出来。
判断靠启发式（边缘密度 + 饱和度分布），所以既要验证能裁对，也要验证
分不出来时肯放弃 —— 非双拼图被误切一半比不裁更难看。
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


def test_人像在右时裁出右半边():
    data = _encode(_join(_sleeve(), _portrait()))
    out = imgcrop.pick_portrait_half(data)

    assert out is not data, "双拼图应该被裁剪"
    result = _decode(out)
    assert result.size == (PANEL_W, PANEL_H)
    # 封套那面是深蓝底，人像那面是肤色。取红通道明显高于蓝通道即可确认没裁反
    r, g, b = result.getpixel((5, 5))
    assert r > b, f"裁出来的应是人像面，实际像素 {(r, g, b)} 更像封套"


def test_人像在左时裁出左半边():
    """左右哪面是人像没有规律，不能写死切右半。"""
    data = _encode(_join(_portrait(seed=2), _sleeve()))
    out = imgcrop.pick_portrait_half(data)

    assert out is not data
    result = _decode(out)
    assert result.size == (PANEL_W, PANEL_H)
    r, g, b = result.getpixel((5, 5))
    assert r > b, f"裁出来的应是人像面，实际像素 {(r, g, b)} 更像封套"


def test_单张竖版封面保持原样():
    """不是双拼图就不该动它。"""
    data = _encode(_portrait(400, 560, seed=3))
    assert imgcrop.pick_portrait_half(data) is data


def test_左右同质时放弃裁剪():
    """两面得分接近说明多半不是双拼图，宁可不裁也别赌。"""
    data = _encode(_join(_portrait(seed=4), _portrait(seed=5)))
    assert imgcrop.pick_portrait_half(data) is data


def test_重复裁剪幂等():
    """回填脚本可能被跑多次，裁过的图不能越裁越窄。"""
    data = _encode(_join(_sleeve(), _portrait()))
    once = imgcrop.pick_portrait_half(data)
    assert once is not data

    twice = imgcrop.pick_portrait_half(once)
    assert twice is once, "已经是竖版的图不该再被裁一次"


def test_损坏数据原样返回():
    """图片处理失败不该让整条封面缓存链路断掉。"""
    broken = b"definitely not an image" * 50
    assert imgcrop.pick_portrait_half(broken) is broken


def test_空数据原样返回():
    assert imgcrop.pick_portrait_half(b"") == b""


def test_带透明通道的图不报错():
    """源图可能是带 alpha 的 png/webp，直接送去 HSV 转换会抛错。"""
    left = _sleeve().convert("RGBA")
    right = _portrait().convert("RGBA")
    canvas = Image.new("RGBA", (PANEL_W * 2, PANEL_H))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (PANEL_W, 0))

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    data = buffer.getvalue()

    out = imgcrop.pick_portrait_half(data)
    assert out is not data
    assert _decode(out).size == (PANEL_W, PANEL_H)
