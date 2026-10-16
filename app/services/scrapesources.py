"""下载源视频列表、指定番号与后台手动刮削任务。"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings, save_settings
from app.database.models import Code, CodeStatus, MediaLink, ScrapeAttempt
from app.database.session import session_scope
from app.services import scrape, scrapewatch
from app.services.medialink import is_adoptable_video
from app.utils import get_true_code, mediafile

_LOCK = threading.RLock()
_CONFIG_LOCK = threading.Lock()
_STATE = {"running": False, "job_id": "", "total": 0, "done": 0, "ok": 0,
          "current": "", "results": [], "error": "", "cancel_requested": False}


def key(path) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()


def configured() -> list[str]:
    return list(dict.fromkeys(s.strip() for s in re.split(r"[,\r\n]+", get_settings().scrape_source_dirs) if s.strip()))


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


def roots() -> list[dict]:
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
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not is_adoptable_video(path):
        raise ValueError(f"不是可处理的源视频: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"视频为空文件: {path.name}")
    path = path.resolve()
    if not any(r['available'] and path.is_relative_to(Path(r['resolved'])) for r in roots()):
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


def list_videos(*, root: str = '', keyword: str = '', status: str = '', page: int = 1, size: int = 30) -> dict:
    configured_roots = roots()
    if root and root not in {r['path'] for r in configured_roots}:
        raise ValueError("所选目录未配置")
    with session_scope() as session:
        links = defaultdict(list)
        for source, target in session.execute(select(MediaLink.source_path, MediaLink.link_path)):
            links[key(source)].append(target)
        attempts = {a.source_key: a.to_dict() for a in session.scalars(select(ScrapeAttempt))}
        titles = {c.code: (c.cn_title or c.title or '') for c in session.scalars(select(Code))}
        downloading = set(session.scalars(select(Code.code).where(Code.status == CodeStatus.DOWNLOADING)))
    job = state()
    rows, seen, errors = [], set(), []
    counts = defaultdict(int)
    needle = keyword.casefold().strip()
    for item in configured_roots:
        if not item['available'] or root and item['path'] != root:
            continue
        def onerror(exc):
            errors.append(f"目录读取失败: {exc.filename}")
        for folder, dirs, files in os.walk(item['resolved'], followlinks=False, onerror=onerror):
            dirs[:] = sorted(d for d in dirs if d not in {'@eaDir', '#recycle', '.unwanted'} and not Path(folder, d).is_symlink())
            for filename in sorted(files):
                path = Path(folder, filename)
                if path.is_symlink() or not is_adoptable_video(path):
                    continue
                identity = key(path)
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    stat = path.stat()
                    if not path.is_file():
                        continue
                except OSError:
                    errors.append(f"视频不可访问: {path}")
                    continue
                info = mediafile.parse(path)
                attempt = attempts.get(identity, {})
                code = attempt.get('code') or info.code
                target_links = links.get(identity, [])
                current = 'pending'
                if not code:
                    current = 'unrecognized'
                elif target_links:
                    current = 'complete' if scrapewatch._complete(path, code, target_links) else 'incomplete'
                if attempt.get('status') in ('failed', 'cancelled'):
                    current = attempt['status']
                elif attempt.get('status') in ('queued', 'running'):
                    active = (job['running'] and attempt.get('job_id') == job['job_id']) if attempt.get('job_id') else scrapewatch._LOCK.locked()
                    current = attempt['status'] if active else 'interrupted'
                if code in downloading or info.code in downloading:
                    current = 'downloading'
                elif not stat.st_size:
                    current = 'empty'
                row = {'path': str(path), 'name': path.name, 'root': item['path'], 'code': code, 'detected_code': info.code,
                       'title': titles.get(code, ''), 'part': info.part, 'size': stat.st_size, 'mtime': stat.st_mtime,
                       'status': current, 'links': target_links, 'last_run': attempt.get('updated_at'), 'error': attempt.get('error', '')}
                if needle and needle not in f"{path} {code} {row['title']}".casefold():
                    continue
                counts[current] += 1
                if not status or status == current:
                    rows.append(row)
    rows.sort(key=lambda r: (-r['mtime'], r['path']))
    offset = (page - 1) * size
    return {'roots': configured_roots, 'items': rows[offset:offset + size], 'total': len(rows), 'page': page,
            'size': size, 'counts': dict(counts), 'warnings': errors[:20], 'job': job}


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
