import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.database.base import DBBase
from app.database.models import Code, CodeStatus, MediaLink
from app.modules.scrape import merge, preferences, watermark
from app.services import scrape, scrapewatch
from app.utils.mediafile import parse


@pytest.fixture
def db(monkeypatch):
    import app.database.session as database
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    yield factory
    engine.dispose()


def test_slow_source_does_not_extend_wait_and_airav_starts(monkeypatch):
    from app.modules import ladysite
    from app.core import config
    stop = threading.Event()
    settings = Settings(scrape_merge_grace=0)
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: ('javbus', 'javdb', 'jav321', 'avbase', 'avmoo', 'dmm', 'airav'))
    def fetch(site, code):
        if site == 'airav':
            return {'code': code, 'title': '快速结果'}
        stop.wait(3)
        return {'code': code, 'publisher': site}
    monkeypatch.setattr(ladysite, '_fetch_detail', fetch)
    with ThreadPoolExecutor(max_workers=6) as pool:
        monkeypatch.setattr(merge, '_POOL', pool)
        monkeypatch.setattr(merge, '_SLOTS', threading.BoundedSemaphore(6))
        try:
            start = time.perf_counter()
            got = merge.fetch_merged('SONE-725', timeout=0.5)
            assert time.perf_counter() - start < 1
            assert got['title'] == '快速结果'
            assert any(r['status'] == 'merge_window' for r in got['_sources'])
        finally:
            stop.set()


def test_all_sources_timeout_is_bounded(monkeypatch):
    from app.modules import ladysite
    stop = threading.Event()
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: ('javbus',))
    monkeypatch.setattr(ladysite, '_fetch_detail', lambda *a: stop.wait(3) and {})
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(merge, '_POOL', pool)
        monkeypatch.setattr(merge, '_SLOTS', threading.BoundedSemaphore(1))
        try:
            start = time.perf_counter()
            assert merge.fetch_merged('SONE-725', timeout=0.05) == {}
            assert time.perf_counter() - start < 0.5
        finally:
            stop.set()


def test_busy_pool_waits_for_capacity_instead_of_reporting_no_metadata(monkeypatch):
    from app.modules import ladysite
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    called = []
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: ('javbus',))
    def fetch(site, code):
        called.append(code)
        return {'code': code, 'title': 'Recovered after capacity became available'}
    monkeypatch.setattr(ladysite, '_fetch_detail', fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(merge, '_POOL', pool)
        monkeypatch.setattr(merge, '_SLOTS', slots)
        release = threading.Timer(0.05, slots.release)
        release.start()
        try:
            result = merge.fetch_merged('483DAM-001', timeout=0.5)
            assert result['title'] == 'Recovered after capacity became available'
            assert called == ['483DAM-001']
        finally:
            release.join()


def test_busy_pool_deadline_is_bounded_and_reports_unqueried_sources(monkeypatch):
    from app.modules import ladysite
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(merge, '_SLOTS', slots)
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: ('javbus', 'javdb'))
    monkeypatch.setattr(ladysite, '_fetch_detail', lambda *a: pytest.fail('No slot was available'))
    started = time.perf_counter()
    result = merge.fetch_merged('483DAM-001', timeout=0.05)
    assert 0.04 <= time.perf_counter() - started < 0.5
    assert not result
    assert all(s['status'] == 'not_started' for s in result.sources)


def test_parallel_movies_cannot_starve_each_others_source_requests(monkeypatch):
    from app.modules import ladysite
    from app.core import config
    first_started, second_started = threading.Event(), threading.Event()
    monkeypatch.setattr(config, 'get_settings', lambda: Settings(scrape_merge_grace=0))
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: tuple(f'source{i}' for i in range(6)))
    def fetch(site, code):
        if code == 'SONE-725':
            first_started.set()
            second_started.wait(1)
        else:
            second_started.set()
        return {'code': code, 'title': 'Title'}
    monkeypatch.setattr(ladysite, '_fetch_detail', fetch)
    with ThreadPoolExecutor(max_workers=6) as sources, ThreadPoolExecutor(max_workers=2) as films:
        monkeypatch.setattr(merge, '_POOL', sources)
        monkeypatch.setattr(merge, '_SLOTS', threading.BoundedSemaphore(6))
        try:
            first = films.submit(merge.fetch_merged, 'SONE-725', 0.4)
            assert first_started.wait(1)
            second = films.submit(merge.fetch_merged, 'SONE-726', 0.4)
            assert first.result(timeout=2)['title'] == 'Title'
            assert second.result(timeout=2)['title'] == 'Title'
        finally:
            second_started.set()


