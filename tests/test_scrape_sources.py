import os
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.database.base import DBBase
from app.database.models import Code, CodeStatus, MediaLink, ScrapeAttempt, ScrapeSourceVideo
from app.modules.scrape import local, merge, nfo
from app.services import scrape, scrapesources as sources, scrapewatch, scrapeinventory


@pytest.fixture
def env(tmp_path, monkeypatch):
    import app.database.session as database
    engine = create_engine('sqlite:///' + str(tmp_path / 'sources.db'), connect_args={'check_same_thread': False})
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    root = tmp_path / 'downloads'; root.mkdir()
    settings = Settings(scrape_source_dirs=str(root), medialink_scrape_dir=str(tmp_path / 'library'),
                        scrape_dir_template='{number}', scrape_still_limit=0, scrape_notify=False)
    for module in (sources, scrape, scrapewatch, scrapeinventory):
        monkeypatch.setattr(module, 'get_settings', lambda reload=False: settings)
    writes = []
    def save(updates):
        writes.append(updates)
        settings.scrape_source_dirs = updates['SCRAPE_SOURCE_DIRS']
    monkeypatch.setattr(sources, 'save_settings', save)
    monkeypatch.setattr(sources, '_STATE', {'running': False, 'job_id': '', 'total': 0, 'done': 0,
        'ok': 0, 'current': '', 'results': [], 'error': '', 'cancel_requested': False})
    tasks = []
    class DeferredThread:
        def __init__(self, *, target, args, **kwargs): self.target, self.args = target, args
        def start(self): tasks.append(self)
        def run(self): self.target(*self.args)
    monkeypatch.setattr(sources, 'Thread', DeferredThread)
    yield root, settings, factory, writes, tasks
    engine.dispose()


def video(root, name='SONE-725.mp4'):
    target = root / name; target.write_bytes(b'video-data')
    return target


def test_directory_management_saves_config_without_deleting_files(env, tmp_path):
    root, settings, _, writes, _ = env
    original = video(root)
    second = tmp_path / 'other'; second.mkdir()
    assert len(sources.add_root(str(second))) == 2
    sources.add_root(str(second / '.'))
    assert len(writes) == 1
    sources.remove_root(str(root))
    assert settings.scrape_source_dirs == str(second)
    assert original.read_bytes() == b'video-data'
    with pytest.raises(ValueError): sources.add_root('relative')
    with pytest.raises(ValueError): sources.add_root(str(tmp_path / 'missing'))
    with pytest.raises(ValueError): sources.add_root(str(Path(settings.medialink_scrape_dir).parent))


def test_video_listing_filters_paginates_and_never_scrapes(env, monkeypatch):
    root, _, db, _, _ = env
    first = video(root)
    unknown = video(root, 'random-name.mkv')
    video(root, 'SONE-725-trailer.mp4')
    (root / 'readme.txt').write_text('text')
    scrapeinventory.refresh_now()
    with db() as session:
        session.add(Code(code='SONE-725', title='已知标题')); session.commit()
    monkeypatch.setattr(scrape, 'scrape_group', lambda *a, **kw: pytest.fail('列表不能刮削'))
    result = sources.list_videos(size=1)
    assert result['total'] == 2 and len(result['items']) == 1
    assert sources.list_videos(status='unrecognized')['items'][0]['path'] == str(unknown)
    assert sources.list_videos(keyword='已知标题')['items'][0]['path'] == str(first)
    assert sources.list_videos(page=2, size=1)['items'][0]['path'] != result['items'][0]['path']
    with pytest.raises(ValueError): sources.list_videos(root='not-configured')


