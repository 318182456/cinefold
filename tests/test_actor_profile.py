import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.database.models import Actor, ActorProfile, Code, CodeStatus, EmbyWatch, LibraryPreference, MediaLink, RuleExclusion
from app.services import actorprofile
from tests.test_scrape_reliability import db


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(actorprofile, '_JOBS', {})
    monkeypatch.setattr(actorprofile, '_CAPACITY', threading.BoundedSemaphore(4))


def wait(name='甲'):
    for _ in range(200):
        state = actorprofile.sync_state(name)
        if not state['running']:
            return state
        time.sleep(0.01)
    pytest.fail('actor sync did not finish')


def test_exact_actor_tokens_aliases_and_version_statistics(db):
    with db() as session:
        session.add_all([
            Actor(name='甲', name_2='旧名甲', subscribed=True),
            Code(code='ABP-101', casts='甲', title='Base'),
            Code(code='ABP-101-C', casts='旧名甲', status=CodeStatus.COMPLETED),
            Code(code='ABP-102', casts='甲乙'),
            Code(code='ABP-103', casts='丙、旧名甲'),
            Code(code='ABP-104', casts='甲'),
            LibraryPreference(code='ABP-101-C', favorite=True),
            EmbyWatch(user_id='u', item_id='i', code='ABP-103-U', played=True, favorite=True),
            MediaLink(link_path='/library/x.mp4', source_path='/source/x.mp4', code='ABP-104-C'),
        ])
        session.commit()
    result = actorprofile.profile('甲')
    assert result['actor']['subscribed']
    assert result['stats'] == dict(total=3, owned=2, missing=1, favorite=2, watched=1)
    assert {item['code'] for item in result['items']} == {'ABP-101', 'ABP-103', 'ABP-104'}
    assert actorprofile.profile('旧名甲', filter='missing')['items'][0]['code'] == 'ABP-103'
    assert actorprofile.profile('甲', filter='watched')['total'] == 1
    assert actorprofile.profile('甲', filter='owned', size=1, page=999)['page'] == 2


def test_get_profile_and_directory_never_fetch_or_create_subscription(db, monkeypatch):
    monkeypatch.setattr(actorprofile, '_fetch', lambda *a: pytest.fail('GET must stay local'))
    with db() as session:
        session.add_all([Code(code='ABP-101', casts='甲,乙'), Code(code='ABP-102', casts='甲')])
        session.commit()
    assert actorprofile.profile('甲')['total'] == 2
    assert actorprofile.directory(subscribed=False)['total'] == 2
    assert actorprofile.directory(subscribed=False, page=2, size=1)['items'][0]['name'] == '甲'
    with db() as session:
        assert not list(session.scalars(select(Actor)))
        assert not list(session.scalars(select(ActorProfile)))


def test_directory_deduplicates_known_actor_alias_and_keeps_filters(db):
    with db() as session:
        session.add_all([Actor(name='甲', name_2='旧甲', subscribed=False),
                         Actor(name='乙', subscribed=True), Code(code='ABP-101', casts='旧甲,甲,丙')])
        session.commit()
    assert actorprofile.directory(subscribed=False)['total'] == 3
    assert actorprofile.directory(subscribed=True)['total'] == 1
    assert actorprofile.directory(subscribed=False, keyword='旧甲')['items'][0]['name'] == '甲'


@pytest.mark.parametrize('name', ['', 'x' * 129])
def test_invalid_actor_name_is_rejected(db, name):
    with pytest.raises(ValueError):
        actorprofile.profile(name)
    with pytest.raises(ValueError):
        actorprofile.refresh(name)


def test_remote_import_only_fills_empty_fields_and_keeps_subscription_status(db, monkeypatch):
    with db() as session:
        session.add_all([Actor(name='甲', subscribed=True, photo='manual-photo'),
                         Code(code='ABP-101', title='人工标题', status=CodeStatus.DOWNLOADING)])
        session.commit()
    calls = []
    def fetch(name, page, actor_url):
        calls.append((page, actor_url))
        return {'name': '甲', 'aliases': ['旧甲'], 'photo': 'remote-photo', 'birthday': '1990-01-01',
                'source_url': 'https://javdb.com/actors/id', 'has_more': page < 2,
                'items': [{'code': f'ABP-{100 + page}', 'title': '远端标题', 'casts': '甲',
                           'status': CodeStatus.SUBSCRIBED}]}
    monkeypatch.setattr(actorprofile, '_fetch', fetch)
    actorprofile.refresh('甲', 3)
    state = wait()
    assert state['phase'] == 'finished' and state['remote_pages'] == 2 and state['imported'] == 1
    assert calls == [(1, ''), (2, 'https://javdb.com/actors/id')]
    with db() as session:
        assert session.get(Code, 'ABP-101').title == '人工标题'
        assert session.get(Code, 'ABP-101').status == CodeStatus.DOWNLOADING
        assert session.get(Code, 'ABP-102').status == CodeStatus.NONE
        assert session.get(Actor, '甲').subscribed
        assert session.get(Actor, '甲').photo == 'manual-photo'
        assert session.get(ActorProfile, '甲').data['birthday'] == '1990-01-01'
    assert actorprofile.profile('旧甲')['total'] == 2


