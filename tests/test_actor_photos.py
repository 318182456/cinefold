import pytest

from app.database.models import Actor, ActorProfile, Code
from app.services import actorphotos, actorprofile
from app.utils.actornames import names, matches, presentation
from tests.test_scrape_reliability import db


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(actorphotos, '_STATE', None)


def test_cast_cleanup_keeps_json_aliases_and_english_names():
    assert names('["石川澪", {"name":"Jane Doe"}]') == ['石川澪', 'Jane Doe']
    assert names('0,24,<img src="bad">,未知,河北彩伽（河北彩花）') == ['河北彩伽', '河北彩花']
    assert matches('Jane Doe', 'JANE DOE')
    assert not matches('石川澪', '石川鈴華')


def test_directory_filters_garbage_and_prefers_verified_photo_without_writes(db):
    db.configure(autoflush=False)
    with db() as session:
        session.add_all([Actor(name='0'), Actor(name='石川澪', subscribed=True, photo='stale'),
                        ActorProfile(name='石川澪', data={'source_url':'https://javdb.com/actors/id', 'photo':'verified'}),
                        Code(code='ABP-100', casts='24,<img,Jane Doe,石川澪')])
        session.commit()
    result = actorprofile.directory(subscribed=False)
    assert [item['name'] for item in result['items']] == ['石川澪', 'Jane Doe']
    assert result['items'][0]['photo'] == 'verified'
    assert actorprofile.profile('石川澪')['actor']['photo'] == 'verified'
    with db() as session:
        assert session.get(Actor, '0') is not None
        assert session.get(Actor, '石川澪').photo == 'stale'


def test_avatar_sources_reject_wrong_search_hit(monkeypatch):
    from app.modules import ladysite
    from app.modules.ladysite.base import ActorInfo
    class Site:
        def search_actor(self, name):
            return ActorInfo(name='其他人', photo='wrong-photo')
    monkeypatch.setattr(ladysite, '_get_site', lambda name: Site())
    assert ladysite._fetch_actor_photo('javbus', '石川澪') == ''


def test_bus_selects_exact_match_instead_of_first_hit(monkeypatch):
    from app.modules.ladysite.bus import Bus
    from app.modules.ladysite.base import ActorInfo
    from types import SimpleNamespace
    site = Bus.__new__(Bus)
    site.client = SimpleNamespace(get=lambda url: '<html/>')
    monkeypatch.setattr(site, 'html_to_actors', lambda html: [
        ActorInfo(name='石川鈴華', photo='wrong'), ActorInfo(name='石川澪', photo='correct')])
    assert site.search_actor('石川澪').photo == 'correct'


def test_actor_fetch_workers_share_cancellable_budget(monkeypatch):
    from app.modules import ladysite
    from app.modules.ladysite.transport import current_budget
    budgets = []
    monkeypatch.setattr(ladysite, '_enabled_sites', lambda: ('javbus',))
    def fetch(*args):
        budgets.append(current_budget())
        return 'photo'
    monkeypatch.setattr(ladysite, '_fetch_actor_photo', fetch)
    assert ladysite.get_actor_photo('石川澪') == 'photo'
    assert budgets[0] is not None
    with pytest.raises(TimeoutError):
        budgets[0].check()


def test_photo_batch_persists_results_cooldown_and_preserves_subscription(db, monkeypatch):
    db.configure(autoflush=False)
    class InlinePool:
        def submit(self, func, *args):
            func(*args)
    monkeypatch.setattr(actorphotos, '_POOL', InlinePool())
    monkeypatch.setattr(actorphotos, '_fetch', lambda name: {'name': name, 'photo': 'photo'} if name == '甲' else {})
    with db() as session:
        session.add(Actor(name='甲', subscribed=True))
        session.commit()
    result = actorphotos.start(['甲', '甲', '乙', '0'])
    assert (result['total'], result['done'], result['ok'], result['failed']) == (2, 2, 1, 1)
    with db() as session:
        assert session.get(Actor, '甲').subscribed
        assert session.get(Actor, '甲').photo == 'photo'
        assert session.get(Actor, '乙') is None
        assert session.get(ActorProfile, '乙').data['photo_error']
    assert actorphotos.start(['乙'])['started'] is False
    assert actorphotos.start(['乙'], force=True)['started'] is True
    monkeypatch.setattr(actorphotos, '_STATE', None)
    assert actorphotos.current()['done'] == 1


