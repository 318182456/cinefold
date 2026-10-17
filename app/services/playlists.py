"""动态智能片单规则；与自动订阅规则分开校验，避免影响下载条件。"""
from app.services.rules import matches, validate_conditions


SYSTEM_PLAYLISTS = [
    {'key': 'top_played', 'name': '播放最多', 'sort': 'plays', 'description': '按 Emby 累计播放次数排序'},
    {'key': 'top_rated', 'name': '评分最高', 'sort': 'rating', 'description': '按个人评分排序'},
    {'key': 'most_actresses', 'name': '群星荟萃', 'sort': 'casts', 'description': '按出演演员人数排序'},
    {'key': 'latest_added', 'name': '最近入库', 'sort': 'added', 'description': '按影片首次登记时间排序'},
]
SORTS = {'recent', 'release', 'rating', 'plays', 'size', 'casts', 'added'}
STATES = {'favorite', 'watch_later', 'watched', 'unwatched'}


def validate(conditions):
    extra = {'states', 'op', 'min_rating', 'sort'}
    result = validate_conditions({k: v for k, v in conditions.items() if k not in extra}, allow_empty=True)
    states = conditions.get('states', [])
    if not isinstance(states, list) or any(s not in STATES for s in states):
        raise ValueError('未知观看状态条件')
    op = conditions.get('op', 'AND')
    sort = conditions.get('sort', 'recent')
    if op not in ('AND', 'OR') or sort not in SORTS:
        raise ValueError('未知片单匹配方式或排序')
    minimum = conditions.get('min_rating')
    if minimum not in (None, ''):
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not 0 <= minimum <= 10:
            raise ValueError('最低评分必须在 0 到 10 之间')
    else:
        minimum = None
    return {**result, 'states': states, 'op': op, 'sort': sort, 'min_rating': minimum}


def accepts(item, conditions, preference, watches):
    minimum = conditions.get('min_rating')
    rating = preference.get('rating')
    if minimum is not None and (rating is None or rating < minimum):
        return False
    clauses = []
    for key in ('prefixes', 'publisher', 'producer', 'series', 'genres', 'casts',
                'earliest_release_date', 'max_coactors', 'exclude_vr'):
        if conditions.get(key):
            clauses.append(matches(item, {key: conditions[key], 'genre_mode': conditions.get('genre_mode', 'all')}))
    watched = any(w.get('played') for w in watches)
    flags = {'favorite': bool(preference.get('favorite')), 'watch_later': bool(preference.get('watch_later')),
             'watched': watched, 'unwatched': not watched}
    clauses.extend(flags[s] for s in conditions.get('states', []) if s in flags)
    return not clauses or (any(clauses) if conditions.get('op') == 'OR' else all(clauses))
