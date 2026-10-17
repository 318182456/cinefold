"""重复入库检查与内容校验合并；不调用联动删除或下载器删除接口。"""
from __future__ import annotations

from app.services.scrapemaintenance import guarded as maintenance_guarded

import hashlib
import copy
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from loguru import logger

from app.core.config import get_settings
from app.database.models import MediaLink, MediaMerge, PendingDelete, ScrapeSourceVideo
from app.database.session import session_scope
from app.modules.scrape.merge import base_code
from app.utils import get_true_code, mediafile
from app.services.filehash import identity, signature, stable_digest

LOCK = threading.RLock()
_JOBS_LOCK = threading.Lock()
_JOBS = {}
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='media-duplicate')


def start_job(action):
    with _JOBS_LOCK:
        for key, value in list(_JOBS.items()):
            if not value['running'] and time.monotonic() - value['updated'] > 1800:
                _JOBS.pop(key)
        if sum(j['running'] for j in _JOBS.values()) >= 2:
            raise ValueError('已有重复影片校验或合并正在进行，请完成后再试')
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = {'running': True, 'updated': time.monotonic(), 'result': None, 'error': ''}
    def run():
        result, error = None, ''
        try:
            result = action()
        except Exception as exc:
            logger.exception('重复影片检查或合并失败')
            error = str(exc)
        with _JOBS_LOCK:
            _JOBS[job_id].update(running=False, updated=time.monotonic(), result=result, error=error)
    _POOL.submit(run)
    return {'job_id': job_id}


def job_state(job_id):
    with _JOBS_LOCK:
        if job_id not in _JOBS:
            raise ValueError('任务不存在或已过期，请重新检查')
        job = _JOBS[job_id]
        return {k: job[k] for k in ('running', 'result', 'error')}


_SCAN_LOCK = threading.Lock()
_SCAN_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='media-global-duplicate')
_SCAN_CANCEL = threading.Event()
_SCAN_STATE = {'scan_id': '', 'running': False, 'cancelled': False, 'phase': 'idle',
               'done': 0, 'total': 0, 'hashed_files': 0, 'hash_candidates': 0,
               'hash_bytes': 0, 'hash_bytes_total': 0, 'result': None, 'error': ''}


def _registered():
    with session_scope() as session:
        return [dict(row._mapping) for row in session.execute(select(
            MediaLink.link_path, MediaLink.source_path, MediaLink.code).order_by(MediaLink.link_path))]


