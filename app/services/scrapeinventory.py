"""后台扫描源目录，将文件清单与产物检查结果持久化。"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import get_settings
from app.database.models import MediaLink, ScrapeAttempt, ScrapeSourceRoot, ScrapeSourceVideo, ScrapeSourceMembership
from app.database.session import session_scope
from app.services.medialink import is_adoptable_video
from app.utils import CODE_PATTERN, _SITE_DOMAIN, _DMM_FILENAME, get_true_code, mediafile

_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_STATE = {"running": False, "done": 0, "files": 0, "total": 0, "current": "", "errors": [], "finished_at": ""}


def key(path) -> str:
    # 只做字符串规范化；查询与分页不能 resolve/stat 网络文件系统。
    normalized = os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
    return hashlib.sha256(normalized.encode()).hexdigest()


def configured() -> list[str]:
    return list(dict.fromkeys(s.strip() for s in re.split(r"[,\r\n]+", get_settings().scrape_source_dirs) if s.strip()))


def sync_roots(session) -> list[ScrapeSourceRoot]:
    entries = {}
    for path in configured():
        entries.setdefault(key(path), path)
    if not entries:
        return []
    # 列表读取与后台扫描可能同时首次建立目录记录。
    rows = {row.root_key: row for row in session.scalars(select(ScrapeSourceRoot).where(ScrapeSourceRoot.root_key.in_(entries)))}
    missing = {identity: path for identity, path in entries.items() if identity not in rows}
    if missing:
        session.execute(_insert(session, ScrapeSourceRoot).on_conflict_do_nothing(),
            [{'root_key': identity, 'path': path, 'available': None, 'error': '', 'resolved': ''}
             for identity, path in missing.items()])
        rows.update({row.root_key: row for row in session.scalars(select(ScrapeSourceRoot).where(ScrapeSourceRoot.root_key.in_(missing)))})
    result = []
    for identity, path in entries.items():
        row = rows[identity]
        row.path = path
        result.append(row)
    return result


def roots() -> list[dict]:
    with session_scope() as session:
        rows = sync_roots(session)
        counts = dict(session.execute(select(ScrapeSourceMembership.root_key, func.count()).group_by(ScrapeSourceMembership.root_key)).all())
        return [{**row.to_dict(), 'count': counts.get(row.root_key, 0)} for row in rows]


def state() -> dict:
    with _STATE_LOCK:
        return copy.deepcopy(_STATE)


def _begin(root: str):
    paths = configured()
    if root and root not in paths:
        raise ValueError('所选目录未配置')
    paths = [root] if root else paths
    if not _LOCK.acquire(blocking=False):
        return None
    with _STATE_LOCK:
        _STATE.update(running=True, done=0, files=0, total=len(paths), current='', errors=[], finished_at='')
    return paths


def refresh_now(root: str = '') -> dict:
    paths = _begin(root)
    if paths is not None:
        _run(paths)
    return state()


def start_refresh(root: str = '') -> dict:
    paths = _begin(root)
    if paths is not None:
        try:
            threading.Thread(target=_run, args=(paths,), daemon=True, name='source-inventory').start()
        except Exception:
            with _STATE_LOCK:
                _STATE.update(running=False, errors=['无法启动目录扫描'])
            _LOCK.release()
            raise
    return state()


def _insert(session, model):
    return sqlite_insert(model) if session.get_bind().dialect.name == 'sqlite' else pg_insert(model)


def repair_filename_codes() -> int:
    """纠正旧识别结果与广告误归类，只读路径字符串，不操作媒体文件。"""
    repaired = 0
    with session_scope() as session:
        query = select(ScrapeSourceVideo, ScrapeAttempt).outerjoin(
            ScrapeAttempt, ScrapeAttempt.source_key == ScrapeSourceVideo.source_key).execution_options(yield_per=200)
        for row, attempt in session.execute(query):
            domains = ' '.join(match.group(0) for match in _SITE_DOMAIN.finditer(row.name))
            wrong = {get_true_code(match.group(1)) for match in CODE_PATTERN.finditer(domains)} - {''}
            info = mediafile.parse(Path(row.path))
            changed = False
            old_code = row.detected_code
            # DMM 的填充数字与视频编码后缀是明确的发行命名，其他非空番号不重猜。
            cid = bool(_DMM_FILENAME.search(row.name) or re.search(r'(?i)MKBD[-_]S\d|H(?:4610|0930)[-_]|HEYDOUGA[-_]|[A-Z]{2,7}00\d{3}|\d[-_]U[-_]C', row.name))
            date_version = bool(re.fullmatch(r'\d{6}_\d{2,4}', old_code)) and info.code.startswith(old_code + '-')
            if (not row.detected_code or row.detected_code in wrong or cid or date_version or info.ignored) and row.detected_code != info.code:
                row.detected_code, row.part = info.code, info.part
                changed = True
            if row.detected_code == info.code and row.part != info.part:
                row.part = info.part
                changed = True
            if attempt and (attempt.code in wrong or (cid or date_version) and attempt.code == old_code) and attempt.code != info.code:
                attempt.code = info.code
                attempt.status, attempt.error, attempt.job_id = 'pending', '', ''
                changed = True
            if changed and not info.ignored:
                row.artifact_status = 'incomplete' if row.links else 'pending'
            # 旧版本已经修正 attempt.code，却留下了旧域名番号对应的失败信息。
            # 仅清理明确指向域名误识别的错误，当前正确番号的真实失败继续保留。
            if (attempt and attempt.status == 'failed' and attempt.code == info.code and info.code
                    and any(re.fullmatch(r'取不到元数据:\s*' + re.escape(code), attempt.error or '')
                            for code in wrong if code != info.code)):
                attempt.status, attempt.error, attempt.job_id = 'pending', '', ''
                changed = True
            if info.ignored and row.artifact_status != 'ignored':
                row.artifact_status = 'ignored'
                changed = True
            if changed:
                row.indexed_at = datetime.now()
                repaired += 1
    if repaired:
        logger.info(f'[源视频清单] 已修正 {repaired} 条文件名识别或广告归类，请核对后重新刮削')
    return repaired


def _flush(root_key, batch, started):
    if not batch:
        return
    with session_scope() as session:
        stmt = _insert(session, ScrapeSourceVideo)
        stmt = stmt.on_conflict_do_update(index_elements=['source_key'],
            set_={name: getattr(stmt.excluded, name) for name in batch[0] if name != 'source_key'},
            where=ScrapeSourceVideo.indexed_at <= started)
        session.execute(stmt, batch)
        members = [{'root_key': root_key, 'source_key': row['source_key']} for row in batch]
        session.execute(_insert(session, ScrapeSourceMembership).on_conflict_do_nothing(), members)


def _run(paths):
    try:
        if not paths:
            return
        from app.services import scrapesources, scrapewatch
        repair_filename_codes()
        with session_scope() as session:
            sync_roots(session)
            links = defaultdict(list)
            for source, target in session.execute(select(MediaLink.source_path, MediaLink.link_path)):
                links[key(source)].append(target)
            overrides = {a.source_key: a.code for a in session.scalars(select(ScrapeAttempt)) if a.code}
        for raw in paths:
            started = datetime.now()
            with _STATE_LOCK:
                _STATE['current'] = raw
            identity = key(raw)
            seen, batch, errors = set(), [], []
            resolved, available = '', False
            try:
                root = scrapesources._root(raw)
                resolved, available = str(root), True
                def onerror(exc):
                    errors.append(f'目录读取失败: {exc.filename}')
                from app.services.scrape import inspect_source
                from app.services.subtitle import _DirProbe
                from app.services.sourceidentity import SourceIdentityResolver
                subtitle_probe = _DirProbe()
                identity_resolver = SourceIdentityResolver()
                for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
                    dirs[:] = [d for d in dirs if d not in {'@eaDir', '#recycle', '.unwanted'} and not Path(folder, d).is_symlink()]
                    for name in files:
                        path = Path(folder, name)
                        if path.is_symlink() or not is_adoptable_video(path):
                            continue
                        source_key = key(path)
                        if source_key in seen:
                            continue
                        try:
                            stat = path.stat()
                            if not path.is_file():
                                continue
                            info = inspect_source(path, subtitle_probe, identity_resolver)
                            target_links = links.get(source_key, [])
                            code = overrides.get(source_key) or info.code
                            status = 'pending'
                            if info.ignored:
                                status = 'ignored'
                            elif target_links:
                                status = 'complete' if scrapewatch._complete(path, code, target_links) else 'incomplete'
                            batch.append({'source_key': source_key, 'path': str(path), 'name': name,
                                'detected_code': info.code, 'part': info.part, 'size': stat.st_size, 'mtime': stat.st_mtime,
                                'artifact_status': status, 'links': target_links, 'indexed_at': started})
                            seen.add(source_key)
                            with _STATE_LOCK:
                                _STATE['files'] += 1
                            if len(batch) >= 200:
                                _flush(identity, batch, started)
                                batch = []
                        except OSError:
                            errors.append(f'视频不可访问: {path}')
                _flush(identity, batch, started)
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
            with session_scope() as session:
                row = session.get(ScrapeSourceRoot, identity)
                if row is None:
                    # 扫描期间配置可能被移除；缓存仍可保存，但列表只查当前配置。
                    row = ScrapeSourceRoot(root_key=identity, path=raw)
                    session.add(row)
                row.resolved, row.available, row.error, row.last_scan = resolved, available, '\n'.join(errors[:20]), datetime.now()
                if not errors:
                    # 只有完整成功扫描才清理消失文件的清单引用；断挂载不清空缓存。
                    old = set(session.scalars(select(ScrapeSourceMembership.source_key).where(ScrapeSourceMembership.root_key == identity)))
                    missing = list(old - seen)
                    for offset in range(0, len(missing), 400):
                        session.execute(delete(ScrapeSourceMembership).where(ScrapeSourceMembership.root_key == identity,
                            ScrapeSourceMembership.source_key.in_(missing[offset:offset + 400])))
            with _STATE_LOCK:
                _STATE['done'] += 1
                _STATE['errors'].extend(errors[:20])
                _STATE['errors'] = _STATE['errors'][:30]
    except Exception:
        logger.exception('[源视频清单] 扫描失败，已保存的清单保留')
        with _STATE_LOCK:
            _STATE['errors'].append('扫描未完成，已有清单保留，请查看日志后重试')
    finally:
        with _STATE_LOCK:
            _STATE.update(running=False, current='', finished_at=datetime.now().isoformat())
        _LOCK.release()


def update_result(items, code: str):
    """刮削结束后只核对这组文件，立即更新清单，避免再扫整个目录。"""
    from app.services.scrapewatch import _complete
    with session_scope() as session:
        for item in items:
            path = item.path if hasattr(item, 'path') else Path(item['path'])
            row = session.get(ScrapeSourceVideo, key(path))
            if row is None:
                continue
            target_links = session.scalars(select(MediaLink.link_path).where(MediaLink.source_path == str(path))).all()
            row.links = list(target_links)
            row.artifact_status = ('complete' if _complete(path, code, target_links) else 'incomplete') if target_links else 'pending'
            row.indexed_at = datetime.now()
