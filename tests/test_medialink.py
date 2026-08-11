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
            "medialink_scrape_dir",
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
# 刮削附属文件与空目录清理
# ----------------------------------------------------------------------
def test_delete_removes_sidecars_and_empty_dirs(linked):
    """nfo / 海报 / 字幕 / extrafanart 跟着影片一起删，番号目录随后清掉。"""
    source, link = linked
    folder = link.parent

    nfo = folder / "ABS-001.nfo"
    poster = folder / "ABS-001-poster.jpg"
    subtitle = folder / "ABS-001.zh.srt"
    dir_poster = folder / "poster.jpg"
    extrafanart = folder / "extrafanart"
    for f in (nfo, poster, subtitle, dir_poster):
        f.write_bytes(b"meta")
    extrafanart.mkdir()
    (extrafanart / "fanart1.jpg").write_bytes(b"img")

    medialink.register_scrape("ABS-001", str(source))
    result = medialink.handle_media_deleted(link_path=str(link))

    for f in (nfo, poster, subtitle, dir_poster):
        assert not f.exists(), f
    assert not extrafanart.exists()
    # 番号目录空了，应被清掉；媒体库根目录必须保留
    assert not folder.exists()
    assert folder.parent.exists()
    assert str(folder) in result.dirs_deleted
    assert not result.errors


def test_delete_prunes_nested_empty_dirs_up_to_library_root(linked, configure):
    """演员/厂牌等中间层空了要继续往上删，但根目录永远保留。"""
    source, _link = linked
    library = Path(get_settings().medialink_library_path)
    nested = library / "女优A" / "厂牌B" / "ABS-002"
    nested.mkdir(parents=True)

    link2 = nested / "ABS-002.mp4"
    os.link(source, link2)
    (nested / "ABS-002.nfo").write_bytes(b"meta")

    medialink.register_scrape("ABS-002", str(source), str(link2))
    medialink.handle_media_deleted(link_path=str(link2))

    assert not nested.exists()
    assert not (library / "女优A" / "厂牌B").exists()
    assert not (library / "女优A").exists()
    assert library.exists(), "媒体库根目录不能被删掉"


def test_delete_keeps_shared_dir_with_other_video(linked):
    """同目录还有别的影片时，目录级 poster.jpg 和目录本身都要留着。"""
    source, link = linked
    folder = link.parent

    other = folder / "ABS-999.mp4"
    other.write_bytes(b"another movie")
    dir_poster = folder / "poster.jpg"
    dir_poster.write_bytes(b"shared")
    own_nfo = folder / "ABS-001.nfo"
    own_nfo.write_bytes(b"meta")

    medialink.register_scrape("ABS-001", str(source))
    medialink.handle_media_deleted(link_path=str(link))

    assert not own_nfo.exists(), "自己的 nfo 该删"
    assert dir_poster.exists(), "共用的目录级封面不能删"
    assert other.exists()
    assert folder.exists()


def test_delete_never_touches_paths_outside_library(linked, configure, tmp_path):
    """硬链接落在媒体库之外时，不清附属也不删目录。"""
    source, _link = linked
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = outside / "ABS-003.mp4"
    os.link(source, stray)
    stray_nfo = outside / "ABS-003.nfo"
    stray_nfo.write_bytes(b"meta")

    medialink.register_scrape("ABS-003", str(source), str(stray))
    medialink.handle_media_deleted(link_path=str(stray))

    assert not stray.exists(), "影片本身仍然要删"
    assert stray_nfo.exists(), "库外的附属文件不能碰"
    assert outside.exists()


def test_dry_run_previews_sidecars_without_deleting(linked):
    """演练要报出附属文件和会空掉的目录，但一个都不能真删。"""
    source, link = linked
    folder = link.parent
    nfo = folder / "ABS-001.nfo"
    nfo.write_bytes(b"meta")

    medialink.register_scrape("ABS-001", str(source))
    result = medialink.handle_media_deleted(link_path=str(link), dry_run=True)

    assert str(nfo) in result.sidecars_deleted
    assert str(folder) in result.dirs_deleted
    assert nfo.exists() and link.exists() and folder.exists()


def test_sidecar_prefix_match_does_not_hit_other_codes(linked):
    """前缀匹配不能误伤番号相近的另一部片子。"""
    source, link = linked
    folder = link.parent
    # ABS-001 与 ABS-0011 前缀相同，后者的 nfo 不能被带走
    neighbour = folder / "ABS-0011.mp4"
    neighbour.write_bytes(b"other movie")
    neighbour_nfo = folder / "ABS-0011.nfo"
    neighbour_nfo.write_bytes(b"meta")

    medialink.register_scrape("ABS-001", str(source))
    medialink.handle_media_deleted(link_path=str(link))

    assert neighbour.exists()
    assert neighbour_nfo.exists(), "ABS-0011 的 nfo 不该被 ABS-001 带走"


