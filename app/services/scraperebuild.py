"""Preview and rebuild a library without deleting downloads or old library data.

Prefer renaming the old tree outside the playback library. Across mounts, only
non-video artifacts are copied; verified videos remain recoverable from their
independent download sources. No download source is removed by this service.
"""
from __future__ import annotations

import copy
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import threading
from threading import Thread
import time
import uuid

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Code, CodeStatus, MediaLink, ScrapeAttempt, ScrapeSourceRoot, ScrapeSourceVideo
from app.database.session import DATA_DIR, session_scope
from app.modules.scrape import local, merge
from app.services.filehash import stable_digest
from app.utils import get_true_code, mediafile

_LOCK = threading.RLock()
_SECRET = secrets.token_bytes(32)
_PLAN = None
_STATE = {"running": False, "phase": "idle", "done": 0, "total": 0, "ok": 0,
          "error": "", "token": "", "plan": None, "quarantine_dir": "", "results": []}
_TOKEN_LIFETIME = 3600
_STATE_FILE = DATA_DIR / 'scrape-rebuild-state.json'
_PROGRESS_SAVED = 0.0
_GENERATED_SIDECARS = {'.nfo', '.json', '.jpg', '.jpeg', '.png', '.webp', '.tbn'}
_CLEANUP_LOCAL = threading.local()


def _progress_start(stage, label, total=0, unit='文件', bytes_total=0):
    now = time.time()
    with _LOCK:
        steps = _STATE.setdefault('progress_steps', {})
        previous = _STATE.get('progress')
        if previous and previous.get('stage') != stage:
            previous['finished_at'] = now
        value = dict(stage=stage, label=label, done=0, total=total, unit=unit,
                     bytes_done=0, bytes_total=bytes_total, current='', current_bytes=0,
                     current_total_bytes=0, started_at=now, updated_at=now)
        steps[stage] = value
        _STATE['progress'] = value
        _persist_state()


def _progress(**values):
    """Frequent in-memory progress; throttle recovery-journal disk writes."""
    global _PROGRESS_SAVED
    with _LOCK:
        current = _STATE.get('progress')
        if current is None:
            return
        current.update(values, updated_at=time.time())
        if time.monotonic() - _PROGRESS_SAVED >= 2:
            _persist_state()
            _PROGRESS_SAVED = time.monotonic()


def _progress_hash(path):
    processed = 0
    _progress(current=str(path), current_bytes=0, current_total_bytes=Path(path).stat().st_size)
    def advance(size):
        nonlocal processed
        processed += size
        _progress(current_bytes=processed)
    result = stable_digest(path, progress=advance)
    _progress(current_bytes=Path(path).stat().st_size)
    return result


def _maintenance():
    from app.services import scrapemaintenance
    return scrapemaintenance


def state():
    with _LOCK:
        if _STATE['phase'] == 'idle' and _STATE_FILE.is_file():
            try:
                saved = json.loads(_STATE_FILE.read_text(encoding='utf-8'))
                _STATE.update({key: value for key, value in saved.items() if key in _STATE or key in ('job_id', 'output_dir', 'quarantine_mode', 'backup_bytes', 'progress', 'progress_steps', 'started_at', 'finished_at', 'cleanup_note_count', 'cleanup_notes', 'retained_file_count')})
                if saved.get('running'):
                    _STATE.update(running=False, phase='interrupted', finished_at=time.time(),
                                  error='进程重启，重建中断；请核对隔离目录和恢复清单。跨盘模式仅备份非视频文件，视频从下载源恢复')
            except (OSError, ValueError, TypeError):
                _STATE.update(error='无法读取上次重建恢复记录，请检查数据目录')
        value = copy.deepcopy(_STATE)
    if not value.get('output_dir'):
        settings = get_settings()
        value['output_dir'] = settings.medialink_scrape_dir or settings.medialink_library_path or ''
    value.update(_maintenance().protection())
    now = time.time()
    if value.get('started_at'):
        value['elapsed_seconds'] = max(0, (value.get('finished_at') or now) - value['started_at'])
    progress = value.get('progress')
    if progress:
        progress['elapsed_seconds'] = max(0, (progress.get('finished_at') or value.get('finished_at') or now) - progress['started_at'])
        progress['idle_seconds'] = max(0, (value.get('finished_at') or now) - progress['updated_at'])
    from app.services.scraperecovery import info
    value['recovery'] = info(sys.modules[__name__], value['running'])
    return value


current = state


def active_job():
    """DB-only inventory queries must not read the recovery journal or disk."""
    with _LOCK:
        return {key: _STATE.get(key) for key in ('running', 'phase', 'job_id')}


def _update(**values):
    with _LOCK:
        _STATE.update(values)
        _persist_state()


def _persist_state():
    if not _STATE.get('quarantine_dir'):
        return
    keys = ('running', 'phase', 'done', 'total', 'ok', 'error', 'quarantine_dir', 'job_id', 'output_dir', 'quarantine_mode', 'backup_bytes',
            'progress', 'progress_steps', 'started_at', 'finished_at', 'cleanup_note_count', 'cleanup_notes', 'retained_file_count')
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_manifest(_STATE_FILE, {key: _STATE.get(key) for key in keys})
    if (Path(_STATE['quarantine_dir']) / 'manifest.json').is_file():
        from app.services.scraperecovery import reference_path
        _write_manifest(reference_path(sys.modules[__name__]),
                        {key: _STATE.get(key) for key in ('job_id', 'quarantine_dir', 'phase', 'running', 'done', 'total', 'ok')})


