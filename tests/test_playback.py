import hashlib
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.database.base import DBBase
from app.database.models import Code, EmbyWatch, LibraryPreference, PlaybackSession, PlaybackSpan
from app.services import embysync, playback


@pytest.fixture
def state(monkeypatch):
    import app.database.session as database
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    settings = Settings(emby_url='http://emby.test', emby_api_key='test-key', playback_tracking_enabled=True, playback_timezone='Asia/Shanghai')
    monkeypatch.setattr(playback, 'get_settings', lambda: settings)
    monkeypatch.setattr(embysync, 'get_settings', lambda: settings)
    now = [datetime(2026, 10, 15, 15, 59, 30)]  # 上海时间 23:59:30
    monkeypatch.setattr(playback, '_now', lambda: now[0])
    yield settings, factory, now
    engine.dispose()


def playing(*, user='user-1', item='item-1', position=0, paused=False):
    return {'Id': 'session-1', 'DeviceId': 'device-1', 'DeviceName': '客厅电视', 'Client': 'Emby TV',
            'UserId': user, 'UserName': user, 'NowPlayingItem': {'Id': item, 'Type': 'Movie', 'Name': 'SONE-725 标题'},
            'PlayState': {'PositionTicks': position * 10_000_000, 'IsPaused': paused}}


def hook(raw, event):
    result = dict(raw)
    result['Item'] = result.pop('NowPlayingItem')
    result['Event'] = event
    return result


def test_pause_seek_duplicate_and_midnight_calendar(state):
    _, db, now = state
    playback.webhook(hook(playing(), 'playback.start'))
    now[0] += timedelta(seconds=60)
    playback.webhook(hook(playing(position=600), 'playback.pause'))
    now[0] += timedelta(seconds=60)
    playback.webhook(hook(playing(position=600), 'playback.unpause'))
    now[0] += timedelta(seconds=30)
    stop = hook(playing(position=630), 'playback.stop')
    playback.webhook(stop)
    playback.webhook(stop)
    report = playback.report('2026-10')
    days = {d['date']: d for d in report['days']}
    assert report['plays'] == 1 and report['seconds'] == 90
    assert days['2026-10-15']['seconds'] == 30
    assert days['2026-10-16']['seconds'] == 60
    assert report['active_days'] == 2
    assert playback.report('2026-10', day='2026-10-16')['total'] == 1
    with db() as session:
        assert len(session.scalars(select(PlaybackSession)).all()) == 1
        assert len(session.scalars(select(PlaybackSpan)).all()) == 2


def test_offline_gap_is_not_invented_as_watch_time(state):
    _, _, now = state
    playback.webhook(hook(playing(), 'playback.start'))
    now[0] += timedelta(minutes=60)
    playback.webhook(hook(playing(position=3600), 'playback.progress'))
    assert playback.report('2026-10')['seconds'] == 0
    assert playback.report('2026-10')['plays'] == 2


def test_wrong_item_stop_does_not_close_current_play(state):
    _, db, now = state
    playback.webhook(hook(playing(item='old'), 'playback.start'))
    now[0] += timedelta(seconds=20)
    playback.webhook(hook(playing(item='new'), 'playback.start'))
    playback.webhook(hook(playing(item='old'), 'playback.stop'))
    with db() as session:
        active = session.scalars(select(PlaybackSession).where(PlaybackSession.active.is_(True))).all()
        assert len(active) == 1 and active[0].item_id == 'new'


def test_selected_user_and_disabled_mode(state):
    settings, _, _ = state
    settings.emby_user_id = 'selected'
    assert playback.webhook(hook(playing(), 'playback.start'))['ignored']
    playback.webhook(hook(playing(user='selected'), 'playback.start'))
    assert playback.report('2026-10')['plays'] == 1
    settings.playback_tracking_enabled = False
    assert playback.webhook(hook(playing(user='selected'), 'playback.start'))['ignored']


def test_poll_failure_preserves_active_session_and_success_closes(state, monkeypatch):
    _, db, now = state
    responses = [playing()]
    failing = [False]
    def handle(request):
        assert request.headers['X-Emby-Token'] == 'test-key'
        assert request.url.path == '/emby/Sessions'
        return httpx.Response(503) if failing[0] else httpx.Response(200, json=responses)
    client_class = httpx.Client
    monkeypatch.setattr(playback.httpx, 'Client', lambda **kw: client_class(transport=httpx.MockTransport(handle), **kw))
    assert playback.poll() == 1
    now[0] += timedelta(seconds=60)
    responses[0] = playing(position=60)
    playback.poll()
    failing[0] = True
    playback.poll()
    with db() as session:
        assert session.scalar(select(PlaybackSession)).active
    failing[0] = False
    responses.clear()
    playback.poll()
    assert playback.report('2026-10')['seconds'] == 60
    with db() as session:
        assert not session.scalar(select(PlaybackSession)).active