def _registry_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scan_registered(*, hashes=False, cancelled=None, progress=None):
    """全表快照，只读已登记路径；同大小的独立物理文件才读完整内容。"""
    rows = _registered()
    registry = _registry_digest(rows)
    cancelled = cancelled or threading.Event()
    physical, by_size, by_code, entries = {}, defaultdict(list), defaultdict(list), []
    counters = {'done': 0, 'total': len(rows), 'hashed_files': 0, 'hash_candidates': 0,
                'hash_bytes': 0, 'hash_bytes_total': 0}

    def emit(phase=None):
        if cancelled.is_set():
            raise InterruptedError('已取消重复检索')
        if progress is not None:
            progress(**counters, **({'phase': phase} if phase else {}))

    emit('inventory')
    for row in rows:
        emit()
        code = get_true_code(row['code']) or row['code']
        core = base_code(code) or code
        entry = {**row, 'base_code': core, 'link_exists': False, 'part': 0,
                 'version': '', 'size': None, 'error': '', 'sha256': None}
        try:
            path = Path(row['link_path'])
            if path.is_symlink() or not path.is_file():
                raise ValueError('不是可访问的普通文件，请检查路径、挂载与权限')
            stat = path.stat()
            info = _file_info(path)
            entry.update(link_exists=True, size=stat.st_size, part=info.part,
                         version=('无码/破解' if info.uncensored else '普通') + (' · 字幕' if info.subbed else ''),
                         inode=stat.st_ino, device=stat.st_dev, _stat=stat,
                         _identity=identity(path, stat), _signature=signature(stat))
            if entry['_identity'] not in physical:
                physical[entry['_identity']] = entry
                by_size[stat.st_size].append(entry)
        except (OSError, ValueError) as exc:
            entry['error'] = str(exc)
        entries.append(entry)
        by_code[core].append(entry)
        counters['done'] += 1
        emit()

    candidates = [entry for size, group in by_size.items() if size and len(group) > 1 for entry in group]
    counters.update(hash_candidates=len(candidates), hash_bytes_total=sum(x['size'] for x in candidates))
    if hashes:
        emit('hash')
        for entry in candidates:
            emit()
            def read_progress(count):
                counters['hash_bytes'] += count
                emit()
            try:
                entry['sha256'] = stable_digest(entry['link_path'], expected=entry['_stat'],
                                                cancelled=cancelled, progress=read_progress)
            except InterruptedError:
                raise
            except (OSError, ValueError) as exc:
                entry['error'] = str(exc)
            counters['hashed_files'] += 1
            emit()
    emit('finalizing')
    # 硬链接别名复用同一份哈希，但每个路径都重新校验，不能复用已移动/替换路径。
    for entry in entries:
        emit()
        if not entry['link_exists']:
            continue
        try:
            if signature(Path(entry['link_path']).stat()) != entry['_signature']:
                raise ValueError('扫描期间文件发生变化，请重新检索')
            representative = physical[entry['_identity']]
            entry['sha256'] = representative['sha256']
            if representative['error']:
                entry['error'] = representative['error']
        except (OSError, ValueError) as exc:
            entry.update(error=str(exc), sha256=None, changed=True)

    def group_result(code, files, kind='code'):
        live = [x for x in files if x['link_exists'] and not x['error']]
        inodes, digests = defaultdict(list), defaultdict(list)
        parts, part_sizes = defaultdict(int), defaultdict(set)
        for item in live:
            inodes[item['_identity']].append(item)
            parts[item['part']] += 1
            part_sizes[item['part']].add(item['size'])
            if item['sha256']:
                digests[item['sha256']].append(item)
        result = []
        for item in files:
            if item['error']:
                match = 'changed' if item.get('changed') else 'unavailable'
            elif len(inodes[item['_identity']]) > 1:
                match = 'hardlink'
            elif item['sha256'] and len(digests[item['sha256']]) > 1:
                match = 'same_content'
            elif (hashes and item['sha256']) or len(part_sizes[item['part']]) > 1:
                match = 'different_content'
            elif len(live) > 1 and parts[item['part']] == 1:
                match = 'different_part'
            else:
                match = 'unverified'
            result.append({k: v for k, v in item.items() if not k.startswith('_')} | {'match_type': match})
        group_id = hashlib.sha256(json.dumps(sorted(x['link_path'] for x in files)).encode()).hexdigest()[:20]
        return {'id': f'{kind}:{group_id}', 'code': code, 'kind': kind, 'codes': sorted({x['code'] for x in files}),
                'links': result, 'count': len(files), 'parts': sorted(parts),
                'merge_available': kind == 'code' and any(n > 1 for n in parts.values()),
                'hardlink_groups': sum(len(x) > 1 for x in inodes.values()),
                'same_content_groups': sum(len({x['_identity'] for x in group}) > 1 for group in digests.values())}

    groups = [group_result(code, files) for code, files in sorted(by_code.items()) if len(files) > 1]
    # 相同物理文件/完整内容却登记为不同番号，也显示出来；不跨番号自动套用合并。
    content = defaultdict(list)
    for entry in entries:
        if entry['link_exists'] and not entry['error']:
            key = ('hash', entry['sha256']) if entry['sha256'] else ('inode', entry['_identity'])
            content[key].append(entry)
    for files in content.values():
        if len({x['base_code'] for x in files}) > 1:
            groups.append(group_result('', files, 'cross_code'))
    return {'groups': groups, 'registered': len(rows), 'physical_files': len(physical),
            'unavailable': sum(not x['link_exists'] for x in entries),
            'read_errors': sum(bool(x['error']) and x['link_exists'] for x in entries),
            'file_changes': sum(bool(x.get('changed')) for x in entries), 'hashes': hashes,
            'registry_snapshot': registry, 'registry_changed': registry != _registry_digest(_registered()),
            'scope': '全部已登记的媒体库关联；不遍历未登记磁盘文件', **counters}