@pytest.mark.parametrize('code,lookup', [('ZSD-074-U', 'ZSD-074'), ('JUL-915-C', 'JUL-915'), ('483DAM-001', '483DAM-001')])
def test_failed_examples_lookup_base_number_without_changing_numeric_prefix(monkeypatch, code, lookup):
    calls = []
    monkeypatch.setattr(merge, '_fetch_one', lambda value, timeout: calls.append(value) or {})
    merge.fetch_merged(code)
    assert calls == [lookup]


def test_cover_only_hit_does_not_end_window_before_title(monkeypatch):
    from app.modules import ladysite
    from app.core import config
    monkeypatch.setattr(config, 'get_settings', lambda: Settings(scrape_merge_grace=0))
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: ('cover', 'title'))
    def fetch(site, code):
        if site == 'cover':
            return {'code': code, 'banner': 'https://images.example/cover.jpg'}
        time.sleep(0.03)
        return {'code': code, 'title': 'Actual movie title'}
    monkeypatch.setattr(ladysite, '_fetch_detail', fetch)
    assert merge.fetch_merged('SONE-725', timeout=0.5)['title'] == 'Actual movie title'


def test_metadata_rejects_number_only_titles_and_deduplicates_lists():
    assert merge.clean_detail({'title': 'MXGS-783-C', 'cn_title': 'MXGS-783'}, 'MXGS-783-C') == {}
    result = merge.clean_detail({'title': 'A real title', 'casts': ['甲', '甲', '乙'],
                                 'genres': '标签,标签,其他'}, 'MXGS-783')
    assert result == {'title': 'A real title', 'casts': '甲,乙', 'genres': '标签,其他'}


def test_merge_cancel_releases_real_async_network_worker(monkeypatch):
    import asyncio
    import httpx
    from app.modules import ladysite
    from app.modules.ladysite.transport import http_client
    from app.core import config
    running, closed = threading.Event(), threading.Event()
    async def respond(request):
        running.set()
        try:
            await asyncio.sleep(10)
            return httpx.Response(200, text='late')
        finally:
            closed.set()
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(config, 'get_settings', lambda: Settings(scrape_merge_grace=0))
    monkeypatch.setattr(ladysite, '_sites_for_code', lambda code: ('slow', 'fast'))
    def fetch(site, code):
        if site == 'fast':
            assert running.wait(1)
            return {'code': code, 'title': 'Ready'}
        with http_client(transport=transport, timeout=10) as client:
            client.get('https://source.example/detail')
        return {}
    monkeypatch.setattr(ladysite, '_fetch_detail', fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slots = threading.BoundedSemaphore(2)
        monkeypatch.setattr(merge, '_POOL', pool)
        monkeypatch.setattr(merge, '_SLOTS', slots)
        result = merge.fetch_merged('SONE-725', timeout=1)
        assert result['title'] == 'Ready'
        assert closed.wait(1), 'HTTP coroutine should be cancelled when the merge ends'
        assert slots.acquire(timeout=1) and slots.acquire(timeout=1)
        slots.release(); slots.release()


def test_capacity_timeout_is_not_reported_as_missing_metadata(scrape_case, monkeypatch):
    video, _ = scrape_case
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {
        '_sources': [{'site': 'javbus', 'status': 'not_started', 'elapsed': 0}]})
    result = scrape.scrape_file(video)
    assert '尚未查询数据源' in result.error
    assert '取不到元数据' not in result.error


