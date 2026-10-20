import json
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.database.models import ScrapeTask, ScrapeTaskResult
from app.services import scrape, scrapehistory, scraperebuild, scraperecovery, scrapesources, scrapewatch
from tests.test_scrape_rebuild import library, copy_mode, wait
from tests.test_scrape_recovery import partial, writer
from tests.test_scrape_sources import env, video


def test_download_automatic_history_survives_restart_and_retries_only_failed(env, monkeypatch):
    from types import SimpleNamespace
    from app import services
    root, settings, factory, _, tasks = env
    settings.scrape_mode = 'builtin'
    monkeypatch.setattr(services, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    paths = [video(root, f'SONE-{n}.mp4') for n in (725, 726, 727)]
    original = [path.read_bytes() for path in paths]
    seen = []
    def run(code, items, **options):
        rows = scrapehistory.listing()['items']
        assert len(rows) == 1 and rows[0]['running']
        assert not rows[0]['can_retry_failed']
        with pytest.raises(ValueError, match='运行中'):
            scrapehistory.remove(rows[0]['job_id'])
        seen.append(code)
        if code == 'SONE-726':
            raise RuntimeError('metadata unavailable')
        return scrape.ScrapeResult(code=code, links=[items[0].path], warnings=['artwork warning'] if code=='SONE-727' else [])
    monkeypatch.setattr(scrape, 'scrape_group', run)
    client = SimpleNamespace(list_torrent_files=lambda hashes: [str(path) for path in paths])
    services._scrape_just_completed(client, ['test-hash'])
    row = scrapehistory.listing()['items'][0]
    assert row['kind'] == 'automatic' and row['phase'] == 'partial'
    assert (row['total'], row['done'], row['ok'], row['failed']) == (3, 3, 2, 1)
    assert row['can_retry_failed'] and row['finished_at']
    assert scrapehistory.detail(row['job_id'], status='failed')['items'][0]['error'] == 'metadata unavailable'
    with factory() as session:
        assert session.get(ScrapeTask, row['job_id']).options['trigger'] == 'download'
    # A fresh request reads persisted results after the active task registry is empty.
    assert not scrapehistory._AUTOMATIC
    assert scrapehistory.detail(row['job_id'])['total'] == 3
    seen.clear()
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **kw: seen.append(code) or scrape.ScrapeResult(code=code, links=[items[0].path]))
    resumed = scrapehistory.resume(row['job_id'], 'failed')
    tasks[-1].run()
    assert seen == ['SONE-726']
    assert scrapehistory.detail(resumed['job_id'])['task']['parent_id'] == row['job_id']
    scrapehistory.remove(row['job_id'])
    assert [path.read_bytes() for path in paths] == original


def test_directory_automatic_records_only_selected_stable_groups(env, monkeypatch):
    import os
    import time
    root, settings, _, _, _ = env
    settings.scrape_mode = 'builtin'
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    monkeypatch.setattr(scrapewatch, '_OBSERVED', {})
    monkeypatch.setattr(scrapewatch, '_RETRY', {})
    paths = [video(root, f'SONE-725-CD{n}.mp4') for n in (1, 2)]
    for path in paths:
        os.utime(path, (time.time()-500, time.time()-500))
    now = [1000.0]
    monkeypatch.setattr(scrapewatch.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **kw: scrape.ScrapeResult(code=code, links=[i.path for i in items]))
    assert scrapewatch.scan() == 0
    assert scrapehistory.listing()['total'] == 0
    now[0] += 130
    assert scrapewatch.scan() == 1
    task = scrapehistory.listing()['items'][0]
    assert task['kind'] == 'automatic' and task['phase'] == 'completed'
    assert task['total'] == task['done'] == task['ok'] == 1
    assert len(scrapehistory.detail(task['job_id'])['items'][0]['paths']) == 2
    assert scrapewatch.scan() == 0
    assert scrapehistory.listing()['total'] == 1


