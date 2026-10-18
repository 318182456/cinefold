"""按当前页补全演员头像，精确匹配、限量并发，失败冷却，不修改订阅。"""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from app.database.models import Actor, ActorProfile, Cache
from app.database.session import session_scope
from app.utils.actornames import normalize, valid, names as clean_names, presentation

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='actor-photos')
_STATE = None
_COOLDOWN = 6 * 3600


def _save():
    with session_scope() as session:
        row = session.scalar(select(Cache).where(Cache.namespace == 'actor-photos', Cache.key == 'current'))
        if row is None:
            row = Cache(namespace='actor-photos', key='current')
            session.add(row)
        _STATE['updated_at'] = time.time()
        row.content = json.dumps(_STATE, ensure_ascii=False)


def current():
    global _STATE
    with _LOCK:
        if _STATE is None:
            with session_scope() as session:
                row = session.scalar(select(Cache).where(Cache.namespace == 'actor-photos', Cache.key == 'current'))
                _STATE = json.loads(row.content) if row and row.content else {'running': False, 'phase': 'idle', 'results': []}
            if _STATE.get('running'):
                _STATE.update(running=False, phase='interrupted', error='服务重启，已保存的头像保留；打开演员页后补全缺项')
                _save()
        return copy.deepcopy(_STATE)


def start(names, force=False):
    global _STATE
    wanted = list(dict.fromkeys(normalize(name) for name in names if valid(name)))[:24]
    with _LOCK:
        if current()['running']:
            return current()
        pending = []
        with session_scope() as session:
            for name in wanted:
                actor = session.get(Actor, name)
                cached = session.get(ActorProfile, name)
                data = (cached.data or {}) if cached else {}
                aliases = [*clean_names(actor.name_2 if actor else ''), *clean_names(data.get('aliases'))]
                needs_name = presentation(name, aliases)['needs_name_lookup']
                if ((actor and actor.photo) or data.get('photo')) and not needs_name:
                    continue
                if not force and time.time() - data.get('photo_checked_at', 0) < _COOLDOWN:
                    continue
                pending.append(name)
        if not pending:
            return {**current(), 'started': False}
        _STATE = {'running': True, 'phase': 'fetching', 'names': pending, 'total': len(pending), 'done': 0,
                  'ok': 0, 'failed': 0, 'results': [], 'started_at': time.time(), 'error': ''}
        try:
            _save()
            _POOL.submit(_run, pending)
        except Exception:
            _STATE.update(running=False, phase='failed', error='无法启动头像补全任务')
            _save()
            raise
        return {**current(), 'started': True}


def _fetch(name):
    from app.modules.ladysite import get_actor_identity
    with session_scope() as session:
        actor = session.get(Actor, name)
        cached = session.get(ActorProfile, name)
        data = (cached.data or {}) if cached else {}
        aliases = [*clean_names(actor.name_2 if actor else ''), *clean_names(data.get('aliases'))]
        lookup = presentation(name, aliases, data.get('native_name', ''))['display_name']
    return get_actor_identity(lookup)


def _run(names):
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='actor-photo-item') as pool:
            futures = {pool.submit(_fetch, name): name for name in names}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    identity = future.result() or {}
                    photo = identity.get('photo', '')
                except Exception:
                    identity, photo = {}, ''
                with session_scope() as session:
                    cached = session.get(ActorProfile, name)
                    if cached is None:
                        cached = ActorProfile(name=name, data={})
                        session.add(cached)
                    data = dict(cached.data or {})
                    if identity:
                        data['aliases'] = clean_names([*clean_names(data.get('aliases')), *clean_names(identity.get('aliases')), identity.get('name', '')])
                        data['native_name'] = identity.get('name', '')
                    data.update(photo_checked_at=time.time(), photo_error='' if photo else '未找到精确匹配头像，请更新 JavDB 档案或检查数据源')
                    if photo:
                        data['photo'] = photo
                        actor = session.get(Actor, name)
                        if actor is None:
                            actor = Actor(name=name, subscribed=False)
                            session.add(actor)
                        if not actor.photo:
                            actor.photo = photo
                    cached.data = data
                with _LOCK:
                    _STATE['done'] += 1
                    _STATE['ok' if photo else 'failed'] += 1
                    _STATE['results'].append({'name': name, 'ok': bool(photo)})
                    _save()
        with _LOCK:
            _STATE['phase'] = 'partial' if _STATE['failed'] else 'completed'
    except Exception:
        with _LOCK:
            _STATE.update(phase='failed', error='头像补全中断，已取得的头像保留')
    finally:
        with _LOCK:
            _STATE['running'] = False
            _save()