def test_field_priority_and_exclusion_override_longest(monkeypatch):
    from app.core import config
    settings = Settings(scrape_field_priority={'casts': 'javbus,javdb', 'outline': 'airav'}, scrape_field_exclude={'title': 'javdb'})
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    got = merge.merge_details([
        merge.SourceResult('javdb', {'title': '忽略', 'casts': '演员甲,演员乙', 'outline': '更长的简介'}),
        merge.SourceResult('javbus', {'title': '保留', 'casts': '演员丙'}),
        merge.SourceResult('airav', {'outline': '短简介'}),
    ], 'SONE-725')
    assert got == {'title': '保留', 'casts': '演员丙', 'outline': '短简介'}


def test_type_order_keeps_only_available_sources():
    assert preferences.ordered('FC2-PPV-1234567', ['javdb', 'fc2'], {'fc2': 'fc2,airav'}) == ['fc2', 'javdb']
    assert preferences.validate({'scrape_field_priority': {'casts': 'invalid'}})
    assert preferences.validate({'scrape_field_priority': {'casts': 'Javbus,Airav_io'}}) == ''


def test_global_priority_and_chinese_field_use_real_returned_titles(monkeypatch):
    from app.core import config
    settings = Settings(scrape_source_priority={'censored': 'dmm,airav'}, scrape_field_priority={'cn_title': 'airav'})
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    got = merge.merge_details([
        merge.SourceResult('dmm', {'title': '日本語のタイトル', 'casts': '演员甲'}),
        merge.SourceResult('airav', {'title': '中文片名', 'casts': '演员乙,演员丙'}),
    ], 'SONE-725')
    assert got['title'] == '日本語のタイトル' and got['cn_title'] == '中文片名'
    assert got['casts'] == '演员甲'


def test_failed_source_reports_survive_empty_metadata(db, monkeypatch):
    report = {'site': 'airav', 'status': 'timeout', 'elapsed': 0.1}
    monkeypatch.setattr(merge, 'fetch_merged', lambda *a: merge.MetadataResult([report]))
    meta = scrape._load_meta('SONE-725')
    assert meta == {'_sources': [report]}


@pytest.fixture
def scrape_case(db, tmp_path, monkeypatch):
    source = tmp_path / 'download'; source.mkdir()
    video = source / 'SONE-725.mp4'; video.write_bytes(b'video')
    settings = Settings(medialink_scrape_dir=str(tmp_path / 'library'), scrape_dir_template='{number}', scrape_still_limit=0, scrape_notify=False)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'title': '标题'})
    monkeypatch.setattr(scrape, '_cover_bytes', lambda *a: b'')
    from app.services import medialink
    monkeypatch.setattr(medialink, '_fetch_subtitle_quietly', lambda *a: None)
    monkeypatch.setattr(medialink, '_generate_review_quietly', lambda *a: None)
    with db() as session:
        session.add(Code(code='SONE-725', status=CodeStatus.DOWNLOADED)); session.commit()
    return video, settings


def test_nfo_failure_never_registers_or_completes(scrape_case, db, monkeypatch):
    video, _ = scrape_case
    monkeypatch.setattr(scrape.nfo, 'write', lambda *a, **kw: False)
    result = scrape.scrape_file(video)
    assert not result.ok and 'NFO' in result.error
    with db() as session:
        assert not session.scalars(select(MediaLink)).all()
        assert session.get(Code, 'SONE-725').status == CodeStatus.DOWNLOADED
    assert video.exists()


