import pytest

from app.database.models import EmbyCollectionLink
from app.services import embycollections as collections
from app.services.library import library_items
from tests.test_emby_collections_auto import case, db, wait


def test_catalog_and_library_filter_use_only_current_user_published_members(case, monkeypatch):
    factory, settings, server = case
    factory.configure(autoflush=False)
    collections.automatic('actor')
    assert wait()['phase'] == 'published'
    result = collections.catalog()['items']
    assert len(result) == 1 and result[0]['count'] == 3
    record = result[0]
    assert record['codes'] == ['ABP-101', 'ABP-102', 'ABP-103']
    assert '/web/index.html#!/item?id=collection-' in record['emby_url']
    with factory.begin() as session:
        row = session.get(EmbyCollectionLink, record['key'])
        row.member_ids = ['1', '2']
        session.add(EmbyCollectionLink(key='foreign', server=row.server, user_id='other', name='其他用户', remote_id='other', status='published', member_ids=['3']))
        session.add(EmbyCollectionLink(key='uncertain', server=row.server, user_id=row.user_id, name='尚未确认', remote_id='unknown', status='append_unknown', member_ids=['3']))
    assert collections.catalog()['items'][0]['count'] == 2
    assert len(collections.catalog()['items']) == 1
    assert {row['code'] for row in library_items(emby_collection_key=record['key'])['items']} == {'ABP-101', 'ABP-102'}
    assert library_items(emby_collection_key=record['key'], keyword='103')['total'] == 0
    with pytest.raises(ValueError, match='不属于'):
        library_items(emby_collection_key='foreign')
    settings.emby_user_id = 'different'
    assert collections.catalog()['items'] == []
    with pytest.raises(ValueError):
        library_items(emby_collection_key=record['key'])
    # Catalog is local/read-only, not a network scan or another publishing job.
    assert len(server.posts) == 1


def test_catalog_without_emby_configuration_is_empty(case):
    case[1].emby_api_key = ''
    assert collections.catalog() == {'items': []}
