import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import Code, DownloadRecord, History
from app.services import downloads, seedtransfer
from tests.test_scrape_reliability import db
from tests.test_seedtransfer import FakeQB, FakeTR, _detail

A, B = 'a' * 40, 'b' * 40


@pytest.fixture
def managed(db, monkeypatch):
    db.configure(autoflush=False)
    monkeypatch.setattr(downloads, '_LIVE', None)
    monkeypatch.setattr(downloads, '_STATE', None)
    monkeypatch.setattr(downloads, 'get_settings', lambda: Settings(qbittorrent_url='http://qb', transmission_url='http://tr'))
    monkeypatch.setattr(downloads, 'list_configured_clients', lambda: ['qbittorrent', 'transmission'])
    with db.begin() as session:
        session.add_all([Code(code='ABC-001', status=2), History(hash=A, code='ABC-001', site='PT')])
    return db


def test_parallel_snapshot_all_categories_cache_and_persisted_history(managed, monkeypatch):
    reached = {name: threading.Event() for name in ('qbittorrent', 'transmission')}
    calls = []
    def read(name):
        calls.append(name); reached[name].set()
        assert all(event.wait(2) for event in reached.values()), 'clients must be read concurrently'
        return [{'hash': A if name == 'qbittorrent' else B, 'name': '测试影片', 'progress': .4,
                 'state': 'downloading', 'save_path': '/downloads', 'download_speed': 4096,
                 'upload_speed': 2048, 'seeds_total': 12}]
    monkeypatch.setattr(downloads, '_read', read)
    result = downloads.live(code_status=2)
    assert result['total'] == 1 and result['items'][0]['code'] == 'ABC-001'
    assert result['items'][0]['seeds_total'] == 12 and result['items'][0]['save_path'] == '/downloads'
    assert result['counts']['all'] == 1 and result['download_speed'] == 4096
    assert downloads.live()['counts']['all'] == 2
    assert downloads.live(client='transmission')['total'] == 1
    assert len(calls) == 2, 'filters must reuse the snapshot, not poll every downloader again'
    with managed() as session:
        records = session.scalars(select(DownloadRecord)).all()
        assert len(records) == 2
        assert all('download_speed' not in record.details for record in records)
    monkeypatch.setattr(downloads, '_LIVE', None)
    monkeypatch.setattr(downloads, '_read', lambda name: [])
    assert downloads.live()['total'] == 0
    history = downloads.history(code_status=2)
    assert history['total'] == 1 and history['items'][0]['name'] == '测试影片'
    assert downloads.history()['total'] == 2, 'removing a torrent must not erase its recorded history'


def test_disconnection_keeps_explicitly_stale_snapshot_and_hides_stale_speed(managed, monkeypatch):
    monkeypatch.setattr(downloads, '_read', lambda name: [{'hash': A, 'name': name, 'progress': .5, 'state': 'downloading', 'download_speed': 123}])
    downloads.live()
    downloads._LIVE['updated_at'] = 0
    monkeypatch.setattr(downloads, '_read', lambda name: (_ for _ in ()).throw(ValueError('secret password must not leak')))
    result = downloads.live()
    assert result['total'] == 2 and len(result['errors']) == 2
    assert all(row['stale'] for row in result['items']) and result['download_speed'] == 0
    assert 'secret' not in str(result)


def test_existing_legacy_history_is_read_without_contacting_downloader(managed):
    result = downloads.history()
    assert result['total'] == 1
    row = result['items'][0]
    assert row['hash'] == A and row['code_status'] == 2 and row['client'] == ''
    assert 'progress' not in row, 'a pushed torrent is not evidence that it finished'


def wait_transfer():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with downloads._LOCK:
            state = downloads.transfer_current()
        if not state['running']:
            return state
        time.sleep(.01)
    pytest.fail('transfer worker did not finish')


def test_transfer_checkpoint_retry_preserves_successes_and_source_files(managed, monkeypatch):
    qb, tr = FakeQB(details={A: _detail(A), B: _detail(B)}), FakeTR()
    monkeypatch.setattr(seedtransfer, '_clients', lambda: (qb, tr))
    monkeypatch.setattr(seedtransfer, 'is_available', lambda: (True, ''))
    original = qb.export_torrent
    qb.export_torrent = lambda value: original(value) if value == A else None
    downloads.start_transfer([A, B])
    result = wait_transfer()
    assert result['phase'] == 'partial'
    assert [row['status'] for row in result['results']] == ['succeeded', 'failed']
    assert not qb.deleted, 'manual transfer defaults to preserving the qB task'
    with downloads._LOCK:
        downloads._STATE['running'] = True  # durable checkpoint left by an interrupted process
        downloads._save()
        downloads._STATE = None
    assert downloads.transfer_current()['phase'] == 'interrupted'
    qb.export_torrent = original
    downloads.start_transfer(resume=True)
    resumed = wait_transfer()
    assert resumed['phase'] == 'completed'
    assert qb.exported.count(A) == 1 and qb.exported.count(B) == 1
    assert len(downloads.transfer_history()) == 1 and not qb.deleted
    # Explicit removal still reuses the existing guarantee that delete_files is always false.
    downloads.start_transfer([A], delete_source=True)
    assert wait_transfer()['phase'] == 'completed'
    assert qb.deleted == [([A], False)]


