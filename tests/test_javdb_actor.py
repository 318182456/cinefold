"""Public JavDB-style fixtures; no live site or browser access is claimed."""
from types import SimpleNamespace

import pytest

from app.modules.ladysite import javdb_actor

HOST = 'https://javdb.test'
ACTOR = HOST + '/actors/verified123'
SEARCH = '''<html><title>演員搜尋 - JavDB</title><div class="actor-box">
<a href="/actors/wrong"><img src="/wrong.jpg"><strong>河北彩花子</strong></a></div>
<div class="actor-box"><a href="/actors/verified123" title="河北彩伽 (河北彩花)">
<img src="//images.test/actor.jpg"><strong>河北彩伽 (河北彩花)</strong></a></div></html>'''


def card(code='SONE-725', title='影片标题', actor_link=''):
    return f'''<div class="item"><a class="box" href="/v/movie123">
    <div class="cover"><img data-src="//images.test/thumbs/film.jpg"></div>
    <div class="video-title"><strong>{code}</strong> {title}</div>
    <div class="meta">2025-08-01</div><div class="score"><span class="value">4.5 分, 100人评价</span></div>
    </a>{actor_link}</div>'''


def profile(cards=None, heading='河北彩伽', aliases='河北彩花', next_page=2, extras=''):
    cards = card() if cards is None else cards
    return f'''<html><title>{heading} - JavDB</title><main>
    <section class="actor-section"><div class="avatar"><img src="//images.test/profile.jpg"></div>
    <h2 class="title is-4">{heading}</h2><div class="aliases">別名: {aliases}</div>{extras}</section>
    <div class="movie-list h cols-4">{cards}</div>
    <nav class="pagination"><a class="pagination-next" rel="next" href="?page={next_page}">下一页</a></nav>
    </main></html>'''


class Client:
    host = HOST
    last_url = ''

    def __init__(self, search=SEARCH, pages=None):
        self.search = search
        self.pages = pages if pages is not None else {1: profile()}
        self.calls = []
        self.redirect = ''

    def get(self, url, params=None):
        self.calls.append((url, params))
        self.last_url = self.redirect or url
        if url.endswith('/search'):
            return self.search
        return self.pages[(params or {}).get('page', 1)]


@pytest.fixture
def client(monkeypatch):
    result = Client()
    monkeypatch.setattr(javdb_actor, 'Avdb', lambda: SimpleNamespace(client=result))
    return result


def test_exact_actor_and_work_metadata_from_public_style_cards(client):
    got = javdb_actor.fetch_profile('河北彩花')
    assert got['name'] == '河北彩伽'
    assert got['aliases'] == ['河北彩花']
    assert got['source_url'] == ACTOR
    assert got['photo'] == 'https://images.test/profile.jpg'
    assert got['has_more']
    assert got['items'] == [{'code': 'SONE-725', 'title': '影片标题', 'release_date': '2025-08-01',
                             'casts': '河北彩伽', 'star': 4.5,
                             'banner': 'https://images.test/covers/film.jpg', 'poster': 'https://images.test/covers/film.jpg'}]
    assert len(client.calls) == 2  # actor search + profile, never each movie detail
    assert not any(key in got for key in ('birthday', 'height', 'measurements', 'description'))


@pytest.mark.parametrize('search', [
    '<div class="actor-item"><a href="/actors/verified123"><img src="/a.jpg"></a><div class="name">河北彩伽</div><div class="alias">河北彩花</div></div>',
    '<div class="actors"><a href="/actors/verified123"><strong>河北彩伽 (河北彩花)</strong></a></div>',
    '<a class="actor-box" href="/actors/verified123" title="河北彩花"><img src="/a.jpg"></a>',
])
def test_search_supports_actor_box_and_link_layouts(client, search):
    client.search = search
    assert javdb_actor.fetch_profile('河北彩花')['name'] == '河北彩伽'


def test_near_match_is_not_a_match(client):
    client.search = '<div class="actor-box"><a href="/actors/wrong"><strong>河北彩花子</strong></a></div>'
    with pytest.raises(ValueError, match='精确匹配'):
        javdb_actor.fetch_profile('河北彩花')
    assert len(client.calls) == 1


def test_profile_must_verify_name_independently_of_search(client):
    client.pages[1] = profile(heading='其他演员', aliases='不同名字')
    with pytest.raises(ValueError, match='不一致'):
        javdb_actor.fetch_profile('河北彩花')


def test_duplicate_same_name_ids_are_ambiguous(client):
    client.search = '<a href="/actors/first">河北彩花</a><a href="/actors/second">河北彩花</a>'
    with pytest.raises(ValueError, match='多个同名'):
        javdb_actor.fetch_profile('河北彩花')