# ----------------------------------------------------------------------
# 种子关联删除：文件清单由下载器给出
# ----------------------------------------------------------------------
@pytest.fixture
def fake_client(monkeypatch):
    """注入一个假下载器，可指定它汇报的种子文件清单。"""
    import app.modules.downloadclient as dc

    def _install(files: list[str]):
        class FakeClient:
            def delete_torrent(self, hashes, delete_files=False):
                return list(hashes)

            def list_torrent_files(self, hashes):
                return list(files)

        monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
        monkeypatch.setattr(dc, "get_download_client", lambda name="": FakeClient())

    return _install


def test_delete_removes_all_files_reported_by_torrent(linked, fake_client, tmp_path):
    """种子里的样品图、说明 txt 等一并删掉，不只删影片。"""
    source, link = linked
    task_dir = source.parent / "[厂牌]ABS-001[1080p]"
    task_dir.mkdir()
    movie = task_dir / "ABS-001.mp4"
    movie.write_bytes(b"movie")
    sample = task_dir / "sample.jpg"
    sample.write_bytes(b"img")
    readme = task_dir / "说明.txt"
    readme.write_bytes(b"text")

    fake_client([str(movie), str(sample), str(readme)])
    with session_scope() as session:
        session.add(History(hash="d" * 40, code="ABS-001", save_path=str(movie)))
    medialink.register_scrape("ABS-001", str(source))

    result = medialink.handle_media_deleted(link_path=str(link))

    for f in (movie, sample, readme):
        assert not f.exists(), f
    assert str(sample) in result.files_deleted
    # 任务目录空了要清掉，但下载根目录必须留着
    assert not task_dir.exists()
    assert source.parent.exists()


def test_torrent_files_queried_before_deletion(linked, monkeypatch, tmp_path):
    """文件清单必须赶在删种前取 —— 种子删了就查不到了。"""
    source, link = linked
    extra = source.parent / "extra.jpg"
    extra.write_bytes(b"img")

    calls: list[str] = []

    class OrderTrackingClient:
        def list_torrent_files(self, hashes):
            calls.append("list")
            return [str(extra)]

        def delete_torrent(self, hashes, delete_files=False):
            calls.append("delete")
            return list(hashes)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": OrderTrackingClient())

    with session_scope() as session:
        session.add(History(hash="e" * 40, code="ABS-001", save_path=str(source)))
    medialink.register_scrape("ABS-001", str(source))

    medialink.handle_media_deleted(link_path=str(link))

    assert calls == ["list", "delete"], f"取清单必须在删种之前: {calls}"
    assert not extra.exists()


def test_single_file_torrent_never_deletes_download_root(linked, fake_client):
    """单文件种子直接躺在下载根目录时，绝不能把下载根删掉。"""
    source, link = linked
    download_root = source.parent

    fake_client([str(source)])
    with session_scope() as session:
        session.add(History(hash="f" * 40, code="ABS-001", save_path=str(source)))
    medialink.register_scrape("ABS-001", str(source))

    medialink.handle_media_deleted(link_path=str(link))

    assert not source.exists()
    assert download_root.exists(), "下载根目录不能被删掉"


def test_nested_torrent_dirs_cleaned_but_not_above_task_dir(linked, fake_client):
    """种子内的多层子目录要清干净，但不能越过任务目录往上删。"""
    source, link = linked
    downloads = source.parent
    task = downloads / "ABS-001.1080p"
    subs = task / "subs"
    imgs = task / "images"
    subs.mkdir(parents=True)
    imgs.mkdir()

    movie = task / "ABS-001.mp4"
    movie.write_bytes(b"movie")
    sub = subs / "ABS-001.srt"
    sub.write_bytes(b"sub")
    img = imgs / "cover.jpg"
    img.write_bytes(b"img")

    fake_client([str(movie), str(sub), str(img)])
    with session_scope() as session:
        session.add(History(hash="2" * 40, code="ABS-001", save_path=str(movie)))
    medialink.register_scrape("ABS-001", str(source))

    medialink.handle_media_deleted(link_path=str(link))

    assert not subs.exists() and not imgs.exists()
    assert not task.exists(), "任务目录应被清掉"
    assert downloads.exists(), "下载根目录不能被删掉"


def test_task_dir_kept_when_other_files_remain(linked, fake_client):
    """任务目录里还有种子之外的文件时，目录要保留。"""
    source, link = linked
    task = source.parent / "ABS-001.1080p"
    task.mkdir()
    movie = task / "ABS-001.mp4"
    movie.write_bytes(b"movie")
    extra = task / "我自己放的笔记.txt"
    extra.write_bytes(b"keep me")

    fake_client([str(movie)])
    with session_scope() as session:
        session.add(History(hash="3" * 40, code="ABS-001", save_path=str(movie)))
    medialink.register_scrape("ABS-001", str(source))

    medialink.handle_media_deleted(link_path=str(link))

    assert not movie.exists()
    assert extra.exists(), "种子之外的文件不能删"
    assert task.exists(), "目录非空时必须保留"


