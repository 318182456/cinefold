"""重复入库检查与内容校验合并；不调用联动删除或下载器删除接口。"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from loguru import logger

from app.core.config import get_settings
from app.database.models import MediaLink, MediaMerge, PendingDelete, ScrapeSourceVideo
from app.database.session import session_scope
from app.modules.scrape.merge import base_code
from app.utils import get_true_code, mediafile

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


def rows_for(code):
    code = get_true_code(code)
    if not code:
        raise ValueError('请填写有效番号')
    core = base_code(code) or code
    variants = [core] + [core + suffix for suffix in ('-C', '-CH', '-U', '-UC', '-UCH')]
    with session_scope() as session:
        return list(session.scalars(select(MediaLink).where(MediaLink.code.in_(variants))
                                   .order_by(MediaLink.create_time, MediaLink.link_path)))


def _file_info(path):
    info = mediafile.parse(path)
    if info.code.endswith('-C'):
        from app.services.scrape import inspect_source
        return inspect_source(path)
    return info


def check_existing(code, items, provided=None):
    """仅探测同番号登记项；同文件复用，不同文件暂停，不按大小猜内容。"""
    rows = rows_for(code)
    targets, conflicts = {}, []
    for info in items:
        if info.trailer or info.ignored:
            continue
        candidates, identical = [], []
        for row in rows:
            target = Path(row.link_path)
            if not target.is_file():
                continue
            other = _file_info(target)
            if other.part != info.part or other.uncensored != info.uncensored:
                continue
            candidates.append(target)
            if target.samefile(info.path) or verified_existing(info.path, target):
                identical.append(target)
        if identical:
            preferred = (provided or {}).get(str(info.path))
            targets[str(info.path)] = preferred if preferred in identical else identical[0]
        elif candidates:
            conflicts.append(f'媒体库已存在同番号、同分段文件，请先检查重复影片并合并或确认版本: {candidates[0]}')
        elif (provided or {}).get(str(info.path)):
            # 保留同一源文件原有位置；不同编号的旧关联不能复用。
            preferred = Path(provided[str(info.path)])
            if preferred.is_file() and preferred.samefile(info.path) and any(r.link_path == str(preferred) for r in rows):
                targets[str(info.path)] = preferred
    return targets, conflicts


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
    before = _signature(path)
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            value.update(chunk)
    if _signature(path) != before:
        raise ValueError(f'校验期间文件发生变化，请等待下载完成: {path}')
    return value.hexdigest()


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
