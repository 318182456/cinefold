"""MDC-ng 边界问题及 AVM 数据源能力的隔离回归。"""
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.modules.scrape import images, local, naming, preferences
from app.modules.ladysite.identity import validate_page
from app.utils import mediafile
from tests.test_scrape_reliability import db, scrape_case  # noqa: F401


def picture(width=800, height=538, color='blue', fmt='PNG'):
    stream = BytesIO()
    with Image.new('RGB', (width, height), color) as image:
        image.save(stream, fmt)
    return stream.getvalue()


@pytest.mark.parametrize('filename,code,part', [
    ('MKBD-S127.mp4', 'MKBD-S127', 0),
    ('MKBD-S94-1080P-演员无码.mp4', 'MKBD-S94', 0),
    ('H4610-ori696.mp4', 'H4610-ORI696', 0),
    ('H0930-gol065.mp4', 'H0930-GOL065', 0),
    ('heydouga-4017-181-6.mkv', 'HEYDOUGA-4017-181', 6),
    ('bibivr00118-10.mp4', 'BIBIVR-118', 10),
    ('bibivr00118-CD10.mp4', 'BIBIVR-118', 10),
    ('MIRD00270_4K60FPS2.mp4', 'MIRD-270', 0),
    ('ABP-554-U-C.mp4', 'ABP-554-UC', 0),
    ('ABP-554-R12.mp4', 'ABP-554', 12),
])
def test_issue_filename_samples(filename, code, part):
    info = mediafile.parse(filename)
    assert (info.code, info.part) == (code, part)
    if filename == 'ABP-554-U-C.mp4':
        assert info.subbed and info.uncensored


def test_c_requires_both_siblings(tmp_path):
    from app.services.scrape import inspect_source
    video = tmp_path / 'SONE-725-C.mp4'; video.touch()
    assert inspect_source(video).subbed
    for letter in ('A', 'B'):
        (tmp_path / f'SONE-725-{letter}.mp4').touch()
    info = inspect_source(video)
    assert (info.code, info.part, info.subbed) == ('SONE-725', 3, False)


def test_correct_category_and_utf8_path_budget():
    assert preferences.category('MDBK-346') == 'censored'
    assert preferences.category('H4610-ORI696') == 'uncensored'
    assert len(naming.sanitize_segment('😀' * 100).encode('utf-8')) <= 220


@pytest.mark.parametrize('html,url,expected', [
    ('<h2 class="title"><strong>ABW-306</strong></h2>', 'https://example.test/v/hash', True),
    ('<h2 class="title"><strong>ABF-134</strong></h2>', 'https://example.test/v/hash', False),
    ('<h1>无编号的标题</h1>', 'https://example.test/v/hash', False),
    ('<h1>ABW-306</h1>', 'https://example.test/ABF-134', False),
    ('<h1>无编号的标题</h1>', 'https://example.test/?cid=118abw00306', True),
    ('<h1>ABW-306</h1>', 'https://example.test/?cid=118abf00134', False),
    ('{"barcode":"ABW-306","name":"T"}', 'https://example.test/api', True),
    ('{"name":"T"}', 'https://example.test/ABW-306', False),
    ('{"data":{"external_id":"ABF-134","title":"T"}}', 'https://example.test/api', False),
])
def test_source_page_identity(html, url, expected):
    assert validate_page(html, url, 'ABW-306') is expected


def test_images_are_real_jpegs_and_stills_do_not_collide(tmp_path):
    original = picture()
    for code, color in [('AAA-001', 'red'), ('BBB-002', 'blue')]:
        image_set = images.build_image_set(original, [picture(color=color), b'<html>error</html>' * 100])
        assert len(image_set.stills) == 1
        written = images.write_images(tmp_path / f'{code}.mp4', image_set)
        assert 'stills' in written
        for kind in ('poster', 'fanart', 'thumb'):
            with Image.open(tmp_path / written[kind]) as image:
                assert image.format == 'JPEG'
    assert len(list((tmp_path / 'extrafanart').glob('*.jpg'))) == 2
    assert not images.jpeg_bytes(b'garbage' * 1000)


def test_failed_still_write_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(images, '_write_atomic', lambda *a: False)
    assert images.write_images(tmp_path / 'AAA-001.mp4', images.ImageSet(stills=[picture()])) == {}


