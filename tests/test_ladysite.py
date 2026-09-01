"""资源站解析测试。

用构造的 HTML 片段验证解析逻辑，不发真实请求。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def ensure_tables():
    """建表。

    解析类站点在构造时会读 datasource 表拿地址与节流配置（SiteClient.from_source），
    表不存在就直接抛 OperationalError —— 单独跑这个文件时没有别的用例替它建表。

    必须先 import models：建表建的是 DBBase.metadata 里登记过的表，而登记发生在
    模型类被导入的那一刻。不导入就是一张空 metadata，create_all 什么也不建。
    """
    from app.database import models  # noqa: F401  导入即向 metadata 登记各表
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)


JAVDB_DETAIL = """
<html><body>
<h2 class="title is-4">
  <strong>SSIS-001</strong>
  <strong class="current-title">テスト作品タイトル</strong>
</h2>
<div class="video-cover"><img src="//c0.jdbstatic.com/thumbs/ss/ssis001.jpg"></div>
<nav class="movie-panel-info">
  <div class="panel-block"><strong>日期:</strong><span class="value">2021-07-01</span></div>
  <div class="panel-block"><strong>時長:</strong><span class="value">120 分鍾</span></div>
  <div class="panel-block"><strong>片商:</strong><span class="value">S1 NO.1 STYLE</span></div>
  <div class="panel-block"><strong>發行:</strong><span class="value">S1</span></div>
  <div class="panel-block"><strong>系列:</strong><span class="value">テストシリーズ</span></div>
  <div class="panel-block"><strong>評分:</strong><span class="value">4.52分, 由1234人評價</span></div>
  <div class="panel-block"><strong>類別:</strong><span class="value">
    <a href="/tags?c=1">単体作品</a><a href="/tags?c=2">ハイビジョン</a>
  </span></div>
  <div class="panel-block"><strong>演員:</strong><span class="value">
    <a href="/actors/x1">女優A</a><strong class="symbol female">♀</strong>
    <a href="/actors/x2">男優B</a><strong class="symbol male">♂</strong>
  </span></div>
</nav>
<div class="preview-images">
  <a class="tile-item" href="//c0.jdbstatic.com/previews/p1.jpg"></a>
  <a class="tile-item" href="//c0.jdbstatic.com/previews/p2.jpg"></a>
</div>
</body></html>
"""

JAVDB_SEARCH = """
<html><body><div class="movie-list">
  <div class="item"><a href="/v/aaaaa">
    <div class="video-title"><strong>SSIS-002</strong> 别的片</div></a></div>
  <div class="item"><a href="/v/bbbbb">
    <div class="video-title"><strong>SSIS-001</strong> 目标片</div></a></div>
</div></body></html>
"""

JAVDB_RANK = """
<html><body><div class="movie-list">
  <div class="item"><a href="/v/1"><div class="video-title"><strong>ABP-984</strong> t1</div></a></div>
  <div class="item"><a href="/v/2"><div class="video-title"><strong>mide777</strong> t2</div></a></div>
  <div class="item"><a href="/v/3"><div class="video-title"><strong></strong> 无番号</div></a></div>
</div></body></html>
"""

JAVBUS_DETAIL = """
<html><body>
<h3>ABP-984 テストタイトル</h3>
<div class="bigImage"><img src="/pics/cover/abc_b.jpg"></div>
<div class="info">
  <p><span class="header">識別碼:</span> ABP-984</p>
  <p><span class="header">發行日期:</span> 2021-08-01</p>
  <p><span class="header">長度:</span> 130分鐘</p>
  <p><span class="header">製作商:</span> プレステージ</p>
  <p><span class="header">發行商:</span> ABSOLUTELY PERFECT</p>
  <p class="star-show"><span class="header">系列:</span> テストシリーズ</p>
</div>
<span class="genre"><a href="/genre/1">ハイビジョン</a></span>
<span class="genre"><a href="/genre/2">単体作品</a></span>
<div class="star-name"><a href="/star/aaa">女優C</a></div>
<div id="sample-waterfall">
  <a class="sample-box" href="https://pics.javbus.com/sample/s1.jpg"></a>
</div>
</body></html>
"""

JAVLIB_RANK = """
<html><body>
<div class="video"><div class="id">SSIS-100</div></div>
<div class="video"><div class="id">abp985</div></div>
<div class="video"><div class="id">---</div></div>
</body></html>
"""

BRAND_DATE = """
<html><body>
<a href="/works/detail/ssis001/">A</a>
<a href="/works/detail/SSIS002/">B</a>
<a href="/works/detail/ssis001/">重复</a>
<a href="/other/page">无关</a>
</body></html>
"""

# /works/date 是日期索引页，番号在它列出的各个列表页里
BRAND_DATE_INDEX = """
<html><body>
<a href="https://s1s1s1.com/works/list/date/2026-08-25">预定</a>
<a href="/works/list/date/2026-08-11/">今天</a>
<a href="/works/list/date/2026-08-11">重复</a>
<a href="/works/list/date/badvalue">非日期</a>
<a href="/works/list/release">无关</a>
</body></html>
"""


class TestJavdbParse:
    def test_detail_fields(self):
        from app.modules.ladysite.javdb import html_to_code

        info = html_to_code(JAVDB_DETAIL, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.title == "テスト作品タイトル"
        assert info.release_date == "2021-07-01"
        assert info.duration == "120 分鍾"
        assert info.producer == "S1 NO.1 STYLE"
        assert info.publisher == "S1"
        assert info.series == "テストシリーズ"
        assert info.star == 4.52
        assert "単体作品" in info.genres and "ハイビジョン" in info.genres

    def test_detail_excludes_male_actor(self):
        """男优应被过滤掉，只保留女优。"""
        from app.modules.ladysite.javdb import html_to_code

        info = html_to_code(JAVDB_DETAIL, "SSIS-001")
        assert "女優A" in info.casts
        assert "男優B" not in info.casts

    def test_cover_upgraded_to_full_size(self):
        from app.modules.ladysite.javdb import html_to_code

        info = html_to_code(JAVDB_DETAIL, "SSIS-001")
        assert info.banner.startswith("https://")
        # thumbs 应被换成 covers
        assert "/covers/" in info.banner

    def test_preview_images_collected(self):
        from app.modules.ladysite.javdb import html_to_code

        info = html_to_code(JAVDB_DETAIL, "SSIS-001")
        assert info.still_photo.count(",") == 1

    def test_search_matches_exact_code_only(self):
        """搜索页有多条时必须精确匹配，不能拿错片。"""
        from app.modules.ladysite.javdb import html_to_detail_url

        assert html_to_detail_url(JAVDB_SEARCH, "SSIS-001") == "/v/bbbbb"
        assert html_to_detail_url(JAVDB_SEARCH, "SSIS-002") == "/v/aaaaa"

    def test_search_no_match_returns_empty(self):
        from app.modules.ladysite.javdb import html_to_detail_url

        assert html_to_detail_url(JAVDB_SEARCH, "ZZZZ-999") == ""

    def test_rank_normalizes_and_skips_empty(self):
        from app.modules.ladysite.javdb import html_to_rank

        codes = html_to_rank(JAVDB_RANK)
        assert codes == ["ABP-984", "MIDE-777"]

    def test_malformed_html_returns_none(self):
        from app.modules.ladysite.javdb import html_to_code

        assert html_to_code("<html><body>nothing</body></html>", "") is None

    @pytest.mark.parametrize("raw,expected_prefix", [
        ("//c0.jdbstatic.com/x.jpg", "https://"),
        ("https://c0.jdbstatic.com/x.jpg", "https://"),
        ("", ""),
    ])
    def test_convert_image_url(self, raw, expected_prefix):
        from app.modules.ladysite.javdb import convert_image_url
        assert convert_image_url(raw).startswith(expected_prefix)


class TestJavbusParse:
    def test_detail_fields(self):
        from app.modules.ladysite.bus import Bus

        info = Bus().html_to_code(JAVBUS_DETAIL, "ABP-984")
        assert info is not None
        assert info.code == "ABP-984"
        assert info.release_date == "2021-08-01"
        assert info.duration == "130分鐘"
        assert info.producer == "プレステージ"

    def test_title_strips_leading_code(self):
        """javbus 标题以番号开头，应剥离。"""
        from app.modules.ladysite.bus import Bus

        info = Bus().html_to_code(JAVBUS_DETAIL, "ABP-984")
        assert info.title == "テストタイトル"

    def test_genres_exclude_cast_names(self):
        from app.modules.ladysite.bus import Bus

        info = Bus().html_to_code(JAVBUS_DETAIL, "ABP-984")
        assert "女優C" in info.casts
        assert "女優C" not in info.genres
        assert "ハイビジョン" in info.genres

    def test_relative_image_becomes_absolute(self):
        from app.modules.ladysite.bus import Bus

        info = Bus().html_to_code(JAVBUS_DETAIL, "ABP-984")
        assert info.banner.startswith("https://www.javbus.com/")


JAVBUS_LIST = """
<html><body><div id="waterfall">
  <div class="item"><a href="/TLDC-058">
    <div class="photo-info"><span>标题<br>
      <date>TLDC-058</date><date>2026-08-10</date></span></div></a></div>
  <div class="item"><a href="/ROYD-344">
    <div class="photo-info"><span>标题<br>
      <date>ROYD-344</date><date>2026-08-09</date></span></div></a></div>
  <div class="item"><a href="/TLDC-058">
    <div class="photo-info"><span>重复<br>
      <date>TLDC-058</date><date>2026-08-10</date></span></div></a></div>
