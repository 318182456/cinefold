import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import Cache, Code, DeletionRecord, History, MediaLink
from app.services import deletionhistory, medialink
from tests.test_scrape_reliability import db


@pytest.fixture
def case(db, tmp_path, monkeypatch):
    db.configure(autoflush=False)
    source = tmp_path / 'download' / 'SONE-725.mkv'
    link = tmp_path / 'library' / 'SONE-725' / 'SONE-725.mkv'
    source.parent.mkdir(parents=True); link.parent.mkdir(parents=True)
    source.write_bytes(b'original-video')
    os.link(source, link)
    settings = Settings(medialink_library_path=str(tmp_path / 'library'), medialink_delete_enabled=True)
    monkeypatch.setattr(medialink, 'get_settings', lambda: settings)
    monkeypatch.setattr(medialink, '_torrent_hashes_by_path', lambda *a: [])
    monkeypatch.setattr(medialink, '_plan_torrent_deletion', lambda hashes, paths: (list(hashes), [], {}))
    monkeypatch.setattr('app.modules.downloadclient.list_configured_clients', lambda: [])
    with db.begin() as session:
        session.add(Code(code='SONE-725', title='Original title', status=4))
        session.add(Cache(namespace='media', key='SONE-725', content='1'))
        session.add(History(hash='a'*40, code='SONE-725', save_path=str(source), site='PT'))
        session.add(MediaLink(link_path=str(link), source_path=str(source), code='SONE-725'))
    return db, source, link, settings


def records(db):
    with db() as session:
        return list(session.scalars(select(DeletionRecord).order_by(DeletionRecord.created_at)))


def test_delete_keeps_snapshot_then_resubscribe_and_delete_again(case, monkeypatch):
    db, source, link, _ = case
    first = medialink.handle_media_deleted(link_path=str(link))
    assert not first.errors and not source.exists() and not link.exists()
    audit = records(db)[0]
    assert audit.status == 'completed' and audit.snapshot['downloads'][0]['hash'] == 'a'*40
    assert audit.snapshot['links'][0]['source_path'] == str(source)
    assert audit.title == 'Original title' and str(source) in audit.result['files_deleted']
    with db() as session:
        assert session.get(History, 'a'*40) is None
        assert session.get(MediaLink, str(link)) is None
        assert session.scalar(select(Cache).where(Cache.namespace == 'media', Cache.key == 'SONE-725')) is None
    from app import services
    monkeypatch.setattr(services, 'is_filtered_code', lambda *a: '')
    monkeypatch.setattr(services, 'send_subscribe_message', lambda *a: None)
    assert services.subscribe_code('SONE-725')
    with db() as session:
        assert session.get(Code, 'SONE-725').status == 1
    # 同一番号重新下载、再次删除，应形成第二次历史，不能覆盖第一次。
    source.parent.mkdir(parents=True, exist_ok=True); link.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'new-video'); os.link(source, link)
    with db.begin() as session:
        session.add(History(hash='b'*40, code='SONE-725', save_path=str(source)))
        session.add(MediaLink(link_path=str(link), source_path=str(source), code='SONE-725'))
    assert not medialink.handle_media_deleted(link_path=str(link)).errors
    audits = records(db)
    assert len(audits) == 2 and audits[0].id != audits[1].id
    assert [row.snapshot['downloads'][0]['hash'] for row in audits] == ['a'*40, 'b'*40]


def test_dry_run_disabled_and_ignored_callback_do_not_claim_real_deletion(case):
    db, source, link, settings = case
    assert medialink.handle_media_deleted(link_path=str(link), dry_run=True).dry_run
    settings.medialink_delete_enabled = False
    assert medialink.handle_media_deleted(link_path=str(link)).dry_run
    assert medialink.handle_emby_deleted(str(link))['ignored']
    assert records(db) == [] and source.exists() and link.exists()


def test_snapshot_failure_prevents_destructive_work(case, monkeypatch):
    _, source, link, _ = case
    def fail(*a, **kw):
        raise RuntimeError('journal unavailable')
    monkeypatch.setattr(deletionhistory, 'new_record', fail)
    with pytest.raises(RuntimeError, match='journal unavailable'):
        medialink.handle_media_deleted(link_path=str(link))
    assert source.exists() and link.exists()


def test_unexpected_failure_retains_pre_delete_snapshot(case, monkeypatch):
    db, source, link, _ = case
    def fail():
        raise RuntimeError('downloader failure')
    monkeypatch.setattr('app.modules.downloadclient.list_configured_clients', fail)
    with pytest.raises(RuntimeError):
        medialink.handle_media_deleted(link_path=str(link))
    audit = records(db)[0]
    assert audit.status == 'failed' and audit.error == 'RuntimeError'
    assert audit.snapshot['downloads'][0]['hash'] == 'a'*40
    assert source.exists() and link.exists()


def test_partial_file_failure_is_preserved(case, monkeypatch):
    db, source, link, _ = case
    original = medialink._delete_file
    def remove(path, result, **kw):
        if path == str(source):
            result.errors.append('source permission denied')
        else:
            original(path, result, **kw)
    monkeypatch.setattr(medialink, '_delete_file', remove)
    result = medialink.handle_media_deleted(link_path=str(link))
    assert result.errors and source.exists()
    audit = records(db)[0]
    assert audit.status == 'partial' and 'source permission denied' in audit.result['errors']


@pytest.fixture
def client():
    from app.api.endpoints import get_current_user
    from app.api.endpoints.medialink import router
    app = FastAPI(); app.include_router(router)
    client = TestClient(app)
    assert client.get('/medialinks/deletion-history').status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: 'tester'
    return client


def test_record_only_delete_is_atomic_audited_and_file_preserved(case, client):
    db, source, link, _ = case
    response = client.delete('/medialinks/record', params={'link_path': str(link)})
    assert response.json()['code'] == 200 and source.exists() and link.exists()
    audit = records(db)[0]
    assert audit.action == 'record_remove' and audit.result['files_deleted'] == []
    listing = client.get('/medialinks/deletion-history', params={'keyword': 'SONE-725'}).json()['data']
    assert listing['total'] == 1 and 'snapshot' not in listing['items'][0]
    detail = client.get(f'/medialinks/deletion-history/{audit.id}').json()['data']
    assert detail['snapshot']['downloads'][0]['hash'] == 'a'*40
    assert client.get('/medialinks/deletion-history', params={'keyword': '%'}).json()['data']['total'] == 0
    assert client.get('/medialinks/deletion-history', params={'page': 0}).status_code == 422
    assert client.get('/medialinks/deletion-history/missing').status_code == 404


def test_batch_record_delete_preserves_each_snapshot(case, client):
    db, source, link, _ = case
    response = client.post('/medialinks/batch-record', json={'link_paths': [str(link), 'missing']})
    assert response.json()['data']['removed'] == [str(link)]
    assert source.exists() and link.exists() and len(records(db)) == 1


def test_prior_process_running_is_interrupted_without_replaying(case, client):
    db, source, link, _ = case
    with db.begin() as session:
        links = list(session.scalars(select(MediaLink)))
        row = deletionhistory.new_record(session, links)
        row.owner = 'old-process'
    result = client.get('/medialinks/deletion-history', params={'status': 'interrupted'}).json()['data']
    assert result['total'] == 1 and result['items'][0]['status'] == 'interrupted'
    assert source.exists() and link.exists()
