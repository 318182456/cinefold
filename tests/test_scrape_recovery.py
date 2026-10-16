import json
import os
from pathlib import Path
import time
from datetime import datetime

import pytest

from app.database.models import Code, CodeStatus, MediaLink, ScrapeSourceVideo
from app.services import scrape, scrapeinventory, scraperebuild, scraperecovery
from tests.test_scrape_rebuild import library, copy_mode, preview, wait


def add_second(case, number=726):
    code = f'SONE-{number}'
    source = case['root'] / f'{code}.mp4'
    source.write_bytes(b'second independent video')
    os.utime(source, (time.time() - 600, time.time() - 600))
    with case['db']() as session:
        session.add(Code(code=code, title='第二影片', status=CodeStatus.DOWNLOADED))
        session.add(ScrapeSourceVideo(source_key=scrapeinventory.key(source), path=str(source), name=source.name,
                    detected_code=code, size=source.stat().st_size, mtime=source.stat().st_mtime,
                    artifact_status='pending', links=[], indexed_at=datetime.now()))
        session.commit()
    return source


def writer(case, calls, fail_second=False):
    def run(code, items, **options):
        calls.append((code, options))
        if fail_second and code == 'SONE-726':
            return scrape.ScrapeResult(code=code, error='network unavailable')
        target = case['output'] / 'rebuilt' / f'{code}.mp4'
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.link(items[0].path, target)
        target.with_suffix('.nfo').write_text(f'<movie><title>{code}</title></movie>')
        with case['db']() as session:
            if not session.get(MediaLink, str(target)):
                session.add(MediaLink(code=code, link_path=str(target), source_path=str(items[0].path)))
            session.commit()
        return scrape.ScrapeResult(code=code, links=[target], nfo_written=1)
    return run


def partial(case, monkeypatch):
    add_second(case)
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls, fail_second=True))
    value = preview()
    scraperebuild.start(value['token'], str(case['output']), refresh_metadata=True, force=True)
    result = wait()
    assert result['phase'] == 'partial', result
    assert result['done'] == result['total'] and result['ok'] == result['total'] - 1
    return result, calls


@pytest.mark.parametrize('mode', ['unfinished', 'failed'])
def test_resume_skips_success_and_preserves_its_files_and_options(copy_mode, monkeypatch, mode):
    case = copy_mode
    result, _ = partial(case, monkeypatch)
    target = case['output'] / 'rebuilt' / 'SONE-725.mp4'
    nfo = target.with_suffix('.nfo')
    original = (target.stat().st_ino, nfo.read_bytes(), nfo.stat().st_mtime_ns)
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    monkeypatch.setattr(scraperebuild, '_clear_copied_output', lambda *a: pytest.fail('resume must not clear rebuilt films'))
    scraperecovery.start(result['job_id'], mode)
    resumed = wait()
    assert resumed['phase'] == 'completed', resumed
    assert resumed['done'] == resumed['ok'] == 2
    assert [row[0] for row in calls] == ['SONE-726']
    assert calls[0][1]['force'] and calls[0][1]['refresh_metadata']
    assert (target.stat().st_ino, nfo.read_bytes(), nfo.stat().st_mtime_ns) == original
    assert resumed['quarantine_dir'] == result['quarantine_dir']
    assert not resumed['recovery']['can_resume']


def test_process_restart_and_partial_result_line_resume(copy_mode, monkeypatch):
    case = copy_mode
    result, _ = partial(case, monkeypatch)
    state_file = json.loads(scraperebuild._STATE_FILE.read_text(encoding='utf-8'))
    state_file['running'] = True
    scraperebuild._STATE_FILE.write_text(json.dumps(state_file), encoding='utf-8')
    monkeypatch.setattr(scraperebuild, '_STATE', {'phase':'idle', 'running':False})
    monkeypatch.setattr(scraperebuild, '_PLAN', None)
    restored = scraperebuild.state()
    assert restored['phase'] == 'interrupted' and restored['recovery']['can_resume']
    with (Path(result['quarantine_dir']) / 'results.jsonl').open('ab') as stream:
        stream.write(b'{"code":')
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperecovery.start(result['job_id'])
    resumed = wait()
    assert resumed['phase'] == 'completed', resumed
    assert [row[0] for row in calls] == ['SONE-726']


