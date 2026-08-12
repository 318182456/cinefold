"""监控目录：硬链接同步、移动判定、延迟删除、种子登记。

硬链接是真的建、真的删，所以全部用 tmp_path 里的真实文件跑 —— mock 掉
文件系统就测不出 inode 相关的行为，而这个功能的正确性几乎全靠 inode。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.core.config import get_settings
from app.database.models import History, MediaLink, PendingDelete, WatchDir
from app.database.session import session_scope
from app.services import medialink, watchdir


@pytest.fixture
def configure():
    """直接改写已加载的 Settings。理由同 test_medialink.py。"""
    settings = get_settings()
    keys = (
        "medialink_library_path",
        "medialink_scrape_dir",
        "medialink_delete_enabled",
        "watchdir_delete_grace",
        "qbittorrent_download_path",
        "transmission_download_path",
    )
    original = {key: getattr(settings, key) for key in keys}

    def _apply(**kwargs):
        for key, value in kwargs.items():
            setattr(settings, key, value)

    yield _apply
    for key, value in original.items():
        setattr(settings, key, value)


@pytest.fixture(autouse=True)
def clean_tables():
    """建表并清掉本模块用到的表，避免用例相互污染。"""
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)

    def _clear():
        with session_scope() as session:
            for model in (PendingDelete, MediaLink, WatchDir, History):
                for row in session.scalars(sa.select(model)).all():
                    session.delete(row)

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def instant_stability(monkeypatch):
    """稳定性检查在测试里没必要真等 —— 文件都是一次写完的。"""
    monkeypatch.setattr(watchdir, "STABLE_CHECK_INTERVAL", 0.0)
    monkeypatch.setattr(watchdir, "STABLE_CHECK_ROUNDS", 1)


@pytest.fixture
def rule(tmp_path, configure):
    """造一条监控规则：源目录 + 媒体库，宽限期默认关掉。

    返回 (rule_id, 源目录, 媒体库根)。
    """
    source_dir = tmp_path / "downloads"
    library = tmp_path / "library"
    source_dir.mkdir(parents=True)
    library.mkdir(parents=True)

    configure(
        medialink_library_path=str(library),
        # 不设刮削输出目录 —— 它会让空目录清理多一道保护，与本文件的用例无关
        medialink_scrape_dir="",
        medialink_delete_enabled=True,
        # 大部分用例测的是「判定对不对」，不是「等够没等够」
        watchdir_delete_grace=0,
    )

    with session_scope() as session:
        session.add(WatchDir(
            source_dir=str(source_dir), target_subdir="sv", name="short",
            enabled=True, recursive=True, reverse_delete=True, code_prefix="SV",
        ))
    with session_scope() as session:
        rule_id = session.scalar(sa.select(WatchDir.id))

    return rule_id, source_dir, library


def _links() -> dict[str, str]:
    with session_scope() as session:
        return {
            r.link_path: r.code
            for r in session.scalars(sa.select(MediaLink)).all()
        }


def _same_inode(a: Path, b: Path) -> bool:
    sa_, sb = a.stat(), b.stat()
    return (sa_.st_ino, sa_.st_dev) == (sb.st_ino, sb.st_dev)


# ----------------------------------------------------------------------
# 命名规则
# ----------------------------------------------------------------------
def test_make_code_uses_filename_without_prefix():
    plain = WatchDir(source_dir="/x", code_prefix="")
    assert watchdir.make_code(plain, Path("/x/ABC-123.mp4")) == "ABC-123"


def test_make_code_applies_prefix():
    prefixed = WatchDir(source_dir="/x", code_prefix="SV")
    assert watchdir.make_code(prefixed, Path("/x/clip.mp4")) == "SV-clip"


def test_target_path_preserves_relative_structure(tmp_path):
    """target_path 的第三参是已解析好的目标根，不再重复拼 target_subdir。"""
    rule = WatchDir(source_dir=str(tmp_path / "src"), target_subdir="sv")
    (tmp_path / "src" / "2026").mkdir(parents=True)
    base = tmp_path / "lib" / "sv"
    base.mkdir(parents=True)

    target = watchdir.target_path(
        rule, tmp_path / "src" / "2026" / "a.mp4", base
    )
    assert target == base / "2026" / "a.mp4"


def test_target_path_resolves_base_when_omitted(tmp_path, configure):
    """省略 base 时自行按规则解析。"""
    library = tmp_path / "lib"
    library.mkdir()
    configure(medialink_library_path=str(library))
    (tmp_path / "src").mkdir()
    rule = WatchDir(source_dir=str(tmp_path / "src"), target_subdir="sv")

    target = watchdir.target_path(rule, tmp_path / "src" / "a.mp4")
    assert target == library.resolve() / "sv" / "a.mp4"


def test_target_path_rejects_file_outside_source(tmp_path):
    rule = WatchDir(source_dir=str(tmp_path / "src"), target_subdir="sv")
    (tmp_path / "src").mkdir()
    assert watchdir.target_path(
        rule, tmp_path / "elsewhere.mp4", tmp_path / "lib"
    ) is None


# ----------------------------------------------------------------------
# 目标目录解析：target_dir 优先，回退到库根 + 子目录
# ----------------------------------------------------------------------
def test_target_dir_takes_precedence_over_library(tmp_path, configure):
    configure(medialink_library_path=str(tmp_path / "library"))
    rule = WatchDir(
        source_dir=str(tmp_path / "src"),
        target_dir=str(tmp_path / "elsewhere" / "短视频"),
        target_subdir="ignored",
    )
    assert watchdir.target_base(rule) == tmp_path / "elsewhere" / "短视频"


def test_target_base_falls_back_to_library_subdir(tmp_path, configure):
    library = tmp_path / "library"
    library.mkdir()
    configure(medialink_library_path=str(library))
    rule = WatchDir(source_dir=str(tmp_path / "src"), target_dir="", target_subdir="sv")
    assert watchdir.target_base(rule) == library.resolve() / "sv"


def test_target_base_none_without_any_config(tmp_path, configure):
    configure(medialink_library_path="")
    rule = WatchDir(source_dir=str(tmp_path / "src"), target_dir="", target_subdir="")
    assert watchdir.target_base(rule) is None


def test_sync_uses_absolute_target_dir(tmp_path, configure):
    """目标目录填绝对路径时，链接建到那里，与媒体库根目录无关。"""
    source_dir = tmp_path / "downloads"
    target = tmp_path / "independent_library" / "短视频"
    source_dir.mkdir(parents=True)
    # 目标目录故意不预先创建 —— 建链接时应自动 mkdir
    configure(
        medialink_library_path=str(tmp_path / "unrelated"),
        medialink_delete_enabled=True,
        watchdir_delete_grace=0,
    )

    with session_scope() as session:
        session.add(WatchDir(
            source_dir=str(source_dir), target_dir=str(target),
            name="sv", enabled=True, recursive=True, code_prefix="SV",
        ))
    with session_scope() as session:
        rule_id = session.scalar(sa.select(WatchDir.id))

    (source_dir / "2026").mkdir()
    a = source_dir / "2026" / "a.mp4"
    a.write_bytes(b"A" * 64)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    link = target / "2026" / "a.mp4"
    assert link.exists()
    assert _same_inode(a, link)
    # 不该在媒体库根目录下建任何东西
    assert not (tmp_path / "unrelated").exists()


def test_sync_with_target_dir_works_without_library_config(tmp_path, configure):
    """填了目标目录时，完全不配媒体库根目录也应能同步。"""
    source_dir = tmp_path / "downloads"
    target = tmp_path / "target"
    source_dir.mkdir(parents=True)
    configure(medialink_library_path="", watchdir_delete_grace=0)

    with session_scope() as session:
        session.add(WatchDir(
            source_dir=str(source_dir), target_dir=str(target),
            name="sv", enabled=True, recursive=True,
        ))
    with session_scope() as session:
        rule_id = session.scalar(sa.select(WatchDir.id))

    (source_dir / "a.mp4").write_bytes(b"A" * 64)
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    assert (target / "a.mp4").exists()


# ----------------------------------------------------------------------
# 正向同步
# ----------------------------------------------------------------------
def test_sync_creates_hardlinks_and_records(rule):
    rule_id, source_dir, library = rule
    (source_dir / "2026").mkdir()
    a = source_dir / "a.mp4"
    b = source_dir / "2026" / "b.mkv"
    a.write_bytes(b"A" * 64)
    b.write_bytes(b"B" * 64)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    la = library / "sv" / "a.mp4"
    lb = library / "sv" / "2026" / "b.mkv"
    assert la.exists() and lb.exists()
    # 硬链接而非拷贝：inode 必须相同
    assert _same_inode(a, la)
    assert _same_inode(b, lb)
    assert sorted(_links().values()) == ["SV-a", "SV-b"]


def test_dry_run_touches_nothing(rule):
    rule_id, source_dir, library = rule
    (source_dir / "a.mp4").write_bytes(b"A")

    result = watchdir.sync_rule(rule_id, dry_run=True)

    assert len(result.linked) == 1
    assert not (library / "sv" / "a.mp4").exists()
    assert _links() == {}


def test_sync_is_idempotent(rule):
    rule_id, source_dir, _ = rule
    (source_dir / "a.mp4").write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)

    again = watchdir.sync_rule(rule_id, dry_run=False)

    assert again.linked == []
    assert again.unlinked == []
    assert again.errors == []


def test_sync_skips_partial_download_files(rule):
    rule_id, source_dir, library = rule
    (source_dir / "incomplete.mp4.part").write_bytes(b"X")
    (source_dir / "incomplete.mp4.!qb").write_bytes(b"X")

    watchdir.sync_rule(rule_id, dry_run=False)

    assert list((library / "sv").glob("*")) == [] or _links() == {}


def test_non_recursive_ignores_subdirectories(rule):
    rule_id, source_dir, library = rule
    with session_scope() as session:
        session.get(WatchDir, rule_id).recursive = False
    (source_dir / "deep").mkdir()
    (source_dir / "top.mp4").write_bytes(b"T")
    (source_dir / "deep" / "nested.mp4").write_bytes(b"N")

    watchdir.sync_rule(rule_id, dry_run=False)

    assert (library / "sv" / "top.mp4").exists()
    assert not (library / "sv" / "deep" / "nested.mp4").exists()


def test_disabled_rule_does_nothing(rule):
    rule_id, source_dir, library = rule
    with session_scope() as session:
        session.get(WatchDir, rule_id).enabled = False
    (source_dir / "a.mp4").write_bytes(b"A")

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert not (library / "sv" / "a.mp4").exists()
    assert result.skipped


def test_missing_library_config_reports_error(rule, configure):
    rule_id, source_dir, _ = rule
    configure(medialink_library_path="")
    (source_dir / "a.mp4").write_bytes(b"A")

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert any("媒体库根目录" in e for e in result.errors)


# ----------------------------------------------------------------------
# 源侧删除
# ----------------------------------------------------------------------
def test_source_deleted_removes_link(rule):
    rule_id, source_dir, library = rule
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"
    assert link.exists()

    a.unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.unlinked) == 1
    assert not link.exists()
    assert _links() == {}


def test_source_outside_scope_keeps_link(rule):
    """源文件还在但已不在监控范围（移出目录）时不该删链接。"""
    rule_id, source_dir, library = rule
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)

    # 记录仍指向原路径，但把文件挪到监控范围之外且保持存在
    outside = source_dir.parent / "moved_away.mp4"
    a.rename(outside)
    with session_scope() as session:
        row = session.scalar(sa.select(MediaLink))
        row.source_path = str(outside)
        row.inode = None  # 关掉移动判定，单测「范围外」这条分支
        row.device = None

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.unlinked == []
    assert any("已不在监控范围" in s for s in result.skipped)


# ----------------------------------------------------------------------
# 移动判定
# ----------------------------------------------------------------------
def test_move_within_source_updates_record_without_relinking(rule):
    """移动不该走「删了又建」—— inode 变了 Emby 的观看记录就丢了。"""
    rule_id, source_dir, library = rule
    old = source_dir / "movable.mp4"
    old.write_bytes(b"M" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    link_old = library / "sv" / "movable.mp4"
    inode_before = link_old.stat().st_ino

    (source_dir / "moved").mkdir()
    new = source_dir / "moved" / "movable.mp4"
    old.rename(new)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    link_new = library / "sv" / "moved" / "movable.mp4"
    assert len(result.moved) == 1
    assert result.unlinked == []
    assert result.reverse_deleted == []
    assert link_new.exists() and not link_old.exists()
    # 关键：inode 不变
    assert link_new.stat().st_ino == inode_before
    assert new.exists()
    assert list(_links()) == [str(link_new)]


def test_rename_is_treated_as_move(rule):
    rule_id, source_dir, library = rule
    old = source_dir / "before.mp4"
    old.write_bytes(b"R" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)
    inode_before = (library / "sv" / "before.mp4").stat().st_ino

    old.rename(source_dir / "after.mp4")
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.moved) == 1
    link_new = library / "sv" / "after.mp4"
    assert link_new.exists()
    assert link_new.stat().st_ino == inode_before


# ----------------------------------------------------------------------
# 反向删除
# ----------------------------------------------------------------------
def test_library_deletion_removes_source_when_enabled(rule):
    rule_id, source_dir, library = rule
    a = source_dir / "a.mp4"
    a.write_bytes(b"A" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    (library / "sv" / "a.mp4").unlink()  # 模拟 Emby 删除
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.reverse_deleted) == 1
    assert not a.exists()
    assert _links() == {}


def test_library_deletion_keeps_source_when_disabled(rule):
    rule_id, source_dir, library = rule
    with session_scope() as session:
        session.get(WatchDir, rule_id).reverse_delete = False
    a = source_dir / "a.mp4"
    a.write_bytes(b"A" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    (library / "sv" / "a.mp4").unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.reverse_deleted == []
    assert a.exists()
    assert any("未开启反向删除" in s for s in result.skipped)


def test_library_deletion_does_not_relink(rule):
    """媒体库侧删掉的不能被正向同步重建，否则来回拉锯。"""
    rule_id, source_dir, library = rule
    with session_scope() as session:
        session.get(WatchDir, rule_id).reverse_delete = False
    a = source_dir / "a.mp4"
    a.write_bytes(b"A" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    link = library / "sv" / "a.mp4"
    link.unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.linked == []
    assert not link.exists()


# ----------------------------------------------------------------------
# 延迟删除
# ----------------------------------------------------------------------
def test_grace_period_holds_deletion(rule, configure):
    rule_id, source_dir, library = rule
    configure(watchdir_delete_grace=3600)
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"

    a.unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.unlinked == []
    assert len(result.held) == 1
    assert link.exists()
    assert len(watchdir.list_holds(rule_id)) == 1


def test_deletion_executes_after_grace_expires(rule, configure):
    rule_id, source_dir, library = rule
    configure(watchdir_delete_grace=3600)
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"
    a.unlink()
    watchdir.sync_rule(rule_id, dry_run=False)

    # 把发现时间往前挪，等价于宽限期已过
    with session_scope() as session:
        session.get(PendingDelete, str(link)).detected_time = (
            datetime.now() - timedelta(seconds=7200)
        )
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.unlinked) == 1
    assert not link.exists()
    assert watchdir.list_holds(rule_id) == []


def test_recovered_file_cancels_hold(rule, configure):
    """网络存储瞬时不可达 / 用户放回文件 —— 不该删。"""
    rule_id, source_dir, library = rule
    configure(watchdir_delete_grace=3600)
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"

    a.unlink()
    watchdir.sync_rule(rule_id, dry_run=False)
    assert len(watchdir.list_holds(rule_id)) == 1

    a.write_bytes(b"A")  # 文件回来了
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.unlinked == []
    assert link.exists()
    assert watchdir.list_holds(rule_id) == []


def test_cancel_hold_manually(rule, configure):
    rule_id, source_dir, library = rule
    configure(watchdir_delete_grace=3600)
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"
    a.unlink()
    watchdir.sync_rule(rule_id, dry_run=False)

    assert watchdir.cancel_hold(str(link)) is True
    assert watchdir.list_holds(rule_id) == []
    assert watchdir.cancel_hold("/nonexistent/x.mp4") is False


# ----------------------------------------------------------------------
# 种子登记
# ----------------------------------------------------------------------
class _FakeClient:
    """假下载器：torrents 是 {hash: [文件绝对路径]}。"""

    def __init__(self, torrents=None):
        self.torrents = dict(torrents or {})
        self.deleted: list[str] = []

    def find_torrents_by_path(self, paths):
        wanted = set(paths)
        out: dict[str, list[str]] = {}
        for h, files in self.torrents.items():
            for f in files:
                if f in wanted:
                    out.setdefault(f, []).append(h)
        return out

    def list_torrent_files(self, hashes):
        out: list[str] = []
        for h in hashes:
            out.extend(self.torrents.get(h, []))
        return out

    def delete_torrent(self, hashes, delete_files=False):
        hit = [h for h in hashes if h in self.torrents]
        self.deleted.extend(hit)
        for h in hit:
            self.torrents.pop(h, None)
        return hit


@pytest.fixture
def fake_downloader(monkeypatch):
    """接管下载器工厂。返回可改 torrents 的假客户端。"""
    client = _FakeClient()
    import app.modules.downloadclient as dc

    monkeypatch.setattr(dc, "get_download_client", lambda name="": client)
    monkeypatch.setattr(dc, "list_configured_clients", lambda: ["qbittorrent"])
    return client


def test_torrent_hash_recorded_at_link_time(rule, fake_downloader):
    rule_id, source_dir, _ = rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"V" * 64)
    fake_downloader.torrents["HASH_A"] = [str(a)]

    watchdir.sync_rule(rule_id, dry_run=False)

    with session_scope() as session:
        rows = {h.hash: h.code for h in session.scalars(sa.select(History)).all()}
    assert rows == {"HASH_A": "SV-clip"}


def test_torrent_deleted_even_when_downloader_lookup_fails(
    rule, fake_downloader, monkeypatch
):
    """种子已从下载器消失（做种到期/换下载器）时仍要能删 —— 靠建链接时落的库。"""
    rule_id, source_dir, library = rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"V" * 64)
    fake_downloader.torrents["HASH_A"] = [str(a)]
    watchdir.sync_rule(rule_id, dry_run=False)

    # 现查能力失效，只留删除能力
    monkeypatch.setattr(fake_downloader, "find_torrents_by_path", lambda paths: {})

    (library / "sv" / "clip.mp4").unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.reverse_deleted) == 1
    assert result.reverse_deleted[0]["torrents"] == ["HASH_A"]
    assert fake_downloader.deleted == ["HASH_A"]


def test_manual_file_without_torrent_is_not_an_error(rule, fake_downloader):
    rule_id, source_dir, library = rule
    (source_dir / "manual.mp4").write_bytes(b"M")

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    assert (library / "sv" / "manual.mp4").exists()
    with session_scope() as session:
        assert session.scalars(sa.select(History)).all() == []


def test_existing_history_code_is_not_overwritten(rule, fake_downloader):
    """cinefold 自己下载的 code 是真番号，比文件名生成的更准，不该被覆盖。"""
    rule_id, source_dir, _ = rule
    a = source_dir / "ABC-123.mp4"
    a.write_bytes(b"R")
    fake_downloader.torrents["HASH_R"] = [str(a)]
    with session_scope() as session:
        session.add(History(hash="HASH_R", code="ABC-123", save_path=str(a)))

    watchdir.sync_rule(rule_id, dry_run=False)

    with session_scope() as session:
        assert session.get(History, "HASH_R").code == "ABC-123"


def test_backfill_records_torrent_that_appeared_later(rule, fake_downloader):
    """建链接时下载器里还没有种子（下载未完成 / 完成后才移入），事后补上。"""
    rule_id, source_dir, _ = rule
    a = source_dir / "late.mp4"
    a.write_bytes(b"L" * 64)

    # 建链接时下载器是空的
    watchdir.sync_rule(rule_id, dry_run=False)
    with session_scope() as session:
        assert session.scalars(sa.select(History)).all() == []

    # 种子随后出现（下载完成 / 移入完成 / 事后做种）
    fake_downloader.torrents["HASH_LATE"] = [str(a)]
    added = watchdir.backfill_torrents()

    assert added == 1
    with session_scope() as session:
        row = session.get(History, "HASH_LATE")
        assert row is not None and row.code == "SV-late"


def test_backfill_skips_already_recorded(rule, fake_downloader):
    rule_id, source_dir, _ = rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"V" * 64)
    fake_downloader.torrents["HASH_A"] = [str(a)]
    watchdir.sync_rule(rule_id, dry_run=False)

    # 建链接时已登记，补查不该重复写
    assert watchdir.backfill_torrents() == 0


def test_backfill_makes_later_torrent_deletable(rule, fake_downloader, monkeypatch):
    """补登记之后，即使下载器现查失效也能删掉种子。"""
    rule_id, source_dir, library = rule
    a = source_dir / "late.mp4"
    a.write_bytes(b"L" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    fake_downloader.torrents["HASH_LATE"] = [str(a)]
    watchdir.backfill_torrents()

    monkeypatch.setattr(fake_downloader, "find_torrents_by_path", lambda paths: {})
    (library / "sv" / "late.mp4").unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.reverse_deleted[0]["torrents"] == ["HASH_LATE"]
    assert fake_downloader.deleted == ["HASH_LATE"]


def test_watch_dir_is_protected_from_directory_pruning(rule, fake_downloader):
    """种子文件散落在监控目录下时，那个目录不能被当成任务目录删掉。"""
    rule_id, source_dir, library = rule
    video = source_dir / "clip.mp4"
    sample = source_dir / "clip_sample.jpg"
    video.write_bytes(b"V" * 64)
    sample.write_bytes(b"I")
    fake_downloader.torrents["HASH_A"] = [str(video), str(sample)]
    watchdir.sync_rule(rule_id, dry_run=False)

    (library / "sv" / "clip.mp4").unlink()
    watchdir.sync_rule(rule_id, dry_run=False)

    assert source_dir.is_dir()


# ----------------------------------------------------------------------
# 全量对账
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------
@pytest.fixture
def client():
    """带鉴权绕过的测试客户端。"""
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.api.endpoints import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: "admin"
    return TestClient(app)


def test_literal_routes_are_not_shadowed_by_rule_id(client):
    """/holds 等固定路径必须声明在 /{rule_id} 之前。

    否则 FastAPI 会先匹配 /{rule_id}，把 "holds" 当成 int 解析，返回 422 ——
    撤销扣留的接口就永远调不通。
    """
    resp = client.delete("/api/v1/watchdirs/holds", params={"link_path": "/x/y.mp4"})
    body = resp.json()
    # 记录不存在返回 404 是对的；422 说明被 {rule_id} 抢先匹配了
    assert body["code"] == 404, body
    assert "int_parsing" not in str(body)

    assert client.get("/api/v1/watchdirs/holds").json()["code"] == 200
    assert client.post("/api/v1/watchdirs/backfill").json()["code"] == 200
    assert client.post("/api/v1/watchdirs/sync").json()["code"] == 200


def test_create_rejects_missing_source_dir(client, tmp_path, configure):
    configure(medialink_library_path=str(tmp_path))
    resp = client.post(
        "/api/v1/watchdirs",
        json={"source_dir": str(tmp_path / "nope"), "target_subdir": "sv"},
    )
    assert resp.json()["code"] == 400


def test_create_requires_target_when_no_library(client, tmp_path, configure):
    """既没配库根、也没填目标目录时要报错 —— 不知道往哪建链接。"""
    configure(medialink_library_path="")
    (tmp_path / "src").mkdir()
    resp = client.post("/api/v1/watchdirs", json={"source_dir": str(tmp_path / "src")})
    body = resp.json()
    assert body["code"] == 400
    assert "目标目录" in body["message"]


def test_create_accepts_target_dir_without_library(client, tmp_path, configure):
    configure(medialink_library_path="")
    (tmp_path / "src").mkdir()
    resp = client.post("/api/v1/watchdirs", json={
        "source_dir": str(tmp_path / "src"),
        "target_dir": str(tmp_path / "target"),
    })
    assert resp.json()["code"] == 200

    with session_scope() as session:
        row = session.scalar(sa.select(WatchDir))
    assert row.target_dir == str(tmp_path / "target")


def test_create_rejects_relative_target_dir(client, tmp_path, configure):
    configure(medialink_library_path=str(tmp_path))
    (tmp_path / "src").mkdir()
    resp = client.post("/api/v1/watchdirs", json={
        "source_dir": str(tmp_path / "src"),
        "target_dir": "relative/path",
    })
    body = resp.json()
    assert body["code"] == 400
    assert "绝对路径" in body["message"]


def test_list_reports_resolved_target(client, rule):
    _, _, library = rule
    items = client.get("/api/v1/watchdirs").json()["data"]["items"]
    # 第一条是刮削输出目录的受保护占位项，真实规则在它之后
    real = [i for i in items if not i.get("protected")]
    assert real[0]["resolved_target"] == str(library / "sv")


# ----------------------------------------------------------------------
# 刮削输出目录：受保护的占位条目
# ----------------------------------------------------------------------
def test_scrape_dir_listed_as_protected(client, tmp_path, configure):
    scrape = tmp_path / "h_video" / "日本AV"
    scrape.mkdir(parents=True)
    configure(
        medialink_library_path=str(tmp_path / "h_video"),
        medialink_scrape_dir=str(scrape),
    )

    items = client.get("/api/v1/watchdirs").json()["data"]["items"]

    assert items[0]["protected"] is True
    assert items[0]["id"] == 0
    assert items[0]["resolved_target"] == str(scrape)


def test_scrape_dir_falls_back_to_library(client, tmp_path, configure):
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir="",
    )
    items = client.get("/api/v1/watchdirs").json()["data"]["items"]
    assert items[0]["resolved_target"] == str(tmp_path / "lib")


def test_protected_entry_cannot_be_deleted(client, tmp_path, configure):
    configure(medialink_library_path=str(tmp_path), medialink_scrape_dir=str(tmp_path))
    body = client.delete("/api/v1/watchdirs/0").json()
    assert body["code"] == 400
    assert "受保护" in body["message"]


def test_protected_entry_cannot_be_edited_or_synced(client, tmp_path, configure):
    configure(medialink_library_path=str(tmp_path), medialink_scrape_dir=str(tmp_path))
    assert client.put("/api/v1/watchdirs/0", json={"enabled": False}).json()["code"] == 400
    assert client.post("/api/v1/watchdirs/0/sync").json()["code"] == 400


def test_update_target_dir_is_validated(client, rule, tmp_path):
    """单独改目标目录也要走文件系统校验，不能绕过。"""
    rule_id, _, _ = rule
    resp = client.put(f"/api/v1/watchdirs/{rule_id}", json={
        "target_dir": str(tmp_path / "new_target"),
    })
    assert resp.json()["code"] == 200

    with session_scope() as session:
        assert session.get(WatchDir, rule_id).target_dir == str(tmp_path / "new_target")


def test_duplicate_source_dir_is_rejected(client, rule):
    _, source_dir, _ = rule
    resp = client.post("/api/v1/watchdirs", json={"source_dir": str(source_dir)})
    assert resp.json()["code"] == 400


def test_delete_rule_keeps_existing_links(client, rule):
    rule_id, source_dir, library = rule
    (source_dir / "a.mp4").write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"

    assert client.delete(f"/api/v1/watchdirs/{rule_id}").json()["code"] == 200

    # 删规则只是「不再自动同步」，不该顺手删用户媒体库里的文件
    assert link.exists()
    assert str(link) in _links()


def test_sync_all_skips_disabled_rules(rule):
    rule_id, source_dir, _ = rule
    (source_dir / "a.mp4").write_bytes(b"A")
    with session_scope() as session:
        session.get(WatchDir, rule_id).enabled = False

    assert watchdir.sync_all() == []


def test_sync_all_survives_one_bad_rule(rule, tmp_path):
    """一条规则的源目录不存在，不该影响其余规则。"""
    rule_id, source_dir, library = rule
    (source_dir / "a.mp4").write_bytes(b"A")
    with session_scope() as session:
        session.add(WatchDir(
            source_dir=str(tmp_path / "does_not_exist"), target_subdir="x",
            name="broken", enabled=True,
        ))

    results = watchdir.sync_all()

    assert len(results) == 2
    assert (library / "sv" / "a.mp4").exists()
    assert any(r.errors for r in results)


# ----------------------------------------------------------------------
# 认领已有硬链接
# ----------------------------------------------------------------------
def test_claims_existing_hardlink_instead_of_duplicating(rule):
    """目标目录里已有同 inode 的硬链接时，只登记，不再建一份。

    刮削工具（MDCng 之类）早就把源文件链接进媒体库了，路径按它自己的规则
    组织，与我们算出来的对不上。不认领就会重复建，媒体库里凭空多出一部
    同样的片子。
    """
    rule_id, source_dir, library = rule
    source = source_dir / "ofku-232.mp4"
    source.write_bytes(b"DATA")

    # 刮削工具建的链接：路径完全是它自己的命名规则
    scraped = library / "sv" / "一条美绪" / "OFKU-232 一条美绪" / "OFKU-232-有码.mp4"
    scraped.parent.mkdir(parents=True)
    os.link(source, scraped)

    result = watchdir.sync_rule(rule_id)

    # 认领了那条，没有新建
    assert len(result.claimed) == 1
    assert result.claimed[0]["link_path"] == str(scraped)
    assert result.linked == []

    # 规则本来会算出这个路径，认领之后不该存在
    assert not (library / "sv" / "ofku-232.mp4").exists()

    # 记录指向真实存在的那个文件
    links = _links()
    assert list(links) == [str(scraped)]
    assert links[str(scraped)] == "SV-ofku-232"


def test_claim_is_idempotent(rule):
    """认领过的链接，再对账一次不该重复处理。"""
    rule_id, source_dir, library = rule
    source = source_dir / "a.mp4"
    source.write_bytes(b"A")
    scraped = library / "sv" / "custom" / "renamed.mp4"
    scraped.parent.mkdir(parents=True)
    os.link(source, scraped)

    watchdir.sync_rule(rule_id)
    second = watchdir.sync_rule(rule_id)

    # 第二轮无事可做：已登记且文件存在
    assert second.claimed == []
    assert second.linked == []
    assert len(_links()) == 1


def test_claim_skipped_when_no_existing_link(rule):
    """目标目录里没有同 inode 的文件时，照常建链接。"""
    rule_id, source_dir, library = rule
    (source_dir / "a.mp4").write_bytes(b"A")

    result = watchdir.sync_rule(rule_id)

    assert result.claimed == []
    assert result.linked == [str(library / "sv" / "a.mp4")]


def test_claim_ignores_unrelated_file_with_same_name(rule):
    """同名但不同 inode 的文件不算数 —— 那是另一份数据，不能认领。"""
    rule_id, source_dir, library = rule
    source = source_dir / "a.mp4"
    source.write_bytes(b"A")

    # 目标位置上有个同名文件，但内容是独立的（不是硬链接）
    other = library / "sv" / "elsewhere" / "a.mp4"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"DIFFERENT")

    result = watchdir.sync_rule(rule_id)

    # 没认领，正常建链接
    assert result.claimed == []
    assert result.linked == [str(library / "sv" / "a.mp4")]
    # 那个无关文件原样保留
    assert other.read_bytes() == b"DIFFERENT"


def test_claim_dry_run_reports_without_writing(rule):
    """演练只报告认领，不写库。"""
    rule_id, source_dir, library = rule
    source = source_dir / "a.mp4"
    source.write_bytes(b"A")
    scraped = library / "sv" / "custom" / "renamed.mp4"
    scraped.parent.mkdir(parents=True)
    os.link(source, scraped)

    result = watchdir.sync_rule(rule_id, dry_run=True)

    assert len(result.claimed) == 1
    assert _links() == {}


def test_claimed_link_deleted_in_library_triggers_reverse(rule):
    """认领来的链接在媒体库侧被删掉，同样要触发反向删除。

    认领的 link_path 由刮削工具决定，不等于规则算出的目标路径。反向删除的
    判定不能只认后者，否则「在 Emby 里删掉刮削建的那份」永远不会联动。
    """
    rule_id, source_dir, library = rule
    source = source_dir / "a.mp4"
    source.write_bytes(b"A")
    scraped = library / "sv" / "custom" / "renamed.mp4"
    scraped.parent.mkdir(parents=True)
    os.link(source, scraped)

    watchdir.sync_rule(rule_id)
    assert list(_links()) == [str(scraped)]

    # 用户在媒体库里删掉了这个链接
    scraped.unlink()

    result = watchdir.sync_rule(rule_id)

    # 源文件应该被反向删除
    assert result.reverse_deleted, "认领的链接被删后没有触发反向删除"
    assert not source.exists()
    assert _links() == {}


def test_claimed_link_not_rebuilt_after_library_delete(rule):
    """媒体库侧删掉认领的链接后，不该又建一份出来。"""
    rule_id, source_dir, library = rule
    source = source_dir / "a.mp4"
    source.write_bytes(b"A")
    scraped = library / "sv" / "custom" / "renamed.mp4"
    scraped.parent.mkdir(parents=True)
    os.link(source, scraped)

    watchdir.sync_rule(rule_id)
    scraped.unlink()
    watchdir.sync_rule(rule_id)

    # 规则算出的那个路径也不该冒出来
    assert not (library / "sv" / "a.mp4").exists()


# ----------------------------------------------------------------------
# 直通模式：不建硬链接，源目录自己就是 Emby 扫的目录
# ----------------------------------------------------------------------
@pytest.fixture
def passthrough_rule(tmp_path, configure):
    """直通规则：没有目标目录，Emby 直接扫源目录。

    返回 (rule_id, 源目录)。
    """
    source_dir = tmp_path / "shorts"
    source_dir.mkdir(parents=True)

    configure(
        medialink_library_path=str(source_dir),
        medialink_scrape_dir="",
        medialink_delete_enabled=True,
        watchdir_delete_grace=0,
    )

    with session_scope() as session:
        session.add(WatchDir(
            source_dir=str(source_dir), target_dir="", target_subdir="",
            name="shorts", enabled=True, recursive=True,
            reverse_delete=True, code_prefix="SV", passthrough=True,
        ))
    with session_scope() as session:
        rule_id = session.scalar(sa.select(WatchDir.id))

    return rule_id, source_dir


def test_passthrough_target_is_the_source_itself():
    rule = WatchDir(source_dir="/downloads/shorts", passthrough=True)
    source = Path("/downloads/shorts/2026/a.mp4")
    assert watchdir.target_path(rule, source) == source


def test_passthrough_registers_without_creating_links(passthrough_rule):
    """只登记，不建文件 —— 目录里还是那一个文件。"""
    rule_id, source_dir = passthrough_rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"C" * 64)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.linked == [str(a)]
    assert result.errors == []
    # 没有多出任何文件，且 link_path 就是源文件本身
    assert list(source_dir.rglob("*.mp4")) == [a]
    assert _links() == {str(a): "SV-clip"}
    assert a.stat().st_nlink == 1


def test_passthrough_records_torrent_at_register_time(
    passthrough_rule, fake_downloader
):
    """删除时全靠这条记录找回种子，登记这一刻必须落库。"""
    rule_id, source_dir = passthrough_rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"C" * 64)
    fake_downloader.torrents["HASH_C"] = [str(a)]

    watchdir.sync_rule(rule_id, dry_run=False)

    with session_scope() as session:
        rows = {h.hash: h.code for h in session.scalars(sa.select(History)).all()}
    assert rows == {"HASH_C": "SV-clip"}


def test_passthrough_deletion_removes_torrent_and_record(
    passthrough_rule, fake_downloader
):
    """Emby 删掉文件 → 删种 + 清记录。这是这个模式存在的唯一理由。"""
    rule_id, source_dir = passthrough_rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"C" * 64)
    fake_downloader.torrents["HASH_C"] = [str(a)]
    watchdir.sync_rule(rule_id, dry_run=False)

    a.unlink()  # 模拟 Emby 删除
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.reverse_deleted) == 1
    assert result.reverse_deleted[0]["torrents"] == ["HASH_C"]
    assert fake_downloader.deleted == ["HASH_C"]
    assert _links() == {}


def test_passthrough_deletion_reports_link_once(
    passthrough_rule, fake_downloader
):
    """link_path 与 source_path 是同一个文件，不该被计两次。"""
    rule_id, source_dir = passthrough_rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"C" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)
    a.unlink()

    outcome = medialink.handle_media_deleted(link_path=str(a), dry_run=False)

    assert outcome.links_deleted == [str(a)]
    assert outcome.errors == []


def test_passthrough_keeps_file_when_reverse_delete_off(passthrough_rule):
    """没开反向删除时，记录留着不动 —— 别把用户的文件当成该清理的垃圾。"""
    rule_id, source_dir = passthrough_rule
    with session_scope() as session:
        session.get(WatchDir, rule_id).reverse_delete = False
    a = source_dir / "clip.mp4"
    a.write_bytes(b"C" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    a.unlink()
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.reverse_deleted == []
    assert any("未开启反向删除" in s for s in result.skipped)


def test_passthrough_move_updates_both_paths(passthrough_rule):
    """在源目录里挪动文件是移动不是删除，两个路径字段都要跟着走。"""
    rule_id, source_dir = passthrough_rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"C" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    moved = source_dir / "2026"
    moved.mkdir()
    target = moved / "clip.mp4"
    a.rename(target)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.reverse_deleted == []
    assert target.exists()
    with session_scope() as session:
        rows = session.scalars(sa.select(MediaLink)).all()
        assert len(rows) == 1
        # 直通模式下两个字段指的是同一个文件，必须同时更新
        assert rows[0].link_path == str(target)
        assert rows[0].source_path == str(target)


def test_passthrough_sync_is_idempotent(passthrough_rule):
    """重复对账不该反复登记，也不该把已登记的文件判成待处理。"""
    rule_id, source_dir = passthrough_rule
    (source_dir / "clip.mp4").write_bytes(b"C" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.linked == []
    assert result.unlinked == []
    assert result.reverse_deleted == []
    assert len(_links()) == 1


# ----------------------------------------------------------------------
# 写入稳定性判定
# ----------------------------------------------------------------------


def test_wait_stable_skips_observation_for_old_files(tmp_path, monkeypatch):
    """mtime 够老的文件直接放行，不进 sleep 观测。

    首轮全量登记的性能全靠这条快路径：几千个存量文件逐个 sleep 2s
    要跑几小时。
    """
    old = tmp_path / "old.mp4"
    old.write_bytes(b"A" * 64)
    # 把 mtime 推到快路径阈值之外
    stale = time.time() - watchdir.STABLE_MTIME_AGE - 10
    os.utime(old, (stale, stale))

    def _no_sleep(_seconds):
        raise AssertionError("mtime 够老的文件不该再 sleep 观测")

    monkeypatch.setattr(watchdir.time, "sleep", _no_sleep)

    assert watchdir._wait_stable(old) is True


def test_wait_stable_still_observes_fresh_files(tmp_path, monkeypatch):
    """刚写过的文件仍要走观测 —— 它可能还在写。"""
    fresh = tmp_path / "fresh.mp4"
    fresh.write_bytes(b"A" * 64)

    slept = []
    monkeypatch.setattr(watchdir.time, "sleep", lambda s: slept.append(s))

    assert watchdir._wait_stable(fresh) is True
    assert slept, "新文件应当至少观测一轮"


def test_wait_stable_rejects_growing_file(tmp_path, monkeypatch):
    """持续变大的文件判为未稳定，本轮跳过。"""
    growing = tmp_path / "growing.mp4"
    growing.write_bytes(b"A" * 64)

    # 每次「睡醒」都让文件长大一点，模拟下载中
    def _grow(_seconds):
        with growing.open("ab") as fh:
            fh.write(b"B" * 64)

    monkeypatch.setattr(watchdir.time, "sleep", _grow)
    monkeypatch.setattr(watchdir, "STABLE_CHECK_ROUNDS", 3)

    assert watchdir._wait_stable(growing) is False