</div></body></html>
"""


class TestJavbusList:
    def test_extracts_codes_from_list_page(self):
        from app.modules.ladysite.bus import Bus

        codes = Bus().html_to_codes(JAVBUS_LIST)
        assert codes == ["TLDC-058", "ROYD-344"]

    def test_date_is_not_mistaken_for_code(self):
        """日期同样在 <date> 标签里，不能被当成番号。"""
        from app.modules.ladysite.bus import Bus

        codes = Bus().html_to_codes(JAVBUS_LIST)
        assert all("-0" not in c or not c.startswith("20") for c in codes)

    def test_empty_html_returns_empty(self):
        from app.modules.ladysite.bus import Bus

        assert Bus().html_to_codes("<html></html>") == []


class TestLibraryParse:
    def test_rank_normalizes(self):
        from app.modules.ladysite.library import html_to_rank

        assert html_to_rank(JAVLIB_RANK) == ["SSIS-100", "ABP-985"]


MISSAV_DETAIL = """
<html><head>
<meta property="og:image" content="https://fourhoi.com/ssis-001/cover-n.jpg">
</head><body>
<h1>SSIS-001 女友不在的三天 葵司</h1>
<div class="space-y-2">
  <div class="text-secondary"><span>发行日期:</span>
    <time datetime="2021-02-18T11:00:54+08:00">2021-02-18</time></div>
  <div class="text-secondary"><span>番号:</span><span>SSIS-001</span></div>
  <div class="text-secondary"><span>女优:</span>
    <a href="/a">葵司 (葵つかさ)</a>, <a href="/b">乙白沙也加</a></div>
  <div class="text-secondary"><span>类型:</span>
    <a href="/c">中文字幕</a>, <a href="/d">美乳</a></div>
  <div class="text-secondary"><span>发行商:</span><span>S1</span></div>
</div>
</body></html>
"""

# 番号不存在时站点返回 200，标题是提示语、封面是站点 logo
MISSAV_NOT_FOUND = """
<html><head>
<meta property="og:image" content="https://missav.ws/missav/logo-square.png">
</head><body><h1>找不到页面</h1></body></html>
"""


class TestMissavParse:
    def test_detail_fields(self):
        from app.modules.ladysite.missav import html_to_code

        info = html_to_code(MISSAV_DETAIL, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.release_date == "2021-02-18"
        assert info.publisher == "S1"
        assert "葵司 (葵つかさ)" in info.casts
        assert "中文字幕" in info.genres
        assert info.banner.endswith("cover-n.jpg")

    def test_title_strips_leading_code(self):
        """h1 以番号开头，应剥离后只留标题。"""
        from app.modules.ladysite.missav import html_to_code

        info = html_to_code(MISSAV_DETAIL, "SSIS-001")
        assert info.title == "女友不在的三天 葵司"

    def test_not_found_page_returns_none(self):
        """占位页没有发行日期，不能当详情写进库。"""
        from app.modules.ladysite.missav import html_to_code

        assert html_to_code(MISSAV_NOT_FOUND, "NOSUCH-999") is None


JAV321_DETAIL = """
<html><body>
<div class="panel panel-info">
  <div class="panel-heading"><h3>作品タイトル <small>ssis-001 葵つかさ</small></h3></div>
  <div class="panel-body"><div class="row"><div class="col-md-9">
    <b>出演者</b>: <a href="/star/1">葵つかさ</a> &nbsp; <a href="/star/2">乙白さやか</a> &nbsp; <br>
    <b>メーカー</b>: <a href="/company/x/1">エスワン ナンバーワンスタイル</a><br>
    <b>品番</b>: ssis-001<br>
    <b>配信開始日</b>: 2021-02-19<br>
    <b>収録時間</b>: 147 minutes<br>
    <b>平均評価</b>: 4.5<br>
  </div></div></div>
</div>
<img src="http://pics.dmm.co.jp/digital/video/ssis00001/ssis00001ps.jpg">
<img src="http://pics.dmm.co.jp/digital/video/ssis00001/ssis00001jp-1.jpg">
<img src="http://pics.dmm.co.jp/digital/video/ssis00001/ssis00001jp-2.jpg">
<video><source src="https://example.com/preview.mp4" type="video/mp4"></video>
</body></html>
"""


class TestJav321Parse:
    def test_detail_fields(self):
        from app.modules.ladysite.jav321 import html_to_code

        info = html_to_code(JAV321_DETAIL, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.release_date == "2021-02-19"
        assert info.duration == "147 minutes"
        assert info.producer == "エスワン ナンバーワンスタイル"
        assert info.casts == "葵つかさ,乙白さやか"
        assert info.star == 4.5

    def test_title_drops_small_tag(self):
        """h3 里的 <small> 是番号与演员的重复信息，不该进标题。"""
        from app.modules.ladysite.jav321 import html_to_code

        info = html_to_code(JAV321_DETAIL, "SSIS-001")
        assert info.title == "作品タイトル"

    def test_cover_upgraded_and_stills_collected(self):
        from app.modules.ladysite.jav321 import html_to_code

        info = html_to_code(JAV321_DETAIL, "SSIS-001")
        # ps 是缩略图，应换成大图 pl
        assert info.banner.endswith("ssis00001pl.jpg")
        assert info.still_photo.count(",") == 1
        assert info.preview_url == "https://example.com/preview.mp4"

    @pytest.mark.parametrize("code,expected", [
        ("SSIS-001", "/video/ssis00001"),
        ("JUR-786", "/video/jur00786"),
        ("MIDE-777", "/video/mide00777"),
        ("NODASH", ""),
    ])
    def test_detail_path(self, code, expected):
        from app.modules.ladysite.jav321 import _detail_path
        assert _detail_path(code) == expected


AVBASE_DETAIL = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"work":{
  "work_id":"SSIS-001",
  "title":"作品タイトル",
  "min_date":"Thu Feb 18 2021 09:00:00 GMT+0900 (Japan Standard Time)",
  "casts":[{"actor":{"name":"葵つかさ"}},{"actor":{"name":"乙白さやか"}}],
  "genres":[{"name":"美少女"},{"name":"ドラマ"}],
  "products":[{
    "maker":{"name":"エスワン ナンバーワンスタイル"},
    "label":{"name":"S1 NO.1 STYLE"},
    "series":null,
    "image_url":"https://img/ssis00001pl.jpg",
    "thumbnail_url":"https://img/ssis00001ps.jpg",
    "sample_image_urls":[{"s":"https://img/s1.jpg","l":"https://img/l1.jpg"},
                         {"s":"https://img/s2.jpg","l":"https://img/l2.jpg"}]
  }]
}}}}
</script>
</body></html>
"""

# 番号不存在时 pageProps 里只有 statusCode
AVBASE_NOT_FOUND = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"statusCode":404}}}
</script>
</body></html>
"""


class TestAvbaseParse:
    def test_detail_fields(self):
        from app.modules.ladysite.avbase import html_to_code

        info = html_to_code(AVBASE_DETAIL, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.title == "作品タイトル"
        assert info.release_date == "2021-02-18"
        assert info.producer == "エスワン ナンバーワンスタイル"
        assert info.publisher == "S1 NO.1 STYLE"
        assert info.casts == "葵つかさ,乙白さやか"
        assert "美少女" in info.genres

    def test_prefers_large_still_images(self):
        """剧照是 {s: 小图, l: 大图}，应取大图。"""
        from app.modules.ladysite.avbase import html_to_code

        info = html_to_code(AVBASE_DETAIL, "SSIS-001")
        assert info.still_photo == "https://img/l1.jpg,https://img/l2.jpg"
        assert info.banner == "https://img/ssis00001pl.jpg"

    def test_missing_work_returns_none(self):
        """avbase 收录范围有限，没有的番号要返回 None 交给下一个源。"""
        from app.modules.ladysite.avbase import html_to_code

        assert html_to_code(AVBASE_NOT_FOUND, "JUR-786") is None

    def test_no_next_data_returns_none(self):
        from app.modules.ladysite.avbase import html_to_code

        assert html_to_code("<html><body>nothing</body></html>", "SSIS-001") is None

    @pytest.mark.parametrize("raw,expected", [
        ("Thu Feb 18 2021 09:00:00 GMT+0900 (Japan Standard Time)", "2021-02-18"),
        ("Mon Dec 01 2025 00:00:00 GMT+0900", "2025-12-01"),
        ("", ""),
        ("garbage", ""),
    ])
    def test_js_date(self, raw, expected):
        from app.modules.ladysite.avbase import _js_date
        assert _js_date(raw) == expected


FREEJAVBT_DETAIL = """
<html><body>
<h1>SSIS-001 作品标题 免费AV在线看</h1>
<div class="single-video-info">
  <div class="single-video-meta code d-flex"><span>番号:&nbsp;</span>
    <a href="/zh/code/SSIS">SSIS</a><span>-001</span></div>
  <div class="single-video-meta d-flex"><span>日期:&nbsp;</span><span>2021-02-19</span></div>
  <div class="single-video-meta d-flex"><span>时长:&nbsp;</span><span>150分钟</span></div>
  <div class="single-video-meta director d-flex"><span>导演:&nbsp;</span>
    <a href="/d/1">苺原</a></div>
  <div class="single-video-meta d-flex"><span>类别:&nbsp;</span>
    <a href="/g/1">戏剧</a><a href="/g/2">多P</a></div>
  <div class="single-video-meta d-flex"><span>女优:&nbsp;</span>
    <a href="/a/1">葵司</a></div>
