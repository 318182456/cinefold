"""Persistent, sequential forum crawling with bounded work and retry cooldowns."""
import copy
import json
import re
import threading
import time
import uuid
from threading import Thread
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core.config import get_settings
from app.database.models import Cache, CrawlerAttempt, ResourcePost
from app.database.session import session_scope
from app.modules.resourcecrawler import ForumCrawler, SOURCES
from app.modules.ladysite.sources import get_source
from app.services import resourcecatalog

NS = 'resource-crawler'
_lock = threading.RLock()
_serial = threading.Lock()
_active = set()
_stops = {source:threading.Event() for source in SOURCES}


def _error(exc):
    message=re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>"\']+','[地址已隐藏]',str(exc))
    message=re.sub(r'(?i)(password|token|api[_-]?key|passkey)\s*[=:]\s*[^\s&,;]+',r'\1=[已隐藏]',message)
    return f'{type(exc).__name__}: {message}'[:128]


def _save(data, session=None):
    data['updated_at'] = time.time()
    def write(db):
        stmt=(pg_insert if db.bind.dialect.name=='postgresql' else sqlite_insert)(Cache)
        content=json.dumps(data,ensure_ascii=False)
        db.execute(stmt.values(namespace=NS,key=data['source'],content=content).on_conflict_do_update(
            index_elements=['namespace','key'],set_={'content':content}))
    if session is not None:write(session)
    else:
        with session_scope() as db:write(db)


def state(source):
    if source not in SOURCES:raise ValueError('未知爬虫')
    with _lock, session_scope() as db:
        content=db.scalar(select(Cache.content).where(Cache.namespace==NS,Cache.key==source))
        try:
            data=json.loads(content) if content else dict(source=source,phase='idle',running=False)
            if not isinstance(data,dict):raise ValueError('Invalid checkpoint')
        except (ValueError,TypeError):
            return dict(source=source,phase='failed',running=False,error='任务记录损坏，请重新抓取')
        if data.get('running') and source not in _active:
            data.update(running=False,phase='interrupted',message='后台任务已中断，可继续未完成的帖子')
        return data


def sources():
    settings=get_settings()
    return [{**spec,'key':key,'enabled':bool((get_source(key) or {}).get('enabled')), 'job':state(key),
        'defaults':dict(pages=settings.resource_crawl_pages,limit=settings.resource_crawl_limit,
            sections=getattr(settings,'resource_sht_sections' if key=='sehuatang' else 'resource_x1080_sections').split(','))}
        for key,spec in SOURCES.items()]


def _options(source, sections=None, pages=None, limit=None):
    settings=get_settings()
    default=getattr(settings,'resource_sht_sections' if source=='sehuatang' else 'resource_x1080_sections')
    sections=list(dict.fromkeys(str(v).strip() for v in (sections if sections is not None else default.split(',')) if str(v).strip()))
    if not sections or any(value not in SOURCES[source]['sections'] for value in sections):raise ValueError('请选择有效的论坛板块')
    pages=int(pages if pages is not None else settings.resource_crawl_pages)
    limit=int(limit if limit is not None else settings.resource_crawl_limit)
    if not 1<=pages<=100 or not 1<=limit<=1000:raise ValueError('每板块页数需为 1–100，每轮帖子上限需为 1–1000')
    return dict(sections=sections,pages=pages,limit=limit)


def _begin(source, *, resume=False, sections=None, pages=None, limit=None):
    if source not in SOURCES:raise ValueError('未知爬虫')
    if not (get_source(source) or {}).get('enabled'):raise ValueError('请先在数据源中启用该资源爬虫')
    with _lock:
        if source in _active:raise ValueError('该爬虫已在排队或执行中')
        previous=state(source)
        if resume:
            if previous.get('phase') not in ('paused','interrupted','failed') or not previous.get('options'):
                raise ValueError('没有可继续的任务')
            data=previous
        else:
            data=dict(source=source,run_id=uuid.uuid4().hex,options=_options(source,sections,pages,limit),
                section_index=0,page=1,pending=[],retries_loaded=False,done=0,saved=0,failed=0,skipped=0,errors=[],started_at=time.time())
            history=previous.get('history',[])
            if previous.get('run_id'):
                history=history+[{key:previous.get(key) for key in ('run_id','phase','done','saved','failed','started_at','finished_at','error')}]
            data['history']=history[-5:]
        data.update(phase='queued',running=True,message='等待后台资源',error='')
        _save(data)
        _active.add(source);_stops[source].clear()
        return data


def start(source, **options):
    data=_begin(source,**options)
    snapshot=copy.deepcopy(data)
    try:Thread(target=_run,args=(data,),daemon=True,name='crawler-'+source).start()
    except Exception:
        with _lock:_active.discard(source)
        data.update(running=False,phase='failed',error='无法启动后台线程');_save(data)
        raise
    return snapshot


def stop(source):
    if source not in SOURCES:raise ValueError('未知爬虫')
    _stops[source].set()
    return state(source)


