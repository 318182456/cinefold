from datetime import datetime, timedelta
from types import SimpleNamespace
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.core.config import Settings
from app.database.models import ResourcePost, CrawlerAttempt
from app.modules import resourcecrawler as parser
from app.services import resourcecrawler as jobs, resourcecatalog as catalog
from tests.test_scrape_reliability import db
from tests.test_cache_maintenance import postgres_catalog

A='a'*40
B='b'*40


def post(tid=1, title='ABC-001 中文字幕 4K 1.5 GB'):
    return dict(title=title,magnets=[f'magnet:?xt=urn:btih:{A if tid==1 else B}'],ed2k=[],previews=['https://images/1.jpg'],
                detail_url=f'https://forum/thread-{tid}.html',size_mb=1536,published='2026-10-27')


@pytest.fixture
def native(db,monkeypatch):
    db.configure(autoflush=False)
    settings=Settings(resource_sht_sections='37',resource_x1080_sections='5206',resource_crawl_pages=1,resource_crawl_limit=50)
    for module in (jobs,catalog):monkeypatch.setattr(module,'get_settings',lambda:settings)
    monkeypatch.setattr('app.core.config.get_settings',lambda:settings)
    monkeypatch.setattr(jobs,'get_source',lambda key:dict(enabled=True,host='https://forum'))
    monkeypatch.setattr(jobs,'_active',set())
    for stop in jobs._stops.values():stop.clear()
    yield settings,db
    for stop in jobs._stops.values():stop.clear()


def test_forum_parsers_and_identity_do_not_merge_ambiguous_codes():
    html=f'''<table id="threadlisttableid"><tbody id="normalthread_123"></tbody><tbody id="normalthread_123"></tbody></table>'''
    assert parser.parse_list('sehuatang',html)==[123]
    assert parser.parse_list('x1080x','<div id="content"><a href="forum.php?mod=viewthread&amp;tid=7">t</a></div>')==[7]
    with pytest.raises(ValueError):parser.parse_list('sehuatang','<title>Just a moment</title>')
    html=f'''<h1 class="ts"><a>[高清]</a><span id="thread_subject">[高清] ABC-001 中文字幕</span></h1>
    <em id="authorposton1"><span title="2026-10-27 12:00">today</span></em>
    <div id="postmessage_1"><div class="blockcode">magnet:?xt=urn:btih:{A}&amp;dn=test</div>1.5 GB<img file="/one.jpg" /></div>'''
    value=parser.parse_detail('sehuatang',html,'https://forum/thread-1.html')
    assert value['size_mb']==1536 and value['published']=='2026-10-27'
    assert value['previews']==['https://forum/one.jpg'] and '&dn=test' in value['magnets'][0]
    assert catalog.matched_code(value['title'])=='ABC-001'
    assert catalog.matched_code('ABC-001 ABC-002 合集')==''
    assert catalog.matched_code('ABC-001-UC')=='ABC-001'


def test_disabled_remote_duplicate_does_not_hide_local_result(native,monkeypatch):
    from app import services
    from app.schemas.torrent import Torrent
    native[0].mteam_enabled=False
    monkeypatch.setattr(services,'get_settings',lambda:native[0])
    monkeypatch.setattr(services.ptsite,'get_settings',lambda:native[0])
    monkeypatch.setattr(services,'_search_pt_cached',lambda *a,**k:[Torrent(site='MTeam',download_url='magnet:?xt=urn:btih:'+A)])
    catalog.put('sehuatang',1,'37',post())
    assert services.search_torrents('ABC-001',use_filter=False)[0].site=='BT'
    assert catalog.browse('ABC001')['total']==1


def test_x1080_archived_post_and_base32_magnets():
    import base64
    encoded=base64.b32encode(bytes.fromhex(A)).decode()
    html=f'<div id="nav">首页 › ABC-001</div><p class="author">发表于 2026-10-27</p><div id="content">[code]magnet:?xt=urn:btih:{encoded}[/code][img]https://images/1.jpg[/img]</div>'
    value=parser.parse_detail('x1080x',html,'https://forum/forum.php?tid=1')
    assert value['title']=='ABC-001' and value['magnets']==['magnet:?xt=urn:btih:'+A]
    assert value['previews']==['https://images/1.jpg']
    assert parser.magnets('magnet:?xt=urn:btih:'+A+'f')==[]