def _stamp(path):
    value = Path(path).stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _plain(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _unsafe_links(path):
    for item in [path, *path.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return True
    return False


def _safe_path(raw, *, output=False):
    path = Path(raw)
    if not raw or not path.is_absolute() or '..' in path.parts or _unsafe_links(path):
        raise ValueError(f"路径必须是绝对路径且不能包含符号链接: {raw}")
    path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError(f"不能使用磁盘根目录: {path}")
    # System trees are never acceptable library/download targets. Temporary
    # subdirectories remain usable for tests and normal temporary downloads.
    forbidden = ('/bin', '/sbin', '/usr', '/etc', '/proc', '/sys', '/dev', '/boot')
    if any(path == Path(root) or path.is_relative_to(Path(root)) for root in forbidden):
        raise ValueError(f"不能使用系统目录: {path}")
    if os.name == 'nt' and any(part.casefold() in {'windows', 'program files', 'program files (x86)', 'programdata', '$recycle.bin', 'system volume information'} for part in path.parts):
        raise ValueError(f"不能使用系统目录: {path}")
    if output and str(path) in {'/home', '/root', '/mnt', '/media', '/tmp', '/var', '/opt'}:
        raise ValueError(f"不能清空系统公共目录: {path}")
    if output:
        from app.database.session import DATA_DIR
        for protected in (Path(DATA_DIR).resolve(), Path(__file__).resolve().parents[2]):
            if path == protected or protected.is_relative_to(path):
                raise ValueError(f'输出目录不能包含应用程序或应用数据库: {path}')
    return path


def _tree(output):
    manifest, videos, blockers = [], [], []
    if not output.exists():
        if not output.parent.is_dir():
            raise ValueError('输出目录的父目录不可访问')
        return manifest, videos, blockers
    if not output.is_dir():
        raise ValueError('输出路径不是目录')
    for folder, dirs, files in os.walk(output, followlinks=False, onerror=lambda exc: (_ for _ in ()).throw(exc)):
        for name in sorted(dirs + files):
            path = Path(folder, name)
            if _unsafe_links(path):
                blockers.append(f'输出目录含符号链接或重解析点: {path}')
                dirs[:] = [entry for entry in dirs if entry != name]
                continue
            mode = path.stat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                blockers.append(f'输出目录包含特殊文件: {path}')
                continue
            manifest.append([str(path.relative_to(output)), _stamp(path), 'dir' if path.is_dir() else 'file'])
            if path.is_file() and path.suffix.lower() in local.VIDEO_SUFFIXES:
                videos.append(path)
    return sorted(manifest), sorted(videos), blockers


def _independent_hardlink(source, video):
    """Bind-mount aliases can be samefile without being two directory entries."""
    if os.path.normcase(source.name) == os.path.normcase(video.name) and source.parent.samefile(video.parent):
        return False
    return source.stat().st_nlink >= 2 and video.stat().st_nlink >= 2


def _device(path):
    return path.stat().st_dev


def _same_rename_mount(left, right):
    """Linux bind mounts may have equal devices while rename returns EXDEV."""
    if os.name != 'posix':
        return True
    try:
        entries = []
        for line in Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines():
            fields = line.split()
            mount = Path(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), fields[4]))
            entries.append((mount, fields[0]))
        def mount_id(path):
            matches = [(len(mount.parts), identity) for mount, identity in entries if path == mount or path.is_relative_to(mount)]
            return max(matches)[1] if matches else None
        return mount_id(left) == mount_id(right)
    except (OSError, ValueError, IndexError):
        # If Linux mount identities cannot be established, prefer copying the
        # small artifacts rather than discovering EXDEV after the first move.
        return False


def _quarantine_location(output, settings):
    """Prefer a rename outside the entire playback root; otherwise back up sidecars."""
    playback = _safe_path(settings.medialink_library_path) if settings.medialink_library_path else output
    parent = playback.parent if output.is_relative_to(playback) else output.parent
    device = _device(output if output.exists() else output.parent)
    if not _unsafe_links(parent) and parent.is_dir() and _device(parent) == device and _same_rename_mount(output, parent):
        return 'rename', parent
    parent = _safe_path(str(Path(DATA_DIR) / 'rebuild-quarantine'))
    if parent == playback or parent.is_relative_to(playback) or parent == output or parent.is_relative_to(output):
        raise ValueError('应用备份目录必须在整个播放目录之外，请检查DATA_DIR位置')
    ancestor = parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir() or _unsafe_links(ancestor):
        raise ValueError('应用数据目录中的备份位置不可访问')
    return 'copy_sidecars', parent


def _free_space(parent):
    while not parent.exists():
        parent = parent.parent
    return shutil.disk_usage(parent).free


