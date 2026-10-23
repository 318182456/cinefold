from datetime import datetime, timezone

import httpx
import pytest
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.services import playback, playbackreporting
from tests.test_playback import state


COLUMNS = ['RecordId', 'DateCreated', 'UserId', 'ItemId', 'ItemName', 'ClientName', 'DeviceName', 'PlayDuration']


@pytest.fixture
def plugin(monkeypatch):
    rows, requests = [], []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/Users'):
            return httpx.Response(200, json=[{'Id': 'user1', 'Name': 'Viewer'}])
        return httpx.Response(200, json={'colums': COLUMNS, 'results': rows, 'message': ''})
    factory = httpx.Client
    monkeypatch.setattr(playbackreporting.httpx, 'Client', lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    return rows, requests


def get_report(**kwargs):
    return playbackreporting.plugin_report(Settings(emby_url='http://emby.test', emby_api_key='key'), '2026-10', ZoneInfo('Asia/Shanghai'), **kwargs)


def test_dotnet_dates_midnight_pagination_and_stable_repeated_reads(plugin):
    rows, requests = plugin
    rows.extend([
        ['2', '2026-10-15 15:59:30.1234567', 'user1', 'item1', 'SONE-725', 'TV', 'Room', '90'],
        ['1', '2026-09-30 15:59:30.0000000', 'user1', 'item2', 'ADN-737', 'TV', 'Room', '90'],
    ])
    result = get_report(day='2026-10-16', size=1)
    assert result['source'] == 'playback_reporting' and result['plays'] == 2
    assert result['seconds'] == 150 and result['total'] == 1
    assert result['records'][0]['user_name'] == 'Viewer'
    cells = {d['date']: d for d in result['days']}
    assert cells['2026-10-15']['seconds'] == 30 and cells['2026-10-16']['seconds'] == 60
    assert get_report()['seconds'] == 150
    assert all(r.headers['X-Emby-Token'] == 'key' for r in requests)
    assert 'SELECT' in requests[0].content.decode() and 'DELETE' not in requests[0].content.decode()


def test_zero_duration_counts_and_user_identity_normalization(plugin):
    plugin[0].extend([
        ['1', '2026-10-15T10:00:00Z', 'user1', '1', '', '', '', '0'],
        ['2', '2026-10-15T10:00:00Z', 'other', '2', '', '', '', '9'],
    ])
    result = get_report(user_id='USER-1')
    assert result['plays'] == 1 and result['seconds'] == 0 and result['total'] == 1


def test_configured_user_cannot_be_overridden(plugin):
    plugin[0].append(['1', '2026-10-15 10:00:00', 'other', '1', '', '', '', '60'])
    result = playbackreporting.plugin_report(Settings(emby_url='http://emby.test', emby_api_key='key', emby_user_id='fixed'), '2026-10', timezone.utc, user_id='other')
    assert result['plays'] == 0


def test_invalid_duration_does_not_return_partial_statistics(plugin):
    plugin[0].append(['1', '2026-10-15 10:00:00', 'user1', '1', '', '', '', 'nan'])
    with pytest.raises(ValueError):
        get_report()


def test_plugin_failure_falls_back_with_explicit_source(state, monkeypatch):
    state[0].playback_reporting_enabled = True
    def fail(*args):
        raise ValueError('plugin unavailable')
    monkeypatch.setattr(playbackreporting, 'plugin_report', fail)
    result = playback.report('2026-10')
    assert result['source'] == 'local' and result['source_error'] == 'plugin unavailable'


def test_plugin_result_is_not_added_to_local_statistics(state, monkeypatch):
    state[0].playback_reporting_enabled = True
    monkeypatch.setattr(playbackreporting, 'plugin_report', lambda *args: {'source': 'playback_reporting', 'plays': 7})
    assert playback.report('2026-10') == {'source': 'playback_reporting', 'plays': 7}
