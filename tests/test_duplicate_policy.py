import os
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import Settings
from app.database.models import MediaLink, ScrapeSourceVideo
from app.modules.scrape import preferences
from app.services import duplicatepolicy, mediaduplicate, scrape, scrapeinventory, scrapewatch
from tests.test_media_duplicates import library, db, register


def setup(case, monkeypatch, choice):
    root, download = case
    older = download / 'old' / 'MEYD-568.mp4'
    older.parent.mkdir()
    older.write_bytes(b'old version')
    incoming = download / 'MEYD-568.mp4'
    incoming.write_bytes(b'new version')
    target = root / 'MEYD-568.mp4'
    os.link(older, target)
    target.with_suffix('.nfo').write_text('<movie><num>MEYD-568</num><title>existing metadata</title></movie>')
    for kind in ('poster', 'thumb', 'fanart'):
        Image.new('RGB', (30, 40), 'blue').save(target.with_name(f'{target.stem}-{kind}.jpg'))
    settings = Settings(medialink_scrape_dir=str(root), medialink_library_path=str(root), scrape_source_dirs=str(download),
                        scrape_still_limit=0, scrape_notify=False, scrape_duplicate_policy=choice)
    for module in (scrape, scrapeinventory, duplicatepolicy, mediaduplicate, scrapewatch):
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: {'code':'MEYD-568', 'title':'film metadata'})
    return incoming, older, target, settings


@pytest.mark.parametrize('choice,incoming_wins', [('overwrite',True), ('keep_existing',False),
    ('highest_quality',True), ('highest_quality',False), ('versions',None)])
def test_all_policies_preserve_downloads_and_rescan_state(library, db, monkeypatch, choice, incoming_wins):
    db.configure(autoflush=False)
    source, older, target, settings = setup(library, monkeypatch, choice)
    register(db, target, older)
    if choice == 'highest_quality':
        monkeypatch.setattr(duplicatepolicy, 'quality', lambda path:
            ((3840*2160, 20_000_000, 3600) if (Path(path) == source) == incoming_wins else (1920*1080, 10_000_000, 3600)))
    if incoming_wins is False:
        monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: pytest.fail('keep policy must not refetch metadata'))
    before = target.stat().st_ino
    result = scrape.scrape_group('MEYD-568', [scrape.inspect_source(source)])
    assert result.ok, result
    assert source.read_bytes() == b'new version' and older.read_bytes() == b'old version'
    if incoming_wins is True:
        from app.services.scrapemaintenance import protects_delete
        assert result.links == [target] and target.samefile(source)
        assert protects_delete(str(target))
        assert duplicatepolicy.retained(older, target)
        assert scrapewatch._complete(older, 'MEYD-568', [str(target)])
        with db() as session:
            assert session.get(MediaLink, str(target)).source_path == str(source)
    elif incoming_wins is False:
        assert result.links == [target] and target.stat().st_ino == before
        assert duplicatepolicy.retained(source, target)
        assert not mediaduplicate.verified_existing(source, target)
        assert scrapewatch._complete(source, 'MEYD-568', [str(target)])
    else:
        assert result.links[0] != target and result.links[0].samefile(source)
        assert target.stat().st_ino == before
    scrapeinventory.refresh_now()
    with db() as session:
        row = session.get(ScrapeSourceVideo, scrapeinventory.key(source))
        if incoming_wins is False:
            assert row.artifact_status == 'kept_existing' and row.links == [str(target)]
    if incoming_wins is not None:
        repeated = scrape.scrape_group('MEYD-568', [scrape.inspect_source(older if incoming_wins else source)])
        assert repeated.ok and target.stat().st_ino != 0
        assert target.read_bytes() == (b'new version' if incoming_wins else b'old version')


@pytest.mark.parametrize('unknown', [True, False])
def test_quality_unknown_or_different_edit_keeps_both(library, db, monkeypatch, unknown):
    source, older, target, _ = setup(library, monkeypatch, 'highest_quality')
    register(db, target, older)
    monkeypatch.setattr(duplicatepolicy, 'quality', lambda path: None if unknown else (1920*1080, 10_000_000, 3600 if Path(path)==source else 1800))
    result = scrape.scrape_group('MEYD-568', [scrape.inspect_source(source)])
    assert result.ok and result.links[0] != target
    assert target.read_bytes() == b'old version'


def test_changed_keep_decision_and_policy_are_rechecked(library, db, monkeypatch):
    source, older, target, settings = setup(library, monkeypatch, 'keep_existing')
    register(db, target, older)
    assert scrape.scrape_group('MEYD-568', [scrape.inspect_source(source)]).ok
    assert duplicatepolicy.retained(source, target)
    settings.scrape_duplicate_policy = 'versions'
    assert not duplicatepolicy.retained(source, target)
    result = scrape.scrape_group('MEYD-568', [scrape.inspect_source(source)])
    assert result.ok and result.links[0] != target


def test_failed_atomic_replacement_leaves_old_video(library, db, monkeypatch):
    source, older, target, _ = setup(library, monkeypatch, 'overwrite')
    register(db, target, older)
    monkeypatch.setattr(duplicatepolicy.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError, match='disk unavailable'):
        duplicatepolicy.replace_video(source, target, duplicatepolicy.signature(target))
    assert target.read_bytes() == older.read_bytes() == b'old version'
    assert source.read_bytes() == b'new version'
    assert not list(target.parent.glob('.cinefold-replace-*'))
    assert not duplicatepolicy.retained(older, target)


def test_duplicate_policy_validation():
    for choice in duplicatepolicy.POLICIES:
        assert not preferences.validate({'scrape_duplicate_policy':choice})
    assert preferences.validate({'scrape_duplicate_policy':'delete_all'})


def test_keep_existing_without_artwork_is_handled_not_requeued(library, db, monkeypatch):
    source, older, target, _ = setup(library, monkeypatch, 'keep_existing')
    register(db, target, older)
    target.with_suffix('.nfo').unlink()
    for path in target.parent.glob('*.jpg'):
        path.unlink()
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *a, **kw: pytest.fail('explicitly skipped incoming file'))
    assert scrape.scrape_group('MEYD-568', [scrape.inspect_source(source)]).ok
    assert scrapewatch._complete(source, 'MEYD-568', [str(target)])
    scrapeinventory.refresh_now()
    with db() as session:
        row = session.get(ScrapeSourceVideo, scrapeinventory.key(source))
        assert row.artifact_status == 'kept_existing'


def test_ffprobe_contract_and_missing_probe_fallback(tmp_path, monkeypatch):
    from types import SimpleNamespace
    source = tmp_path / 'movie.mp4'
    source.write_bytes(b'video')
    duplicatepolicy._probe.cache_clear()
    calls = []
    monkeypatch.setattr(duplicatepolicy.shutil, 'which', lambda _: 'ffprobe')
    def run(command, **options):
        calls.append(command)
        assert options['timeout'] == 20 and options['check']
        return SimpleNamespace(stdout=b'{"streams":[{"width":3840,"height":2160,"bit_rate":"20000000"}],"format":{"duration":"3600"}}')
    monkeypatch.setattr(duplicatepolicy.subprocess, 'run', run)
    assert duplicatepolicy.quality(source) == (3840*2160, 20_000_000, 3600)
    assert duplicatepolicy.quality(source) == (3840*2160, 20_000_000, 3600)
    assert len(calls) == 1
    duplicatepolicy._probe.cache_clear()
    monkeypatch.setattr(duplicatepolicy.shutil, 'which', lambda _: None)
    assert duplicatepolicy.quality(source) is None