def test_crop_policy_keeps_uncensored_and_vr_cover():
    assert not images.should_crop('091416_256', {})
    assert not images.should_crop('FC2-PPV-1234567', {})
    assert not images.should_crop('SSIS-001', {'genres': 'VR'})
    assert images.should_crop('SSIS-001', {})
    assert not images.should_crop('SSIS-001', {}, 'never')
    result = images.build_image_set(picture(), crop=False)
    with Image.open(BytesIO(result.poster)) as image:
        assert image.size == (800, 538)


def test_nfo_ready_before_video_visible_and_no_image_loop(scrape_case, monkeypatch):
    from app.services import scrape, scrapewatch
    video, _ = scrape_case
    original = scrape._make_link
    def link(source, target):
        assert target.with_suffix('.nfo').is_file()
        assert not target.exists()
        return original(source, target)
    monkeypatch.setattr(scrape, '_make_link', link)
    result = scrape.scrape_file(video)
    assert result.ok
    links = [str(p) for p in result.links]
    assert scrapewatch._complete(video, result.code, links, require_images=False)
    assert not scrapewatch._complete(video, result.code, links)


def test_local_identity_recovery_does_not_share_unknown_movies(tmp_path):
    from app.services.scrape import inspect_source
    video = tmp_path / 'movie.mp4'; video.touch()
    video.with_suffix('.json').write_text('{"code":"SSIS-001","title":"T"}', encoding='utf-8')
    assert inspect_source(video).code == 'SSIS-001'
    video.with_suffix('.json').unlink()
    (tmp_path / 'other.mp4').touch()
    (tmp_path / 'movie.nfo').write_text('<movie><num>SSIS-001</num></movie>', encoding='utf-8')
    # 明确同名 movie.nfo 属于 movie.mp4；other.mp4 不能借用它。
    assert not local.identify(tmp_path / 'other.mp4')


@pytest.mark.parametrize('parser,options,raw', [
    ('http_json', {'detail_url': '/api/{code}', 'fields': {'code': 'data.code', 'title': 'data.title', 'casts': 'data.actors'}},
     '{"data":{"code":"SSIS-001","title":"T","actors":["A","B"]}}'),
    ('http_html', {'detail_url': '/v/{code}', 'selectors': {'code': '.number', 'title': 'h1', 'banner': 'img::attr(src)'}},
     '<span class="number">SSIS-001</span><h1>T</h1><img src="/cover.jpg">'),
])
def test_avm_custom_providers_require_real_matching_code(monkeypatch, parser, options, raw):
    from app.modules.ladysite.custom import CustomSource
    from app.modules.ladysite import sources
    source = dict(key='custom_test', parser=parser, options=options, host='https://example.test', enabled=True)
    monkeypatch.setattr(sources, 'get_source', lambda key: source)
    site = CustomSource(source)
    site.client.last_url = 'https://example.test/v/SSIS-001'
    monkeypatch.setattr(site.client, 'get', lambda url: raw)
    info = site.crawler_original('SSIS-001')
    assert info and info.title == 'T' and info.identity_verified
    if parser == 'http_html':
        assert info.banner == 'https://example.test/cover.jpg'
    assert site.crawler_original('SSIS-002') is None


def test_avwiki_real_name_and_metadata(db, monkeypatch):
    from app.modules.ladysite.avwiki import AvWiki
    site = AvWiki()
    site.client.last_url = 'https://av-wiki.net/mfc-354/'
    monkeypatch.setattr(site.client, 'get', lambda *a: '<h1>MFC-354 作品标题</h1><article><dl><dt>AV女優名</dt><dd>演员甲</dd><dt>配信開始日</dt><dd>2024/01/02</dd><dt>メーカー</dt><dd>厂商</dd></dl></article>')
    info = site.crawler_original('MFC-354')
    assert info and info.casts == '演员甲' and info.producer == '厂商'
    assert info.release_date == '2024-01-02'


