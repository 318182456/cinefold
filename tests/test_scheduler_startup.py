"""覆盖完整任务注册表的真实启动，避免只测排班翻译而漏掉启动失败。"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi.testclient import TestClient

from app.core.config import Settings
from tests.test_scrape_reliability import db


def test_registry_entries_match_their_trigger_contract():
    from app import scheduler
    assert not set(scheduler.JOBS) & set(scheduler.INTERVAL_JOBS)
    for job_id, meta in scheduler.JOBS.items():
        assert 'cron_key' in meta, f'{job_id} 不属于 cron 任务注册表'
        assert meta['cron_key'] == '' or hasattr(Settings(), meta['cron_key'])
    for job_id, meta in scheduler.INTERVAL_JOBS.items():
        assert meta['minutes'] > 0, job_id
        assert not meta.get('interval_key') or hasattr(Settings(), meta['interval_key'])


def test_application_lifespan_registers_download_history_as_five_minute_interval(monkeypatch, db):
    import app.api as api
    from app import scheduler
    from app.database.utils import setup
    from app.modules.notify import tgpolling
    from app.modules import watcher

    settings = Settings(redis_job_store=False)
    monkeypatch.setattr(scheduler, '_scheduler', None)
    monkeypatch.setattr(scheduler, 'get_settings', lambda: settings)
    monkeypatch.setattr(api, 'get_settings', lambda: settings)
    monkeypatch.setattr(api, 'setup_loguru_logger', lambda: None)
    calls = []
    monkeypatch.setattr(setup, 'setup_database', lambda: calls.append('database'))
    for module, start, stop in ((tgpolling, 'start_polling', 'stop_polling'), (watcher, 'start_watching', 'stop_watching')):
        monkeypatch.setattr(module, start, lambda: None)
        monkeypatch.setattr(module, stop, lambda: None)

    def paused_scheduler(**kwargs):
        instance = BackgroundScheduler(**kwargs)
        start = instance.start
        # 使用真实注册、线程和生命周期；暂停业务触发，避免测试下载或通知。
        instance.start = lambda: start(paused=True)
        return instance
    monkeypatch.setattr(scheduler, 'BackgroundScheduler', paused_scheduler)
    try:
        with TestClient(api.create_app()) as client:
            assert client.get('/api/openapi.json').status_code == 200
            assert calls == ['database']
            assert scheduler._scheduler.running
            job = scheduler._scheduler.get_job('sync_download_history')
            assert job is not None and isinstance(job.trigger, IntervalTrigger)
            assert job.trigger.interval.total_seconds() == 300
            item = next(row for row in scheduler.list_jobs() if row['id'] == job.id)
            assert item['kind'] == 'interval' and item['schedule'] == '5'
            assert not item['editable']
            assert scheduler.schedule_key_of(job.id) is None
    finally:
        scheduler.stop_scheduler()
    assert scheduler._scheduler is None
