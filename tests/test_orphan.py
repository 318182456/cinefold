"""孤儿关联：下载侧已删、媒体库侧仍在的一览。"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.database.models import History, MediaLink
from app.database.session import session_scope
from app.services import orphan


@pytest.fixture(autouse=True)
def clean_tables():
    """建表并清掉关联表与历史，避免用例相互污染。"""
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)

    def _clear():
        with session_scope() as session:
            for row in session.query(MediaLink).all():
                session.delete(row)
            for row in session.query(History).all():
                session.delete(row)

    _clear()
    yield
    _clear()


@pytest.fixture
def no_downloader(monkeypatch):
    """默认把下载器置成「没配置」，用例按需再覆盖。

    不隔离的话 _torrent_view 会走真实工厂，answered 取决于环境配置，
    种子维度的断言就不稳定了。
    """
    monkeypatch.setattr(
        "app.modules.downloadclient.list_configured_clients", lambda: []
    )


def _link(tmp_path, code="ABS-001", *, keep_source=True):
    """造一条 media_link 记录，返回 (source, link)。

    keep_source=False 时源文件建完就删 —— 模拟「qb/tr 里删掉了」。
    """
    source_dir = tmp_path / "downloads"
    library = tmp_path / "library" / code
    source_dir.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)

    source = source_dir / f"{code.lower()}.mp4"
    source.write_bytes(b"x" * 1024)
    link = library / f"{code}.mp4"
    os.link(source, link)

    with session_scope() as session:
        session.add(MediaLink(
            link_path=str(link),
            code=code,
            source_path=str(source),
            inode=source.stat().st_ino,
            device=source.stat().st_dev,
            create_time=datetime(2026, 1, 1, 12, 0, 0),
        ))

    if not keep_source:
        source.unlink()
    return source, link


# ----------------------------------------------------------------------
def test_source_gone_but_link_alive_is_orphan(tmp_path, no_downloader):
    """源文件被删、硬链接还在 —— 正是要报告的那种。"""
    source, link = _link(tmp_path, keep_source=False)

    items = orphan.scan_orphans()

    assert len(items) == 1
    assert items[0]["link_path"] == str(link)
    assert items[0]["source_gone"] is True
    assert items[0]["delete_time"]          # 删除时间已记录
    assert items[0]["create_time"].startswith("2026-01-01")


def test_healthy_link_not_reported(tmp_path, no_downloader):
    """两侧都在的正常关联不该出现在一览里。"""
    _link(tmp_path)
    assert orphan.scan_orphans() == []


def test_both_gone_not_reported(tmp_path, no_downloader):
    """两侧都没了是普通失效记录，归 prune 管，不属于这个一览。

    这个一览的前提是「Emby 里还看得见」，链接都没了就不成立。
    """
    source, link = _link(tmp_path, keep_source=False)
    link.unlink()
    assert orphan.scan_orphans() == []


def test_delete_time_persisted_and_stable(tmp_path, no_downloader):
    """删除时间首次发现时记下，之后重复扫描不该被刷新。

    时间戳的意义是「什么时候没的」，每轮扫描都改写就变成了「上次扫描时间」。
    """
    _link(tmp_path, keep_source=False)

    first = orphan.scan_orphans()[0]["delete_time"]
    assert first

    with session_scope() as session:
        row = session.scalars(session.query(MediaLink).statement).first()
        assert row.source_gone_time is not None

    second = orphan.scan_orphans()[0]["delete_time"]
    assert second == first


def test_recovered_source_clears_delete_time(tmp_path, no_downloader):
    """源文件回来了（移动/改名后又回到原位）要清掉删除时间并移出一览。"""
    source, _ = _link(tmp_path, keep_source=False)

    assert len(orphan.scan_orphans()) == 1

    source.write_bytes(b"x" * 1024)          # 文件回来了
    assert orphan.scan_orphans() == []

    with session_scope() as session:
        row = session.scalars(session.query(MediaLink).statement).first()
        assert row.source_gone_time is None


def test_passthrough_record_never_orphan(tmp_path, no_downloader):
    """直通模式 link_path == source_path，同一个文件不可能一个在一个没。"""
    library = tmp_path / "library"
    library.mkdir(parents=True)
    same = library / "FC2-123.mp4"
    same.write_bytes(b"x" * 1024)

    with session_scope() as session:
        session.add(MediaLink(
            link_path=str(same), code="FC2-123", source_path=str(same),
        ))

    assert orphan.scan_orphans() == []


# ---------------------------------------------------------------- 种子维度
class _Client:
    """假下载器。hashes 为当前持有的种子，files 为种子内文件。"""

    def __init__(self, hashes, files=()):
        self._hashes = list(hashes)
        self._files = list(files)

    def monitor_torrent(self, hashes=None):
        return [{"hash": h} for h in self._hashes]

    def list_torrent_files(self, hashes):
        return list(self._files)


def _use_client(monkeypatch, client):
    monkeypatch.setattr(
        "app.modules.downloadclient.list_configured_clients", lambda: ["qbittorrent"]
    )
    monkeypatch.setattr(
        "app.modules.downloadclient.get_download_client", lambda name="": client
    )


def test_torrent_gone_flagged(tmp_path, monkeypatch):
    """History 里登记过的 hash 已不在下载器里 —— 标成「种子已删」。

    源文件仍在，所以只有种子维度成立，delete_time 应为空。
    """
    source, _ = _link(tmp_path)
    with session_scope() as session:
        session.add(History(hash="deadbeef", code="ABS-001", save_path=str(source)))

    _use_client(monkeypatch, _Client(hashes=["other"]))

    items = orphan.scan_orphans()
    assert len(items) == 1
    assert items[0]["torrent_gone"] is True
    assert items[0]["source_gone"] is False
    # 源文件还在，没被删过，就没有删除时刻
    assert items[0]["delete_time"] == ""


def test_torrent_still_present_not_flagged(tmp_path, monkeypatch):
    """种子还在下载器里，不该报告。"""
    source, _ = _link(tmp_path)
    with session_scope() as session:
        session.add(History(hash="DEADBEEF", code="ABS-001", save_path=str(source)))

    # 大小写不同：hash 在各下载器/接口间写法不一，必须归一化后再比
    _use_client(monkeypatch, _Client(hashes=["deadbeef"]))

    assert orphan.scan_orphans() == []


def test_downloader_offline_never_flags_torrent_gone(tmp_path, monkeypatch):
    """下载器全挂时不做种子维度判定 —— 否则会把全库标成「种子已删」。

    这是这个功能最不该出的错：monitor_torrent 失败返回空列表，
    与「一个种子都没有」在返回值上完全一样。
    """
    source, _ = _link(tmp_path)
    with session_scope() as session:
        session.add(History(hash="deadbeef", code="ABS-001", save_path=str(source)))

    class _Dead:
        def monitor_torrent(self, hashes=None):
            raise RuntimeError("connection refused")

        def list_torrent_files(self, hashes):
            raise RuntimeError("connection refused")

    _use_client(monkeypatch, _Dead())

    assert orphan.scan_orphans() == []


def test_no_history_never_flags_torrent_gone(tmp_path, monkeypatch):
    """从没登记过种子的关联（手工拷进来的）不该被算成「种子被删了」。"""
    _link(tmp_path)
    _use_client(monkeypatch, _Client(hashes=["whatever"]))

    assert orphan.scan_orphans() == []


def test_transferred_seed_not_flagged(tmp_path, monkeypatch):
    """转种后 History 里的旧 hash 已失效，但源文件仍被新种子持有。

    只比 hash 会把转过种的片子全体误报，得回退到「文件是否仍在种子清单里」。
    """
    source, _ = _link(tmp_path)
    with session_scope() as session:
        session.add(History(hash="oldhash", code="ABS-001", save_path=str(source)))

    # 旧 hash 不在了，但文件出现在新种子的清单里
    _use_client(monkeypatch, _Client(hashes=["newhash"], files=[str(source)]))

    assert orphan.scan_orphans() == []


# ---------------------------------------------------------------- 批量操作
def _batch_body(paths, dry_run=True):
    from app.api.endpoints.medialink import BatchRequest

    return BatchRequest(link_paths=paths, dry_run=dry_run)


def test_batch_drop_records_removes_all(tmp_path, no_downloader):
    """批量删记录：选中的全删掉，文件一个都不碰。"""
    from app.api.endpoints.medialink import batch_drop_records

    _, link_a = _link(tmp_path, "ABS-001", keep_source=False)
    _, link_b = _link(tmp_path, "ABS-002", keep_source=False)

    resp = batch_drop_records(
        _batch_body([str(link_a), str(link_b)]), current_user="admin"
    )
    data = resp["data"]

    assert sorted(data["removed"]) == sorted([str(link_a), str(link_b)])
    assert data["missing"] == []
    # 只删记录，媒体库里的文件必须还在
    assert link_a.exists() and link_b.exists()

    with session_scope() as session:
        assert session.query(MediaLink).count() == 0


def test_batch_drop_records_reports_missing(tmp_path, no_downloader):
    """选中的记录已被别处删掉时，如实报告而不是静默略过。"""
    from app.api.endpoints.medialink import batch_drop_records

    _, link = _link(tmp_path, "ABS-001", keep_source=False)

    resp = batch_drop_records(
        _batch_body([str(link), "/nowhere/ghost.mp4"]), current_user="admin"
    )
    data = resp["data"]

    assert data["removed"] == [str(link)]
    assert data["missing"] == ["/nowhere/ghost.mp4"]


def test_batch_rejects_empty_selection():
    """没选中任何记录时拒绝，不该当成「全选」误删全库。"""
    from app.api.endpoints.medialink import batch_delete, batch_drop_records

    assert batch_delete(_batch_body([]), current_user="admin")["code"] == 400
    assert batch_drop_records(_batch_body([]), current_user="admin")["code"] == 400
    # 全是空串同理
    assert batch_drop_records(_batch_body(["", ""]), current_user="admin")["code"] == 400


def test_batch_delete_continues_after_failure(tmp_path, monkeypatch, no_downloader):
    """单条抛异常不该中断整批 —— 其余的照常删完，错误逐条收集。"""
    from app.api.endpoints import medialink as endpoint

    _, link_a = _link(tmp_path, "ABS-001", keep_source=False)
    _, link_b = _link(tmp_path, "ABS-002", keep_source=False)

    real = endpoint.handle_media_deleted

    def _flaky(link_path="", code="", dry_run=True):
        if link_path == str(link_a):
            raise RuntimeError("boom")
        return real(link_path=link_path, code=code, dry_run=dry_run)

    monkeypatch.setattr(endpoint, "handle_media_deleted", _flaky)

    resp = endpoint.batch_delete(
        _batch_body([str(link_a), str(link_b)], dry_run=True), current_user="admin"
    )
    data = resp["data"]

    assert data["total"] == 2
    # 挂掉的那条进 errors，另一条仍然产出了结果
    assert len(data["results"]) == 1
    assert any("boom" in e for e in data["errors"])


def test_batch_delete_dry_run_keeps_files(tmp_path, no_downloader, monkeypatch):
    """演练不动磁盘：文件与记录都必须原样保留。"""
    from app.api.endpoints.medialink import batch_delete
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "medialink_delete_enabled", True)
    monkeypatch.setattr(settings, "medialink_library_path", str(tmp_path / "library"))

    _, link = _link(tmp_path, "ABS-001", keep_source=False)

    resp = batch_delete(_batch_body([str(link)], dry_run=True), current_user="admin")
    data = resp["data"]

    assert data["dry_run"] is True
    assert data["deleted"] == 0
    assert link.exists()
    with session_scope() as session:
        assert session.query(MediaLink).count() == 1


# ---------------------------------------------------------------- 批内缓存
def test_torrent_batch_queries_downloader_once(tmp_path, monkeypatch):
    """批作用域内，同一个种子的文件清单只问下载器一次。

    不带缓存时每条删除都要重拉一轮全量清单 —— 种子上千时这是分钟级与
    秒级的差别，也是这个功能最初慢到不可用的原因。
    """
    from app.core.config import get_settings
    from app.services import medialink

    settings = get_settings()
    monkeypatch.setattr(settings, "medialink_library_path", str(tmp_path / "library"))
    monkeypatch.setattr(settings, "medialink_delete_enabled", True)

    _, link_a = _link(tmp_path, "ABS-001", keep_source=False)
    _, link_b = _link(tmp_path, "ABS-002", keep_source=False)

    # 两条记录各有种子。hash 是主键，同一个 hash 插不了两行
    with session_scope() as session:
        session.add(History(hash="f" * 40, code="ABS-001"))
        session.add(History(hash="e" * 40, code="ABS-002"))

    calls: list[str] = []

    class _Counting:
        def monitor_torrent(self, hashes=None):
            return [{"hash": "f" * 40}]

        def list_torrent_files(self, hashes):
            calls.append("list")
            return []

        def find_torrents_by_path(self, paths):
            calls.append("find")
            return {}

        def delete_torrent(self, hashes, delete_files=False):
            return list(hashes)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": _Counting())

    with medialink.torrent_batch():
        medialink.handle_media_deleted(link_path=str(link_a), dry_run=True)
        medialink.handle_media_deleted(link_path=str(link_b), dry_run=True)

    # 按路径反查整批只做一次 —— 它的成本与种子总数成正比而与查询量无关，
    # 逐条问就是 N 轮全量拉取，这是批量删除慢的主因
    assert calls.count("find") == 1, f"路径反查应只做一次: {calls}"
    # 两个不同的 hash 各查一次清单：缓存按 hash 去重，不同种子本就该各查各的。
    # 真正省掉的是「同一 hash 在批内被反复问」，见下面的合集用例
    assert calls.count("list") == 2, f"两个不同种子各查一次: {calls}"


def test_torrent_batch_dedupes_same_hash(tmp_path, monkeypatch):
    """同一个种子被多条记录共用（合集）时，清单在批内只查一次。"""
    from app.core.config import get_settings
    from app.services import medialink

    settings = get_settings()
    monkeypatch.setattr(settings, "medialink_library_path", str(tmp_path / "library"))

    _, link_a = _link(tmp_path, "ABS-001", keep_source=False)
    _, link_b = _link(tmp_path, "ABS-002", keep_source=False)

    # 合集种子：一个 hash 打包两部片。History 的主键是 hash，所以两个番号
    # 的映射靠按路径反查建立，而不是两行 History
    shared = "f" * 40
    with session_scope() as session:
        session.add(History(hash=shared, code="ABS-001"))

    calls: list[str] = []

    class _Counting:
        def monitor_torrent(self, hashes=None):
            return [{"hash": shared}]

        def list_torrent_files(self, hashes):
            calls.append("list")
            return []

        def find_torrents_by_path(self, paths):
            # 两条记录的源文件都属于这一个种子
            return {p: [shared] for p in paths}

        def delete_torrent(self, hashes, delete_files=False):
            return list(hashes)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": _Counting())

    with medialink.torrent_batch():
        medialink.handle_media_deleted(link_path=str(link_a), dry_run=True)
        medialink.handle_media_deleted(link_path=str(link_b), dry_run=True)

    assert calls.count("list") == 1, f"同一 hash 应只查一次清单: {calls}"


def test_no_batch_scope_keeps_original_behaviour(tmp_path, monkeypatch):
    """不开批作用域时行为与优化前一致：每次都问下载器要最新的。

    缓存必须是显式开启的 —— 陈旧的种子清单会让删除范围出错，
    这个代价不能靠「时间还没到」来兜。
    """
    from app.core.config import get_settings
    from app.services import medialink

    settings = get_settings()
    monkeypatch.setattr(settings, "medialink_library_path", str(tmp_path / "library"))

    _, link_a = _link(tmp_path, "ABS-001", keep_source=False)
    _, link_b = _link(tmp_path, "ABS-002", keep_source=False)
    with session_scope() as session:
        session.add(History(hash="f" * 40, code="ABS-001"))
        session.add(History(hash="e" * 40, code="ABS-002"))

    calls: list[str] = []

    class _Counting:
        def monitor_torrent(self, hashes=None):
            return [{"hash": "f" * 40}]

        def list_torrent_files(self, hashes):
            calls.append("list")
            return []

        def find_torrents_by_path(self, paths):
            return {}

        def delete_torrent(self, hashes, delete_files=False):
            return list(hashes)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    monkeypatch.setattr(dc, "get_download_client", lambda name="": _Counting())

    medialink.handle_media_deleted(link_path=str(link_a), dry_run=True)
    medialink.handle_media_deleted(link_path=str(link_b), dry_run=True)

    # 没有批作用域，两次删除各查各的
    assert calls.count("list") == 2, f"无缓存时应各查一次: {calls}"
