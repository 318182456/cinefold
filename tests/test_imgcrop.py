"""封面偏移方向的判断。

源站封面多是「碟片封套 + 人像正片」的横版双拼图。图片完整存盘，只判断该往
哪半边偏，前端据此设 object-position 让卡片露出人像那面。

判断按番号分类走规则，不看图内容 —— 看图的启发式在真实封面上会翻车，
写真集类封套贴满小照片，细节比人像面还密（详见 utils/imgcrop.py 的说明）。
"""
from __future__ import annotations

import io

import pytest

from app.utils import imgcrop

Image = pytest.importorskip("PIL.Image", reason="未安装 Pillow")


def _encode(width: int, height: int) -> bytes:
    """造一张指定尺寸的图。内容不影响判断，只有尺寸和番号参与决策。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (128, 110, 100)).save(buffer, format="JPEG")
    return buffer.getvalue()


# 双拼图的典型尺寸，来自真实图库
WIDE = _encode(800, 538)
# 单张竖版海报
TALL = _encode(400, 560)


class Test番号分类:
    @pytest.mark.parametrize(
        "code,expect",
        [
            ("SVERS-005", "standard"),
            ("REBD-971", "standard"),
            ("ADN-771", "standard"),
            ("T28-544", "standard"),
            ("FC2-PPV-1570936", "fc2"),
            ("200GANA-3282", "amateur"),
            ("300MIUM-812", "amateur"),
            ("032416_267", "uncensored"),
            ("N1234", "uncensored"),
            ("", "unknown"),
        ],
    )
    def test_分类(self, code, expect):
        assert imgcrop.classify(code) == expect


class Test偏移判断:
    def test_常规jav双拼图偏右(self):
        """有码 JAV 版式固定：缩略图是光碟封面，人像恒在右侧。"""
        assert imgcrop.detect_portrait_side(WIDE, "SVERS-005") == imgcrop.RIGHT

    def test_常规jav竖版不偏(self):
        """已经是竖版海报，整张就是人像，没有另一半可偏。"""
        assert imgcrop.detect_portrait_side(TALL, "SVERS-005") == imgcrop.NONE

    @pytest.mark.parametrize("code", ["200GANA-3282", "FC2-PPV-1570936", "032416_267"])
    def test_版式不固定的类型一律不偏(self, code):
        """素人/FC2/无码的图尺寸五花八门，右侧未必是人像，不猜。"""
        assert imgcrop.detect_portrait_side(WIDE, code) == imgcrop.NONE

    def test_没有番号时不偏(self):
        """认不出类型就别赌。"""
        assert imgcrop.detect_portrait_side(WIDE, "") == imgcrop.NONE

    def test_损坏数据不偏(self):
        """判断失败不该让整条封面缓存链路断掉。"""
        assert imgcrop.detect_portrait_side(b"not an image" * 50, "SVERS-005") == imgcrop.NONE

    def test_空数据不偏(self):
        assert imgcrop.detect_portrait_side(b"", "SVERS-005") == imgcrop.NONE

    def test_判断不修改图片(self):
        """图片要完整存盘，灯箱还要看原图。"""
        before = len(WIDE)
        imgcrop.detect_portrait_side(WIDE, "SVERS-005")
        assert len(WIDE) == before


class Test从文件判断:
    def test_番号取自目录名(self, tmp_path):
        """缓存布局是 pics/<番号>/banner.jpg，不传番号时从目录名取。"""
        d = tmp_path / "SVERS-005"
        d.mkdir()
        path = d / "banner.jpg"
        path.write_bytes(WIDE)
        assert imgcrop.detect_from_file(path) == imgcrop.RIGHT

    def test_素人番号目录不偏(self, tmp_path):
        d = tmp_path / "200GANA-3282"
        d.mkdir()
        path = d / "banner.jpg"
        path.write_bytes(WIDE)
        assert imgcrop.detect_from_file(path) == imgcrop.NONE

    def test_显式传番号优先(self, tmp_path):
        """回填脚本走的是这条路径，番号来自数据库而非目录名。"""
        path = tmp_path / "banner.jpg"
        path.write_bytes(WIDE)
        assert imgcrop.detect_from_file(path, "SVERS-005") == imgcrop.RIGHT

    def test_文件不存在不偏(self, tmp_path):
        assert imgcrop.detect_from_file(tmp_path / "nope.jpg", "SVERS-005") == imgcrop.NONE
