from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.database.base import DBBase
from app.database.models import Code, CodeStatus, History, MediaLink, ScrapeAttempt, ScrapeSourceRoot, ScrapeSourceVideo
from app.services import scrape, scrapeinventory, scrapemaintenance, scraperebuild


@pytest.fixture
def library(tmp_path, monkeypatch):
    import app.database.session as database
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    root, output = tmp_path / 'downloads', tmp_path / 'library'
    root.mkdir(); output.mkdir()
    source = root / 'SONE-725.mp4'
    source.write_bytes(b'original video content')
    os.utime(source, (time.time() - 600, time.time() - 600))
    old_dir = output / 'old-name'; old_dir.mkdir()
    video = old_dir / 'SONE-725.mp4'
    os.link(source, video)
    video.with_suffix('.nfo').write_text('<movie><num>SONE-725</num><title>旧标题</title><plot>旧简介</plot></movie>', encoding='utf-8')
    for kind, color in [('fanart', 'blue'), ('poster', 'green'), ('thumb', 'red')]:
        Image.new('RGB', (40, 60), color).save(video.with_name(video.stem + '-' + kind + '.jpg'))
    stills = old_dir / 'extrafanart'; stills.mkdir()
    Image.new('RGB', (40, 30), 'yellow').save(stills / 'fanart1.jpg')
    settings = Settings(scrape_source_dirs=str(root), medialink_scrape_dir=str(output),
                        scrape_dir_template='{number}/new', scrape_file_template='{number}',
                        scrape_still_limit=1, scrape_notify=False)
    monkeypatch.setattr(scraperebuild, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrapeinventory, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrapemaintenance, '_PROTECTION_FILE', tmp_path / 'protection.json')
    monkeypatch.setattr(scraperebuild, '_STATE_FILE', tmp_path / 'rebuild-state.json')
    monkeypatch.setattr(scraperebuild, '_PLAN', None)
    monkeypatch.setattr(scraperebuild, '_STATE', {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'ok': 0,
        'error': '', 'token': '', 'plan': None, 'quarantine_dir': '', 'results': []})
    with factory() as session:
        session.add(Code(code='SONE-725', title='人工保留标题', casts='演员甲', genres='标签', duration='120',
                         release_date='2020-01-01', banner='https://cover.invalid/cover.jpg', status=CodeStatus.COMPLETED))
        session.add(History(hash='download-hash', code='SONE-725', save_path=str(root)))
        session.add(MediaLink(code='SONE-725', source_path=str(source), link_path=str(video)))
        session.add(ScrapeSourceRoot(root_key=scrapeinventory.key(root), path=str(root), resolved=str(root), available=True))
        session.add(ScrapeSourceVideo(source_key=scrapeinventory.key(source), path=str(source), name=source.name,
            detected_code='SONE-725', size=source.stat().st_size, mtime=source.stat().st_mtime,
            artifact_status='complete', links=[str(video)], indexed_at=datetime.now()))
        session.commit()
    yield dict(root=root, output=output, source=source, video=video, db=factory, settings=settings, tmp=tmp_path)
    deadline = time.monotonic() + 5
    while scraperebuild._STATE['running'] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not scraperebuild._STATE['running']
    assert not scrapemaintenance.busy()
    engine.dispose()


def wait():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = scraperebuild.state()
        if not value['running']:
            return value
        time.sleep(0.01)
    pytest.fail('rebuild worker did not stop')


def preview():
    scraperebuild.preview()
    value = wait()
    assert value['phase'] == 'ready', value
    return value


def test_preview_is_read_only_and_validates_independent_download(library):
    value = preview()
    assert value['plan']['video_count'] == 1
    assert value['plan']['source_count'] == 1
    assert value['plan']['blocked_count'] == 0
    assert library['video'].exists() and library['source'].exists()
    assert not value['quarantine_dir']
    assert not value['deletion_protected']


def test_quarantine_stays_outside_entire_configured_playback_tree(library, monkeypatch):
    # A scrape subdirectory can live inside a broader Emby playback root.
    # Storing old videos beside that subdirectory would keep them playable.
    playback = library['tmp'] / 'playback'
    playback.mkdir()
    output = playback / 'scraped'
    library['output'].rename(output)
    library['settings'].medialink_scrape_dir = str(output)
    library['settings'].medialink_library_path = str(playback)
    plan = scraperebuild._collect()
    assert not plan['public']['blocked_count']
    assert not Path(plan['quarantine_parent']).is_relative_to(playback)