def test_torrent_files_absent_falls_back_to_source_path(linked, monkeypatch):
    """下载器查不到清单（种子已被手动删除）时，仍按登记的 source_path 删。"""
    source, link = linked

    class EmptyClient:
        def list_torrent_files(self, hashes):
            return []

        def delete_torrent(self, hashes, delete_files=False):
            return []

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": EmptyClient())

    medialink.register_scrape("ABS-001", str(source))
    result = medialink.handle_media_deleted(link_path=str(link))

    assert not source.exists()
    assert str(source) in result.files_deleted


def test_dry_run_lists_torrent_files(linked, fake_client):
    """演练要把种子里的文件也报出来，且一个都不能删。"""
    source, link = linked
    extra = source.parent / "sample.jpg"
    extra.write_bytes(b"img")

    fake_client([str(source), str(extra)])
    with session_scope() as session:
        session.add(History(hash="0" * 40, code="ABS-001", save_path=str(source)))
    medialink.register_scrape("ABS-001", str(source))

    result = medialink.handle_media_deleted(link_path=str(link), dry_run=True)

    assert str(extra) in result.files_deleted
    assert extra.exists() and source.exists()


def test_client_without_list_method_is_skipped(linked, monkeypatch):
    """老下载器没实现 list_torrent_files 时要跳过，不能抛异常。"""
    source, link = linked

    class LegacyClient:
        def delete_torrent(self, hashes, delete_files=False):
            return list(hashes)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": LegacyClient())

    with session_scope() as session:
        session.add(History(hash="1" * 40, code="ABS-001", save_path=str(source)))
    medialink.register_scrape("ABS-001", str(source))

    result = medialink.handle_media_deleted(link_path=str(link))

    assert not source.exists()
    assert not result.errors


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


# ----------------------------------------------------------------------
# 硬链接保护
# ----------------------------------------------------------------------
def test_delete_refuses_source_with_external_hardlink(linked, tmp_path):
    """源文件还被别处硬链接引用时不删它。

    删了那些引用会全部变成坏文件，而空间根本不会释放 —— 引用计数没到 0。
    """
    source, link = linked
    external = tmp_path / "another_library"
    external.mkdir()
    extra = external / "ABS-001.mp4"
    os.link(source, extra)

    medialink.register_scrape("ABS-001", str(source))
    result = medialink.handle_media_deleted(link_path=str(link), dry_run=False)

    # 源文件与外部引用都得留着
    assert source.exists()
    assert extra.exists()
    assert extra.read_bytes() == source.read_bytes()
    # 媒体库那份链接仍然删掉 —— 那是本次删除的目标
    assert not link.exists()
    assert any("其它硬链接引用" in e for e in result.errors)
    assert str(source) not in result.files_deleted


def test_scrape_dir_survives_empty_dir_pruning(tmp_path, configure):
    """刮削输出目录清空后不能被删 —— 删了 Emby 会认为整个媒体库掉线。

    库根设成分类目录的上一层时才会触发：ABS-001/ 空了往上删到日本AV/，
    日本AV/ 也空了就会被一起删掉。
    """
    library = tmp_path / "h_video"
    scrape = library / "日本AV"
    movie = scrape / "ABS-001"
    source_dir = tmp_path / "downloads"
    movie.mkdir(parents=True)
    source_dir.mkdir()

    source = source_dir / "abs-001.mp4"
    source.write_bytes(b"x" * 512)
    link = movie / "ABS-001.mp4"
    os.link(source, link)

    configure(
        medialink_library_path=str(library),
        medialink_scrape_dir=str(scrape),
        medialink_delete_enabled=True,
    )
    medialink.register_scrape("ABS-001", str(source))

    medialink.handle_media_deleted(link_path=str(link), dry_run=False)

    # 番号目录空了应该被清掉，但刮削输出目录必须留着
    assert not movie.exists()
    assert scrape.is_dir()
    assert library.is_dir()


def test_delete_proceeds_once_external_hardlink_is_gone(linked, tmp_path):
    source, link = linked
    external = tmp_path / "another_library"
    external.mkdir()
    extra = external / "ABS-001.mp4"
    os.link(source, extra)
    extra.unlink()

    medialink.register_scrape("ABS-001", str(source))
    result = medialink.handle_media_deleted(link_path=str(link), dry_run=False)

    assert not source.exists()
    assert not any("其它硬链接引用" in e for e in result.errors)
