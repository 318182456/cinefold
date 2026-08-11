"""资源站解析测试。

用构造的 HTML 片段验证解析逻辑，不发真实请求。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


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