def test_pause_resume_commits_resource_and_checkpoint_once(native,monkeypatch):
    calls=[]
    fake=SimpleNamespace(listing=lambda section,page:[1,2],detail=lambda tid:(calls.append(tid) or post(tid)))
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:fake)
    data=jobs._begin('sehuatang',limit=1);jobs._run(data)
    assert data['phase']=='paused' and data['saved']==1 and calls==[1]
    persisted=jobs.state('sehuatang')
    assert persisted['pending']==[{'tid':2,'section':'37'}]
    # A reconstructed job resumes its persisted pending IDs, not the already-read page.
    resumed=jobs._begin('sehuatang',resume=True);jobs._run(resumed)
    assert calls==[1,2] and resumed['saved']==2
    last=jobs._begin('sehuatang',resume=True);jobs._run(last)
    assert last['phase']=='finished' and calls==[1,2]
    with native[1]() as session:assert session.scalar(select(func.count()).select_from(ResourcePost))==2


def test_failure_cooldown_and_retry_outside_first_pages(native,monkeypatch):
    calls=[]
    def detail(tid):calls.append(tid);raise RuntimeError('offline')
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=lambda section,page:[1],detail=detail))
    data=jobs._begin('sehuatang');jobs._run(data)
    assert data['phase']=='finished' and data['failed']==1
    again=jobs._begin('sehuatang');jobs._run(again)
    assert calls==[1] and again['skipped']==1
    with native[1].begin() as session:
        attempt=session.get(CrawlerAttempt,catalog.identity('sehuatang',1));attempt.retry_at=datetime.now()-timedelta(seconds=1)
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=lambda section,page:[],detail=lambda tid:(calls.append(tid) or post(tid))))
    retried=jobs._begin('sehuatang');jobs._run(retried)
    assert calls==[1,1] and retried['saved']==1


def test_listing_failure_is_failed_not_empty_success_and_resumes_page(native,monkeypatch):
    pages=[];details=[];failed=[False]
    def listing(section,page):
        pages.append(page)
        if page==2 and not failed[0]:failed[0]=True;raise RuntimeError('challenge')
        return [1] if page==1 else [2]
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=listing,detail=lambda tid:(details.append(tid) or post(tid))))
    data=jobs._begin('sehuatang',pages=2);jobs._run(data)
    assert data['phase']=='failed' and data['saved']==1
    resumed=jobs._begin('sehuatang',resume=True);jobs._run(resumed)
    assert resumed['phase']=='finished' and pages==[1,2,2] and details==[1,2]


def test_retry_backlog_leaves_budget_for_new_posts(native,monkeypatch):
    with native[1].begin() as session:
        session.add_all([CrawlerAttempt(key=catalog.identity('sehuatang',tid),source='sehuatang',tid=tid,section='37',
            retry_at=datetime.now()-timedelta(hours=1),error='offline') for tid in range(1,31)])
    calls=[]
    def detail(tid):
        calls.append(tid)
        if tid!=100:raise RuntimeError('offline')
        return post(tid)
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=lambda *a:[100],detail=detail))
    data=jobs._begin('sehuatang',limit=12);jobs._run(data)
    assert data['saved']==1 and len(calls)==4 and 100 in calls


def test_crash_before_commit_does_not_advance_checkpoint(native,monkeypatch):
    class Crash(BaseException):pass
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=lambda section,page:[1],detail=lambda tid:post(tid)))
    original=jobs._save
    def save(data,session=None):
        if session is not None and data['saved']==1:raise Crash()
        return original(data,session)
    monkeypatch.setattr(jobs,'_save',save)
    data=jobs._begin('sehuatang')
    with pytest.raises(Crash):jobs._run(data)
    persisted=jobs.state('sehuatang')
    assert persisted['phase']=='interrupted' and persisted['saved']==0 and len(persisted['pending'])==1
    with native[1]() as session:assert session.scalar(select(func.count()).select_from(ResourcePost))==0
    monkeypatch.setattr(jobs,'_save',original)
    resumed=jobs._begin('sehuatang',resume=True);jobs._run(resumed)
    assert resumed['phase']=='finished' and resumed['saved']==1


