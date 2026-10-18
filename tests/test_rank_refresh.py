import json
import threading
import time

import pytest

from app import services
from app.database.models import Cache, Code
from app.services import rankpages
from tests.test_curation import client, db, no_startup


@pytest.fixture(autouse=True)
def isolated(db, monkeypatch):
    db.configure(autoflush=False)
    monkeypatch.setattr(rankpages, '_STATES', {})
    monkeypatch.setattr(rankpages, '_ACTIVE', set())
    monkeypatch.setattr(services, 'get_rank_cache', lambda *args, **kwargs: None)


def wait(kind='daily'):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = rankpages.read(kind)
        if not result['refreshing']:
            return result
        time.sleep(.01)
    pytest.fail('rank refresh did not finish')


def test_slow_source_never_blocks_page_and_duplicate_refresh_is_single_flight(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(kind, **kwargs):
        calls.append((kind, kwargs)); entered.set()
        assert release.wait(3)
        return [{'code': 'ABC-101', 'title': '新日榜'}]
    monkeypatch.setattr(services, 'get_rank_items', fetch)
    try:
        started = time.monotonic()
        result = client.get('/api/v1/rank?rank_type=daily&limit=120').json()['data']
        assert time.monotonic() - started < 1
        assert result['refreshing'] and result['items'] == []
        assert entered.wait(1)
        for _ in range(3):
            assert client.post('/api/v1/rank/refresh?rank_type=daily').json()['data']['refreshing']
        assert len(calls) == 1
    finally:
        release.set(); result = wait()
    assert result['phase'] == 'completed' and result['items'][0]['title'] == '新日榜'
    assert calls == [('daily', {'refresh': True, 'fetch_limit': 0})]
    assert result['fetched_at'] > 0 and not result['stale']


def test_failed_refresh_retains_snapshot_across_restart_and_does_not_retry_every_poll(db, monkeypatch):
    saved = {'items': [{'code': 'ABC-101', 'title': '上次日榜', 'status': 0}], 'fetched_at': 1,
             'running': False, 'phase': 'completed', 'attempt_at': 0}
    with db.begin() as session:
        session.add_all([Cache(namespace='rank-pages', key='daily', content=json.dumps(saved)), Code(code='ABC-101', status=1)])
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise TimeoutError('source unavailable')
    monkeypatch.setattr(services, 'get_rank_items', fail)
    rankpages.read('daily', force=True)
    result = wait()
    assert result['phase'] == 'failed' and result['stale'] and result['error']
    assert result['items'][0]['title'] == '上次日榜' and result['items'][0]['status'] == 1
    for _ in range(5):
        assert rankpages.read('daily')['phase'] == 'failed'
    assert len(calls) == 1
    rankpages._STATES.clear()
    assert rankpages.read('daily')['items'][0]['title'] == '上次日榜'
    assert len(calls) == 1


def test_cold_failed_rank_is_not_replaced_with_unrelated_local_recommendations(client, db, monkeypatch):
    with db.begin() as session:
        session.add(Code(code='ABC-999', status=4, star=10))
    monkeypatch.setattr(services, 'get_rank_items', lambda *a, **k: [])
    client.get('/api/v1/rank?rank_type=daily')
    result = wait()
    assert result['phase'] == 'failed' and result['items'] == []
    assert result['source'] == 'javdb'


def test_fast_fetch_skips_detail_network_and_sets_redis_expiry(db, monkeypatch):
    from app.modules import ladysite
    monkeypatch.setattr(ladysite, 'get_rank', lambda kind: [{'code': 'ABC-101'}])
    monkeypatch.setattr(services, 'fill_lack_codes_by_list', lambda *a, **k: pytest.fail('details must not delay rank refresh'))
    writes = []
    monkeypatch.setattr(services, 'set_rank_cache', lambda *a, **k: writes.append((a, k)))
    result = services.get_rank_items('daily', refresh=True, fetch_limit=0)
    assert result[0]['code'] == 'ABC-101'
    assert writes[0][1]['ttl'] == services.RANK_CACHE_TTL


def test_legacy_snapshot_is_shown_while_refreshing_and_local_details_update(db, monkeypatch):
    old = [{'code': 'ABC-101', 'title': '旧标题', 'status': 0}]
    monkeypatch.setattr(services, 'get_rank_cache', lambda *a, **k: json.dumps(old))
    with db.begin() as session:
        session.add(Code(code='ABC-101', title='已补全的标题', status=1))
    entered, release = threading.Event(), threading.Event()
    def fetch(*args, **kwargs):
        entered.set(); assert release.wait(3)
        return old
    monkeypatch.setattr(services, 'get_rank_items', fetch)
    try:
        result = rankpages.read('daily')
        assert entered.wait(1)
        assert result['items'][0]['title'] == '已补全的标题'
        assert result['items'][0]['status'] == 1 and result['refreshing']
    finally:
        release.set(); wait()