def test_blocks_unique_library_copy_even_when_database_claims_link(library):
    library['source'].unlink()
    value = preview()
    assert value['plan']['blocked_count'] >= 1
    assert any('独立下载源' in item for item in value['plan']['blockers'])
    with pytest.raises(ValueError, match='无法安全'):
        scraperebuild.start(value['token'], str(library['output']))
    assert library['video'].read_bytes() == b'original video content'


def test_history_source_outside_configured_roots_is_available(library):
    historical = library['tmp'] / 'old-downloads'; historical.mkdir()
    source = historical / 'SONE-725.mp4'
    os.link(library['source'], source)
    with library['db']() as session:
        session.get(MediaLink, str(library['video'])).source_path = str(source)
        session.delete(session.get(ScrapeSourceVideo, scrapeinventory.key(library['source'])))
        session.commit()
    value = preview()
    assert value['plan']['source_count'] == 1 and value['plan']['blocked_count'] == 0
    assert scraperebuild._PLAN['sources'][0]['origin'] == '历史关联源'


def test_hash_proves_independent_copy_without_same_inode(library):
    content = library['video'].read_bytes()
    library['video'].unlink()
    library['video'].write_bytes(content)
    value = preview()
    assert value['plan']['blocked_count'] == 0
    assert scraperebuild._PLAN['evidence'][0]['method'] == 'sha256'


def test_same_size_different_bytes_is_blocked(library):
    size = library['video'].stat().st_size
    library['video'].unlink()
    library['video'].write_bytes(b'x' * size)
    value = preview()
    assert value['plan']['blocked_count']


def test_configuration_overlap_and_root_are_rejected(library):
    library['settings'].medialink_scrape_dir = str(library['root'])
    scraperebuild.preview()
    assert wait()['phase'] == 'failed'
    library['settings'].medialink_scrape_dir = str(Path(library['root'].anchor))
    scraperebuild.preview()
    assert wait()['phase'] == 'failed'


def test_unfinished_download_is_not_a_rebuild_source(library):
    with library['db']() as session:
        session.get(Code, 'SONE-725').status = CodeStatus.DOWNLOADING
        session.commit()
    assert preview()['plan']['blocked_count']


def test_rechecks_output_change_before_any_move(library, monkeypatch):
    value = preview()
    extra = library['output'] / 'arrived-after-preview.txt'
    extra.write_text('retain', encoding='utf-8')
    scraperebuild.start(value['token'], str(library['output']))
    result = wait()
    assert result['phase'] == 'failed' and '已变化' in result['error']
    assert library['video'].exists() and extra.exists()
    assert not result['quarantine_dir']


def test_rechecks_database_and_requires_exact_confirmation(library):
    value = preview()
    with pytest.raises(ValueError, match='完整输出目录'):
        scraperebuild.start(value['token'], 'yes')
    with library['db']() as session:
        session.get(Code, 'SONE-725').title = 'changed after preview'
        session.commit()
    scraperebuild.start(value['token'], str(library['output']))
    assert wait()['phase'] == 'failed'
    assert library['video'].exists()