def test_listing_shows_complete_missing_failed_and_interrupted(env):
    root, settings, db, _, _ = env
    original = video(root)
    target = Path(settings.medialink_scrape_dir) / 'SONE-725.mp4'
    target.parent.mkdir(); os.link(original, target)
    nfo.write(target.with_suffix('.nfo'), nfo.NfoData(code='SONE-725', title='标题'))
    from io import BytesIO
    from PIL import Image
    image = BytesIO(); Image.new('RGB', (100, 100)).save(image, 'JPEG')
    for kind in ('poster', 'fanart', 'thumb'): target.with_name(f'{target.stem}-{kind}.jpg').write_bytes(image.getvalue())
    with db() as session:
        session.add(MediaLink(code='SONE-725', source_path=str(original), link_path=str(target))); session.commit()
    scrapeinventory.refresh_now()
    assert sources.list_videos()['items'][0]['status'] == 'complete'
    target.with_name(f'{target.stem}-poster.jpg').unlink()
    scrapeinventory.refresh_now()
    assert sources.list_videos()['items'][0]['status'] == 'incomplete'
    sources.record_attempt([{'path': str(original)}], 'SONE-725', status='failed', error='网络失败')
    row = sources.list_videos()['items'][0]
    assert row['status'] == 'failed' and row['error'] == '网络失败'
    sources.record_attempt([{'path': str(original)}], 'SONE-725', status='running', job_id='old-process')
    assert sources.list_videos()['items'][0]['status'] == 'interrupted'


def test_manual_codes_apply_per_video_and_parts_are_grouped(env):
    root, _, _, _, tasks = env
    first = video(root, 'unknown-A.mp4')
    second = video(root, 'unknown-B.mp4')
    third = video(root, 'another.mp4')
    got = sources.start([{'path': str(first), 'code': 'SONE-725'}, {'path': str(second), 'code': 'SONE-725'},
                         {'path': str(third), 'code': 'SSIS-001'}])
    groups = tasks[0].args[0]
    assert got['total'] == 2 and got['running']
    assert [x.part for x in groups['SONE-725']] == [1, 2]
    assert groups['SSIS-001'][0].path == third
    assert not list(root.parent.glob('library/**/*'))
    scrapeinventory.refresh_now()
    assert sources.list_videos(keyword='unknown-A')['items'][0]['code'] == 'SONE-725'
    with pytest.raises(ValueError, match='已有手动刮削'): sources.start([{'path': str(first), 'code': 'SONE-725'}])


def test_manual_queue_rejects_unknown_invalid_outside_empty_and_downloading(env, tmp_path):
    root, _, db, _, _ = env
    unknown = video(root, 'unknown.mp4')
    with pytest.raises(ValueError, match='有效番号'): sources.start([{'path': str(unknown)}])
    with pytest.raises(ValueError, match='有效番号'): sources.start([{'path': str(unknown), 'code': 'bad'}])
    outside = video(tmp_path, 'SONE-725.mp4')
    with pytest.raises(ValueError, match='源目录'): sources.start([{'path': str(outside)}])
    empty = root / 'SONE-726.mp4'; empty.touch()
    with pytest.raises(ValueError, match='空文件'): sources.start([{'path': str(empty)}])
    known = video(root)
    with db() as session:
        session.add(Code(code='SONE-725', status=CodeStatus.DOWNLOADING)); session.commit()
    with pytest.raises(ValueError, match='下载中'): sources.start([{'path': str(known)}])
    scrapeinventory.refresh_now()
    assert sources.list_videos(keyword='SONE-725')['items'][0]['status'] == 'downloading'


def test_queue_rejects_symlink_escape(env, tmp_path):
    root, _, _, _, _ = env
    outside = video(tmp_path)
    link = root / 'SONE-725.mp4'
    try: link.symlink_to(outside)
    except OSError: pytest.skip('当前环境不允许建立符号链接')
    scrapeinventory.refresh_now()
    assert sources.list_videos()['total'] == 0
    with pytest.raises(ValueError): sources.start([{'path': str(link)}])