def test_interrupted_automatic_task_can_continue_pending_only(env, monkeypatch):
    root, settings, _, _, tasks = env
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    paths = [video(root, f'SONE-{n}.mp4') for n in (725, 726)]
    scrapehistory.begin('interrupted-auto', 'automatic', [{'path':str(path),'code':f'SONE-{n}'} for path,n in zip(paths,(725,726))], {'overwrite':False}, 2)
    scrapehistory.record('interrupted-auto', {'code':'SONE-725','ok':True})
    scrapehistory.sync({'job_id':'interrupted-auto','phase':'running','done':1,'ok':1}, 'automatic')
    row = scrapehistory.listing()['items'][0]
    assert row['phase'] == 'interrupted' and row['can_resume']
    seen = []
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **kw: seen.append(code) or scrape.ScrapeResult(code=code, links=[items[0].path]))
    scrapehistory.resume(row['job_id'])
    tasks[-1].run()
    assert seen == ['SONE-726']


def test_download_history_retry_accepts_only_original_files_outside_inventory(env, monkeypatch, tmp_path):
    root, settings, _, _, tasks = env
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    outside = tmp_path / 'downloader'; outside.mkdir()
    source = video(outside)
    unrelated = video(outside, 'SONE-726.mp4')
    groups = scrapesources.group_entries([{'path':str(source),'code':'SONE-725'}])
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **kw: scrape.ScrapeResult(code=code, error='offline'))
    with scrapehistory.AutomaticTask(groups, settings, 'download') as task:
        task.process('SONE-725', groups['SONE-725'])
    with pytest.raises(ValueError, match='下载源目录'):
        scrapesources.validate_file(str(unrelated), task.job_id)
    with pytest.raises(ValueError, match='下载源目录'):
        scrapesources.validate_file(str(source))
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **kw: scrape.ScrapeResult(code=code, links=[items[0].path]))
    resumed = scrapehistory.resume(task.job_id, 'failed')
    tasks[-1].run()
    assert scrapehistory.detail(resumed['job_id'])['task']['phase'] == 'completed'
    assert source.read_bytes() == unrelated.read_bytes() == b'video-data'


def test_rebuild_history_retry_and_delete_preserve_files(copy_mode, monkeypatch):
    case = copy_mode
    case['db'].configure(autoflush=False)
    result, _ = partial(case, monkeypatch)
    job_id = result['job_id']
    rows = scrapehistory.listing()['items']
    assert len(rows) == 1 and rows[0]['failed'] == 1
    assert scrapehistory.detail(job_id, status='failed')['items'][0]['code'] == 'SONE-726'
    target = case['output'] / 'rebuilt' / 'SONE-725.mp4'
    original = (target.stat().st_ino, target.with_suffix('.nfo').read_bytes())
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', writer(case, calls))
    scrapehistory.resume(job_id, 'failed')
    assert wait()['phase'] == 'completed'
    assert [row[0] for row in calls] == ['SONE-726']
    detail = scrapehistory.detail(job_id)
    assert detail['task']['done'] == detail['task']['ok'] == 2
    assert detail['total'] == 2
    assert not detail['task']['can_retry_failed']
    manifest = Path(result['quarantine_dir']) / 'manifest.json'
    backup = manifest.read_bytes()
    assert scrapehistory.remove(job_id) == {'deleted':True, 'files_deleted':False}
    assert scrapehistory.listing()['total'] == 0
    assert scraperecovery.history(scraperebuild) == []
    assert not scraperebuild.state()['recovery']['can_resume']
    assert scraperebuild.state()['phase'] == 'idle'
    assert (target.stat().st_ino, target.with_suffix('.nfo').read_bytes()) == original
    assert case['source'].exists() and manifest.read_bytes() == backup
    with pytest.raises(ValueError): scrapehistory.resume(job_id)
    # A process restart must not bring the deleted banner or task back.
    monkeypatch.setitem(scraperebuild._STATE, 'phase', 'idle')
    assert scraperebuild.state()['phase'] == 'idle'
    assert scrapehistory.listing()['items'] == []


def test_legacy_history_imports_all_rows_from_journal(copy_mode, monkeypatch):
    result, _ = partial(copy_mode, monkeypatch)
    with copy_mode['db']() as session:
        session.execute(delete(ScrapeTaskResult))
        session.execute(delete(ScrapeTask))
        session.commit()
    # Simulate older versions: only the journal contains the final results.
    manifest_path = Path(result['quarantine_dir'])/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['results'] = []
    scraperebuild._write_manifest(manifest_path, manifest)
    assert scrapehistory.listing()['total'] == 1
    detail = scrapehistory.detail(result['job_id'], size=1)
    assert detail['total'] == 2 and len(detail['items']) == 1
    assert detail['task']['total'] == detail['task']['done'] == 2
    assert detail['task']['ok'] == 1
    # Import is one-time; a temporarily unavailable backup does not hide DB results.
    manifest_path.rename(manifest_path.with_suffix('.saved'))
    assert scrapehistory.detail(result['job_id'])['total'] == 2


