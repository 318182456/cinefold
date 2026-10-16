"""自建刮削下载目录补漏。源文件始终保留，重启后通过关联与产物检查去重。"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Code, CodeStatus, MediaLink
from app.database.session import session_scope
from app.modules.scrape import local
from app.services import scrape
from app.services.medialink import is_adoptable_video
from app.utils import mediafile

_LOCK = threading.Lock()
# path -> (size, mtime_ns, 首次看到该状态的时间)。连续两轮稳定才处理。
_OBSERVED: dict[str, tuple[int, int, float]] = {}
# path -> 下次可重试的 monotonic 时间，失败不阻塞后面的新影片。
_RETRY: dict[str, float] = {}


def _complete(source: Path, code: str, links: list[str]) -> bool:
    for raw in links:
        target = Path(raw)
        try:
            if not target.samefile(source):
                continue
            if not local.read(target.with_suffix(".nfo"), code).get("title"):
                continue
            if all(target.with_name(f"{target.stem}-{kind}.jpg").is_file() for kind in ("poster", "fanart", "thumb")):
                return True
        except OSError:
            continue
    return False


def scan() -> int:
    settings = get_settings()
    if settings.scrape_mode != "builtin" or not settings.scrape_source_dirs.strip():
        return 0
    if not _LOCK.acquire(blocking=False):
        return 0
    try:
        return _scan(settings)
    finally:
        _LOCK.release()


def _scan(settings) -> int:
    output = settings.medialink_scrape_dir or settings.medialink_library_path
    if not output or not Path(output).is_absolute():
        logger.warning("[自动刮削] 请先设置绝对路径的刮削输出目录")
        return 0
    output = Path(output).resolve()
    roots = []
    for raw in re.split(r"[,\r\n]+", settings.scrape_source_dirs):
        if not raw.strip():
            continue
        root = Path(raw.strip())
        if not root.is_absolute() or not root.is_dir():
            logger.warning(f"[自动刮削] 源目录不可访问或不是绝对路径: {root}")
            continue
        root = root.resolve()
        if root.is_relative_to(output) or output.is_relative_to(root):
            logger.warning(f"[自动刮削] 源目录与输出目录不能重叠: {root}")
            continue
        if root not in roots:
            roots.append(root)
    if not roots:
        return 0

    with session_scope() as session:
        registered: dict[str, list[str]] = {}
        for source, link in session.execute(select(MediaLink.source_path, MediaLink.link_path)):
            registered.setdefault(source, []).append(link)
        downloading = set(session.scalars(select(Code.code).where(Code.status == CodeStatus.DOWNLOADING)))

    now = time.monotonic()
    wall = time.time()
    seen, candidates = set(), []
    for root in roots:
        for folder, dirs, files in os.walk(root, followlinks=False):
            # 避开下载器的临时目录，也不跟随目录符号链接。
            dirs[:] = [d for d in dirs if d not in {"@eaDir", ".unwanted", "#recycle"} and not Path(folder, d).is_symlink()]
            for filename in files:
                path = Path(folder, filename)
                if path.is_symlink() or not is_adoptable_video(path):
                    continue
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                fingerprint = (stat.st_size, stat.st_mtime_ns)
                previous = _OBSERVED.get(key)
                if previous is None or previous[:2] != fingerprint:
                    _OBSERVED[key] = (*fingerprint, now)
                    _RETRY.pop(key, None)
                    continue
                if stat.st_size == 0 or wall - stat.st_mtime < 120 or now - previous[2] < 120:
                    continue
                candidates.append(path)

    for key in list(_OBSERVED):
        if key not in seen:
            _OBSERVED.pop(key, None)
            _RETRY.pop(key, None)

    # 先分组再跳过已入库分集，保持分集编号与冲突消解稳定。
    groups = mediafile.group_parts(candidates)
    groups.pop("", None)
    processed = successful = 0
    for code, items in sorted(groups.items(), key=lambda pair: max(_RETRY.get(str(i.path), 0) for i in pair[1])):
        if code in downloading:
            continue
        if all(_RETRY.get(str(i.path), 0) > now for i in items):
            continue
        if all(i.trailer or _complete(i.path, code, registered.get(str(i.path), [])) for i in items):
            continue
        if processed >= max(1, min(100, settings.scrape_scan_limit)):
            break
        processed += 1
        for item in items:
            _RETRY[str(item.path)] = now + max(600, settings.scrape_scan_interval * 60)
        try:
            result = scrape.scrape_group(code, items)
            if result.ok:
                successful += 1
            else:
                logger.warning(f"[{code}] 自动刮削未完成，将在后续扫描重试: {result.error}")
        except Exception:
            logger.exception(f"[{code}] 自动刮削失败，将在后续扫描重试")
    if processed:
        logger.info(f"[自动刮削] 本轮处理 {processed} 个番号，成功 {successful} 个")
    return successful