def test_failed_group_does_not_block_remaining_files_and_records_survive(env, monkeypatch):
    root, _, db, _, tasks = env
    first = video(root); second = video(root, 'SONE-726.mp4')
    def run(code, items, **options):
        assert options['overwrite'] and options['refresh_metadata']
        if code == 'SONE-725': raise OSError('模拟失败')
        return scrape.ScrapeResult(code=code, links=[items[0].path], nfo_written=1)
    monkeypatch.setattr(scrape, 'scrape_group', run)
    sources.start([{'path': str(first)}, {'path': str(second)}], overwrite=True, refresh_metadata=True)
    tasks[0].run()
    result = sources.state()
    assert not result['running'] and result['done'] == 2 and result['ok'] == 1
    with db() as session:
        assert session.get(ScrapeAttempt, sources.key(first)).status == 'failed'
        assert session.get(ScrapeAttempt, sources.key(second)).status == 'succeeded'


def test_force_queue_enables_network_overwrite_and_propagates_force(env, monkeypatch):
    root, _, _, _, tasks = env
    path = video(root)
    calls = []
    def run(code, items, **options):
        calls.append(options)
        return scrape.ScrapeResult(code=code, links=[items[0].path])
    monkeypatch.setattr(scrape, 'scrape_group', run)
    sources.start([{'path': str(path)}], force=True, fetch_meta=False)
    tasks[0].run()
    assert len(calls) == 1
    assert all(calls[0][key] for key in ('force', 'overwrite', 'refresh_metadata', 'fetch_meta'))


def test_manual_batch_runs_two_movies_at_a_time(env, monkeypatch):
    root, _, _, _, tasks = env
    paths = [video(root, f'SONE-{number}.mp4') for number in (725, 726, 727)]
    guard, barrier = threading.Lock(), threading.Barrier(2)
    counts = {'running': 0, 'peak': 0, 'started': 0}
    def run(code, items, **options):
        with guard:
            counts['running'] += 1
            counts['started'] += 1
            ordinal = counts['started']
            counts['peak'] = max(counts['peak'], counts['running'])
        try:
            if ordinal <= 2:
                barrier.wait(timeout=2)
            return scrape.ScrapeResult(code=code, links=[items[0].path])
        finally:
            with guard:
                counts['running'] -= 1
    monkeypatch.setattr(scrape, 'scrape_group', run)
    sources.start([{'path': str(path)} for path in paths])
    tasks[0].run()
    result = sources.state()
    assert counts['peak'] == 2 and result['done'] == result['ok'] == 3


def test_cancel_skips_queued_work_and_keeps_source_files(env, monkeypatch):
    root, _, db, _, tasks = env
    original = video(root)
    monkeypatch.setattr(scrape, 'scrape_group', lambda *a, **kw: pytest.fail('已取消的任务不能刮削'))
    sources.start([{'path': str(original)}])
    sources.cancel(); tasks[0].run()
    assert original.exists() and sources.state()['done'] == 0 and not sources.state()['running']
    with db() as session: assert session.get(ScrapeAttempt, sources.key(original)).status == 'cancelled'


def test_worker_revalidates_root_scope_before_writing(env, monkeypatch):
    root, settings, _, _, tasks = env
    original = video(root)
    sources.start([{'path': str(original)}])
    settings.scrape_source_dirs = ''
    monkeypatch.setattr(scrape, 'scrape_group', lambda *a, **kw: pytest.fail('已撤销的目录不能处理'))
    tasks[0].run()
    assert sources.state()['ok'] == 0 and '源目录' in sources.state()['results'][0]['error']


def test_overwrite_is_per_run_and_preserves_existing_target(env, monkeypatch):
    root, settings, db, _, tasks = env
    original = video(root)
    old_target = Path(settings.medialink_scrape_dir) / 'original-position' / 'old-name.mp4'
    old_target.parent.mkdir(parents=True); os.link(original, old_target)
    nfo.write(old_target.with_suffix('.nfo'), nfo.NfoData(code='SONE-725', title='手动旧标题'))
    with db() as session:
        session.add(Code(code='SONE-725'))
        session.add(MediaLink(code='SONE-725', source_path=str(original), link_path=str(old_target)))
        session.commit()
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'title': '重刮新标题'})
    monkeypatch.setattr(scrape, '_cover_bytes', lambda *a, **kw: b'')
    from app.services import medialink
    monkeypatch.setattr(medialink, '_fetch_subtitle_quietly', lambda *a: None)
    monkeypatch.setattr(medialink, '_generate_review_quietly', lambda *a: None)
    sources.start([{'path': str(original)}], overwrite=True, fetch_meta=False)
    tasks[0].run()
    assert sources.state()['ok'] == 1
    assert local.read(old_target.with_suffix('.nfo'), 'SONE-725')['title'] == '重刮新标题'
    assert old_target.samefile(original) and not settings.scrape_overwrite
    assert list(Path(settings.medialink_scrape_dir).rglob('*.mp4')) == [old_target]


