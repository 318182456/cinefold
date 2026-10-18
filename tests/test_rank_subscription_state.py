import json
import threading
import time

import pytest

from app.database.models import Code
from app import services
from app.services import ranksubscriptions
from tests.test_curation import client, db, no_startup


@pytest.fixture(autouse=True)
def isolated(monkeypatch, db):
    db.configure(autoflush=False)
    monkeypatch.setattr(ranksubscriptions, '_STATES', {})
    monkeypatch.setattr(ranksubscriptions, '_ACTIVE', set())
    monkeypatch.setattr(services, '_drop_vanished_history', lambda code: None)
    monkeypatch.setattr(services, 'send_subscribe_message', lambda code: None)
    monkeypatch.setattr(services, 'is_filtered_code', lambda code: '')


@pytest.mark.parametrize('kind', ['rank', 'brand'])
def test_cached_listing_status_tracks_subscribe_cancel_and_download_without_rebuilding_cache(kind, db, monkeypatch):
    with db.begin() as session:
        session.add_all([Code(code='ABC-101', status=0), Code(code='032426_100', status=2)])
    payload = json.dumps([{'code': 'ABC-101', 'status': 0, 'title': '缓存标题'},
                          {'code': '032426-100', 'status': 0}, {'code': 'ABC-999', 'status': 1}])
    monkeypatch.setattr(services, 'get_rank_cache', lambda *a, **k: payload)
    monkeypatch.setattr(services, 'set_rank_cache', lambda *a, **k: pytest.fail('status refresh must not reset content cache TTL'))
    read = (lambda: services.get_rank_items('daily')) if kind == 'rank' else (lambda: services.get_brand_items('test'))
    assert [row['status'] for row in read()] == [0, 2, 0]
    assert services.subscribe_code('ABC-101')
    assert read()[0]['status'] == 1 and read()[0]['title'] == '缓存标题'
    assert services.cancel_subscribe('ABC-101')
    assert read()[0]['status'] == 0
    with db.begin() as session:
        session.get(Code, 'ABC-101').status = 4
    assert read()[0]['status'] == 4
    assert json.loads(payload)[0]['status'] == 0


def test_single_subscribe_reports_filter_rejection_and_returns_committed_state(client, db, monkeypatch):
    monkeypatch.setattr(services, 'is_filtered_code', lambda code: 'VR' if code == 'ABC-101' else '')
    response = client.post('/api/v1/codes/sub', json={'code': 'ABC-101'}).json()
    assert response['code'] == 409 and '过滤' in response['message']
    with db() as session:
        assert session.get(Code, 'ABC-101') is None
    response = client.post('/api/v1/codes/sub', json={'code': 'ABC-102'}).json()
    assert response['code'] == 200 and response['data']['status'] == 1
    with db() as session:
        assert session.get(Code, 'ABC-102').status == 1


def wait(kind):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = ranksubscriptions.current(kind)
        if not result['running']:
            return result
        time.sleep(.01)
    pytest.fail('rank subscription did not finish')


def test_current_rank_background_progress_skips_owned_and_survives_reload(client, db, monkeypatch):
    with db.begin() as session:
        session.add_all([Code(code='ABC-101', status=2), Code(code='ABC-102', status=4)])
    entered, release = threading.Event(), threading.Event()
    def get_rank(kind):
        assert kind == 'weekly'
        entered.set(); assert release.wait(3)
        return [{'code': code} for code in ['ABC-101', 'ABC-102', 'ABC-103', 'ABC-104']]
    monkeypatch.setattr(services, 'get_rank_items', get_rank)
    monkeypatch.setattr(services, 'is_filtered_code', lambda code: 'VR' if code == 'ABC-104' else '')
    try:
        first = client.post('/api/v1/rank/subscribe?rank_type=weekly').json()['data']
        assert entered.wait(1)
        second = client.post('/api/v1/rank/subscribe?rank_type=weekly').json()['data']
        assert first['job_id'] == second['job_id']
    finally:
        release.set(); result = wait('weekly')
    assert result['phase'] == 'completed'
    assert (result['done'], result['total'], result['existing'], result['subscribed'], result['filtered']) == (4, 4, 2, 1, 1)
    with db() as session:
        assert session.get(Code, 'ABC-101').status == 2
        assert session.get(Code, 'ABC-102').status == 4
        assert session.get(Code, 'ABC-103').status == 1
    ranksubscriptions._STATES.clear()
    assert client.get('/api/v1/rank/subscribe/status?rank_type=weekly').json()['data'] == result
    assert client.post('/api/v1/rank/subscribe?rank_type=invalid').status_code == 422


def test_legacy_local_cover_path_reads_disk_without_proxying_or_opening_traversal(client, monkeypatch, tmp_path):
    from app.utils import imagecache
    from app.api.endpoints import picproxy
    monkeypatch.setattr(imagecache, 'PIC_DIR', tmp_path / 'pics')
    monkeypatch.setattr(picproxy, '_get_client', lambda: pytest.fail('local cover must never make a network request'))
    target = imagecache.PIC_DIR / 'MIDA-459' / 'banner.jpg'
    target.parent.mkdir(parents=True)
    content = b'x' * 600
    target.write_bytes(content)
    response = client.get('/api/v1/image-proxy', params={'url': '/pic/MIDA-459/banner.jpg'})
    assert response.status_code == 200 and response.content == content
    assert client.get('/api/v1/image-proxy', params={'url': '/pic/missing/banner.jpg'}).status_code == 404
    outside = tmp_path / 'outside.jpg'; outside.write_bytes(content)
    assert client.get('/api/v1/image-proxy', params={'url': '/pic/../outside.jpg'}).status_code == 404
    assert client.get('/api/v1/image-proxy', params={'url': 'http://evil.test/cover.jpg'}).status_code == 403


def test_scheduled_rank_preserves_existing_download_and_library_state(db, monkeypatch):
    from app import scheduler
    from app.core.config import Settings
    from app.modules import ladysite
    with db.begin() as session:
        session.add_all([Code(code='ABC-101', status=2), Code(code='ABC-102', status=4)])
    monkeypatch.setattr(scheduler, 'get_settings', lambda: Settings(rank_page=1))
    monkeypatch.setattr(ladysite, 'get_rank_codes', lambda *args: ['ABC-101', 'ABC-102', 'ABC-103'])
    assert scheduler.sub_rank() == 1
    assert scheduler.sub_rank() == 0
    with db() as session:
        assert session.get(Code, 'ABC-101').status == 2
        assert session.get(Code, 'ABC-102').status == 4
        assert session.get(Code, 'ABC-103').status == 1