def scan_state(*, page=1, size=10):
    page, size = max(1, page), max(1, min(50, size))
    with _SCAN_LOCK:
        state = {k: copy.deepcopy(v) for k, v in _SCAN_STATE.items() if k != 'result'}
        result = _SCAN_STATE.get('result')
        total = len(result['groups']) if result else 0
        page = min(page, max(1, (total + size - 1) // size))
        state['groups'] = copy.deepcopy(result['groups'][(page - 1) * size:page * size]) if result else []
        state['summary'] = {k: v for k, v in result.items() if k != 'groups'} if result else None
    if state['summary']:
        state['summary']['registry_changed'] = state['summary']['registry_changed'] or (
            state['summary']['registry_snapshot'] != _registry_digest(_registered()))
    return {**state, 'page': page, 'size': size, 'group_total': total,
            'scope': '全部已登记的媒体库关联；不遍历未登记磁盘文件'}


def start_scan(*, hashes=False):
    global _SCAN_CANCEL
    with _SCAN_LOCK:
        if _SCAN_STATE['running']:
            return {'scan_id': _SCAN_STATE['scan_id'], 'running': True, 'hashes': _SCAN_STATE['hashes']}
        scan_id = uuid.uuid4().hex
        stop = _SCAN_CANCEL = threading.Event()
        _SCAN_STATE.update(scan_id=scan_id, running=True, cancelled=False, phase='queued', hashes=hashes,
                           done=0, total=0, hashed_files=0, hash_candidates=0, hash_bytes=0, hash_bytes_total=0,
                           result=None, error='', started_at=datetime.now().isoformat(timespec='seconds'), finished_at=None)

    def progress(**values):
        with _SCAN_LOCK:
            _SCAN_STATE.update(values)

    def run():
        result, error, was_cancelled = None, '', False
        try:
            result = scan_registered(hashes=hashes, cancelled=stop, progress=progress)
            if stop.is_set():
                raise InterruptedError('已取消重复检索')
        except InterruptedError:
            was_cancelled = True
        except Exception as exc:
            logger.exception('全局重复检索失败')
            error = str(exc)
        with _SCAN_LOCK:
            was_cancelled = was_cancelled or stop.is_set()
            _SCAN_STATE.update(running=False, result=None if was_cancelled else result,
                               cancelled=was_cancelled, error=error,
                               phase='cancelled' if was_cancelled else ('failed' if error else 'finished'),
                               finished_at=datetime.now().isoformat(timespec='seconds'))
    _SCAN_POOL.submit(run)
    return {'scan_id': scan_id, 'running': True, 'hashes': hashes}


def cancel_scan(scan_id):
    with _SCAN_LOCK:
        if not scan_id or scan_id != _SCAN_STATE['scan_id']:
            raise ValueError('检索任务已变化，请刷新后再取消')
        if _SCAN_STATE['running']:
            _SCAN_CANCEL.set()
            _SCAN_STATE['phase'] = 'cancelling'
    return {'scan_id': scan_id, 'cancel_requested': True}


def rows_for(code):
    code = get_true_code(code)
    if not code:
        raise ValueError('请填写有效番号')
    core = base_code(code) or code
    from app.modules.scrape.merge import product_alias
    cores = [core] + ([product_alias(core)] if product_alias(core) else [])
    variants = [value + suffix for value in cores for suffix in ('', '-C', '-CH', '-U', '-UC', '-UCH')]
    with session_scope() as session:
        return list(session.scalars(select(MediaLink).where(MediaLink.code.in_(variants))
                                   .order_by(MediaLink.create_time, MediaLink.link_path)))


def _file_info(path):
    info = mediafile.parse(path)
    if info.code.endswith('-C'):
        from app.services.scrape import inspect_source
        return inspect_source(path)
    return info


class DuplicateTargets(dict):
    def __init__(self):
        super().__init__()
        self.actions = {}


def check_existing(code, items, provided=None):
    """Reuse proven identical copies; keep subtitle/uncensored versions distinct."""
    rows = rows_for(code)
    from app.services import duplicatepolicy
    targets, conflicts = DuplicateTargets(), []
    for info in items:
        if info.trailer or info.ignored:
            continue
        candidates, versions, identical, retained = [], [], [], []
        for row in rows:
            target = Path(row.link_path)
            if not target.is_file():
                continue
            # 已登记的同一物理源（或合并时验证过的源）优先复用原位置。
            # 源名和产物名的分段/版本标记可能不同，不能因此误报新副本。
            if target.samefile(info.path) or verified_existing(info.path, target):
                identical.append(target)
                continue
            if duplicatepolicy.retained(info.path, target):
                retained.append(target)
                continue
            other = _file_info(target)
            if other.part != info.part:
                continue
            if target.stat().st_size == info.path.stat().st_size and _digest(target) == _digest(info.path):
                identical.append(target)
                continue
            from app.services.subtitle import _has_sidecar
            other.subbed |= _has_sidecar(target)
            (versions if other.subbed != info.subbed or other.uncensored != info.uncensored else candidates).append(target)
        if identical:
            preferred = (provided or {}).get(str(info.path))
            chosen = preferred if preferred in identical else identical[0]
            if not chosen.samefile(info.path) and not verified_existing(info.path, chosen):
                _remember_copy(info.path, chosen)
            targets[str(info.path)] = chosen
        elif retained:
            targets[str(info.path)] = retained[0]
            targets.actions[str(info.path)] = {'action':'keep', 'reason':'沿用已确认的重复处理结果', 'remember':False}
        elif candidates or versions:
            # Different encodes/edits are not interchangeable. Retain both,
            # using a deterministic source label rather than a destructive merge.
            choice = duplicatepolicy.policy() if candidates else 'versions'
            action, selected, reason = duplicatepolicy.prefer(info.path, candidates or versions, choice)
            existing = _library_file(selected)
            targets.actions[str(info.path)] = {'action':action, 'reason':reason, 'remember':True,
                                              'signature':_signature(existing), 'policy':choice}
            if action == 'variant':
                label = hashlib.sha256(str(info.path).encode()).hexdigest()[:10]
                stem = info.code + (f'-CD{info.part}' if info.part else '')
                targets[str(info.path)] = existing.with_name(f'{stem} - version-{label}{info.path.suffix}')
            else:
                targets[str(info.path)] = existing
        elif (provided or {}).get(str(info.path)):
            # 保留同一源文件原有位置；不同编号的旧关联不能复用。
            preferred = Path(provided[str(info.path)])
            if preferred.is_file() and preferred.samefile(info.path) and any(r.link_path == str(preferred) for r in rows):
                targets[str(info.path)] = preferred
    return targets, conflicts


def _remember_copy(source, target):
    """Record a verified source alias without changing/deleting either video."""
    source, target = Path(source), _library_file(target)
    if source.is_symlink():
        raise ValueError('重复下载源不能是符号链接')
    before, kept = _signature(source), _signature(target)
    if before[2] != kept[2] or _digest(source) != _digest(target):
        raise ValueError('视频内容变化，无法复用已有影片')
    _copy_subtitles(source, target)
    if _signature(source) != before or _signature(target) != kept:
        raise ValueError('视频校验期间发生变化，停止自动合并')
    with session_scope() as session:
        row = session.get(MediaMerge, str(source))
        if row is None:
            row = MediaMerge(old_path=str(source), keep_path=str(target))
            session.add(row)
        row.keep_path, row.source_path = str(target), str(source)
        row.source_signature, row.keep_signature = before, kept
    logger.info(f'内容完全相同，自动复用已有影片: {source} → {target}；下载源保留')


def source_aliases(session, source=None):
    """Snapshot for inventory/watch scans; signatures are checked by _complete."""
    query = select(MediaMerge)
    if source is not None:
        query = query.where(MediaMerge.source_path == str(source))
    return [(row.source_path, row.keep_path) for row in session.scalars(query)
            if row.source_path and row.source_signature and row.keep_signature]


def verified_existing(source, target):
    """合并时已逐字节校验的源文件映射；任一文件变化即失效。"""
    with session_scope() as session:
        records = list(session.scalars(select(MediaMerge).where(
            MediaMerge.source_path == str(source), MediaMerge.keep_path == str(target))))
    return any(row.source_signature == _signature(source) and row.keep_signature == _signature(target)
               for row in records)


def _signature(path):
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _digest(path):
    return stable_digest(path)


def _library_file(path):
    settings = get_settings()
    path = Path(path)
    roots = [Path(p).resolve() for p in (settings.medialink_scrape_dir, settings.medialink_library_path) if p]
    if path.is_symlink() or not path.is_file() or not any(path.resolve().is_relative_to(root) for root in roots):
        raise ValueError(f'只能合并已登记且位于媒体库内的普通视频文件: {path}')
    from app.services.scrapeinventory import configured
    downloads = configured() + [p for p in (settings.qbittorrent_download_path, settings.transmission_download_path) if p]
    if any(path.resolve().is_relative_to(Path(p).resolve()) for p in downloads):
        raise ValueError(f'下载源目录中的文件不能作为合并清理项: {path}')
    return path


def preview(code, keep_path):
    rows = rows_for(code)
    keep = next((r for r in rows if r.link_path == keep_path), None)
    if keep is None:
        raise ValueError('保留文件不在该番号的关联记录中')
    target = _library_file(keep_path)
    original = _signature(target)
    keep_info = _file_info(target)
    digest = None
    duplicates, skipped = [], []
    for row in rows:
        if row.link_path == keep_path:
            continue
        candidate = Path(row.link_path)
        try:
            candidate = _library_file(candidate)
            if _file_info(candidate).part != keep_info.part:
                skipped.append({'path': str(candidate), 'reason': '不同分段，保留'})
                continue
            signature = _signature(candidate)
            same = candidate.samefile(target)
            if not same and signature[2] == original[2]:
                digest = digest or _digest(target)
                same = _digest(candidate) == digest
            if not same:
                skipped.append({'path': str(candidate), 'reason': '视频内容不同，保留'})
                continue
            duplicates.append({'path': str(candidate), 'source_path': row.source_path, 'signature': signature})
        except (OSError, ValueError) as exc:
            skipped.append({'path': str(candidate), 'reason': str(exc)})
    if _signature(target) != original:
        raise ValueError('保留文件发生变化，请重新检查')
    data = {'code': get_true_code(code), 'keep_path': keep_path, 'keep_signature': original,
            'keep_source': keep.source_path, 'duplicates': duplicates, 'skipped': skipped}
    data['token'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data


def _copy_subtitles(source, target):
    """补齐字幕；同名而内容不同的字幕另存，旧文件也保留。"""
    for path in source.parent.iterdir():
        if path.suffix.lower() not in ('.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt'):
            continue
        if not path.name.startswith(source.stem + '.') or path.is_symlink() or not path.is_file():
            continue
        suffix = path.name[len(source.stem):]
        dest = target.with_name(target.stem + suffix)
        if dest.exists():
            if _digest(dest) == _digest(path):
                continue
            tag = _digest(path)[:12]
            dest = target.with_name(target.stem + '.merged-' + tag + suffix)
            if dest.exists():
                if _digest(dest) == _digest(path):
                    continue
                raise ValueError(f'字幕目标冲突，请先处理: {dest}')
        try:
            with path.open('rb') as src, dest.open('xb') as out:
                shutil.copyfileobj(src, out)
        except FileExistsError:
            raise ValueError(f'字幕目标发生变化，请重新检查: {dest}')


@maintenance_guarded
def merge(code, keep_path, token):
    from app.services.scrape import _SCRAPE_LOCK
    with LOCK, _SCRAPE_LOCK:
        plan = preview(code, keep_path)
        if not token or token != plan['token']:
            raise ValueError('文件或关联记录已变化，请重新检查并确认')
        if not plan['duplicates']:
            raise ValueError('没有内容完全相同的重复影片可合并')
        # 先持久化旧路径保护，后续 Emby 延迟回调不能回退到保留项。
        with session_scope() as session:
            for item in plan['duplicates']:
                row = session.get(MediaMerge, item['path'])
                if row:
                    row.keep_path = keep_path
                else:
                    row = MediaMerge(old_path=item['path'], keep_path=keep_path)
                    session.add(row)
                source = item['source_path']
                # 内容相同的两个独立文件也可合并；记录已验证的下载源，
                # 后续重刮能继续复用保留位置，不会再次要求合并。
                if source and source != item['path'] and Path(source).is_file():
                    candidate = Path(item['path'])
                    if Path(source).samefile(candidate) or (_signature(source)[2] == candidate.stat().st_size
                                                           and _digest(source) == _digest(candidate)):
                        row.source_path = source
                        row.source_signature = _signature(source)
                        row.keep_signature = plan['keep_signature']
        removed = []
        for item in plan['duplicates']:
            old, target = _library_file(item['path']), _library_file(keep_path)
            _copy_subtitles(old, target)
            if _signature(old) != item['signature'] or _signature(target) != plan['keep_signature']:
                raise ValueError('文件发生变化，停止合并，请刷新列表后重新检查')
            verified_source = (item['source_path'] != str(old)
                               and verified_existing(item['source_path'], keep_path))
            # 只取消库内这一个文件名；不删种子、不调用源文件或附属清理。
            old.unlink()
            logger.info(f'[{code}] 合并重复媒体入口: {old} → {keep_path}，下载源与种子保留')
            removed.append(str(old))
            with session_scope() as session:
                row = session.get(MediaLink, str(old))
                if row:
                    session.delete(row)
                keeper = session.get(MediaLink, keep_path)
                if keeper and keeper.source_path == keep_path and verified_source:
                    keeper.source_path = item['source_path']
                    try:
                        stat = Path(keeper.source_path).stat()
                        keeper.device, keeper.inode = stat.st_dev, stat.st_ino
                    except OSError:
                        keeper.device, keeper.inode = None, None
                if keeper:
                    keeper.size_probe_time = None
                hold = session.get(PendingDelete, str(old))
                if hold:
                    session.delete(hold)
                for video in session.scalars(select(ScrapeSourceVideo)):
                    if str(old) in (video.links or []):
                        video.links = list(dict.fromkeys(keep_path if p == str(old) else p for p in video.links))
                # 其他记录把旧库文件当作源时，改指向保留文件。
                for row in session.scalars(select(MediaLink).where(MediaLink.source_path == str(old))):
                    row.source_path = keep_path
                    stat = target.stat()
                    row.device, row.inode = stat.st_dev, stat.st_ino
        return {'keep_path': keep_path, 'removed': removed, 'skipped': plan['skipped']}