def test_refresh_updates_returned_fields_and_preserves_status_and_translation(env, monkeypatch):
    _, _, db, _, _ = env
    with db() as session:
        session.add(Code(code='SONE-725', title='旧标题', cn_title='手动译文', casts='旧演员', genres='旧标签',
            release_date='2025-01-01', duration='120', banner='https://old.invalid/cover.jpg', local_banner='old.jpg', status=CodeStatus.COMPLETED))
        session.commit()
    monkeypatch.setattr(merge, 'fetch_merged', lambda code: {'title': '新标题', 'casts': '新演员', 'banner': 'https://new.invalid/cover.jpg', 'status': 0})
    got = scrape._load_meta('SONE-725', refresh=True)
    assert got['title'] == '新标题' and got['casts'] == '新演员' and got['cn_title'] == '手动译文'
    with db() as session:
        row = session.get(Code, 'SONE-725')
        assert row.status == CodeStatus.COMPLETED and row.genres == '旧标签' and row.local_banner == ''
    monkeypatch.setattr(merge, 'fetch_merged', lambda code: {})
    got = scrape._load_meta('SONE-725', refresh=True)
    assert got['_refresh_failed'] and got['title'] == '新标题'


def test_auto_scan_retries_saved_manual_number(env, monkeypatch):
    root, settings, _, _, _ = env
    settings.scrape_mode = 'builtin'
    unknown = video(root, 'cannot-detect.mp4')
    os.utime(unknown, (time.time() - 500, time.time() - 500))
    sources.record_attempt([{'path': str(unknown)}], 'SONE-725', status='failed', error='此前失败')
    monkeypatch.setattr(scrapewatch, '_OBSERVED', {})
    monkeypatch.setattr(scrapewatch, '_RETRY', {})
    now = [1000.0]; monkeypatch.setattr(scrapewatch.time, 'monotonic', lambda: now[0])
    calls = []
    monkeypatch.setattr(scrape, 'scrape_group', lambda code, items, **kw: calls.append(code) or scrape.ScrapeResult(code=code, error='offline'))
    scrapewatch.scan(); now[0] += 130; scrapewatch.scan()
    assert calls == ['SONE-725']


