from datetime import datetime, timedelta
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_subtitle_version_does_not_block_distinct_original(library, db, monkeypatch):
    root, download = library
    old, source = root / 'MEYD-568-C.mp4', download / 'MEYD-568.mp4'
    old.write_bytes(b'original'); source.write_bytes(b'new copy')
    register(db, old)
    targets, conflicts = mediaduplicate.check_existing('MEYD-568', [parse(source)])
    assert not conflicts and targets[str(source)] != old
    assert targets[str(source)].name.startswith('MEYD-568 - version-')
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


def test_titled_numbered_sources_reuse_matching_existing_cd_targets(library, db):
    root, download = library
    sources, expected = [], {}
    for index in (1, 2, 3, 4):
        name = f'STARS-043 小倉由菜{("_" if index == 2 else " ")}{index}.mp4'
        source, target = download / name, root / f'STARS-043-CD{index}.mp4'
        source.write_bytes(f'part {index}'.encode())
        os.link(source, target)
        register(db, target, source, code='STARS-043')
        sources.append(source)
        expected[str(source)] = target
    infos = scrape.group_sources(sources)['STARS-043']
    assert [info.part for info in infos] == [1, 2, 3, 4]
    assert mediaduplicate.check_existing('STARS-043', infos) == (expected, [])


def test_verified_same_source_reuses_existing_target_despite_old_part_inference(library, db):
    root, download = library
    source, target = download / 'STARS-043 小倉由菜.mp4', root / 'STARS-043-CD2.mp4'
    source.write_bytes(b'part two')
    os.link(source, target)
    register(db, target, source, code='STARS-043')
    assert parse(source).part == 0 and parse(target).part == 2
    assert mediaduplicate.check_existing('STARS-043', [parse(source)]) == ({str(source): target}, [])


def test_different_titled_file_with_existing_part_gets_separate_version(library, db):
    root, download = library
    source, target = download / 'STARS-043 小倉由菜_2.mp4', root / 'STARS-043-CD2.mp4'
    source.write_bytes(b'different copy')
    target.write_bytes(b'existing part two')
    register(db, target, code='STARS-043')
    targets, conflicts = mediaduplicate.check_existing('STARS-043', [parse(source)])
    assert not conflicts and ' - version-' in targets[str(source)].stem
    assert not targets[str(source)].exists()
    assert source.exists() and target.exists()


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
    targets, conflicts = mediaduplicate.check_existing('MEYD-568', [scrape.inspect_source(source)])
    assert not conflicts and targets.get(str(source)) != first
    assert first.read_bytes() == b'video'


@pytest.mark.parametrize('autoflush', [False, True])
def test_equal_independent_copy_auto_reuses_and_survives_inventory_scan(library, db, monkeypatch, autoflush):
    from PIL import Image
    from app.database.models import Code, CodeStatus
    from app.services import scrapewatch
    db.configure(autoflush=autoflush)
    root, download = library
    source, canonical, target = download / 'MEYD-568.mp4', download / 'original.mp4', root / 'MEYD-568-C.mp4'
    source.write_bytes(b'exact identical content')
    canonical.write_bytes(source.read_bytes())
    os.link(canonical, target)
    register(db, target, canonical, code='MEYD-568-C')
    target.with_suffix('.nfo').write_text('<movie><num>MEYD-568-C</num><title>existing title</title></movie>')
    for kind in ('poster', 'thumb', 'fanart'):
        Image.new('RGB', (30, 40), 'blue').save(target.with_name(f'{target.stem}-{kind}.jpg'))
    source.with_suffix('.chs.srt').write_text('keep subtitle')
    before = target.with_suffix('.nfo').read_bytes()
    settings = Settings(medialink_scrape_dir=str(root), medialink_library_path=str(root),
                        scrape_source_dirs=str(download), scrape_still_limit=0, scrape_notify=False)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrapeinventory, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'code':'MEYD-568', 'title':'existing title'})
    with db() as session:
        session.add(Code(code='MEYD-568', title='existing title', status=CodeStatus.DOWNLOADED))
        session.add(ScrapeSourceVideo(source_key=scrapeinventory.key(source), path=str(source), name=source.name,
                    detected_code='MEYD-568', size=source.stat().st_size, mtime=source.stat().st_mtime,
                    artifact_status='pending', links=[], indexed_at=datetime.now()))
        session.commit()
    result = scrape.scrape_group('MEYD-568', [scrape.inspect_source(source)])
    assert result.ok and result.links == [target], result
    assert target.with_suffix('.nfo').read_bytes() == before
    assert target.with_suffix('.chs.srt').read_text() == 'keep subtitle'
    assert source.exists() and canonical.exists()
    assert len(list(root.glob('*.mp4'))) == 1
    with db() as session:
        assert session.get(MediaLink, str(target)).source_path == str(canonical)
        cached = session.get(ScrapeSourceVideo, scrapeinventory.key(source))
        assert cached.links == [str(target)] and cached.artifact_status == 'complete'
    scrapeinventory.refresh_now(str(download))
    with db() as session:
        cached = session.get(ScrapeSourceVideo, scrapeinventory.key(source))
        assert cached.links == [str(target)] and cached.artifact_status == 'complete'
    assert scrapewatch._complete(source, 'MEYD-568', [str(target)])


