"""No real server writes: verify collection requests against an in-memory Emby."""
import hashlib
import threading
import time

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import Code, CodeStatus, EmbyCollectionLink, EmbyWatch, MediaLink
from app.services import embycollections as collections
from tests.test_scrape_reliability import db


class Server:
    def __init__(self):
        self.movies = {str(i): {'Id': str(i), 'Type': 'Movie', 'Name': f'ABP-{100 + i}',
                               'Path': f'/library/ABP-{100 + i}.mp4'} for i in (1, 2, 3)}
        self.collections = {}
        self.posts = []
        self.timeout_create = False
        self.timeout_append = False

    def __call__(self, request):
        path, query = request.url.path, request.url.params
        if request.method == 'GET':
            if path.endswith('/Items'):
                if query.get('IncludeItemTypes') == 'BoxSet':
                    items = [{'Id': key, 'Type': 'BoxSet', 'Name': item['name']} for key, item in self.collections.items()]
                else:
                    group = self.collections.get(query.get('ParentId'), {'ids': set()})
                    items = [self.movies[key] for key in sorted(group['ids'])]
                return httpx.Response(200, json={'Items': items, 'TotalRecordCount': len(items)})
            item_id = path.rsplit('/', 1)[-1]
            if item_id in self.movies:
                return httpx.Response(200, json=self.movies[item_id])
            if item_id in self.collections:
                return httpx.Response(200, json={'Id': item_id, 'Type': 'BoxSet', 'Name': self.collections[item_id]['name']})
            return httpx.Response(404, json={})
        assert request.method == 'POST', 'must never delete/update movies or user collections'
        assert request.content == b'', 'Emby collections require query params, not JSON'
        self.posts.append((path, dict(query)))
        ids = set(query['Ids'].split(','))
        assert ids <= set(self.movies), 'numbers must never be used as Emby item IDs'
        if path == '/emby/Collections':
            key = f'collection-{len(self.collections) + 1}'
            self.collections[key] = {'name': query['Name'], 'ids': ids}
            if self.timeout_create:
                self.timeout_create = False
                raise httpx.ReadTimeout('create response lost', request=request)
            return httpx.Response(200, json={'Id': key})
        key = path.split('/')[-2]
        self.collections[key]['ids'].update(ids)
        if self.timeout_append:
            self.timeout_append = False
            raise httpx.ReadTimeout('append response lost', request=request)
        return httpx.Response(204)


@pytest.fixture
def case(db, monkeypatch):
    settings = Settings(emby_url='http://emby.test', emby_api_key='test-key', emby_user_id='selected')
    server_hash = hashlib.sha256(settings.emby_url.encode()).hexdigest()
    with db() as session:
        for i in (1, 2, 3):
            code = f'ABP-{100 + i}'
            session.add_all([Code(code=code, title=f'作品{i}', casts='演员甲', series='系列甲', status=CodeStatus.COMPLETED),
                             EmbyWatch(user_id='selected', item_id=str(i), code=code, server=server_hash),
                             MediaLink(link_path=f'/library/{code}.mp4', source_path=f'/source/{code}.mp4', code=code)])
        session.commit()
    server = Server()
    original = httpx.Client
    monkeypatch.setattr(collections, 'get_settings', lambda: settings)
    monkeypatch.setattr(collections, '_client', lambda credential: original(transport=httpx.MockTransport(server), headers={'X-Emby-Token': credential}))
    monkeypatch.setattr(collections, '_PLAN', None)
    monkeypatch.setattr(collections, '_STATE', {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'error': '', 'groups': [], 'token': ''})
    monkeypatch.setattr(collections, '_ai', lambda items, mode: [{'name': '测试系列', 'kind': 'series', 'reason': '相同系列', 'codes': ['ABP-101', 'ABP-102'], 'confidence': 0.9}])
    return db, settings, server


def wait():
    for _ in range(300):
        state = collections.current()
        if not state['running']:
            return state
        time.sleep(0.01)
    pytest.fail('collection task did not finish')


def preview():
    collections.suggest()
    state = wait()
    assert state['phase'] == 'ready', state
    return state


def publish(state, codes=None, name=None):
    group = state['groups'][0]
    collections.publish(state['token'], [{'id': group['id'], 'name': name or group['name'], 'codes': codes or group['codes']}])
    return wait()


def test_normalized_duplicate_names_are_rejected_before_server_writes(case, monkeypatch):
    _, _, server = case
    monkeypatch.setattr(collections, '_ai', lambda *args: [
        {'name': 'series', 'kind': 'series', 'codes': ['ABP-101', 'ABP-102']},
        {'name': 'theme', 'kind': 'theme', 'codes': ['ABP-101', 'ABP-103']},
    ])
    state = preview()
    groups = [{**group, 'name': name} for group, name in zip(state['groups'], ['A合集', 'Ａ合集'])]
    with pytest.raises(ValueError, match='名称重复'):
        collections.publish(state['token'], groups)
    assert not server.posts


