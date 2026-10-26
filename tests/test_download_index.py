"""Persistent cache freshness, atomic snapshots and downloader event ordering."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.models import Cache, TorrentFile, TorrentManifest
from app.services import downloadindex as index


class Client:
    url = 'http://downloader.test'

    def __init__(self):
        self.rows = [{'hash': 'one', 'name': 'film', 'save_path': '/downloads', 'progress': 1, 'state': 'seeding'}]
        self.files = {'one': ['/downloads/film.mp4']}
        self.reads = []
        self.polls = 0
        self.failed = False

    def list_downloads(self):
        self.polls += 1
        if self.failed:
            raise RuntimeError('offline')
        return [dict(row) for row in self.rows]

    def iter_torrent_file_pairs(self, hashes, strict=False):
        self.reads.append(list(hashes))
        for value in hashes:
            for path in self.files.get(value, []):
                yield path, value
        if self.failed:
            raise RuntimeError('interrupted manifest')


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'index.db'))
    for model in (Cache, TorrentManifest, TorrentFile):
        model.__table__.create(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    @contextmanager
    def scope():
        with maker() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
    monkeypatch.setattr(index, 'session_scope', scope)
    monkeypatch.setattr(index, 'get_settings', lambda: SimpleNamespace(download_index_interval=5,
        download_index_batch_size=1))
    yield scope
    engine.dispose()


def manifest(db, client, value='one', name='transmission'):
    with db() as session:
        return session.get(TorrentManifest, (index.connection(name, client), value))


def test_recent_cache_survives_new_client_and_does_not_call_rpc(db):
    client = Client()
    assert list(index.file_pairs('transmission', ['ONE'], client)) == [('/downloads/film.mp4', 'one')]
    assert client.polls == 1 and client.reads == [['one']]
    restored = Client()
    restored.failed = True
    assert list(index.file_pairs('transmission', ['one'], restored)) == [('/downloads/film.mp4', 'one')]
    assert restored.polls == 0 and not restored.reads
    assert index.current_hashes('transmission', restored) == ['one']
    assert list(index.file_pairs('transmission', ['one'], restored, paths={'/DOWNLOADS/FILM.MP4'})) == [('/downloads/film.mp4', 'one')]
    assert list(index.file_pairs('transmission', ['one'], restored, paths={'/downloads/film2.mp4'})) == []


def test_move_invalidates_files_and_failed_refresh_preserves_prior_snapshot(db):
    client = Client()
    list(index.file_pairs('transmission', ['one'], client))
    client.rows[0]['save_path'] = '/moved'
    index.refresh_snapshot('transmission', client)
    assert manifest(db, client).checked_at is None
    client.failed = True
    with pytest.raises(RuntimeError):
        list(index.file_pairs('transmission', ['one'], client))
    with db() as session:
        assert session.scalars(select(TorrentFile.path)).all() == ['/downloads/film.mp4']
    row = manifest(db, client)
    assert row.error and row.retry_at and row.checked_at is None
    assert not index._fresh(row)


def test_only_complete_polls_mark_missing_and_removed_files_are_not_used(db):
    client = Client()
    list(index.file_pairs('transmission', ['one'], client))
    client.failed = True
    with pytest.raises(RuntimeError):
        index.refresh_snapshot('transmission', client)
    assert manifest(db, client).present
    client.failed = False
    client.rows = []
    index.refresh_snapshot('transmission', client)
    assert not manifest(db, client).present
    assert list(index.file_pairs('transmission', ['one'], client)) == []
    with db() as session:
        assert session.scalars(select(TorrentFile.path)).all() == ['/downloads/film.mp4']


def test_delete_tombstone_cannot_be_undone_by_older_poll(db):
    client = Client()
    started = datetime.now() - timedelta(seconds=30)
    index.deleted('transmission', ['one'], client)
    index.remember('transmission', client.rows, client=client, full=True, started=started)
    assert not manifest(db, client).present
    index.refresh_snapshot('transmission', client)
    assert manifest(db, client).present


def test_refresh_batches_rotate_past_fresh_and_failed_entries(db, monkeypatch):
    from app.modules import downloadclient
    client = Client()
    client.rows += [dict(client.rows[0], hash='two'), dict(client.rows[0], hash='three')]
    client.files.update(two=['/downloads/two.mp4'], three=['/downloads/three.mp4'])
    monkeypatch.setattr(downloadclient, 'list_configured_clients', lambda: ['transmission'])
    monkeypatch.setattr(downloadclient, 'get_download_client', lambda name: client)
    assert index.sync() == 1
    assert index.sync() == 1
    assert index.sync() == 1
    assert index.sync() == 0
    assert {v[0] for v in client.reads} == {'one', 'two', 'three'}


def test_connection_change_never_reuses_another_downloaders_files(db):
    client = Client()
    list(index.file_pairs('transmission', ['one'], client))
    client.url = 'http://another.test'
    client.files = {'one': ['/different/film.mp4']}
    assert list(index.file_pairs('transmission', ['one'], client)) == [('/different/film.mp4', 'one')]
    assert client.polls == 2


def test_active_manifest_expires_earlier_than_completed(db):
    client = Client()
    list(index.file_pairs('transmission', ['one'], client))
    with db() as session:
        row = session.get(TorrentManifest, (index.connection('transmission', client), 'one'))
        row.checked_at = datetime.now() - timedelta(hours=1)
    assert index._fresh(manifest(db, client))
    client.rows[0]['progress'] = .5
    index.refresh_snapshot('transmission', client)
    assert not index._fresh(manifest(db, client))


def test_confirmed_delete_hook_updates_db_and_rpc_failure_does_not(db):
    from app.modules.downloadclient.qbittorrent import QBitTorrentClient
    qb = QBitTorrentClient(url=Client.url)
    index.remember('qbittorrent', Client().rows, client=qb)
    def fail(**kwargs):
        raise RuntimeError('delete failed')
    qb.client = SimpleNamespace(torrents_info=lambda **kw: [SimpleNamespace(hash='one')], torrents_delete=fail)
    assert qb.delete_torrent(['one']) == []
    assert manifest(db, qb, name='qbittorrent').present
    qb.client.torrents_delete = lambda **kw: None
    assert qb.delete_torrent(['one']) == ['one']
    assert not manifest(db, qb, name='qbittorrent').present


def test_transfer_target_added_and_index_failure_does_not_remove_source(db):
    target = Client()
    source = Client()
    index.remember('qbittorrent', source.rows, client=source)
    index.after_transfer('one', 'film', '/downloads', target)
    assert manifest(db, target).present
    assert manifest(db, target).checked_at is not None
    assert manifest(db, source, name='qbittorrent').present
    index.after_delete('qbittorrent', ['one'], source)
    assert not manifest(db, source, name='qbittorrent').present
    assert manifest(db, target).present


def test_partial_sync_failure_cools_down_and_does_not_starve_following_torrents(db, monkeypatch):
    from app.modules import downloadclient
    client = Client()
    client.rows.append(dict(client.rows[0], hash='two'))
    client.files['two'] = ['/downloads/two.mp4']
    original = client.iter_torrent_file_pairs
    def read(hashes, strict=False):
        if hashes == ['one']:
            raise RuntimeError('unavailable')
        yield from original(hashes, strict=strict)
    client.iter_torrent_file_pairs = read
    monkeypatch.setattr(downloadclient, 'list_configured_clients', lambda: ['transmission'])
    monkeypatch.setattr(downloadclient, 'get_download_client', lambda name: client)
    with pytest.raises(RuntimeError, match='部分更新失败'):
        index.sync()
    assert manifest(db, client).retry_at > datetime.now()
    assert index.sync() == 1
    assert manifest(db, client, 'two').checked_at is not None
