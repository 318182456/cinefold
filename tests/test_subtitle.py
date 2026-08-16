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


def test_has_subtitle_not_fooled_by_longer_code(enabled, tmp_path):
    """ABS-0011.srt 不是 ABS-001 的字幕，前缀匹配会误判。"""
    folder = tmp_path / "lib" / "ABS-001"
    folder.mkdir(parents=True)
    video = folder / "ABS-001.mp4"
    video.write_bytes(b"x")
    (folder / "ABS-0011.srt").write_text(SRT_ZH, encoding="utf-8")

    assert not service._has_subtitle(video)