</div>
</body></html>
"""


class TestFreejavbtParse:
    def test_detail_fields(self):
        from app.modules.ladysite.freejavbt import html_to_code

        info = html_to_code(FREEJAVBT_DETAIL, "SSIS-001")
        assert info is not None
        assert info.release_date == "2021-02-19"
        assert info.duration == "150分钟"
        assert "戏剧" in info.genres
        assert info.casts == "葵司"

    def test_title_strips_code_and_promo_suffix(self):
        from app.modules.ladysite.freejavbt import html_to_code

        info = html_to_code(FREEJAVBT_DETAIL, "SSIS-001")
        assert info.title == "作品标题"

    def test_director_not_mapped_to_producer(self):
        """导演不是片商，映射过去会把导演名当片商写库。"""
        from app.modules.ladysite.freejavbt import html_to_code

        info = html_to_code(FREEJAVBT_DETAIL, "SSIS-001")
        assert info.producer == ""

    def test_empty_meta_returns_none(self):
        from app.modules.ladysite.freejavbt import html_to_code

        assert html_to_code("<html><body><h1>X</h1></body></html>", "NOSUCH-999") is None


class TestBrandsParse:
    def test_date_page_dedupes_and_normalizes(self):
        from app.modules.ladysite.brands import Brands

        codes = Brands("s1").crawling_date(BRAND_DATE)
        assert codes == ["SSIS-001", "SSIS-002"]

    def test_date_index_collects_dates(self, monkeypatch):
        """索引页只取形如 YYYY-MM-DD 的日期，去重并按新到旧排。"""
        from app.modules.ladysite.brands import Brands

        monkeypatch.setattr(
            "app.modules.ladysite.base.SiteClient.get",
            lambda self, path, **kw: BRAND_DATE_INDEX,
        )
        assert Brands("s1").list_dates() == ["2026-08-25", "2026-08-11"]

    def test_date_index_returns_none_when_request_fails(self):
        """索引页请求失败返回 None，调用方据此报"站点不可达"。"""
        from app.modules.ladysite.brands import Brands

        site = Brands("s1")
        site.client.get = lambda path, **kw: ""
        assert site.list_dates() is None

    def test_unknown_brand_falls_back(self):
        from app.modules.ladysite.brands import Brands

        assert Brands("不存在的厂牌").client.host.startswith("https://")

    def test_date_rank_returns_none_when_request_fails(self, monkeypatch):
        """请求失败返回 None，与"这天没新片"的空列表区分开。"""
        from app.modules.ladysite.brands import Brands

        monkeypatch.setattr(
            "app.modules.ladysite.base.SiteClient.get",
            lambda self, *a, **kw: "",
        )
        assert Brands("s1").get_date_rank("2024-01-01") is None

    def test_date_rank_returns_empty_list_when_no_works(self, monkeypatch):
        from app.modules.ladysite.brands import Brands

        monkeypatch.setattr(
            "app.modules.ladysite.base.SiteClient.get",
            lambda self, *a, **kw: "<html><body>没有新片</body></html>",
        )
        assert Brands("s1").get_date_rank("2024-01-01") == []


class TestBrandCrawlRange:
    @pytest.fixture(autouse=True)
    def _stub_date_index(self, monkeypatch):
        """日期索引固定为今天起往前 40 天，避免真的去请求官网。

        crawl_range 只抓索引里落在区间内的日期，所以给足天数让各用例
        自己的 past_days/future_days 决定实际抓几天。
        """
        from datetime import date, timedelta

        days = [
            (date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(40)
        ]
        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.list_dates",
            lambda self: days,
        )

    def test_unreachable_index_raises(self, monkeypatch):
        """索引页都请求不通时报错，不能静默当成"没有作品"。"""
        from app.modules.ladysite.brands import BrandUnreachable, crawl_range

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.list_dates",
            lambda self: None,
        )
        with pytest.raises(BrandUnreachable):
            crawl_range("s1", past_days=2, future_days=1)

    def test_no_dates_in_range_returns_empty(self, monkeypatch):
        """索引页通但区间内没有任何发行日，返回空列表而非报错。"""
        from app.modules.ladysite.brands import crawl_range

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.list_dates",
            lambda self: [],
        )
        assert crawl_range("s1", past_days=2, future_days=1) == []

    def test_all_requests_failing_raises(self, monkeypatch):
        """整段区间都抓不到时报错，不能静默当成"没有作品"。"""
        from app.modules.ladysite.brands import BrandUnreachable, crawl_range

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.get_date_rank",
            lambda self, day: None,
        )
        with pytest.raises(BrandUnreachable):
            crawl_range("s1", past_days=2, future_days=1)

    def test_empty_but_reachable_returns_empty(self, monkeypatch):
        """站点通但确实没作品，返回空列表而非报错。"""
        from app.modules.ladysite.brands import crawl_range

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.get_date_rank",
            lambda self, day: [],
        )
        assert crawl_range("s1", past_days=2, future_days=1) == []

    def test_partial_failure_keeps_results(self, monkeypatch):
        """站点通但个别日期抓失败时，其余结果照常保留。"""
        from app.modules.ladysite.brands import crawl_range

        calls = []

        def _rank(self, day):
            calls.append(day)
            # 第 2 天失败，其余正常返回（多数日期没有新片）
            if len(calls) == 2:
                return None
            return ["SSIS-001"] if len(calls) == 3 else []

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.get_date_rank", _rank
        )
        found = crawl_range("s1", past_days=5, future_days=0)
        assert [i["code"] for i in found] == ["SSIS-001"]

    def test_early_stop_on_consecutive_failures(self, monkeypatch):
        """开头连续失败就放弃，不把剩下的日期挨个撞满超时。"""
        from app.modules.ladysite.brands import (
            UNREACHABLE_THRESHOLD, BrandUnreachable, crawl_range,
        )

        calls = []

        def _rank(self, day):
            calls.append(day)
            return None

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.get_date_rank", _rank
        )
        with pytest.raises(BrandUnreachable):
            crawl_range("s1", past_days=30, future_days=0)
        assert len(calls) == UNREACHABLE_THRESHOLD

    def test_failure_after_results_does_not_stop(self, monkeypatch):
        """已经抓到数据后再遇失败不早停，避免丢掉后面的日期。"""
        from app.modules.ladysite.brands import UNREACHABLE_THRESHOLD, crawl_range

        calls = []

        def _rank(self, day):
            calls.append(day)
            if len(calls) == 1:
                return ["SSIS-001"]
            return None

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.get_date_rank", _rank
        )
        found = crawl_range("s1", past_days=6, future_days=0)
        assert [i["code"] for i in found] == ["SSIS-001"]
        # 全部日期都走完，没有因连续失败提前退出
        assert len(calls) > UNREACHABLE_THRESHOLD

    def test_dedupes_across_days(self, monkeypatch):
        from app.modules.ladysite.brands import crawl_range

        monkeypatch.setattr(
            "app.modules.ladysite.brands.Brands.get_date_rank",
            lambda self, day: ["SSIS-001"],
        )
        found = crawl_range("s1", past_days=3, future_days=2)
        assert [i["code"] for i in found] == ["SSIS-001"]


class TestAggregator:
    def test_imports(self):
        from app.modules import ladysite
        assert hasattr(ladysite, "get_code_detail")
        assert hasattr(ladysite, "sync_hot")
        assert hasattr(ladysite, "get_rank_codes")

    def test_empty_code_returns_empty_dict(self):
        from app.modules import ladysite
        assert ladysite.get_code_detail("") == {}

    def test_code_info_to_dict_skips_empty(self):
        from app.modules.ladysite.base import CodeInfo

        data = CodeInfo(code="X-1", title="t").to_dict()
        assert data == {"code": "X-1", "title": "t"}

    def test_join_list_dedupes_preserving_order(self):
        from app.modules.ladysite.base import join_list

        assert join_list(["b", "a", "b", "", None, "c"]) == "b,a,c"

    @pytest.mark.parametrize("text,expected", [
        ("4.52分, 由1234人評價", 4.52),
        ("3.8", 3.8),
        ("", None),
        ("abc", None),
        ("99分", None),  # 超出 0-10 视为无效
    ])
    def test_parse_star(self, text, expected):
        from app.modules.ladysite.base import parse_star
        assert parse_star(text) == expected


# ----------------------------------------------------------------------
# avmoo / avsox —— 与 javbus 同模板，复用 Bus 的解析
# ----------------------------------------------------------------------
AVMOO_SEARCH = """
<html><body>
  <div class="item"><a href="/cn/movie/aaaa">
    <div class="photo-info"><span>别的片<br>
      <date>SSIS-0011</date><date>2021-02-20</date></span></div></a></div>
  <div class="item"><a href="/cn/movie/bbbb">
    <div class="photo-info"><span>目标片<br>
      <date>SSIS-001</date><date>2021-02-18</date></span></div></a></div>
</body></html>
"""

AVMOO_DETAIL = """
<html><body>
<h3>SSIS-001 テストタイトル</h3>
<div class="bigImage"><img src="/imgs/cover/ssis001.jpg"></div>
<div class="info">
  <p><span class="header">識別碼:</span> SSIS-001</p>
  <p><span class="header">發行日期:</span> 2021-02-18</p>
  <p><span class="header">長度:</span> 147分鐘</p>
  <p><span class="header">製作商:</span> エスワン</p>