def _collect(missing_policy='block'):
    """Read-only plan. No downloader calls and no discovery outside known paths."""
    settings = get_settings()
    output = _safe_path(settings.medialink_scrape_dir or settings.medialink_library_path, output=True)
    quarantine_mode, quarantine_parent = _quarantine_location(output, settings)
    if missing_policy not in ('block', 'keep', 'discard'):
        raise ValueError('未知缺失源处理方式')
    if missing_policy != 'block':
        quarantine_mode = 'copy_sidecars'
    with session_scope() as session:
        roots = [_plain(row) for row in session.scalars(select(ScrapeSourceRoot))]
        inventory = [_plain(row) for row in session.scalars(select(ScrapeSourceVideo))]
        links = [_plain(row) for row in session.scalars(select(MediaLink))]
        attempts = [_plain(row) for row in session.scalars(select(ScrapeAttempt))]
        codes = [_plain(row) for row in session.scalars(select(Code))]
    from app.services import scrapeinventory
    configured = scrapeinventory.configured()
    allowed, warnings, blockers = [], [], []
    for raw in dict.fromkeys([*configured, *(row['path'] for row in roots)]):
        root = _safe_path(raw)
        if root == output or root.is_relative_to(output) or output.is_relative_to(root):
            raise ValueError(f'源目录与输出目录不能重叠: {root}')
        if root.is_dir():
            allowed.append(root)
        else:
            warnings.append(f'源目录不可访问: {root}')
    output_manifest, old_videos, tree_blockers = _tree(output)
    blockers.extend(tree_blockers)
    backup_bytes = sum(stamp[2] for raw, stamp, kind in output_manifest
                       if kind == 'file' and Path(raw).suffix.lower() not in local.VIDEO_SUFFIXES) if quarantine_mode == 'copy_sidecars' else 0
    if quarantine_mode == 'copy_sidecars':
        warnings.append('保留源视频，不额外复制大视频；旧图片、NFO、字幕等完整备份后，移除已验证的播放文件，可按恢复清单从下载源重建旧视频链接')
        if _free_space(quarantine_parent) < backup_bytes:
            blockers.append(f'非视频文件备份空间不足，需要 {backup_bytes} 字节')
    overrides = {row['source_key']: row['code'] for row in attempts if row['code']}
    downloading = {row['code'] for row in codes if row['status'] == CodeStatus.DOWNLOADING}
    candidates = {}
    for row in inventory:
        raw = row['path']
        code = overrides.get(row['source_key']) or row['detected_code']
        candidates[raw] = {'path': raw, 'code': code, 'inventory': row, 'origin': '下载源清单'}
    for row in links:
        raw = row['source_path']
        if not raw or raw == row['link_path']:
            continue
        candidate = candidates.setdefault(raw, {'path': raw, 'code': row['code'], 'inventory': None, 'origin': '历史关联源'})
        candidate['historical'] = True
        if not candidate['code']:
            candidate['code'] = row['code']
    sources, seen_identity, deduplicated = [], set(), 0
    for raw, candidate in sorted(candidates.items()):
        try:
            source = _safe_path(raw)
            if source == output or source.is_relative_to(output):
                # A link-side path is never an independent rebuild source.
                continue
            if source.suffix.lower() not in local.VIDEO_SUFFIXES or not source.is_file():
                continue
            if candidate['inventory'] and not any(source.is_relative_to(root) for root in allowed) and not candidate.get('historical'):
                continue
            info = mediafile.parse(source)
            if info.ignored or info.trailer or mediafile.is_advertisement(source.stem):
                continue
            code = get_true_code(candidate['code']) or info.code
            if not code or code in downloading or (merge.base_code(code) or code) in downloading:
                warnings.append(f'未完成下载或未识别番号，跳过: {source}')
                continue
            stamp = _stamp(source)
            cached = candidate['inventory']
            if stamp[2] <= 0 or time.time() - source.stat().st_mtime < 120:
                warnings.append(f'视频尚未稳定，跳过: {source}')
                continue
            if cached and (cached['size'] != stamp[2] or abs(cached['mtime'] - source.stat().st_mtime) > 0.001):
                warnings.append(f'源视频与持久清单不同，请先刷新清单: {source}')
                continue
            identity = (stamp[0], stamp[1]) if stamp[1] else str(source)
            if identity in seen_identity:
                deduplicated += 1
                continue
            seen_identity.add(identity)
            sources.append({'path': str(source), 'code': code, 'stamp': stamp, 'origin': candidate['origin']})
        except (OSError, ValueError) as exc:
            warnings.append(f'源视频不可使用: {raw}: {exc}')
    # Separate download files can contain exactly the same movie. Do not let
    # group_entries turn those duplicates into artificial CD1/CD2 segments.
    buckets, canonical = {}, []
    for source in sources:
        info = mediafile.parse(source['path'])
        bucket = (merge.base_code(source['code']) or source['code'], info.part, source['stamp'][2])
        matches = buckets.setdefault(bucket, [])
        duplicate = any(stable_digest(source['path']) == stable_digest(other['path']) for other in matches)
        if duplicate:
            deduplicated += 1
            continue
        matches.append(source)
        canonical.append(source)
    sources = canonical
    if deduplicated:
        warnings.append(f'{deduplicated} 个内容相同的下载源仅选一份重建；全部原下载文件保留')
    inode_map, size_map, source_map = {}, {}, {row['path']: row for row in sources}
    for row in sources:
        inode_map.setdefault(tuple(row['stamp'][:2]), []).append(row)
        size_map.setdefault(row['stamp'][2], []).append(row)
    by_link = {row['link_path']: row for row in links}
    evidence, metadata, unrecoverable = [], [], []
    for video in old_videos:
        stamp = _stamp(video)
        row = by_link.get(str(video), {})
        preferred = source_map.get(row.get('source_path'))
        possibilities = [preferred] if preferred else []
        possibilities += inode_map.get(tuple(stamp[:2]), []) + size_map.get(stamp[2], [])
        verified, method, verified_digest = None, '', ''
        tested = set()
        for candidate in possibilities:
            if candidate['path'] in tested:
                continue
            tested.add(candidate['path'])
            source = Path(candidate['path'])
            if source.samefile(video):
                if not _independent_hardlink(source, video):
                    continue
                verified, method = candidate, 'inode'
                break
            if stamp[2] == candidate['stamp'][2]:
                digest = stable_digest(video)
                if digest == stable_digest(source):
                    verified, method, verified_digest = candidate, 'sha256', digest
                    break
        if verified is None:
            unrecoverable.append(str(video))
            if missing_policy == 'block':
                blockers.append(f'无法验证独立下载源，不能移走此视频: {video}')
            continue
        if _stamp(video) != stamp or _stamp(verified['path']) != verified['stamp']:
            raise ValueError('校验时视频发生变化，请下载完成后重新预览')
        code = get_true_code(row.get('code', '')) or verified['code']
        part = mediafile.parse(video).part
        evidence.append({'video': str(video), 'source': verified['path'], 'method': method, 'code': code, 'part': part,
                         'sha256': verified_digest, 'video_stamp': stamp, 'source_stamp': verified['stamp']})
        detail = local.read_group(code, [video])
        detail.update(_artwork(video))
        if detail:
            same_identity = (merge.base_code(code) or code) == (merge.base_code(verified['code']) or verified['code'])
            metadata.append({'code': code, 'target_code': verified['code'] if same_identity else code,
                             'part': part, 'detail': detail})
            if not same_identity:
                warnings.append(f'旧登记与下载源番号不同，保留旧资料但不套用到新番号: {video}')
    if not sources and missing_policy == 'block':
        blockers.append('没有可用于重建的已完成源视频')
    model_dump = getattr(settings, 'model_dump', None)
    settings_data = model_dump() if model_dump else vars(settings)
    # Compare the effective rebuild plan, not whole mutable tables. A directory
    # refresh rewrites indexed_at/last_scan without changing a single video.
    # Subscription updates for unrelated films must not invalidate this plan.
    settings_data = {key: value for key, value in settings_data.items()
                     if key.startswith('scrape_') or key in ('medialink_scrape_dir', 'medialink_library_path')}
    output_links = [row for row in links if _under(row['link_path'], output)]
    relevant_codes = {row['code'] for row in sources}
    relevant_codes.update(row['code'] for row in output_links)
    relevant_codes.update(merge.base_code(code) for code in list(relevant_codes))
    relevant_codes.discard(None)
    code_details = [{key: value for key, value in row.items() if key not in ('create_time', 'update_time', 'status')}
                    for row in codes if row['code'] in relevant_codes]
    keep_dirs = {str(Path(path).parent) for path in unrecoverable} if missing_policy == 'keep' else set()
    retained = [str(output / relative) for relative, _, kind in output_manifest if kind == 'file'
                and any((output / relative).is_relative_to(Path(folder)) for folder in keep_dirs)]
    if unrecoverable and missing_policy != 'block':
        warnings.append(f'{len(unrecoverable)} 个视频无法验证独立下载源；' +
                        ('保留其所在目录的文件，其余影片继续重建' if missing_policy == 'keep' else '确认后删除这些播放视频，不保留视频备份'))
    signatures = {
        'output': _digest([str(output), output.exists(),
                           [[raw, stamp[:2] if kind == 'dir' else stamp, kind] for raw, stamp, kind in output_manifest
                            if kind == 'dir' or Path(raw).suffix.lower() not in _GENERATED_SIDECARS]]),
        'sources': _digest([sorted(configured), sorted(str(path) for path in allowed),
                            sorted(([row['path'], row['code'], row['stamp']] for row in sources), key=lambda row: row[0])]),
        'links': _digest(sorted(([row['link_path'], row['source_path'], row['code']] for row in output_links))),
        'metadata': _digest(sorted(code_details, key=lambda row: row['code'])),
        'settings': _digest(settings_data),
        'recovery': _digest([evidence, missing_policy, unrecoverable, retained,
                             quarantine_mode, str(quarantine_parent)]),
    }
    fingerprint = _digest(signatures)
    plan = {'output_dir': str(output), 'source_count': len(sources), 'video_count': len(old_videos),
            'deduplicated_count': deduplicated,
            'quarantine_mode': quarantine_mode, 'quarantine_parent': str(quarantine_parent), 'backup_bytes': backup_bytes,
            'file_count': sum(item[2] == 'file' for item in output_manifest),
            'blocked_count': len(blockers), 'blockers': blockers[:200], 'warning_count': len(warnings), 'warnings': warnings[:200],
            'sample_paths': [str(path) for path in old_videos[:20]], 'metadata_count': len(metadata)}
    plan.update(missing_policy=missing_policy, unrecoverable_count=len(unrecoverable),
                unrecoverable_paths=unrecoverable, retained_file_count=len(retained))
    return {'public': plan, 'fingerprint': fingerprint, 'signatures': signatures, 'manifest': output_manifest, 'sources': sources,
            'quarantine_parent': str(quarantine_parent),
            'quarantine_mode': quarantine_mode,
            'missing_policy': missing_policy, 'unrecoverable': unrecoverable, 'retained': retained,
            'evidence': evidence, 'metadata': metadata,
            'links': output_links,
            'codes': codes, 'inventory': inventory, 'issued': time.time()}