def test_source_api_auth_and_invalid_inputs(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints.scrape import router
    root, _, _, _, _ = env
    original = video(root, 'unknown.mp4')
    scrapeinventory.refresh_now()
    app = FastAPI(); app.include_router(router)
    client = TestClient(app)
    assert client.get('/scrape/videos').status_code == 401
    assert client.get('/scrape/videos/detail', params={'path': str(original)}).status_code == 401
    assert client.post('/scrape/videos/refresh').status_code == 401
    assert client.post('/scrape/jobs', json={'items': [{'path': str(original), 'code': 'SONE-725'}]}).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    assert client.get('/scrape/videos').json()['data']['total'] == 1
    assert client.get('/scrape/videos/detail', params={'path': str(original)}).json()['data']['metadata'] == {}
    assert client.get('/scrape/videos/detail', params={'path': str(root / 'missing.mp4')}).status_code == 404
    assert client.post('/scrape/jobs', json={'items': []}).status_code == 422
    assert client.post('/scrape/jobs', json={'items': [{'path': str(original)}]}).status_code == 400
    assert client.post('/scrape/jobs', json={'items': [{'path': str(original), 'code': 'SONE-725'}]}).json()['data']['running']


def test_database_pagination_search_and_status_do_not_touch_disk(env, monkeypatch):
    root, _, db, _, _ = env
    original = video(root)
    video(root, 'unknown.mkv')
    scrapeinventory.refresh_now()
    with db() as session:
        session.add(Code(code='SONE-725', title='数据库标题')); session.commit()
    def forbidden(*args, **kwargs):
        pytest.fail('读取清单不能访问文件系统或重新解析文件')
    with monkeypatch.context() as check:
        check.setattr(Path, 'stat', forbidden)
        check.setattr(Path, 'resolve', forbidden)
        check.setattr(sources.os, 'walk', forbidden)
        check.setattr(sources.mediafile, 'parse', forbidden)
        check.setattr(scrapewatch, '_complete', forbidden)
        assert sources.list_videos(size=1)['total'] == 2
        assert len(sources.list_videos(size=1, page=2)['items']) == 1
        assert sources.list_videos(keyword='数据库标题')['items'][0]['path'] == str(original)
        assert sources.list_videos(status='unrecognized')['total'] == 1


def test_inventory_survives_process_state_reset_and_syncs_file_changes(env, monkeypatch):
    root, _, _, _, _ = env
    original = video(root)
    scrapeinventory.refresh_now()
    monkeypatch.setattr(scrapeinventory, '_STATE', {'running': False, 'done': 0, 'files': 0, 'total': 0,
        'current': '', 'errors': [], 'finished_at': ''})
    second = video(root, 'SONE-726.mp4')
    assert sources.list_videos()['total'] == 1  # 新文件只在扫描时纳入。
    assert not sources.list_videos()['needs_refresh']
    scrapeinventory.refresh_now()
    assert sources.list_videos()['total'] == 2
    original.unlink()
    assert sources.list_videos()['total'] == 2  # 页面查询不会自动扫盘。
    scrapeinventory.refresh_now()
    result = sources.list_videos()
    assert result['total'] == 1 and result['items'][0]['path'] == str(second)


def test_offline_root_preserves_inventory_and_reports_error(env, monkeypatch):
    root, _, _, _, _ = env
    video(root)
    scrapeinventory.refresh_now()
    def unavailable(path):
        raise OSError('挂载不可访问')
    monkeypatch.setattr(sources, '_root', unavailable)
    scrapeinventory.refresh_now()
    result = sources.list_videos()
    assert result['total'] == 1
    assert not result['roots'][0]['available'] and '挂载不可访问' in result['warnings'][0]


def test_partial_directory_scan_does_not_drop_unseen_rows(env, monkeypatch):
    root, _, _, _, _ = env
    video(root); video(root, 'SONE-726.mp4')
    scrapeinventory.refresh_now()
    def partial(folder, *, onerror, **kwargs):
        onerror(PermissionError(13, 'permission denied', str(root / 'subdir')))
        yield str(root), [], ['SONE-725.mp4']
    monkeypatch.setattr(scrapeinventory.os, 'walk', partial)
    scrapeinventory.refresh_now()
    result = sources.list_videos()
    assert result['total'] == 2 and result['warnings']


def test_overlapping_roots_share_video_without_duplicate_rows(env):
    root, settings, _, _, _ = env
    child = root / 'nested'; child.mkdir()
    original = video(child)
    settings.scrape_source_dirs = f'{root},{child}'
    scrapeinventory.refresh_now()
    assert sources.list_videos()['total'] == 1
    assert sources.list_videos(root=str(child))['items'][0]['path'] == str(original)
    settings.scrape_source_dirs = str(child)
    assert sources.list_videos()['total'] == 1
    settings.scrape_source_dirs = ''
    assert sources.list_videos()['total'] == 0 and original.exists()


def test_old_scan_cannot_overwrite_new_scrape_result(env):
    root, _, db, _, _ = env
    original = video(root)
    scrapeinventory.refresh_now()
    with db() as session:
        row = session.get(ScrapeSourceVideo, sources.key(original))
        old = row.to_dict()
        old['indexed_at'] = datetime.now() - timedelta(minutes=10)
        old['artifact_status'] = 'pending'
        row.artifact_status = 'complete'
        row.links = ['/library/new.mp4']
        row.indexed_at = datetime.now()
        session.commit()
    scrapeinventory._flush(sources.key(root), [old], old['indexed_at'])
    assert sources.list_videos()['items'][0]['status'] == 'complete'
    assert sources.list_videos()['items'][0]['links'] == ['/library/new.mp4']


def test_attempt_updates_are_visible_without_rescan(env, monkeypatch):
    root, _, _, _, _ = env
    original = video(root, 'unknown.mp4')
    scrapeinventory.refresh_now()
    sources.record_attempt([{'path': str(original)}], 'SONE-725', status='failed', error='请求失败')
    monkeypatch.setattr(scrapeinventory.os, 'walk', lambda *a, **kw: pytest.fail('任务状态更新后不应扫目录'))
    row = sources.list_videos()['items'][0]
    assert row['code'] == 'SONE-725' and row['status'] == 'failed' and row['error'] == '请求失败'


def test_direct_scrape_updates_cached_artifacts_without_directory_scan(env, monkeypatch):
    root, _, db, _, _ = env
    original = video(root)
    scrapeinventory.refresh_now()
    with db() as session:
        session.add(Code(code='SONE-725')); session.commit()
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'title': '标题'})
    monkeypatch.setattr(scrape, '_cover_bytes', lambda *a, **kw: b'')
    from app.services import medialink
    monkeypatch.setattr(medialink, '_fetch_subtitle_quietly', lambda *a: None)
    monkeypatch.setattr(medialink, '_generate_review_quietly', lambda *a: None)
    monkeypatch.setattr(scrapeinventory.os, 'walk', lambda *a, **kw: pytest.fail('刮削后只更新对应记录'))
    assert scrape.scrape_file(original, fetch_meta=False).ok
    row = sources.list_videos()['items'][0]
    assert row['links'] and row['status'] == 'incomplete'  # 无封面时仍准确显示产物缺项。


