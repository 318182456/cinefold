"""Verified JavDB public actor profiles and their paginated work cards.

Reuse Avdb's configured SiteClient. Names must match a primary profile heading
or an explicit alias; neither the first search hit nor a recommended movie can
establish an actor's identity.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, normalize_date, parse_star
from app.modules.ladysite.identity import normalize_code
from app.modules.ladysite.javdb import Avdb, convert_image_url

_ACTOR_PATH = re.compile(r'^/actors/[A-Za-z0-9_-]+/?$')
_MOVIE_PATH = re.compile(r'^/v/[A-Za-z0-9_-]+/?$')
_RECOMMENDATIONS = '.related,.recommend,.recommendations,.recommendation,.recommended,.recommended-movies,.related-movies,.similar,.suggestions,#related,#recommendations,aside'


def _text(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '')).strip()


def _key(value):
    return re.sub(r'\s+', '', _text(value)).casefold()


def _names(value):
    label = r'^(?:演員|演员|Actor|別名|别名|又名|旧名|舊名|Aliases?|Also known as)\s*[:：]\s*'
    value = re.sub(label, '', _text(value), flags=re.I)
    value = re.sub(r'[♂♀]', '', value).strip()
    names = [re.sub(label, '', part.strip(), flags=re.I) for part in re.split(r'[()（）,，、;；/|\n]+', value) if part.strip()]
    names = [name for name in names if name.casefold() not in {'女', '男', 'female', 'male', '女優', '女优', '男優', '男优'}]
    return list(dict.fromkeys(names))


def _origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('JavDB 地址格式无效')
    try:
        return parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        raise ValueError('JavDB 地址端口无效') from exc


def _local_url(raw, host, pattern):
    if not raw or '\\' in raw:
        raise ValueError('JavDB 链接格式无效')
    url = urljoin(host.rstrip('/') + '/', raw)
    parsed = urlsplit(url)
    if _origin(url) != _origin(host) or not pattern.fullmatch(unquote(parsed.path)):
        raise ValueError('仅允许当前 JavDB 站点的演员或作品链接')
    # Profile URLs stored by callers are stable actor identities, not redirects
    # or arbitrary query-string endpoints. Pagination is supplied separately.
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query and (set(query) != {'page'} or len(query['page']) != 1 or not query['page'][0].isdigit()):
        raise ValueError('JavDB 链接含不支持的查询参数')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))


def _image(raw, host):
    if not raw:
        return ''
    url = urljoin(host.rstrip('/') + '/', raw)
    parsed = urlsplit(url)
    return convert_image_url(url) if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else ''


def _guard(html):
    if not html or not html.strip():
        raise ValueError('JavDB 未返回演员页面，请检查代理、Cookie 或过盾服务')
    doc = PyQuery(html)
    title = _text(doc('title').text()).casefold()
    if doc('#challenge-form,#cf-challenge-running,.cf-browser-verification') or re.match(r'just a moment|attention required|access denied|403 forbidden|verify you are human', title):
        raise ValueError('JavDB 返回验证或防护页面，请检查 Cookie 和过盾服务')
    if doc('input[type="password"]') and not doc('.actor-section,.actor-info,.actor-profile,.actor-page'):
        raise ValueError('JavDB 要求登录，请配置该数据源的 Cookie')
    return doc


def _read(client, url, *, params=None, actor=False):
    html = client.get(url, params=params)
    final = str(getattr(html, 'url', '') or getattr(client, 'last_url', '') or url)
    parsed = urlsplit(final)
    if _origin(final) != _origin(client.host):
        raise ValueError('JavDB 页面重定向到了其他站点，已停止解析')
    if parsed.path.rstrip('/') in ('/login', '/sessions', '/sign_in', '/users/sign_in'):
        raise ValueError('JavDB 要求登录，请配置该数据源的 Cookie')
    doc = _guard(html)
    if actor:
        final_actor = _local_url(final, client.host, _ACTOR_PATH)
        if final_actor != _local_url(url, client.host, _ACTOR_PATH):
            raise ValueError('JavDB 演员链接发生身份不一致的重定向，请重新搜索')
    return doc, final


def _search(client, name):
    search_url = urljoin(client.host + '/', 'search')
    doc, final = _read(client, search_url, params={'q': name, 'f': 'actor'})
    if _ACTOR_PATH.fullmatch(urlsplit(final).path):
        return _local_url(final, client.host, _ACTOR_PATH), '', doc
    if urlsplit(final).path.rstrip('/') != '/search':
        raise ValueError('JavDB 未返回有效的演员搜索页面')
    matches = {}
    for anchor in doc('a[href]').items():
        href = anchor.attr('href') or ''
        try:
            actor_url = _local_url(href, client.host, _ACTOR_PATH)
        except ValueError:
            continue
        values = [anchor.attr('title') or '']
        for child in anchor('.actor-name,.name,.title,strong,.aliases,.alias').items():
            values.append(child.text())
        values.append(anchor.text())
        # Some actor boxes put the avatar anchor and the name beside each other.
        box = anchor.closest('.actor-box,.actor-item')
        if box:
            values.extend(node.text() for node in box('.actor-name,.name,.title,.aliases,.alias').items())
        if not any(_key(name) == _key(candidate) for value in values for candidate in _names(value)):
            continue
        image = anchor('img').eq(0)
        if not image and box:
            image = box('img').eq(0)
        photo = _image(image.attr('data-src') or image.attr('src') or '', client.host) if image else ''
        matches.setdefault(actor_url, photo)
    if not matches:
        raise ValueError(f'JavDB 未找到姓名或明确别名精确匹配的演员：{name}')
    if len(matches) > 1:
        raise ValueError(f'JavDB 返回多个同名演员，无法确认身份：{name}')
    actor_url, photo = next(iter(matches.items()))
    return actor_url, photo, None


def _profile(doc, requested, host, fallback_photo=''):
    scope = doc('.actor-section,.actor-info,.actor-profile,.actor-page').eq(0)
    if not scope:
        raise ValueError('JavDB 页面缺少可验证的演员档案区域')
    heading = scope('.actor-name,h1,h2.title,.title').eq(0)
    names = _names(heading.text())
    if not names:
        raise ValueError('JavDB 演员页面缺少可验证姓名')
    aliases = []
    for node in scope('.aliases,.alias,.actor-aliases,.other-names,.other-name').items():
        aliases.extend(_names(node.text()))
    for block in scope('.panel-block,li,dl').items():
        label = _text(block('strong,.label,dt').eq(0).text()).rstrip(':：')
        if label.casefold() in ('別名', '别名', '又名', 'aliases', 'alias'):
            aliases.extend(_names(block('.value,dd').eq(0).text()))
    identities = list(dict.fromkeys([*names, *aliases]))
    if _key(requested) not in {_key(value) for value in identities}:
        raise ValueError(f'JavDB 档案姓名与请求演员不一致：{requested}')
    photo_node = scope('.actor-avatar img,.avatar img,.actor-photo img,.actor-picture img').eq(0)
    if not photo_node:
        photo_node = scope('img').filter(lambda _, node: not PyQuery(node).closest('.movie-list')).eq(0)
    photo = _image(photo_node.attr('data-src') or photo_node.attr('src') or '', host) if photo_node else fallback_photo
    result = {'name': names[0], 'aliases': [value for value in identities if _key(value) != _key(names[0])], 'photo': photo}
    for block in scope('.panel-block,li,dl').items():
        label = _text(block('strong,.label,dt').eq(0).text()).rstrip(':：').casefold()
        value = _text(block('.value,dd').eq(0).text())
        if not value:
            continue
        if label in ('生日', '出生日期', '生年月日', 'birthday') and normalize_date(value):
            result['birthday'] = normalize_date(value)
        elif label in ('身高', '身長', 'height'):
            result['height'] = value
        elif label in ('三圍', '三围', 'スリーサイズ', 'measurements'):
            result['measurements'] = value
    description = _text(scope('.actor-description,.actor-bio,.biography').eq(0).text())
    if description:
        result['description'] = description
    return result


def _works(doc, profile, source_url, host):
    container = None
    for node in doc('.movie-list').items():
        previous = node.prev()
        heading = previous if previous.is_('h1,h2,h3,h4,.title,.section-title') else previous('h1,h2,h3,h4,.section-title')
        recommended_heading = bool(re.search(r'推荐|推薦|相关|相關|猜你喜欢|猜你喜歡|related|similar|recommend', heading.text(), re.I))
        if not node.closest(_RECOMMENDATIONS) and not recommended_heading:
            container = node
            break
    if container is None:
        return []
    items, seen = [], set()
    for card in container('.item').items():
        if card.closest(_RECOMMENDATIONS):
            continue
        link = card('a[href]').eq(0)
        try:
            _local_url(link.attr('href') or '', host, _MOVIE_PATH)
        except ValueError:
            continue
        explicit_actors = card('a[href*="/actors/"]')
        if explicit_actors:
            matched = False
            for actor in explicit_actors.items():
                try:
                    matched |= _local_url(actor.attr('href'), host, _ACTOR_PATH) == source_url
                except ValueError:
                    continue
            if not matched:
                continue
        raw = _text(card('.video-title strong').eq(0).text())
        code = normalize_code(raw)
        if not code or re.sub(r'[\s_-]', '', raw).casefold() != re.sub(r'[\s_-]', '', code).casefold() or code in seen:
            continue
        title_node = card('.video-title').eq(0).clone()
        title_node('strong,.tags,.tag').remove()
        title = _text(title_node.text()) or _text(link.attr('title'))
        title = re.sub(rf'^{re.escape(raw)}\s*[-:]?\s*', '', title, flags=re.I)
        image = card('.cover img,img').eq(0)
        cover = _image(image.attr('data-src') or image.attr('src') or '', host) if image else ''
        info = CodeInfo(code=code, title=title, casts=profile['name'], banner=cover, poster=cover,
                        release_date=normalize_date(card('.meta,.date,time').text()),
                        star=parse_star(card('.score .value,.score').eq(0).text()))
        items.append(info.to_dict())
        seen.add(code)
    return items


def _has_more(doc, source_url, host, page):
    for link in doc('.pagination a.pagination-next,.pagination a[rel="next"]').items():
        if link.has_class('disabled') or link.has_class('is-disabled') or link.attr('disabled') is not None or link.attr('aria-disabled') == 'true':
            continue
        raw = urljoin(source_url, link.attr('href') or '')
        try:
            if _local_url(raw, host, _ACTOR_PATH) != source_url:
                continue
            values = parse_qs(urlsplit(raw).query).get('page', [])
            if len(values) == 1 and values[0].isdigit() and int(values[0]) == page + 1:
                return True
        except ValueError:
            continue
    return False


def fetch_profile(name: str, page: int = 1, actor_url: str = '') -> dict:
    name = _text(name)
    if not name or len(name) > 160 or not isinstance(page, int) or page < 1:
        raise ValueError('请提供有效演员姓名和正整数页码')
    client = Avdb().client
    doc, fallback_photo = None, ''
    if actor_url:
        source_url = _local_url(actor_url, client.host, _ACTOR_PATH)
    else:
        source_url, fallback_photo, doc = _search(client, name)
    if doc is None or page != 1:
        doc, _ = _read(client, source_url, params={'page': page}, actor=True)
    profile = _profile(doc, name, client.host, fallback_photo)
    profile.update(source_url=source_url, items=_works(doc, profile, source_url, client.host),
                   has_more=_has_more(doc, source_url, client.host, page))
    return profile
