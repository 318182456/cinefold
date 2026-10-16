"""下载源视频列表、指定番号与后台手动刮削任务。"""
from __future__ import annotations

import copy
import os
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import and_, case, exists, func, literal, or_, select

from app.core.config import get_settings, save_settings
from app.database.models import Code, CodeStatus, MediaLink, ScrapeAttempt, ScrapeSourceVideo, ScrapeSourceMembership, ScrapeSourceRoot
from app.database.session import session_scope
from app.services import scrape, scrapewatch, scrapeinventory
from app.services.medialink import is_adoptable_video
from app.utils import get_true_code, mediafile

_LOCK = threading.RLock()
_CONFIG_LOCK = threading.Lock()
_STATE = {"running": False, "job_id": "", "total": 0, "done": 0, "ok": 0,
          "current": "", "results": [], "error": "", "cancel_requested": False}


def key(path) -> str:
    return scrapeinventory.key(path)


def configured() -> list[str]:
    return scrapeinventory.configured()


def _root(path: str) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise ValueError("请填写容器内的绝对路径")
    if not root.is_dir():
        raise ValueError(f"目录不存在或不可访问: {root}")
    root = root.resolve()
    settings = get_settings()
    output = settings.medialink_scrape_dir or settings.medialink_library_path
    if output:
        target = Path(output).resolve()
        if root.is_relative_to(target) or target.is_relative_to(root):
            raise ValueError("下载源目录与刮削输出目录不能重叠")
    return root


def roots(probe: bool = False) -> list[dict]:
    if not probe:
        return scrapeinventory.roots()
    result = []
    for raw in configured():
        try:
            root = _root(raw)
            result.append({"path": raw, "resolved": str(root), "available": True, "error": ""})
        except (ValueError, OSError) as exc:
            result.append({"path": raw, "resolved": "", "available": False, "error": str(exc)})
    return result


def add_root(path: str) -> list[dict]:
    if any(c in path for c in (',', '\r', '\n')):
        raise ValueError("请一次添加一个目录，目录名不能包含逗号或换行")
    root = _root(path.strip())
    with _CONFIG_LOCK:
        existing = configured()
        if all(os.path.normcase(str(Path(p).resolve())) != os.path.normcase(str(root)) for p in existing):
            save_settings({"SCRAPE_SOURCE_DIRS": ','.join([*existing, str(root)])})
            get_settings(reload=True)
    return roots()


def remove_root(path: str) -> list[dict]:
    with _CONFIG_LOCK:
        existing = configured()
        if path not in existing:
            raise ValueError("此目录未配置")
        save_settings({"SCRAPE_SOURCE_DIRS": ','.join(p for p in existing if p != path)})
        get_settings(reload=True)
    return roots()