def test_live_catalog_search_ignores_remote_negative_cache_and_respects_bt_auto(native,monkeypatch):
    from app import services
    monkeypatch.setattr(services,'get_settings',lambda:native[0])
    monkeypatch.setattr(services,'_search_pt_cached',lambda *a,**k:[])
    catalog.put('sehuatang',1,'37',post())
    catalog.put('x1080x',2,'5206',post())  # identical magnet across sources
    from app.database.models import Code
    with native[1]() as session:
        stub=session.get(Code,'ABC-001')
        assert stub.status==0 and not stub.title, 'Forum titles are not official movie metadata'
    found=services.search_torrents('ABC-001-UC',use_filter=False)
    assert len(found)==1 and found[0].site=='BT' and found[0].chinese and found[0].uhd
    native[0].bt_auto_download=False
    assert services.find_torrent('ABC-001') is None
    native[0].resource_catalog_enabled=False
    assert services.search_torrents('ABC-001',use_filter=False)==[]


def test_removed_custom_bt_config_and_cache_cannot_supply_results(native,monkeypatch):
    from app import services
    settings=native[0]
    assert not {'bt_url','bt_method','bt_header','bt_json_data'} & settings.to_safe_dict().keys()
    # Even an old in-memory setting must not register an external provider.
    monkeypatch.setattr(settings,'bt_url','https://removed.invalid',raising=False)
    monkeypatch.setattr(services.ptsite,'get_settings',lambda:settings)
    assert all(site.name!='BT' for site in services.ptsite.get_sites())
    keys=[]
    def cached(namespace,key,**kwargs):
        keys.append(key)
        return json.dumps([{'site':'BT','title':'obsolete'}]) if '@2@' in key else None
    monkeypatch.setattr(services,'get_rank_cache',cached)
    monkeypatch.setattr(services.ptsite,'search_pt_detailed',lambda code:([],0,0))
    assert services._search_pt_cached('ABC-001')==[]
    assert keys and '@2@' not in keys[0]


def test_resource_magnet_download_uses_explicit_qb_and_records_history(native,monkeypatch):
    from app import services
    from app.database.models import History, Code, CodeStatus
    from app.api.endpoints import get_current_user
    from app.api.endpoints.subscribe import router
    catalog.put('sehuatang',1,'37',post())
    monkeypatch.setattr(services,'get_settings',lambda:native[0])
    monkeypatch.setattr(services,'is_filtered_code',lambda code:'')
    monkeypatch.setattr(services,'is_exist_server',lambda code:False)
    monkeypatch.setattr(services,'send_downloading_message',lambda *a:None)
    pushed=[]
    client=SimpleNamespace(add_torrent_by_magnet=lambda url,code:(pushed.append((url,code)) or A))
    names=[]
    monkeypatch.setattr(services.downloadclient,'get_download_client',lambda name='':(names.append(name) or client))
    monkeypatch.setattr(services.downloadclient,'list_configured_clients',lambda:['transmission','qbittorrent'])
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_current_user]=lambda:'test'
    body=dict(code='ABC-001',site='BT',title='Chosen resource',download_url='magnet:?xt=urn:btih:'+A,downloader='qbittorrent')
    with TestClient(app) as api:
        assert api.post('/codes/download',json=body).json()['code']==200
        assert names==['qbittorrent'] and pushed==[(body['download_url'],'ABC-001')]
        with native[1]() as db:
            assert db.get(History,A).site=='BT'
            assert db.get(Code,'ABC-001').status==CodeStatus.DOWNLOADING
        # A second click is blocked by existing download history until explicitly confirmed.
        assert api.post('/codes/download',json=body).json()['code']==500
        assert len(pushed)==1
        assert api.post('/codes/download',json={**body,'force':True}).json()['code']==200
        assert len(pushed)==2
        assert api.post('/codes/download',json={**body,'downloader':'unknown'}).status_code==422
        monkeypatch.setattr(services.downloadclient,'list_configured_clients',lambda:['transmission'])
        assert api.post('/codes/download',json={**body,'force':True}).json()['code']==500
        assert len(pushed)==2, 'An unavailable explicit client must not fall back to Transmission'


