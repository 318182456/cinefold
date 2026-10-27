"""图片本地缓存。

图源在墙外且带防盗链，每次回源都要走代理重新握手，列表页几十张图会很慢。
缓存目录沿用旧版布局 pics/<番号>/banner.jpg，这样历史数据不用迁移就能直接命中。

番号是业务主键，拿它当缓存键比拿 URL 哈希稳：源站换 CDN 域名时缓存不会全部失效。
没有番号的图（演员头像等）退回按 URL 摘要存到 _misc 下。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from loguru import logger

from app.utils import get_image_suffix_from_url, get_true_code

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
PIC_DIR = DATA_DIR / "pics"

# 无番号的图片统一放这里，避免和番号目录混在一层
MISC_DIR = "_misc"

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# 小于这个尺寸基本是错误页或占位图，不值得当成缓存写下去
MIN_IMAGE_BYTES = 512
_LOCKS = [threading.RLock() for _ in range(32)]
_ENCODERS = threading.BoundedSemaphore(2)


def file_lock(path):
    return _LOCKS[int(hashlib.sha256(str(path.with_suffix('')).encode()).hexdigest()[:8], 16) % len(_LOCKS)]


def important_condition():
    from sqlalchemy import exists, select, or_
    from app.database.models import Code, CollectionItem, LibraryPreference, MediaLink
    return or_(Code.status.in_((1, 2, 3, 4)),
        exists(select(1).where(LibraryPreference.code == Code.code, LibraryPreference.favorite.is_(True))),
        exists(select(1).where(CollectionItem.code == Code.code)),
        exists(select(1).where(MediaLink.code == Code.code)))


def optimized(content: bytes):
    with _ENCODERS:
        return _optimized(content)


def _optimized(content: bytes):
    """Convert still images once, only when WebP is smaller; preserve alpha/animation."""
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.image_cache_webp:
        return content, None
    try:
        import io
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(content)) as src:
            if src.format not in ('JPEG', 'PNG') or getattr(src, 'is_animated', False):
                return content, None
            if src.width * src.height > 16_000_000:
                return content, None
            src.load()
            with ImageOps.exif_transpose(src) as image:
                mode = 'RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB'
                out = image if image.mode == mode else image.convert(mode)
                try:
                    buf = io.BytesIO()
                    out.save(buf, format='WEBP', quality=max(60, min(95, settings.image_cache_quality)), method=4,
                             **({'icc_profile': src.info['icc_profile']} if src.info.get('icc_profile') else {}))
                    data = buf.getvalue()
                finally:
                    if out is not image:
                        out.close()
        if MIN_IMAGE_BYTES <= len(data) < len(content):
            return data, '.webp'
    except Exception as exc:
        logger.debug(f'图片优化跳过: {type(exc).__name__}')
    return content, None


def atomic_write(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+target.name, suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def touch(path):
    try:
        stat = path.stat()
        if time.time() - stat.st_atime > 3600:
            os.utime(path, (time.time(), stat.st_mtime))
    except OSError:
        pass


def _safe_code(code: str) -> str:
    """番号转成安全的目录名，非法字符一律丢弃。

    get_true_code 已经做了格式标准化，这里只兜底防目录穿越。
    """
    normalized = get_true_code(code or "")
    if not normalized:
        return ""
    return re.sub(r"[^A-Z0-9\-]", "", normalized)


def cache_path(url: str, code: str = "", kind: str = "banner") -> Path | None:
    """算出某张图的缓存落盘位置。

    kind 为 banner / poster 时映射到 banner.jpg，与旧版命名保持一致；
    still_photo_N 之类的名字直接透传。
    """
    if not url:
        return None

    suffix = get_image_suffix_from_url(url)
    safe = _safe_code(code)

    if safe:
        # 旧版封面固定叫 banner.jpg，poster 与 banner 在本项目里同源，共用一份
        name = "banner" if kind in ("banner", "poster") else kind
        if kind == 'still':
            name = 'still_' + hashlib.sha256(url.encode()).hexdigest()[:24]
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', name):
            return None
        return PIC_DIR / safe / f"{name}{suffix}"

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    # 按摘要前两位分桶，避免单目录塞进上万个文件
    return PIC_DIR / MISC_DIR / digest[:2] / f"{digest}{suffix}"


def find_cached(url: str, code: str = "", kind: str = "banner") -> Path | None:
    """查已缓存的图片，命中返回路径。

    旧数据里同一张封面可能是 .jpg 也可能是 .webp，所以按扩展名逐个探测，
    而不是只认当前 URL 推断出的那一个。
    """
    target = cache_path(url, code, kind)
    if target is None:
        return None

    if target.is_file() and target.stat().st_size >= MIN_IMAGE_BYTES:
        return target

    for ext in CONTENT_TYPES:
        if ext == target.suffix:
            continue
        candidate = target.with_suffix(ext)
        if candidate.is_file() and candidate.stat().st_size >= MIN_IMAGE_BYTES:
            return candidate
    return None


def store(content: bytes, url: str, code: str = "", kind: str = "banner") -> Path | None:
    """把回源拿到的图片写入缓存，返回落盘路径。

    写临时文件再 rename，避免并发请求同一张图时读到写了一半的内容。
    保留完整画面，较小的 WebP 可替代原编码：双拼封面只在显示时偏到人像那半边（前端按
    code.portrait_side 设 object-position），点开灯箱还要看完整原图。
    """
    if not content or len(content) < MIN_IMAGE_BYTES:
        return None

    target = cache_path(url, code, kind)
    if target is None:
        return None

    try:
        content, suffix = optimized(content)
        if suffix is None:
            if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
                suffix = '.webp'
            elif content.startswith(b'\x89PNG\r\n\x1a\n'):
                suffix = '.png'
            elif content.startswith((b'GIF87a', b'GIF89a')):
                suffix = '.gif'
            elif content.startswith(b'\xff\xd8'):
                suffix = '.jpg'
        with file_lock(target):
            if suffix:
                target = target.with_suffix(suffix)
            atomic_write(target, content)
        return target
    except OSError as exc:
        logger.warning(f"写入图片缓存失败 {target}: {exc}")
        return None


def drop_cached(url: str, code: str = "", kind: str = "banner") -> int:
    """删掉某张图的全部缓存副本，返回删除数量。

    要跨扩展名删干净：裁剪会把 .webp 重编码成 .jpg，只删当前 URL 推断出的
    那一个后缀，另一个后缀的旧文件会被 find_cached 继续命中，重抓就失效了。
    """
    target = cache_path(url, code, kind)
    if target is None:
        return 0

    removed = 0
    for ext in CONTENT_TYPES:
        candidate = target.with_suffix(ext)
        try:
            candidate.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(f"删除图片缓存失败 {candidate}: {exc}")
    return removed


def resolve_relative(relative: str) -> Path | None:
    """把库里存的相对路径解析成 pics 下的绝对路径。

    这个路径来自 HTTP 参数，必须确认解析结果仍在 pics 目录内，
    否则 ../../ 就能把容器里任意文件读出去。
    """
    raw = (relative or "").strip().lstrip("/\\")
    if not raw:
        return None

    suffix = Path(raw).suffix.lower()
    if suffix not in CONTENT_TYPES:
        return None

    try:
        base = PIC_DIR.resolve()
        target = (base / raw).resolve()
    except OSError:
        return None

    if target != base and base not in target.parents:
        logger.warning(f"拒绝越界的图片路径: {relative[:80]}")
        return None
    if not target.is_file():
        # Keep old database/local URLs usable after format-only migration.
        target = next((target.with_suffix(ext) for ext in CONTENT_TYPES if target.with_suffix(ext).is_file()), None)
        if target is None or not target.resolve().is_relative_to(base):
            return None
    return target


def thumbnail(path: Path, width=480):
    """A bounded-size list image; lightboxes continue to request the full image."""
    target = path.with_name(path.stem + '.thumb.webp')
    try:
        with file_lock(path), _ENCODERS:
            if target.is_file() and target.stat().st_mtime_ns >= path.stat().st_mtime_ns:
                return target
            import io
            from PIL import Image, ImageOps
            with Image.open(path) as src:
                if getattr(src, 'is_animated', False):
                    return path
                if src.format == 'JPEG':
                    src.draft('RGB', (width*2, width*2))
                if src.width*src.height > 16_000_000:
                    return path
                with ImageOps.exif_transpose(src) as image:
                    image.thumbnail((width, width), Image.Resampling.LANCZOS)
                    with image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB') as out:
                        buf = io.BytesIO()
                        out.save(buf, format='WEBP', quality=80)
                        atomic_write(target, buf.getvalue())
        return target
    except Exception:
        return path


def relative_of(path: Path) -> str:
    """绝对缓存路径 → 库里存的相对路径（ABC-123/banner.jpg）。"""
    try:
        return path.resolve().relative_to(PIC_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "image/jpeg")


def etag_for(path: Path) -> str:
    """用 大小-修改时间 生成弱 ETag。

    图片一旦落盘就不再变动，没必要为了强校验去读全文件算哈希。
    """
    stat = path.stat()
    return f'W/"{stat.st_size:x}-{int(stat.st_mtime):x}"'


# 统计时最多探测多少个目录。pics 挂在网络存储上时逐目录 stat 很慢，
# 数量级足够说明问题，没必要为了精确值让接口卡住。
STATS_PROBE_LIMIT = 300


def stats() -> dict:
    """缓存概览，给管理接口用。

    只数番号目录，封面存在性抽样探测 —— pics 下有上万个目录时，
    逐个 stat 在网络存储上要跑很久。
    """
    if not PIC_DIR.is_dir():
        return {"exists": False, "codes": 0, "banners": 0, "dir": str(PIC_DIR)}

    codes = 0
    probed = with_banner = 0
    try:
        with os.scandir(PIC_DIR) as it:
            for entry in it:
                if entry.name == MISC_DIR or not entry.is_dir():
                    continue
                codes += 1
                # 只对前若干个目录探测封面，后面的按比例估算
                if probed < STATS_PROBE_LIMIT:
                    probed += 1
                    if any(
                        Path(entry.path, f"banner{ext}").is_file()
                        for ext in (".jpg", ".webp", ".png")
                    ):
                        with_banner += 1
    except OSError as exc:
        logger.warning(f"统计图片缓存失败: {exc}")

    return {
        "exists": True,
        "codes": codes,
        "probed": probed,
        "with_banner": with_banner,
        "sampled": codes > probed,
        "dir": str(PIC_DIR),
    }
