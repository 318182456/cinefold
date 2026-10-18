from datetime import datetime

import pytest

from app.database.models import Code, EmbyWatch, LibraryPreference
from app.services.library import library_items
from app.services.playlists import validate
from tests.test_curation import client, db, no_startup


@pytest.fixture
def library(db):
    db.configure(autoflush=False)
    with db.begin() as session:
        session.add_all([
            Code(code='ABC-001', status=4, genres='主题甲', casts='演员甲,演员乙', create_time=datetime(2025, 1, 1)),
            Code(code='ABC-002', status=4, genres='主题乙', casts='演员甲', create_time=datetime(2026, 1, 1)),
            LibraryPreference(code='ABC-001', rating=9, favorite=True),
            LibraryPreference(code='ABC-002', rating=6, watch_later=True),
            EmbyWatch(user_id='u', item_id='one', code='ABC-001', play_count=1, played=True),
            EmbyWatch(user_id='u', item_id='two', code='ABC-002', play_count=5, played=False),
        ])


def test_system_playlists_are_live_and_sorted(client, library, db):
    systems = client.get('/api/v1/collections/system').json()['data']
    expected = {'top_played': 'ABC-002', 'top_rated': 'ABC-001',
                'most_actresses': 'ABC-001', 'latest_added': 'ABC-002'}
    assert {s['key'] for s in systems} == set(expected)
    for key, first in expected.items():
        result = client.get('/api/v1/library', params={'system_collection': key}).json()['data']
        assert result['total'] == 2 and result['items'][0]['code'] == first
    with db.begin() as session:
        session.get(EmbyWatch, ('u', 'one')).play_count = 10
    assert library_items(system_collection='top_played')['items'][0]['code'] == 'ABC-001'
    assert client.get('/api/v1/library', params={'system_collection': 'invalid'}).status_code == 404


def test_smart_conditions_saved_combined_and_dynamic(client, library):
    body = {'name': '按条件收录', 'kind': 'smart', 'conditions': {
        'genres': '主题乙', 'states': ['favorite'], 'op': 'OR', 'min_rating': 7, 'sort': 'rating'}}
    response = client.post('/api/v1/collections', json=body)
    assert response.status_code == 200, response.text
    key = response.json()['data']['id']
    def members():
        return [i['code'] for i in client.get('/api/v1/library', params={'collection_id': key}).json()['data']['items']]
    assert members() == ['ABC-001']
    client.patch('/api/v1/library/ABC-002/preference', json={'rating': 10})
    assert members() == ['ABC-002', 'ABC-001']
    body['conditions'].update(op='AND', states=['unwatched'])
    assert client.put(f'/api/v1/collections/{key}', json=body).status_code == 200
    assert members() == ['ABC-002']
    # Saved rules survive reopening; ordinary filters still intersect the playlist.
    saved = client.get('/api/v1/collections').json()['data'][0]
    assert saved['conditions']['states'] == ['unwatched']
    assert client.get('/api/v1/library', params={'collection_id': key, 'actor': '演员乙'}).json()['data']['total'] == 0


@pytest.mark.parametrize('conditions', [{'states': ['bad']}, {'states': 'favorite'}, {'op': 'bad'},
                                      {'min_rating': -1}, {'min_rating': 11}, {'sort': 'bad'}, {'unknown': 1}])
def test_smart_invalid_rules_rejected(conditions):
    with pytest.raises(ValueError):
        validate(conditions)


def test_whole_library_genres_bridge_ai_batches():
    from app.modules.collections.batches import explicit_groups
    groups = explicit_groups([{'code': f'ABC-{n}', 'genres': '主题甲,1080p,4K'} for n in range(150)], 'all')
    assert len(groups) == 1 and groups[0]['kind'] == 'theme'
    assert len(groups[0]['codes']) == 150
    groups = explicit_groups([{'code': f'ABC-{n}', 'genres': '["主题甲", "4K"]'} for n in range(2)], 'all')
    assert len(groups) == 1 and groups[0]['name'] == '主题 · 主题甲'


def test_analysis_numbers_filter_actual_watched_month_and_release_year(client, library, db):
    with db.begin() as session:
        session.get(EmbyWatch, ('u', 'one')).last_played = '2026-10-17T08:00:00Z'
        session.get(EmbyWatch, ('u', 'two')).last_played = '2026-09-01T08:00:00Z'
        session.get(Code, 'ABC-001').release_date = '2025-03-01'
        session.get(Code, 'ABC-002').release_date = '2026-04-01'
    def codes(params):
        response = client.get('/api/v1/library', params=params)
        assert response.status_code == 200
        return [item['code'] for item in response.json()['data']['items']]
    assert codes({'watched': True}) == ['ABC-001']
    assert codes({'last_played_month': '2026-10'}) == ['ABC-001']
    assert codes({'last_played_month': '2026-09'}) == ['ABC-002']
    assert codes({'release_year': 2026}) == ['ABC-002']
    assert codes({'watched': True, 'release_year': 2026}) == []
    assert client.get('/api/v1/library', params={'last_played_month': '2026-1'}).status_code == 422