def test_photo_batch_limits_targets_and_reports_restart(db, monkeypatch):
    class DeferredPool:
        def submit(self, func, *args):
            pass
    monkeypatch.setattr(actorphotos, '_POOL', DeferredPool())
    result = actorphotos.start([f'演员{index}' for index in range(30)])
    assert result['total'] == 24
    assert actorphotos.start(['其他演员'])['names'] == result['names']
    monkeypatch.setattr(actorphotos, '_STATE', None)
    assert actorphotos.current()['phase'] == 'interrupted'
    assert actorphotos.current()['running'] is False


def test_native_label_keeps_english_search_key_and_work_subscription(db):
    db.configure(autoflush=False)
    with db() as session:
        session.add_all([Actor(name='Mio Ishikawa', name_2='石川澪', subscribed=True),
                         Code(code='ABP-123', casts='Mio Ishikawa')])
        session.commit()
    for keyword in ['Mio', '石川']:
        item = actorprofile.directory(keyword=keyword, subscribed=True)['items'][0]
        assert item['name'] == 'Mio Ishikawa'
        assert item['display_name'] == '石川澪'
        assert item['aliases'] == ['Mio Ishikawa']
    result = actorprofile.profile('Mio Ishikawa')
    assert result['actor']['display_name'] == '石川澪'
    assert result['actor']['subscribed'] is True
    assert result['total'] == actorprofile.profile('石川澪')['total'] == 1
    assert actorprofile.subscribe_works('Mio Ishikawa')['subscribed'] == 1


def test_unknown_western_name_is_not_guessed_or_hidden():
    assert presentation('Jane Doe')['display_name'] == 'Jane Doe'
    assert presentation('AIKA')['display_name'] == 'AIKA'
    assert presentation('石川澪', ['Mio Ishikawa'])['display_name'] == '石川澪'


def test_native_name_is_saved_even_when_photo_is_missing(db, monkeypatch):
    class InlinePool:
        def submit(self, func, *args):
            func(*args)
    monkeypatch.setattr(actorphotos, '_POOL', InlinePool())
    monkeypatch.setattr(actorphotos, '_fetch', lambda name: {'name':'石川澪', 'aliases':['Mio Ishikawa'], 'photo':''})
    with db() as session:
        session.add(Actor(name='Mio Ishikawa', photo='existing', subscribed=True))
        session.commit()
    assert actorphotos.start(['Mio Ishikawa'])['done'] == 1
    item = actorprofile.directory(subscribed=True)['items'][0]
    assert item['display_name'] == '石川澪'
    assert item['photo'] == 'existing'
    assert item['needs_name_lookup'] is False
    with db() as session:
        assert session.get(Actor, 'Mio Ishikawa').subscribed
        assert session.get(ActorProfile, 'Mio Ishikawa').data['native_name'] == '石川澪'


def test_identity_prefers_verified_native_alias_over_english_photo(monkeypatch):
    from app.modules import ladysite
    monkeypatch.setattr(ladysite, '_enabled_sites', lambda: ('javbus', 'javdb'))
    def fetch(site, name):
        if site == 'javbus':
            return {'name': name, 'aliases':[name], 'photo':'english-avatar'}
        return {'name':'石川澪', 'aliases':[name, '石川澪'], 'photo':'verified-avatar'}
    monkeypatch.setattr(ladysite, '_fetch_actor_identity', fetch)
    result = ladysite.get_actor_identity('Mio Ishikawa')
    assert result['name'] == '石川澪'
    assert result['photo'] == 'verified-avatar'
