"""字幕：简体判定、编码识别、落盘与跳过逻辑。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.database.models import MediaLink
from app.database.session import session_scope
from app.modules.subtitle.base import (
    SubtitleItem,
    decode_subtitle,
    is_simplified_chinese,
    looks_like_subtitle,
    pick_suffix,
)
from app.services import subtitle as service

SRT_ZH = """1
00:00:01,000 --> 00:00:04,000
我们说过这个问题还没有解决

2
00:00:05,000 --> 00:00:08,000
他来的时候对我说学校已经开门了

3
00:00:09,000 --> 00:00:12,000
这个国家将要实现给认识的话请让边远地区
"""

SRT_TW = """1
00:00:01,000 --> 00:00:04,000
我們說過這個問題還沒有解決

2
00:00:05,000 --> 00:00:08,000
他來的時候對我說學校已經開門了

3
00:00:09,000 --> 00:00:12,000
這個國家將要實現給認識的話請讓邊遠地區
"""

SRT_JA = """1
00:00:01,000 --> 00:00:04,000
私はあなたのことが好きです

2
00:00:05,000 --> 00:00:08,000
今日は学校に行きましたが誰もいませんでした

3
00:00:09,000 --> 00:00:12,000
彼女は本を読んでいる時間が長いですね
"""


# ----------------------------------------------------------------------
# 语言判定
# ----------------------------------------------------------------------
def test_simplified_accepts_simplified():
    assert is_simplified_chinese(SRT_ZH)


def test_simplified_rejects_traditional():
    """繁体不能当简体收：字幕站的「中文」大量是繁体。"""
    assert not is_simplified_chinese(SRT_TW)


def test_simplified_rejects_japanese():
    """日文里也有汉字，靠汉字数量区分不了，得看假名占比。"""
    assert not is_simplified_chinese(SRT_JA)


def test_simplified_rejects_english_and_short():
    assert not is_simplified_chinese("Hello world, plain english subtitle")
    # 汉字太少，判不出就该放弃
    assert not is_simplified_chinese("你好")


def test_decode_handles_gbk_and_bom():
    """站点常透传 GBK 原文件，按响应头当 UTF-8 解会出乱码。"""
    assert is_simplified_chinese(decode_subtitle(SRT_ZH.encode("gb18030")))
    assert is_simplified_chinese(decode_subtitle(b"\xef\xbb\xbf" + SRT_ZH.encode()))


def test_looks_like_subtitle_rejects_html():
    """站点找不到资源时返回 200 + 错误页，只看状态码会把它存进媒体库。"""
    assert looks_like_subtitle(SRT_ZH)
    assert looks_like_subtitle("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n嗨")
    assert not looks_like_subtitle("<!DOCTYPE html><html>404 not found</html>")
    assert not looks_like_subtitle("")


def test_pick_suffix():
    assert pick_suffix("/sub/ABS-001.ASS") == ".ass"
    assert pick_suffix("/sub/ABS-001.bin") == ".srt"


# ----------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_links():
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)

    def _clear():
        with session_scope() as session:
            for row in session.query(MediaLink).all():
                session.delete(row)
    _clear()
    yield
    _clear()


@pytest.fixture
def enabled():
    settings = get_settings()
    original = settings.subtitle_enabled
    settings.subtitle_enabled = True
    yield
    settings.subtitle_enabled = original


@pytest.fixture
def library(tmp_path):
    """造一部已登记的影片，返回它的路径。"""
    folder = tmp_path / "library" / "ABS-001"
    folder.mkdir(parents=True)
    video = folder / "ABS-001.mp4"
    video.write_bytes(b"x" * 1024)

    with session_scope() as session:
        session.add(MediaLink(
            link_path=str(video), code="ABS-001",
            source_path=str(video), inode=1, device=1,
        ))
    return video


def _stub_search(monkeypatch, item):
    import app.modules.subtitle as module
    monkeypatch.setattr(module, "search", lambda code: item)


def test_fetch_writes_beside_video(monkeypatch, enabled, library):
    _stub_search(monkeypatch, SubtitleItem(
        code="ABS-001", site="test", content=SRT_ZH, suffix=".srt"
    ))

    assert service.fetch_for_code("ABS-001") == 1
    written = library.parent / "ABS-001.zh-CN.srt"
    assert written.is_file()
    assert "我们说过" in written.read_text(encoding="utf-8")


def test_fetch_skips_when_subtitle_exists(monkeypatch, enabled, library):
    """已有字幕就不该再抓 —— 包括用户手工放的那份。"""
    (library.parent / "ABS-001.chs.srt").write_text(SRT_ZH, encoding="utf-8")

    called = []
    import app.modules.subtitle as module
    monkeypatch.setattr(
        module, "search", lambda code: called.append(code) or None
    )

    assert service.fetch_for_code("ABS-001") == 0
    # 连请求都不该发出去
    assert called == []


def test_force_overwrites_existing(monkeypatch, enabled, library):
    (library.parent / "ABS-001.zh-CN.srt").write_text("old", encoding="utf-8")
    _stub_search(monkeypatch, SubtitleItem(
        code="ABS-001", site="test", content=SRT_ZH, suffix=".srt"
    ))

    assert service.fetch_for_code("ABS-001", force=True) == 1
    assert "我们说过" in (
        library.parent / "ABS-001.zh-CN.srt"
    ).read_text(encoding="utf-8")


def test_disabled_switch_blocks_auto_but_not_manual(monkeypatch, library):
    """开关管的是自动行为；人点了按钮就是明确要抓。"""
    settings = get_settings()
    original = settings.subtitle_enabled
    settings.subtitle_enabled = False
    try:
        _stub_search(monkeypatch, SubtitleItem(
            code="ABS-001", site="test", content=SRT_ZH, suffix=".srt"
        ))
        assert service.fetch_for_code("ABS-001") == 0
        assert service.fetch_for_code("ABS-001", manual=True) == 1
    finally:
        settings.subtitle_enabled = original


def test_no_subtitle_found_writes_nothing(monkeypatch, enabled, library):
    """抓不到就什么都不放 —— 空文件会让播放器显示一条空字幕轨。"""
    _stub_search(monkeypatch, None)

    assert service.fetch_for_code("ABS-001") == 0
    assert not list(library.parent.glob("*.srt"))
    # 临时文件也不能留下
    assert not list(library.parent.glob(".cinefold-sub-*"))


def test_writes_to_every_hardlink(monkeypatch, enabled, tmp_path):
    """同一部片有多个入口时，每个位置都要放，否则换个入口播就没字幕。"""
    paths = []
    for name in ("分类A", "分类B"):
        folder = tmp_path / name / "ABS-002"
        folder.mkdir(parents=True)
        video = folder / "ABS-002.mp4"
        video.write_bytes(b"x" * 1024)
        paths.append(video)
        with session_scope() as session:
            session.add(MediaLink(
                link_path=str(video), code="ABS-002",
                source_path=str(video), inode=2, device=1,
            ))

    _stub_search(monkeypatch, SubtitleItem(
        code="ABS-002", site="test", content=SRT_ZH, suffix=".srt"
    ))

    assert service.fetch_for_code("ABS-002") == 2
    for video in paths:
        assert (video.parent / "ABS-002.zh-CN.srt").is_file()


def test_scrape_register_triggers_fetch(monkeypatch, enabled, tmp_path):
    """刮削登记完成后自动抓 —— 三条触发路径里最主要的那条。"""
    from app.services import medialink

    source_dir = tmp_path / "downloads"
    library = tmp_path / "library" / "ABS-003"
    source_dir.mkdir(parents=True)
    library.mkdir(parents=True)
    source = source_dir / "abs-003.mp4"
    source.write_bytes(b"x" * 1024)
    link = library / "ABS-003.mp4"
    os.link(source, link)

    settings = get_settings()
    original = settings.medialink_library_path
    settings.medialink_library_path = str(tmp_path / "library")
    try:
        _stub_search(monkeypatch, SubtitleItem(
            code="ABS-003", site="test", content=SRT_ZH, suffix=".srt"
        ))
        medialink.register_scrape("ABS-003", str(source))
        assert (library / "ABS-003.zh-CN.srt").is_file()
    finally:
        settings.medialink_library_path = original


def test_subtitle_failure_does_not_break_register(monkeypatch, enabled, tmp_path):
    """抓字幕是附赠，它炸了不能带倒刮削登记这件正事。"""
    from app.services import medialink

    source_dir = tmp_path / "downloads"
    library = tmp_path / "library" / "ABS-004"
    source_dir.mkdir(parents=True)
    library.mkdir(parents=True)
    source = source_dir / "abs-004.mp4"
    source.write_bytes(b"x" * 1024)
    link = library / "ABS-004.mp4"
    os.link(source, link)

    def _boom(code):
        raise RuntimeError("字幕站炸了")

    import app.modules.subtitle as module
    monkeypatch.setattr(module, "search", _boom)

    settings = get_settings()
    original = settings.medialink_library_path
    settings.medialink_library_path = str(tmp_path / "library")
    try:
        links = medialink.register_scrape("ABS-004", str(source))
        assert str(link) in links
    finally:
        settings.medialink_library_path = original


def test_endpoint_returns_written_count(monkeypatch, library):
    """端点契约：成功回 data.written，找不到回 404 —— 前端按这两个分支写的。"""
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.api.endpoints import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: "tester"
    client = TestClient(app)

    _stub_search(monkeypatch, SubtitleItem(
        code="ABS-001", site="test", content=SRT_ZH, suffix=".srt"
    ))
    payload = client.post(
        "/api/v1/medialinks/subtitle", json={"code": "ABS-001"}
    ).json()
    assert payload["code"] == 200, payload
    assert payload["data"]["written"] == 1

    _stub_search(monkeypatch, None)
    payload = client.post(
        "/api/v1/medialinks/subtitle", json={"code": "ABS-001", "force": True}
    ).json()
    assert payload["code"] == 404, payload


# ----------------------------------------------------------------------
# subtitlecat 解析
# ----------------------------------------------------------------------
# 详情页的真实结构：语言标在文件名上，链接文本一律是 Download
_DETAIL_HTML = """
<html><body>
<a href="/subs/1341/SONE-895-zh-CN.srt">Download</a>
<a href="/subs/1340/SONE-895-zh-TW.srt">Download</a>
<a href="/subs/1348/SONE-895-en.srt">Download</a>
<a href="/subs/1353/SONE-895-ja.srt">Download</a>
<a href="/subs/1350/SONE-895-th.srt">Download</a>
</body></html>
"""


def test_candidate_links_ranks_simplified_then_rest_then_traditional():
    """语言标在 URL 上，只看链接文本的话十几种语言全都无从区分。

    繁中不再排除 —— 它能转成简体，是可用候选，只排在最后。
    """
    from app.modules.subtitle.subtitlecat import SubtitleCat

    links = SubtitleCat(host="https://sc.test")._candidate_links(_DETAIL_HTML)

    # 简中排第一
    assert links[0].endswith("SONE-895-zh-CN.srt")
    # 繁中仍在候选里，但垫底：有简体可用时不该动它
    assert any("zh-TW" in u for u in links)
    assert links[-1].endswith("SONE-895-zh-TW.srt")
    # 其余语言仍留作兜底（正文判定会把它们挡下）
    assert any(u.endswith("-en.srt") for u in links)
    # 全部补成绝对地址
    assert all(u.startswith("https://sc.test/") for u in links)


def test_candidate_links_drops_cantonese():
    """粤语转不成通用简体，仍旧排除。"""
    from app.modules.subtitle.subtitlecat import SubtitleCat

    html = """
    <html><body>
    <a href="/subs/1/SONE-895-zh-CN.srt">Download</a>
    <a href="/subs/2/SONE-895-cantonese.srt">Download</a>
    </body></html>
    """
    links = SubtitleCat(host="https://sc.test")._candidate_links(html)
    assert not any("cantonese" in u for u in links)


def test_search_result_relative_href_becomes_absolute(monkeypatch):
    """搜索结果的 href 不带前导斜杠，拼错会变成 sc.testsubs/... 这种死域名。"""
    from app.modules.subtitle.subtitlecat import SubtitleCat

    site = SubtitleCat(host="https://sc.test")
    monkeypatch.setattr(site.client, "get", lambda *a, **k: """
        <table><tbody><tr><td>
          <a href="subs/1340/SONE-895.html">SONE-895</a>
        </td></tr></tbody></table>
    """)

    path, title = site._find_detail("SONE-895")
    assert path == "https://sc.test/subs/1340/SONE-895.html"
    assert title == "SONE-895"


def test_search_result_ignores_other_codes(monkeypatch):
    """搜索是模糊匹配，SSIS-001 会带回 SSIS-0011 那部片。"""
    from app.modules.subtitle.subtitlecat import SubtitleCat

    site = SubtitleCat(host="https://sc.test")
    monkeypatch.setattr(site.client, "get", lambda *a, **k: """
        <table><tbody>
          <tr><td><a href="subs/1/SSIS-0011.html">SSIS-0011</a></td></tr>
        </tbody></table>
    """)

    assert site._find_detail("SSIS-001") == ("", "")


def test_list_reports_subtitle_state(monkeypatch, tmp_path):
    """列表要带 has_subtitle —— 页面靠它标「有字幕」，且要能看出部分命中。"""
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.api.endpoints import get_current_user
    from app.api.endpoints import medialink as endpoint

    # 两个位置，只有一处放了字幕
    paths = []
    for name, with_sub in (("分类A", True), ("分类B", False)):
        folder = tmp_path / name / "ABS-005"
        folder.mkdir(parents=True)
        video = folder / "ABS-005.mp4"
        video.write_bytes(b"x" * 1024)
        if with_sub:
            (folder / "ABS-005.zh-CN.srt").write_text(SRT_ZH, encoding="utf-8")
        paths.append(str(video))
        with session_scope() as session:
            session.add(MediaLink(
                link_path=str(video), code="ABS-005",
                source_path=str(video), inode=5, device=1,
            ))

    # 探测结果带 TTL 缓存，用例之间会互相污染
    endpoint._subtitle_cache.clear()
    endpoint._exists_cache.clear()

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: "tester"
    client = TestClient(app)

    payload = client.get("/api/v1/medialinks", params={"keyword": "ABS-005"}).json()
    assert payload["code"] == 200, payload
    state = {i["link_path"]: i["has_subtitle"] for i in payload["data"]["items"]}
    assert state == {paths[0]: True, paths[1]: False}


def test_has_subtitle_not_fooled_by_longer_code(enabled, tmp_path):
    """ABS-0011.srt 不是 ABS-001 的字幕，前缀匹配会误判。"""
    folder = tmp_path / "lib" / "ABS-001"
    folder.mkdir(parents=True)
    video = folder / "ABS-001.mp4"
    video.write_bytes(b"x")
    (folder / "ABS-0011.srt").write_text(SRT_ZH, encoding="utf-8")

    assert not service._has_subtitle(video)

# ----------------------------------------------------------------------
# 补漏候选筛选与回写
# ----------------------------------------------------------------------
def _add_link(video: Path, code: str, has_subtitle=None):
    """登记一条硬链接，has_subtitle 显式给三态之一。"""
    with session_scope() as session:
        session.add(MediaLink(
            link_path=str(video), code=code, source_path=str(video),
            inode=None, device=None, has_subtitle=has_subtitle,
        ))


def _make_video(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{name}.mp4"
    video.write_bytes(b"x" * 1024)
    return video


def test_lacking_skips_rows_marked_subtitled(tmp_path):
    """has_subtitle=True 的行不该再进候选 —— 这是 SQL 侧筛掉的那批。"""
    done = _make_video(tmp_path / "a", "AAA-001")
    todo = _make_video(tmp_path / "b", "BBB-002")
    _add_link(done, "AAA-001", has_subtitle=True)
    _add_link(todo, "BBB-002", has_subtitle=False)

    codes = service._codes_lacking_subtitle(10)
    assert codes == ["BBB-002"]


def test_lacking_probes_null_rows(tmp_path):
    """列为 NULL（旧库升上来）必须实地探，不能当成已有字幕跳过。"""
    bare = _make_video(tmp_path / "a", "AAA-001")
    withsub = _make_video(tmp_path / "b", "BBB-002")
    (withsub.parent / "BBB-002.zh-CN.srt").write_text(SRT_ZH, encoding="utf-8")
    _add_link(bare, "AAA-001", has_subtitle=None)
    _add_link(withsub, "BBB-002", has_subtitle=None)

    codes = service._codes_lacking_subtitle(10)
    # 磁盘上真有字幕的那个要被复核掉，只剩没字幕的
    assert codes == ["AAA-001"]


def test_lacking_rechecks_false_rows_against_disk(tmp_path):
    """列说 False 但用户手工放了字幕，要以磁盘为准。"""
    video = _make_video(tmp_path / "a", "AAA-001")
    (video.parent / "AAA-001.chs.srt").write_text(SRT_ZH, encoding="utf-8")
    _add_link(video, "AAA-001", has_subtitle=False)

    assert service._codes_lacking_subtitle(10) == []


def test_lacking_respects_limit(tmp_path):
    for i in range(5):
        v = _make_video(tmp_path / f"d{i}", f"AAA-{i:03d}")
        _add_link(v, f"AAA-{i:03d}", has_subtitle=False)

    assert len(service._codes_lacking_subtitle(2)) == 2


def test_fetch_writes_back_has_subtitle(monkeypatch, enabled, library):
    """抓完要把列标成 True，否则下一轮补漏还会把它算进候选。"""
    _stub_search(monkeypatch, SubtitleItem(
        code="ABS-001", site="test", content=SRT_ZH, suffix=".srt"
    ))
    assert service.fetch_for_code("ABS-001") == 1

    with session_scope() as session:
        row = session.get(MediaLink, str(library))
        assert row.has_subtitle is True


def test_dirprobe_lists_each_directory_once(tmp_path, monkeypatch):
    """同一目录下多部片只该列一次目录 —— 这是扫全库的主要开销。"""
    folder = tmp_path / "mixed"
    videos = [_make_video(folder, f"AAA-{i:03d}") for i in range(4)]

    calls = []
    real = service._subtitle_names

    def counting(parent):
        calls.append(parent)
        return real(parent)

    monkeypatch.setattr(service, "_subtitle_names", counting)

    probe = service._DirProbe()
    for v in videos:
        probe.has_subtitle(v)

    assert len(calls) == 1


def test_matches_stem_rejects_longer_code():
    """ABS-0011.srt 不是 ABS-001 的字幕。"""
    assert service._matches_stem("abs-001.srt", "abs-001")
    assert service._matches_stem("abs-001.zh-cn.srt", "abs-001")
    assert service._matches_stem("abs-001-chs.srt", "abs-001")
    assert not service._matches_stem("abs-0011.srt", "abs-001")


def test_simplified_accepts_beyond_original_glyphs():
    """扩表后，避开旧字表的正经简体字幕也该认出来。"""
    zh = """1