def test_different_content_scrapes_as_separate_version_without_overwrite(library, db, monkeypatch):
    root, download = library
    old, source = root / 'MEYD-568.mp4', download / 'MEYD-568.mp4'
    old.write_bytes(b'original version'); source.write_bytes(b'different encode')
    old.with_suffix('.nfo').write_text('<movie><title>original metadata</title></movie>')
    register(db, old)
    settings = Settings(medialink_scrape_dir=str(root), medialink_library_path=str(root),
                        scrape_source_dirs=str(download), scrape_still_limit=0, scrape_notify=False)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'code':'MEYD-568', 'title':'new metadata'})
    first = scrape.scrape_group('MEYD-568', [parse(source)])
    assert first.ok and len(first.links) == 1, first
    target = first.links[0]
    assert ' - version-' in target.name and target.samefile(source)
    assert old.read_bytes() == b'original version'
    assert 'original metadata' in old.with_suffix('.nfo').read_text()
    second = scrape.scrape_group('MEYD-568', [parse(source)])
    assert second.ok and second.links == [target]
    assert len(list(root.glob('*.mp4'))) == 2


def test_identical_multiple_targets_remember_only_selected_canonical(library, db):
    root, download = library
    source = download / 'MEYD-568.mp4'
    source.write_bytes(b'copy')
    paths = [root / 'MEYD-568.mp4', root / 'MEYD-568-C.mp4']
    for path in paths:
        path.write_bytes(b'copy')
        register(db, path)
    selected, conflicts = mediaduplicate.check_existing('MEYD-568', [parse(source)])
    chosen = selected[str(source)]
    assert not conflicts and chosen in paths
    assert mediaduplicate.verified_existing(source, chosen)
    assert not mediaduplicate.verified_existing(source, next(path for path in paths if path != chosen))
    assert mediaduplicate.check_existing('MEYD-568', [parse(source)], selected) == (selected, [])


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


def test_global_scan_covers_all_registered_rows_and_groups_variants_parts(library, db):
    root, _ = library
    with db() as session:
        for index in range(24):
            code = f'ABP-{index + 100}'
            for suffix in ('', '-U'):
                path = root / f'{code}{suffix}.mp4'
                path.write_bytes(b'video')
                session.add(MediaLink(link_path=str(path), source_path=str(path), code=code + suffix))
        for number in (1, 2):
            path = root / f'MEYD-568-CD{number}.mp4'
            path.write_bytes(b'part')
            session.add(MediaLink(link_path=str(path), source_path=str(path), code='MEYD-568'))
        session.commit()
    (root / 'unregistered.mp4').write_bytes(b'video')
    result = mediaduplicate.scan_registered()
    assert result['registered'] == 50 and len(result['groups']) == 25
    assert result['done'] == result['total'] == 50
    assert not result['hashes'] and not result['registry_changed']
    assert all(len(group['links']) == 2 for group in result['groups'])
    group = next(x for x in result['groups'] if x['code'] == 'MEYD-568')
    assert group['parts'] == [1, 2] and not group['merge_available']
    assert {x['match_type'] for x in group['links']} == {'different_part'}
    assert all('unregistered.mp4' not in x['link_path'] for g in result['groups'] for x in g['links'])