def test_different_movies_prepare_concurrently_but_commit_serially(scrape_case, monkeypatch):
    video, _ = scrape_case
    other = video.with_name('SONE-726.mp4'); other.write_bytes(b'other-video')
    from app.services import mediaduplicate
    monkeypatch.setattr(mediaduplicate, 'check_existing', lambda code, items, provided=None: ({}, []))
    barrier = threading.Barrier(2)
    def load(code, *args, **kwargs):
        barrier.wait(timeout=2)
        return {'title': 'Title ' + code}
    monkeypatch.setattr(scrape, 'load_group_meta', load)
    monkeypatch.setattr(scrape, '_register', lambda *args: True)
    original = scrape.nfo.write
    guard = threading.Lock()
    counts = {'current': 0, 'peak': 0}
    def write(*args, **kwargs):
        with guard:
            counts['current'] += 1
            counts['peak'] = max(counts['peak'], counts['current'])
        try:
            time.sleep(0.02)
            return original(*args, **kwargs)
        finally:
            with guard:
                counts['current'] -= 1
    monkeypatch.setattr(scrape.nfo, 'write', write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(scrape.scrape_file, path) for path in (video, other)]
        assert all(f.result(timeout=3).ok for f in futures)
    assert counts['peak'] == 1
    assert not scrape._CODE_LOCKS


def test_commit_rechecks_duplicates_created_during_network_wait(scrape_case, monkeypatch):
    video, settings = scrape_case
    from app.services import mediaduplicate
    prepared = threading.Event()
    def check(code, items, provided=None):
        return ({}, ['New conflicting library file']) if prepared.is_set() else ({}, [])
    monkeypatch.setattr(mediaduplicate, 'check_existing', check)
    def load(*args, **kwargs):
        prepared.set()
        return {'title': 'Title'}
    monkeypatch.setattr(scrape, 'load_group_meta', load)
    result = scrape.scrape_file(video)
    assert not result.ok and 'New conflicting' in result.error
    assert not Path(settings.medialink_scrape_dir).exists()


def test_registration_failure_rolls_back_and_retry_succeeds(scrape_case, db, monkeypatch):
    from app.services import medialink
    video, _ = scrape_case
    original = medialink.mark_completed
    def fail(*a, **kw):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(medialink, 'mark_completed', fail)
    result = scrape.scrape_file(video)
    assert not result.ok and '登记失败' in result.error
    with db() as session:
        assert not session.scalars(select(MediaLink)).all()
    monkeypatch.setattr(medialink, 'mark_completed', original)
    result = scrape.scrape_file(video)
    assert result.ok and result.nfo_written == 0
    with db() as session:
        assert len(session.scalars(select(MediaLink)).all()) == 1
        assert session.get(Code, 'SONE-725').status == CodeStatus.COMPLETED


def test_strict_images_prevent_completed_status(scrape_case, db):
    video, settings = scrape_case
    settings.scrape_require_images = True
    assert not scrape.scrape_file(video).ok
    with db() as session:
        assert session.get(Code, 'SONE-725').status == CodeStatus.DOWNLOADED


def test_auto_scan_waits_for_stable_file_retries_without_starvation(db, tmp_path, monkeypatch):
    source = tmp_path / 'downloads'; source.mkdir()
    first = source / 'SONE-725.mp4'; first.write_bytes(b'video')
    second = source / 'SONE-726.mp4'; second.write_bytes(b'video')
    for path in (first, second): os.utime(path, (time.time() - 500, time.time() - 500))
    settings = Settings(scrape_mode='builtin', scrape_source_dirs=str(source), medialink_scrape_dir=str(tmp_path / 'library'), scrape_scan_limit=1)
    monkeypatch.setattr(scrapewatch, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrapewatch, '_OBSERVED', {})
    monkeypatch.setattr(scrapewatch, '_RETRY', {})
    now = [1000.0]
    monkeypatch.setattr(scrapewatch.time, 'monotonic', lambda: now[0])
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items: calls.append(code) or scrape.ScrapeResult(code=code, error='offline'))
    assert scrapewatch.scan() == 0 and not calls
    now[0] += 130
    scrapewatch.scan(); assert len(calls) == 1
    now[0] += 601
    scrapewatch.scan(); assert len(calls) == 2 and len(set(calls)) == 2
    settings.medialink_scrape_dir = str(source / 'output')
    scrapewatch.scan(); assert len(calls) == 2