</div>
<span class="genre"><a href="/genre/1">ハイビジョン</a></span>
<div class="star-name"><a href="/star/aaa">葵つかさ</a></div>
</body></html>
"""


class TestAvmooParse:
    def test_search_matches_exact_code_only(self):
        """SSIS-0011 不能被当成 SSIS-001 的结果。"""
        from app.modules.ladysite.avmoo import html_to_detail_url

        assert html_to_detail_url(AVMOO_SEARCH, "SSIS-001") == "/cn/movie/bbbb"
        assert html_to_detail_url(AVMOO_SEARCH, "SSIS-0011") == "/cn/movie/aaaa"

    def test_search_no_match_returns_empty(self):
        from app.modules.ladysite.avmoo import html_to_detail_url

        assert html_to_detail_url(AVMOO_SEARCH, "ZZZZ-999") == ""

    def test_detail_reuses_bus_parser(self):
        """模板与 javbus 一致，字段应照样解析出来。"""
        from app.modules.ladysite.avmoo import Avmoo

        info = Avmoo(host="https://avmoo.website").html_to_code(AVMOO_DETAIL, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.title == "テストタイトル"
        assert info.release_date == "2021-02-18"
        assert info.producer == "エスワン"
        assert info.casts == "葵つかさ"
        assert info.banner.startswith("https://avmoo.website/")

    def test_list_page_dedupes(self):
        from app.modules.ladysite.avmoo import html_to_codes

        assert html_to_codes(AVMOO_SEARCH) == ["SSIS-0011", "SSIS-001"]

    def test_avsox_shares_implementation(self):
        from app.modules.ladysite.avmoo import Avsox

        assert Avsox(host="https://avsox.click").name == "avsox"

    def test_mismatched_detail_is_discarded(self, monkeypatch):
        """搜索命中但详情页是别的番号时必须丢弃，不能写错数据。"""
        from app.modules.ladysite.avmoo import Avmoo

        site = Avmoo(host="https://avmoo.website")
        monkeypatch.setattr(site, "search_detail_url", lambda code: "/cn/movie/bbbb")
        monkeypatch.setattr(site.client, "get", lambda *a, **kw: AVMOO_DETAIL)
        # 详情页里是 SSIS-001，查 SSIS-002 应被拒
        assert site.crawler_original("SSIS-002") is None
        assert site.crawler_original("SSIS-001") is not None


# ----------------------------------------------------------------------
# DMM
# ----------------------------------------------------------------------
DMM_DETAIL = """
<html><head>
<meta property="og:image" content="https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001pl.jpg">
</head><body>
<h1>テスト作品タイトル</h1>
<table class="mg-b20"><tr>
  <td>商品発売日：</td><td>2021/02/19</td></tr>
  <tr><td>収録時間：</td><td>147分</td></tr>
  <tr><td>出演者：</td><td><a href="/x/1">葵つかさ</a><a href="/x/2">乙白さやか</a></td></tr>
  <tr><td>メーカー：</td><td><a href="/m/1">エスワン ナンバーワンスタイル</a></td></tr>
  <tr><td>レーベル：</td><td><a href="/l/1">S1 NO.1 STYLE</a></td></tr>
  <tr><td>シリーズ：</td><td>----</td></tr>
  <tr><td>ジャンル：</td><td><a href="/g/1">美少女</a><a href="/g/2">単体作品</a></td></tr>
  <tr><td>品番：</td><td>ssis00001</td></tr>
</table>
<div id="sample-image-block">
  <img src="https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001-1.jpg">
  <img src="https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001-2.jpg">
</div>
</body></html>
"""

DMM_AGE_CHECK = """
<html><body><a href="/age_check/=/declared=yes/">はい</a>
<p>年齢確認</p></body></html>
"""

DMM_SEARCH = """
<html><body>
<a href="/digital/videoa/-/detail/=/cid=ssis00002/">别的片</a>
<a href="/digital/videoa/-/detail/=/cid=h_1234ssis00001/">带前缀的同作品</a>
<a href="/digital/videoa/-/detail/=/cid=ssis00001/">目标片</a>
</body></html>
"""


class TestDmmParse:
    def test_detail_fields(self):
        from app.modules.ladysite.dmm import html_to_code

        info = html_to_code(DMM_DETAIL, "SSIS-001")
        assert info is not None
        assert info.title == "テスト作品タイトル"
        assert info.release_date == "2021-02-19"
        assert info.duration == "147分"
        assert info.producer == "エスワン ナンバーワンスタイル"
        assert info.publisher == "S1 NO.1 STYLE"
        assert info.casts == "葵つかさ,乙白さやか"
        assert "美少女" in info.genres

    def test_placeholder_dashes_are_skipped(self):
        """DMM 用「----」表示"无"，不能写成字段值。"""
        from app.modules.ladysite.dmm import html_to_code

        info = html_to_code(DMM_DETAIL, "SSIS-001")
        assert info.series == ""

    def test_stills_upgraded_to_large(self):
        """剧照缩略图 -1.jpg 要换成大图 jp-1.jpg。"""
        from app.modules.ladysite.dmm import html_to_code

        info = html_to_code(DMM_DETAIL, "SSIS-001")
        assert "ssis00001jp-1.jpg" in info.still_photo
        assert info.still_photo.count(",") == 1

    def test_age_check_page_returns_none(self):
        """年龄确认页是 200，不能当详情页解析。"""
        from app.modules.ladysite.dmm import html_to_code

        assert html_to_code(DMM_AGE_CHECK, "SSIS-001") is None

    def test_age_cookie_is_added_automatically(self):
        """缺 age_check_done 整站抓不到，必须由代码补上。"""
        from app.modules.ladysite.dmm import Dmm

        assert "age_check_done=1" in Dmm(host="https://www.dmm.co.jp").client.cookie

    def test_age_cookie_preserves_user_cookie(self):
        from app.modules.ladysite.dmm import Dmm

        site = Dmm(host="https://www.dmm.co.jp", cookie="foo=bar")
        assert "foo=bar" in site.client.cookie
        assert "age_check_done=1" in site.client.cookie

    @pytest.mark.parametrize("code,expected", [
        ("SSIS-001", "ssis00001"),
        ("MIDE-777", "mide00777"),
        ("NODASH", ""),
    ])
    def test_to_cid(self, code, expected):
        from app.modules.ladysite.dmm import _to_cid
        assert _to_cid(code) == expected

    def test_search_prefers_exact_cid(self):
        """精确 cid 优先于带 h_ 前缀的同作品。"""
        from app.modules.ladysite.dmm import html_to_detail_url

        url = html_to_detail_url(DMM_SEARCH, "SSIS-001")
        assert url == "/digital/videoa/-/detail/=/cid=ssis00001/"

    def test_looks_like_detail_rejects_age_page(self):
        from app.modules.ladysite.dmm import _looks_like_detail

        assert _looks_like_detail(DMM_DETAIL) is True
        assert _looks_like_detail(DMM_AGE_CHECK) is False
        assert _looks_like_detail("") is False


# ----------------------------------------------------------------------
# MGStage
# ----------------------------------------------------------------------
MGSTAGE_DETAIL = """
<html><head>
<meta property="og:image" content="https://image.mgstage.com/images/siro/4321/pb_e_siro-4321.jpg">
</head><body>
<h1 class="tag">SIRO-4321 【初撮り】ネットでAV応募</h1>
<table>
  <tr><th>出演：</th><td><a href="/a/1">しほ</a></td></tr>
  <tr><th>メーカー：</th><td>SIRO</td></tr>
  <tr><th>レーベル：</th><td>シロウトTV</td></tr>
  <tr><th>シリーズ：</th><td>----</td></tr>
  <tr><th>配信開始日：</th><td>2021/02/19</td></tr>
  <tr><th>収録時間：</th><td>60min</td></tr>
  <tr><th>ジャンル：</th><td><a href="/g/1">素人</a><a href="/g/2">ハメ撮り</a></td></tr>
</table>
</body></html>
"""


class TestMgstageParse:
    def test_detail_fields(self):
        from app.modules.ladysite.mgstage import html_to_code

        info = html_to_code(MGSTAGE_DETAIL, "SIRO-4321")
        assert info is not None
        assert info.code == "SIRO-4321"
        assert info.release_date == "2021-02-19"
        assert info.duration == "60min"
        assert info.producer == "SIRO"
        assert info.publisher == "シロウトTV"
        assert info.casts == "しほ"
        assert "素人" in info.genres

    def test_title_strips_leading_code(self):
        from app.modules.ladysite.mgstage import html_to_code

        info = html_to_code(MGSTAGE_DETAIL, "SIRO-4321")
        assert info.title == "【初撮り】ネットでAV応募"

    def test_placeholder_dashes_are_skipped(self):
        from app.modules.ladysite.mgstage import html_to_code

        assert html_to_code(MGSTAGE_DETAIL, "SIRO-4321").series == ""

    def test_missing_date_returns_none(self):
        from app.modules.ladysite.mgstage import html_to_code

        assert html_to_code("<html><body><h1>エラー</h1></body></html>", "X-1") is None

    def test_age_cookie_is_added(self):
        from app.modules.ladysite.mgstage import Mgstage

        assert "adc=1" in Mgstage(host="https://www.mgstage.com").client.cookie


# ----------------------------------------------------------------------
# FC2
# ----------------------------------------------------------------------
FC2_DETAIL = """
<html><body>
<div class="items_article_headerInfo">
  <h3>【個人撮影】テスト作品 FC2-PPV</h3>
  <a href="/users/seller123/">売主テスト</a>
</div>
<div class="items_article_Releasedate"><p>販売日 : 2021/02/19</p></div>
<div class="items_article_TagArea"><a href="/tag/1">素人</a><a href="/tag/2">個人撮影</a></div>
<meta property="og:image" content="https://contents-thumbnail.fc2.com/1234567.jpg">
</body></html>
"""

FC2HUB_DETAIL = """
<html><body>
<h1>FC2-PPV-1234567 テスト作品</h1>
<div class="video-info">
  <a href="/actress/1">演員テスト</a>
  <a href="/tag/1">素人</a>
  <p>発売日: 2021-02-19</p>
