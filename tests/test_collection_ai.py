import json
from types import SimpleNamespace

import httpx
import pytest

from app.modules.collections import collectionai as ai

MOVIES = [{'code': 'ABC-001', 'title': 'one'}, {'code': 'ABC-002', 'title': 'two'},
          {'code': 'ABC-001-C', 'title': 'subtitle version'}]


def test_ai_cannot_introduce_unknown_movies_or_count_versions_as_sequels():
    value = {'groups': [
        {'name': 'unknown', 'kind': 'theme', 'codes': ['ABC-001', 'UNKNOWN-999']},
        {'name': 'versions', 'kind': 'sequel', 'codes': ['ABC-001', 'ABC-001-C']},
        {'name': 'valid', 'kind': 'series', 'codes': ['ABC-001', 'ABC-002', 'UNKNOWN-999']},
    ]}
    groups = ai.parse_groups(json.dumps(value), MOVIES)
    assert len(groups) == 1 and groups[0]['codes'] == ['ABC-001', 'ABC-002']


def test_multi_kind_memberships_are_preserved_and_mode_is_respected():
    value = {'groups': [{'name': kind, 'kind': kind, 'codes': ['ABC-001', 'ABC-002'],
                         'confidence': 'NaN'} for kind in sorted(ai.KINDS)]}
    raw = '```json\n' + json.dumps(value) + '\n```'
    groups = ai.parse_groups(raw, MOVIES)
    assert len(groups) == 5 and all(row['confidence'] == 0 for row in groups)
    assert [row['kind'] for row in ai.parse_groups(raw, MOVIES, 'actor')] == ['actor']


@pytest.mark.parametrize('raw', ['not json', '[]', '{}', '{"groups":null}'])
def test_bad_ai_json_fails_without_manufacturing_groups(raw):
    with pytest.raises(ValueError):
        ai.parse_groups(raw, MOVIES)


def test_only_movie_metadata_is_sent_to_configured_ai(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, 'ReviewAI', lambda: SimpleNamespace(
        enabled=True, url='https://ai.invalid/v1', model='model', api_key='test-key', proxy=None))

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'groups': [
            {'name': 'same style', 'kind': 'style', 'reason': 'shared tags', 'codes': ['ABC-001', 'ABC-002']},
        ]})}}]})

    monkeypatch.setattr(ai, 'http_client', lambda **kw: httpx.Client(transport=httpx.MockTransport(handle)))
    data = [{**row, 'source_path': '/private/movie.mp4', 'item_id': 'private-emby-id',
             'notes': 'private note', 'favorite': True} for row in MOVIES[:2]]
    assert len(ai.suggest(data)) == 1
    sent = json.loads(calls[0].content)
    assert '/private' not in calls[0].content.decode() and 'private note' not in calls[0].content.decode()
    assert 'favorite' not in sent['messages'][1]['content']
    assert calls[0].url.path == '/v1/chat/completions'
    assert calls[0].headers['Authorization'] == 'Bearer test-key'
