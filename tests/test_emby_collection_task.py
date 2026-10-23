import threading
from types import SimpleNamespace

import pytest

from app.services import embycollections as collections
from tests.test_emby_collections_auto import case, db, wait, expand


@pytest.fixture
def task_case(case, monkeypatch):
    from app.modules.review import reviewai
    case[0].configure(autoflush=False)
    monkeypatch.setattr(reviewai,'ReviewAI',lambda:SimpleNamespace(enabled=True))
    return case


def test_task_publishes_all_categories_and_scheduled_run_skips_unchanged(task_case, monkeypatch):
    collections.task_run()
    result=wait()
    assert result['phase']=='published' and result['automatic']
    assert result['scope']['limit']==0 and result['mode']=='all'
    assert len(task_case[2].collections)==2
    posts=list(task_case[2].posts)
    monkeypatch.setattr(collections,'_ai',lambda *args:pytest.fail('unchanged scheduled task must not call AI'))
    assert collections.task_run(scheduled=True)['skipped']
    assert task_case[2].posts==posts
    expand(task_case,4)
    monkeypatch.setattr(collections,'_ai',lambda *args:[])
    collections.task_run(scheduled=True)
    assert wait()['phase']=='published'
    assert len(task_case[2].collections)==2
    assert all(row['ids']=={'1','2','3','4'} for row in task_case[2].collections.values())


def test_task_resumes_failed_publication_without_reanalyzing_success(task_case, monkeypatch):
    task_case[2].timeout_create=True
    collections.task_run()
    result=wait()
    assert result['phase']=='partial' and result['can_resume']
    posts=list(task_case[2].posts)
    monkeypatch.setattr(collections,'_ai',lambda *args:pytest.fail('resume must not repeat finished AI batches'))
    collections.task_run()
    assert wait()['phase']=='published'
    assert task_case[2].posts==posts


def test_task_replaces_legacy_limited_preview_with_full_automatic_job(task_case):
    collections.suggest(limit=20)
    assert wait()['phase']=='ready'
    collections.task_run()
    result=wait()
    assert result['phase']=='published' and result['automatic']
    assert result['scope']['limit']==0


def test_busy_task_rejects_second_manual_start_and_skips_schedule(task_case, monkeypatch):
    entered, release=threading.Event(),threading.Event()
    def ai(*args):
        entered.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr(collections,'_ai',ai)
    try:
        collections.task_run()
        assert entered.wait(1)
        with pytest.raises(ValueError,match='正在运行'):
            collections.task_run()
        assert collections.task_run(scheduled=True)['skipped']
    finally:
        release.set()
        wait()


def test_task_api_returns_actual_state_and_allows_explicit_restart(task_case, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.config import router
    app=FastAPI()
    app.include_router(router)
    client=TestClient(app)
    assert client.post('/task?job_id=auto_emby_collections').status_code in (401,403)
    app.dependency_overrides[get_current_user]=lambda:'tester'
    response=client.post('/task?job_id=auto_emby_collections').json()
    assert response['code']==200 and response['data']['automatic']
    assert wait()['phase']=='published'
    calls=[]
    monkeypatch.setattr(collections,'_ai',lambda *args:(calls.append(True) or []))
    assert client.post('/task?job_id=auto_emby_collections&restart=true').json()['code']==200
    assert wait()['phase']=='published' and calls


def test_task_schedule_edit_is_persisted_and_registered(monkeypatch, tmp_path, db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import scheduler
    from app.core import config
    from app.api.endpoints import get_current_user
    from app.api.endpoints.config import router
    monkeypatch.setenv('EMBY_COLLECTION_TIME','0 5 * * *')
    monkeypatch.setattr(config,'ENV_FILE',tmp_path/'app.env')
    config.ENV_FILE.write_text('EMBY_COLLECTION_TIME=0 5 * * *\n',encoding='utf-8')
    monkeypatch.setattr(config,'_settings',None)
    monkeypatch.setattr(scheduler,'restart_scheduler',lambda:None)
    job=SimpleNamespace(id='auto_emby_collections',name='AI 全库自动合集',next_run_time=None,trigger='cron')
    monkeypatch.setattr(scheduler,'_scheduler',SimpleNamespace(get_jobs=lambda:[job]))
    assert config.Settings().emby_collection_time=='0 5 * * *'
    assert scheduler.schedule_key_of(job.id)==('emby_collection_time','cron')
    app=FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user]=lambda:'tester'
    client=TestClient(app)
    result=client.post('/cron',json={'job_id':job.id,'text':'每天凌晨6点'}).json()
    assert result['code']==200 and result['data']['schedule']=='0 6 * * *'
    assert 'EMBY_COLLECTION_TIME=0 6 * * *' in config.ENV_FILE.read_text(encoding='utf-8')
    assert config.load_settings().emby_collection_time=='0 6 * * *'
    jobs=client.get('/cron').json()['data']['jobs']
    assert jobs[0]['editable'] and jobs[0]['schedule']=='0 6 * * *'
