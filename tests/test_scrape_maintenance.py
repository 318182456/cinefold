from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.services import scrapemaintenance as gate


@pytest.fixture(autouse=True)
def isolated_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, '_PROTECTION_FILE', tmp_path / 'protection.json')
    monkeypatch.setattr(gate, '_ACTIVE', 0)
    monkeypatch.setattr(gate, '_BUSY', False)


def test_exclusive_rebuild_cannot_overlap_existing_writer():
    entered, stop = Event(), Event()

    def write():
        with gate.activity():
            entered.set()
            stop.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(write)
        try:
            assert entered.wait(1)
            with pytest.raises(ValueError):
                gate.reserve()
        finally:
            stop.set()
        task.result(timeout=2)
    gate.reserve()
    try:
        with pytest.raises(gate.MaintenanceBusy):
            with gate.activity():
                pytest.fail('ordinary writers must be excluded')
        with gate.internal(), gate.activity():
            assert gate.busy()
    finally:
        gate.release()
    assert gate._ACTIVE == 0


def test_deletion_protection_survives_end_of_maintenance(tmp_path):
    output = tmp_path / 'library'
    gate.reserve()
    gate.protect(output)
    gate.release()
    assert gate.protects_delete(str(output / 'old-film.mp4'))
    assert gate.protects_delete('')
    assert not gate.protects_delete(str(tmp_path / 'unrelated' / 'film.mp4'))
    with pytest.raises(ValueError):
        gate.release_protection(str(tmp_path))
    assert gate.protection()['deletion_protected']
    gate.release_protection(str(output.resolve()))
    assert not gate.protection()['deletion_protected']


def test_corrupt_protection_fails_closed():
    gate._PROTECTION_FILE.write_text('{broken', encoding='utf-8')
    assert gate.protects_delete('/any/movie.mp4')
    with pytest.raises(ValueError):
        gate.protect('/new/library')


def test_delayed_emby_delete_never_reaches_source_deletion(monkeypatch, tmp_path):
    from app.services import medialink
    output = tmp_path / 'library'
    gate.protect(output)
    monkeypatch.setattr(medialink, '_handle_media_deleted', lambda *a: pytest.fail('must not delete'))
    result = medialink.handle_media_deleted(str(output / 'ABC-123.mp4'))
    assert result.errors and not result.files_deleted and not result.torrents_deleted


def test_rebuild_api_requires_auth_and_explicit_execution(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.api.endpoints import get_current_user
    from app.services import scraperebuild
    calls = []
    monkeypatch.setattr(scraperebuild, 'preview', lambda: {'phase': 'preview', 'running': True})
    monkeypatch.setattr(scraperebuild, 'start', lambda token, confirmation, **kw:
                        calls.append((token, confirmation, kw)) or {'running': True})
    app = create_app()
    client = TestClient(app)
    assert client.post('/api/v1/scrape/rebuild/preview').status_code == 401
    assert client.post('/api/v1/scrape/rebuild/run', json={
        'token': 't', 'confirmation': '/library'}).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    assert client.post('/api/v1/scrape/rebuild/preview').status_code == 200
    assert not calls
    assert client.post('/api/v1/scrape/rebuild/run', json={'token': 't'}).status_code == 422
    assert client.post('/api/v1/scrape/rebuild/run', json={
        'token': 't', 'confirmation': '/library', 'refresh_metadata': True}).status_code == 200
    assert calls == [('t', '/library', {'refresh_metadata': True})]