</div>
<meta property="og:image" content="https://javten.com/img/1234567.jpg">
</body></html>
"""


class TestFc2Parse:
    @pytest.mark.parametrize("code,expected", [
        ("FC2-PPV-1234567", "1234567"),
        ("FC2-1234567", "1234567"),
        # 普通番号的数字段不足 5 位，取不出 article id
        ("SSIS-001", ""),
        ("", ""),
    ])
    def test_article_id(self, code, expected):
        from app.modules.ladysite.fc2 import _article_id
        assert _article_id(code) == expected

    def test_detail_fields(self):
        from app.modules.ladysite.fc2 import html_to_code

        info = html_to_code(FC2_DETAIL, "FC2-PPV-1234567")
        assert info is not None
        assert info.code == "FC2-PPV-1234567"
        assert info.release_date == "2021-02-19"
        assert "素人" in info.genres
        assert info.banner.endswith("1234567.jpg")

    def test_title_strips_promo_suffix(self):
        from app.modules.ladysite.fc2 import html_to_code

        info = html_to_code(FC2_DETAIL, "FC2-PPV-1234567")
        assert info.title == "【個人撮影】テスト作品"

    def test_missing_date_returns_none(self):
        """作品下架时是 200 提示页，没有販売日。"""
        from app.modules.ladysite.fc2 import html_to_code

        assert html_to_code("<html><body><h3>削除されました</h3></body></html>", "FC2-PPV-1") is None

    def test_non_fc2_code_skipped(self):
        """官方站只收自家投稿，普通番号不该发请求。"""
        from app.modules.ladysite.fc2 import Fc2

        site = Fc2(host="https://adult.contents.fc2.com")
        site.client.get = lambda *a, **kw: pytest.fail("不应发起请求")
        assert site.crawler_original("SSIS-001") is None

    def test_hub_detail_fields(self):
        from app.modules.ladysite.fc2 import hub_html_to_code

        info = hub_html_to_code(FC2HUB_DETAIL, "FC2-PPV-1234567")
        assert info is not None
        assert info.release_date == "2021-02-19"
        assert info.casts == "演員テスト"
        assert info.title == "テスト作品"

    @pytest.mark.parametrize("raw,expected", [
        ("販売日 : 2021/02/19", "2021-02-19"),
        ("2021年2月19日", "2021-02-19"),
        ("no date here", ""),
    ])
    def test_normalize_date(self, raw, expected):
        from app.modules.ladysite.fc2 import _normalize_date
        assert _normalize_date(raw) == expected


# ----------------------------------------------------------------------
# Airav
# ----------------------------------------------------------------------
AIRAV_JSON = """
{"barcode":"SSIS-001","name":"女友不在的三天",
 "publish_date":"2021-02-18T00:00:00",
 "actors":[{"name":"葵司"},{"name":"乙白沙也加"}],
 "tags":[{"name":"中文字幕"},{"name":"美乳"}],
 "factories":[{"name":"S1"}],
 "description":"女友不在的三天裡，我和她的姐姐發生了關係。",
 "img_url":"https://airav.io/img/ssis001.jpg"}
"""

AIRAV_JSON_EMPTY = '{"count":0}'

AIRAV_HTML = """
<html><head>
<meta property="og:image" content="https://airav.io/img/ssis001.jpg">
</head><body>
<h1>SSIS-001 女友不在的三天</h1>
<ul class="video-info">
  <li><span>發行日期</span>2021-02-18</li>
  <li><span>播放時間</span>147分鐘</li>
  <li><span>片商</span>S1</li>
  <li><span>女優</span><a href="/a/1">葵司</a><a href="/a/2">乙白沙也加</a></li>
  <li><span>類型</span><a href="/t/1">中文字幕</a></li>
</ul>
</body></html>
"""


class TestAiravParse:
    def test_json_fields(self):
        from app.modules.ladysite.airav import json_to_code

        info = json_to_code(AIRAV_JSON, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.title == "女友不在的三天"
        assert info.release_date == "2021-02-18"
        assert info.casts == "葵司,乙白沙也加"
        assert "中文字幕" in info.genres
        assert info.producer == "S1"

    def test_json_empty_returns_none(self):
        from app.modules.ladysite.airav import json_to_code

        assert json_to_code(AIRAV_JSON_EMPTY, "NOSUCH-999") is None

    def test_简介繁转简(self):
        """接口按 lng=zh-TW 请求，拿到的是繁体。

        媒体库里其余字段都是简体，NFO 的 plot 混着繁体很难看。
        airav 是目前唯一给简介的源，这个字段进 NFO 的 <plot>。
        """
        from app.modules.ladysite.airav import json_to_code

        info = json_to_code(AIRAV_JSON, "SSIS-001")
        assert info.outline == "女友不在的三天里，我和她的姐姐发生了关系。"

    def test_简介清掉HTML标签(self):
        from app.modules.ladysite.airav import _clean_outline

        assert _clean_outline("第一句。<br>第二句。") == "第一句。 第二句。"
        assert _clean_outline("<p>帶標籤</p>") == "带标签"

    def test_简介缺失时为空(self):
        """绝大多数站不给简介，空是常态，不能因此判定番号不存在。"""
        from app.modules.ladysite.airav import _clean_outline, json_to_code

        assert _clean_outline(None) == ""
        assert _clean_outline("") == ""
        # 非字符串（接口偶尔给 null 之外的类型）不能抛
        assert _clean_outline(123) == ""

        info = json_to_code(
            '{"barcode":"ABS-001","name":"标题"}', "ABS-001"
        )
        assert info is not None
        assert info.outline == ""

    def test_简介超长截断在句末(self):
        """几千字的营销文案灌进 Emby 简介栏会把 AI 看点挤到折叠线以下。"""
        from app.modules.ladysite.airav import OUTLINE_MAX_CHARS, _clean_outline

        got = _clean_outline("這是一段簡介。" * 200)
        assert len(got) <= OUTLINE_MAX_CHARS
        # 切在句末而不是半句上
        assert got.endswith("。")

    def test_html_fallback_when_api_returns_html(self):
        """接口路径没了会返回 HTML，此时应走 HTML 分支而非报错。"""
        from app.modules.ladysite.airav import json_to_code

        assert json_to_code(AIRAV_HTML, "SSIS-001") is None

    def test_html_fields(self):
        from app.modules.ladysite.airav import html_to_code

        info = html_to_code(AIRAV_HTML, "SSIS-001")
        assert info is not None
        assert info.release_date == "2021-02-18"
        assert info.title == "女友不在的三天"
        assert info.casts == "葵司,乙白沙也加"

    @pytest.mark.parametrize("host,expected", [
        ("https://airav.io/cn", "https://airav.io"),
        ("https://airav.io", "https://airav.io"),
    ])
    def test_root_strips_language_path(self, host, expected):
        """内置地址带 /cn，接口在站点根上，拼接前要剥掉。"""
        from app.modules.ladysite.airav import _root
        assert _root(host) == expected


# ----------------------------------------------------------------------
# 7mmTV
# ----------------------------------------------------------------------
MMTV_DETAIL = """
<html><head>
<meta property="og:image" content="https://7mmtv.sx/img/ssis001.jpg">
</head><body>
<h1>SSIS-001 測試標題</h1>
<ul class="video-info">
  <li><strong>發行日期:</strong>2021-02-18</li>
  <li><strong>播放時長:</strong>147分</li>
  <li><strong>製作商:</strong>S1</li>
  <li><strong>演員:</strong><a href="/a/1">葵司</a></li>
  <li><strong>類型:</strong><a href="/t/1">中文字幕</a></li>
</ul>
</body></html>
"""

MMTV_SEARCH = """
<html><body>
<a href="/zh/uncensored_content/ssis-001.html" title="SSIS-001 測試標題">目标</a>
<a href="/zh/about">无关</a>
</body></html>
"""


MMTV_SEARCH_VARIANT = """
<html><body>
<a href="/zh/censored/ssis-0011.html" title="SSIS-0011 別的片">变体</a>
</body></html>
"""


class TestMmtvParse:
    def test_detail_fields(self):
        from app.modules.ladysite.mmtv import html_to_code

        info = html_to_code(MMTV_DETAIL, "SSIS-001")
        assert info is not None
        assert info.release_date == "2021-02-18"
        assert info.producer == "S1"
        assert info.casts == "葵司"
        assert info.title == "測試標題"

    def test_search_finds_detail_url(self):
        from app.modules.ladysite.mmtv import html_to_detail_url

        assert html_to_detail_url(MMTV_SEARCH, "SSIS-001").endswith("ssis-001.html")

    def test_search_no_match_returns_empty(self):
        from app.modules.ladysite.mmtv import html_to_detail_url

        assert html_to_detail_url(MMTV_SEARCH, "ZZZZ-999") == ""

    def test_search_rejects_longer_variant(self):
        """SSIS-001 不能命中 SSIS-0011 —— 子串包含会拿错片。"""
        from app.modules.ladysite.mmtv import html_to_detail_url

        assert html_to_detail_url(MMTV_SEARCH_VARIANT, "SSIS-001") == ""
        assert html_to_detail_url(MMTV_SEARCH_VARIANT, "SSIS-0011") != ""

    def test_missing_date_returns_none(self):
        from app.modules.ladysite.mmtv import html_to_code

        assert html_to_code("<html><body><h1>X</h1></body></html>", "X-1") is None

    def test_language_prefix_not_doubled(self):
        """内置 host 带 /zh，站内路径也带 /zh，拼接不能出 /zh/zh。"""
        from app.modules.ladysite.mmtv import Mmtv

        site = Mmtv(host="https://7mmtv.sx/zh")
        seen = []

        def fake_get(path, **kw):
            seen.append(path)
            return ""

        site.client.get = fake_get
        site.search_detail_url("SSIS-001")
        assert seen == ["https://7mmtv.sx/zh/search/"]

    def test_relative_detail_joined_to_root(self):
        """搜索结果的相对路径自带 /zh，要拼到站点根而不是 host 上。"""
        from app.modules.ladysite.mmtv import Mmtv

        site = Mmtv(host="https://7mmtv.sx/zh")
        site.search_detail_url = lambda code: "/zh/censored/ssis-001.html"
        seen = []
        site.client.get = lambda path, **kw: seen.append(path) or ""
        site.crawler_original("SSIS-001")
        assert seen == ["https://7mmtv.sx/zh/censored/ssis-001.html"]


# ----------------------------------------------------------------------
# Caribbeancom —— 日期型番号
# ----------------------------------------------------------------------
CARIB_DETAIL = """
<html><head>
<meta property="og:image" content="https://www.caribbeancom.com/moviepages/032416-267/images/l_l.jpg">
</head><body>
<h1 class="heading">テスト無修正作品</h1>
<div class="movie-info">
  <dl><dt>出演</dt><dd><a href="/a/1">女優D</a></dd></dl>
  <dl><dt>配信日</dt><dd>2016/03/24</dd></dl>
  <dl><dt>再生時間</dt><dd>60:12</dd></dl>
  <dl><dt>シリーズ</dt><dd>テストシリーズ</dd></dl>
  <dl><dt>タグ</dt><dd><a href="/t/1">単体作品</a><a href="/t/2">無修正</a></dd></dl>
