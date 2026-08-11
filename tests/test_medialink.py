"""媒体联动：硬链接反查、刮削登记、删除联动、webhook 端点。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.database.models import History, MediaLink
from app.database.session import session_scope
from app.services import medialink


@pytest.fixture
def configure():
    """直接改写已加载的 Settings。

    不能用 monkeypatch.setenv：load_settings() 会 load_dotenv(override=True)，
    .env.example 里的空值会把环境变量覆盖掉。
    """
    settings = get_settings()
    original = {
        key: getattr(settings, key)
        for key in (
            "medialink_library_path",
            "medialink_delete_enabled",
            "medialink_webhook_token",
        )
    }

    def _apply(**kwargs):
        for key, value in kwargs.items():
            setattr(settings, key, value)

    yield _apply
    for key, value in original.items():
        setattr(settings, key, value)


@pytest.fixture
def linked(tmp_path, configure):
    """造一份源文件 + 媒体库硬链接，并把库目录指向 tmp。"""
    source_dir = tmp_path / "downloads"
    library = tmp_path / "library" / "ABS-001"
    source_dir.mkdir(parents=True)
    library.mkdir(parents=True)

    source = source_dir / "abs-001-C.mp4"
    source.write_bytes(b"x" * 1024)
    link = library / "ABS-001.mp4"
    os.link(source, link)

    configure(
        medialink_library_path=str(tmp_path / "library"),
        medialink_delete_enabled=True,
        medialink_webhook_token="",
    )
    return source, link


@pytest.fixture(autouse=True)
def clean_tables():
    """建表并在每个用例前后清掉关联表与历史，避免相互污染。"""
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)

    def _clear():
        with session_scope() as session:
            for row in session.query(MediaLink).all():
                session.delete(row)
            for row in session.query(History).filter(
                History.code.like("ABS-%")
            ).all():
                session.delete(row)
    _clear()
    yield
    _clear()


# ----------------------------------------------------------------------
def test_find_hardlinks_matches_by_inode(linked):
    source, link = linked
    found = medialink.find_hardlinks(str(source))
    assert str(link) in found


def test_find_hardlinks_skips_when_no_link(tmp_path, configure):
    """没有硬链接的文件（st_nlink=1）不该触发扫库。"""
    library = tmp_path / "library"
    library.mkdir()
    lone = tmp_path / "lone.mp4"
    lone.write_bytes(b"z")
    configure(medialink_library_path=str(library))

    assert medialink.find_hardlinks(str(lone)) == []


def test_find_hardlinks_without_library_config(tmp_path, configure):
    configure(medialink_library_path="")
    assert medialink.find_hardlinks(str(tmp_path / "any.mp4")) == []


def test_register_scrape_persists(linked):
    source, link = linked
    links = medialink.register_scrape("ABS-001", str(source))

    assert links == [str(link)]
    with session_scope() as session:
        row = session.get(MediaLink, str(link))
        assert row is not None
        assert row.code == "ABS-001"
        assert row.source_path == str(source)
        assert row.inode


def test_register_scrape_rejects_mismatched_link_path(linked, tmp_path):
    """webhook 给的 link_path 若不是同一份数据，必须丢弃而不是照存。"""
    source, link = linked
    fake = tmp_path / "library" / "ABS-001" / "wrong.mp4"
    fake.write_bytes(b"different content")

    links = medialink.register_scrape("ABS-001", str(source), str(fake))

    assert str(fake) not in links
    assert str(link) in links


def test_register_scrape_requires_code_and_path():
    assert medialink.register_scrape("", "/some/path.mp4") == []
    assert medialink.register_scrape("ABS-001", "") == []


# ----------------------------------------------------------------------
def test_delete_collects_all_torrents_for_transcoded(linked):
    """转种：同一文件多个种子，必须全部反查出来。"""
    source, link = linked
    medialink.register_scrape("ABS-001", str(source))
    with session_scope() as session:
        session.add(History(hash="a" * 40, code="ABS-001", save_path=str(source)))
        session.add(History(hash="b" * 40, code="ABS-001", save_path=str(source)))

    result = medialink.handle_media_deleted(link_path=str(link), dry_run=True)

    assert sorted(result.torrents_deleted) == ["a" * 40, "b" * 40]
    assert result.dry_run is True
    # 演练不能动文件
    assert source.exists() and link.exists()


def test_delete_removes_files_and_records(linked):
    source, link = linked
    medialink.register_scrape("ABS-001", str(source))

    result = medialink.handle_media_deleted(link_path=str(link))

    assert not source.exists()
    assert not link.exists()
    assert str(source) in result.files_deleted
    assert str(link) in result.files_deleted
    with session_scope() as session:
        assert session.get(MediaLink, str(link)) is None


def test_delete_clears_history_so_resubscribe_works(linked, monkeypatch):
    """删除后必须清 history，否则订阅任务会以为已下载而跳过。"""
    source, link = linked
    medialink.register_scrape("ABS-001", str(source))
    with session_scope() as session:
        session.add(History(hash="c" * 40, code="ABS-001", save_path=str(source)))

    class FakeClient:
        def delete_torrent(self, hashes, delete_files=False):
            return list(hashes)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": FakeClient())

    result = medialink.handle_media_deleted(link_path=str(link))

    assert "c" * 40 in result.torrents_deleted
    with session_scope() as session:
        assert session.get(History, "c" * 40) is None


def test_delete_without_record_does_nothing(linked, tmp_path):
    """找不到关联记录时绝不能删任何东西。"""
    source, link = linked

    result = medialink.handle_media_deleted(link_path=str(tmp_path / "unknown.mp4"))

    assert result.files_deleted == []
    assert result.errors
    assert source.exists() and link.exists()


def test_delete_disabled_falls_back_to_dry_run(linked, configure):
    """开关关闭时只记录不删。"""
    source, link = linked
    medialink.register_scrape("ABS-001", str(source))
    configure(medialink_delete_enabled=False)

    result = medialink.handle_media_deleted(link_path=str(link))

    assert result.dry_run is True
    assert source.exists() and link.exists()


def test_delete_matches_by_filename_when_path_differs(linked):
    """Emby 报的路径分隔符可能与登记时不同，需按文件名兜底匹配。"""
    source, link = linked
    medialink.register_scrape("ABS-001", str(source))

    odd_path = str(link).replace("\\", "/") + ""
    # 构造一个精确匹配不到但文件名相同的路径
    result = medialink.handle_media_deleted(
        link_path="/mnt/media/ABS-001/" + Path(link).name, dry_run=True
    )

    assert result.code == "ABS-001"
    assert str(link) in result.links_deleted
    assert odd_path  # 保留变量说明意图


# ----------------------------------------------------------------------
def test_scrape_webhook_repairs_unescaped_backslashes(linked, configure):
    """MDCng 模板输出的 Windows 路径常带未转义反斜杠，JSON 非法但要能救回来。"""
    from fastapi.testclient import TestClient
    from app.api import create_app

    source, link = linked
    configure(medialink_webhook_token="s3cret")

    body = (
        '{"event":"finished","number":"ABS-001","source_path":"'
        + str(source)
        + '"}'
    )
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/webhook/scrape",
        content=body.encode(),
        headers={"Content-Type": "application/json", "X-Cinefold-Token": "s3cret"},
    )

    payload = response.json()
    assert payload["code"] == 200, payload
    assert str(link) in payload["data"]["links"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 目录名以 t/b/n/r/f 开头时，\t \b \n 等不能被当成控制字符转义
        (r'{"p":"D:\test\backup\new\report\file.mp4"}',
         "D:\\test\\backup\\new\\report\\file.mp4"),
        (r'{"p":"E:\media\JAV\ABS-001.mp4"}', "E:\\media\\JAV\\ABS-001.mp4"),
        # 已正确转义的输入不应被二次破坏
        ('{"p":"D:\\\\ok\\\\path.mp4"}', "D:\\ok\\path.mp4"),
        # 正常的 POSIX 路径
        ('{"p":"/mnt/media/ABS-001.mp4"}', "/mnt/media/ABS-001.mp4"),
    ],
)
def test_parse_body_repairs_windows_paths(raw, expected):
    """未转义反斜杠的修复不能把路径里的 \\t \\b 误当控制字符。"""
    import asyncio

    from app.api.endpoints.webhook import _parse_body

    class FakeRequest:
        async def body(self):
            return raw.encode()

    parsed = asyncio.get_event_loop().run_until_complete(_parse_body(FakeRequest()))
    assert parsed.get("p") == expected


@pytest.mark.parametrize("via", ["header", "query"])
def test_webhook_accepts_token_via_header_or_query(linked, configure, via):
    """密钥可以走请求头，也可以走 query —— 有些工具不支持自定义头。"""
    from fastapi.testclient import TestClient
    from app.api import create_app

    source, link = linked
    configure(medialink_webhook_token="s3cret")
    medialink.register_scrape("ABS-001", str(source))

    client = TestClient(create_app())
    url = "/api/v1/webhook/emby?dry_run=1"
    headers = {}
    if via == "header":
        headers["X-Cinefold-Token"] = "s3cret"
    else:
        url += "&token=s3cret"

    response = client.post(
        url,
        json={"Event": "item.remove", "Item": {"Path": str(link)}},
        headers=headers,
    )

    assert response.json()["code"] == 200, response.json()


def test_webhook_rejects_bad_token(configure):
    from fastapi.testclient import TestClient
    from app.api import create_app

    configure(medialink_webhook_token="s3cret")

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/webhook/emby",
        json={"Event": "item.remove", "Item": {"Path": "/x/y.mp4"}},
        headers={"X-Cinefold-Token": "wrong"},
    )

    assert response.json()["code"] == 403


def test_emby_webhook_ignores_non_delete_events(linked):
    from fastapi.testclient import TestClient
    from app.api import create_app

    source, link = linked
    medialink.register_scrape("ABS-001", str(source))

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/webhook/emby",
        json={"Event": "library.new", "Item": {"Path": str(link)}},
    )

    assert response.json()["data"]["ignored"] is True
    assert source.exists()


def test_emby_webhook_deletes_on_item_remove(linked):
    from fastapi.testclient import TestClient
    from app.api import create_app

    source, link = linked
    medialink.register_scrape("ABS-001", str(source))

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/webhook/emby",
        json={"Event": "item.remove", "Item": {"Path": str(link)}},
    )

    assert response.json()["code"] == 200
    assert not source.exists()
    assert not link.exists()
