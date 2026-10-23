import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.database.models import Cache
from app.services import taskruns
from tests.test_scrape_reliability import db


@pytest.fixture
def runs(db, monkeypatch):
    db.configure(autoflush=False)
    monkeypatch.setattr(taskruns, '_STATES', {})
    monkeypatch.setattr(taskruns, '_ACTIVE', set())
    monkeypatch.setattr(taskruns, '_LOADED', False)
    with ThreadPoolExecutor(max_workers=3) as pool:
        monkeypatch.setattr(taskruns, '_POOL', pool)
        yield db, pool


def test_manual_and_schedule_share_ownership_and_persist(runs):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def work():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return 7
    try:
        taskruns.submit('same', work)
        assert entered.wait(3)
        with pytest.raises(ValueError):
            taskruns.submit('same', work)
        assert taskruns.scheduled('same', work) is None
    finally:
        release.set()
        runs[1].shutdown(wait=True)
    result = taskruns.states()['same']
    assert calls == [1] and result['phase'] == 'finished' and result['processed'] == 7
    with runs[0]() as session:
        row = session.scalar(select(Cache).where(Cache.namespace == 'scheduler-runs'))
        assert json.loads(row.content)['run_id'] == result['run_id']


def test_manual_pool_limits_running_and_keeps_fourth_queued(runs):
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]
    def work(i):
        entered[i].set()
        assert release.wait(5)
    try:
        for i in range(4):
            taskruns.submit(str(i), lambda i=i: work(i))
        assert all(event.wait(3) for event in entered[:3])
        assert not entered[3].is_set()
        assert taskruns.states()['3']['phase'] == 'queued'
    finally:
        release.set()
        runs[1].shutdown(wait=True)
    assert entered[3].is_set()


def test_failed_run_can_retry_and_history_is_bounded(runs):
    def fail():
        raise RuntimeError('https://secret.test/?token=private password=secret')
    with pytest.raises(RuntimeError):
        taskruns.scheduled('retry', fail)
    error = taskruns.states()['retry']['error']
    assert 'secret' not in error and 'private' not in error
    for i in range(7):
        assert taskruns.scheduled('retry', lambda: i) == i
    result = taskruns.states()['retry']
    assert len(result['history']) == 5 and result['processed'] == 6
    assert 'retry' not in taskruns._ACTIVE


def test_restart_marks_unfinished_as_interrupted_without_running(runs, monkeypatch):
    with runs[0]() as session:
        session.add(Cache(namespace='scheduler-runs', key='unfinished', content=json.dumps({
            'phase': 'running', 'owner': 'prior-process', 'run_id': 'old', 'history': []})))
        session.commit()
    state = taskruns.states()['unfinished']
    assert state['phase'] == 'interrupted' and state['history'][0]['phase'] == 'interrupted'
    assert taskruns._ACTIVE == set()


def test_task_api_duplicate_is_conflict_and_unknown_is_not_found(runs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import scheduler
    from app.api.endpoints import get_current_user
    from app.api.endpoints.config import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post('/task?job_id=one').status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: 'tester'
    monkeypatch.setattr(scheduler, 'JOBS', {'one': {'func': lambda: 0, 'name': 'one'}})
    taskruns._reserve('one', 'manual')
    assert client.post('/task?job_id=one').json()['code'] == 409
    assert client.post('/task?job_id=missing').json()['code'] == 404
