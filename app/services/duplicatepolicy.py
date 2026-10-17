"""Video duplicate preferences, persistent decisions and atomic replacement."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import ScrapeDuplicateDecision, MediaLink, MediaMerge
from app.database.session import session_scope

POLICIES = {'versions', 'overwrite', 'highest_quality', 'keep_existing'}


def policy():
    value = getattr(get_settings(), 'scrape_duplicate_policy', 'versions')
    return value if value in POLICIES else 'versions'


def signature(path):
    value = Path(path).stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def remember(source, target, choice, target_signature=None):
    from app.services.scrapeinventory import key
    source, target = Path(source), Path(target)
    if source.is_symlink() or target.is_symlink():
        raise ValueError('重复处理路径不能是符号链接')
    with session_scope() as session:
        session.merge(ScrapeDuplicateDecision(source_key=key(source), source_path=str(source), target_path=str(target),
            source_signature=signature(source), target_signature=target_signature or signature(target),
            policy=choice, updated_at=datetime.now()))


def retained(source, target):
    from app.services.scrapeinventory import key
    try:
        with session_scope() as session:
            row = session.get(ScrapeDuplicateDecision, key(source))
            return bool(row and row.target_path == str(target) and row.policy in (policy(), 'superseded')
                        and not Path(source).is_symlink() and not Path(target).is_symlink()
                        and row.source_signature == signature(source) and row.target_signature == signature(target))
    except OSError:
        return False


def links(session, source=None):
    query = select(ScrapeDuplicateDecision)
    if source is not None:
        query = query.where(ScrapeDuplicateDecision.source_path == str(source))
    return [(row.source_path, row.target_path) for row in session.scalars(query)]


def disposition(source, targets):
    for target in targets:
        try:
            if not Path(target).samefile(source) and retained(source, target):
                return 'kept_existing'
        except OSError:
            pass
    return 'complete'


@lru_cache(maxsize=256)
def _probe(raw, stamp):
    executable = shutil.which('ffprobe')
    if not executable:
        return None
    try:
        response = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,bit_rate:format=duration,bit_rate', '-of', 'json', raw],
            capture_output=True, timeout=20, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        value = json.loads(response.stdout)
        stream = value['streams'][0]
        width, height = int(stream['width']), int(stream['height'])
        duration = float(value.get('format', {}).get('duration') or 0)
        bitrate = float(stream.get('bit_rate') or value.get('format', {}).get('bit_rate') or 0)
        if not bitrate and duration:
            bitrate = stamp[2] * 8 / duration
        if not width or not height or not duration or tuple(signature(raw)) != stamp:
            return None
        return width * height, bitrate, duration
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        return None


def quality(path):
    try:
        return _probe(str(path), tuple(signature(path)))
    except OSError:
        return None


def prefer(source, targets, choice):
    """Unknown quality or different cut lengths always retain separate versions."""
    if choice == 'keep_existing':
        return 'keep', targets[0], '按设置保留已有影片'
    if choice == 'overwrite':
        return 'replace', targets[0], '按设置覆盖已有播放文件'
    if choice == 'highest_quality':
        incoming = quality(source)
        existing = [(path, quality(path)) for path in targets]
        if incoming and all(value for _, value in existing):
            if all(abs(value[2] - incoming[2]) <= max(10, incoming[2] * 0.05) for _, value in existing):
                target, best = max(existing, key=lambda row: row[1][:2])
                return ('replace' if incoming[:2] > best[:2] else 'keep'), target, '按实际分辨率、码率保留较高清版本'
        return 'variant', targets[0], '无法可靠比较清晰度或片长不同，保留两个版本'
    return 'variant', targets[0], '按设置保留两个版本'


def replace_video(source, target, expected):
    source, target = Path(source), Path(target)
    temporary = target.with_name('.cinefold-replace-' + uuid.uuid4().hex + '.tmp')
    before = signature(source)
    try:
        if source.is_symlink() or target.is_symlink() or signature(target) != expected:
            raise ValueError('重复处理期间目标文件变化，请重试')
        os.link(source, temporary)
        if signature(source) != before or signature(target) != expected:
            raise ValueError('替换前文件变化，保留原文件')
        # Persist the outgoing source decision first to prevent later scans from
        # replacing the newly selected video with the old download again.
        with session_scope() as session:
            old = session.get(MediaLink, str(target))
            old_sources = {old.source_path} if old else set()
            old_sources.update(session.scalars(select(MediaMerge.source_path).where(MediaMerge.keep_path == str(target))))
            old_sources.update(session.scalars(select(ScrapeDuplicateDecision.source_path).where(ScrapeDuplicateDecision.target_path == str(target))))
        for old_source in old_sources:
            if old_source and old_source != str(target) and Path(old_source).is_file() and not Path(old_source).samefile(source):
                remember(old_source, target, 'superseded', target_signature=before)
        if source.is_symlink() or target.is_symlink() or signature(target) != expected or signature(source) != before:
            raise ValueError('替换提交前文件变化，原视频保留')
        from app.services.scrapemaintenance import protect
        settings = get_settings()
        output = settings.medialink_scrape_dir or settings.medialink_library_path
        if not output or not target.resolve().is_relative_to(Path(output).resolve()):
            raise ValueError('替换目标不在播放输出目录内')
        # Emby can report a delayed deletion for this same path after replacement.
        # Reuse the rebuild deletion guard so it cannot remove the new download.
        protect(output)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
