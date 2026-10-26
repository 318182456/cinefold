"""下载管理：缓存实时列表、持久化历史、可恢复的后台转移任务。"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.models import Cache, Code, DownloadRecord, History
from app.database.session import session_scope
from app.modules.downloadclient import get_download_client, list_configured_clients

_READERS = ThreadPoolExecutor(max_workers=3, thread_name_prefix='download-status')
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='seed-transfer-ui')
_LIVE_LOCK = threading.Lock()
_LIVE = None
_LOCK = threading.RLock()
_STATE = None


def group(row):
    state = str(row.get('state') or '').lower()
    if any(word in state for word in ('error', 'missing')):
        return 'error'
    if 'check' in state or state in ('1', '2'):
        return 'checking'
    if any(word in state for word in ('paused', 'stopped')) or state == '0':
        return 'paused'
    if row.get('progress', 0) >= 1:
        return 'seeding'
    return 'downloading'


def _read(name):
    client = get_download_client(name)
    if client is None or not hasattr(client, 'list_downloads'):
        raise ValueError('该下载器暂不支持实时管理')
    return client.list_downloads()


def invalidate_live():
    global _LIVE
    with _LIVE_LOCK:
        _LIVE = None


def live(*, client='', state='', keyword='', code_status=None, page=1, size=30):
    global _LIVE
    settings = get_settings()
    names = list_configured_clients()
    context = [(name, getattr(settings, name+'_url', '')) for name in names]
    signature = hashlib.sha256(json.dumps(context).encode()).hexdigest()
    with _LIVE_LOCK:
        if _LIVE is None or _LIVE.get('signature') != signature or time.time()-_LIVE['updated_at'] >= 8:
            futures = {name: _READERS.submit(_read, name) for name in names}
            rows, errors, answered = [], [], []
            for name, future in futures.items():
                try:
                    values = future.result()
                    answered.append(name)
                    server = dict(context)[name]
                    for value in values:
                        item = {**value, 'client': name, 'stale': False, 'snapshot_at': time.time()}
                        item['hash'] = str(item.get('hash') or '').lower()
                        if not item['hash']:
                            continue
                        item['key'] = hashlib.sha256(f'{name}|{server}|{item["hash"]}'.encode()).hexdigest()
                        item['group'] = group(item)
                        rows.append(item)
                except Exception:
                    errors.append({'client': name, 'message': '任务读取失败，请检查下载器连接、凭据或兼容性'})
                    if _LIVE and _LIVE.get('signature') == signature:
                        rows.extend({**row, 'stale': True} for row in _LIVE['items'] if row['client'] == name)
            _remember([row for row in rows if not row.get('stale')])
            _LIVE = {'items': rows, 'errors': errors, 'clients': names, 'answered': answered,
                     'updated_at': time.time(), 'signature': signature}
        # 完整快照保持只读；只复制当前页，避免每次筛选都深拷贝几千个任务。
        result = {key: copy.deepcopy(value) for key, value in _LIVE.items() if key not in ('items', 'signature')}
        rows = list(_LIVE['items'])
    if client:
        rows = [row for row in rows if row['client'] == client]
    if code_status is not None:
        rows = [row for row in rows if row.get('code_status') == code_status]
    if keyword:
        word = keyword.casefold()
        rows = [row for row in rows if word in ' '.join(str(row.get(k) or '') for k in ('name', 'code', 'hash', 'save_path')).casefold()]
    counts = {key: sum(row['group'] == key for row in rows) for key in ('downloading', 'seeding', 'paused', 'checking', 'error')}
    result.update(counts={'all': len(rows), **counts},
                  download_speed=sum(row.get('download_speed') or 0 for row in rows if not row.get('stale')),
                  upload_speed=sum(row.get('upload_speed') or 0 for row in rows if not row.get('stale')))
    if state:
        rows = [row for row in rows if row['group'] == state]
    rows.sort(key=lambda row: (row.get('download_speed') or 0, row.get('added_at') or 0), reverse=True)
    return {**result, 'total': len(rows), 'page': page, 'size': size, 'items': copy.deepcopy(rows[(page-1)*size:page*size])}


def _remember(rows):
    if not rows:
        return
    now = datetime.now()
    with session_scope() as session:
        known, statuses, records = {}, {}, {}
        hashes = list({item['hash'] for item in rows})
        keys = list({item['key'] for item in rows})
        for offset in range(0, len(hashes), 400):
            known.update((row.hash.lower(), row) for row in session.scalars(
                select(History).where(func.lower(History.hash).in_(hashes[offset:offset+400]))))
        codes = list({row.code for row in known.values() if row.code})
        for offset in range(0, len(codes), 400):
            statuses.update(session.execute(select(Code.code, Code.status).where(Code.code.in_(codes[offset:offset+400]))).all())
        for offset in range(0, len(keys), 400):
            records.update((row.key, row) for row in session.scalars(
                select(DownloadRecord).where(DownloadRecord.key.in_(keys[offset:offset+400]))))
        for item in rows:
            history = known.get(item['hash'])
            item['code'] = history.code if history else ''
            item['site'] = history.site if history else ''
            item['code_status'] = statuses.get(item['code'])
            # Speeds/peer counts are live measurements, not historical facts.
            saved = {k: v for k, v in item.items() if k not in ('download_speed', 'upload_speed', 'seeds_connected',
                      'seeds_total', 'peers_connected', 'peers_total', 'eta', 'ratio', 'snapshot_at', 'stale')}
            saved['progress'] = round(float(saved.get('progress') or 0), 2)
            record = records.get(item['key'])
            if record is None:
                record = DownloadRecord(key=item['key'], client=item['client'], hash=item['hash'])
                session.add(record)
                records[item['key']] = record
            if record.details != saved or record.last_seen is None or (now-record.last_seen).total_seconds() >= 300:
                record.code = item['code']
                record.details = saved
                record.last_seen = now


def history(*, keyword='', client='', code_status=None, page=1, size=30):
    with session_scope() as session:
        rows = []
        statuses = dict(session.execute(select(Code.code, Code.status)).all())
        hashes = set()
        for record in session.scalars(select(DownloadRecord)):
            rows.append({**record.details, 'code_status': statuses.get(record.code), 'last_seen': record.last_seen.isoformat(), 'recorded_at': record.create_time.isoformat()})
            hashes.add(record.hash.lower())
        # Include existing system download history from before this feature was installed.
        for record, status in session.execute(select(History, Code.status).outerjoin(Code, Code.code == History.code)):
            if record.hash.lower() in hashes:
                continue
            rows.append({'key': 'legacy:'+record.hash, 'hash': record.hash, 'code': record.code, 'name': record.code,
                         'client': '', 'site': record.site, 'save_path': record.save_path,
                         'recorded_at': record.create_time.isoformat(), 'last_seen': '', 'code_status': status,
                         'state': '历史推送记录', 'group': 'history'})
    if client:
        rows = [row for row in rows if row.get('client') == client]
    if code_status is not None:
        rows = [row for row in rows if row.get('code_status') == code_status]
    if keyword:
        word = keyword.casefold()
        rows = [row for row in rows if word in ' '.join(str(row.get(k) or '') for k in ('name', 'code', 'hash', 'save_path')).casefold()]
    rows.sort(key=lambda row: row.get('recorded_at') or '', reverse=True)
    return {'items': rows[(page-1)*size:page*size], 'total': len(rows), 'page': page, 'size': size}


def _save():
    with session_scope() as session:
        for key in ('current', _STATE['job_id']):
            record = session.scalar(select(Cache).where(Cache.namespace == 'download-transfer', Cache.key == key))
            if record is None:
                record = Cache(namespace='download-transfer', key=key)
                session.add(record)
            record.content = json.dumps(_STATE, ensure_ascii=False)


def _transfer_context():
    settings = get_settings()
    values = [getattr(settings, key, '') for key in (
        'qbittorrent_url', 'qbittorrent_username', 'transmission_url', 'transmission_username', 'seed_transfer_path_map')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def transfer_current():
    global _STATE
    with _LOCK:
        if _STATE is None:
            with session_scope() as session:
                saved = session.scalar(select(Cache).where(Cache.namespace == 'download-transfer', Cache.key == 'current'))
                _STATE = json.loads(saved.content) if saved and saved.content else {'running': False, 'phase': 'idle', 'results': []}
            if _STATE.get('running'):
                _STATE.update(running=False, phase='interrupted', error='服务重启，可继续未完成的转移；已完成项会跳过')
                _save()
        return copy.deepcopy(_STATE)


def start_transfer(hashes=None, *, delete_source=False, resume=False):
    global _STATE
    from app.services.seedtransfer import is_available
    with _LOCK:
        previous = transfer_current()
        if previous['running']:
            raise ValueError('转移做种任务正在运行')
        ok, reason = is_available()
        if not ok:
            raise ValueError(reason)
        if resume:
            if previous.get('phase') not in ('interrupted', 'partial', 'failed'):
                raise ValueError('没有可继续的转移任务')
            if previous.get('context') != _transfer_context():
                raise ValueError('下载器连接或路径映射已变化，请重新选择任务')
            _STATE['results'] = [r for r in previous['results'] if r['status'] in ('succeeded', 'skipped')]
        else:
            hashes = list(dict.fromkeys(h.lower() for h in hashes or []))
            if not hashes:
                raise ValueError('请先选择 qBittorrent 已完成任务')
            _STATE = {'job_id': uuid.uuid4().hex, 'hashes': hashes, 'delete_source': delete_source,
                      'results': [], 'started_at': time.time(), 'context': _transfer_context()}
        _STATE.update(running=True, phase='running', error='', current='', updated_at=time.time())
        _save()
        try:
            _WORKER.submit(_transfer_work)
        except Exception:
            _STATE.update(running=False, phase='failed', error='后台任务启动失败')
            _save()
            raise
        return copy.deepcopy(_STATE)


def _transfer_work():
    global _LIVE
    from app.services.seedtransfer import transfer_hashes
    try:
        finished = {r['hash'] for r in _STATE['results']}
        for value in _STATE['hashes']:
            if value in finished:
                continue
            if _STATE.get('context') != _transfer_context():
                raise ValueError('下载器连接或路径映射已变化，请重新选择任务')
            with _LOCK:
                _STATE.update(current=value, updated_at=time.time())
                _save()
            result = transfer_hashes([value], delete_source=_STATE['delete_source'])
            row = {'hash': value, 'status': 'succeeded' if value in result.transferred else 'skipped' if value in result.skipped else 'failed',
                   'error': next((r.get('reason', '') for r in result.failed if r['hash'] == value), '')}
            with _LOCK:
                _STATE['results'].append(row)
                _STATE['updated_at'] = time.time()
                _save()
        with _LOCK:
            _STATE['phase'] = 'partial' if any(r['status'] == 'failed' for r in _STATE['results']) else 'completed'
    except Exception:
        with _LOCK:
            _STATE.update(phase='failed', error='转移中断，请检查下载器连接后继续任务')
    finally:
        with _LOCK:
            _STATE.update(running=False, current='', updated_at=time.time())
            _save()
        with _LIVE_LOCK:
            _LIVE = None


def transfer_history():
    with session_scope() as session:
        records = session.scalars(select(Cache).where(Cache.namespace == 'download-transfer', Cache.key != 'current')
                                  .order_by(Cache.id.desc()).limit(20))
        return [json.loads(row.content) for row in records if row.content]