def test_suggest_is_local_preview_and_ai_receives_metadata_only(case, monkeypatch):
    _, _, server = case
    def ai(items, mode):
        assert mode == 'all'
        assert all('paths' not in item and 'emby_ids' not in item for item in items)
        assert {item['code'] for item in items} == {'ABP-101', 'ABP-102', 'ABP-103'}
        return [{'name': '建议', 'kind': 'theme', 'codes': ['ABP-101', 'ABP-102', 'NOT-999']}]
    monkeypatch.setattr(collections, '_ai', ai)
    assert collections.current()['phase'] == 'idle'
    state = preview()
    assert not server.posts
    assert state['scope']['included'] == 3 and not state['scope']['truncated']
    assert state['groups'][0]['codes'] == ['ABP-101', 'ABP-102']


def test_publish_query_params_real_movie_ids_and_retry_no_duplicate_create(case):
    factory, _, server = case
    state = preview()
    result = publish(state)
    assert result['phase'] == 'published', result
    assert server.posts == [('/emby/Collections', {'Name': '测试系列', 'Ids': '1,2', 'IsLocked': 'false'})]
    assert publish(state)['phase'] == 'published'
    assert len(server.posts) == 1
    with factory() as session:
        link = session.scalar(select(EmbyCollectionLink))
        assert link.status == 'published' and link.remote_id == 'collection-1'


def test_creation_response_timeout_reconciles_by_name_and_exact_members(case):
    _, _, server = case
    state = preview()
    server.timeout_create = True
    assert publish(state)['phase'] == 'partial'
    assert publish(state)['phase'] == 'published'
    assert len(server.posts) == 1


def test_unknown_create_never_reposts_when_remote_outcome_cannot_be_confirmed(case):
    _, _, server = case
    state = preview()
    server.timeout_create = True
    publish(state)
    server.collections.clear()
    assert publish(state)['phase'] == 'partial'
    assert len(server.posts) == 1


def test_unknown_same_name_wrong_members_is_not_adopted(case):
    _, _, server = case
    state = preview()
    server.timeout_create = True
    publish(state)
    server.collections['collection-1']['ids'] = {'1', '3'}
    assert publish(state)['phase'] == 'partial'
    assert len(server.posts) == 1


def test_external_same_name_collection_is_never_taken_over(case):
    _, _, server = case
    server.collections['external'] = {'name': '测试系列', 'ids': {'3'}}
    result = publish(preview())
    assert result['phase'] == 'partial' and '外部' in result['error']
    assert not server.posts and server.collections['external']['ids'] == {'3'}


@pytest.mark.parametrize('bad', [
    {'Name': 'ABP-1011', 'Path': '/library/ABP-1011.mp4'},
    {'Name': 'ABP-00101', 'Path': '/library/ABP-00101.mp4'},
    {'Type': 'Episode'},
    {'Name': 'ABP-101', 'Path': '/library/ABP-102.mp4'},
])
def test_movie_identity_recheck_blocks_similar_codes_wrong_type_and_path(case, bad):
    _, _, server = case
    state = preview()
    server.movies['1'].update(bad)
    assert publish(state)['phase'] == 'partial'
    assert not server.posts


def test_token_rejects_new_members_and_changed_server_user_or_snapshot(case):
    factory, settings, server = case
    state = preview()
    with pytest.raises(ValueError, match='原建议'):
        publish(state, codes=['ABP-101', 'ABP-103'])
    settings.emby_user_id = 'other'
    with pytest.raises(ValueError, match='已变化'):
        publish(state)
    settings.emby_user_id = 'selected'
    with factory() as session:
        session.get(EmbyWatch, ('selected', '1')).server = 'wrong-server'
        session.commit()
    with pytest.raises(ValueError, match='已变化'):
        publish(state)
    assert not server.posts


def test_append_only_managed_collection_and_timeout_rechecks_members(case, monkeypatch):
    _, _, server = case
    assert publish(preview())['phase'] == 'published'
    monkeypatch.setattr(collections, '_ai', lambda *args: [{'name': '测试系列', 'kind': 'series', 'codes': ['ABP-101', 'ABP-102', 'ABP-103']}])
    state = preview()
    server.timeout_append = True
    assert publish(state)['phase'] == 'partial'
    assert publish(state)['phase'] == 'published'
    assert server.collections['collection-1']['ids'] == {'1', '2', '3'}
    assert server.posts[-1] == ('/emby/Collections/collection-1/Items', {'Ids': '3'})
    assert len(server.posts) == 2