def test_preview_urls_stay_metadata_and_ambiguous_posts_are_not_auto_matched(native):
    catalog.put('sehuatang',1,'37',post(title='ABC-001 ABC-002 合集'))
    assert catalog.search('ABC-001')==[]
    page=catalog.browse('合集')
    assert page['total']==1 and page['items'][0]['previews']==['https://images/1.jpg']


def test_disabled_source_and_bad_options_cannot_start(native,monkeypatch):
    with pytest.raises(ValueError):jobs._begin('sehuatang',sections=['99999'])
    with pytest.raises(ValueError):jobs._begin('sehuatang',pages=0)
    monkeypatch.setattr(jobs,'get_source',lambda key:dict(enabled=False))
    with pytest.raises(ValueError):jobs._begin('sehuatang')


def test_api_start_duplicate_stop_and_legacy_settings_removed(native,monkeypatch):
    from app.api.endpoints import get_current_user
    from app.api.endpoints.resourcecrawler import router
    from app import scheduler
    from dataclasses import fields
    tasks=[]
    class Deferred:
        def __init__(self,*,target,args,**kwargs):tasks.append((target,args))
        def start(self):pass
    monkeypatch.setattr(jobs,'Thread',Deferred)
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=lambda *a:pytest.fail('Paused queued work must not request a page')))
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_current_user]=lambda:'test'
    with TestClient(app) as client:
        assert client.get('/resource-crawler').json()['data']['sources'][0]['job']['phase']=='idle'
        first=client.post('/resource-crawler',json={'source':'sehuatang','sections':['37'],'pages':1,'limit':5}).json()
        assert first['code']==200 and first['data']['phase']=='queued'
        assert client.post('/resource-crawler',json={'source':'sehuatang'}).json()['code']==400
        assert client.post('/resource-crawler/sehuatang/stop').json()['code']==200
        target,args=tasks[0];target(*args)
        assert client.get('/resource-crawler').json()['data']['sources'][0]['job']['phase']=='paused'
    assert 'import_crawler_db' not in scheduler.JOBS and 'import_crawler_db_full' not in scheduler.JOBS
    assert not any(field.name.startswith('crawler_db_') for field in fields(Settings))


def test_avbase_metadata_feed_reuses_native_parser_and_does_not_subscribe(native):
    from app.services.resourcefeeds import parse_avbase
    from app import services
    from app.database.models import Code,Actor
    work={'work_id':'ABC-001','actors':[{'name':'演员甲','image_url':'https://images/actor.jpg'}],
        'products':[{'title':'标题','image_url':'https://images/banner.jpg'}]}
    html='<script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'pageProps':{'works':[work,work]}}})+'</script>'
    movies,actors=parse_avbase(html)
    assert len(movies)==1 and movies[0]['casts']=='演员甲'
    services.cache_remote_codes(movies)
    for actor in actors:catalog.cache_actor(actor)
    with native[1]() as session:
        assert session.get(Code,'ABC-001').status==0
        assert session.get(Actor,'演员甲').subscribed is False


def test_library_host_has_single_cn_prefix(monkeypatch):
    from app.modules.ladysite.library import Library
    monkeypatch.setattr(parser.SiteClient,'from_source',lambda key:SimpleNamespace(host='https://library/cn'))
    assert Library().client.host=='https://library'


def test_metadata_source_failure_does_not_discard_or_block_other_sources(native,monkeypatch):
    from app.services import resourcefeeds as feeds
    from app.modules import ladysite
    from app.database.models import Code
    monkeypatch.setattr(feeds,'get_settings',lambda:native[0])
    monkeypatch.setattr(feeds,'get_source',lambda key:dict(enabled=key!='javlibrary'))
    monkeypatch.setattr(feeds.SiteClient,'from_source',lambda key:SimpleNamespace(get=lambda *a,**k:'<title>challenge</title>'))
    monkeypatch.setattr(ladysite,'get_rank_codes',lambda *a:['ABC-001'])
    with pytest.raises(RuntimeError,match='avbase'):feeds.run()
    with native[1]() as session:assert session.get(Code,'ABC-001').status==0


