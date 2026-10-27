"""Bounded catalog writes and immediate search, independent of remote search TTL."""
import hashlib
from sqlalchemy import select, func, case, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core.config import get_settings
from app.database.models import Actor, Code, ResourcePost
from app.database.session import session_scope
from app.modules.resourcecrawler import SOURCES, magnets
from app.modules.subtitle.local import _strip_version
from app.modules.ptsite import convert_torrent
from app.utils import find_serial_numbers, get_true_code, get_magnet_hash


def identity(source, tid):
    return hashlib.sha256(f'{source}:{int(tid)}'.encode()).hexdigest()


def matched_code(title):
    # Only a single explicit identity may enter exact-code automatic searches.
    codes = list(dict.fromkeys(_strip_version(code) for code in find_serial_numbers(title or '')))
    return codes[0] if len(codes)==1 else ''


def put(source, tid, section, row, *, session=None):
    code = matched_code(row.get('title',''))
    fields = dict(key=identity(source,tid),source=source,tid=int(tid),section=str(section), code=code,
        title=str(row.get('title') or '')[:5000], category=str(row.get('category') or '')[:128],
        detail_url=row.get('detail_url') or '', published=row.get('published') or '',
        size_mb=max(0,float(row.get('size_mb') or 0)), magnets=magnets(' '.join(row.get('magnets') or []))[:100],
        ed2k=list(dict.fromkeys(row.get('ed2k') or []))[:100], previews=list(row.get('previews') or [])[:40])
    def write(db):
        stmt = (pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert)(ResourcePost).values(**fields)
        db.execute(stmt.on_conflict_do_update(index_elements=['key'],set_={k:getattr(stmt.excluded,k) for k in fields if k!='key'}))
        if code:
            stub=(pg_insert if db.bind.dialect.name=='postgresql' else sqlite_insert)(Code)
            db.execute(stub.values(code=code).on_conflict_do_nothing(index_elements=['code']))
    if session is not None:
        write(session)
    else:
        with session_scope() as db:write(db)
    return fields['key']


def search(code):
    if not get_settings().resource_catalog_enabled:
        return []
    code = _strip_version(get_true_code(code))
    if not code:
        return []
    with session_scope() as db:
        rows = db.scalars(select(ResourcePost).where(ResourcePost.code==code).order_by(ResourcePost.create_time.desc()).limit(100)).all()
        results, seen = [], set()
        for row in rows:
            for url in row.magnets:
                key = get_magnet_hash(url)
                if not key or key in seen:continue
                seen.add(key)
                item = convert_torrent(dict(title=row.title,size_mb=row.size_mb,download_url=url,detail_url=row.detail_url), 'BT', code)
                item.source_site = '资源库 · '+SOURCES.get(row.source,{}).get('name',row.source)
                results.append(item)
                if len(results)>=200:return results
        return results


def browse(keyword='', source='', page=1, size=30):
    page,size=max(1,page),max(1,min(100,size))
    query=select(ResourcePost)
    if source:query=query.where(ResourcePost.source==source)
    if keyword:
        match=ResourcePost.title.ilike('%'+keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%',escape='\\')
        code=get_true_code(keyword)
        query=query.where(or_(match,ResourcePost.code==_strip_version(code)) if code else match)
    with session_scope() as db:
        total=db.scalar(select(func.count()).select_from(query.subquery()))
        rows=db.scalars(query.order_by(ResourcePost.create_time.desc(),ResourcePost.key).offset((page-1)*size).limit(size)).all()
        return dict(total=total,page=page,size=size,items=[row.to_dict() for row in rows])


def cache_actor(item):
    """Crawler metadata enriches actors without turning them into subscriptions."""
    name=(item.get('name') or '').strip()
    from app.utils.actornames import valid
    if not valid(name):return 0
    with session_scope() as db:
        stmt=(pg_insert if db.bind.dialect.name=='postgresql' else sqlite_insert)(Actor).values(
            name=name,subscribed=False,photo=item.get('photo') or item.get('avatar') or '',
            name_2=item.get('name_2') or item.get('cn_name') or '')
        db.execute(stmt.on_conflict_do_update(index_elements=['name'],set_={
            'photo':case((or_(Actor.photo.is_(None),Actor.photo==''),stmt.excluded.photo),else_=Actor.photo),
            'name_2':case((or_(Actor.name_2.is_(None),Actor.name_2==''),stmt.excluded.name_2),else_=Actor.name_2)}))
    return 1
