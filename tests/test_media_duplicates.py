from datetime import datetime, timedelta
import os
import time

import pytest

from app.core.config import Settings
from app.database.models import MediaLink, MediaMerge, History, ScrapeSourceVideo
from app.services import mediaduplicate, medialink, scrape, scrapeinventory
from app.utils.mediafile import parse
from tests.test_scrape_reliability import db


@pytest.fixture
def library(tmp_path, monkeypatch, db):
    root, download = tmp_path / 'library', tmp_path / 'downloads'
    root.mkdir(); download.mkdir()
    settings = Settings(medialink_library_path=str(root), medialink_scrape_dir=str(root),
                        scrape_source_dirs=str(download))
    monkeypatch.setattr(mediaduplicate, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrapeinventory, 'get_settings', lambda: settings)
    return root, download


def register(db, target, source=None, code='MEYD-568'):
    with db() as session:
        session.add(MediaLink(link_path=str(target), source_path=str(source or target), code=code))
        session.commit()


def test_scrape_blocks_existing_other_file_before_fetch_or_write(library, db, monkeypatch):
    root, download = library
    old, source = root / 'MEYD-568-C.mp4', download / 'MEYD-568.mp4'
    old.write_bytes(b'original'); source.write_bytes(b'new copy')
    register(db, old)
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: pytest.fail('查重必须早于获取元数据'))
    result = scrape.scrape_group('MEYD-568', [parse(source)])
    assert not result.ok and '已存在' in result.error
    assert old.read_bytes() == b'original' and source.exists()
    assert len(list(root.iterdir())) == 1


def test_same_file_reuses_existing_actor_directory(library, db):
    root, download = library
    old, source = root / 'MEYD-568-C.mp4', download / 'MEYD-568.mp4'
    source.write_bytes(b'video')
    os.link(source, old)
    register(db, old, source)
    targets, conflicts = mediaduplicate.check_existing('MEYD-568', [parse(source)])
    assert not conflicts and targets[str(source)] == old


def test_other_parts_and_uncensored_version_do_not_block(library, db):
    root, download = library
    part, uncensored = root / 'MEYD-568-CD1.mp4', root / 'MEYD-568-U.mp4'
    part.write_bytes(b'a'); uncensored.write_bytes(b'b')
    register(db, part); register(db, uncensored, code='MEYD-568-U')
    source = download / 'MEYD-568-CD2.mp4'; source.write_bytes(b'c')
    assert mediaduplicate.check_existing('MEYD-568', [parse(source)]) == ({}, [])


def test_merge_preserves_download_history_subtitles_and_late_delete_callback(library, db):
    root, download = library
    first, second = root / 'old' / 'MEYD-568.mp4', root / 'new' / 'MEYD-568.mp4'
    first.parent.mkdir(); second.parent.mkdir()
    source = download / 'MEYD-568.mp4'; source.write_bytes(b'video')
    first.write_bytes(b'video'); os.link(source, second)
    register(db, first); register(db, second, source)
    first.with_suffix('.chs.srt').write_text('first subtitle')
    second.with_suffix('.chs.srt').write_text('second subtitle')
    second.with_suffix('.nfo').write_text('<movie/>')
    with db() as session:
        session.add(History(hash='test-hash', code='MEYD-568'))
        session.add(ScrapeSourceVideo(source_key='source', path=str(source), name=source.name,
                    links=[str(second)], indexed_at=datetime.now()))
        session.commit()
    plan = mediaduplicate.preview('MEYD-568', str(first))
    assert first.exists() and second.exists() and plan['duplicates'][0]['path'] == str(second)
    result = mediaduplicate.merge('MEYD-568', str(first), plan['token'])
    assert result['removed'] == [str(second)]
    assert first.read_bytes() == source.read_bytes() == b'video'
    assert not second.exists() and second.with_suffix('.nfo').exists()
    assert first.with_suffix('.chs.srt').read_text() == 'first subtitle'
    assert any(p.read_text() == 'second subtitle' for p in first.parent.glob('*.srt'))
    with db() as session:
        assert session.get(History, 'test-hash')
        assert session.get(MediaLink, str(first)).source_path == str(source)
        assert session.get(MediaLink, str(second)) is None
        assert session.get(MediaMerge, str(second)).keep_path == str(first)
        assert session.get(ScrapeSourceVideo, 'source').links == [str(first)]
    # 文件名相同也不能让旧位置的回调匹配到保留项。
    assert medialink._lookup_links(link_path=str(second)) == []
    assert medialink._lookup_links(link_path=str(first))[0].link_path == str(first)
    assert mediaduplicate.check_existing('MEYD-568', [parse(source)]) == ({str(source): first}, [])
    source.write_bytes(b'changed download')
    assert mediaduplicate.check_existing('MEYD-568', [parse(source)])[1]