@pytest.mark.parametrize('source_code', ['SONE-725', 'SONE-725-C'])
def test_rebuild_retains_metadata_download_and_old_artwork(library, monkeypatch, source_code):
    from app.modules.scrape import merge
    if source_code.endswith('-C'):
        # Disabling new still downloads must not discard retained photographs.
        library['settings'].scrape_still_limit = 0
    with library['db']() as session:
        session.get(ScrapeSourceVideo, scrapeinventory.key(library['source'])).detected_code = source_code
        session.add(ScrapeAttempt(source_key=scrapeinventory.key(library['source']), source_path=str(library['source']),
                                 code=source_code, status='failed', error='old failed scrape', job_id='old'))
        session.commit()
    monkeypatch.setattr(merge, 'fetch_merged', lambda *a, **kw: pytest.fail('existing database metadata should be reused'))
    monkeypatch.setattr(scrape, '_download', lambda *a, **kw: pytest.fail('preserved artwork must be reused'))
    value = preview()
    scraperebuild.start(value['token'], str(library['output']))
    result = wait()
    assert result['phase'] == 'completed', result
    assert result['ok'] == 1
    target = library['output'] / source_code / 'new' / f'{source_code}.mp4'
    assert target.samefile(library['source'])
    assert not library['video'].exists()
    quarantine = Path(result['quarantine_dir'])
    old = quarantine / 'library' / 'old-name' / 'SONE-725.mp4'
    assert old.samefile(library['source'])
    assert (quarantine / 'manifest.json').is_file()
    assert result['deletion_protected']
    with library['db']() as session:
        code = session.get(Code, 'SONE-725')
        assert code.title == '人工保留标题' and code.outline == '旧简介'
        assert session.get(History, 'download-hash')
        assert session.get(MediaLink, str(library['video'])) is None
        assert session.get(MediaLink, str(target))
        attempt = session.get(ScrapeAttempt, scrapeinventory.key(library['source']))
        assert attempt.status == 'succeeded' and not attempt.error
        assert attempt.job_id == result['job_id']
    assert target.with_suffix('.nfo').is_file()
    assert any((target.parent / 'extrafanart').glob('*.jpg'))
    for kind in ('poster', 'thumb', 'fanart'):
        with Image.open(target.with_name(f'{target.stem}-{kind}.jpg')) as current, Image.open(old.with_name(f'{old.stem}-{kind}.jpg')) as previous:
            assert current.size == previous.size
            assert all(abs(a - b) < 5 for a, b in zip(current.getpixel((10, 10)), previous.getpixel((10, 10)))), (kind, current.getpixel((10, 10)), previous.getpixel((10, 10)))


def test_failed_movie_keeps_quarantine_and_never_restores_old_tree(library, monkeypatch):
    monkeypatch.setattr(scrape, 'scrape_group', lambda *a, **kw: scrape.ScrapeResult(code='SONE-725', error='offline'))
    value = preview()
    scraperebuild.start(value['token'], str(library['output']))
    result = wait()
    assert result['phase'] == 'partial' and result['ok'] == 0
    assert not library['video'].exists()
    assert list(library['output'].iterdir()) == []
    assert (Path(result['quarantine_dir']) / 'library' / 'old-name' / 'SONE-725.mp4').is_file()
    assert library['source'].exists()


def test_expired_preview_cannot_start(library):
    value = preview()
    scraperebuild._PLAN['issued'] -= 3601
    with pytest.raises(ValueError, match='过期'):
        scraperebuild.start(value['token'], str(library['output']))


def test_restart_exposes_interrupted_quarantine(library):
    saved = {'running': True, 'phase': 'quarantine', 'done': 0, 'total': 1, 'ok': 0,
             'error': '', 'quarantine_dir': '/retained/quarantine', 'job_id': 'retained-job'}
    scraperebuild._STATE_FILE.write_text(json.dumps(saved), encoding='utf-8')
    value = scraperebuild.state()
    assert value['phase'] == 'interrupted' and not value['running']
    assert value['quarantine_dir'] == '/retained/quarantine'
    assert '重启' in value['error']


def add_source(case, name, content, code):
    path = case['root'] / name
    path.write_bytes(content)
    os.utime(path, (time.time() - 600, time.time() - 600))
    with case['db']() as session:
        session.add(ScrapeSourceVideo(source_key=scrapeinventory.key(path), path=str(path), name=path.name,
            detected_code=code, size=path.stat().st_size, mtime=path.stat().st_mtime,
            artifact_status='pending', links=[], indexed_at=datetime.now()))
        session.commit()
    return path


def test_identical_independent_downloads_do_not_become_fake_parts(library):
    duplicate = add_source(library, 'copy-SONE-725.mp4', library['source'].read_bytes(), 'SONE-725')
    value = preview()
    assert value['plan']['source_count'] == 1
    assert value['plan']['deduplicated_count'] == 1
    assert value['plan']['blocked_count'] == 0
    assert duplicate.exists() and library['source'].exists()


