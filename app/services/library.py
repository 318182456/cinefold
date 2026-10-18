"""本地收藏目录。播放留给 Emby，本站仅维护资料和收藏。"""
from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import quote, urlsplit

from sqlalchemy import exists, or_, select

from app.core.config import get_settings
from app.database.models import Code, CodeStatus, Collection, CollectionItem, EmbyCollectionLink, EmbyWatch, LibraryPreference, MediaLink
from app.database.session import session_scope
from app.services.rules import matches, terms
from app.services.playlists import SYSTEM_PLAYLISTS, accepts


def library_items(*, keyword: str = "", conditions: dict | None = None, collection_id: int | None = None,
                  emby_collection_key: str = '',
                  system_collection: str = '',
                  favorite: bool = False, emby_favorite: bool = False, watch_later: bool = False, unseen: bool = False,
                  watched: bool = False, last_played_month: str = '', release_year: int | None = None,
                  sort: str = "recent", page: int = 1, size: int = 30, include_skipped: bool = False) -> dict:
    conditions = conditions or {}
    collection_conditions = {}
    system = next((item for item in SYSTEM_PLAYLISTS if item['key'] == system_collection), None)
    if system_collection and not system:
        raise ValueError('系统片单不存在')
    with session_scope() as session:
        emby_codes = None
        if emby_collection_key:
            from app.services.embycollections import _context, _core
            context, _ = _context()
            collection = session.get(EmbyCollectionLink,emby_collection_key)
            if not collection or not collection.remote_id or collection.server!=context['server'] or collection.user_id!=context['user_id']:
                raise ValueError('合集不存在或不属于当前 Emby 用户')
            members = {str(item) for item in collection.member_ids or []}
            emby_codes = {_core(row.code) for row in session.scalars(select(EmbyWatch).where(
                EmbyWatch.server==context['server'],EmbyWatch.user_id==context['user_id'])) if row.item_id in members}
        query = select(Code).where(or_(
            Code.status == CodeStatus.COMPLETED,
            exists(select(MediaLink.link_path).where(MediaLink.code == Code.code)),
            exists(select(EmbyWatch.item_id).where(EmbyWatch.code == Code.code)),
        ))
        if keyword:
            pattern = f"%{keyword}%"
            query = query.where(or_(*(c.ilike(pattern) for c in (Code.code, Code.title, Code.cn_title, Code.casts, Code.genres))))
        if collection_id:
            collection = session.get(Collection, collection_id)
            if not collection:
                raise ValueError("片单不存在")
            if collection.kind == "smart":
                collection_conditions = collection.conditions
            else:
                query = query.where(exists(select(CollectionItem.code).where(CollectionItem.code == Code.code, CollectionItem.collection_id == collection_id)))
        prefs = {p.code: p.to_dict() for p in session.scalars(select(LibraryPreference))}
        watches = defaultdict(list)
        for w in session.scalars(select(EmbyWatch)):
            watches[w.code].append(w.to_dict())
        links = defaultdict(list)
        for link in session.scalars(select(MediaLink)):
            links[link.code].append(link.to_dict())
        result = []
        for row in session.scalars(query).yield_per(500):
            if emby_codes is not None and _core(row.code) not in emby_codes:
                continue
            item = row.to_dict()
            if not matches(item, conditions):
                continue
            pref = prefs.get(row.code, {})
            watch = watches.get(row.code, [])
            if not accepts(item, collection_conditions or {}, pref, watch):
                continue
            if emby_favorite and not any(w.get("favorite") for w in watch):
                continue
            if favorite and not pref.get("favorite") or watch_later and not pref.get("watch_later"):
                continue
            if pref.get("skipped") and not include_skipped:
                continue
            if unseen and any(w["played"] for w in watch):
                continue
            if watched and not any(w['played'] for w in watch):
                continue
            if last_played_month and not any((w.get('last_played') or '').startswith(last_played_month) for w in watch):
                continue
            if release_year and not (row.release_date or '').startswith(str(release_year)):
                continue
            item.update(preference=pref, watches=watch, files=links.get(row.code, []))
            item["emby_url"] = emby_link(watch[0]["item_id"]) if watch else ""
            result.append(item)
    keys = {
        "recent": lambda x: x.get("update_time") or "",
        "release": lambda x: x.get("release_date") or "",
        "rating": lambda x: x["preference"].get("rating") or 0,
        "plays": lambda x: sum(w["play_count"] for w in x["watches"]),
        "size": lambda x: sum(f.get("size") or 0 for f in x["files"]),
        "casts": lambda x: len(set(terms(x.get('casts')))),
        "added": lambda x: x.get('create_time') or '',
    }
    sort = system['sort'] if system else (collection_conditions or {}).get('sort', sort)
    result.sort(key=keys.get(sort, keys["recent"]), reverse=True)
    total = len(result)
    start = (page - 1) * size
    return {"items": result[start:start + size], "total": total, "page": page, "size": size}


def emby_link(item_id: str) -> str:
    url = get_settings().emby_url.rstrip("/")
    return f"{url}/web/index.html#!/item?id={quote(item_id, safe='')}" if urlsplit(url).scheme in ("http", "https") else ""


def library_stats(year: int | None = None) -> dict:
    """观看次数是 Emby 累计值；按年仅统计最后观看日期，不伪造历史事件。"""
    actors, genres, publishers, releases = Counter(), Counter(), Counter(), Counter()
    watched, plays, last_watched = set(), 0, Counter()
    with session_scope() as session:
        for w in session.scalars(select(EmbyWatch)):
            if w.played:
                watched.add(w.code)
            plays += w.play_count
            if w.last_played and (year is None or w.last_played.startswith(str(year))):
                last_watched[w.last_played[:7]] += 1
        query = select(Code).where(or_(Code.status == CodeStatus.COMPLETED, exists(select(MediaLink.code).where(MediaLink.code == Code.code)), exists(select(EmbyWatch.code).where(EmbyWatch.code == Code.code))))
        total = 0
        for row in session.scalars(query).yield_per(500):
            total += 1
            actors.update(terms(row.casts))
            genres.update(terms(row.genres))
            if row.publisher:
                publishers[row.publisher] += 1
            if row.release_date:
                releases[row.release_date[:4]] += 1
        preferences = session.scalars(select(LibraryPreference)).all()
    def ranked(counter):
        return [{"name": k, "count": v} for k, v in counter.most_common(20)]
    return {"total": total, "watched": len(watched), "plays": plays,
            "favorites": sum(p.favorite for p in preferences), "actors": ranked(actors),
            "genres": ranked(genres), "publishers": ranked(publishers), "release_years": ranked(releases),
            "last_watched_months": dict(sorted(last_watched.items())),
            "watch_note": "播放次数来自 Emby 累计值；月份按每个条目最后观看时间分布，不代表当月实际播放次数。"}


def similar(code: str, limit: int = 12) -> list[dict]:
    with session_scope() as session:
        row = session.get(Code, code)
        if not row:
            raise ValueError("番号不存在")
        casts, genres = set(terms(row.casts)), set(terms(row.genres))
        scored = []
        for other in session.scalars(select(Code).where(Code.code != code)).yield_per(500):
            score = 3 * len(casts & set(terms(other.casts))) + len(genres & set(terms(other.genres)))
            score += 2 * bool(row.series and row.series == other.series)
            if score:
                scored.append((score, other.code, other.to_dict()))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [x[2] for x in scored[:limit]]