</div>
</body></html>
"""


class TestCaribParse:
    @pytest.mark.parametrize("code,expected", [
        ("032416_267", "032416-267"),
        ("032416-267", "032416-267"),
        ("SSIS-001", ""),      # 普通番号不属于本站
        ("", ""),
    ])
    def test_to_path_code(self, code, expected):
        """get_true_code 统一成下划线，但站点路径用横杠。"""
        from app.modules.ladysite.carib import _to_path_code
        assert _to_path_code(code) == expected

    def test_detail_fields(self):
        from app.modules.ladysite.carib import html_to_code

        info = html_to_code(CARIB_DETAIL, "032416_267")
        assert info is not None
        assert info.code == "032416_267"
        assert info.title == "テスト無修正作品"
        assert info.release_date == "2016-03-24"
        assert info.duration == "60:12"
        assert info.casts == "女優D"
        assert "無修正" in info.genres

    def test_normal_code_skipped(self):
        """本站只收日期型番号，普通番号不该发请求。"""
        from app.modules.ladysite.carib import Carib

        site = Carib(host="https://www.caribbeancom.com")
        site.client.get = lambda *a, **kw: pytest.fail("不应发起请求")
        assert site.crawler_original("SSIS-001") is None

    def test_missing_date_returns_none(self):
        from app.modules.ladysite.carib import html_to_code

        assert html_to_code("<html><body><h1>X</h1></body></html>", "032416_267") is None


# ----------------------------------------------------------------------
# ThePornDB —— JSON API
# ----------------------------------------------------------------------
TPDB_JSON = """
{"data":{"external_id":"SSIS-001","title":"Test Title","date":"2021-02-18",
 "duration":8820,
 "performers":[{"name":"Tsukasa Aoi"}],
 "tags":[{"name":"Featured"}],
 "site":{"name":"S1 NO.1 STYLE","network":{"name":"S1"}},
 "posters":[{"full":"https://tpdb/poster.jpg"}],
 "background":{"full":"https://tpdb/bg.jpg"}}}
"""


class TestThePornDbParse:
    def test_json_fields(self):
        from app.modules.ladysite.theporndb import json_to_code

        info = json_to_code(TPDB_JSON, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.title == "Test Title"
        assert info.release_date == "2021-02-18"
        assert info.casts == "Tsukasa Aoi"
        assert info.producer == "S1 NO.1 STYLE"
        assert info.poster == "https://tpdb/poster.jpg"
        assert info.banner == "https://tpdb/bg.jpg"

    def test_duration_seconds_to_minutes(self):
        """接口给的是秒，要换成分钟。"""
        from app.modules.ladysite.theporndb import json_to_code

        assert json_to_code(TPDB_JSON, "SSIS-001").duration == "147分钟"

    def test_empty_data_returns_none(self):
        from app.modules.ladysite.theporndb import json_to_code

        assert json_to_code('{"data":null}', "X-1") is None
        assert json_to_code('{"data":[]}', "X-1") is None

    def test_malformed_json_returns_none(self):
        from app.modules.ladysite.theporndb import json_to_code

        assert json_to_code("<html>not json</html>", "X-1") is None

    @pytest.mark.parametrize("raw,expected", [
        ("abc123", "abc123"),
        ("Bearer abc123", "abc123"),
        ("Authorization: Bearer abc123", "abc123"),
        ("", ""),
    ])
    def test_token_accepts_both_forms(self, raw, expected):
        """Token 填裸值或整行 Authorization 都要认。"""
        from app.modules.ladysite.theporndb import ThePornDb

        site = ThePornDb(host="https://api.theporndb.net", cookie=raw or "x")
        site.client.cookie = raw
        assert site._token() == expected

    def test_no_token_skips_request(self):
        """没配 Token 接口一律 401，不该白发请求。"""
        from app.modules.ladysite.theporndb import ThePornDb

        site = ThePornDb(host="https://api.theporndb.net")
        site.client.cookie = ""
        site.client.get = lambda *a, **kw: pytest.fail("不应发起请求")
        assert site.crawler_original("SSIS-001") is None


# ----------------------------------------------------------------------
# 国产站 madou / madouqu
# ----------------------------------------------------------------------
MADOU_DETAIL = """
<html><head>
<meta property="og:image" content="https://madou.club/img/mdx0123.jpg">
</head><body>
<article>
<h1 class="entry-title">MDX-0123 测试国产作品</h1>
<div class="entry-meta"><time datetime="2024-03-15">2024-03-15</time></div>
<div class="tags">
  <a rel="tag" href="/actor/1">女优E</a>
  <a rel="tag" href="/tag/2">剧情</a>
</div>
</article>
</body></html>
"""

MADOU_SEARCH = """
<html><body>
<article><h2><a href="/mdx-0123-test/" title="MDX-0123 测试国产作品">目标</a></h2></article>
<article><h2><a href="/other/" title="MDX-0456 别的片">别的</a></h2></article>
</body></html>
"""

MADOU_NOT_FOUND = """
<html><body><article><h1 class="entry-title">没有找到内容</h1></article></body></html>
"""


class TestMadouParse:
    def test_detail_fields(self):
        from app.modules.ladysite.madou import html_to_code

        info = html_to_code(MADOU_DETAIL, "MDX-0123")
        assert info is not None
        assert info.code == "MDX-0123"
        assert info.title == "测试国产作品"
        assert info.release_date == "2024-03-15"
        assert info.casts == "女优E"
        assert "剧情" in info.genres

    def test_cast_and_tag_links_are_separated(self):
        """演员与标签都是 rel=tag，靠 URL 段区分，不能混。"""
        from app.modules.ladysite.madou import html_to_code

        info = html_to_code(MADOU_DETAIL, "MDX-0123")
        assert "剧情" not in info.casts
        assert "女优E" not in info.genres

    def test_search_matches_flattened_code(self):
        """国产站番号写法混乱（MDX-0123/MDX0123），归一化后比对。"""
        from app.modules.ladysite.madou import html_to_detail_url

        assert html_to_detail_url(MADOU_SEARCH, "MDX-0123") == "/mdx-0123-test/"
        assert html_to_detail_url(MADOU_SEARCH, "MDX0123") == "/mdx-0123-test/"

    def test_not_found_page_returns_none(self):
        """搜不到时 WordPress 返回提示页，h1 是提示语不是片名。"""
        from app.modules.ladysite.madou import html_to_code

        assert html_to_code(MADOU_NOT_FOUND, "MDX-0123") is None

    def test_madouqu_shares_implementation(self):
        from app.modules.ladysite.madou import Madouqu

        site = Madouqu(host="https://madouqu.com")
        assert site.name == "madouqu"


# ----------------------------------------------------------------------
# XChina
# ----------------------------------------------------------------------
XCHINA_DETAIL = """
<html><head>
<meta property="og:image" content="https://xchina.co/img/cover.jpg">
</head><body>
<h1>MDX-0123 测试作品</h1>
<div class="items">
  <div class="item"><span class="label">番号</span><span class="value">MDX-0123</span></div>
  <div class="item"><span class="label">发行日期</span><span class="value">2024-03-15</span></div>
  <div class="item"><span class="label">时长</span><span class="value">45分钟</span></div>
  <div class="item"><span class="label">演员</span><span class="value"><a href="/a/1">女优E</a></span></div>
  <div class="item"><span class="label">标签</span><span class="value"><a href="/t/1">剧情</a></span></div>
</div>
</body></html>
"""

XCHINA_SEARCH = """
<html><body>
<a href="/video/id-abc123.html" title="MDX-0123 测试作品">目标</a>
<a href="/about">无关</a>
</body></html>
"""


class TestXchinaParse:
    def test_detail_fields(self):
        from app.modules.ladysite.xchina import html_to_code

        info = html_to_code(XCHINA_DETAIL, "MDX-0123")
        assert info is not None
        assert info.code == "MDX-0123"
        assert info.title == "测试作品"
        assert info.release_date == "2024-03-15"
        assert info.duration == "45分钟"
        assert info.casts == "女优E"
        assert "剧情" in info.genres

    def test_search_finds_detail_url(self):
        from app.modules.ladysite.xchina import html_to_detail_url

        assert html_to_detail_url(XCHINA_SEARCH, "MDX-0123") == "/video/id-abc123.html"

    def test_search_no_match_returns_empty(self):
        from app.modules.ladysite.xchina import html_to_detail_url

        assert html_to_detail_url(XCHINA_SEARCH, "ZZZZ-999") == ""

    def test_missing_date_returns_none(self):
        from app.modules.ladysite.xchina import html_to_code

        assert html_to_code("<html><body><h1>X</h1></body></html>", "X-1") is None


# ----------------------------------------------------------------------
# Hbox
# ----------------------------------------------------------------------
HBOX_DETAIL = """
<html><head>
<meta property="og:image" content="https://hbox.jp/img/ssis001.jpg">
</head><body>
<h1>SSIS-001 テストタイトル</h1>
<dl>
  <dt>出演</dt><dd><a href="/a/1">葵つかさ</a></dd>
  <dt>発売日</dt><dd>2021/02/19</dd>
  <dt>収録時間</dt><dd>147分</dd>
  <dt>メーカー</dt><dd>エスワン</dd>
  <dt>シリーズ</dt><dd>----</dd>
  <dt>ジャンル</dt><dd><a href="/g/1">単体作品</a></dd>