def test_manual_results_survive_restart_and_retry_failed_only(env, monkeypatch):
    root, settings, factory, _, tasks = env
    factory.configure(autoflush=False)
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    monkeypatch.setattr(scrapehistory, '_live', lambda: {scrapesources._STATE['job_id']} if scrapesources._STATE['running'] else set())
    paths = [video(root, f'SONE-{number}.mp4') for number in (725,726)]
    calls = []
    def run(code, items, **options):
        calls.append(code)
        return scrape.ScrapeResult(code=code, links=[items[0].path], error='network unavailable' if code=='SONE-726' else '')
    monkeypatch.setattr(scrape, 'scrape_group', run)
    job = scrapesources.start([{'path':str(path)} for path in paths], force=True)
    with pytest.raises(ValueError, match='运行中'): scrapehistory.remove(job['job_id'])
    tasks[0].run()
    assert scrapehistory.detail(job['job_id'])['task']['failed'] == 1
    monkeypatch.setitem(scrapesources._STATE, 'job_id', '')
    assert scrapehistory.listing()['items'][0]['failed'] == 1
    calls.clear()
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **options: (calls.append(code) or scrape.ScrapeResult(code=code, links=[items[0].path])))
    resumed = scrapehistory.resume(job['job_id'], 'failed')
    tasks[-1].run()
    assert calls == ['SONE-726']
    assert scrapehistory.detail(resumed['job_id'])['task']['parent_id'] == job['job_id']
    assert scrapehistory.detail(resumed['job_id'])['task']['phase'] == 'completed'
    assert all(path.exists() for path in paths)


def test_interrupted_manual_task_keeps_pending_sources(env, monkeypatch):
    root, _, _, _, tasks = env
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    monkeypatch.setattr(scrapehistory, '_live', lambda: set())
    first, second = video(root), video(root, 'SONE-726.mp4')
    scrapehistory.begin('old-task', 'manual', [{'path':str(first),'code':'SONE-725'}, {'path':str(second),'code':'SONE-726'}], {}, 2)
    scrapehistory.record('old-task', {'code':'SONE-725','ok':True,'error':''})
    scrapehistory.sync({'job_id':'old-task','phase':'running','done':1,'ok':1}, 'manual')
    assert scrapehistory.listing()['items'][0]['phase'] == 'interrupted'
    scrapehistory.resume('old-task')
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **options: (calls.append(code) or scrape.ScrapeResult(code=code)))
    tasks[-1].run()
    assert calls == ['SONE-726']


def test_history_api_auth_validation_pagination_and_delete(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.scrape import router
    monkeypatch.setattr(scrapehistory, '_legacy', lambda: None)
    monkeypatch.setattr(scrapehistory, '_live', lambda: set())
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    client = TestClient(app)
    assert client.get('/api/v1/scrape/history').status_code in (401,403)
    app.dependency_overrides[get_current_user] = lambda: 'test-user'
    scrapehistory.begin('api-task','manual',[],{},1)
    scrapehistory.record('api-task',{'code':'SONE-725','ok':False,'error':'network unavailable'})
    scrapehistory.sync({'job_id':'api-task','phase':'partial','done':1,'ok':0},'manual')
    assert client.get('/api/v1/scrape/history?size=101').status_code == 422
    response = client.get('/api/v1/scrape/history?size=1').json()['data']
    assert response['total'] == 1 and response['items'][0]['failed'] == 1
    assert client.get('/api/v1/scrape/history/api-task?status=failed').json()['data']['total'] == 1
    assert client.post('/api/v1/scrape/history/api-task/resume',json={'mode':'all'}).status_code == 422
    assert client.delete('/api/v1/scrape/history/api-task').json()['data']['files_deleted'] is False
    assert client.get('/api/v1/scrape/history').json()['data']['total'] == 0
    assert client.get('/api/v1/scrape/history/api-task').status_code == 409
