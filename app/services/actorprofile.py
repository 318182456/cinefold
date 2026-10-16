"""本地演员档案与有界、显式触发的远程作品同步。"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import re
import threading
import time
import unicodedata

from sqlalchemy import select, update

from app.database.models import Actor, ActorProfile, Code, CodeStatus, EmbyWatch, LibraryPreference, MediaLink, RuleExclusion
from app.database.session import session_scope
from app.modules.ladysite.transport import FetchBudget
from app.modules.scrape.merge import base_code, clean_detail
from app.utils import get_true_code

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='actor-profile')
_CAPACITY = threading.BoundedSemaphore(4)
_JOBS = {}
_SPLIT = re.compile(r'[,，、;；|/\n]+')
_FIELDS = ('photo', 'birthday', 'height', 'measurements', 'description', 'source_url')


def _name(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def _key(value):
    return _name(value).casefold()


def names(value):
    """演员名按独立 token 匹配，姓名中的空格不作分隔符。"""
    out = []
    for raw in _SPLIT.split(_name(value)):
        raw = raw.strip()
        if not raw:
            continue
        match = re.fullmatch(r'([^()]+)\(([^()]+)\)', raw)
        out.extend([match[1].strip(), match[2].strip()] if match else [raw])
    return list(dict.fromkeys(out))


def _identity(session, name):
    aliases = {_key(name)}
    actors = list(session.scalars(select(Actor)))
    profiles = list(session.scalars(select(ActorProfile)))
    # A cached alias is authoritative only when attached to an exact name token.
    changed = True
    while changed:
        changed = False
        for row in [*actors, *profiles]:
            values = [row.name, *(names(row.name_2) if isinstance(row, Actor) else (row.data or {}).get('aliases', []))]
            keys = {_key(value) for value in values if _name(value)}
            if aliases & keys and not keys <= aliases:
                aliases.update(keys)
                changed = True
    actor = next((row for row in actors if _key(row.name) == _key(name)), None)
    actor = actor or next((row for row in actors if _key(row.name) in aliases), None)
    profile = next((row for row in profiles if _key(row.name) == _key(name)), None)
    profile = profile or next((row for row in profiles if _key(row.name) in aliases), None)
    return aliases, actor, profile


def sync_state(name):
    with _LOCK:
        return copy.deepcopy(_JOBS.get(_key(name), {'running': False, 'phase': 'idle', 'error': '', 'imported': 0, 'remote_pages': 0, 'truncated': False}))


def profile(name, *, page=1, size=24, filter='all'):
    name = _name(name)
    if not name or len(name) > 128:
        raise ValueError('请填写有效演员姓名（最多128字）')
    if filter not in {'all', 'owned', 'missing', 'favorite', 'watched'}:
        raise ValueError('未知作品筛选条件')
    page, size = max(1, page), max(1, min(100, size))
    with session_scope() as session:
        aliases, actor, cached = _identity(session, name)
        data = copy.deepcopy(cached.data or {}) if cached else {}
        data.update(name=actor.name if actor else cached.name if cached else name,
                    aliases=list(dict.fromkeys([*(data.get('aliases') or []), *(names(actor.name_2) if actor else [])])),
                    photo=(actor.photo if actor else '') or data.get('photo') or '',
                    subscribed=bool(actor and actor.subscribed),
                    updated_at=cached.updated_at.isoformat() if cached and cached.updated_at else None)
        for field in _FIELDS:
            data.setdefault(field, '')
        grouped = {}
        for row in session.scalars(select(Code)):
            core = base_code(row.code) or row.code
            grouped.setdefault(core, []).append(row)
        owned = {base_code(code) or code for code in session.scalars(select(MediaLink.code))}
        owned.update(base_code(row.code) or row.code for rows in grouped.values() for row in rows if row.status == CodeStatus.COMPLETED)
        favorite = {base_code(code) or code for code in session.scalars(select(LibraryPreference.code).where(LibraryPreference.favorite.is_(True)))}
        watched = set()
        for watch in session.scalars(select(EmbyWatch)):
            core = base_code(watch.code) or watch.code
            if watch.favorite:
                favorite.add(core)
            if watch.played or watch.play_count > 0:
                watched.add(core)
        stats = dict(total=0, owned=0, missing=0, favorite=0, watched=0)
        items = []
        for core, rows in grouped.items():
            if not any(aliases & {_key(token) for token in names(row.casts)} for row in rows):
                continue
            flags = {'owned': core in owned, 'missing': core not in owned, 'favorite': core in favorite, 'watched': core in watched}
            stats['total'] += 1
            for flag, enabled in flags.items():
                stats[flag] += int(enabled)
            if filter != 'all' and not flags[filter]:
                continue
            rows.sort(key=lambda row: (row.code != core, -int(row.status == CodeStatus.COMPLETED), row.code))
            item = rows[0].to_dict()
            for row in rows[1:]:
                for key, value in row.to_dict().items():
                    if item.get(key) in (None, '') and value not in (None, ''):
                        item[key] = value
            item.update(code=core, **flags)
            items.append(item)
        items.sort(key=lambda item: (item.get('release_date') or '', item['code']), reverse=True)
        total = len(items)
        page = min(page, max(1, (total + size - 1) // size))
        sync = sync_state(name)
        if sync['phase'] == 'idle' and cached and cached.error:
            sync.update(phase='failed', error=cached.error)
        return {'actor': data, 'stats': stats, 'items': items[(page - 1) * size:page * size],
                'total': total, 'page': page, 'size': size, 'sync': sync}


def directory(*, keyword='', page=1, size=15, subscribed=True):
    page, size = max(1, page), max(1, min(100, size))
    with session_scope() as session:
        rows = list(session.scalars(select(Actor)))
        items = {_key(row.name): row.to_dict() for row in rows if not subscribed or row.subscribed}
        if not subscribed:
            canonical = {}
            for row in rows:
                for token in [row.name, *names(row.name_2)]:
                    canonical[_key(token)] = _key(row.name)
            for casts in session.scalars(select(Code.casts).where(Code.casts.is_not(None))):
                for token in names(casts):
                    key = canonical.get(_key(token), _key(token))
                    items.setdefault(key, {'name': token, 'name_2': '', 'photo': '', 'subscribed': False})
        found = [item for item in items.values() if not keyword or _key(keyword) in _key(item['name']) or _key(keyword) in _key(item.get('name_2'))]
        found.sort(key=lambda item: _key(item['name']))
        return {'items': found[(page - 1) * size:page * size], 'total': len(found), 'page': page, 'size': size}


def subscribe_works(name):
    name = _name(name)
    if not name or len(name) > 128:
        raise ValueError('请填写有效演员姓名（最多128字）')
    result = dict(matched=0, subscribed=0, already_subscribed=0, owned=0, downloading=0, excluded=0, skipped=0)
    with session_scope() as session:
        aliases, _, _ = _identity(session, name)
        grouped = {}
        for row in session.scalars(select(Code).with_for_update()):
            grouped.setdefault(base_code(row.code) or row.code, []).append(row)
        owned = {base_code(code) or code for code in session.scalars(select(MediaLink.code))}
        excluded = {base_code(code) or code for code in session.scalars(select(RuleExclusion.code))}
        for core, rows in grouped.items():
            if not any(aliases & {_key(token) for token in names(row.casts)} for row in rows):
                continue
            result['matched'] += 1
            if core in owned or any(row.status == CodeStatus.COMPLETED for row in rows):
                result['owned'] += 1
            elif core in excluded:
                result['excluded'] += 1
            elif any(row.status in (CodeStatus.DOWNLOADING, CodeStatus.DOWNLOADED) for row in rows):
                result['downloading'] += 1
            elif any(row.status == CodeStatus.SUBSCRIBED for row in rows):
                result['already_subscribed'] += 1
            else:
                candidates = [row for row in rows if row.status in (CodeStatus.NONE, CodeStatus.FAILED)]
                candidates.sort(key=lambda row: (row.code != core, row.code))
                if candidates:
                    changed = session.execute(update(Code).where(
                        Code.code == candidates[0].code,
                        Code.status.in_((CodeStatus.NONE, CodeStatus.FAILED)),
                    ).values(status=CodeStatus.SUBSCRIBED)).rowcount
                    result['subscribed' if changed else 'skipped'] += 1
                else:
                    result['skipped'] += 1
    return result


def _fetch(name, page, actor_url):
    from app.modules.ladysite.javdb_actor import fetch_profile
    return fetch_profile(name, page=page, actor_url=actor_url)


def _import(name, payload):
    aliases = list(dict.fromkeys([*payload.get('aliases', []), *([payload['name']] if payload.get('name') and payload['name'] != name else [])]))
    imported = 0
    with session_scope() as session:
        cached = session.get(ActorProfile, name)
        if cached is None:
            cached = ActorProfile(name=name, data={})
            session.add(cached)
        data = dict(cached.data or {})
        data['aliases'] = list(dict.fromkeys([*data.get('aliases', []), *aliases]))
        for field in _FIELDS:
            if payload.get(field):
                data[field] = payload[field]
        cached.data, cached.updated_at, cached.error = data, datetime.now(), ''
        actor = session.get(Actor, name)
        if actor is None:
            actor = Actor(name=name, subscribed=False)
            session.add(actor)
        if not actor.photo and data.get('photo'):
            actor.photo = data['photo']
        if not actor.name_2 and aliases:
            actor.name_2 = ','.join(aliases)
        for item in payload.get('items', []):
            code = get_true_code(item.get('code', ''))
            if not code:
                continue
            row = session.get(Code, code)
            if row is None:
                row = Code(code=code, status=CodeStatus.NONE)
                session.add(row)
                imported += 1
            detail = clean_detail(item, code)
            if not detail.get('casts'):
                detail['casts'] = name
            elif _key(name) not in {_key(token) for token in names(detail['casts'])}:
                detail['casts'] += ',' + name
            confirmed = {_key(value) for value in [name, *aliases]}
            if row.casts and not confirmed & {_key(token) for token in names(row.casts)}:
                # An actor page verifies this performer, but does not provide
                # a complete cast. Enrich the cast without losing other names.
                row.casts = row.casts.rstrip(',') + ',' + name
            for field, value in detail.items():
                if field != 'code' and hasattr(Code, field) and getattr(row, field, None) in (None, ''):
                    setattr(row, field, value)
    return imported


def refresh(name, max_pages=3):
    name = _name(name)
    if not name or len(name) > 128:
        raise ValueError('请填写有效演员姓名（最多128字）')
    max_pages = max(1, min(10, max_pages))
    key = _key(name)
    with _LOCK:
        if _JOBS.get(key, {}).get('running'):
            return sync_state(name)
        if not _CAPACITY.acquire(blocking=False):
            raise ValueError('已有4个演员同步任务，请稍后重试')
        for old in list(_JOBS):
            if len(_JOBS) < 256:
                break
            if not _JOBS[old].get('running'):
                _JOBS.pop(old)
        _JOBS[key] = {'running': True, 'phase': 'queued', 'error': '', 'imported': 0, 'remote_pages': 0,
                      'truncated': False, 'max_pages': max_pages}
    def work():
        budget = FetchBudget(time.perf_counter() + 45)
        try:
            with session_scope() as session:
                _, _, cached = _identity(session, name)
                actor_url = (cached.data or {}).get('source_url', '') if cached else ''
            with budget.activate():
                for page in range(1, max_pages + 1):
                    budget.check()
                    with _LOCK:
                        _JOBS[key].update(phase='fetching')
                    payload = _fetch(name, page, actor_url)
                    budget.check()
                    imported = _import(name, payload)
                    actor_url = payload.get('source_url') or actor_url
                    with _LOCK:
                        _JOBS[key]['imported'] += imported
                        _JOBS[key].update(remote_pages=page, truncated=bool(payload.get('has_more')))
                    if not payload.get('has_more'):
                        break
            with _LOCK:
                _JOBS[key].update(phase='finished')
        except Exception as exc:
            with _LOCK:
                _JOBS[key].update(phase='failed', error=str(exc), truncated=True)
            with session_scope() as session:
                cached = session.get(ActorProfile, name)
                if cached is None:
                    cached = ActorProfile(name=name, data={})
                    session.add(cached)
                cached.error = str(exc)
        finally:
            budget.cancel()
            with _LOCK:
                _JOBS[key]['running'] = False
            _CAPACITY.release()
    try:
        _POOL.submit(work)
    except Exception:
        with _LOCK:
            _JOBS[key].update(running=False, phase='failed', error='无法启动演员同步')
        _CAPACITY.release()
        raise
    return sync_state(name)