00:00:01,000 --> 00:00:04,000
她的脸颊有点热，紧张得不敢抬头

2
00:00:05,000 --> 00:00:08,000
爱人在剧场里等着，电灯忽然灭了

3
00:00:09,000 --> 00:00:12,000
妈妈买了双鞋，钱不够只能先欠着
"""
    tw = """1
00:00:01,000 --> 00:00:04,000
她的臉頰有點熱，緊張得不敢抬頭

2
00:00:05,000 --> 00:00:08,000
愛人在劇場裡等著，電燈忽然滅了

3
00:00:09,000 --> 00:00:12,000
媽媽買了雙鞋，錢不夠只能先欠著
"""
    assert is_simplified_chinese(zh)
    assert not is_simplified_chinese(tw)


# ----------------------------------------------------------------------
# 繁转简
# ----------------------------------------------------------------------
def test_to_simplified_converts_and_leaves_simplified_alone():
    from app.modules.subtitle.t2s import has_traditional, to_simplified

    assert to_simplified("臉頰劇場錢") == "脸颊剧场钱"
    # 已经是简体的原样返回
    assert to_simplified("脸颊剧场钱") == "脸颊剧场钱"
    # 非汉字不受影响
    assert to_simplified("OK 123 あい") == "OK 123 あい"
    assert to_simplified("") == ""
    assert has_traditional("臉頰")
    assert not has_traditional("脸颊")


def test_t2s_table_is_aligned():
    """字表逐位对应是转换的前提，错位会让结果整体乱掉。"""
    from app.modules.subtitle import t2s

    assert len(t2s._TRAD_CHARS) == len(t2s._SIMP_CHARS)
    assert len(t2s._TRAD_CHARS) > 2000


def test_as_simplified_accepts_traditional_by_converting():
    """繁体以前一律丢弃，现在转成简体收下 —— 这是命中率的主要来源。"""
    from app.modules.subtitle.base import as_simplified_chinese

    out = as_simplified_chinese(SRT_TW)
    assert out
    assert "我们说过" in out
    # 时间轴不能被动到
    assert "00:00:01,000 --> 00:00:04,000" in out


def test_as_simplified_passes_simplified_through_unchanged():
    from app.modules.subtitle.base import as_simplified_chinese

    assert as_simplified_chinese(SRT_ZH) == SRT_ZH


def test_as_simplified_still_rejects_japanese_and_english():
    """转换只解决字形差异，看不懂的语言仍旧不收。"""
    from app.modules.subtitle.base import as_simplified_chinese

    assert as_simplified_chinese(SRT_JA) == ""
    assert as_simplified_chinese("Hello world, plain english subtitle") == ""
    assert as_simplified_chinese("") == ""