def test_global_hash_only_same_size_physical_candidates_and_cross_code(library, db, monkeypatch):
    root, _ = library
    original, alias = root / 'MEYD-568.mp4', root / 'MEYD-568-C.mp4'
    copy, unique = root / 'ABP-123.mp4', root / 'ABP-456.mp4'
    original.write_bytes(b'abcd'); os.link(original, alias)
    copy.write_bytes(b'abcd'); unique.write_bytes(b'unique size')
    for path, code in [(original, 'MEYD-568'), (alias, 'MEYD-568-C'), (copy, 'ABP-123'), (unique, 'ABP-456')]:
        register(db, path, code=code)
    calls = []
    digest = mediaduplicate.stable_digest
    def counted(path, **kwargs):
        calls.append(str(path))
        return digest(path, **kwargs)
    monkeypatch.setattr(mediaduplicate, 'stable_digest', counted)
    progress = []
    result = mediaduplicate.scan_registered(hashes=True, progress=lambda **p: progress.append(p))
    assert result['physical_files'] == 3 and result['hash_candidates'] == 2
    assert len(calls) == 2 and str(unique) not in calls
    assert result['hash_bytes'] == 8 and result['hashed_files'] == 2
    assert len(result['groups']) == 2
    same_code = next(x for x in result['groups'] if x['kind'] == 'code')
    assert same_code['hardlink_groups'] == 1 and same_code['merge_available']
    cross = next(x for x in result['groups'] if x['kind'] == 'cross_code')
    assert not cross['merge_available'] and len(cross['links']) == 3
    assert {x['match_type'] for x in cross['links']} == {'hardlink', 'same_content'}
    assert {'inventory', 'hash', 'finalizing'} <= {p.get('phase') for p in progress}
    assert all(path.exists() for path in (original, alias, copy, unique))


def test_global_fast_scan_never_claims_equal_size_files_have_same_content(library, db):
    root, _ = library
    paths = [root / 'MEYD-568.mp4', root / 'MEYD-568-U.mp4']
    for path, data in zip(paths, (b'abcd', b'abce')):
        path.write_bytes(data); register(db, path, code=path.stem)
    quick = mediaduplicate.scan_registered()['groups'][0]
    assert {x['match_type'] for x in quick['links']} == {'unverified'}
    full = mediaduplicate.scan_registered(hashes=True)['groups'][0]
    assert {x['match_type'] for x in full['links']} == {'different_content'}
    assert full['same_content_groups'] == 0 and all(path.exists() for path in paths)


def test_global_hash_failure_is_inherited_by_hardlink_alias(library, db, monkeypatch):
    root, _ = library
    first, alias, independent = root / 'MEYD-568.mp4', root / 'MEYD-568-U.mp4', root / 'ABP-123.mp4'
    first.write_bytes(b'video'); os.link(first, alias); independent.write_bytes(b'video')
    for path in (first, alias, independent):
        register(db, path, code=path.stem)
    failed_identity = first.stat().st_ino
    def digest(path, **kwargs):
        if Path(path).stat().st_ino == failed_identity:
            raise OSError('读取代表项失败')
        return 'independent-digest'
    monkeypatch.setattr(mediaduplicate, 'stable_digest', digest)
    result = mediaduplicate.scan_registered(hashes=True)
    group = next(x for x in result['groups'] if x['code'] == 'MEYD-568')
    assert {x['error'] for x in group['links']} == {'读取代表项失败'}
    assert {x['match_type'] for x in group['links']} == {'unavailable'}
    assert all(x['sha256'] is None for x in group['links'])
    assert not group['hardlink_groups'] and not group['same_content_groups']
    assert result['read_errors'] == 2 and not group['merge_available']


def test_global_scan_detects_file_and_registry_changes_during_hash(library, db, monkeypatch):
    root, _ = library
    first, second, late = root / 'MEYD-568.mp4', root / 'MEYD-568-U.mp4', root / 'ABP-999.mp4'
    for path in (first, second):
        path.write_bytes(b'abcd'); register(db, path, code=path.stem)
    mutated = False
    def changed(path, **kwargs):
        nonlocal mutated
        if not mutated:
            Path(path).write_bytes(b'replaced')
            late.write_bytes(b'late registration'); register(db, late, code='ABP-999')
            mutated = True
        return 'same-digest'
    monkeypatch.setattr(mediaduplicate, 'stable_digest', changed)
    result = mediaduplicate.scan_registered(hashes=True)
    assert result['registered'] == 2 and result['registry_changed'] and result['file_changes'] == 1
    group = result['groups'][0]
    assert group['same_content_groups'] == 0
    assert any(x['match_type'] == 'changed' for x in group['links'])
    assert not group['merge_available'] and first.exists() and second.exists()


