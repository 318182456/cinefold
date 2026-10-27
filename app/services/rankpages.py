"""榜单页面先读快照，联网刷新在后台执行，不阻塞 HTTP 请求。"""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from app.database.models import Cache
from app.modules.ladysite.ranks import spec
from app.database.session import session_scope

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='rank-refresh')
_ACTIVE = set()
_STATES = {}
_RETRY_SECONDS = 60


def _save(kind):
    with session_scope() as session:
        row = session.scalar(select(Cache).where(Cache.namespace == 'rank-pages', Cache.key == kind))
        if row is None:
            row = Cache(namespace='rank-pages', key=kind)
            session.add(row)
        row.content = json.dumps(_STATES[kind], ensure_ascii=False, default=str)


def _load(kind):
    if kind in _STATES:
        return _STATES[kind]
    from app import services
    with session_scope() as session:
        row = session.scalar(select(Cache).where(Cache.namespace == 'rank-pages', Cache.key == kind))
        try:
            saved = json.loads(row.content) if row and row.content else None
        except (ValueError, TypeError):
            saved = None
    if not isinstance(saved, dict) or not isinstance(saved.get('items'), list):
        # 接住旧版 Redis/数据库快照。其抓取时间未知，先展示再刷新。
        try:
            legacy = json.loads(services.get_rank_cache('rank', kind) or '[]')
        except (ValueError, TypeError):
            legacy = []
        saved = {'items': legacy if isinstance(legacy, list) else [], 'fetched_at': 0,
                 'running': False, 'phase': 'idle', 'error': '', 'attempt_at': 0}
    if saved.get('running') and kind not in _ACTIVE:
        saved.update(running=False, phase='interrupted', error='上次刷新被服务重启中断，可重试', attempt_at=0)
    saved['items'] = [item for item in saved['items'] if isinstance(item, dict) and isinstance(item.get('code'), str)]
    _STATES[kind] = saved
    return saved


def _start(kind):
    state = _load(kind)
    if kind in _ACTIVE:
        return
    state.update(running=True, phase='fetching', error='', attempt_at=time.time())
    _ACTIVE.add(kind)
    try:
        _save(kind)
        _POOL.submit(_refresh, kind)
    except Exception:
        _ACTIVE.discard(kind)
        state.update(running=False, phase='failed', error='无法启动榜单刷新任务')
        _save(kind)
        raise


def read(kind, *, limit=120, force=False):
    from app import services
    with _LOCK:
        state = _load(kind)
        stale = time.time() - (state.get('fetched_at') or 0) >= services.RANK_CACHE_TTL
        retry_ready = time.time() - (state.get('attempt_at') or 0) >= _RETRY_SECONDS
        if force or (stale and retry_ready):
            _start(kind)
        result = copy.deepcopy(state)
    # 仅访问本地资料和订阅状态；不等待源站或任何影片详情抓取。
    items = result.pop('items')[:limit]
    known = services._rows_by_code([item['code'] for item in items])
    fields = ('title', 'cn_title', 'banner', 'poster', 'local_banner', 'portrait_side', 'casts', 'genres')
    items = [{**item, **{key: known[item['code']][key] for key in fields if known.get(item['code'], {}).get(key)}} for item in items]
    return {**result, 'items': services._listing_with_current_status(items), 'stale': stale,
            'refreshing': result.get('running', False), 'source': spec(kind)['source'], 'rank_type': kind}


def _refresh(kind):
    from app import services
    from app.modules.ladysite.transport import FetchBudget
    budget = FetchBudget(time.perf_counter() + 90)
    try:
        with budget.activate():
            # 只抓榜单、读取库内详情。缺失元数据交给已有“补全缺图”后台任务。
            items = services.get_rank_items(kind, refresh=True, fetch_limit=0)
        if not items:
            raise ValueError('未获取到有效榜单，请检查所选榜单的数据源、代理或过盾服务；已有榜单保留')
        with _LOCK:
            _STATES[kind].update(items=items, fetched_at=time.time(), phase='completed', error='')
            _save(kind)
    except Exception:
        with _LOCK:
            _STATES[kind].update(phase='failed', error='榜单刷新失败或超时，请检查所选榜单的数据源、代理或过盾服务；已有榜单保留')
    finally:
        budget.cancel()
        with _LOCK:
            _STATES[kind]['running'] = False
            _ACTIVE.discard(kind)
            _save(kind)