def test_watermark_targets_and_fixed_position():
    data = BytesIO(); Image.new('RGB', (400, 600), '#182330').save(data, 'JPEG')
    opts = {'enabled': True, 'targets': 'poster,thumb', 'position_sub': 'bottom_right'}
    original = data.getvalue()
    assert watermark.render(original, ['sub'], opts, 'fanart') == original
    marked = watermark.render(original, ['sub'], opts, 'poster')
    with Image.open(BytesIO(marked)) as img:
        assert img.getpixel((380, 575))[1] > 90
        assert img.getpixel((20, 20))[1] < 60
    assert watermark.validate({'scale': 'nan'})


def test_watermark_detects_explicit_markers_and_mosaic_precedence():
    assert watermark.labels(parse('SONE-725-UCH-2160p.mp4')) == ['sub', 'crack', '4K']
    assert watermark.labels(parse('SONE-725-leaked-8K.mp4')) == ['leak', '8K']
    assert watermark.labels(parse('SONE-725.mp4')) == ['censored']


def test_watermark_preview_is_read_only():
    from app.api.endpoints.scrape import WatermarkPreviewRequest, watermark_preview
    got = watermark_preview(WatermarkPreviewRequest(config={'enabled': True}), 'admin')
    assert got['data']['image'].startswith('data:image/jpeg;base64,')
    assert got['data']['labels'] == ['sub', 'censored', '4K']


def test_subtitle_version_reuses_cached_base_without_remote_fetch(db, monkeypatch):
    from app import services
    monkeypatch.setattr(services, '_ensure_codes', lambda codes: ensure_codes(db, codes))
    with db() as session:
        session.add(Code(code='MXGS-783', title='基础资料', casts='演员甲', genres='标签',
                         duration='150', release_date='2015-07-16', banner='https://cover.test/base.jpg'))
        session.commit()
    monkeypatch.setattr(merge, 'fetch_merged', lambda *a, **kw: pytest.fail('已有完整基础资料，不能联网查 -C'))
    got = scrape._load_meta('MXGS-783-C')
    assert got['title'] == '基础资料' and got['code'] == 'MXGS-783-C'
    with db() as session:
        assert session.get(Code, 'MXGS-783-C').title == '基础资料'
        assert session.get(Code, 'MXGS-783').title == '基础资料'


def ensure_codes(factory, codes):
    with factory() as session:
        for code in codes:
            if session.get(Code, code) is None:
                session.add(Code(code=code))
        session.commit()


def test_version_specific_edits_win_over_base_metadata(db):
    with db() as session:
        session.add(Code(code='MXGS-783', title='原标题', casts='演员甲', banner='https://cover.test/base.jpg'))
        session.add(Code(code='MXGS-783-C', cn_title='人工标题', banner='https://cover.test/custom.jpg'))
        session.commit()
    got = scrape._read_meta('MXGS-783-C')
    assert got['title'] == '原标题' and got['cn_title'] == '人工标题'
    assert got['banner'] == 'https://cover.test/custom.jpg' and got['casts'] == '演员甲'


def test_subtitle_query_uses_full_timeout_for_base_and_preserves_empty_reports(monkeypatch):
    calls = []
    report = merge.MetadataResult([{'site': 'missav', 'status': 'timeout'}])
    def fetch(code, timeout):
        calls.append((code, timeout))
        return report
    monkeypatch.setattr(merge, '_fetch_one', fetch)
    got = merge.fetch_merged('MXGS-783-C', timeout=25)
    assert calls == [('MXGS-783', 25)]
    assert not got and got.sources[0]['site'] == 'missav'


def test_cached_base_remains_usable_when_remote_supplement_times_out(db, monkeypatch):
    from app import services
    monkeypatch.setattr(services, '_ensure_codes', lambda codes: ensure_codes(db, codes))
    with db() as session:
        session.add(Code(code='MXGS-783', title='已有基础资料', banner='https://cover.test/base.jpg'))
        session.commit()
    monkeypatch.setattr(merge, 'fetch_merged', lambda *a, **kw: merge.MetadataResult([{'site': 'missav', 'status': 'timeout'}]))
    got = scrape._load_meta('MXGS-783-C')
    assert got['title'] == '已有基础资料' and got['code'] == 'MXGS-783-C'
    assert got['_sources'][0]['status'] == 'timeout'