def test_verified_actor_enriches_existing_cast_without_replacing_it(db):
    with db() as session:
        session.add_all([Code(code='ABP-101', casts='乙', title='manual'),
                         Code(code='ABP-102', casts='旧甲,乙')])
        session.commit()
    actorprofile._import('甲', {'name': '甲', 'aliases': ['旧甲'], 'items': [
        {'code': 'ABP-101', 'casts': '甲'}, {'code': 'ABP-102', 'casts': '甲'}]})
    with db() as session:
        assert session.get(Code, 'ABP-101').casts == '乙,甲'
        assert session.get(Code, 'ABP-101').title == 'manual'
        assert session.get(Code, 'ABP-102').casts == '旧甲,乙'
    assert actorprofile.profile('甲')['total'] == 2


def test_remote_failure_and_page_limit_are_explicit(db, monkeypatch):
    monkeypatch.setattr(actorprofile, '_fetch', lambda *args: {'name': '甲', 'items': [], 'has_more': True})
    actorprofile.refresh('甲', 1)
    assert wait()['truncated'] and actorprofile.sync_state('甲')['remote_pages'] == 1
    def fail(*args):
        raise ValueError('站点要求验证，未获取演员资料')
    monkeypatch.setattr(actorprofile, '_fetch', fail)
    actorprofile.refresh('甲')
    assert '站点要求验证' in wait()['error']
    with db() as session:
        assert '站点要求验证' in session.get(ActorProfile, '甲').error


def test_refresh_deduplicates_name_and_bounds_pending_tasks(db, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fetch(*args):
        entered.set()
        release.wait(2)
        return {'items': [], 'has_more': False}
    monkeypatch.setattr(actorprofile, '_fetch', fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(actorprofile, '_POOL', pool)
        try:
            actorprofile.refresh('甲')
            assert entered.wait(1)
            actorprofile.refresh('甲')
            for name in ('乙', '丙', '丁'):
                actorprofile.refresh(name)
            with pytest.raises(ValueError, match='4个'):
                actorprofile.refresh('戊')
            assert len(actorprofile._JOBS) == 4
        finally:
            release.set()


def test_bulk_subscribe_is_exact_idempotent_and_preserves_owned_downloads_exclusions(db):
    with db() as session:
        session.add_all([
            Actor(name='甲', name_2='旧甲', subscribed=False),
            Code(code='ABP-101', casts='甲', status=CodeStatus.NONE),
            Code(code='ABP-102', casts='甲', status=CodeStatus.FAILED),
            Code(code='ABP-103', casts='甲', status=CodeStatus.DOWNLOADING),
            Code(code='ABP-104', casts='旧甲', status=CodeStatus.DOWNLOADED),
            Code(code='ABP-105', casts='甲'),
            Code(code='ABP-105-C', casts='甲', status=CodeStatus.COMPLETED),
            Code(code='ABP-106', casts='甲'), RuleExclusion(code='ABP-106'),
            Code(code='ABP-107', casts='甲'),
            MediaLink(link_path='/lib/107', source_path='/src/107', code='ABP-107-U'),
            Code(code='ABP-108', casts='甲乙'),
        ])
        session.commit()
    result = actorprofile.subscribe_works('甲')
    assert result == dict(matched=7, subscribed=2, already_subscribed=0, owned=2, downloading=2, excluded=1, skipped=0)
    again = actorprofile.subscribe_works('甲')
    assert again['subscribed'] == 0 and again['already_subscribed'] == 2
    with db() as session:
        assert session.get(Code, 'ABP-105').status == CodeStatus.NONE
        assert session.get(Code, 'ABP-106').status == CodeStatus.NONE
        assert session.get(Code, 'ABP-108').status == CodeStatus.NONE
        assert session.get(Code, 'ABP-103').status == CodeStatus.DOWNLOADING
        assert not session.get(Actor, '甲').subscribed


def test_profile_routes_stay_local_and_subscribe_explicitly(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.actors import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: 'tester'
    with db() as session:
        session.add(Code(code='ABP-101', casts='甲'))
        session.commit()
    client = TestClient(app)
    assert client.get('/actors/profile?name=甲').json()['data']['stats']['total'] == 1
    assert client.get('/actors?subscribed=false').json()['data']['total'] == 1
    assert client.get('/actors/profile/sync?name=甲').json()['data']['phase'] == 'idle'
    assert client.post('/actors/profile/subscribe', json={'name': '甲'}).json()['data']['subscribed'] == 1


def test_refresh_activates_one_budget_for_all_pages_and_cancels_it(db, monkeypatch):
    from app.modules.ladysite.transport import current_budget
    budgets = []
    def fetch(name, page, actor_url):
        budgets.append(current_budget())
        return {'name': name, 'items': [], 'has_more': page < 2}
    monkeypatch.setattr(actorprofile, '_fetch', fetch)
    actorprofile.refresh('甲', 2)
    assert wait()['phase'] == 'finished'
    assert len(budgets) == 2 and budgets[0] is budgets[1]
    assert budgets[0]._cancelled.is_set()


def test_sync_failure_survives_memory_reset_and_never_creates_actor_subscription(db, monkeypatch):
    def fail(*args):
        raise ValueError('verification required')
    monkeypatch.setattr(actorprofile, '_fetch', fail)
    actorprofile.refresh('甲')
    assert wait()['phase'] == 'failed'
    monkeypatch.setattr(actorprofile, '_JOBS', {})
    assert actorprofile.profile('甲')['sync']['error'] == 'verification required'
    with db() as session:
        assert session.get(Actor, '甲') is None