def test_invalid_snapshots_are_ignored():
    assert playback._snapshot({'Session': []}) is None
    assert playback._snapshot({'Session': ['bad']}) is None
    assert playback._snapshot({'NowPlayingItem': {'Type': 'Audio', 'Id': 'x'}}) is None


@pytest.mark.parametrize('action,method,resource,field,expected', [
    ('favorite', 'POST', 'FavoriteItems', 'favorite', True),
    ('unfavorite', 'DELETE', 'FavoriteItems', 'favorite', False),
    ('played', 'POST', 'PlayedItems', 'played', True),
    ('unplayed', 'DELETE', 'PlayedItems', 'played', False),
])
def test_emby_actions_are_scoped_and_do_not_modify_local_favorites(state, monkeypatch, action, method, resource, field, expected):
    settings, db, _ = state
    settings.emby_user_id = 'selected'
    with db() as session:
        session.add(Code(code='SONE-725'))
        session.add(EmbyWatch(user_id='selected', item_id='item-1', code='SONE-725', server=hashlib.sha256(settings.emby_url.encode()).hexdigest()))
        session.add(LibraryPreference(code='SONE-725', favorite=True))
        session.commit()
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == method
        assert request.url.path == f'/emby/Users/selected/{resource}/item-1'
        return httpx.Response(200, json={'IsFavorite': expected, 'Played': expected, 'PlayCount': 7})
    client_class = httpx.Client
    monkeypatch.setattr(embysync.httpx, 'Client', lambda **kw: client_class(transport=httpx.MockTransport(handle), **kw))
    assert embysync.set_user_state('SONE-725', 'item-1', action)[field] == expected
    with pytest.raises(ValueError): embysync.set_user_state('SONE-726', 'item-1', action)
    settings.emby_user_id = 'other'
    with pytest.raises(ValueError): embysync.set_user_state('SONE-725', 'item-1', action)
    with db() as session:
        assert session.get(LibraryPreference, 'SONE-725').favorite
        assert not session.scalars(select(PlaybackSession)).all()
    assert len(requests) == 1


def test_remote_action_failure_preserves_snapshot(state, monkeypatch):
    settings, db, _ = state
    settings.emby_user_id = 'selected'
    with db() as session:
        session.add(Code(code='SONE-725'))
        session.add(EmbyWatch(user_id='selected', item_id='item-1', code='SONE-725', server=hashlib.sha256(settings.emby_url.encode()).hexdigest(), favorite=False))
        session.commit()
    client_class = httpx.Client
    monkeypatch.setattr(embysync.httpx, 'Client', lambda **kw: client_class(transport=httpx.MockTransport(lambda req: httpx.Response(500)), **kw))
    with pytest.raises(httpx.HTTPStatusError): embysync.set_user_state('SONE-725', 'item-1', 'favorite')
    with db() as session:
        assert not session.get(EmbyWatch, ('selected', 'item-1')).favorite


def test_playback_webhook_never_enters_deletion_handler(state, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import webhook as api
    monkeypatch.setattr(api, '_check_token', lambda request: True)
    def forbidden(*args, **kwargs):
        raise AssertionError('播放事件不能触发删除')
    monkeypatch.setattr(api, 'handle_media_deleted', forbidden)
    app = FastAPI(); app.include_router(api.router)
    response = TestClient(app).post('/webhook/emby', json=hook(playing(), 'playback.start'))
    assert response.status_code == 200 and response.json()['data']['recorded']


def test_playback_api_auth_and_calendar_validation(state):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.curation import router
    app = FastAPI(); app.include_router(router)
    client = TestClient(app)
    assert client.get('/playback?month=2026-10').status_code == 401
    assert client.post('/library/SONE-725/emby', json={'item_id': 'item-1', 'action': 'favorite'}).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    assert client.get('/playback?month=2026-13').status_code == 422
    assert client.get('/playback?month=2026-10&day=2026-11-01').status_code == 422
    assert client.get('/playback?month=2026-10').json()['data']['plays'] == 0
