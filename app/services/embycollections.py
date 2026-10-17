"""Reviewable previews and user-triggered whole-library automatic collections."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import PureWindowsPath
import posixpath
import re
import secrets
import threading
import time
import unicodedata
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Cache, Code, CodeStatus, EmbyCollectionLink, EmbyWatch, MediaLink
from app.database.session import session_scope
from app.modules.scrape.merge import base_code
from app.utils import CODE_PATTERN, get_true_code

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='emby-collections')
_SECRET = secrets.token_bytes(32)
_PLAN = None
_STATE = {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'error': '', 'groups': [], 'token': ''}
_KINDS = {'series', 'theme', 'sequel', 'actor', 'style'}
_MODES = {'all', *_KINDS}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _core(code):
    normalized = get_true_code(code or '')
    return base_code(normalized) or normalized


def _name(name):
    name = unicodedata.normalize('NFKC', str(name or '')).strip()
    if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
        raise ValueError('合集名称不能为空、不能含控制字符，且最多128字')
    return name


def _context():
    settings = get_settings()
    url = settings.emby_url.rstrip('/')
    if not url or not settings.emby_api_key or not settings.emby_user_id:
        raise ValueError('请先配置 Emby 地址、API Key 并选择已同步的 Emby 用户')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError('Emby 地址格式无效')
    return {'url': url, 'server': hashlib.sha256(url.encode()).hexdigest(), 'user_id': settings.emby_user_id,
            'credential': hashlib.sha256(settings.emby_api_key.encode()).hexdigest()}, settings.emby_api_key


def _snapshot(limit):
    context, _ = _context()
    with session_scope() as session:
        watches = {}
        for row in session.scalars(select(EmbyWatch).where(EmbyWatch.server == context['server'], EmbyWatch.user_id == context['user_id'])):
            watches.setdefault(_core(row.code), set()).add(row.item_id)
        paths = {}
        for row in session.scalars(select(MediaLink)):
            paths.setdefault(_core(row.code), set()).add(row.link_path)
        grouped = {}
        for row in session.scalars(select(Code)):
            core = _core(row.code)
            if core and (core in watches or core in paths or row.status == CodeStatus.COMPLETED):
                grouped.setdefault(core, []).append(row)
        items = []
        for core, rows in grouped.items():
            rows.sort(key=lambda row: (row.code != core, row.code))
            values = {}
            for row in rows:
                for field in ('title', 'cn_title', 'series', 'casts', 'genres', 'outline', 'release_date', 'poster', 'banner'):
                    if not values.get(field) and getattr(row, field):
                        values[field] = getattr(row, field)
            items.append({'code': core, **values, 'emby_ids': sorted(watches.get(core, [])),
                          'paths': sorted(paths.get(core, [])), 'missing': not bool(watches.get(core))})
    items.sort(key=lambda item: (item.get('release_date') or '', item['code']), reverse=True)
    mapped = [item for item in items if not item['missing']]
    scoped = mapped[:limit] if limit else mapped
    return {'context': context, 'items': scoped, 'available': len(mapped), 'unmapped': len(items) - len(mapped),
            'truncated': bool(limit and len(mapped) > limit), 'limit': limit, 'fingerprint': _hash([context, scoped])}


def current():
    global _PLAN
    with _LOCK:
        if _STATE['phase'] == 'idle':
            with session_scope() as session:
                row = session.scalar(select(Cache).where(Cache.namespace=='emby-collections', Cache.key=='current'))
                saved = json.loads(row.content) if row and row.content else {}
            if saved:
                _STATE.update(saved['state'])
                _PLAN = saved.get('plan')
                if _STATE['running']:
                    _STATE.update(running=False, phase='interrupted', error='进程重启，任务已中断，可继续原任务')
        result = copy.deepcopy(_STATE)
        result['can_resume'] = bool(_PLAN and _PLAN.get('automatic') and not _STATE['running'] and _STATE['phase'] in ('interrupted','failed','partial'))
        return result


def _save():
    with session_scope() as session:
        row = session.scalar(select(Cache).where(Cache.namespace=='emby-collections', Cache.key=='current'))
        if row is None:
            row = Cache(namespace='emby-collections', key='current')
            session.add(row)
        row.content = json.dumps({'state':_STATE, 'plan':_PLAN}, ensure_ascii=False)


def _update(**values):
    with _LOCK:
        _STATE.update(values)
        _STATE['updated_at'] = time.time()
        _save()


def _ai(items, mode):
    from app.modules.collections.collectionai import suggest
    return suggest(items, mode)


def _automatic_name(context, kind, codes, proposed):
    """Keep the name stable if another AI run renames identical membership."""
    key = _hash([context['server'], context['user_id'], kind, sorted(codes)])
    labels = {'series':'系列', 'theme':'主题', 'sequel':'续作', 'actor':'演员', 'style':'风格'}
    prefix = labels[kind]+' · '
    name = proposed if proposed.startswith(prefix) else prefix+proposed
    if len(name)>128:
        name = name[:115]+' · '+_hash(name)[:8]
    with session_scope() as session:
        row = session.scalar(select(Cache).where(Cache.namespace=='emby-collection-names', Cache.key==key))
        if row and row.content:
            return _name(row.content)
        session.add(Cache(namespace='emby-collection-names',key=key,content=name))
    return name


def suggest(mode='all', limit=200, automatic=False):
    global _PLAN
    if mode not in _MODES:
        raise ValueError('不支持的合集模式；同片多版本合集暂不支持')
    limit = max(20, min(500, limit)) if limit else 0
    _context()
    current()
    with _LOCK:
        if _STATE['running']:
            raise ValueError('合集建议或发布正在运行，请稍后再试')
        _PLAN = None
        _update(running=True, phase='suggesting', error='', done=0, total=0, groups=[], token='',
                mode=mode, limit=limit, automatic=automatic, current='', analyzed=0, started_at=time.time(), finished_at=None)
    try:
        _POOL.submit(_suggest_work, mode, limit, automatic)
    except Exception:
        _update(running=False, phase='failed', error='无法启动合集建议')
        raise
    return current()


def _suggest_work(mode, limit, automatic):
    global _PLAN
    from app.modules.collections.batches import batches, explicit_groups, merge_candidates
    try:
        if not _PLAN:
            snapshot = _snapshot(limit)
            if len(snapshot['items']) < 2:
                raise ValueError('当前用户至少需要同步两部不同番号的 Emby 影片')
            with _LOCK:
                _PLAN = {'snapshot':snapshot, 'candidates':[], 'next_batch':0, 'automatic':automatic, 'mode':mode}
                _update(scope={'limit':limit, 'included':len(snapshot['items']), 'available':snapshot['available'],
                        'unmapped':snapshot['unmapped'], 'truncated':snapshot['truncated'],
                        'description':'覆盖当前用户已同步映射的全部影片' if not limit else '本次按指定上限分析'},
                        total=len(snapshot['items']))
        snapshot = _PLAN['snapshot']
        if 'groups' not in _PLAN:
            chunks = list(batches(snapshot['items']))
            seed = explicit_groups(snapshot['items'], mode) if not limit else []
            for index in range(_PLAN['next_batch'], len(chunks)):
                chunk = chunks[index]
                safe_items = chunk if len(chunk)>1 else [chunks[index-1][-1], *chunk]
                _update(phase='suggesting', current=f'第 {index+1} / {len(chunks)} 批', batch=index+1, batches=len(chunks))
                allowed = {item['code'] for item in safe_items}
                candidates = []
                for candidate in _ai(safe_items, mode):
                    codes = list(dict.fromkeys(_core(code) for code in candidate.get('codes',[]) if _core(code) in allowed))
                    if any(row['kind']==candidate.get('kind') and set(codes)<=set(row['codes']) for row in seed):
                        continue
                    candidates.append({**candidate, 'codes':codes})
                with _LOCK:
                    _PLAN['candidates'].extend(candidates)
                    _PLAN['next_batch'] = index+1
                    done = sum(len(batch) for batch in chunks[:index+1])
                    _update(done=done, analyzed=done)
            candidates = merge_candidates(seed + _PLAN['candidates'])
            known = {item['code']: item for item in snapshot['items']}
            groups, seen = [], set()
            for candidate in candidates:
                kind = candidate.get('kind') or (mode if mode in _KINDS else 'theme')
                if kind not in _KINDS or mode!='all' and kind!=mode:
                    continue
                name = _name(candidate.get('name'))
                codes = list(dict.fromkeys(_core(code) for code in candidate.get('codes', []) if _core(code) in known))
                if len(codes) < 2:
                    continue
                if automatic:
                    name = _automatic_name(snapshot['context'], kind, codes, name)
                identity = (kind, name.casefold(), tuple(sorted(codes)))
                if identity in seen:
                    continue
                seen.add(identity)
                group_id = _hash(identity)[:24]
                groups.append({'id': group_id, 'name': name, 'kind': kind, 'reason': str(candidate.get('reason') or '')[:2000],
                               'confidence': candidate.get('confidence'), 'codes': codes,
                               'items': [] if automatic else [{key: value for key, value in known[code].items() if key != 'paths'} for code in codes],
                               'publish_status': 'preview', 'missing': [], 'can_publish': True})
            issued = time.time()
            token = hmac.new(_SECRET, _hash([snapshot['fingerprint'], groups, issued]).encode(), hashlib.sha256).hexdigest()
            with _LOCK:
                _PLAN.update(groups=copy.deepcopy(groups), token=token, issued=issued)
                _update(phase='ready', groups=groups, token=token, total=len(groups), done=0, current='')
        if automatic:
            latest = _snapshot(limit)
            known = {item['code']:item for item in latest['items']}
            if latest['context'] != snapshot['context'] or any(known.get(item['code']) != item for item in snapshot['items']):
                raise ValueError('已分析影片的 Emby 关联或资料发生变化，请重新开始全库任务')
            done = sum(row.get('publish_status')=='published' for row in _STATE['groups'])
            selected = [row for row in _STATE['groups'] if row.get('publish_status')!='published']
            _update(phase='publishing', done=done, total=len(_STATE['groups']))
            _publish_work(copy.deepcopy(_PLAN), copy.deepcopy(selected), finish=False)
    except Exception as exc:
        _update(phase='failed', error=str(exc))
    finally:
        _update(running=False, finished_at=time.time(), current='')


def automatic(mode='all'):
    from app.services import scrapemaintenance
    if scrapemaintenance.busy():
        raise ValueError('媒体库正在重建，请等待完成后再创建合集')
    return suggest(mode, 0, automatic=True)


def resume():
    from app.services import scrapemaintenance
    if scrapemaintenance.busy():
        raise ValueError('媒体库正在重建，请等待完成后再继续合集任务')
    with _LOCK:
        if not current().get('can_resume'):
            raise ValueError('没有可继续的自动合集任务')
        context, _ = _context()
        if _PLAN and _PLAN['snapshot']['context'] != context:
            raise ValueError('Emby 配置已变化，请重新开始全库任务')
        _update(running=True, error='', finished_at=None)
        try:
            _POOL.submit(_suggest_work, _STATE['mode'], _STATE['limit'], True)
        except Exception:
            _update(running=False, phase='failed', error='无法继续合集任务')
            raise
    return current()


def _path(value):
    raw = str(value or '')
    normalized = posixpath.normpath(raw.replace('\\', '/')) if raw else ''
    return normalized.casefold() if re.match(r'^[A-Za-z]:/', normalized) or raw.startswith('\\\\') else normalized


def _get(client, context, resource, **params):
    response = client.get(context['url'] + '/emby/' + resource, params=params)
    response.raise_for_status()
    return response.json()


def _client(credential):
    return httpx.Client(timeout=20, trust_env=False, headers={'X-Emby-Token': credential})


def _paged(client, context, **params):
    result, start = [], 0
    for _ in range(100):
        data = _get(client, context, f"Users/{quote(context['user_id'], safe='')}/Items", **params, StartIndex=start, Limit=200)
        batch = data.get('Items', [])
        result.extend(batch)
        start += len(batch)
        if not batch or start >= data.get('TotalRecordCount', start):
            return result
    raise ValueError('Emby 返回过多条目，未能完整验证合集')


def _verify_movie(client, context, item_id, expected, snapshot):
    item = _get(client, context, f"Users/{quote(context['user_id'], safe='')}/Items/{quote(item_id, safe='')}")
    if str(item.get('Id') or '') != item_id or item.get('Type') != 'Movie':
        raise ValueError(f'{expected["code"]} 的 Emby ID 已失效或不是影片，请重新同步')
    path = _path(item.get('Path'))
    path_owners = snapshot['_path_owners'].get(path, set()) if '_path_owners' in snapshot else {
        entry['code'] for entry in snapshot['items'] if path and path in {_path(p) for p in entry['paths']}}
    if path_owners and path_owners != {expected['code']}:
        raise ValueError(f'{expected["code"]} 的 Emby 路径对应其他番号')
    text = unicodedata.normalize('NFKC', f"{PureWindowsPath(item.get('Path') or '').stem} {item.get('Name') or ''}")
    # Page identity cannot use filename normalization that guesses DMM zero padding.
    codes = {_core(match[1]) for match in CODE_PATTERN.finditer(text)}
    for field, value in (item.get('ProviderIds') or {}).items():
        if field.casefold() in {'num', 'number', 'code', 'javbus'}:
            normalized = _core(str(value))
            if normalized:
                codes.add(normalized)
    if codes and codes != {expected['code']}:
        raise ValueError(f'{expected["code"]} 的 Emby 影片番号不匹配，禁止发布')
    if path_owners != {expected['code']} and codes != {expected['code']}:
        raise ValueError(f'{expected["code"]} 的 Emby 影片缺少可验证的路径或番号')


def _collection_members(client, context, collection_id):
    return {str(item['Id']) for item in _paged(client, context, ParentId=collection_id, IncludeItemTypes='Movie', Recursive='false') if item.get('Type') == 'Movie'}


def _same_name(client, context, name):
    return [item for item in _paged(client, context, IncludeItemTypes='BoxSet', Recursive='true', SearchTerm=name)
            if item.get('Type') == 'BoxSet' and str(item.get('Name') or '').strip().casefold() == name.casefold()]


def _record(key, **values):
    with session_scope() as session:
        row = session.get(EmbyCollectionLink, key)
        for field, value in values.items():
            setattr(row, field, value)


def _publish_group(client, context, group, ids, fingerprint):
    key = _hash([context['server'], context['user_id'], group['name'].casefold()])
    with session_scope() as session:
        managed = session.get(EmbyCollectionLink, key)
        prior = managed.to_dict() if managed else None
    matches = _same_name(client, context, group['name'])
    if len(matches) > 1:
        raise ValueError('Emby 存在多个完全同名合集，请先人工核对')
    remote_id = prior.get('remote_id') if prior else None
    if remote_id:
        remote = _get(client, context, f"Users/{quote(context['user_id'], safe='')}/Items/{quote(remote_id, safe='')}")
        if str(remote.get('Id')) != remote_id or remote.get('Type') != 'BoxSet' or str(remote.get('Name', '')).strip().casefold() != group['name'].casefold():
            raise ValueError('受管合集身份已变化，不能修改其他合集')
        if matches and str(matches[0]['Id']) != remote_id:
            raise ValueError('同名合集不属于本系统管理')
    elif prior and prior['status'] in ('creating', 'unknown'):
        if not matches:
            raise ValueError('上次创建结果不确定且尚未找到同名合集；停止重发，请先人工核对 Emby')
        candidate_id = str(matches[0]['Id'])
        if _collection_members(client, context, candidate_id) != set(prior.get('member_ids') or []):
            raise ValueError('上次创建结果不确定，同名合集成员不一致；不能擅自接管')
        remote_id = candidate_id
        _record(key, remote_id=remote_id, status='managed', error='')
    elif matches:
        raise ValueError('Emby 已有同名外部合集，未接管；请修改建议名称后再发布')
    if not remote_id:
        initial_ids = sorted(ids)[:100]
        with session_scope() as session:
            managed = session.get(EmbyCollectionLink, key)
            if managed is None:
                managed = EmbyCollectionLink(key=key, server=context['server'], user_id=context['user_id'], name=group['name'])
                session.add(managed)
            managed.status, managed.member_ids, managed.source_fingerprint = 'creating', initial_ids, fingerprint
            managed.error = ''
        try:
            response = client.post(context['url'] + '/emby/Collections', params={'Name': group['name'], 'Ids': ','.join(initial_ids), 'IsLocked': 'false'})
            response.raise_for_status()
            remote_id = str(response.json().get('Id') or '')
            if not remote_id:
                raise ValueError('Emby 创建响应缺少合集 ID')
            created = _get(client, context, f"Users/{quote(context['user_id'], safe='')}/Items/{quote(remote_id, safe='')}")
            if str(created.get('Id')) != remote_id or created.get('Type') != 'BoxSet' or str(created.get('Name', '')).strip().casefold() != group['name'].casefold():
                raise ValueError('Emby 创建响应的合集身份不符')
        except Exception:
            _record(key, status='unknown', error='创建结果不确定；下次仅核对同名合集及成员，不盲目重发')
            raise ValueError('Emby 创建结果不确定，请保留建议并核对后重试') from None
        _record(key, remote_id=remote_id, status='managed', error='')
    current_ids = _collection_members(client, context, remote_id)
    missing = set(ids) - current_ids
    while missing:
        batch = set(sorted(missing)[:100])
        # Adding is idempotent: a timeout is reconciled against members before retry.
        _record(key, status='appending', member_ids=sorted(current_ids | batch), error='')
        try:
            response = client.post(context['url'] + f'/emby/Collections/{quote(remote_id, safe="")}/Items', params={'Ids': ','.join(sorted(batch))})
            response.raise_for_status()
            current_ids = _collection_members(client, context, remote_id)
            if not batch <= current_ids:
                raise ValueError('Emby 尚未返回全部新增成员')
        except Exception:
            _record(key, status='append_unknown', error='追加结果不确定；重试前将先核对成员')
            raise ValueError('Emby 追加结果不确定，请核对后重试') from None
        missing = set(ids) - current_ids
    _record(key, status='published', member_ids=sorted(current_ids), source_fingerprint=fingerprint, error='')
    return remote_id


def publish(token, groups):
    from app.services import scrapemaintenance
    if scrapemaintenance.busy():
        raise ValueError('媒体库正在重建，请等待完成后再发布合集')
    if len(groups) > 40:
        raise ValueError('每次最多发布40组合集')
    with _LOCK:
        if _STATE['running']:
            raise ValueError('合集任务正在运行')
        if not _PLAN or not token or not hmac.compare_digest(token, _PLAN['token']) or time.time() - _PLAN['issued'] > 3600:
            raise ValueError('合集预览已失效，请重新生成建议')
        plan = copy.deepcopy(_PLAN)
        known = {group['id']: group for group in plan['groups']}
        selected, seen, selected_names = [], set(), set()
        for group in groups:
            original = known.get(group.get('id'))
            if original is None or group['id'] in seen:
                raise ValueError('请选择有效且不重复的建议组')
            seen.add(group['id'])
            if len(group.get('codes', [])) > 500:
                raise ValueError('每组合集最多500部影片')
            codes = list(dict.fromkeys(group.get('codes', [])))
            if len(codes) < 2 or any(code not in original['codes'] for code in codes):
                raise ValueError('每组至少选择两个不同基础番号，且只能选择原建议成员')
            name = _name(group.get('name'))
            if name.casefold() in selected_names:
                raise ValueError('所选合集名称重复，请修改名称后再发布')
            selected_names.add(name.casefold())
            selected.append({**original, 'name': name, 'codes': codes})
        if not selected:
            raise ValueError('请选择要发布的合集')
        latest = _snapshot(plan['snapshot']['limit'])
        if latest['fingerprint'] != plan['snapshot']['fingerprint']:
            raise ValueError('Emby配置、影片或映射已变化，请重新生成建议')
        _update(running=True, phase='publishing', done=0, total=len(selected), error='')
    try:
        _POOL.submit(_publish_work, plan, selected)
    except Exception:
        _update(running=False, phase='failed', error='无法启动合集发布')
        raise
    return current()


def _publish_work(plan, selected, finish=True):
    from app.services import scrapemaintenance
    try:
        with scrapemaintenance.activity():
            context, credential = _context()
            if context != plan['snapshot']['context']:
                raise ValueError('Emby配置已变化，请重新生成建议')
            items = {item['code']: item for item in plan['snapshot']['items']}
            snapshot = {**plan['snapshot'], '_path_owners':{}}
            for item in items.values():
                for path in item['paths']:
                    snapshot['_path_owners'].setdefault(_path(path), set()).add(item['code'])
            verified = set()
            failures = []
            with _client(credential) as client:
                for group in selected:
                    _update(current=group['name'])
                    try:
                        ids = set()
                        for code in group['codes']:
                            item = items[code]
                            if not item['emby_ids']:
                                raise ValueError(f'{code} 缺少 Emby 影片 ID，请先同步')
                            for item_id in item['emby_ids']:
                                if item_id not in verified:
                                    _verify_movie(client, context, item_id, item, snapshot)
                                    verified.add(item_id)
                                ids.add(item_id)
                        remote_id = _publish_group(client, context, group, ids, plan['snapshot']['fingerprint'])
                        status, error = 'published', ''
                    except Exception as exc:
                        remote_id, status, error = None, 'failed', str(exc)
                        failures.append(error)
                    with _LOCK:
                        _STATE['done'] += 1
                        for display in _STATE['groups']:
                            if display['id'] == group['id']:
                                display.update(publish_status=status, publish_error=error, remote_id=remote_id,
                                               name=group['name'], published_codes=group['codes'] if status == 'published' else [])
                        _update()
            _update(phase='partial' if failures else 'published', error='；'.join(failures)[:4000])
    except Exception as exc:
        _update(phase='failed', error=str(exc))
    finally:
        if finish:
            _update(running=False, finished_at=time.time(), current='')