def _defer(db, source, item, error, hours=0):
    stmt=(pg_insert if db.bind.dialect.name=='postgresql' else sqlite_insert)(CrawlerAttempt)
    values=dict(key=resourcecatalog.identity(source,item['tid']),source=source,tid=item['tid'],section=item['section'],
        retry_at=datetime.now()+timedelta(hours=hours,minutes=0 if hours else 30),error=error[:128])
    db.execute(stmt.values(**values).on_conflict_do_update(index_elements=['key'],set_={k:v for k,v in values.items() if k!='key'}))


def _step(data, crawler, item):
    source=data['source'];key=resourcecatalog.identity(source,item['tid'])
    with session_scope() as db:
        exists=db.get(ResourcePost,key)
        attempt=db.get(CrawlerAttempt,key)
        skip=exists is not None or bool(attempt and attempt.retry_at>datetime.now())
    if skip:
        data['skipped']+=1;data['pending'].pop(0);_save(data);return False
    data.update(current=str(item['tid']),message=f"读取板块 {item['section']} 的帖子 {item['tid']}")
    _save(data)
    try:
        row=crawler.detail(item['tid'])
        if not row or not (row.get('magnets') or row.get('ed2k')):raise LookupError('帖子暂无可用资源链接')
        next_data=copy.deepcopy(data)
        with session_scope() as db:
            resourcecatalog.put(source,item['tid'],item['section'],row,session=db)
            db.execute(delete(CrawlerAttempt).where(CrawlerAttempt.key==key))
            next_data['done']+=1;next_data['saved']+=1;next_data['pending'].pop(0)
            _save(next_data,session=db)
        data.update(next_data)  # Advance in memory only after the transaction committed.
    except Exception as exc:
        # Transaction failures must leave pending unchanged; only a committed failure advances it.
        next_data=copy.deepcopy(data)
        with session_scope() as db:
            _defer(db,source,item,_error(exc),hours=24 if isinstance(exc,LookupError) else 0)
            next_data['done']+=1;next_data['failed']+=1;next_data['pending'].pop(0)
            next_data['errors']=(data['errors']+[f"{item['tid']}: {_error(exc)}"])[-20:]
            _save(next_data,session=db)
        data.update(next_data)
    return True


def _work(data):
    source=data['source'];options=data['options'];crawler=ForumCrawler(source)
    work=0
    if not data.get('retries_loaded'):
        with session_scope() as db:
            data['pending']=[dict(tid=row.tid,section=row.section,retry=True) for row in db.scalars(
                select(CrawlerAttempt).where(CrawlerAttempt.source==source,CrawlerAttempt.retry_at<=datetime.now())
                .order_by(CrawlerAttempt.retry_at,CrawlerAttempt.key).limit(min(10,max(1,options['limit']//4))))]
        data['retries_loaded']=True;_save(data)
    while True:
        if not (get_source(source) or {}).get('enabled'):
            data.update(phase='paused',message='数据源已停用，抓取暂停');return
        if _stops[source].is_set() or work>=options['limit']:
            data.update(phase='paused',message='本轮已暂停，可继续剩余帖子');return
        if data['pending']:
            work+=int(_step(data,crawler,data['pending'][0]));continue
        if data.get('page_loaded'):
            data['page']+=1;data['page_loaded']=False
        if data['page']>options['pages']:
            data['section_index']+=1;data['page']=1
        if data['section_index']>=len(options['sections']):
            data.update(phase='finished',message='抓取完成；失败帖子已安排冷却后补抓');return
        section=options['sections'][data['section_index']]
        data.update(message=f"读取板块 {section} 第 {data['page']} 页",current='');_save(data)
        ids=crawler.listing(section,data['page'])[:200]
        data['pending']=[dict(tid=int(tid),section=section) for tid in ids]
        data['page_loaded']=True
        if not ids:data['page']=options['pages']
        _save(data)


def _run(data):
    from app.services.resourcebudget import slot
    try:
        with _serial,slot():
            data.update(phase='running',message='开始抓取');_save(data)
            _work(data)
        data.update(running=False,finished_at=time.time());_save(data)
    except Exception as exc:
        logger.exception(f"资源爬虫 {data['source']} 执行失败")
        data.update(running=False,phase='failed',error=_error(exc),message='抓取中断，可继续原任务');_save(data)
    finally:
        with _lock:_active.discard(data['source'])


def run_scheduled():
    from app.services.taskruns import progress
    count=0;failures=[]
    for source in SOURCES:
        if not (get_source(source) or {}).get('enabled'):continue
        previous=state(source)
        try:data=_begin(source,resume=previous.get('phase') in ('paused','interrupted'))
        except ValueError:continue
        progress('crawl_resources','正在抓取 '+SOURCES[source]['name'],current=source,processed=count)
        _run(data);count+=data.get('saved',0)-previous.get('saved',0) if data.get('run_id')==previous.get('run_id') else data.get('saved',0)
        if data.get('phase')=='failed':failures.append(source)
    if failures:raise RuntimeError('资源爬虫执行失败: '+','.join(failures))
    progress('crawl_resources','本轮论坛抓取完成',processed=count)
    return count
