import tracemalloc

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.utils import log


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(log, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(log, 'LOG_FILE', tmp_path / 'cinefold.log')
    return tmp_path


def test_archives_are_selectable_and_filtered_before_tail(logs):
    name = 'cinefold.2026-10-22_00-00-00_235068.log'
    (logs / name).write_text('2026-10-22 | WARNING | ADN-737-C removed\n'
                             + '2026-10-22 | INFO | refresh\n' * 600, encoding='utf-8')
    log.LOG_FILE.write_text('2026-10-23 | INFO | current\n', encoding='utf-8')
    (logs / 'private.env').write_text('secret', encoding='utf-8')
    assert [row['name'] for row in log.list_log_files()] == ['cinefold.log', name]
    assert log.read_logs(1, 'adn-737', name, 'WARNING') == ['2026-10-22 | WARNING | ADN-737-C removed']
    assert log.read_logs() == ['2026-10-23 | INFO | current']


@pytest.mark.parametrize('name', ['../cinefold.log', '/tmp/cinefold.log', r'..\cinefold.log', 'private.env', 'cinefold.log/../../private.env'])
def test_archive_paths_cannot_escape_log_directory(logs, name):
    with pytest.raises(ValueError):
        log.read_logs(filename=name)


def test_missing_and_invalid_archive_filters_are_explicit(logs):
    assert log.read_logs() == []
    with pytest.raises(FileNotFoundError):
        log.read_logs(filename='cinefold.2026-10-22_00-00-00_1.log')
    with pytest.raises(ValueError):
        log.read_logs(level='anything')


def test_reading_large_log_keeps_only_tail_in_memory(logs):
    with log.LOG_FILE.open('w', encoding='utf-8') as stream:
        for number in range(30000):
            stream.write(f'{number} | INFO | ' + 'x' * 160 + '\n')
    tracemalloc.start()
    try:
        rows = log.read_logs(30)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(rows) == 30 and rows[-1].startswith('29999 ')
    assert peak < 512000, 'reading a multi-MB log must not load the whole file'


def test_logs_api_requires_auth_and_reads_selected_archive(logs):
    from app.api.endpoints import get_current_user
    from app.api.endpoints.config import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get('/logs/files').status_code == 401
    assert client.get('/logs').status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'test'
    name = 'cinefold.2026-10-22_00-00-00_1.log'
    (logs / name).write_text('2026-10-22 | WARNING | removed\n', encoding='utf-8')
    assert client.get('/logs/files').json()['data']['files'][0]['name'] == name
    assert client.get('/logs', params={'filename': name, 'level': 'WARNING'}).json()['data']['logs'] == ['2026-10-22 | WARNING | removed']
    assert client.get('/logs', params={'filename': '../private.env'}).json()['code'] == 400
    (logs / name).unlink()
    assert client.get('/logs', params={'filename': name}).json()['code'] == 404


def test_agent_can_list_and_read_archived_logs(logs):
    from app.modules.agent.tools import tool_read_logs
    name = 'cinefold.2026-10-22_00-00-00_1.log'
    (logs / name).write_text('2026-10-22 | WARNING | ADN-737-C\n', encoding='utf-8')
    assert tool_read_logs({'list_files': True})['files'][0]['name'] == name
    assert tool_read_logs({'filename': name, 'keyword': 'ADN-737'})['returned'] == 1
