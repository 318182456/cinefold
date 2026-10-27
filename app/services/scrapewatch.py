"""自建刮削下载目录补漏。源文件始终保留，重启后通过关联与产物检查去重。"""
from __future__ import annotations

from app.services.scrapemaintenance import guarded as maintenance_guarded

import os
import re
import threading
import time
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Code, CodeStatus, MediaLink, ScrapeAttempt
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


def _complete(source: Path, code: str, links: list[str], require_images: bool = True) -> bool:
    for raw in links:
        target = Path(raw)
        try:
            if not target.samefile(source):
                from app.services.mediaduplicate import verified_existing
                from app.services.duplicatepolicy import retained
                # Explicitly keeping the old video is a completed disposition,
                # even when its artwork needs a separate metadata repair.
                if retained(source, target):
                    return True
                if not verified_existing(source, target):
                    continue
            if not local.read(target.with_suffix(".nfo"), code).get("title"):
                from app.modules.scrape.merge import product_alias
                alias = product_alias(code)
                if not alias or not local.read(target.with_suffix('.nfo'), alias).get('title'):
                    continue
            from app.services.artifactvalidation import valid_image
            if not require_images or all(valid_image(target.with_name(f"{target.stem}-{kind}.jpg")) for kind in ("poster", "fanart", "thumb")):
                return True
        except OSError:
            continue
    return False


def scan() -> int:
    from app.services.scrapemaintenance import busy, MaintenanceBusy
    if busy():
        return 0
    settings = get_settings()
    if settings.scrape_mode != "builtin" or not settings.scrape_source_dirs.strip():
        return 0
    if not _LOCK.acquire(blocking=False):
        return 0
    try:
        return _scan(settings)
    except MaintenanceBusy:
        return 0
    finally:
        _LOCK.release()


@maintenance_guarded
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
        from app.services.mediaduplicate import source_aliases
        for source, link in source_aliases(session):
            if link not in registered.setdefault(source, []):
                registered[source].append(link)
        from app.services.duplicatepolicy import links as decision_links
        for source, link in decision_links(session):
            if link not in registered.setdefault(source, []):
                registered[source].append(link)
        downloading = set(session.scalars(select(Code.code).where(Code.status == CodeStatus.DOWNLOADING)))
        attempts = list(session.scalars(select(ScrapeAttempt)))
        overrides = {a.source_key: a.code for a in attempts if a.code}
        failed = {a.source_key for a in attempts if a.status == 'failed'}

    now = time.monotonic()
    wall = time.time()
    seen, candidates = set(), []
    for root in roots:
        for folder, dirs, files in os.walk(root, followlinks=False):
            # 避开下载器的临时目录，也不跟随目录符号链接。
            dirs[:] = [d for d in dirs if d not in {"@eaDir", ".unwanted", "#recycle"} and not Path(folder, d).is_symlink()]
            for filename in files:
                path = Path(folder, filename)
                if path.is_symlink() or not is_adoptable_video(path) or mediafile.is_advertisement(path.stem):
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
    from app.services.scrapesources import group_entries, key
    entries = []
    from app.services.sourceidentity import SourceIdentityResolver
    identity_resolver = SourceIdentityResolver()
    for path in candidates:
        code = overrides.get(key(path)) or scrape.inspect_source(path, identity_resolver=identity_resolver).code
        if code:
            entries.append({'path': str(path), 'code': code})
    groups = group_entries(entries)
    selected = {}
    for code, items in sorted(groups.items(), key=lambda pair: max(_RETRY.get(str(i.path), 0) for i in pair[1])):
        if code in downloading:
            continue
        if all(_RETRY.get(str(i.path), 0) > now for i in items):
            continue
        if all(i.trailer or key(i.path) not in failed and _complete(i.path, code, registered.get(str(i.path), []), settings.scrape_require_images) for i in items):
            continue
        if len(selected) >= max(1, min(100, settings.scrape_scan_limit)):
            break
        selected[code] = items
    if not selected:
        return 0
    from app.services.scrapehistory import AutomaticTask
    successful = 0
    with AutomaticTask(selected, settings, 'directory') as task:
        for code, items in selected.items():
            for item in items:
                _RETRY[str(item.path)] = now + max(600, settings.scrape_scan_interval * 60)
            existing = {str(i.path): Path(registered[str(i.path)][0]) for i in items if registered.get(str(i.path))}
            result = task.process(code, items, **({'existing_targets':existing} if existing else {}))
            successful += int(result.ok)
            if not result.ok:
                logger.warning(f"[{code}] 自动刮削未完成，将在后续扫描重试: {result.error}")
    logger.info(f"[自动刮削] 本轮处理 {len(selected)} 个番号，成功 {successful} 个")
    return successful