def test_duplicate_start_rejected_and_transfer_api_validates_hashes(managed, monkeypatch):
    from app.api.endpoints.downloads import router
    from app.api.endpoints import get_current_user
    app = FastAPI(); app.include_router(router)
    client = TestClient(app)
    assert client.get('/downloads/history').status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: 'test'
    assert client.post('/downloads/transfer', json={'hashes': ['not-a-hash']}).status_code == 422
    assert client.get('/downloads/history').json()['data']['total'] == 1
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(seedtransfer, 'is_available', lambda: (True, ''))
    def transfer(*args, **kwargs):
        entered.set(); assert release.wait(3)
        return seedtransfer.TransferResult(transferred=[A])
    monkeypatch.setattr(seedtransfer, 'transfer_hashes', transfer)
    try:
        assert client.post('/downloads/transfer', json={'hashes': [A]}).status_code == 200
        assert entered.wait(1)
        assert client.post('/downloads/transfer', json={'hashes': [B]}).status_code == 409
    finally:
        release.set(); wait_transfer()


def test_native_clients_return_metrics_without_files_or_tracker_secrets(monkeypatch):
    from app.modules.downloadclient.qbittorrent import QBitTorrentClient
    from app.modules.downloadclient.transmission import TransmissionClient
    from transmission_rpc.torrent import Torrent
    qb = QBitTorrentClient()
    calls = []
    qb.client = SimpleNamespace(torrents_info=lambda **kwargs: (calls.append(kwargs) or [SimpleNamespace(
        hash=A, name='test', progress=.5, state='downloading', save_path='/qb', dlspeed=1024, num_complete=7)]))
    row = qb.list_downloads()[0]
    assert calls == [{}] and row['download_speed'] == 1024 and row['seeds_total'] == 7
    tr = TransmissionClient()
    fields = dict(id=1, hashString=B, name='test', percentDone=1.0, status=6, downloadDir='/tr',
                  totalSize=1024, rateDownload=0, rateUpload=23, peersConnected=2,
                  trackerStats=[{'seederCount': 12, 'leecherCount': 3, 'announce': 'https://tracker/secret-passkey'}],
                  downloadedEver=1024, uploadedEver=2048, uploadRatio=2, eta=-1, addedDate=100, doneDate=200, error=0)
    tr.client = SimpleNamespace(get_torrents=lambda **kwargs: (calls.append(kwargs) or [Torrent(fields=fields)]))
    row = tr.list_downloads()[0]
    assert row['seeds_total'] == 12 and row['seeds_connected'] is None and row['upload_speed'] == 23
    assert 'secret-passkey' not in str(row) and 'files' not in calls[-1]['arguments']


def test_retry_reconciles_qb_source_already_removed_before_checkpoint(managed, monkeypatch):
    qb, tr = FakeQB(), FakeTR()
    tr.monitor_torrent = lambda hashes: [{'hash': A, 'completed': True, 'progress': 1, 'state': 'seeding'}]
    monkeypatch.setattr(seedtransfer, '_clients', lambda: (qb, tr))
    monkeypatch.setattr(seedtransfer, 'is_available', lambda: (True, ''))
    result = seedtransfer.transfer_hashes([A], delete_source=True)
    assert result.transferred == [A] and not result.failed
    assert not qb.deleted and not tr.added


@pytest.mark.parametrize('code,kind', [(1, 'tracker_warning'), (2, 'tracker_error'), (3, 'local_error')])
def test_transmission_error_reason_is_classified_and_redacted(code, kind):
    from app.modules.downloadclient.transmission import _error_details
    data = _error_details({'error': code, 'errorString': 'tracker rejected https://user:secret@tracker/secret-passkey?token=hidden passkey=private'})
    assert data['error_kind'] == kind and data['error_code'] == code
    assert 'rejected' in data['error_message']
    assert not any(value in str(data) for value in ['secret', 'hidden', 'private'])
    assert _error_details({'error': 0, 'errorString': 'obsolete error'})['error_message'] == ''


def test_error_reason_survives_history_and_page_edits_do_not_change_snapshot(managed, monkeypatch):
    monkeypatch.setattr(downloads, '_read', lambda name: [{'hash': A, 'name': 'test', 'progress': 1,
        'state': 'error', 'error_kind': 'tracker_error', 'error_message': 'unregistered torrent'}] if name == 'transmission' else [])
    result = downloads.live()
    result['items'][0]['error_message'] = 'changed by caller'
    assert downloads.live()['items'][0]['error_message'] == 'unregistered torrent'
    assert downloads.history()['items'][0]['error_kind'] == 'tracker_error'


def test_large_cached_download_page_does_not_copy_entire_snapshot(managed, monkeypatch):
    import hashlib
    import json
    import tracemalloc
    signature = downloads._signature(downloads.get_settings(), ['qbittorrent', 'transmission'])
    rows = [{'hash': str(i), 'name': f'video-{i}', 'client': 'transmission', 'group': 'seeding',
             'state': 'seeding', 'added_at': i, 'progress': 1, 'stale': False,
             'details': {'values': list(range(30))}} for i in range(7000)]
    monkeypatch.setattr(downloads, '_LIVE', {'signature': signature, 'updated_at': time.time(),
        'items': rows, 'errors': [], 'clients': ['transmission'], 'answered': ['transmission']})
    tracemalloc.start()
    try:
        result = downloads.live(size=30)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result['total'] == 7000 and len(result['items']) == 30
    assert peak < 1_500_000, 'a page must not deep-copy the entire cached torrent list'
    result['items'][0]['details']['values'].clear()
    assert len(rows[-1]['details']['values']) == 30