@pytest.mark.parametrize('legacy', [False, True])
def test_resume_after_interrupted_cleanup_reuses_backup(copy_mode, monkeypatch, legacy):
    case = copy_mode
    original_unlink = Path.unlink
    def interrupted(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if path == case['video']:
            raise OSError('process stopped after deleting old video')
        return result
    monkeypatch.setattr(Path, 'unlink', interrupted)
    value = preview()
    scraperebuild.start(value['token'], str(case['output']))
    failed = wait()
    assert failed['phase'] == 'failed' and not case['video'].exists()
    if legacy:
        path = Path(failed['quarantine_dir']) / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['version'] = 1
        for key in ('checkpoint', 'options', 'naming_config'):
            manifest.pop(key, None)
        path.write_text(json.dumps(manifest), encoding='utf-8')
        scraperecovery.reference_path(scraperebuild).unlink()
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    monkeypatch.setattr(scraperebuild, '_backup_sidecars', lambda *a: pytest.fail('completed backups must be reused'))
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperecovery.start(failed['job_id'])
    resumed = wait()
    assert resumed['phase'] == 'completed', resumed
    assert case['source'].exists() and [row[0] for row in calls] == ['SONE-725']
    assert resumed['quarantine_dir'] == failed['quarantine_dir']
    if legacy:
        assert any(note['status'] == 'legacy' for note in resumed['cleanup_notes'])


def test_resume_after_backup_failure_reuses_completed_copies(copy_mode, monkeypatch):
    case = copy_mode
    backup = scraperebuild._backup_sidecars
    def interrupted(*args):
        backup(*args)
        raise OSError('stopped after copies before checkpoint')
    monkeypatch.setattr(scraperebuild, '_backup_sidecars', interrupted)
    value = preview()
    scraperebuild.start(value['token'], str(case['output']))
    failed = wait()
    assert failed['phase'] == 'failed'
    monkeypatch.setattr(scraperebuild, '_backup_sidecars', backup)
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperecovery.start(failed['job_id'])
    assert wait()['phase'] == 'completed'


def test_resume_after_rename_before_journal_append(library, monkeypatch):
    case = library
    replace = os.replace
    def interrupted(source, target):
        result = replace(source, target)
        if Path(source) == case['video'].parent:
            raise OSError('stopped after rename')
        return result
    monkeypatch.setattr(os, 'replace', interrupted)
    value = preview()
    scraperebuild.start(value['token'], str(case['output']))
    failed = wait()
    assert failed['phase'] == 'failed' and not case['video'].exists()
    monkeypatch.setattr(os, 'replace', replace)
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperecovery.start(failed['job_id'])
    resumed = wait()
    assert resumed['phase'] == 'completed', resumed
    assert [row[0] for row in calls] == ['SONE-725']


def test_resume_rejects_stale_job_and_changed_naming(copy_mode, monkeypatch):
    case = copy_mode
    result, _ = partial(case, monkeypatch)
    with pytest.raises(ValueError, match='变化'):
        scraperecovery.start('different-job')
    case['settings'].scrape_file_template = '{number}-changed'
    scraperecovery.start(result['job_id'])
    resumed = wait()
    assert resumed['phase'] == 'failed' and '命名规则' in resumed['error']
    assert (case['output'] / 'rebuilt' / 'SONE-725.mp4').exists()


def test_resume_reference_survives_new_preview(copy_mode, monkeypatch):
    case = copy_mode
    result, _ = partial(case, monkeypatch)
    preview()
    assert scraperebuild.state()['recovery']['job_id'] == result['job_id']
    assert scraperebuild.state()['recovery']['can_resume']


def test_failed_only_leaves_pending_films_for_continue(copy_mode, monkeypatch):
    case = copy_mode
    add_second(case, 727)
    result, _ = partial(case, monkeypatch)
    directory = Path(result['quarantine_dir'])
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    manifest['results'] = [row for row in manifest['results'] if row['code'] != 'SONE-727']
    (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    journal = directory / 'results.jsonl'
    journal.write_text(''.join(json.dumps(row) + '\n' for row in manifest['results']), encoding='utf-8')
    scraperebuild._update(done=2, ok=1)
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperecovery.start(result['job_id'], 'failed')
    resumed = wait()
    assert resumed['phase'] == 'partial' and resumed['done'] == resumed['ok'] == 2
    assert [row[0] for row in calls] == ['SONE-726']
    calls.clear()
    scraperecovery.start(result['job_id'], 'unfinished')
    assert wait()['phase'] == 'completed'
    assert [row[0] for row in calls] == ['SONE-727']


def test_corrupt_middle_journal_record_does_not_clear_completed_films(copy_mode, monkeypatch):
    case = copy_mode
    result, _ = partial(case, monkeypatch)
    journal = Path(result['quarantine_dir']) / 'results.jsonl'
    journal.write_bytes(b'broken record\n' + journal.read_bytes())
    scraperecovery.start(result['job_id'])
    resumed = wait()
    assert resumed['phase'] == 'failed' and '中间记录损坏' in resumed['error']
    assert (case['output'] / 'rebuilt' / 'SONE-725.mp4').exists()


def test_resume_refuses_path_traversal_in_manifest(copy_mode, monkeypatch):
    case = copy_mode
    result, _ = partial(case, monkeypatch)
    path = Path(result['quarantine_dir']) / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['original_manifest'].append(['../outside.mp4', [0, 0, 0, 0, 0], 'file'])
    path.write_text(json.dumps(manifest), encoding='utf-8')
    scraperecovery.start(result['job_id'])
    resumed = wait()
    assert resumed['phase'] == 'failed' and '不安全' in resumed['error']
    assert (case['output'] / 'rebuilt' / 'SONE-725.mp4').exists()


def test_resume_api_auth_and_modes(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.api.endpoints import get_current_user
    app = create_app()
    client = TestClient(app)
    assert client.post('/api/v1/scrape/rebuild/resume', json={'job_id':'job'}).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    calls = []
    monkeypatch.setattr(scraperecovery, 'start', lambda job, mode: calls.append((job, mode)) or {'running':True})
    for mode in ('unfinished', 'failed'):
        assert client.post('/api/v1/scrape/rebuild/resume', json={'job_id':'job', 'mode':mode}).status_code == 200
    assert calls == [('job','unfinished'), ('job','failed')]
    assert client.post('/api/v1/scrape/rebuild/resume', json={'job_id':'job', 'mode':'all'}).status_code == 422


def test_select_older_legacy_task_after_second_backup(copy_mode, monkeypatch):
    case = copy_mode
    original = scraperebuild._clear_copied_output
    def stop_after_backup(*args):
        raise OSError('interrupted before cleanup')
    monkeypatch.setattr(scraperebuild, '_clear_copied_output', stop_after_backup)
    value = preview()
    scraperebuild.start(value['token'], str(case['output']))
    first = wait()
    directory = Path(first['quarantine_dir'])
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['version'] = 1
    manifest['phase'] = 'backups_verified'
    for key in ('checkpoint', 'options', 'naming_config', 'source_count', 'done', 'ok'):
        manifest.pop(key, None)
    # Oversized legacy codes need not be loaded merely to choose a task.
    manifest['codes'] = ['x' * 100000]
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    second = directory.parent / f'.{case["output"].name}.cinefold-rebuild-second'
    second.mkdir()
    second_manifest = {**manifest, 'job_id': 'second-job', 'created_at': '2099-01-01', 'backup_bytes': 123}
    (second / 'manifest.json').write_text(json.dumps(second_manifest, indent=2), encoding='utf-8')
    reference = {'job_id': 'second-job', 'quarantine_dir': str(second), 'phase': 'interrupted', 'running': False,
                 'done': 0, 'total': 1, 'ok': 0}
    scraperecovery.reference_path(scraperebuild).write_text(json.dumps(reference), encoding='utf-8')
    before = (second / 'manifest.json').read_bytes()
    rows = scraperecovery.history(scraperebuild)
    assert rows[0]['job_id'] == 'second-job'
    assert {row['job_id'] for row in rows} == {first['job_id'], 'second-job'}
    assert next(row for row in rows if row['job_id'] == first['job_id'])['total'] is None
    monkeypatch.setattr(scraperebuild, '_clear_copied_output', original)
    monkeypatch.setattr(scraperebuild, '_backup_sidecars', lambda *a: pytest.fail('reuse original backup'))
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperecovery.start(first['job_id'])
    result = wait()
    assert result['phase'] == 'completed', result
    assert result['quarantine_dir'] == str(directory)
    assert [row[0] for row in calls] == ['SONE-725']
    assert (second / 'manifest.json').read_bytes() == before
    assert scraperecovery.reference(scraperebuild)['job_id'] == first['job_id']


def test_new_rebuild_requires_explicit_choice_with_unfinished_history(copy_mode, monkeypatch):
    case = copy_mode
    old, _ = partial(case, monkeypatch)
    value = preview()
    with pytest.raises(ValueError, match='历史任务'):
        scraperebuild.start(value['token'], str(case['output']))
    assert not scraperebuild.state()['running']
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scraperebuild.start(value['token'], str(case['output']), new_task=True)
    result = wait()
    assert result['phase'] == 'completed', result
    assert result['job_id'] != old['job_id']
    assert (Path(old['quarantine_dir']) / 'manifest.json').is_file()


def test_history_api_authenticated_read_only(copy_mode, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.api.endpoints import get_current_user
    result, _ = partial(copy_mode, monkeypatch)
    app = create_app()
    client = TestClient(app)
    assert client.get('/api/v1/scrape/rebuild/tasks').status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    response = client.get('/api/v1/scrape/rebuild/tasks')
    assert response.status_code == 200
    row = response.json()['data']['items'][0]
    assert row['job_id'] == result['job_id'] and row['done'] == 2 and row['ok'] == 1
    assert row['can_retry_failed']
    assert not scraperebuild.state()['running']
