from xml.etree import ElementTree

import pytest

from app.core.config import Settings
from app.database.models import Actor, ActorProfile, Code
from app.modules.scrape import merge
from app.services import scrape
from app.services.actornames import localize_casts
from tests.test_scrape_reliability import db, scrape_case


def test_known_aliases_localize_casts_without_guessing_and_deduplicate(db):
    db.configure(autoflush=False)
    with db() as session:
        session.add_all([Actor(name='石川澪', name_2='Mio Ishikawa', subscribed=True),
                         ActorProfile(name='English Alias', data={'aliases':['已确认姓名'], 'native_name':'已确认姓名'})])
        session.commit()
    assert localize_casts('Mio Ishikawa,石川澪,English Alias,Jane Doe,0,<img') == '石川澪,已确认姓名,Jane Doe'


def test_ambiguous_english_alias_is_kept(db):
    with db() as session:
        session.add_all([Actor(name='演员甲', name_2='Alice'), Actor(name='演员乙', name_2='Alice')])
        session.commit()
    assert localize_casts('Alice') == 'Alice'


def test_source_cleaning_collapses_explicit_aliases_without_duplicating_cast():
    result = merge.clean_detail({'casts':['Mio Ishikawa（石川澪）', 'Jane Doe', '0', '<img src="bad">']})
    assert result['casts'] == '石川澪,Jane Doe'
    assert merge.clean_detail({'casts':'N/A'}) == {}
    assert merge.clean_detail({'casts':True}) == {}


def test_merge_native_cast_tiebreak_respects_explicit_source_priority(monkeypatch):
    from app.core import config
    settings = Settings()
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    results = [merge.SourceResult('javdb', {'casts':'Mio Ishikawa'}),
               merge.SourceResult('javbus', {'casts':'石川澪'})]
    assert merge.merge_details(results)['casts'] == '石川澪'
    settings.scrape_field_priority = {'casts':'javdb,javbus'}
    assert merge.merge_details(results)['casts'] == 'Mio Ishikawa'
    assert merge._better('casts', '演员甲,演员乙', 'English Name') is False


def test_cached_metadata_normalizes_for_preview_and_saves_on_scrape(db):
    with db() as session:
        session.add_all([Actor(name='石川澪', name_2='Mio Ishikawa'),
                         Code(code='ABP-123', title='标题', casts='Mio Ishikawa')])
        session.commit()
    assert scrape._load_meta('ABP-123', fetch=False)['casts'] == '石川澪'
    scrape._save_meta('ABP-123', {'title':'替换标题'})
    with db() as session:
        row = session.get(Code, 'ABP-123')
        assert row.casts == '石川澪'
        assert row.title == '标题'


@pytest.mark.parametrize('force', [False, True])
def test_real_scrape_native_name_reaches_directory_nfo_db_and_notification(scrape_case, db, monkeypatch, force):
    from app.services import scrapefollowup
    from app.utils.mediafile import parse
    video, settings = scrape_case
    settings.scrape_dir_template = '{first_actor}'
    settings.scrape_notify = True
    notifications = []
    monkeypatch.setattr(scrapefollowup, 'schedule', lambda *args, **kwargs: notifications.append(kwargs['notification']))
    with db() as session:
        session.add(Actor(name='石川澪', name_2='Mio Ishikawa', subscribed=True))
        session.commit()
    meta = {'code':'SONE-725', 'title':'标题', 'casts':'Mio Ishikawa'}
    monkeypatch.setattr(scrape, 'load_group_meta', lambda *args, **kwargs: dict(meta))
    monkeypatch.setattr(merge, 'fetch_merged', lambda *args: dict(meta))
    monkeypatch.setattr(scrape, '_cover_bytes', lambda *args, **kwargs: b'')
    result = scrape.scrape_group('SONE-725', [parse(video)], force=force)
    assert result.ok, result.error
    assert result.links[0].parent.name == '石川澪'
    root = ElementTree.parse(result.links[0].with_suffix('.nfo')).getroot()
    assert [node.findtext('name') for node in root.findall('actor')] == ['石川澪']
    with db() as session:
        assert session.get(Code, 'SONE-725').casts == '石川澪'
        assert session.get(Actor, '石川澪').subscribed
    assert notifications[0]['casts'] == '石川澪'