def test_page_two_uses_verified_actor_url_without_search(client):
    client.pages[2] = profile(cards=card('SONE-726'), next_page=3)
    got = javdb_actor.fetch_profile('河北彩花', page=2, actor_url=ACTOR)
    assert len(client.calls) == 1
    assert client.calls[0] == (ACTOR, {'page': 2})
    assert got['items'][0]['code'] == 'SONE-726'
    assert got['has_more']


def test_empty_verified_actor_page_is_valid(client):
    client.pages[1] = profile(cards='', next_page=1)
    got = javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)
    assert got['items'] == [] and not got['has_more']


def test_optional_profile_fields_only_come_from_labeled_actor_values(client):
    client.pages[1] = profile(extras='''
      <div class="panel-block"><strong>生日:</strong><span class="value">1999年08月23日</span></div>
      <div class="panel-block"><strong>身高:</strong><span class="value">164cm</span></div>
      <div class="panel-block"><strong>三围:</strong><span class="value">B87/W58/H89</span></div>
      <div class="actor-description">来源介绍</div>''')
    got = javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)
    assert got['birthday'] == '1999-08-23'
    assert got['height'] == '164cm' and got['measurements'] == 'B87/W58/H89'
    assert got['description'] == '来源介绍'


@pytest.mark.parametrize('html', [
    '', '<html><title>Just a moment...</title><form id="challenge-form"></form></html>',
    '<html><title>Access Denied</title></html>', '<form><input type="password"></form>',
    '<html><h2 class="title">河北彩伽</h2></html>',
])
def test_empty_challenge_login_and_unverifiable_pages_raise(client, html):
    client.pages[1] = html
    with pytest.raises(ValueError):
        javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)


@pytest.mark.parametrize('url', [
    'https://evil.test/actors/verified123', '//evil.test/actors/verified123',
    'http://javdb.test/actors/verified123', 'https://javdb.test@evil.test/actors/verified123',
    'https://javdb.test/login', 'https://javdb.test/actors/../admin',
    'https://javdb.test/actors/%2e%2e/admin', 'https://javdb.test/actors/verified123?url=http://127.0.0.1',
    'javascript:alert(1)',
])
def test_actor_url_rejects_external_login_and_redirect_endpoints_before_request(client, url):
    with pytest.raises(ValueError):
        javdb_actor.fetch_profile('河北彩花', actor_url=url)
    assert client.calls == []


@pytest.mark.parametrize('final', ['https://evil.test/actors/verified123', HOST + '/login', HOST + '/actors/different'])
def test_redirects_cannot_change_actor_identity(client, final):
    client.redirect = final
    with pytest.raises(ValueError):
        javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)


def test_recommendations_and_explicitly_other_actor_cards_are_not_bound(client):
    wrong = card('SONE-726', actor_link='<a href="/actors/other">其他演员</a>')
    bad_number = card('SONE-725 拓展文字')
    page = profile(cards=card() + wrong + bad_number)
    page = page.replace('</main>', '<section class="recommendations"><div class="movie-list">' + card('SONE-999') + '</div></section></main>')
    client.pages[1] = page
    got = javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)
    assert [item['code'] for item in got['items']] == ['SONE-725']


def test_pagination_cannot_jump_to_other_actor_or_external_site(client):
    client.pages[1] = profile().replace('href="?page=2"', 'href="https://evil.test/actors/verified123?page=2"')
    assert not javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)['has_more']
    client.pages[1] = profile().replace('href="?page=2"', 'href="/actors/other?page=2"')
    assert not javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)['has_more']


def test_profile_without_avatar_can_use_search_photo(client):
    client.pages[1] = profile().replace('<div class="avatar"><img src="//images.test/profile.jpg"></div>', '')
    assert javdb_actor.fetch_profile('河北彩花')['photo'] == 'https://images.test/actor.jpg'


def test_explicit_old_name_label_is_an_alias(client):
    client.search = '<a href="/actors/verified123"><strong>演员: 河北彩伽 (旧名: 河北彩花)</strong></a>'
    client.pages[1] = profile(heading='演员: 河北彩伽 (旧名: 河北彩花)', aliases='')
    result = javdb_actor.fetch_profile('河北彩花')
    assert result['name'] == '河北彩伽'
    assert result['aliases'] == ['河北彩花']


def test_empty_actor_does_not_adopt_unwrapped_recommendation_section(client):
    page = profile(cards='', next_page=1)
    page = page.replace('<div class="movie-list h cols-4"></div>', '<h3>你可能喜欢的推荐影片</h3><div class="movie-list">' + card('SONE-999') + '</div>')
    client.pages[1] = page
    assert javdb_actor.fetch_profile('河北彩花', actor_url=ACTOR)['items'] == []
