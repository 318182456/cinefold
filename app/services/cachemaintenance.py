"""Restartable, bounded maintenance of regenerable web images only."""
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core.config import get_settings
from app.database.models import Cache, Code, ImageCacheFile
from app.database.session import session_scope
from app.utils import imagecache

_lock = threading.RLock()
_running = False
_stop = threading.Event()
NS = 'image-maintenance'


def _save(state, session=None):
    def write(db):
        stmt = (pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert)(Cache)
        state['updated_at'] = time.time()
        db.execute(stmt.values(namespace=NS, key='current', content=json.dumps(state, ensure_ascii=False))
            .on_conflict_do_update(index_elements=['namespace', 'key'], set_={'content': json.dumps(state, ensure_ascii=False)}))
    if session is not None:
        write(session)
    else:
        with session_scope() as db:
            write(db)


def state():
    with _lock, session_scope() as db:
        value = db.scalar(select(Cache.content).where(Cache.namespace == NS, Cache.key == 'current'))
        try:
            data = json.loads(value) if value else {'phase': 'idle', 'running': False}
            if not isinstance(data, dict):
                raise ValueError('Invalid checkpoint')
        except (ValueError, TypeError):
            return {'phase': 'failed', 'running': False, 'message': '任务记录损坏，请重新预览，未将其视为完成'}
        if data.get('running') and not _running:
            data.update(running=False, phase='interrupted', message='服务重启，已完成的文件保留，可继续')
        return data


def stop():
    _stop.set()
    return state()


def _policy():
    settings = get_settings()
    return dict(root=str(imagecache.PIC_DIR.resolve()), max_bytes=settings.image_cache_max_gb*1024**3,
                days=settings.image_cache_days, still_days=settings.image_still_days,
                quality=settings.image_cache_quality, webp=settings.image_cache_webp)


def start(mode='preview', resume=False):
    global _running
    if mode not in ('preview', 'optimize', 'clean'):
        raise ValueError('未知图片维护操作')
    with _lock:
        if _running:
            raise ValueError('图片维护任务正在执行')
        old, policy = state(), _policy()
        if resume:
            if old.get('phase') not in ('paused', 'failed', 'interrupted') or old.get('policy') != policy:
                raise ValueError('没有可继续的任务，或缓存目录/设置已变更，请重新预览')
            data = old
        else:
            data = dict(mode=mode, policy=policy, stage='scan', cursor='', scanned=0, processed=0,
                changed=0, saved_bytes=0, candidate_files=0, candidate_bytes=0, errors=[], started_at=time.time())
        data.update(running=True, phase='queued', message='等待后台资源')
        _save(data)
        _stop.clear()
        _running = True
        try:
            threading.Thread(target=_run, args=(data,), daemon=True, name='image-maintenance').start()
        except Exception:
            _running = False
            data.update(running=False, phase='failed', message='后台任务启动失败')
            _save(data)
            raise
        return copy.deepcopy(data)