def test_forum_retains_clearance_cookie_useragent_and_exit_for_attachment(monkeypatch):
    from app.modules.ladysite.base import FetchedPage
    page=FetchedPage('<div id="threadlist"></div>','https://forum/',cookies=[{'name':'cf_clearance','value':'clear','domain':'forum'},
        {'name':'bad','value':'ignore','domain':'other'}],user_agent='browser-agent',proxy='http://node:17890')
    client=SimpleNamespace(get=lambda *a,**k:page,host='https://forum',cookie='session=kept')
    monkeypatch.setattr(parser.SiteClient,'from_source',lambda key:client)
    crawler=parser.ForumCrawler('sehuatang');crawler.get('/')
    assert 'cf_clearance=clear' in client.cookie and 'session=kept' in client.cookie and 'bad=' not in client.cookie
    assert crawler.user_agent=='browser-agent' and crawler.attachment_proxy=='http://node:17890'
    with pytest.raises(ValueError):crawler.binary('https://other/download','https://forum/')


def test_http200_challenge_is_retried_through_existing_bypass(monkeypatch):
    calls=[]
    responses=iter(['<title>Just a moment...</title>','<table id="threadlisttableid"><tbody id="normalthread_7"></tbody></table>'])
    def get(*args,**kwargs):calls.append(kwargs);return next(responses)
    client=SimpleNamespace(get=get,host='https://forum',cookie='',bypass_first=False)
    monkeypatch.setattr(parser.SiteClient,'from_source',lambda key:client)
    assert parser.ForumCrawler('sehuatang').listing('37',1)==[7]
    assert len(calls)==2 and calls[1]['bypass_first'] is True and client.bypass_first


def test_unrecognized_list_retries_once_with_the_same_deadline(monkeypatch):
    from app.modules.ladysite.transport import current_budget
    pages=iter(['<title>Temporary verification</title>','<table id="threadlisttableid"><tbody id="normalthread_9"></tbody></table>'])
    calls=[]
    def get(*args,**kwargs):calls.append((current_budget(),kwargs));return next(pages)
    client=SimpleNamespace(get=get,host='https://forum',cookie='',proxy=None,bypass_first=False)
    monkeypatch.setattr(parser.SiteClient,'from_source',lambda key:client)
    assert parser.ForumCrawler('sehuatang').listing('37',1)==[9]
    assert len(calls)==2 and calls[0][0] is calls[1][0] and client.bypass_first


def test_x1080_follows_official_release_then_reports_maintenance_without_retry(monkeypatch):
    pages=iter(['<title>歡迎訪問 · x1080x</title><body>請使用下面地址之一訪問論壇<a href="01.html">地址一</a></body>',
                '<meta http-equiv="refresh" content="0; url=https://new-forum.test">',
                '<title>網站維護中 - x1080x</title><body>論壇目前仍在改版中，維護期間暫停開放。</body>'])
    calls=[]
    def get(path,**kwargs):calls.append((path,client.host,client.cookie));return next(pages)
    client=SimpleNamespace(get=get,host='https://release.test',cookie='private=do-not-forward',proxy=None,bypass_first=True)
    monkeypatch.setattr(parser.SiteClient,'from_source',lambda key:client)
    with pytest.raises(parser.ForumPageError,match='论坛正在维护') as error:
        parser.ForumCrawler('x1080x').listing('5206',1)
    assert not error.value.retryable and len(calls)==3
    assert calls[1][0]=='https://release.test/01.html'
    assert calls[2][1]=='https://new-forum.test' and calls[2][2]==''


