"""刮削图片落盘：海报、背景、剧照。

Emby / Jellyfin 在影片目录里按固定文件名找图（每部片一个目录时）：

    <影片名>-poster.jpg    竖版海报，列表页的封面
    <影片名>-fanart.jpg    横版背景，详情页大图
    <影片名>-thumb.jpg     缩略图，部分皮肤用它
    extrafanart/N.jpg      剧照集，详情页的"图片"标签

与 utils/imagecache 的分工很关键，两边存的是不同东西：

    imagecache   pics/<番号>/banner.jpg   给 Web UI 用，一部片一份，
                                          存的是**完整原图**
    这里         媒体库影片目录旁           给 Emby 用，按分集各一份，
                                          海报是**裁过的竖版**

所以不能简单把缓存文件硬链过去：源站封面多是横版双拼图（左碟片右人像），
Emby 的列表页按竖版比例显示，直接拿横版图当 poster 会把人像挤成一条。
这里要真的裁一刀 —— 裁哪半边由 Code.portrait_side 决定，那个判断结果
是既有的（imgcrop 在下载时就算好了），不重新看图。

fanart 用完整原图，不裁 —— 详情页大图本来就是横版。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from loguru import logger

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow 是必装依赖，兜底而已
    Image = None  # type: ignore[assignment]

# 海报的目标宽高比（宽 : 高）。日本 AV 的碟片封套是 2:3，
# Emby 的列表页也按这个比例排版
POSTER_RATIO = 2 / 3

# 横版图的判据：宽高比超过这个值就认为是双拼图，需要裁一半。
# 与 imgcrop.MIN_PANEL_RATIO 保持同一量级 —— 那边判「要不要偏移」，
# 这里判「要不要裁」，是同一件事的两面
MIN_WIDE_RATIO = 1.3

# 剧照最多存几张。详情页翻不动那么多，而每张都要下载
MAX_STILLS = 10

# 剧照目录名。Emby 与 Jellyfin 都认这个（Kodi 传下来的约定）
EXTRAFANART_DIR = "extrafanart"

# 小于这个字节数基本是错误页或占位图
MIN_IMAGE_BYTES = 512


@dataclass
class ImageSet:
    """一部影片要落盘的图片。值是「图片内容」而非 URL —— 下载由调用方
    负责（它有 httpx 客户端与代理配置），这里只管裁和写。"""
    poster: bytes = b""
    fanart: bytes = b""
    stills: list[bytes] = field(default_factory=list)
    # 封面的人像面，取自 Code.portrait_side。LEFT / RIGHT / 空
    portrait_side: str = ""


def _load(data: bytes):
    """把字节读成 PIL Image。读不出返回 None。"""
    if not data or len(data) < MIN_IMAGE_BYTES or Image is None:
        return None
    try:
        image = Image.open(BytesIO(data))
        image.load()
        return image
    except Exception as exc:
        logger.debug(f"[刮削] 图片解码失败: {exc}")
        return None


def crop_poster(data: bytes, portrait_side: str = "") -> bytes:
    """从封面裁出竖版海报。

    三种情况：
      本来就是竖版      原样返回，不动
      横版双拼          按 portrait_side 裁人像那半边
      横版但不知哪半边  裁右半边

    右半边是双拼封面的通行版式（左碟片右人像），portrait_side 为空时
    按它兜底比居中裁好 —— 居中会同时切掉人像和碟片，两边都不完整。
    这个兜底与 imgcrop.detect_portrait_side 的返回一致（它判定不出时
    也是给 RIGHT）。
    """
    image = _load(data)
    if image is None:
        return data

    try:
        width, height = image.size
        if not height or width / height < MIN_WIDE_RATIO:
            # 已经是竖版或接近方形，原样用
            return data

        target_width = int(height * POSTER_RATIO)
        if target_width <= 0 or target_width >= width:
            return data

        side = (portrait_side or "").upper()
        if side == "LEFT":
            left = 0
        else:
            # RIGHT 与空值都靠右裁
            left = width - target_width

        cropped = image.crop((left, 0, left + target_width, height))
        buffer = BytesIO()
        # 统一存 JPEG：Emby 对 webp 的支持因版本而异，而海报必须显示出来。
        # RGB 转换是必须的 —— webp/png 可能带 alpha，JPEG 不支持
        if cropped.mode not in ("RGB", "L"):
            cropped = cropped.convert("RGB")
        cropped.save(buffer, format="JPEG", quality=92, optimize=True)
        return buffer.getvalue()
    except Exception as exc:
        logger.warning(f"[刮削] 裁海报失败，改用原图: {exc}")
        return data
    finally:
        image.close()


def _write_atomic(path: Path, data: bytes) -> bool:
    """原子写入。Emby 可能正在扫这个目录，不能让它读到半张图。"""
    if not data:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning(f"[刮削] 写图片失败: {path} ({exc})")
        return False


def write_images(
    video_path: Path,
    images: ImageSet,
    overwrite: bool = True,
) -> dict[str, str]:
    """把一部影片的图片写到它旁边。

    返回 {类型: 文件名}，文件名是相对影片目录的，可直接填进 NFO 的
    <thumb>/<fanart>。没写成的类型不出现在返回里。

    命名带影片名前缀（ABS-001-poster.jpg）而不用目录级的 poster.jpg：
    一个目录里可能放着多部片（用户按演员归档时很常见），目录级图片会
    互相覆盖。带前缀则每部片各自一份，Emby 两种都认。

    分集影片每个分集各写一份 —— 这正是 issue #503 的修法。多占一点
    磁盘（几百 KB）换来每一集在 Emby 里都有图，值得。
    """
    stem = video_path.stem
    directory = video_path.parent
    written: dict[str, str] = {}

    targets = [
        ("poster", f"{stem}-poster.jpg", images.poster),
        ("fanart", f"{stem}-fanart.jpg", images.fanart),
        # thumb 与 fanart 同源。部分皮肤只读 thumb，多写一份省得用户困惑
        ("thumb", f"{stem}-thumb.jpg", images.fanart),
    ]

    for kind, name, data in targets:
        if not data:
            continue
        path = directory / name
        if path.exists() and not overwrite:
            written[kind] = name
            continue
        if _write_atomic(path, data):
            written[kind] = name

    if images.stills:
        still_dir = directory / EXTRAFANART_DIR
        for index, data in enumerate(images.stills[:MAX_STILLS], start=1):
            path = still_dir / f"{index}.jpg"
            if path.exists() and not overwrite:
                continue
            _write_atomic(path, data)
        written["stills"] = EXTRAFANART_DIR

    return written


def build_image_set(
    cover: bytes,
    stills: list[bytes] | None = None,
    portrait_side: str = "",
) -> ImageSet:
    """从下载好的封面与剧照组装 ImageSet。

    海报由封面裁出，fanart 用封面原图 —— 源站通常只给一张封面，
    横版那张既是背景也是海报的来源。
    """
    return ImageSet(
        poster=crop_poster(cover, portrait_side) if cover else b"",
        fanart=cover,
        stills=[s for s in (stills or []) if s and len(s) >= MIN_IMAGE_BYTES],
        portrait_side=portrait_side,
    )
