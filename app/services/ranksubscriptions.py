"""手动榜单订阅：按当前榜单后台执行，并保留可查询的任务结果。"""
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from app.database.models import Cache
from app.database.session import session_scope

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rank-subscribe')
_ACTIVE = set()
_STATES = {}


def _save(kind):
    with session_scope() as session:
        row = session.scalar(select(Cache).where(Cache.namespace == 'rank-subscribe', Cache.key == kind))
        if row is None:
            row = Cache(namespace='rank-subscribe', key=kind)
            session.add(row)
        _STATES[kind]['updated_at'] = time.time()
        row.content = json.dumps(_STATES[kind], ensure_ascii=False)


def current(kind):
    with _LOCK:
        if kind not in _STATES:
            with session_scope() as session:
                row = session.scalar(select(Cache).where(Cache.namespace == 'rank-subscribe', Cache.key == kind))
                _STATES[kind] = json.loads(row.content) if row and row.content else {'running': False, 'phase': 'idle'}
            if _STATES[kind].get('running') and kind not in _ACTIVE:
                _STATES[kind].update(running=False, phase='interrupted', error='进程重启导致中断；重新执行会跳过已订阅影片')
                _save(kind)
        return copy.deepcopy(_STATES[kind])


def start(kind):
    with _LOCK:
        if current(kind)['running']:
            return current(kind)
        _STATES[kind] = {'job_id': uuid.uuid4().hex, 'running': True, 'phase': 'queued', 'rank_type': kind,
                         'done': 0, 'total': 0, 'subscribed': 0, 'existing': 0, 'filtered': 0,
                         'failures': [], 'current': '', 'error': '', 'started_at': time.time()}
        _ACTIVE.add(kind)
        try:
            _save(kind)
            _POOL.submit(_run, kind)
        except Exception:
            _ACTIVE.discard(kind)
            _STATES[kind].update(running=False, phase='failed', error='无法启动榜单订阅任务')
            _save(kind)
            raise
        return current(kind)


def _run(kind):
    from app import services
    try:
        rows = services.get_rank_items(kind)[:120]
        codes = list(dict.fromkeys(row['code'] for row in rows if row.get('code')))
        with _LOCK:
            _STATES[kind].update(phase='running', total=len(codes))
            _save(kind)
        for code in codes:
            with _LOCK:
                _STATES[kind]['current'] = code
                _save(kind)
            try:
                with session_scope() as session:
                    row = services.get_code_row(session, code)
                    already = row is not None and row.status >= 1
                if already:
                    outcome = 'existing'
                else:
                    outcome = 'subscribed' if services.subscribe_code(code) else 'filtered'
                with _LOCK:
                    _STATES[kind][outcome] += 1
            except Exception:
                with _LOCK:
                    _STATES[kind]['failures'].append({'code': code, 'error': '订阅失败，请检查服务连接后重试'})
            with _LOCK:
                _STATES[kind]['done'] += 1
                _save(kind)
        with _LOCK:
            _STATES[kind]['phase'] = 'partial' if _STATES[kind]['failures'] else 'completed'
    except Exception:
        with _LOCK:
            _STATES[kind].update(phase='failed', error='读取榜单失败，请检查数据源连接后重试')
    finally:
        with _LOCK:
            _STATES[kind].update(running=False, current='')
            _ACTIVE.discard(kind)
            _save(kind)