def test_equal_size_different_content_and_other_parts_are_not_merged(library, db):
    root, _ = library
    keep, other, part = root / 'MEYD-568.mp4', root / 'MEYD-568-C.mp4', root / 'MEYD-568-CD2.mp4'
    keep.write_bytes(b'abcd'); other.write_bytes(b'abce'); part.write_bytes(b'abcd')
    for p in (keep, other, part): register(db, p)
    plan = mediaduplicate.preview('MEYD-568', str(keep))
    assert not plan['duplicates'] and len(plan['skipped']) == 2
    with pytest.raises(ValueError, match='没有内容完全相同'):
        mediaduplicate.merge('MEYD-568', str(keep), plan['token'])
    assert all(p.exists() for p in (keep, other, part))


def test_stale_preview_never_deletes_changed_video(library, db):
    root, _ = library
    keep, other = root / 'MEYD-568.mp4', root / 'MEYD-568-C.mp4'
    keep.write_bytes(b'aaaa'); other.write_bytes(b'aaaa')
    register(db, keep); register(db, other)
    plan = mediaduplicate.preview('MEYD-568', str(keep))
    other.write_bytes(b'bbbb')
    with pytest.raises(ValueError, match='变化'):
        mediaduplicate.merge('MEYD-568', str(keep), plan['token'])
    assert keep.exists() and other.read_bytes() == b'bbbb'


def test_multiple_hardlinks_merge_without_invalidating_remaining_signatures(library, db):
    root, download = library
    source = download / 'MEYD-568.mp4'; source.write_bytes(b'video')
    paths = [root / f'{i}' / 'MEYD-568.mp4' for i in range(3)]
    for path in paths:
        path.parent.mkdir(); os.link(source, path); register(db, path, source)
    plan = mediaduplicate.preview('MEYD-568', str(paths[0]))
    assert len(mediaduplicate.merge('MEYD-568', str(paths[0]), plan['token'])['removed']) == 2
    assert paths[0].exists() and source.exists()


def test_download_directory_and_unregistered_keep_are_rejected(library, db):
    root, download = library
    source, keep = download / 'MEYD-568.mp4', root / 'MEYD-568.mp4'
    source.write_bytes(b'video'); keep.write_bytes(b'video')
    register(db, source); register(db, keep)
    with pytest.raises(ValueError, match='下载源目录|媒体库内'):
        mediaduplicate.preview('MEYD-568', str(source))
    plan = mediaduplicate.preview('MEYD-568', str(keep))
    assert not plan['duplicates'] and source.exists()
    with pytest.raises(ValueError, match='不在'):
        mediaduplicate.preview('MEYD-568', str(root / 'unknown.mp4'))


def test_third_letter_part_is_not_merged_as_a_subtitle_version(library, db):
    root, _ = library
    keep = root / 'MEYD-568.mp4'; keep.write_bytes(b'video')
    register(db, keep)
    for letter in 'ABC':
        path = root / f'MEYD-568-{letter}.mp4'; path.write_bytes(b'video')
        register(db, path)
    plan = mediaduplicate.preview('MEYD-568', str(keep))
    assert not plan['duplicates'] and len(plan['skipped']) == 3


def test_background_job_reports_result_and_error():
    def finish(action):
        job_id = mediaduplicate.start_job(action)['job_id']
        for _ in range(100):
            state = mediaduplicate.job_state(job_id)
            if not state['running']:
                return state
            time.sleep(0.01)
        pytest.fail('Job did not finish')
    assert finish(lambda: {'ok': True})['result'] == {'ok': True}
    def fail(): raise ValueError('expected failure')
    assert finish(fail)['error'] == 'expected failure'


def test_merge_marker_protects_before_record_cleanup_and_allows_new_registration(library, db):
    root, _ = library
    old, keep = root / 'old.mp4', root / 'MEYD-568.mp4'
    old.write_bytes(b'video'); keep.write_bytes(b'video')
    register(db, old); register(db, keep)
    stamp = datetime.now()
    with db() as session:
        session.add(MediaMerge(old_path=str(old), keep_path=str(keep), create_time=stamp))
        session.commit()
    # 已写合并保护、旧关联尚未清掉时，也不允许回调删除源视频。
    assert medialink._lookup_links(link_path=str(old)) == []
    with db() as session:
        row = session.get(MediaLink, str(old))
        row.create_time = stamp + timedelta(seconds=1)
        session.commit()
    assert medialink._lookup_links(link_path=str(old))[0].link_path == str(old)