def test_video_detail_reads_saved_manual_metadata_without_disk_or_scraping(env, monkeypatch):
    root, settings, db, _, _ = env
    original = video(root, 'unknown.mp4')
    scrapeinventory.refresh_now()
    sources.record_attempt([{'path': str(original)}], 'SONE-725', status='failed', error='图片失败')
    with db() as session:
        session.add(Code(code='SONE-725', title='已保存标题', casts='演员甲,演员乙', outline='简介'))
        session.commit()
    original.unlink()
    monkeypatch.setattr(scrape, '_load_meta', lambda *a, **kw: pytest.fail('详情不能触发联网刮削'))
    monkeypatch.setattr(Path, 'stat', lambda *a, **kw: pytest.fail('详情不能探测源盘'))
    monkeypatch.setattr(Path, 'resolve', lambda *a, **kw: pytest.fail('详情不能探测源盘'))
    detail = sources.video_detail(str(original))
    assert detail['code'] == 'SONE-725'
    assert detail['metadata']['casts'] == '演员甲,演员乙'
    assert detail['metadata']['title'] == '已保存标题'
    assert detail['error'] == '图片失败'
    assert detail['path'] == str(original)
    settings.scrape_source_dirs = ''
    with pytest.raises(ValueError): sources.video_detail(str(original))


