"""Preview and rebuild a library without deleting downloads or old library data.

The old tree is renamed to a sibling quarantine. Every old video needs an
independently accessible, verified download source before any file is moved.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
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


def _maintenance():
    from app.services import scrapemaintenance
    return scrapemaintenance


def state():
    with _LOCK:
        if _STATE['phase'] == 'idle' and _STATE_FILE.is_file():
            try:
                saved = json.loads(_STATE_FILE.read_text(encoding='utf-8'))
                _STATE.update({key: value for key, value in saved.items() if key in _STATE or key in ('job_id', 'output_dir')})
                if saved.get('running'):
                    _STATE.update(running=False, phase='interrupted',
                                  error='进程重启，重建中断；旧产物保留在隔离目录，请核对后重试')
            except (OSError, ValueError, TypeError):
                _STATE.update(error='无法读取上次重建恢复记录，请检查数据目录')
        value = copy.deepcopy(_STATE)
    value.update(_maintenance().protection())
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
    keys = ('running', 'phase', 'done', 'total', 'ok', 'error', 'quarantine_dir', 'job_id', 'output_dir')
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_manifest(_STATE_FILE, {key: _STATE.get(key) for key in keys})


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


def _collect():
    """Read-only plan. No downloader calls and no discovery outside known paths."""
    settings = get_settings()
    output = _safe_path(settings.medialink_scrape_dir or settings.medialink_library_path, output=True)
    quarantine_parent = output.parent
    if settings.medialink_library_path:
        playback_root = _safe_path(settings.medialink_library_path)
        if output.is_relative_to(playback_root):
            quarantine_parent = playback_root.parent
    if _unsafe_links(quarantine_parent) or not quarantine_parent.is_dir():
        raise ValueError('播放目录外的隔离位置不可访问，请检查媒体库上级目录挂载')
    if output.exists() and quarantine_parent.stat().st_dev != output.stat().st_dev:
        raise ValueError('播放目录外的隔离位置必须在同一文件系统，请挂载媒体库上级目录')
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
    evidence, metadata = [], []
    for video in old_videos:
        stamp = _stamp(video)
        row = by_link.get(str(video), {})
        preferred = source_map.get(row.get('source_path'))
        possibilities = [preferred] if preferred else []
        possibilities += inode_map.get(tuple(stamp[:2]), []) + size_map.get(stamp[2], [])
        verified, method = None, ''
        tested = set()
        for candidate in possibilities:
            if candidate['path'] in tested:
                continue
            tested.add(candidate['path'])
            source = Path(candidate['path'])
            if source.samefile(video):
                verified, method = candidate, 'inode'
                break
            if stamp[2] == candidate['stamp'][2] and stable_digest(video) == stable_digest(source):
                verified, method = candidate, 'sha256'
                break
        if verified is None:
            blockers.append(f'无法验证独立下载源，不能移走此视频: {video}')
            continue
        if _stamp(video) != stamp or _stamp(verified['path']) != verified['stamp']:
            raise ValueError('校验时视频发生变化，请下载完成后重新预览')
        code = get_true_code(row.get('code', '')) or verified['code']
        part = mediafile.parse(video).part
        evidence.append({'video': str(video), 'source': verified['path'], 'method': method, 'code': code, 'part': part})
        detail = local.read_group(code, [video])
        detail.update(_artwork(video))
        if detail:
            same_identity = (merge.base_code(code) or code) == (merge.base_code(verified['code']) or verified['code'])
            metadata.append({'code': code, 'target_code': verified['code'] if same_identity else code,
                             'part': part, 'detail': detail})
            if not same_identity:
                warnings.append(f'旧登记与下载源番号不同，保留旧资料但不套用到新番号: {video}')
    if not sources:
        blockers.append('没有可用于重建的已完成源视频')
    model_dump = getattr(settings, 'model_dump', None)
    settings_data = model_dump() if model_dump else vars(settings)
    database_digest = _digest([roots, inventory, links, attempts, codes])
    fingerprint = _digest([str(output), output.exists(), output_manifest, sources, evidence, database_digest, settings_data])
    plan = {'output_dir': str(output), 'source_count': len(sources), 'video_count': len(old_videos),
            'deduplicated_count': deduplicated,
            'file_count': sum(item[2] == 'file' for item in output_manifest),
            'blocked_count': len(blockers), 'blockers': blockers[:200], 'warnings': warnings[:200],
            'sample_paths': [str(path) for path in old_videos[:20]], 'metadata_count': len(metadata)}
    return {'public': plan, 'fingerprint': fingerprint, 'manifest': output_manifest, 'sources': sources,
            'quarantine_parent': str(quarantine_parent),
            'evidence': evidence, 'metadata': metadata,
            'links': [row for row in links if _under(row['link_path'], output)],
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


def preview():
    global _PLAN
    with _LOCK:
        if _STATE['running']:
            raise ValueError('已有预览或重建任务运行中')
        _PLAN = None
        _STATE.update(running=True, phase='preview', done=0, total=0, ok=0, error='', token='', plan=None, quarantine_dir='', results=[])
    def work():
        global _PLAN
        try:
            with _maintenance().activity():
                plan = _collect()
                token = hmac.new(_SECRET, f"{plan['fingerprint']}:{plan['issued']}".encode(), hashlib.sha256).hexdigest()
                plan['token'] = token
                with _LOCK:
                    _PLAN = plan
                    _STATE.update(phase='ready', plan=plan['public'], token=token)
        except Exception as exc:
            logger.exception('全量重建预览失败')
            _update(phase='failed', error=str(exc))
        finally:
            _update(running=False)
    try:
        Thread(target=work, daemon=True, name='rebuild-preview').start()
    except Exception:
        _update(running=False, phase='failed', error='无法启动重建预览')
        raise
    return state()


def start(token: str, confirmation: str, *, refresh_metadata: bool = False):
    with _LOCK:
        if _STATE['running'] or not _PLAN or not hmac.compare_digest(token or '', _PLAN['token']):
            raise ValueError('预览已失效，请重新预览')
        if _PLAN['public']['blocked_count']:
            raise ValueError('存在无法安全重建的视频，请先处理预览中的阻止项')
        if confirmation != _PLAN['public']['output_dir']:
            raise ValueError('请输入完整输出目录确认重建')
        if time.time() - _PLAN['issued'] > _TOKEN_LIFETIME:
            raise ValueError('预览已过期，请重新预览')
        plan = copy.deepcopy(_PLAN)
        _require_idle()
        _maintenance().reserve()
        try:
            _require_idle()
        except Exception:
            _maintenance().release()
            raise
        _STATE.update(running=True, phase='checking', done=0, total=plan['public']['source_count'], ok=0, error='', results=[],
                      job_id=uuid.uuid4().hex, output_dir=plan['public']['output_dir'])
    try:
        Thread(target=_run, args=(plan, refresh_metadata), daemon=True, name='library-rebuild').start()
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
    with session_scope() as session:
        for item in plan['metadata']:
            for code in dict.fromkeys([item['code'], item.get('target_code', item['code'])]):
                row = session.get(Code, code)
                if row is None:
                    row = Code(code=code)
                    session.add(row)
                    base = session.get(Code, merge.base_code(code)) if merge.base_code(code) else None
                    if base is not None:
                        for key, value in merge.clean_detail(_plain(base), code).items():
                            setattr(row, key, value)
                for key, value in merge.clean_detail(item['detail'], code).items():
                    existing = getattr(row, key, None)
                    if existing in (None, '') or key in ('title', 'cn_title') and not merge.clean_detail({key: existing}, code).get(key):
                        setattr(row, key, value)


def _map_cache_paths(output, storage):
    with session_scope() as session:
        for row in session.scalars(select(Code)):
            for key in ('local_banner', 'local_still_photo'):
                value = getattr(row, key, None)
                if value:
                    setattr(row, key, ','.join(_remap(part, output, storage) for part in value.split(',')))


def _write_manifest(path, manifest):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, default=str, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _run(plan, refresh_metadata):
    quarantine = None
    manifest = None
    try:
        checked = _collect()
        if checked['fingerprint'] != plan['fingerprint'] or checked['public']['blocked_count']:
            raise ValueError('文件、配置或数据库已变化，请重新预览；尚未移动任何文件')
        output = Path(plan['public']['output_dir'])
        # Persist deletion protection before the first rename; release() must not
        # remove it, including on failure or application restart.
        _maintenance().protect(str(output))
        quarantine = Path(plan['quarantine_parent']) / f'.{output.name}.cinefold-rebuild-{uuid.uuid4().hex}'
        quarantine.mkdir(mode=0o700)
        storage = quarantine / 'library'
        storage.mkdir()
        output.mkdir(exist_ok=True)
        if quarantine.stat().st_dev != output.stat().st_dev:
            raise ValueError('隔离目录与输出目录不在同一文件系统')
        _update(phase='quarantine', quarantine_dir=str(quarantine))
        manifest = {'version': 1, 'created_at': datetime.now().isoformat(), 'output_dir': str(output),
                    'job_id': _STATE.get('job_id'),
                    'phase': 'quarantine', 'original_manifest': plan['manifest'], 'sources': plan['sources'],
                    'evidence': plan['evidence'], 'media_links': plan['links'], 'metadata': plan['metadata'],
                    'codes': plan['codes'],
                    'moved': [], 'results': []}
        manifest_path = quarantine / 'manifest.json'
        _write_manifest(manifest_path, manifest)
        _save_metadata(plan)
        # Check exact top-level names as well as every original descendant. A
        # new external file appearing after preview must never be swept away.
        expected = {item[0]: item[1] for item in plan['manifest']}
        current_tree, _, blockers = _tree(output)
        if blockers or current_tree != plan['manifest']:
            raise ValueError('输出目录在清理前变化，停止移动；请检查隔离目录')
        for child in sorted(output.iterdir()):
            if str(child.relative_to(output)) not in expected or _unsafe_links(child):
                raise ValueError('输出目录中出现未预览的文件，停止重建')
            os.replace(child, storage / child.name)
            manifest['moved'].append(child.name)
            # Append a small durable move journal instead of rewriting the full
            # thousands-of-files manifest for every renamed top-level folder.
            with (quarantine / 'moves.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'name': child.name}, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        source_stamps = {}
        for source in plan['sources']:
            stamp = _stamp(source['path'])
            # Moving an old hard-link name can update the shared inode's ctime;
            # its identity, bytes and mtime must still match the preview.
            if stamp[:4] != source['stamp'][:4]:
                raise ValueError('移动旧产物期间源视频发生变化，停止重建')
            source_stamps[source['path']] = stamp
        _map_cache_paths(output, storage)
        # Only this output's associations are retired. Download history,
        # subscriptions and Code metadata remain intact.
        with session_scope() as session:
            for old in plan['links']:
                row = session.get(MediaLink, old['link_path'])
                if row is not None:
                    session.delete(row)
            for cached in session.scalars(select(ScrapeSourceVideo)):
                cached.links = [path for path in (cached.links or []) if not _under(path, output)]
                if not cached.links:
                    cached.artifact_status = 'pending'
        manifest['phase'] = 'rebuilding'
        _write_manifest(manifest_path, manifest)
        _update(phase='rebuilding')
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
        _update(total=len(groups))
        attempt_lock = threading.Lock()
        job_id = _STATE.get('job_id', '')
        def rebuild(code, items):
            with attempt_lock:
                scrapesources.record_attempt(items, code, status='running', job_id=job_id)
            try:
                for item in items:
                    if _unsafe_links(item.path) or _stamp(item.path) != source_stamps[str(item.path)]:
                        raise ValueError('源视频已变化，停止此影片的重建')
                with _maintenance().internal():
                    result = scrape.scrape_group(code, items, refresh_metadata=refresh_metadata,
                                                 preserved_meta=preserved.get(code))
                row = {'code': code, 'ok': result.ok, 'error': result.error,
                       'warnings': result.warnings, 'links': [str(path) for path in result.links]}
            except Exception as exc:
                logger.exception(f'[{code}] 全量重建失败，旧产物保留在隔离目录')
                row = {'code': code, 'ok': False, 'error': str(exc), 'warnings': [], 'links': []}
            with attempt_lock:
                scrapesources.record_attempt(items, code, status='succeeded' if row['ok'] else 'failed',
                                             job_id=job_id, error=row['error'])
            return row
        # Keep the executor's internal queue bounded too; a large library must
        # not create thousands of futures while only two films can progress.
        queue = iter(groups.items())
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
                manifest['results'].append(row)
                with (quarantine / 'results.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                    stream.flush()
                with _LOCK:
                    _STATE['done'] += 1
                    _STATE['ok'] += int(row['ok'])
                    _STATE['results'] = manifest['results'][-200:]
                    _persist_state()
                entry = next(queue, None)
                if entry:
                    pending.add(pool.submit(rebuild, *entry))
        failed = sum(not row['ok'] for row in manifest['results'])
        manifest['phase'] = 'completed' if not failed else 'partial'
        _write_manifest(manifest_path, manifest)
        _update(phase=manifest['phase'], error=f'{failed} 部重建失败，旧产物保留在隔离目录，可手动恢复' if failed else '')
    except Exception as exc:
        logger.exception('全量重建未完成；下载源与旧隔离产物保留')
        if manifest is not None and quarantine is not None:
            manifest['phase'], manifest['error'] = 'failed', str(exc)
            try:
                _write_manifest(quarantine / 'manifest.json', manifest)
            except Exception:
                logger.exception('无法更新隔离目录恢复清单')
        _update(phase='failed', error=str(exc))
    finally:
        try:
            _update(running=False)
        finally:
            _maintenance().release()
