"""Bounded persistent exact-file cache. Destructive operations still use live RPC."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import get_settings
from app.database.models import Cache, TorrentFile, TorrentManifest
from app.database.session import session_scope

_LOCKS = {name: threading.RLock() for name in ('qbittorrent', 'transmission')}


def connection(name, client=None):
    settings = get_settings()
    values = [name, getattr(client, 'url', '') or getattr(settings, name + '_url', '')]
    values.extend(getattr(client, suffix[1:], getattr(settings, name + suffix, ''))
                  for suffix in ('_username', '_password', '_apikey'))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _signature(row):
    values = [row.get('name'), row.get('save_path'), float(row.get('progress') or 0) >= 1,
              any(word in str(row.get('state', '')).lower() for word in ('check', 'moving', 'missing', 'error'))]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _insert(session, model):
    return (pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert)(model)


def _match_key(path):
    normalized = str(Path(path)).replace('\\', '/').casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def remember(name, rows, *, client=None, full=False, started=None):
    """Only complete successful polls mark absent rows; older polls cannot undo events."""
    stamp = started or datetime.now()
    key = connection(name, client)
    values = []
    for item in rows:
        value = str(item.get('hash') or '').lower()
        if value:
            values.append({'connection': key, 'hash': value, 'client': name, 'present': True,
                           'signature': _signature(item), 'details': dict(item), 'seen_at': stamp})
    with _LOCKS[name], session_scope() as session:
        for offset in range(0, len(values), 100):
            stmt = _insert(session, TorrentManifest)
            excluded = stmt.excluded
            session.execute(stmt.on_conflict_do_update(index_elements=['connection', 'hash'],
                set_={'client': excluded.client, 'present': True, 'signature': excluded.signature,
                      'details': excluded.details, 'seen_at': excluded.seen_at,
                      'checked_at': case((TorrentManifest.signature != excluded.signature, None), else_=TorrentManifest.checked_at),
                      'retry_at': case((TorrentManifest.signature != excluded.signature, None), else_=TorrentManifest.retry_at)},
                where=TorrentManifest.seen_at <= excluded.seen_at), values[offset:offset + 100])
        if full:
            session.execute(update(TorrentManifest).where(TorrentManifest.connection == key,
                TorrentManifest.seen_at < stamp).values(present=False))
            record = session.scalar(select(Cache).where(Cache.namespace == 'download-index-poll', Cache.key == key))
            content = json.dumps({'checked_at': stamp.isoformat()})
            if record is None:
                session.add(Cache(namespace='download-index-poll', key=key, content=content))
            else:
                record.content = content


def _due_snapshot(name, client):
    key = connection(name, client)
    with session_scope() as session:
        record = session.scalar(select(Cache.content).where(Cache.namespace == 'download-index-poll', Cache.key == key))
    try:
        checked = datetime.fromisoformat(json.loads(record)['checked_at'])
        return datetime.now() - checked >= timedelta(minutes=max(1, get_settings().download_index_interval))
    except (TypeError, ValueError, KeyError):
        return True


def refresh_snapshot(name, client):
    started = datetime.now()
    # Unlike monitor_torrent(), this method raises on RPC/authentication failure.
    rows = client.list_downloads()
    remember(name, rows, client=client, full=True, started=started)
    return len(rows)


def _fresh(row):
    if not row or not row.present or row.checked_at is None:
        return False
    minutes = 24 * 60 if float(row.details.get('progress') or 0) >= 1 else max(1, get_settings().download_index_interval)
    return datetime.now() - row.checked_at < timedelta(minutes=minutes)


def current_hashes(name, client):
    if _due_snapshot(name, client):
        refresh_snapshot(name, client)
    with session_scope() as session:
        return list(session.scalars(select(TorrentManifest.hash).where(
            TorrentManifest.connection == connection(name, client), TorrentManifest.present.is_(True))))


def _refresh_files(name, value, client):
    """One manifest commits atomically; interrupted/failed reads keep the prior version."""
    key = connection(name, client)
    with _LOCKS[name]:
        try:
            with session_scope() as session:
                row = session.get(TorrentManifest, (key, value))
                if row is None or not row.present:
                    return False
                if _fresh(row):
                    return False
                if row.retry_at and row.retry_at > datetime.now():
                    raise RuntimeError('文件清单暂不可用，等待定时任务重试')
                session.execute(delete(TorrentFile).where(TorrentFile.connection == key, TorrentFile.hash == value))
                batch, count = [], 0
                for path, found in client.iter_torrent_file_pairs([value], strict=True):
                    if found.lower() != value:
                        continue
                    batch.append({'connection': key, 'hash': value,
                                  'path_key': hashlib.sha256(path.encode()).hexdigest(),
                                  'match_key': _match_key(path), 'path': path})
                    if len(batch) >= 200:
                        session.execute(_insert(session, TorrentFile).on_conflict_do_nothing(), batch)
                        count += len(batch)
                        batch.clear()
                if batch:
                    session.execute(_insert(session, TorrentFile).on_conflict_do_nothing(), batch)
                    count += len(batch)
                row.checked_at, row.retry_at, row.error, row.file_count = datetime.now(), None, '', count
            return True
        except Exception:
            with session_scope() as session:
                session.execute(update(TorrentManifest).where(TorrentManifest.connection == key,
                    TorrentManifest.hash == value).values(retry_at=datetime.now() + timedelta(minutes=10),
                                                         error='文件清单读取失败，已有快照保留'))
            raise


def file_pairs(name, hashes, client, *, paths=None):
    """Read recent manifests from DB in small pages; refresh only missing/expired ones."""
    values = list(dict.fromkeys(h.lower() for h in hashes if h))
    if not values:
        return
    if not getattr(client, 'url', ''):
        yield from client.iter_torrent_file_pairs(values)
        return
    if _due_snapshot(name, client):
        refresh_snapshot(name, client)
    key = connection(name, client)
    for offset in range(0, len(values), 128):
        batch = values[offset:offset + 128]
        with session_scope() as session:
            rows = session.scalars(select(TorrentManifest).where(TorrentManifest.connection == key,
                TorrentManifest.hash.in_(batch), TorrentManifest.present.is_(True)))
            stale = [row.hash for row in rows if not _fresh(row)]
        for value in stale:
            _refresh_files(name, value, client)
        if paths is not None:
            continue
        with session_scope() as session:
            stmt = select(TorrentFile.path, TorrentFile.hash).join(TorrentManifest,
                (TorrentManifest.connection == TorrentFile.connection) & (TorrentManifest.hash == TorrentFile.hash))\
                .where(TorrentFile.connection == key, TorrentFile.hash.in_(batch),
                       TorrentManifest.present.is_(True), TorrentManifest.checked_at.isnot(None))\
                .execution_options(yield_per=200)
            yield from session.execute(stmt)
    if paths is not None:
        wanted = set(values)
        keys = list({_match_key(path) for path in paths})
        for offset in range(0, len(keys), 400):
            with session_scope() as session:
                stmt = select(TorrentFile.path, TorrentFile.hash).join(TorrentManifest,
                    (TorrentManifest.connection == TorrentFile.connection) & (TorrentManifest.hash == TorrentFile.hash))\
                    .where(TorrentFile.connection == key, TorrentFile.match_key.in_(keys[offset:offset + 400]),
                           TorrentManifest.present.is_(True), TorrentManifest.checked_at.isnot(None))\
                    .execution_options(yield_per=200)
                for path, value in session.execute(stmt):
                    if value in wanted:
                        yield path, value


def sync():
    from app.modules.downloadclient import get_download_client, list_configured_clients
    limit = max(1, min(512, get_settings().download_index_batch_size))
    updated, failures = 0, []
    for name in list_configured_clients():
        if name not in _LOCKS:
            continue
        client = get_download_client(name)
        try:
            refresh_snapshot(name, client)
            key = connection(name, client)
            now = datetime.now()
            with session_scope() as session:
                rows = session.scalars(select(TorrentManifest).where(TorrentManifest.connection == key,
                    TorrentManifest.present.is_(True),
                    or_(TorrentManifest.retry_at.is_(None), TorrentManifest.retry_at <= now))
                    .order_by(TorrentManifest.checked_at.asc().nullsfirst(), TorrentManifest.seen_at)
                    .execution_options(yield_per=100))
                pending = []
                for row in rows:
                    if not _fresh(row):
                        pending.append(row.hash)
                        if len(pending) >= limit:
                            break
            for value in pending:
                try:
                    updated += int(_refresh_files(name, value, client))
                except Exception:
                    failures.append(name)
        except Exception:
            failures.append(name)
    logger.info(f'[下载器索引] 更新文件清单 {updated} 个，失败 {len(failures)} 次')
    if failures:
        raise RuntimeError(f'下载器索引部分更新失败（成功 {updated} 个，失败 {len(failures)} 次），旧记录已保留')
    return updated


def deleted(name, hashes, client=None):
    key = connection(name, client)
    with _LOCKS[name], session_scope() as session:
        stamp = datetime.now()
        values = [{'connection': key, 'hash': h.lower(), 'client': name, 'present': False,
                   'signature': '', 'details': {}, 'seen_at': stamp} for h in dict.fromkeys(hashes) if h]
        if values:
            stmt = _insert(session, TorrentManifest)
            session.execute(stmt.on_conflict_do_update(index_elements=['connection', 'hash'],
                set_={'present': False, 'seen_at': stamp}), values)


def after_delete(name, hashes, client):
    try:
        deleted(name, hashes, client)
        from app.services.downloads import invalidate_live
        invalidate_live()
    except Exception:
        logger.warning('[下载器索引] 删除已执行，数据库同步失败，定时任务将重试')


def observed(name, rows, client):
    try:
        remember(name, rows, client=client)
    except Exception:
        logger.warning('[下载器索引] 已确认任务状态，数据库同步失败，定时任务将重试')


def after_transfer(torrent_hash, name, save_path, client):
    try:
        remember('transmission', [{'hash': torrent_hash, 'name': name, 'save_path': save_path,
                                  'state': 'added', 'progress': 0}], client=client)
        _refresh_files('transmission', torrent_hash.lower(), client)
        from app.services.downloads import invalidate_live
        invalidate_live()
    except Exception:
        logger.warning('[下载器索引] 转移已执行，数据库同步未完成，定时任务将重试')