def test_video_detail_api_returns_metadata_and_existing_image_routes(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints import scrape as endpoint
    root, settings, db, _, _ = env
    original = video(root)
    scrapeinventory.refresh_now()
    with db() as session:
        session.add(Code(code='SONE-725', cn_title='已保存标题', local_banner='cover/sone.jpg'))
        session.commit()
    monkeypatch.setattr(endpoint, 'get_settings', lambda: settings)
    app = FastAPI(); app.include_router(endpoint.router)
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    response = TestClient(app).get('/scrape/videos/detail', params={'path': str(original)})
    assert response.status_code == 200
    result = response.json()['data']
    assert result['metadata']['cn_title'] == '已保存标题'
    assert result['images']['poster'].startswith('/api/v1/image-local?')
    assert result['code'] == 'SONE-725'


def test_medialink_detail_works_without_source_inventory_or_files(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints import medialink as endpoint
    root, settings, db, _, _ = env
    source = str(root / 'already-gone.mp4')
    target = str(Path(settings.medialink_scrape_dir) / 'SONE-725.mp4')
    with db() as session:
        session.add(MediaLink(code='SONE-725', source_path=source, link_path=target, size=1024))
        session.add(Code(code='SONE-725', title='已入库作品', casts='演员甲'))
        session.commit()
    settings.scrape_source_dirs = ''
    monkeypatch.setattr(endpoint, 'get_settings', lambda: settings)
    app = FastAPI(); app.include_router(endpoint.router)
    client = TestClient(app)
    assert client.get('/medialinks/detail', params={'link_path': target}).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'admin'
    response = client.get('/medialinks/detail', params={'link_path': target})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['metadata']['title'] == '已入库作品'
    assert data['path'] == source
    assert data['links'] == [target]
    assert data['size'] == 1024
    assert client.get('/medialinks/detail', params={'link_path': 'unknown'}).status_code == 404


def test_repair_domain_codes_updates_cache_and_stale_attempts_without_disk(env, monkeypatch):
    root, _, db, _, _ = env
    first = video(root, 'hhd800.com@DASD-875.mp4')
    manual = video(root, 'hhd800.com@200GANA-3282.mp4')
    normal = video(root, 'HHD-800.mp4')
    scrapeinventory.refresh_now()
    for path, code in [(first, 'HHD-800'), (manual, 'SONE-725'), (normal, 'HHD-800')]:
        sources.record_attempt([{'path': str(path)}], code, status='failed', error='旧失败记录')
    with db() as session:
        for path in (first, manual):
            session.get(ScrapeSourceVideo, sources.key(path)).detected_code = 'HHD-800'
        session.commit()
    monkeypatch.setattr(Path, 'stat', lambda *a, **kw: pytest.fail('修复缓存不能探测磁盘'))
    monkeypatch.setattr(Path, 'resolve', lambda *a, **kw: pytest.fail('修复缓存不能探测磁盘'))
    assert scrapeinventory.repair_filename_codes() == 2
    assert scrapeinventory.repair_filename_codes() == 0
    with db() as session:
        assert session.get(ScrapeSourceVideo, sources.key(first)).detected_code == 'DASD-875'
        assert session.get(ScrapeAttempt, sources.key(first)).code == 'DASD-875'
        assert session.get(ScrapeSourceVideo, sources.key(manual)).detected_code == '200GANA-3282'
        assert session.get(ScrapeAttempt, sources.key(manual)).code == 'SONE-725'
        assert session.get(ScrapeAttempt, sources.key(normal)).code == 'HHD-800'


def test_cache_repair_recovers_names_and_skips_ads_preserving_manual_codes(env, monkeypatch):
    root, _, db, _, _ = env
    parent = root / 'SONE-725'; parent.mkdir()
    ad = video(parent, '社 區 最 新 情 報.mp4')
    movie = video(root, 'JUL-915C.mp4')
    cid = video(root, '1dvdes00644hhb4.mp4')
    manual = video(root, 'SONE-982ch.mp4')
    date = video(root, '032517-505-C.mp4')
    scrapeinventory.refresh_now()
    sources.record_attempt([{'path': str(manual)}], 'IPZZ-477-C', status='failed')
    sources.record_attempt([{'path': str(ad)}], 'SONE-725', status='failed')
    with db() as session:
        row = session.get(ScrapeSourceVideo, sources.key(ad)); row.artifact_status = 'pending'; row.detected_code = 'SONE-725'
        session.get(ScrapeSourceVideo, sources.key(movie)).detected_code = ''
        session.get(ScrapeSourceVideo, sources.key(date)).detected_code = '032517_505'
        row = session.get(ScrapeSourceVideo, sources.key(cid)); row.detected_code = 'DVDES-00644'; row.part = 0
        session.commit()
    monkeypatch.setattr(Path, 'stat', lambda *a, **kw: pytest.fail('不能探测磁盘'))
    assert scrapeinventory.repair_filename_codes() == 4
    assert scrapeinventory.repair_filename_codes() == 0
    assert sources.list_videos(status='ignored')['items'][0]['code'] == ''
    assert sources.list_videos(status='unrecognized')['total'] == 0
    assert sources.list_videos(keyword='JUL-915-C')['total'] == 1
    row = sources.list_videos(keyword='1dvdes')['items'][0]
    assert row['code'] == 'DVDES-644' and row['part'] == 4
    assert sources.video_detail(str(manual))['code'] == 'IPZZ-477-C'
    assert sources.video_detail(str(date))['code'] == '032517_505-C'
    with pytest.raises(ValueError): sources.group_entries([{'path': str(ad), 'code': 'SONE-725'}])


def test_repair_clears_only_stale_domain_failure_and_preserves_completed_artifacts(env):
    root, _, db, _, _ = env
    stale = video(root, 'hhd800.com@DASD-875.mp4')
    real = video(root, 'hhd800.com@200GANA-3282.mp4')
    ambiguous = video(root, 'START-377V.2026.1080p.DMM.WEB-DL.mkv')
    scrapeinventory.refresh_now()
    sources.record_attempt([{'path': str(stale)}], 'DASD-875', status='failed', error='取不到元数据: HHD-800')
    sources.record_attempt([{'path': str(real)}], '200GANA-3282', status='failed', error='取不到元数据: 200GANA-3282')
    with db() as session:
        row = session.get(ScrapeSourceVideo, sources.key(stale))
        row.artifact_status = 'complete'; row.links = ['/library/DASD-875.mp4']
        session.get(ScrapeSourceVideo, sources.key(ambiguous)).detected_code = ''
        session.commit()
    assert scrapeinventory.repair_filename_codes() == 2
    assert scrapeinventory.repair_filename_codes() == 0
    with db() as session:
        attempt = session.get(ScrapeAttempt, sources.key(stale))
        assert attempt.code == 'DASD-875' and attempt.status == 'pending' and attempt.error == ''
        assert session.get(ScrapeSourceVideo, sources.key(stale)).artifact_status == 'complete'
        assert session.get(ScrapeAttempt, sources.key(real)).status == 'failed'
        assert session.get(ScrapeSourceVideo, sources.key(ambiguous)).detected_code == 'START-377'
    assert sources.list_videos(keyword='DASD-875')['items'][0]['status'] == 'complete'
    assert sources.list_videos(status='failed')['total'] == 1


def test_repair_drops_old_attempt_failure_when_correcting_domain_number(env):
    root, _, db, _, _ = env
    path = video(root, 'hhd800.com@DASD-875.mp4')
    scrapeinventory.refresh_now()
    sources.record_attempt([{'path': str(path)}], 'HHD-800', status='failed', error='取不到元数据: HHD-800')
    with db() as session:
        session.get(ScrapeSourceVideo, sources.key(path)).detected_code = 'HHD-800'
        session.commit()
    assert scrapeinventory.repair_filename_codes() == 1
    result = sources.list_videos()['items'][0]
    assert result['code'] == 'DASD-875' and result['status'] == 'pending' and result['error'] == ''


@pytest.mark.parametrize('name,wrong,correct', [('1rct00828.mp4','1RCT-00828','RCT-828'),
                                             ('1nhdta00565.mp4','1NHDTA-00565','NHDTA-565')])
def test_repair_old_vendor_product_attempt_and_failure(env, name, wrong, correct):
    root, _, db, _, _ = env
    path = video(root, name)
    scrapeinventory.refresh_now()
    sources.record_attempt([{'path':str(path)}], wrong, status='failed', error=f'取不到元数据: {wrong}')
    with db() as session:
        session.get(ScrapeSourceVideo, sources.key(path)).detected_code = wrong
        session.commit()
    assert scrapeinventory.repair_filename_codes() == 1
    row = sources.list_videos()['items'][0]
    assert row['code'] == correct and row['status'] == 'pending' and row['error'] == ''