def test_different_download_contents_are_not_discarded(library):
    add_source(library, 'other-SONE-725.mp4', b'x' * library['source'].stat().st_size, 'SONE-725')
    value = preview()
    assert value['plan']['source_count'] == 2
    assert value['plan']['deduplicated_count'] == 0


def test_source_version_receives_correct_old_artwork(library, monkeypatch):
    with library['db']() as session:
        session.get(ScrapeSourceVideo, scrapeinventory.key(library['source'])).detected_code = 'SONE-725-C'
        session.commit()
    captured = []

    def rebuild(code, items, **kwargs):
        captured.append((code, kwargs['preserved_meta']))
        return scrape.ScrapeResult(code=code, error='test ends after preserving images')

    monkeypatch.setattr(scrape, 'scrape_group', rebuild)
    value = preview()
    scraperebuild.start(value['token'], str(library['output']))
    wait()
    assert captured[0][0] == 'SONE-725-C'
    assert Path(captured[0][1]['_local_poster']).is_file()
    assert captured[0][1]['_local_by_part']['0']['_local_poster'] == captured[0][1]['_local_poster']


def test_rebuild_prepares_two_films_concurrently(library, monkeypatch):
    add_source(library, 'SONE-726.mp4', b'other film', 'SONE-726')
    barrier = threading.Barrier(2)
    calls = []

    def rebuild(code, items, **kwargs):
        with scrapemaintenance.activity():
            calls.append(code)
            barrier.wait(timeout=2)
        return scrape.ScrapeResult(code=code, error='intentional test stop')

    monkeypatch.setattr(scrape, 'scrape_group', rebuild)
    value = preview()
    scraperebuild.start(value['token'], str(library['output']))
    result = wait()
    assert result['done'] == 2
    assert len(calls) == 2
    assert all('intentional' in row['error'] for row in result['results'])


def test_old_nfo_replaces_only_placeholder_database_title(library, monkeypatch):
    with library['db']() as session:
        session.get(Code, 'SONE-725').title = 'SONE-725'
        session.commit()
    monkeypatch.setattr(scrape, 'scrape_group', lambda *a, **kw: scrape.ScrapeResult(code='SONE-725', error='test'))
    value = preview()
    scraperebuild.start(value['token'], str(library['output']))
    wait()
    with library['db']() as session:
        assert session.get(Code, 'SONE-725').title == '旧标题'


def test_preview_detects_reparse_point_without_following_it(library, monkeypatch):
    original = scraperebuild._unsafe_links
    monkeypatch.setattr(scraperebuild, '_unsafe_links', lambda path: path == library['video'] or original(path))
    value = preview()
    assert value['plan']['blocked_count'] >= 1
    assert any('符号链接' in message for message in value['plan']['blockers'])


def test_registered_number_rebuilds_filename_without_number_offline(library, monkeypatch):
    from app.modules.scrape import merge
    source = library['root'] / '蒼井そら、無碼流出版.mp4'
    library['source'].rename(source)
    with library['db']() as session:
        session.get(MediaLink, str(library['video'])).source_path = str(source)
        cached = session.get(ScrapeSourceVideo, scrapeinventory.key(library['source']))
        session.delete(cached)
        session.add(ScrapeSourceVideo(source_key=scrapeinventory.key(source), path=str(source), name=source.name,
            detected_code='SONE-725', size=source.stat().st_size, mtime=source.stat().st_mtime,
            artifact_status='complete', links=[str(library['video'])], indexed_at=datetime.now()))
        session.commit()
    monkeypatch.setattr(merge, 'fetch_merged', lambda *a, **kw: pytest.fail('metadata already exists'))
    monkeypatch.setattr(scrape, '_download', lambda *a, **kw: pytest.fail('old pictures already exist'))
    import app.modules.downloadclient as downloads
    monkeypatch.setattr(downloads, 'all_torrent_files_with_hashes', lambda: pytest.fail('verified number needs no downloader lookup'))
    value = preview()
    scraperebuild.start(value['token'], str(library['output']))
    result = wait()
    assert result['phase'] == 'completed', result
    target = library['output'] / 'SONE-725' / 'new' / 'SONE-725.mp4'
    assert target.samefile(source)
