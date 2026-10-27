import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.models import Actor, Code, CodeStatus
from app.modules.ladysite.avbase import Avbase
from app.modules.ladysite.ranks import RANKS
from app.services import actorprofile
from tests.test_resource_crawler import native
from tests.test_scrape_reliability import db
from tests.test_cache_maintenance import postgres_catalog


def actor_html():
    work={'work_id':'ABC-001','actors':[{'name':'花井美理'}], 'products':[{'title':'Native title','image_url':'https://images.test/photo.jpg'}]}
    payload={'props':{'pageProps':{'works':[work], 'max_page':1,'talent':{'primary':{'name':'花井美理','image_url':'https://images.test/actor.jpg'},'actors':[{'name':'旧名甲'}]}}}}
    return '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(payload)+'</script>'


def test_avbase_date_actor_alias_and_single_filter(monkeypatch):
    calls=[]
    provider=Avbase.__new__(Avbase)
    provider.client=SimpleNamespace(host='https://www.avbase.net',get=lambda path,**kw:(calls.append((path,kw)) or actor_html()))
    movies,actors=provider.feed('2026-10-27',2)
    assert calls[-1][0]=='/works/date/2026-10-27' and calls[-1][1]['params']['page']==2
    assert movies[0]['code']=='ABC-001' and actors[0]['name']=='花井美理'
    with pytest.raises(ValueError):provider.feed('not-a-date')
    assert len(calls)==1
    result=provider.actor_works('旧名甲',1,True)
    assert result['aliases']==['旧名甲'] and len(result['items'])==1 and not result['has_more']
    assert calls[-1][1]['params']['cast_size']=='single'
    with pytest.raises(ValueError,match='匹配'):provider.actor_works('其他演员')


def _actor_import(factory):
    with factory.begin() as session:
        session.add_all([Actor(name='花井美理',subscribed=True,photo='manual.jpg'),Code(code='ABC-001',title='Manual title',status=CodeStatus.COMPLETED)])
    from app.modules.ladysite.avbase import html_to_works
    movies,_=html_to_works(actor_html())
    actorprofile._import('花井美理',dict(name='花井美理',photo='remote.jpg',aliases=['旧名甲'],items=[*movies,{'code':'ABC-002','title':'New movie','casts':'花井美理'}],source_url='https://www.avbase.net/talents/name'))
    with factory() as session:
        assert session.get(Actor,'花井美理').subscribed and session.get(Actor,'花井美理').photo=='manual.jpg'
        assert session.get(Code,'ABC-001').title=='Manual title' and session.get(Code,'ABC-001').status==CodeStatus.COMPLETED
        assert session.get(Code,'ABC-002').status==CodeStatus.NONE


def test_actor_import_preserves_manual_fields(native):_actor_import(native[1])


def test_postgres_actor_import_preserves_manual_fields(postgres_catalog):_actor_import(postgres_catalog[1])


def test_actor_sync_selects_avbase_without_subscribing(native,monkeypatch):
    from app.modules.ladysite import sources
    calls=[]
    monkeypatch.setattr(actorprofile,'_JOBS',{})
    monkeypatch.setattr(actorprofile,'_CAPACITY',threading.BoundedSemaphore(4))
    monkeypatch.setattr(actorprofile,'_POOL',SimpleNamespace(submit=lambda fn:fn()))
    monkeypatch.setattr(sources,'get_source',lambda key:{'enabled':True})
    monkeypatch.setattr(Avbase,'actor_works',lambda self,name,page,single:(calls.append((name,page,single)) or {'name':name,'items':[{'code':'ABC-003','title':'New','casts':name}],'has_more':False}))
    result=actorprofile.refresh('花井美理',3,source='avbase',single=True)
    assert not result['running'] and not result['error'] and result['remote_pages']==1
    assert calls==[('花井美理',1,True)]
    with native[1]() as session:assert not session.get(Actor,'花井美理').subscribed


def test_all_rank_categories_route_to_their_source(monkeypatch):
    from app.modules import ladysite
    from app.modules.ladysite.javdb import Avdb
    from app.modules.ladysite.library import Library
    monkeypatch.setattr('app.modules.ladysite.sources.get_source',lambda key:{'enabled':True})
    calls=[]
    monkeypatch.setattr(Avdb,'__init__',lambda self:None)
    monkeypatch.setattr(Avdb,'crawling_top',lambda self,period,pages,section='censored':(calls.append(('javdb',period,section)) or ['ABC-001']))
    monkeypatch.setattr(Library,'__init__',lambda self:None)
    monkeypatch.setattr(Library,'crawling_rank',lambda self,kind,page:(calls.append(('library',kind)) or ['ABC-002']))
    assert len(RANKS)==11
    for key,info in RANKS.items():
        assert ladysite.get_rank_codes(key)
        assert calls[-1][0]==('library' if info['source']=='javlibrary' else 'javdb')
        if info['source']=='javdb':assert calls[-1][1:]==(info['period'],info['section'])


def test_optional_javdb_api_uses_signed_direct_request_and_exact_identity(monkeypatch):
    from app.modules.ladysite import javdbapi
    monkeypatch.setattr(javdbapi,'get_source',lambda key:{'enabled':True,'options':{'api_enabled':True,'api_host':'https://api.test'}})
    calls=[]
    def get(self,path,**kwargs):
        calls.append((path,kwargs))
        if 'rankings' in path:return json.dumps({'data':{'movies':[{'number':'FC2-1234567','title':'Rank'}]}})
        if 'search' in path:return json.dumps({'data':{'movies':[{'id':2,'number':'ABC-002'}]}})
        return '{}'
    monkeypatch.setattr(javdbapi.SiteClient,'get',get)
    api=javdbapi.JavdbAPI()
    assert api.rank('weekly','fc2')[0].code.startswith('FC2')
    assert calls[-1][1]['params']=={'type':3,'period':'weekly'}
    assert calls[-1][1]['allow_bypass'] is False and 'jdsignature' in calls[-1][1]['headers']
    assert api.detail('ABC-001') is None
    assert not any('movies/' in path for path,_ in calls)


def test_rank_options_api_and_disabled_tasks_are_visible(native,monkeypatch):
    from app.api.endpoints import get_current_user
    from app.api.endpoints.subscribe import router
    from app import scheduler
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_current_user]=lambda:'test'
    with TestClient(app) as client:
        assert len(client.get('/rank/options').json()['data']['items'])==11
        assert client.get('/rank?rank_type=invalid').status_code==422
    monkeypatch.setattr(scheduler,'get_settings',lambda:native[0])
    monkeypatch.setattr(scheduler,'_scheduler',SimpleNamespace(get_jobs=lambda:[]))
    rows={row['id']:row for row in scheduler.list_jobs()}
    assert rows['crawl_resources']['schedule']==''
    assert rows['crawl_resources']['editable']
    assert not rows['crawl_avbase_date']['editable']