def _artwork(video):
    """Keep independently authored poster/thumb and existing still photographs."""
    artwork = {}
    for kind, key in (('fanart', '_local_cover'), ('poster', '_local_poster'), ('thumb', '_local_thumb')):
        for suffix in ('.jpg', '.jpeg', '.png', '.webp'):
            path = video.with_name(f'{video.stem}-{kind}{suffix}')
            if path.is_file() and not _unsafe_links(path):
                artwork[key] = str(path)
                break
    stills = video.parent / 'extrafanart'
    if stills.is_dir() and not _unsafe_links(stills):
        artwork['_local_stills'] = [str(path) for path in sorted(stills.iterdir())
                                    if path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
                                    and path.is_file() and not _unsafe_links(path)]
    return artwork


def _under(raw, output):
    try:
        return Path(raw).is_absolute() and Path(raw).resolve().is_relative_to(output)
    except (OSError, ValueError):
        return False


def preview(missing_policy='block'):
    global _PLAN
    with _LOCK:
        if _STATE['running']:
            raise ValueError('已有预览或重建任务运行中')
        _PLAN = None
        settings = get_settings()
        _STATE.update(running=True, phase='preview', done=0, total=0, ok=0, error='', token='', plan=None, quarantine_dir='', results=[],
                      progress=None, progress_steps={}, active_codes=[], cleanup_note_count=0, cleanup_notes=[], retained_file_count=0, started_at=time.time(), finished_at=None,
                      output_dir=settings.medialink_scrape_dir or settings.medialink_library_path or '', quarantine_mode='', backup_bytes=0)
    def work():
        global _PLAN
        try:
            _progress_start('checking', '检查源视频与播放目录', unit='项')
            with _maintenance().activity():
                plan = _collect(missing_policy) if missing_policy != 'block' else _collect()
                token = hmac.new(_SECRET, f"{plan['fingerprint']}:{plan['issued']}".encode(), hashlib.sha256).hexdigest()
                plan['token'] = token
                plan['public']['expires_at'] = plan['issued'] + _TOKEN_LIFETIME
                with _LOCK:
                    _PLAN = plan
                    _STATE.update(phase='ready', plan=plan['public'], token=token,
                                  output_dir=plan['public']['output_dir'], quarantine_mode=plan['quarantine_mode'], backup_bytes=plan['public']['backup_bytes'])
        except Exception as exc:
            logger.exception('全量重建预览失败')
            _update(phase='failed', error=str(exc))
        finally:
            _update(running=False, finished_at=time.time())
    try:
        Thread(target=work, daemon=True, name='rebuild-preview').start()
    except Exception:
        _update(running=False, phase='failed', error='无法启动重建预览')
        raise
    return state()


def start(token: str, confirmation: str, *, refresh_metadata: bool = False, force: bool = False, confirm_discard: bool = False, new_task: bool = False):
    with _LOCK:
        if _STATE['running'] or not _PLAN or not hmac.compare_digest(token or '', _PLAN['token']):
            raise ValueError('预览已失效，请重新预览')
        if _PLAN['public']['blocked_count']:
            raise ValueError('存在无法安全重建的视频，请先处理预览中的阻止项')
        if _PLAN.get('missing_policy') == 'discard' and _PLAN.get('unrecoverable') and not confirm_discard:
            raise ValueError('请额外确认删除无法验证独立下载源的视频')
        if confirmation != _PLAN['public']['output_dir']:
            raise ValueError('请输入完整输出目录确认重建')
        if time.time() - _PLAN['issued'] > _TOKEN_LIFETIME:
            raise ValueError('预览已过期，请重新预览')
        if not new_task:
            from app.services.scraperecovery import history
            if any(row['can_resume'] for row in history(sys.modules[__name__])):
                raise ValueError('存在未完成的历史任务，请选择继续原任务，或明确选择重新清理并重建')
        plan = copy.deepcopy(_PLAN)
        _require_idle()
        _maintenance().reserve()
        try:
            _require_idle()
        except Exception:
            _maintenance().release()
            raise
        _STATE.update(running=True, phase='checking', done=0, total=plan['public']['source_count'], ok=0, error='', results=[],
                      progress=None, progress_steps={}, active_codes=[], cleanup_note_count=0, cleanup_notes=[], retained_file_count=0, started_at=time.time(), finished_at=None,
                      job_id=uuid.uuid4().hex, output_dir=plan['public']['output_dir'])
    try:
        Thread(target=_run, args=(plan, refresh_metadata, force), daemon=True, name='library-rebuild').start()
    except Exception:
        _maintenance().release()
        _update(running=False, phase='failed', error='无法启动全量重建')
        raise
    return state()


def _require_idle():
    from app.services import scrapeinventory, scrapesources, scrapewatch
    if scrapesources.state()['running'] or scrapeinventory.state()['running'] or scrapewatch._LOCK.locked():
        raise ValueError('刮削或源目录索引仍在运行，请完成后再开始全量重建')


def _remap(raw, output, storage):
    if isinstance(raw, list):
        return [_remap(value, output, storage) for value in raw]
    if isinstance(raw, str) and _under(raw, output):
        return str(storage / Path(raw).resolve().relative_to(output))
    return raw


def _save_metadata(plan):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    with session_scope() as session:
        # Production sessions disable autoflush: session.get() cannot see a
        # newly added pending row. Multiple parts must reuse one stored row.
        rows = {}
        insert = sqlite_insert if session.get_bind().dialect.name == 'sqlite' else pg_insert
        for index, item in enumerate(plan['metadata'], 1):
            for code in dict.fromkeys([item['code'], item.get('target_code', item['code'])]):
                row = rows.get(code)
                if row is None:
                    row = session.get(Code, code)
                if row is None:
                    # Conflict-safe even when another writer inserts after GET.
                    session.execute(insert(Code).values(code=code).on_conflict_do_nothing(index_elements=['code']))
                    row = session.get(Code, code)
                    base = session.get(Code, merge.base_code(code)) if merge.base_code(code) else None
                    if base is not None:
                        for key, value in merge.clean_detail(_plain(base), code).items():
                            if getattr(row, key, None) in (None, ''):
                                setattr(row, key, value)
                rows[code] = row
                for key, value in merge.clean_detail(item['detail'], code).items():
                    existing = getattr(row, key, None)
                    if existing in (None, '') or key in ('title', 'cn_title') and not merge.clean_detail({key: existing}, code).get(key):
                        setattr(row, key, value)
            if _STATE.get('running') and (_STATE.get('progress') or {}).get('stage') == 'metadata':
                _progress(done=index, current=item['code'])


