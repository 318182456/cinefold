import hashlib
import time

import pytest

from app.database.models import Code, CodeStatus, EmbyWatch, MediaLink
from app.services import embycollections as collections
from tests.test_emby_collections import case
from tests.test_scrape_reliability import db


def wait():
    deadline = time.monotonic()+20
    while time.monotonic()<deadline:
        state = collections.current()
        if not state['running']:
            return state
        time.sleep(.01)
    pytest.fail('automatic collections did not finish')


def expand(case, total):
    factory, settings, server = case
    factory.configure(autoflush=False)
    server_hash = hashlib.sha256(settings.emby_url.encode()).hexdigest()
    with factory() as session:
        for number in range(4, total+1):
            code = f'ABP-{100+number}'
            session.add_all([Code(code=code, title=f'作品{number}', casts='演员甲', series='系列甲', status=CodeStatus.COMPLETED),
                EmbyWatch(user_id='selected', item_id=str(number), code=code, server=server_hash),
                MediaLink(link_path=f'/library/{code}.mp4',source_path=f'/source/{code}.mp4',code=code)])
            server.movies[str(number)] = {'Id':str(number),'Type':'Movie','Name':code,'Path':f'/library/{code}.mp4'}
        session.commit()


def test_full_library_over_500_automatically_publishes_without_duplicate_create(case, monkeypatch):
    expand(case, 501)
    _, _, server = case
    calls = []
    def ai(items, mode):
        assert len(items)<=80 and mode=='actor'
        assert all('paths' not in item and 'emby_ids' not in item for item in items)
        calls.append([item['code'] for item in items])
        return []
    monkeypatch.setattr(collections, '_ai', ai)
    collections.automatic('actor')
    state = wait()
    assert state['phase']=='published', state
    assert state['analyzed']==state['scope']['included']==501
    assert not state['scope']['truncated'] and state['scope']['limit']==0
    assert len(calls)==7 and len(set(code for batch in calls for code in batch))==501
    assert len(server.collections)==1
    assert next(iter(server.collections.values()))['ids']==set(server.movies)
    assert all(len(params['Ids'].split(','))<=100 for _,params in server.posts)
    posts = list(server.posts)
    collections.automatic('actor')
    assert wait()['phase']=='published'
    assert server.posts==posts


def restart(monkeypatch):
    monkeypatch.setattr(collections,'_PLAN',None)
    monkeypatch.setattr(collections,'_STATE',{'running':False,'phase':'idle','done':0,'total':0,'groups':[],'error':'','token':''})


def test_analysis_checkpoint_survives_restart_skips_completed_batch(case, monkeypatch):
    expand(case, 81)
    calls = []
    def ai(items, mode):
        calls.append([item['code'] for item in items])
        if len(calls)==2:
            raise ValueError('model temporarily unavailable')
        return []
    monkeypatch.setattr(collections,'_ai',ai)
    collections.automatic('actor')
    state = wait()
    assert state['phase']=='failed' and state['analyzed']==80 and state['can_resume']
    # Emulate a process stopping with the durable running flag still set.
    with collections._LOCK:
        collections._STATE['running']=True
        collections._save()
    restart(monkeypatch)
    assert collections.current()['phase']=='interrupted'
    collections.resume()
    result = wait()
    assert result['phase']=='published' and result['analyzed']==81
    assert len(calls)==3 and calls[-1]==calls[-2]
    assert len(calls[0])==80 and len(calls[-1])==2
    assert len(case[2].collections)==1


def test_partial_publication_resume_keeps_success_and_reconciles_timeout(case, monkeypatch):
    _, _, server = case
    monkeypatch.setattr(collections,'_ai',lambda *args: [])
    server.timeout_create=True
    collections.automatic()
    result = wait()
    assert result['phase']=='partial' and result['can_resume']
    assert len(server.collections)==2
    successful = next(row['remote_id'] for row in result['groups'] if row['publish_status']=='published')
    posts = list(server.posts)
    restart(monkeypatch)
    monkeypatch.setattr(collections,'_ai',lambda *args: pytest.fail('finished AI analysis must not repeat'))
    collections.resume()
    result = wait()
    assert result['phase']=='published'
    assert len(server.collections)==2 and server.posts==posts
    assert successful in {row['remote_id'] for row in result['groups']}
    assert all(row['publish_status']=='published' for row in result['groups'])


def test_more_than_40_groups_publish_automatically(case, monkeypatch):
    expand(case, 81)
    def ai(items, mode):
        return [{'kind':'theme','name':f"主题 {items[index]['code']}", 'codes':[items[index]['code'],items[index+1]['code']]}
                for index in range(0,len(items)-1,2)]
    monkeypatch.setattr(collections,'_ai',ai)
    collections.automatic('theme')
    state = wait()
    assert state['phase']=='published',state
    assert state['total']==state['done']==41
    assert len(case[2].collections)==41


def test_auto_api_uses_all_movies_and_preserves_preview_option(case, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.embycollection import router
    app=FastAPI()
    app.include_router(router)
    client=TestClient(app)
    assert client.post('/emby/collections/auto',json={'mode':'all'}).status_code in (401,403)
    app.dependency_overrides[get_current_user]=lambda:'tester'
    monkeypatch.setattr(collections,'_ai',lambda *args: [])
    assert client.post('/emby/collections/auto',json={'mode':'actor'}).json()['code']==200
    assert wait()['phase']=='published'
    posts=list(case[2].posts)
    assert client.post('/emby/collections/suggest',json={'mode':'all'}).json()['code']==200
    state=wait()
    assert state['phase']=='ready' and state['scope']['limit']==0
    assert case[2].posts==posts
    assert client.post('/emby/collections/resume').json()['code']==400


def test_repeated_ai_rename_of_same_group_reuses_managed_collection(case, monkeypatch):
    name = ['第一次名称']
    monkeypatch.setattr(collections,'_ai',lambda *args:[{'kind':'theme','name':name[0],'codes':['ABP-101','ABP-102']}])
    collections.automatic('theme')
    first=wait()
    assert first['phase']=='published'
    posts=list(case[2].posts)
    name[0]='AI 换了一个名字'
    collections.automatic('theme')
    second=wait()
    assert second['phase']=='published'
    assert second['groups'][0]['name']==first['groups'][0]['name']
    assert case[2].posts==posts and len(case[2].collections)==1
