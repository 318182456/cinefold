from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database.models import Code, DeletionRecord, History
from app.services.deletionlogimport import import_log_history, parse_logs
from tests.test_scrape_reliability import db


def line(time, message, function='_handle_media_deleted'):
    return f'2026-10-22 {time} | WARNING  | app.services.medialink:{function}:1371 - {message}\n'


def plan(code='ADN-737-C', *, dry=False, path='/downloads/ADN-737-C.mp4'):
    return f'[{code}] 媒体联动删除{"（演练）" if dry else ""} —— 删种 1 个: {"a"*40}; 保留做种 0 个: 无; 源文件 1 个: {path}; 硬链接 1 个: /library/ADN-737-C.mp4'


def done(code='ADN-737-C', errors=0):
    return f'[{code}] 联动删除完成 —— 种子 1，文件 2，刮削附属 5，目录 1，错误 {errors}'


def log(tmp_path, content, name='cinefold.log'):
    path = tmp_path / name
    path.write_text(content, encoding='utf-8')
    return path


def test_completed_event_restores_only_observed_paths_with_evidence(tmp_path):
    path = log(tmp_path, line('00:04:47', plan()) +
               line('00:04:47', '已删除文件: /downloads/ADN-737-C.mp4', '_delete_file') + line('00:04:48', done()))
    rows, summary = parse_logs([path])
    assert len(rows) == 1 and rows[0]['created_at'] == datetime(2026, 10, 21, 16, 4, 47)
    row = rows[0]
    assert row['source'] == 'log_import' and row['status'] == 'completed'
    assert row['result']['files_deleted'] == ['/downloads/ADN-737-C.mp4']
    assert 'links_deleted' not in row['result']  # 计划不能冒充实际删除结果。
    assert row['snapshot']['downloads'][0]['hash'] == 'a'*40
    assert row['snapshot']['provenance']['line'] == 1 and row['snapshot']['provenance']['finished_line'] == 3
    assert summary['unmatched'] == 0


def test_dry_disabled_ignored_and_unpaired_events_do_not_become_completed(tmp_path):
    path = log(tmp_path, line('10:00:00', plan(dry=True)) + line('10:00:01', done()) +
        line('10:01:00', plan()) + line('10:01:01', '[ADN-737-C] 联动删除未启用，仅记录不执行') +
        line('10:02:00', '忽略 Emby 删除回调（播放文件仍存在）: /library/a.mp4', 'handle_emby_deleted') +
        line('10:03:00', plan()))
    rows, summary = parse_logs([path])
    assert rows == [] and summary['dry_runs'] == 2 and summary['unmatched'] == 2


def test_same_code_distinct_deletions_and_duplicate_log_files(tmp_path):
    text = (line('10:00:00', plan(path='/downloads/part1.mp4')) + line('10:00:00', done()) +
            line('10:00:00', plan(path='/downloads/part2.mp4')) + line('10:00:00', done(errors=1)))
    paths = [log(tmp_path, text), log(tmp_path, text, 'cinefold.2026-10-22_00-00-00_1.log')]
    rows, _ = parse_logs(paths)
    assert len(rows) == 2 and rows[0]['id'] != rows[1]['id']
    assert rows[1]['status'] == 'partial' and rows[1]['result']['summary']['errors'] == 1


def test_ambiguous_concurrent_same_code_and_expired_plan_are_skipped(tmp_path):
    path = log(tmp_path, line('10:00:00', plan()) + line('10:00:01', plan()) + line('10:00:02', done()) +
               line('11:00:00', plan()) + line('12:00:00', done()))
    rows, summary = parse_logs([path])
    assert rows == [] and summary['unmatched'] == 2


def test_import_is_idempotent_and_does_not_change_active_download_data(db, tmp_path):
    db.configure(autoflush=False)
    path = log(tmp_path, line('10:00:00', plan()) + line('10:00:01', done()))
    with db.begin() as session:
        session.add(Code(code='ADN-737-C', status=4, title='Current title'))
        session.add(History(hash='a'*40, code='ADN-737-C', save_path='/current/new-file.mp4'))
    assert import_log_history([path])['added'] == 1
    with db() as session:
        assert list(session.scalars(select(DeletionRecord))) == []
    assert import_log_history([path], dry_run=False)['added'] == 1
    result = import_log_history([path], dry_run=False)
    assert result['added'] == 0 and result['existing'] == 1
    with db() as session:
        assert len(list(session.scalars(select(DeletionRecord)))) == 1
        assert session.get(Code, 'ADN-737-C').status == 4
        assert session.get(History, 'a'*40).save_path == '/current/new-file.mp4'


def test_native_audit_record_is_not_duplicated(db, tmp_path):
    db.configure(autoflush=False)
    path = log(tmp_path, line('10:00:00', plan()) + line('10:00:01', done()))
    row = parse_logs([path])[0][0]
    row.update(id='native', source='emby')
    with db.begin() as session:
        session.add(DeletionRecord(**row))
    assert import_log_history([path], dry_run=False)['added'] == 0


def test_authenticated_api_defaults_to_preview(db, tmp_path, monkeypatch):
    from app.api.endpoints.medialink import router
    from app.api.endpoints import get_current_user
    from app.utils import log as logs
    log(tmp_path, line('10:00:00', plan()) + line('10:00:01', done()))
    monkeypatch.setattr(logs, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(logs, 'LOG_FILE', tmp_path/'cinefold.log')
    app = FastAPI(); app.include_router(router); client = TestClient(app)
    assert client.post('/medialinks/deletion-history/import-logs').status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: 'tester'
    assert client.post('/medialinks/deletion-history/import-logs').json()['data']['dry_run']
    with db() as session:
        assert list(session.scalars(select(DeletionRecord))) == []
    result = client.post('/medialinks/deletion-history/import-logs?dry_run=false').json()['data']
    assert result['added'] == 1 and not result['dry_run']