def _map_cache_paths(output, storage, retained=()):
    with session_scope() as session:
        for row in session.scalars(select(Code)):
            for key in ('local_banner', 'local_still_photo'):
                value = getattr(row, key, None)
                if value:
                    setattr(row, key, ','.join(part if part in retained else _remap(part, output, storage) for part in value.split(',')))


def _write_manifest(path, manifest):
    if path.name == 'manifest.json':
        results = {row['code']: row for row in manifest.get('results', [])}
        # Put small discovery fields first: legacy manifests can exceed 100 MB.
        manifest = {'source_count': len(manifest.get('sources', [])), 'done': len(results),
                    'ok': sum(bool(row['ok']) for row in results.values()),
                    **{key: value for key, value in manifest.items() if key not in ('source_count', 'done', 'ok')}}
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, default=str, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path):
    if os.name == 'nt':
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _check_tree(plan, output, mutable_sidecars=False):
    current, _, blockers = _tree(output)
    def essential(items):
        return [[raw, stamp[:2] if kind == 'dir' else stamp, kind] for raw, stamp, kind in items
                if kind == 'dir' or Path(raw).suffix.lower() not in _GENERATED_SIDECARS]
    changed = essential(current) != essential(plan['manifest']) if mutable_sidecars else current != plan['manifest']
    if blockers or changed:
        raise ValueError('输出目录在清理前变化，停止操作；尚未清理的文件保留原位')


def _check_sources(plan, *, owned_inodes=()):
    output = Path(plan['public']['output_dir'])
    stamps = {}
    for source in plan['sources']:
        path = _safe_path(source['path'])
        if path == output or path.is_relative_to(output) or not path.is_file():
            raise ValueError('独立下载源已不可用，停止清理播放目录')
        current, expected = _stamp(path), source['stamp']
        owned = tuple(expected[:2]) in owned_inodes
        changed = current[:4] != expected[:4] if owned else current != expected
        if changed:
            raise ValueError('独立下载源已变化，停止清理播放目录')
        stamps[source['path']] = current
    return stamps


