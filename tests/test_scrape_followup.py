import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from loguru import logger

from app.core.config import Settings
from app.services import scrape, scrapefollowup
from app.utils.mediafile import parse


@pytest.fixture
def background(monkeypatch):
    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(scrapefollowup, '_POOL', pool)
    monkeypatch.setattr(scrapefollowup, '_CAPACITY', threading.BoundedSemaphore(4))
    monkeypatch.setattr(scrapefollowup, '_PENDING', set())
    yield pool, stop
    # Always unblock workers before joining, including when an assertion fails.
    stop.set()
    pool.shutdown(wait=True, cancel_futures=True)
    assert not scrapefollowup._PENDING


def test_slow_followup_does_not_hold_scrape_worker_or_code_lock(background, monkeypatch, tmp_path):
    _, stop = background
    entered = threading.Event()

    def slow(*args):
        entered.set()
        assert stop.wait(3)

    monkeypatch.setattr(scrapefollowup, '_run', slow)
    settings = Settings(review_enabled=True, scrape_notify=False, scrape_still_limit=0)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    from app.services import mediaduplicate
    monkeypatch.setattr(mediaduplicate, 'check_existing', lambda *a: ({}, []))
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'title': 'Title'})
    monkeypatch.setattr(scrape, '_cover_bytes', lambda *a: b'')
    monkeypatch.setattr(scrape, '_poster_bytes', lambda *a: b'')

    def commit(*args):
        result = args[-1]
        result.links.append(tmp_path / 'library' / 'SONE-725.mp4')
        result.nfo_written = 1
        return result, True

    monkeypatch.setattr(scrape, '_commit_group', commit)
    start = time.perf_counter()
    result = scrape.scrape_group('SONE-725', [parse(tmp_path / 'SONE-725.mp4')])
    assert result.ok
    assert time.perf_counter() - start < 0.5
    assert entered.wait(1)
    assert not stop.is_set()
    assert not scrape._CODE_LOCKS


def test_queue_deduplicates_same_code_and_bounds_total_work(background, monkeypatch):
    _, stop = background
    monkeypatch.setattr(scrapefollowup, '_run', lambda *a: stop.wait(3))
    assert scrapefollowup.schedule('SONE-725', 'Title', 1, review=True) == 'queued'
    assert scrapefollowup.schedule('sone-725', 'Title', 1, review=True) == 'pending'
    for code in ('SONE-726', 'SONE-727', 'SONE-728'):
        assert scrapefollowup.schedule(code, 'Title', 1, review=True) == 'queued'
    assert scrapefollowup.schedule('SONE-729', 'Title', 1, review=True) == 'full'
    assert len(scrapefollowup._PENDING) == 4


def test_cancelled_queued_task_releases_capacity(background, monkeypatch):
    pool, stop = background
    futures = []
    original = pool.submit

    def submit(*args):
        future = original(*args)
        futures.append(future)
        return future

    monkeypatch.setattr(pool, 'submit', submit)
    monkeypatch.setattr(scrapefollowup, '_run', lambda *a: stop.wait(3))
    for code in ('SONE-725', 'SONE-726', 'SONE-727', 'SONE-728'):
        assert scrapefollowup.schedule(code, '', 1, review=True) == 'queued'
    assert futures[-1].cancel()
    assert 'SONE-728' not in scrapefollowup._PENDING
    assert scrapefollowup.schedule('SONE-729', '', 1, review=True) == 'queued'


def test_failed_submit_releases_capacity_and_logs(background, monkeypatch):
    pool, _ = background
    monkeypatch.setattr(pool, 'submit', lambda *a: (_ for _ in ()).throw(RuntimeError('executor stopped')))
    logs = []
    token = logger.add(lambda message: logs.append(str(message)))
    try:
        assert scrapefollowup.schedule('SONE-725', '', 1, review=True) == 'error'
    finally:
        logger.remove(token)
    assert not scrapefollowup._PENDING
    assert any('executor stopped' in message for message in logs)


def test_disabled_helpers_do_not_start_background_work(background, monkeypatch):
    pool, _ = background
    monkeypatch.setattr(pool, 'submit', lambda *a: pytest.fail('disabled helpers must not be submitted'))
    assert scrapefollowup.schedule('SONE-725', '', 1) == 'disabled'


def test_background_action_failure_is_visible_and_next_action_runs(monkeypatch):
    from app.services import medialink
    calls, logs = [], []
    monkeypatch.setattr(scrapefollowup, '_notify', lambda *a: calls.append('notify'))

    def fail(code):
        calls.append('subtitle')
        raise RuntimeError('subtitle broken')

    monkeypatch.setattr(medialink, '_fetch_subtitle_quietly', fail)
    monkeypatch.setattr(medialink, '_generate_review_quietly', lambda code: calls.append('review'))
    token = logger.add(lambda message: logs.append(str(message)))
    try:
        scrapefollowup._run('SONE-725', 'Title', 1, True, True, True)
    finally:
        logger.remove(token)
    assert calls == ['notify', 'subtitle', 'review']
    assert any('subtitle broken' in message for message in logs)
    assert any('后台AI 看点结束' in message for message in logs)