def _fingerprint(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def _classify(codes):
    with session_scope() as db:
        protected = set(db.scalars(select(Code.code).where(Code.code.in_(codes), imagecache.important_condition())))
        sources = {row.code: bool((row.banner or row.poster or '').startswith(('http://', 'https://')))
            for row in db.execute(select(Code.code, Code.banner, Code.poster).where(Code.code.in_(codes)))}
    return protected, sources


def _scan(data):
    root = imagecache.PIC_DIR.resolve()
    if not root.is_dir():
        raise OSError('图片缓存目录不存在，已有统计保留')
    stamp, batch = time.time(), []
    data.update(scanned=0, current='', message='扫描网页图片缓存')

    def flush():
        if not batch:
            return
        protected, sources = _classify({row['code'] for row in batch})
        for row in batch:
            row['protected'] = row['code'] in protected and not row['still']
            # Unknown/misc local images have no verified origin: never evict them automatically.
            row['recoverable'] = sources.get(row['code'], False) and (row['relative'].split('/')[-1].startswith('banner.') or row['still'])
        with session_scope() as db:
            stmt = (pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert)(ImageCacheFile)
            db.execute(stmt.on_conflict_do_update(index_elements=['key'],
                set_={key: getattr(stmt.excluded, key) for key in batch[0] if key != 'key'}), batch)
        data['scanned'] += len(batch)
        batch.clear()
        _save(data)

    def failed(exc):
        raise exc
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        dirs[:] = [d for d in dirs if not Path(folder, d).is_symlink()]
        for name in files:
            if _stop.is_set():
                flush()
                return False
            path = Path(folder, name)
            if path.is_symlink() or path.suffix.lower() not in imagecache.CONTENT_TYPES:
                continue
            relative = path.relative_to(root).as_posix()
            stat = path.stat()
            parts = relative.split('/')
            code = parts[0] if len(parts) == 2 else ''
            batch.append(dict(key=hashlib.sha256(relative.encode()).hexdigest(), relative=relative, code=code,
                size=stat.st_size, accessed=max(stat.st_atime, stat.st_mtime), fingerprint=_fingerprint(path),
                scanned=stamp, still=name.startswith(('still_', 'still.')), protected=False, recoverable=False))
            data['current'] = relative
            if len(batch) >= 200:
                flush()
    flush()
    with session_scope() as db:
        db.execute(delete(ImageCacheFile).where(ImageCacheFile.scanned < stamp))
        data['total_bytes'] = db.scalar(select(func.coalesce(func.sum(ImageCacheFile.size), 0)))
        data['total_files'] = db.scalar(select(func.count()).select_from(ImageCacheFile))
    data['stage'] = 'process'
    _save(data)
    return True


def _process(data):
    policy, mode = data['policy'], data['mode']
    remaining = data.get('remaining_bytes', data.get('total_bytes', 0))
    cursor = data.get('cursor', '')
    cursor_access = data.get('cursor_access', -1.0)
    data['message'] = '试算可清理缓存' if mode == 'preview' else ('转换较小的 WebP' if mode == 'optimize' else '清理过期与超额缓存')
    # SQL orders oldest entries first. Only one page of paths lives in memory.
    while True:
        with session_scope() as db:
            query = select(ImageCacheFile).where((ImageCacheFile.accessed > cursor_access) |
                ((ImageCacheFile.accessed == cursor_access) & (ImageCacheFile.key > cursor)))\
                .order_by(ImageCacheFile.accessed, ImageCacheFile.key).limit(100)
            rows = [row.to_dict() for row in db.scalars(query)]
        if not rows:
            break
        protected, sources = _classify({row['code'] for row in rows})
        for row in rows:
            if _stop.is_set():
                return False
            path = imagecache.PIC_DIR / row['relative']
            data['current'] = row['relative']
            changed_before = data['changed']
            try:
                with imagecache.file_lock(path):
                    if path.is_symlink() or not path.resolve().is_relative_to(imagecache.PIC_DIR.resolve()):
                        raise ValueError('图片路径已变更或越界')
                    if _fingerprint(path) != row['fingerprint']:
                        # A new image or a recent access must not be deleted using an old preview.
                        continue
                    if mode == 'optimize':
                        if path.suffix.lower() in ('.jpg', '.jpeg', '.png') and row['size'] <= 20*1024**2:
                            content, suffix = imagecache.optimized(path.read_bytes())
                            if suffix and len(content) < row['size']:
                                target = path.with_suffix(suffix)
                                # Never overwrite an existing independent format with different content.
                                if not target.exists() or (target.stat().st_size == len(content) and target.read_bytes() == content):
                                    if _fingerprint(path) != row['fingerprint']:
                                        continue
                                    if not target.exists():
                                        imagecache.atomic_write(target, content)
                                    path.unlink()
                                    data['changed'] += 1
                                    data['saved_bytes'] += row['size']-len(content)
                    else:
                        age = (time.time()-max(row['accessed'], path.stat().st_atime))/86400
                        expired = age >= (policy['still_days'] if row['still'] else policy['days'])
                        over_quota = remaining > policy['max_bytes']
                        eligible = row['recoverable'] and sources.get(row['code']) and (row['still'] or row['code'] not in protected)
                        if eligible and (expired or over_quota):
                            if mode == 'clean':
                                fresh_protected, fresh_sources = _classify({row['code']})
                                if not fresh_sources.get(row['code']) or (not row['still'] and row['code'] in fresh_protected):
                                    continue
                            data['candidate_files'] += 1
                            data['candidate_bytes'] += row['size']
                            remaining -= row['size']
                            if mode == 'clean':
                                path.unlink()
                                data['changed'] += 1
                                data['saved_bytes'] += row['size']
            except FileNotFoundError:
                pass
            except Exception as exc:
                data['errors'] = (data['errors']+[f"{row['relative']}: {type(exc).__name__}"])[-20:]
            finally:
                cursor = row['key']
                cursor_access = row['accessed']
                data['cursor'] = cursor
                data['cursor_access'] = cursor_access
                data['remaining_bytes'] = remaining
                data['processed'] += 1
                if data['changed'] != changed_before or data['processed'] % 100 == 0:
                    _save(data)
    return True


def _run(data):
    global _running
    from app.services.resourcebudget import slot
    try:
        with slot():
            data.update(phase='running')
            _save(data)
            completed = (data['stage'] != 'scan' or _scan(data)) and _process(data)
            # Refresh catalog after successful mutations; interrupted work never gets treated as completed.
            if completed and data['mode'] != 'preview':
                saved = dict(data)
                data['stage'] = 'scan'
                completed = _scan(data)
                data.update({key: value for key, value in saved.items() if key not in ('total_bytes', 'total_files')})
                if not completed:
                    data['stage'] = 'scan'
            data.update(phase='finished' if completed else 'paused', running=False,
                message='维护完成；重要封面与无法确认来源的图片保留' if completed else '已暂停，可继续', finished_at=time.time())
            _save(data)
    except Exception as exc:
        data.update(running=False, phase='failed', message=f'维护中断: {type(exc).__name__}，已处理文件保留，可继续')
        _save(data)
    finally:
        with _lock:
            _running = False