def test_custom_source_api_routing_and_proxy(db):
    from app.api.endpoints.datasource import create_datasource, SourceCreateRequest
    from app.modules.ladysite import _sites_for_code, sources
    from app.modules.ladysite.base import SiteClient
    result = create_datasource(SourceCreateRequest(key='avm_json', host='https://example.test', parser='http_json',
        proxy='http://proxy.test:8888', options={'detail_url': '/api/{code}', 'fields': {'code': 'code', 'title': 'title'}}), 'test')
    assert result['code'] == 200
    assert 'avm_json' in _sites_for_code('SSIS-001')
    assert SiteClient.from_source('avm_json').proxy == 'http://proxy.test:8888'
    assert sources.get_source('avm_json')['options']['fields']['code'] == 'code'


def test_reverse_identity_uses_exact_file_and_history_hash_once(db, monkeypatch, tmp_path):
    from app.database.models import History
    from app.modules import downloadclient
    from app.services.sourceidentity import SourceIdentityResolver
    source = tmp_path / '蒼井そら、無碼流出版.mp4'
    with db() as session:
        session.add(History(hash='a' * 40, code='SOE-422', save_path=str(tmp_path)))
        session.commit()
    calls = []
    def files():
        calls.append(1)
        return {str(source): ['A' * 40]}
    monkeypatch.setattr(downloadclient, 'all_torrent_files_with_hashes', files)
    resolver = SourceIdentityResolver()
    assert resolver.resolve(source) == 'SOE-422'
    assert resolver.resolve(tmp_path / 'unrelated.mp4') == ''
    assert len(calls) == 1


def test_reverse_identity_does_not_assign_collection_or_conflicting_codes(db, monkeypatch, tmp_path):
    from app.database.models import History
    from app.modules import downloadclient
    from app.services.sourceidentity import SourceIdentityResolver
    with db() as session:
        session.add_all([History(hash='a', code='SOE-422'), History(hash='b', code='SSIS-001')])
        session.commit()
    first, second = tmp_path / 'unknown.mp4', tmp_path / 'another.mp4'
    monkeypatch.setattr(downloadclient, 'all_torrent_files_with_hashes', lambda: {str(first): ['a'], str(second): ['a']})
    assert SourceIdentityResolver().resolve(first) == ''
    monkeypatch.setattr(downloadclient, 'all_torrent_files_with_hashes', lambda: {str(first): ['a', 'b']})
    assert SourceIdentityResolver().resolve(first) == ''


def test_reverse_identity_prefers_registered_source_without_downloader(db, monkeypatch, tmp_path):
    from app.database.models import MediaLink
    from app.modules import downloadclient
    from app.services.sourceidentity import SourceIdentityResolver
    source = tmp_path / '演员名.mp4'
    with db() as session:
        session.add(MediaLink(source_path=str(source), link_path=str(tmp_path / 'SOE-422.mp4'), code='SOE-422'))
        session.commit()
    monkeypatch.setattr(downloadclient, 'all_torrent_files_with_hashes', lambda: pytest.fail('已登记路径不应请求下载器'))
    assert SourceIdentityResolver().resolve(source) == 'SOE-422'


def test_bypass_retains_final_url_without_sending_proxy(monkeypatch):
    import httpx
    from app.core import config
    from app.modules.ladysite.base import fetch_via_bypass
    settings = config.get_settings()
    monkeypatch.setattr(settings, 'bypass_url', 'http://solver.test/v1')
    monkeypatch.setattr(settings, 'proxy', 'http://user:pass@[::1]:8888')
    def post(self, url, **kwargs):
        assert 'proxy' not in kwargs['json']
        assert 'session' not in kwargs['json']
        return httpx.Response(200, json={'status': 'ok', 'solution': {'response': '<h1>ABF-134</h1>', 'url': 'https://site.test/ABF-134'}}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    page = fetch_via_bypass('https://site.test/ABW-306')
    assert page.url.endswith('/ABF-134')
    assert not validate_page(page, page.url, 'ABW-306')


def test_real_source_boundary_rejects_echoed_request_identity(monkeypatch):
    from types import SimpleNamespace
    from app.modules import ladysite
    from app.modules.ladysite.base import CodeInfo
    site = SimpleNamespace(client=SimpleNamespace(last_page='<h1>Unrelated page without identity</h1>', last_url='https://site.test/v/hash'),
        crawler_original=lambda code: CodeInfo(code=code, title='A title copied onto the requested code'))
    monkeypatch.setattr(ladysite, '_get_site', lambda name: site)
    assert ladysite._fetch_detail('javdb', 'ABW-306') == {}