def validate_file(raw: str) -> Path:
    path = Path(raw)
    if mediafile.is_advertisement(path.stem):
        raise ValueError('广告视频已跳过，不参与刮削')
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not is_adoptable_video(path):
        raise ValueError(f"不是可处理的源视频: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"视频为空文件: {path.name}")
    path = path.resolve()
    if not any(r['available'] and path.is_relative_to(Path(r['resolved'])) for r in roots(probe=True)):
        raise ValueError(f"视频不在当前有效的下载源目录中: {path}")
    return path


def state() -> dict:
    with _LOCK:
        return copy.deepcopy(_STATE)


def record_attempt(items, code: str, *, status: str, job_id: str = '', error: str = ''):
    try:
        with session_scope() as session:
            for item in items:
                path = item.path if hasattr(item, 'path') else Path(item['path'])
                session.merge(ScrapeAttempt(source_key=key(path), source_path=str(path), code=code,
                    job_id=job_id, status=status, error=error, updated_at=datetime.now()))
    except Exception:
        logger.exception(f"[{code}] 保存刮削记录失败")


def group_entries(entries: list[dict]):
    groups = defaultdict(list)
    for entry in entries:
        info = mediafile.parse(Path(entry['path']))
        if info.ignored:
            raise ValueError('广告视频已跳过，不参与刮削')
        if entry.get('code'):
            manual = get_true_code(entry['code'])
            if not manual:
                raise ValueError(f"无效的指定番号: {entry['code']}")
            info.code, info.western = manual, False
            info.subbed |= manual.endswith(('-C', '-CH', '-UCH'))
            info.uncensored |= manual.endswith(('-U', '-UC', '-UCH'))
        if not info.code:
            raise ValueError(f"请先为视频指定番号: {info.path.name}")
        groups[info.code].append(info)
    for items in groups.values():
        items.sort(key=lambda i: (i.part, i.path.name, str(i.path)))
        if len(items) > 1 and all(i.part == 0 for i in items):
            for index, info in enumerate(items, 1):
                info.part = index
        mediafile._resolve_collisions(items)
    return groups


def video_detail(path: str) -> dict:
    """读取当前源清单和已保存的刮削资料，不扫描目录或触发刮削。"""
    root_keys = [r['root_key'] for r in roots()]
    with session_scope() as session:
        video = session.scalar(select(ScrapeSourceVideo).where(
            ScrapeSourceVideo.source_key == key(path),
            exists(select(ScrapeSourceMembership.source_key).where(
                ScrapeSourceMembership.source_key == ScrapeSourceVideo.source_key,
                ScrapeSourceMembership.root_key.in_(root_keys)))))
        if video is None:
            raise ValueError('视频不在当前下载源清单中，请更新清单后重试')
        attempt = session.get(ScrapeAttempt, video.source_key)
        code = '' if video.artifact_status == 'ignored' else (attempt.code if attempt else '') or video.detected_code
        row = session.get(Code, code) if code else None
        return {'path': video.path, 'name': video.name, 'code': code, 'size': video.size,
                'part': video.part, 'links': video.links, 'metadata': row.to_dict() if row else {},
                'error': attempt.error if attempt else '',
                'last_run': attempt.updated_at.isoformat() if attempt else None,
                'indexed_at': video.indexed_at.isoformat()}


def list_videos(*, root: str = '', keyword: str = '', status: str = '', page: int = 1, size: int = 30) -> dict:
    """只从持久化清单查询、筛选和分页，不触碰磁盘。"""
    configured_roots = roots()
    if root and root not in {r['path'] for r in configured_roots}:
        raise ValueError("所选目录未配置")
    root_keys = [r['root_key'] for r in configured_roots if not root or r['path'] == root]
    job = state()
    video, attempt = ScrapeSourceVideo, ScrapeAttempt
    number = case((video.artifact_status == 'ignored', ''), else_=func.coalesce(func.nullif(attempt.code, ''), video.detected_code))
    title = func.coalesce(func.nullif(Code.cn_title, ''), Code.title, '')
    active = or_(and_(attempt.job_id == job['job_id'], literal(job['running'])),
                 and_(attempt.job_id == '', literal(scrapewatch._LOCK.locked())))
    queued = case((active, attempt.status), else_='interrupted')
    downloading = video.detected_code.in_(select(Code.code).where(Code.status == CodeStatus.DOWNLOADING).correlate(None))
    current = case(
        (video.artifact_status == 'ignored', 'ignored'),
        (or_(Code.status == CodeStatus.DOWNLOADING, downloading), 'downloading'),
        (video.size == 0, 'empty'),
        (attempt.status.in_(['queued', 'running']), queued),
        (attempt.status.in_(['failed', 'cancelled']), attempt.status),
        (number == '', 'unrecognized'), else_=video.artifact_status,
    )
    membership = exists(select(ScrapeSourceMembership.source_key).where(
        ScrapeSourceMembership.source_key == video.source_key, ScrapeSourceMembership.root_key.in_(root_keys)))
    query = select(video, number.label('number'), title.label('title'), current.label('current'),
                   attempt.updated_at, attempt.error).outerjoin(attempt, attempt.source_key == video.source_key).outerjoin(Code, Code.code == number).where(membership)
    needle = keyword.strip()
    if needle:
        query = query.where(or_(video.path.icontains(needle, autoescape=True), number.icontains(needle, autoescape=True), title.icontains(needle, autoescape=True)))
    with session_scope() as session:
        summary = query.with_only_columns(current.label('status')).order_by(None).subquery()
        counts = dict(session.execute(select(summary.c.status, func.count()).group_by(summary.c.status)).all())
        filtered = query.where(current == status) if status else query
        total = session.scalar(select(func.count()).select_from(filtered.order_by(None).subquery()))
        rows = []
        for cached, code, name, label, last_run, error in session.execute(filtered.order_by(video.mtime.desc(), video.path).offset((page - 1) * size).limit(size)):
            rows.append({'path': cached.path, 'name': cached.name, 'root': root, 'code': code,
                'detected_code': cached.detected_code, 'title': name, 'part': cached.part, 'size': cached.size,
                'mtime': cached.mtime, 'status': label, 'links': cached.links,
                'last_run': last_run.strftime('%Y-%m-%d %H:%M:%S') if last_run else None,
                'error': error or '', 'indexed_at': cached.indexed_at.isoformat()})
    return {'roots': configured_roots, 'items': rows, 'total': total, 'page': page, 'size': size,
            'counts': counts, 'warnings': [r['error'] for r in configured_roots if r['error']], 'job': job,
            'inventory': scrapeinventory.state(), 'needs_refresh': any(r['last_scan'] is None for r in configured_roots)}


def start(entries: list[dict], *, overwrite: bool = False, refresh_metadata: bool = False, fetch_meta: bool = True) -> dict:
    if refresh_metadata and not fetch_meta:
        raise ValueError("重新获取元数据时需要开启联网")
    normalized, seen = [], set()
    with session_scope() as session:
        attempts = {a.source_key: a.code for a in session.scalars(select(ScrapeAttempt))}
        downloading = set(session.scalars(select(Code.code).where(Code.status == CodeStatus.DOWNLOADING)))
    for entry in entries:
        path = validate_file(entry['path'])
        identity = key(path)
        if identity in seen:
            continue
        seen.add(identity)
        info = mediafile.parse(path)
        code = entry.get('code', '').strip() or attempts.get(identity) or info.code
        code = get_true_code(code) if code else ''
        if not code:
            raise ValueError(f"请填写有效番号: {path.name}")
        if code in downloading or info.code in downloading:
            raise ValueError(f"番号仍标记为下载中，请下载完成后再处理: {code}")
        normalized.append({'path': str(path), 'code': code})
    if not normalized:
        raise ValueError("请选择视频")
    groups = group_entries(normalized)
    settings = get_settings()
    output = settings.medialink_scrape_dir or settings.medialink_library_path
    if not output or not Path(output).is_absolute():
        raise ValueError("请先配置绝对路径的刮削输出目录")
    from app.api.endpoints.watchdir import _check_same_fs
    for entry in normalized:
        ok, message = _check_same_fs(Path(entry['path']), Path(output))
        if not ok:
            raise ValueError(message)
    with _LOCK:
        if _STATE['running']:
            raise ValueError("已有手动刮削任务正在进行，请等待完成")
        job_id = uuid.uuid4().hex
        _STATE.update(running=True, job_id=job_id, total=len(groups), done=0, ok=0, current='',
                      results=[], error='', cancel_requested=False)
        for code, items in groups.items():
            record_attempt(items, code, status='queued', job_id=job_id)
        try:
            threading.Thread(target=_run, args=(groups, job_id, overwrite, refresh_metadata, fetch_meta), daemon=True, name='manual-scrape').start()
        except Exception:
            _STATE.update(running=False, error='启动刮削任务失败')
            raise
        return state()


def cancel() -> dict:
    with _LOCK:
        if _STATE['running']:
            _STATE['cancel_requested'] = True
        return state()


def _run(groups, job_id, overwrite, refresh_metadata, fetch_meta):
    try:
        for code, items in groups.items():
            with _LOCK:
                stop = _STATE['cancel_requested']
                if not stop:
                    _STATE['current'] = code
            if stop:
                record_attempt(items, code, status='cancelled', job_id=job_id)
                continue
            record_attempt(items, code, status='running', job_id=job_id)
            try:
                for info in items:
                    validate_file(str(info.path))
                existing = {}
                with session_scope() as session:
                    for row in session.scalars(select(MediaLink).where(MediaLink.source_path.in_([str(i.path) for i in items])).order_by(MediaLink.link_path)):
                        existing.setdefault(row.source_path, Path(row.link_path))
                result = scrape.scrape_group(code, items, fetch_meta=fetch_meta, overwrite=overwrite,
                    refresh_metadata=refresh_metadata, existing_targets=existing)
            except Exception as exc:
                logger.exception(f"[{code}] 手动刮削任务失败")
                result = scrape.ScrapeResult(code=code, error=str(exc))
            record_attempt(items, code, status='succeeded' if result.ok else 'failed', job_id=job_id, error=result.error)
            row = {'code': result.code, 'ok': result.ok, 'paths': [str(i.path) for i in items],
                   'links': [str(p) for p in result.links], 'error': result.error, 'warnings': result.warnings,
                   'sources': result.sources, 'nfo_written': result.nfo_written, 'images_written': result.images_written}
            with _LOCK:
                _STATE['results'].append(row)
                _STATE['done'] += 1
                _STATE['ok'] += int(result.ok)
    except Exception:
        logger.exception("[手动刮削] 任务异常中断")
        with _LOCK:
            _STATE['error'] = '任务异常中断，未完成视频可再次选择重试'
    finally:
        with _LOCK:
            _STATE.update(running=False, current='')