def _append_operation(quarantine, operation):
    batch = getattr(_CLEANUP_LOCAL, 'batch', None)
    if batch is not None and batch.quarantine == quarantine:
        batch.append(operation)
        return
    with (quarantine / 'moves.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(operation, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(quarantine)


class _CleanupJournal:
    """Bound cleanup syncs; recovery reconciles the original snapshot with disk."""
    def __init__(self, quarantine):
        self.quarantine = quarantine
        self.operations = []
        self.directories = set()
        self.updated = time.monotonic()

    def append(self, operation):
        self.operations.append(operation)
        if len(self.operations) >= 64 or time.monotonic() - self.updated >= 1:
            self.flush()

    def flush(self):
        for directory in self.directories:
            _fsync_directory(directory)
        if self.operations:
            with (self.quarantine / 'moves.jsonl').open('a', encoding='utf-8') as stream:
                stream.writelines(json.dumps(row, ensure_ascii=False) + '\n' for row in self.operations)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(self.quarantine)
        self.operations.clear()
        self.directories.clear()
        self.updated = time.monotonic()


def _sync_cleanup_directory(path):
    batch = getattr(_CLEANUP_LOCAL, 'batch', None)
    if batch is None:
        _fsync_directory(path)
    else:
        batch.directories.add(path)


def _batch_cleanup(function):
    @wraps(function)
    def run(plan, output, storage, quarantine, manifest):
        batch = _CleanupJournal(quarantine)
        previous = getattr(_CLEANUP_LOCAL, 'batch', None)
        _CLEANUP_LOCAL.batch = batch
        try:
            return function(plan, output, storage, quarantine, manifest)
        finally:
            try:
                batch.flush()
            finally:
                _CLEANUP_LOCAL.batch = previous
    return run


def _backup_sidecars(plan, storage):
    """Back up sidecars; regenerable artwork/NFO failures do not block jobs."""
    output = Path(plan['public']['output_dir'])
    backups = []
    files = [(relative, stamp) for relative, stamp, kind in plan['manifest']
             if kind == 'file' and Path(relative).suffix.lower() not in local.VIDEO_SUFFIXES]
    _progress_start('backup', '备份并校验图片、NFO、字幕', len(files), bytes_total=sum(stamp[2] for _, stamp in files))
    copied_bytes = 0
    processed = 0
    for relative, expected, kind in plan['manifest']:
        original = output / relative
        destination = storage / relative
        if not _under(original, output) or not _under(destination, storage) or _unsafe_links(original) or _unsafe_links(destination):
            raise ValueError('备份路径越界或出现符号链接，停止重建')
        if kind == 'dir':
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if original.suffix.lower() in local.VIDEO_SUFFIXES:
            continue
        generated = original.suffix.lower() in _GENERATED_SIDECARS
        if generated and (not original.is_file() or _stamp(original) != expected):
            processed += 1
            _progress(done=processed, current=str(original))
            continue
        if _stamp(original) != expected:
            raise ValueError(f'备份前文件已变化: {original}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        _progress(current=str(original), current_bytes=0, current_total_bytes=expected[2])
        file_bytes = 0
        try:
            with original.open('rb') as incoming, destination.open('xb') as outgoing:
                while chunk := incoming.read(4 * 1024 * 1024):
                    outgoing.write(chunk)
                    digest.update(chunk)
                    file_bytes += len(chunk)
                    _progress(bytes_done=copied_bytes + file_bytes, current_bytes=file_bytes)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _stamp(original) != expected or destination.stat().st_size != expected[2] or _progress_hash(destination) != digest.hexdigest():
                raise ValueError(f'备份完整校验失败，未开始清理: {original}')
        except (OSError, ValueError):
            if not generated:
                raise
            processed += 1
            _progress(done=processed, current=str(original))
            continue
        backups.append({'path': relative, 'size': expected[2], 'sha256': digest.hexdigest()})
        copied_bytes += expected[2]
        processed += 1
        _progress(done=processed, bytes_done=copied_bytes)
    for folder, _, _ in os.walk(storage, topdown=False):
        _fsync_directory(Path(folder))
    _fsync_directory(storage.parent)
    _fsync_directory(storage.parent.parent)
    return backups


@_batch_cleanup
def _clear_copied_output(plan, output, storage, quarantine, manifest):
    """Remove only snapshotted files, with per-video independent-source checks."""
    _check_tree(plan, output, mutable_sidecars=True)
    _check_sources(plan)
    sources = {source['path']: source for source in plan['sources']}
    evidence = {item['video']: item for item in plan['evidence']}
    retained = set(plan.get('retained', []))
    discarded = set(plan.get('unrecoverable', [])) if plan.get('missing_policy') == 'discard' else set()
    backups = {item['path']: item for item in manifest['sidecar_backups']}
    _progress_start('verify_backups', '清理前复核备份', len(backups))
    for index, backup in enumerate(backups.values(), 1):
        saved = storage / backup['path']
        if not _under(saved, storage) or _unsafe_links(saved) or not saved.is_file() or _progress_hash(saved) != backup['sha256']:
            if saved.suffix.lower() in _GENERATED_SIDECARS:
                manifest.setdefault('cleanup_notes', []).append({'path': str(saved), 'status': 'regenerate', 'reason': '可重新生成的资料备份不可用，继续重刮'})
            else:
                raise ValueError('备份在清理开始前已变化，尚未删除任何文件')
        _progress(done=index)
    _progress_start('clearing', '校验并清理旧播放文件', sum(kind == 'file' and str(output / relative) not in retained
                                                       for relative, _, kind in plan['manifest']))
    cleared = 0
    expected_stamps = {str(output / relative): stamp for relative, stamp, kind in plan['manifest'] if kind == 'file'}
    expected_stamps.update({source['path']: source['stamp'] for source in plan['sources']})
    inode_paths = {}
    for raw, stamp in expected_stamps.items():
        inode_paths.setdefault(tuple(stamp[:2]), []).append(raw)
    for relative, expected, kind in plan['manifest']:
        if kind != 'file':
            continue
        path = output / relative
        expected = expected_stamps[str(path)]
        if str(path) in retained:
            continue
        if path.suffix.lower() not in local.VIDEO_SUFFIXES:
            _progress(current=str(path), current_bytes=0, current_total_bytes=0)
            status, note = _clear_sidecar(path, relative, storage, quarantine, manifest, backups.get(relative))
            if status == 'retained':
                retained.add(str(path))
            if note:
                manifest.setdefault('cleanup_notes', []).append({'path': str(path), 'status': status, 'reason': note})
                _update(cleanup_note_count=len(manifest['cleanup_notes']), cleanup_notes=manifest['cleanup_notes'][-100:])
                _write_manifest(quarantine / 'manifest.json', manifest)
            cleared += 1
            _progress(done=cleared, current=str(path), retained_count=len(retained))
            continue
        if not _under(path, output) or _unsafe_links(path) or _stamp(path) != expected:
            raise ValueError(f'待清理文件已变化或路径不安全，停止清理: {path}')
        if str(path) in retained:
            continue
        _progress(current=str(path), current_bytes=0, current_total_bytes=0)
        if str(path) in discarded:
            # Only explicitly confirmed, snapshotted unrecoverable videos may
            # bypass independent-source evidence. Path/stamp checks still apply.
            if path.suffix.lower() not in local.VIDEO_SUFFIXES:
                raise ValueError('待丢弃文件不是已预览的视频')
        elif path.suffix.lower() in local.VIDEO_SUFFIXES:
            proof = evidence.get(str(path))
            source = sources.get(proof['source']) if proof else None
            if not source:
                raise ValueError(f'视频缺少独立源恢复证据，停止清理: {path}')
            independent = _safe_path(source['path'])
            source_expected = expected_stamps[str(independent)]
            if independent.is_relative_to(output) or not independent.is_file() or _stamp(independent) != source_expected:
                raise ValueError(f'视频独立下载源已变化，停止清理: {path}')
            if proof['method'] == 'inode':
                if not path.samefile(independent) or not _independent_hardlink(independent, path):
                    raise ValueError(f'视频与独立源硬链接身份不符，停止清理: {path}')
            elif proof['method'] == 'sha256':
                reusable = (bool(proof.get('sha256')) and proof.get('video_stamp') == expected
                            and proof.get('source_stamp') == source_expected)
                if not reusable and _progress_hash(path) != _progress_hash(independent):
                    raise ValueError(f'视频与独立源内容校验不符，停止清理: {path}')
            else:
                raise ValueError(f'未知视频恢复证据，停止清理: {path}')
            # Recheck after any full-file hash and immediately before unlink.
            if _unsafe_links(independent) or _stamp(independent) != source_expected or _stamp(path) != expected:
                raise ValueError('视频校验期间发生变化，停止清理')
        if _unsafe_links(path) or _stamp(path) != expected:
            raise ValueError('文件在最终清理检查时变化，停止操作')
        # Never recurse or remove the root. Newly arrived files are untouched.
        path.unlink()
        _sync_cleanup_directory(path.parent)
        _append_operation(quarantine, {'operation': 'unlink_discard_confirmed' if str(path) in discarded else 'unlink_verified', 'path': relative})
        cleared += 1
        _progress(done=cleared, current=str(path))
        # Only this successful unlink's shared inode may legitimately acquire
        # a new ctime. Unrelated source/sidecar stamps remain fully strict.
        for raw in inode_paths.get(tuple(expected[:2]), []):
            if raw == str(path) or not Path(raw).exists():
                continue
            changed = _stamp(raw)
            if _unsafe_links(Path(raw)) or changed[:4] != expected_stamps[raw][:4]:
                raise ValueError('清理旧链接期间共享文件发生变化，停止操作')
            expected_stamps[raw] = changed
    # Persist file removals while all their parent directories still exist.
    batch = getattr(_CLEANUP_LOCAL, 'batch', None)
    if batch is not None:
        batch.flush()
    directories = [(output / relative, expected) for relative, expected, kind in plan['manifest'] if kind == 'dir']
    _progress_start('cleanup_dirs', '整理空目录', sum(not any(Path(raw).is_relative_to(directory) for raw in retained)
                                               for directory, _ in directories), unit='目录')
    removed_dirs = 0
    for directory, expected in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        if _unsafe_links(directory) or not _under(directory, output) or _stamp(directory)[:2] != expected[:2]:
            raise ValueError('清理空目录时发现不安全路径，停止操作')
        if any(Path(raw).is_relative_to(directory) for raw in retained):
            continue
        try:
            directory.rmdir()
            _fsync_directory(directory.parent)
        except OSError:
            # Emby may create fresh metadata in reaction to video deletions.
            # Never recurse into unexpected children or stop the whole batch.
            retained.add(str(directory))
        removed_dirs += 1
        _progress(done=removed_dirs, current=str(directory), current_bytes=0, current_total_bytes=0)
    remaining, _, remaining_blockers = _tree(output)
    for relative, _, kind in remaining:
        if kind == 'file':
            retained.add(str(output / relative))
    if remaining or remaining_blockers:
        manifest.setdefault('cleanup_notes', []).append({'status': 'retained', 'reason': '清理期间出现或无法移除的文件留在原位，其余影片继续重建', 'count': len(retained)})
        _update(cleanup_note_count=len(manifest['cleanup_notes']), cleanup_notes=manifest['cleanup_notes'][-100:])
    plan['retained'] = sorted(retained)
    manifest['retained'] = plan['retained']
    for source in plan['sources']:
        if _unsafe_links(Path(source['path'])) or _stamp(source['path']) != expected_stamps[source['path']]:
            raise ValueError('清理结束时独立下载源发生变化，停止重建')
    return {source['path']: expected_stamps[source['path']] for source in plan['sources']}


def _clear_sidecar(path, relative, storage, quarantine, manifest, backup):
    """Discard regenerable outputs; leave changed non-generated files in place."""
    output = Path(manifest['output_dir'])
    try:
        if not _under(path, output) or _unsafe_links(path):
            raise ValueError('路径不安全，文件保留')
        if path.suffix.lower() in _GENERATED_SIDECARS:
            if not path.exists():
                return 'already_absent', ''
            if not path.is_file():
                raise ValueError('资料路径已变为目录，原位保留')
            # NFO/artwork are disposable outputs, not source-video evidence.
            # External metadata edits and timestamp changes must not halt jobs.
            path.unlink()
            _sync_cleanup_directory(path.parent)
            _append_operation(quarantine, {'operation': 'unlink_regenerable_sidecar', 'path': relative})
            return 'removed', ''
        saved = storage / relative
        if not backup or _unsafe_links(saved) or not saved.is_file() or _progress_hash(saved) != backup['sha256']:
            raise ValueError('备份无法验证，原文件保留')
        if not path.exists():
            return 'already_absent', '其他程序已移除资料文件，原备份仍保留'
        if not path.is_file():
            raise ValueError('文件类型变化，原路径保留')
        before = _stamp(path)
        digest = _progress_hash(path)
        if _stamp(path) != before:
            raise ValueError('文件仍在写入，留在原位并继续其余影片')
        updated = digest != backup['sha256']
        if updated:
            return 'retained', '不可再生文件内容发生变化，留在原位并继续处理其余文件'
        if _unsafe_links(path) or _stamp(path) != before:
            raise ValueError('最终检查时文件变化，保留当前文件')
        path.unlink()
        _sync_cleanup_directory(path.parent)
        _append_operation(quarantine, {'operation': 'unlink_sidecar_verified', 'path': relative, 'updated_backup': updated})
        return 'removed', ''
    except (OSError, ValueError) as exc:
        logger.warning(f'重建保留单个资料文件，继续执行: {path}: {exc}')
        return 'retained', str(exc)


def _run(plan, refresh_metadata, force=False):
    quarantine = None
    manifest = None
    try:
        _progress_start('checking', '开始前复核源视频与播放目录', unit='项')
        policy = plan.get('missing_policy', 'block')
        checked = _collect(policy) if policy != 'block' else _collect()
        if checked['fingerprint'] != plan['fingerprint'] or checked['public']['blocked_count']:
            labels = {'output': '播放目录文件', 'sources': '下载源或源视频', 'links': '媒体库关联',
                      'metadata': '本次影片资料', 'settings': '刮削设置', 'recovery': '恢复范围'}
            changed = [label for key, label in labels.items()
                       if checked.get('signatures', {}).get(key) != plan.get('signatures', {}).get(key)]
            reason = '、'.join(changed) or '文件、配置或数据库'
            if checked['public']['blocked_count']:
                reason += '；存在阻止项：' + '；'.join(checked['public']['blockers'][:3])
            raise ValueError(f'{reason}已变化，请重新预览；尚未移动任何文件')
        output = Path(plan['public']['output_dir'])
        # Persist deletion protection before the first rename; release() must not
        # remove it, including on failure or application restart.
        _maintenance().protect(str(output))
        mode = plan['quarantine_mode']
        parent = Path(plan['quarantine_parent'])
        if mode == 'copy_sidecars':
            if _free_space(parent) < plan['public']['backup_bytes']:
                raise ValueError('非视频文件备份空间不足，尚未清理任何文件')
        parent.mkdir(parents=True, exist_ok=True)
        if _unsafe_links(parent):
            raise ValueError('隔离位置变成不安全路径，停止重建')
        quarantine = Path(plan['quarantine_parent']) / f'.{output.name}.cinefold-rebuild-{uuid.uuid4().hex}'
        quarantine.mkdir(mode=0o700)
        storage = quarantine / 'library'
        storage.mkdir()
        output.mkdir(exist_ok=True)
        if mode == 'rename' and quarantine.stat().st_dev != output.stat().st_dev:
            raise ValueError('隔离目录与输出目录不在同一文件系统')
        _update(phase='quarantine', quarantine_dir=str(quarantine), quarantine_mode=mode, backup_bytes=plan['public']['backup_bytes'])
        from app.services.scraperecovery import naming_config
        manifest = {'version': 2, 'created_at': datetime.now().isoformat(), 'output_dir': str(output),
                    'checkpoint': 'quarantine', 'naming_config': naming_config(get_settings()),
                    'options': {'refresh_metadata': refresh_metadata, 'force': force},
                    'job_id': _STATE.get('job_id'),
                    'quarantine_mode': mode, 'backup_bytes': plan['public']['backup_bytes'],
                    'video_storage': 'source_references' if mode == 'copy_sidecars' else 'quarantined_files',
                    'recovery_instructions': ('仅非视频文件已备份至library。视频未额外复制；按evidence的source→video恢复旧硬链接（同盘）或复制恢复（跨盘），恢复前核对source签名。'
                                              if mode == 'copy_sidecars' else '旧目录与视频保留在library，按original_manifest和media_links核对后手动恢复。'),
                    'phase': 'quarantine', 'original_manifest': plan['manifest'], 'sources': plan['sources'],
                    'evidence': plan['evidence'], 'media_links': plan['links'], 'metadata': plan['metadata'],
                    'codes': plan['codes'],
                    'missing_policy': policy, 'unrecoverable': plan.get('unrecoverable', []), 'retained': plan.get('retained', []),
                    'moved': [], 'results': []}
        manifest_path = quarantine / 'manifest.json'
        _write_manifest(manifest_path, manifest)
        _progress_start('metadata', '保留影片资料到数据库', len(plan['metadata']), unit='条')
        _save_metadata(plan)
        _progress(done=len(plan['metadata']))
        # Check exact top-level names as well as every original descendant. A
        # new external file appearing after preview must never be swept away.
        _check_tree(plan, output, mutable_sidecars=mode == 'copy_sidecars')
        _check_sources(plan)
        if mode == 'copy_sidecars':
            _update(phase='backup')
            manifest['phase'] = 'backup'
            manifest['checkpoint'] = 'backup'
            _write_manifest(manifest_path, manifest)
            manifest['sidecar_backups'] = _backup_sidecars(plan, storage)
            manifest['phase'] = 'backups_verified'
            manifest['checkpoint'] = 'clearing'
            _write_manifest(manifest_path, manifest)
            _update(phase='clearing')
            source_stamps = _clear_copied_output(plan, output, storage, quarantine, manifest)
        else:
            expected = {item[0]: item[1] for item in plan['manifest']}
            moved_inodes = set()
            children = sorted(output.iterdir())
            _progress_start('quarantine', '移出旧播放目录', len(children), unit='项')
            for index, child in enumerate(children, 1):
                _progress(current=str(child))
                if str(child.relative_to(output)) not in expected or _unsafe_links(child):
                    raise ValueError('输出目录中出现未预览的文件，停止重建')
                if child.is_file():
                    moved_inodes.add(tuple(_stamp(child)[:2]))
                os.replace(child, storage / child.name)
                manifest['moved'].append(child.name)
                _append_operation(quarantine, {'operation': 'rename', 'name': child.name})
                _progress(done=index)
            source_stamps = _check_sources(plan, owned_inodes=moved_inodes)
        retained = set(plan.get('retained', []))
        _map_cache_paths(output, storage, retained) if retained else _map_cache_paths(output, storage)
        manifest['recovery_source_stamps'] = source_stamps
        manifest['recovery_signature_fields'] = ['device', 'inode', 'size', 'mtime_ns', 'ctime_ns']
        # Only this output's associations are retired. Download history,
        # subscriptions and Code metadata remain intact.
        with session_scope() as session:
            for old in plan['links']:
                if old['link_path'] in retained:
                    continue
                row = session.get(MediaLink, old['link_path'])
                if row is not None:
                    session.delete(row)
            for cached in session.scalars(select(ScrapeSourceVideo)):
                cached.links = [path for path in (cached.links or []) if path in retained or not _under(path, output)]
                if not cached.links:
                    cached.artifact_status = 'pending'
        manifest['phase'] = 'rebuilding'
        manifest['checkpoint'] = 'rebuilding'
        _write_manifest(manifest_path, manifest)
        _update(phase='rebuilding')
        _run_films(plan, manifest, quarantine, storage, source_stamps, refresh_metadata, force)
    except Exception as exc:
        logger.exception('全量重建未完成；下载源保留，请核对隔离目录备份和恢复清单')
        if manifest is not None and quarantine is not None:
            manifest['phase'], manifest['error'] = 'failed', str(exc)
            try:
                _write_manifest(quarantine / 'manifest.json', manifest)
            except Exception:
                logger.exception('无法更新隔离目录恢复清单')
        _update(phase='failed', error=str(exc))
    finally:
        try:
            _update(running=False, finished_at=time.time())
        finally:
            _maintenance().release()


def _run_films(plan, manifest, quarantine, storage, source_stamps, refresh_metadata, force, selected_codes=None):
    manifest_path = quarantine / "manifest.json"
    output = Path(plan['public']['output_dir'])
    mode = plan["quarantine_mode"]
    from app.services import scrape, scrapesources
    groups = scrapesources.group_entries([{'path': item['path'], 'code': item['code']} for item in plan['sources']])
    preserved = {}
    for item in plan['metadata']:
        detail = preserved.setdefault(item.get('target_code', item['code']), {})
        artwork = detail.setdefault('_local_by_part', {}).setdefault(str(item.get('part', 0)), {})
        for key, value in item['detail'].items():
            detail.setdefault(key, _remap(value, output, storage) if key.startswith('_local_') else value)
            if key in ('_local_cover', '_local_poster', '_local_thumb', '_local_stills'):
                artwork.setdefault(key, _remap(value, output, storage))
    latest = {row['code']: row for row in manifest.get('results', [])}
    selected = set(groups) if selected_codes is None else set(selected_codes)
    # Retried failures are replaced, not added to the historical failure count.
    for code in selected:
        latest.pop(code, None)
    _update(total=len(groups), done=len(latest), ok=sum(bool(row['ok']) for row in latest.values()), results=list(latest.values())[-200:], active_codes=[])
    _progress_start('rebuilding', '重新刮削影片', len(groups), unit='部')
    _progress(done=len(latest))
    attempt_lock = threading.Lock()
    job_id = _STATE.get('job_id', '')
    def rebuild(code, items):
        with _LOCK:
            active = _STATE.setdefault('active_codes', [])
            if code not in active:
                active.append(code)
            _progress(current='、'.join(active))
        with attempt_lock:
            scrapesources.record_attempt(items, code, status='running', job_id=job_id)
        try:
            for item in items:
                expected = source_stamps.get(str(item.path))
                if not expected or _unsafe_links(item.path) or _stamp(item.path) != expected:
                    raise ValueError('源视频已变化或缺失，停止此影片的重建')
            with _maintenance().internal():
                result = scrape.scrape_group(code, items, refresh_metadata=refresh_metadata,
                                             preserved_meta=preserved.get(code), **({'force': True} if force else {}))
            row = {'code': code, 'ok': result.ok, 'error': result.error,
                   'warnings': result.warnings, 'links': [str(path) for path in result.links]}
        except Exception as exc:
            logger.exception(f'[{code}] 全量重建失败，下载源保留，请核对隔离目录的恢复清单')
            row = {'code': code, 'ok': False, 'error': str(exc), 'warnings': [], 'links': []}
        with attempt_lock:
            scrapesources.record_attempt(items, code, status='succeeded' if row['ok'] else 'failed',
                                         job_id=job_id, error=row['error'])
        return row
    # Keep the executor's internal queue bounded too; a large library must
    # not create thousands of futures while only two films can progress.
    queue = iter((code, items) for code, items in groups.items() if code in selected)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='rebuild-film') as pool:
        pending = set()
        for _ in range(2):
            entry = next(queue, None)
            if entry:
                pending.add(pool.submit(rebuild, *entry))
        while pending:
            future = next(as_completed(pending))
            pending.remove(future)
            row = future.result()
            latest[row['code']] = row
            manifest['results'] = list(latest.values())
            with (quarantine / 'results.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(quarantine)
            with _LOCK:
                _STATE['done'] += 1
                _STATE['ok'] += int(row['ok'])
                _STATE['results'] = manifest['results'][-200:]
                active = _STATE.get('active_codes', [])
                if row['code'] in active:
                    active.remove(row['code'])
                _progress(done=_STATE['done'], current='、'.join(active))
                _persist_state()
            entry = next(queue, None)
            if entry:
                pending.add(pool.submit(rebuild, *entry))
    manifest['results'] = list(latest.values())
    failed = sum(not row['ok'] for row in manifest['results'])
    unfinished = len(set(groups) - set(latest))
    # Successful new hard links legitimately change ctime once more.
    # Record their final signatures for later manual recovery checks.
    for raw, expected in source_stamps.items():
        try:
            current = _stamp(raw)
            if current[:4] == expected[:4] and not _unsafe_links(Path(raw)):
                manifest['recovery_source_stamps'][raw] = current
            else:
                manifest.setdefault('recovery_source_changes', []).append(raw)
        except OSError:
            manifest.setdefault('recovery_source_changes', []).append(raw)
    manifest['phase'] = 'completed' if not failed and not unfinished else 'partial'
    manifest['checkpoint'] = 'rebuilding'
    _write_manifest(manifest_path, manifest)
    recovery = '非视频备份在隔离目录，旧视频可按清单从下载源恢复' if mode == 'copy_sidecars' else '旧产物保留在隔离目录，可手动恢复'
    _update(phase=manifest['phase'], cleanup_note_count=len(manifest.get('cleanup_notes', [])),
            cleanup_notes=manifest.get('cleanup_notes', [])[-100:], retained_file_count=len(plan.get('retained', [])),
            error=f'{failed} 部重建失败，{unfinished} 部未处理；{recovery}' if failed or unfinished else '')