</dl>
</body></html>
"""


class TestHboxParse:
    def test_detail_fields(self):
        from app.modules.ladysite.hbox import html_to_code

        info = html_to_code(HBOX_DETAIL, "SSIS-001")
        assert info is not None
        assert info.code == "SSIS-001"
        assert info.title == "テストタイトル"
        assert info.release_date == "2021-02-19"
        assert info.duration == "147分"
        assert info.producer == "エスワン"
        assert info.casts == "葵つかさ"

    def test_placeholder_dashes_are_skipped(self):
        from app.modules.ladysite.hbox import html_to_code

        assert html_to_code(HBOX_DETAIL, "SSIS-001").series == ""

    def test_missing_date_returns_none(self):
        from app.modules.ladysite.hbox import html_to_code

        assert html_to_code("<html><body><h1>X</h1></body></html>", "X-1") is None


# ----------------------------------------------------------------------
# 番号形态路由
# ----------------------------------------------------------------------
class TestCodeRouting:
    @pytest.fixture(autouse=True)
    def _synced(self):
        """路由要读库里的番号规则，库是空的就只剩内置默认值兜底。

        专用源（fc2/madou 等）不在 DETAIL_SITES 里，只有登记进库、被
        enabled_parser_sources 列出来之后才会参与路由。
        """
        from sqlalchemy import delete

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite.sources import sync_builtin_sources

        with session_scope() as session:
            session.execute(delete(DataSource))
        sync_builtin_sources()
        yield
        with session_scope() as session:
            session.execute(delete(DataSource))

    def test_all_builtin_sources_have_parsers(self):
        """内置详情源全部接入解析，不再有只能测连通性的源。

        字幕源不在此列：它们与详情源共用 datasource 表（同样要能在页面上
        改地址），但解析实现在 modules/subtitle 下，不走详情抓取那条链路。
        """
        from app.modules.ladysite.sources import DETAIL_SOURCES

        assert [s["key"] for s in DETAIL_SOURCES if not s["parser"]] == []

    def test_every_parser_is_constructible(self):
        """详情源登记的 parser 都要能在工厂里建出实例。"""
        from app.modules.ladysite import _SITE_CLASSES, _get_site
        from app.modules.ladysite.sources import DETAIL_SOURCES

        for source in DETAIL_SOURCES:
            key = source["key"]
            assert key in _SITE_CLASSES, f"{key} 未登记到 _SITE_CLASSES"
            assert _get_site(key) is not None, f"{key} 构造失败"

    def test_subtitle_sources_stay_out_of_detail_crawling(self):
        """字幕源不能被拉进详情抓取 —— 它们没有 CodeInfo 解析器。"""
        from app.modules.ladysite import DETAIL_SITES, SPECIAL_SITES
        from app.modules.ladysite.sources import SUBTITLE_SOURCES

        assert SUBTITLE_SOURCES, "字幕源清单不该为空"
        for source in SUBTITLE_SOURCES:
            assert source["key"] not in DETAIL_SITES + SPECIAL_SITES
            # parser 为空是把它挡在 enabled_parser_sources 之外的那道闸
            assert not source["parser"]

    def test_fc2_code_skips_jav_sites(self):
        """FC2 番号在日系有码源上查不到，不该白开线程。

        missav/7mmtv/xchina 这类收录面广的站仍会问 —— 它们确实收 FC2。
        """
        from app.modules.ladysite import _sites_for_code

        sites = _sites_for_code("FC2-PPV-1234567")
        assert "fc2" in sites and "fc2hub" in sites
        for key in ("javbus", "javdb", "dmm", "avbase"):
            assert key not in sites

    def test_date_code_only_asks_uncensored_sites(self):
        from app.modules.ladysite import _sites_for_code

        sites = _sites_for_code("032416_267")
        assert "carib" in sites and "avsox" in sites
        for key in ("javbus", "javdb", "dmm"):
            assert key not in sites

    def test_uncensored_brand_skips_jav_sites(self):
        """HEYZO/1PONDO 这类无码厂牌同理。"""
        from app.modules.ladysite import _sites_for_code

        for code in ("HEYZO-1234", "1PONDO-123"):
            sites = _sites_for_code(code)
            assert "avsox" in sites, code
            assert "javbus" not in sites, code

    def test_normal_code_skips_special_sites(self):
        """普通番号不该去问只收 FC2 或日期型番号的源。"""
        from app.modules.ladysite import _sites_for_code

        sites = _sites_for_code("SSIS-001")
        assert "javbus" in sites
        for key in ("fc2", "fc2hub", "carib"):
            assert key not in sites

    def test_amateur_code_adds_mgstage(self):
        from app.modules.ladysite import _sites_for_code

        assert "mgstage" in _sites_for_code("SIRO-4321")
        assert "mgstage" in _sites_for_code("259LUXU-1234")

    def test_domestic_code_adds_madou(self):
        from app.modules.ladysite import _sites_for_code

        sites = _sites_for_code("MDX-0123")
        assert "madou" in sites and "madouqu" in sites
        assert "javbus" not in sites

    def test_prefix_match_does_not_bleed(self):
        """MD 不能顺带命中 MDBK —— 否则 skip:MD 会把 MDBK 一起排掉。"""
        from app.modules.ladysite import _sites_for_code

        sites = _sites_for_code("MDBK-001")
        assert "madou" in sites
        assert "javbus" not in sites

    def test_theporndb_skipped_without_token(self):
        """没配 Token 时接口必然 401，不该占一个并发位。

        不能只靠"默认停用"挡：老库里这行是 enabled=True（旧默认），
        升级上来的安装必须由 token 门拦住。
        """
        from sqlalchemy import select

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite import _sites_for_code

        # 模拟升级库：theporndb 启用但没配 token
        with session_scope() as session:
            row = session.scalar(
                select(DataSource).where(DataSource.key == "theporndb")
            )
            row.enabled = True
            row.cookie = None

        assert "theporndb" not in _sites_for_code("SSIS-001")

    def test_theporndb_included_with_token(self):
        from sqlalchemy import select

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite import _sites_for_code

        with session_scope() as session:
            row = session.scalar(
                select(DataSource).where(DataSource.key == "theporndb")
            )
            row.enabled = True
            row.cookie = "tok123"

        assert "theporndb" in _sites_for_code("SSIS-001")

    def test_unknown_code_falls_back_to_general(self):
        """认不出的番号退回通用清单，不能一个源都不问。"""
        from app.modules.ladysite import _sites_for_code

        assert len(_sites_for_code("!!!")) > 0


# ----------------------------------------------------------------------
# 抓取顺序按数据源页面上排的优先级走
# ----------------------------------------------------------------------
class TestBaseHelpers:
    @pytest.mark.parametrize("url,host,expected", [
        ("/a.jpg", "https://x.biz", "https://x.biz/a.jpg"),
        # host 带尾斜杠或语言路径都不能拼出双斜杠
        ("/a.jpg", "https://x.biz/", "https://x.biz/a.jpg"),
        ("//cdn.x/a.jpg", "https://x.biz", "https://cdn.x/a.jpg"),
        ("https://cdn.x/a.jpg", "https://x.biz", "https://cdn.x/a.jpg"),
        ("", "https://x.biz", ""),
        # 不带前导斜杠的相对路径（subtitlecat 的搜索结果就是这种）。
        # 原样返回的话调用方拼出 x.bizsubs/1/a.html —— 域名都变了，
        # 报出来却是个看着与 URL 无关的 SSL/DNS 错误
        ("subs/1/a.html", "https://x.biz", "https://x.biz/subs/1/a.html"),
        ("subs/1/a.html", "https://x.biz/", "https://x.biz/subs/1/a.html"),
        # 其它 scheme 不能被当成相对路径拼上 host
        ("data:image/png;base64,AA", "https://x.biz", "data:image/png;base64,AA"),
        ("http://cdn.x/a.jpg", "https://x.biz", "http://cdn.x/a.jpg"),
    ])
    def test_absolute_url(self, url, host, expected):
        from app.modules.ladysite.base import absolute_url
        assert absolute_url(url, host) == expected

    @pytest.mark.parametrize("text,expected", [
        ("2021-02-19", "2021-02-19"),
        ("販売日 : 2021/2/9", "2021-02-09"),
        ("2024年3月15日", "2024-03-15"),
        ("no date", ""),
    ])
    def test_normalize_date(self, text, expected):
        from app.modules.ladysite.base import normalize_date
        assert normalize_date(text) == expected

    @pytest.mark.parametrize("text,code,expected", [
        ("MDX-0123 测试作品", "MDX-0123", True),
        ("MDX0123高清", "MDX-0123", True),
        # 画质后缀是独立词，不能因数字贴着番号而漏判
        ("MDX-0123 1080P", "MDX-0123", True),
        # 多部曲变体是别的片
        ("MD-0180-1 下集", "MD-0180", False),
        ("SSIS-0011 別的片", "SSIS-001", False),
        # 字幕版后缀算同一部
        ("SSIS-001C 中字", "SSIS-001", True),
        ("没有番号的标题", "MDX-0123", False),
    ])
    def test_text_contains_code(self, text, code, expected):
        from app.modules.ladysite.base import text_contains_code
        assert text_contains_code(text, code) is expected

    def test_cookie_pairs(self):
        from app.modules.ladysite.base import _cookie_pairs

        assert _cookie_pairs("adc=1; b=2") == [
            {"name": "adc", "value": "1"}, {"name": "b", "value": "2"},
        ]
        assert _cookie_pairs("") == []
        assert _cookie_pairs("novalue") == []

    def test_bypass_first_passes_cookie(self, monkeypatch):
        """mgstage 的年龄门 Cookie 必须跟着过盾请求走，丢了拿回的
        永远是确认页。"""
        from types import SimpleNamespace

        from app.modules.ladysite import base as base_mod
        from app.modules.ladysite.mgstage import Mgstage

        monkeypatch.setattr(
            base_mod, "get_settings",
            lambda: SimpleNamespace(bypass_url="http://solver:8191/v1", proxy=""),
        )
        seen = {}

        def fake_bypass(url, params=None, timeout=60.0, quick=False, cookie=""):
            seen["cookie"] = cookie
            return ""

        monkeypatch.setattr(base_mod, "fetch_via_bypass", fake_bypass)
        site = Mgstage(host="https://www.mgstage.com")
        site.client.get("/product/product_detail/SIRO-4321/")
        assert "adc=1" in seen["cookie"]


class TestCodeRuleParsing:
    @pytest.mark.parametrize("rule,only,skip", [
        ("only:FC2,SIRO", ["FC2", "SIRO"], []),
        ("skip:MD,MDX", [], ["MD", "MDX"]),
        ("only:FC2;skip:MD", ["FC2"], ["MD"]),
        # 没写前缀时按 only 处理，那是更常见的意图
        ("FC2,SIRO", ["FC2", "SIRO"], []),
        # 中文逗号、空格、换行都当分隔符 —— 用户手打什么都有
        ("only:FC2，SIRO GANA\nskip:MD", ["FC2", "SIRO", "GANA"], ["MD"]),
        ("", [], []),
        ("   ", [], []),
    ])
    def test_parse(self, rule, only, skip):
        from app.modules.ladysite.sources import parse_code_rule

        parsed = parse_code_rule(rule)
        assert parsed["only"] == only
        assert parsed["skip"] == skip

    def test_parse_dedupes_and_uppercases(self):
        from app.modules.ladysite.sources import parse_code_rule

        assert parse_code_rule("only:fc2,FC2,Fc2-")["only"] == ["FC2"]

    @pytest.mark.parametrize("code,rule,allowed", [
        # 空规则不限制
        ("SSIS-001", "", True),
        # only：命中才问
        ("FC2-PPV-1", "only:FC2", True),
        ("SSIS-001", "only:FC2", False),
        # skip：命中就不问
        ("SSIS-001", "skip:FC2", True),
        ("FC2-PPV-1", "skip:FC2", False),
        # 日期型
        ("032416_267", "only:date", True),
        ("SSIS-001", "only:date", False),
        ("032416_267", "skip:date", False),
        # 前缀整段匹配：MD 不命中 MDBK
        ("MDBK-001", "skip:MD", True),
        ("MD-0180", "skip:MD", False),
        ("MDBK-001", "skip:MDBK", False),
        # 两者都有时 skip 优先
        ("FC2-PPV-1", "only:FC2;skip:FC2", False),
    ])
    def test_code_allowed(self, code, rule, allowed):
        from app.modules.ladysite.sources import code_allowed

        assert code_allowed(code, rule) is allowed

    def test_bad_rule_does_not_kill_source(self):
        """写错的规则忽略掉而不是整条报废，否则一个笔误会让源静默失联。"""
        from app.modules.ladysite.sources import code_allowed

        # 只有冒号没有内容，等于没写规则
        assert code_allowed("SSIS-001", "only:") is True
        assert code_allowed("SSIS-001", ";;;") is True


class TestCodeRuleBackfill:
    def test_backfill_fills_null_only(self):
        """存量行补默认规则，但用户改过的（含清空）不能被覆盖。"""
        from sqlalchemy import delete, select

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite.sources import (
            backfill_builtin_rules, sync_builtin_sources,
        )

        with session_scope() as session:
            session.execute(delete(DataSource))
        sync_builtin_sources()

        with session_scope() as session:
            # 模拟老库：一行规则为空（还没补过），一行被用户清成空串
            javbus = session.scalar(select(DataSource).where(DataSource.key == "javbus"))
            javbus.code_rule = None
            fc2 = session.scalar(select(DataSource).where(DataSource.key == "fc2"))
            fc2.code_rule = ""

        backfill_builtin_rules()

        with session_scope() as session:
            javbus = session.scalar(select(DataSource).where(DataSource.key == "javbus"))
            fc2 = session.scalar(select(DataSource).where(DataSource.key == "fc2"))
            assert javbus.code_rule, "NULL 的行应补上默认规则"
            assert fc2.code_rule == "", "用户清空的规则不该被写回默认值"
            session.execute(delete(DataSource))


class TestSourceOrdering:
    @pytest.fixture
    def synced(self):
        """把内置源登记进库，用完清掉，避免影响别的用例。"""
        from sqlalchemy import delete

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite.sources import sync_builtin_sources

        with session_scope() as session:
            session.execute(delete(DataSource))
        sync_builtin_sources()
        yield
        with session_scope() as session:
            session.execute(delete(DataSource))

    def test_order_follows_priority(self, synced):
        """页面上把某个源排到最前，抓取就该先问它。"""
        from sqlalchemy import select

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite import _enabled_sites

        assert _enabled_sites()[0] == "javbus"

        # 把 missav 的优先级压到最小
        with session_scope() as session:
            row = session.scalar(select(DataSource).where(DataSource.key == "missav"))
            row.priority = -1

        assert _enabled_sites()[0] == "missav"

    def test_disabled_source_excluded(self, synced):
        from sqlalchemy import select

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite import _enabled_sites

        with session_scope() as session:
            row = session.scalar(select(DataSource).where(DataSource.key == "javbus"))
            row.enabled = False

        assert "javbus" not in _enabled_sites()

    def test_all_disabled_falls_back(self, synced):
        """全停时退回内置顺序，免得配置失手把抓取彻底关死。"""
        from sqlalchemy import update

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite import DETAIL_SITES, _enabled_sites

        with session_scope() as session:
            session.execute(update(DataSource).values(enabled=False))

        assert _enabled_sites() == DETAIL_SITES

    def test_fresh_seed_uses_curated_order(self, synced):
        """priority 种子按 DEFAULT_ORDER 而非 SOURCES 字母序：首批并发位
        有限，直连可用的 jav321/missav 必须排进第一梯队，不能被字母靠前、
        需过盾的 airav/avbase 挤到队列里。"""
        from app.modules.ladysite import _enabled_sites

        assert _enabled_sites()[:5] == ("javbus", "javdb", "jav321", "avbase", "missav")

    def test_main_site_pinned_survives_disable(self, synced, monkeypatch):
        """MAIN_SITE 锁定单站且该站被停用时，不能悄悄扇出到全部源 ——
        用户显式锁定过就只问那一站（旧行为），宁可没结果。"""
        from types import SimpleNamespace

        from sqlalchemy import select

        import app.modules.ladysite as ladysite_mod
        from app.database.models import DataSource
        from app.database.session import session_scope

        monkeypatch.setattr(
            ladysite_mod, "get_settings",
            lambda: SimpleNamespace(main_site="javbus"),
        )
        with session_scope() as session:
            row = session.scalar(select(DataSource).where(DataSource.key == "javbus"))
            row.enabled = False

        assert ladysite_mod._enabled_sites() == ("javbus",)
        assert ladysite_mod._sites_for_code("SSIS-001") == ("javbus",)


class TestReorderEndpoint:
    @pytest.fixture
    def synced(self):
        from sqlalchemy import delete

        from app.database.models import DataSource
        from app.database.session import session_scope
        from app.modules.ladysite.sources import sync_builtin_sources

        with session_scope() as session:
            session.execute(delete(DataSource))
        sync_builtin_sources()
        yield
        with session_scope() as session:
            session.execute(delete(DataSource))

    def _priorities(self, keys):
        from sqlalchemy import select

        from app.database.models import DataSource
        from app.database.session import session_scope

        with session_scope() as session:
            rows = session.scalars(
                select(DataSource).where(DataSource.key.in_(keys))
            ).all()
            return {row.key: row.priority for row in rows}

    def test_reorder_applies_given_order(self, synced):
        from app.api.endpoints.datasource import ReorderRequest, reorder_datasources

        keys = ["missav", "javbus", "javdb"]
        reorder_datasources(ReorderRequest(keys=keys), current_user="t")

        got = self._priorities(keys)
        assert got["missav"] < got["javbus"] < got["javdb"]

    def test_reorder_keeps_group_position(self, synced):
        """起点沿用这一组现有的最小优先级，不能把整组顶到最前面。"""
        from app.api.endpoints.datasource import ReorderRequest, reorder_datasources

        before = self._priorities(["javdb", "missav"])
        base = min(before.values())

        reorder_datasources(
            ReorderRequest(keys=["missav", "javdb"]), current_user="t"
        )
        after = self._priorities(["javdb", "missav"])
        assert min(after.values()) == base

    def test_reorder_rejects_unknown_key(self, synced):
        from app.api.endpoints.datasource import ReorderRequest, reorder_datasources

        result = reorder_datasources(
            ReorderRequest(keys=["javbus", "nosuchsource"]), current_user="t"
        )
        assert result["code"] == 404

    def test_reorder_rejects_empty(self, synced):
        from app.api.endpoints.datasource import ReorderRequest, reorder_datasources

        result = reorder_datasources(ReorderRequest(keys=["", "  "]), current_user="t")
        assert result["code"] == 400

    def test_reorder_dedupes_keys(self, synced):
        """重复 key 会让优先级互相覆盖，顺序变得不可预测。"""
        from app.api.endpoints.datasource import ReorderRequest, reorder_datasources

        reorder_datasources(
            ReorderRequest(keys=["missav", "javbus", "missav"]), current_user="t"
        )
        got = self._priorities(["missav", "javbus"])
        assert got["missav"] < got["javbus"]

    def test_reorder_route_not_shadowed(self):
        """/datasources/reorder 必须声明在 /datasources/{key} 之前，
        否则 reorder 会被当成 key 落到 update_datasource 上。"""
        from app.api.endpoints.datasource import router

        paths = [
            route.path for route in router.routes
            if "PUT" in getattr(route, "methods", set())
        ]
        assert paths.index("/datasources/reorder") < paths.index("/datasources/{key}")
