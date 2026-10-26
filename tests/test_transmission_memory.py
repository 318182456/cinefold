"""Bound RPC responses and stream exact file ownership without changing downloads."""
from types import SimpleNamespace
from pathlib import PurePath

import pytest
from transmission_rpc.torrent import Torrent

from app.modules.downloadclient.transmission import TransmissionClient
from app.services import orphan


def test_file_listing_batches_only_required_fields_and_preserves_paths():
    calls = []

    def get_torrents(*, ids, arguments):
        calls.append((list(ids), set(arguments)))
        return [Torrent(fields={'id': h, 'hashString': str(h), 'downloadDir': '/downloads',
                                'files': [{'name': f'collection/{h}.mp4', 'length': 100,
                                           'bytesCompleted': 0}]}) for h in ids]

    client = TransmissionClient()
    client.client = SimpleNamespace(get_torrents=get_torrents)
    paths = client.list_torrent_files(list(range(1, 301)))
    assert paths == [str(PurePath('/downloads') / f'collection/{h}.mp4') for h in range(1, 301)]
    assert [len(ids) for ids, fields in calls] == [128, 128, 44]
    assert all(fields == {'downloadDir', 'files'} for ids, fields in calls)


def test_file_iterator_is_lazy_and_empty_input_never_lists_all_torrents():
    calls = []
    client = TransmissionClient()
    client.client = SimpleNamespace(get_torrents=lambda **kw: calls.append(kw) or [])
    iterator = client.iter_torrent_files(['one'])
    assert not calls
    assert list(iterator) == []
    assert len(calls) == 1
    calls.clear()
    assert client.list_torrent_files(['', None]) == []
    assert not calls


def test_list_wrapper_discards_partial_result_on_rpc_failure():
    calls = []

    def get_torrents(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise RuntimeError('connection failed')
        return [Torrent(fields={'id': 1, 'hashString': 'one', 'downloadDir': '/downloads',
                                'files': [{'name': 'one.mp4', 'length': 100, 'bytesCompleted': 0}]})]

    client = TransmissionClient()
    client.client = SimpleNamespace(get_torrents=get_torrents)
    assert client.list_torrent_files([str(h) for h in range(129)]) == []


def test_factory_only_reads_current_history_hashes(monkeypatch):
    from app.modules import downloadclient
    calls = []
    def pairs(hashes):
        calls.append(hashes)
        yield '/downloads/known.mp4', hashes[0]
    client = SimpleNamespace(monitor_torrent=lambda **kw: [{'hash': 'KNOWN'}, {'hash': 'unrelated'}],
                             iter_torrent_file_pairs=pairs)
    monkeypatch.setattr(downloadclient, 'list_configured_clients', lambda: ['transmission'])
    monkeypatch.setattr(downloadclient, 'get_download_client', lambda name: client)
    assert list(downloadclient.iter_torrent_file_pairs(['known', 'removed'])) == [('/downloads/known.mp4', 'KNOWN')]
    assert calls == [['KNOWN']]
    calls.clear()
    assert list(downloadclient.iter_torrent_file_pairs(['removed'])) == []
    assert list(downloadclient.iter_torrent_file_pairs([])) == []
    assert not calls


@pytest.mark.parametrize('fail', [False, True])
def test_orphan_stream_keeps_only_exact_candidate_matches(monkeypatch, fail):
    def files(hashes):
        assert hashes == ['new-hash']
        for index in range(10000):
            yield f'/downloads/unrelated/{index}.mp4'
        yield '/downloads/RELATED.mp4'
        if fail:
            raise RuntimeError('incomplete snapshot')

    def no_full_list(*args):
        pytest.fail('streaming client must not materialize its full file list')

    client = SimpleNamespace(monitor_torrent=lambda hashes=None: [{'hash': 'new-hash'}],
                             iter_torrent_files=files, list_torrent_files=no_full_list)
    monkeypatch.setattr('app.modules.downloadclient.list_configured_clients', lambda: ['transmission'])
    monkeypatch.setattr('app.modules.downloadclient.get_download_client', lambda name: client)
    wanted = orphan._norm('/downloads/related.mp4')
    view = orphan._torrent_view({wanted, orphan._norm('/downloads/related-but-absent.mp4')})
    assert view.files == (set() if fail else {wanted})
    assert view.hashes == {'new-hash'} and view.answered