def test_categories_and_overlapping_groups_are_preserved(case, monkeypatch):
    monkeypatch.setattr(collections, '_ai', lambda *args: [
        {'name': kind, 'kind': kind, 'codes': ['ABP-101', 'ABP-102']} for kind in ('series', 'theme', 'sequel', 'actor', 'style')])
    state = preview()
    assert {group['kind'] for group in state['groups']} == {'series', 'theme', 'sequel', 'actor', 'style'}
    assert len(state['groups']) == 5


def test_missing_mapping_is_excluded_and_only_selected_server_user_is_used(case):
    factory, _, _ = case
    with factory() as session:
        session.get(EmbyWatch, ('selected', '3')).server = 'different-server'
        session.add(EmbyWatch(user_id='other', item_id='foreign', code='ABP-103', server='different-server'))
        session.commit()
    state = preview()
    assert state['scope']['included'] == 2 and state['scope']['unmapped'] == 1


def test_maintenance_busy_blocks_publish_and_missing_config_is_clear(case, monkeypatch):
    _, settings, server = case
    from app.services import scrapemaintenance
    state = preview()
    monkeypatch.setattr(scrapemaintenance, 'busy', lambda: True)
    with pytest.raises(ValueError, match='重建'):
        publish(state)
    assert not server.posts
    settings.emby_user_id = ''
    with pytest.raises(ValueError, match='配置'):
        collections.suggest()


def test_collection_routes_current_is_local_and_actions_explicit(case, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.embycollection import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: 'tester'
    client = TestClient(app)
    assert client.get('/emby/collections/current').json()['data']['phase'] == 'idle'
    response = client.post('/emby/collections/suggest', json={'mode': 'all', 'limit': 200})
    assert response.json()['code'] == 200
    state = wait()
    group = state['groups'][0]
    result = client.post('/emby/collections/publish', json={'token': state['token'], 'groups': [{'id': group['id'], 'name': group['name'], 'codes': group['codes']}]})
    assert result.json()['code'] == 200
    assert wait()['phase'] == 'published'


def test_suggest_scope_limit_and_unknown_member_ids_are_explicit(case, monkeypatch):
    factory, settings, _ = case
    server_hash = hashlib.sha256(settings.emby_url.encode()).hexdigest()
    with factory() as session:
        for number in range(201, 231):
            session.add_all([Code(code=f'ABP-{number}', title='local', status=CodeStatus.COMPLETED),
                             EmbyWatch(user_id='selected', item_id=str(number), code=f'ABP-{number}', server=server_hash)])
        session.commit()
    captured = []
    def ai(items, mode):
        captured.extend(items)
        return []
    monkeypatch.setattr(collections, '_ai', ai)
    collections.suggest(limit=20)
    state = wait()
    assert state['scope']['included'] == 20 and state['scope']['available'] == 33
    assert state['scope']['truncated'] and len(captured) == 20


def test_busy_suggest_does_not_queue_another_job(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def ai(*args):
        entered.set()
        assert release.wait(2)
        return []
    monkeypatch.setattr(collections, '_ai', ai)
    try:
        collections.suggest()
        assert entered.wait(1)
        with pytest.raises(ValueError, match='正在运行'):
            collections.suggest()
    finally:
        release.set()
        wait()


def test_publish_request_rejects_large_or_forged_groups(case):
    state = preview()
    group = state['groups'][0]
    with pytest.raises(ValueError, match='40'):
        collections.publish(state['token'], [{'id': group['id']}] * 41)
    with pytest.raises(ValueError, match='500'):
        collections.publish(state['token'], [{'id': group['id'], 'name': 'test', 'codes': ['ABP-101'] * 501}])
    with pytest.raises(ValueError, match='预览'):
        collections.publish('forged', [{'id': group['id'], 'name': 'test', 'codes': group['codes']}])
    with pytest.raises(ValueError, match='有效'):
        collections.publish(state['token'], [{'id': 'injected-id', 'name': 'test', 'codes': group['codes']}])


def test_explicit_rename_and_subset_are_allowed_without_editing_other_groups(case, monkeypatch):
    _, _, server = case
    monkeypatch.setattr(collections, '_ai', lambda *args: [{'name': 'Original', 'kind': 'series', 'codes': ['ABP-101', 'ABP-102', 'ABP-103']}])
    state = preview()
    assert publish(state, codes=['ABP-101', 'ABP-103'], name='My collection')['phase'] == 'published'
    assert server.collections['collection-1'] == {'name': 'My collection', 'ids': {'1', '3'}}
