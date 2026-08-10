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


class TestBrandsParse:
    def test_date_page_dedupes_and_normalizes(self):
        from app.modules.ladysite.brands import Brands

        codes = Brands("s1").crawling_date(BRAND_DATE)
        assert codes == ["SSIS-001", "SSIS-002"]

    def test_unknown_brand_falls_back(self):
        from app.modules.ladysite.brands import Brands

        assert Brands("不存在的厂牌").client.host.startswith("https://")


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