def test_global_scan_pages_are_independent_of_media_list_and_detect_stale_db(library, db, monkeypatch):
    root, _ = library
    for index in range(12):
        for suffix in ('', '-U'):
            code = f'ABP-{index + 100}'
            path = root / f'{code}{suffix}.mp4'; path.write_bytes(b'video')
            register(db, path, code=code + suffix)
    result = mediaduplicate.scan_registered()
    monkeypatch.setattr(mediaduplicate, '_SCAN_STATE', {'scan_id': 'snapshot', 'running': False, 'result': result})
    page = mediaduplicate.scan_state(page=99, size=10)
    assert page['group_total'] == 12 and page['page'] == 2 and len(page['groups']) == 2
    assert page['summary']['registered'] == 24 and not page['summary']['registry_changed']
    # 返回值不能反向修改共享后台结果。
    page['groups'][0]['links'].clear()
    assert len(mediaduplicate.scan_state(page=2)['groups'][0]['links']) == 2
    with db() as session:
        session.delete(session.get(MediaLink, result['groups'][0]['links'][0]['link_path']))
        session.commit()
    assert mediaduplicate.scan_state()['summary']['registry_changed']


def test_global_background_scan_can_cancel_and_rejects_stale_cancel_id(monkeypatch):
    entered = threading.Event()
    def scan(*, cancelled, progress, **kwargs):
        progress(total=4, done=1, phase='inventory')
        entered.set()
        assert cancelled.wait(2)
        raise InterruptedError('cancelled')
    monkeypatch.setattr(mediaduplicate, 'scan_registered', scan)
    monkeypatch.setattr(mediaduplicate, '_SCAN_STATE', {'running': False, 'result': None})
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(mediaduplicate, '_SCAN_POOL', pool)
        job = mediaduplicate.start_scan(hashes=True)
        assert entered.wait(1)
        assert mediaduplicate.start_scan()['scan_id'] == job['scan_id']
        with pytest.raises(ValueError, match='已变化'):
            mediaduplicate.cancel_scan('stale scan')
        mediaduplicate.cancel_scan(job['scan_id'])
    state = mediaduplicate.scan_state()
    assert not state['running'] and state['cancelled'] and state['phase'] == 'cancelled'
    assert state['done'] == 1 and state['total'] == 4 and state['summary'] is None


def test_shared_hash_cache_uses_inode_and_supports_mid_read_cancellation(tmp_path):
    from app.services import filehash
    source, alias = tmp_path / 'source.mp4', tmp_path / 'alias.mp4'
    source.write_bytes(b'a' * (8 * 1024 * 1024)); os.link(source, alias)
    cancel = threading.Event()
    with pytest.raises(InterruptedError):
        filehash.stable_digest(source, cancelled=cancel, progress=lambda _: cancel.set())
    digest = filehash.stable_digest(source)
    assert filehash.stable_digest(alias, progress=lambda _: pytest.fail('same inode must reuse stable hash')) == digest
    source.write_bytes(b'b' * (8 * 1024 * 1024))
    assert filehash.stable_digest(alias) != digest


def test_global_duplicate_routes_preserve_background_contract(library, db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import get_current_user
    from app.api.endpoints import medialink as endpoint
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[get_current_user] = lambda: 'tester'
    monkeypatch.setattr(mediaduplicate, 'start_scan', lambda **kwargs: {'scan_id': 'job', **kwargs})
    monkeypatch.setattr(mediaduplicate, 'scan_state', lambda **kwargs: {'group_total': 30, **kwargs})
    client = TestClient(app)
    assert client.post('/medialinks/duplicates/scan', json={'hashes': True}).json()['data'] == {'scan_id': 'job', 'hashes': True}
    assert client.get('/medialinks/duplicates/scan?page=3&size=10').json()['data'] == {'group_total': 30, 'page': 3, 'size': 10}
    monkeypatch.setattr(mediaduplicate, '_SCAN_STATE', {'scan_id': 'current', 'running': False})
    assert client.post('/medialinks/duplicates/scan/cancel', json={'scan_id': 'stale'}).json()['code'] == 400