def test_attachment_login_page_is_not_treated_as_missing_links(monkeypatch):
    client=SimpleNamespace(host='https://forum',cookie='')
    monkeypatch.setattr(parser.SiteClient,'from_source',lambda key:client)
    crawler=parser.ForumCrawler('sehuatang')
    crawler.get=lambda path:'<span id="thread_subject">ABC-001</span><div id="postmessage_1"><a href="/attachment">x.torrent</a></div>'
    crawler.binary=lambda *a:b'<html><title>login</title></html>'
    with pytest.raises(RuntimeError,match='附件返回登录或验证页'):crawler.detail(1)


def test_torrent_attachment_uses_page_clearance_and_real_infohash(monkeypatch):
    from contextlib import contextmanager
    import hashlib,httpx
    from app.modules.ladysite.base import SiteClient,FetchedPage
    info=b'd6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:'+b'a'*20+b'e'
    encoded=b'd4:info'+info+b'e'
    html='<span id="thread_subject">ABC-001</span><div id="postmessage_1">1 GB</div><div class="attnm"><a href="/attachment?id=1">test.torrent</a></div>'
    page=FetchedPage(html,'https://forum/thread-1.html',cookies=[{'name':'cf_clearance','value':'clear','domain':'forum'}],
                     user_agent='browser-agent',proxy='http://exit:17890')
    client=SiteClient('https://forum');client.get=lambda *a,**k:page;client._throttle=lambda:None
    monkeypatch.setattr(SiteClient,'from_source',lambda key:client)
    captured={}
    class HTTP:
        def __init__(self,**kw):captured.update(kw)
        def __enter__(self):return self
        def __exit__(self,*a):pass
        @contextmanager
        def stream(self,method,url,headers):
            captured['headers']=headers
            yield httpx.Response(200,content=encoded,request=httpx.Request(method,url))
    monkeypatch.setattr(parser.httpx,'Client',HTTP)
    row=parser.ForumCrawler('sehuatang').detail(1)
    assert row['magnets']==['magnet:?xt=urn:btih:'+hashlib.sha1(info).hexdigest()]
    assert captured['proxy']=='http://exit:17890' and captured['headers']['User-Agent']=='browser-agent'
    assert 'cf_clearance=clear' in captured['headers']['Cookie']


def test_postgres_resource_job_and_actor_upsert(postgres_catalog,monkeypatch):
    from app.database.models import Actor
    _,factory=postgres_catalog
    settings=Settings(resource_sht_sections='37')
    for module in (jobs,catalog):monkeypatch.setattr(module,'get_settings',lambda:settings)
    monkeypatch.setattr(jobs,'get_source',lambda key:dict(enabled=True))
    monkeypatch.setattr(jobs,'_active',set())
    for stop in jobs._stops.values():stop.clear()
    monkeypatch.setattr(jobs,'ForumCrawler',lambda source:SimpleNamespace(listing=lambda *a:[1,2],detail=lambda tid:post(tid)))
    data=jobs._begin('sehuatang',sections=['37'],pages=1,limit=1);jobs._run(data)
    assert jobs.state('sehuatang')['saved']==1 and data['phase']=='paused'
    resumed=jobs._begin('sehuatang',resume=True);jobs._run(resumed)
    assert jobs.state('sehuatang')['saved']==2
    assert len(catalog.search('ABC-001'))==2 and catalog.browse()['total']==2
    with factory.begin() as session:session.add(Actor(name='演员甲',subscribed=True,limit_date='2024-01-01',photo='kept.jpg'))
    catalog.cache_actor({'name':'演员甲','photo':'new.jpg','cn_name':'别名'})
    with factory() as session:
        row=session.get(Actor,'演员甲');assert row.subscribed and row.photo=='kept.jpg' and row.name_2=='别名'


def test_postgres_resource_transaction_rolls_back_checkpoint(postgres_catalog,monkeypatch):
    _,factory=postgres_catalog
    settings=Settings(resource_sht_sections='37')
    for module in (jobs,catalog):monkeypatch.setattr(module,'get_settings',lambda:settings)
    monkeypatch.setattr(jobs,'get_source',lambda key:dict(enabled=True))
    monkeypatch.setattr(jobs,'_active',set())
    for stop in jobs._stops.values():stop.clear()
    test_crash_before_commit_does_not_advance_checkpoint((settings,factory),monkeypatch)
