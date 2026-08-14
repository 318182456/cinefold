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
from app.database.models import (
    CodeAlias, History, MediaLink, PendingDelete, WatchDir,
)
from app.database.session import session_scope
from app.modules import watcher
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
            for model in (PendingDelete, MediaLink, CodeAlias, WatchDir, History):
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


def test_concurrent_sync_of_same_rule_runs_once(rule):
    """同一规则并发触发时只跑一轮，后来者跳过。

    定时对账、watchdog 事件、页面手动触发三条路径互相独立，随时可能撞在
    一起。没有互斥时两轮会同时改同一份 media_link 与磁盘状态。
    """
    import threading

    rule_id, source_dir, _ = rule
    (source_dir / "a.mp4").write_bytes(b"A" * 64)

    entered = threading.Event()
    release = threading.Event()
    calls = []
    original = watchdir._sync_rule_locked

    def blocking(rid, dry_run=False):
        calls.append(rid)
        entered.set()
        release.wait(timeout=5)
        return original(rid, dry_run=dry_run)

    watchdir._sync_rule_locked = blocking
    try:
        first = threading.Thread(target=lambda: watchdir.sync_rule(rule_id))
        first.start()
        assert entered.wait(timeout=5), "第一轮没能进入"

        # 第一轮还卡在里面，此时的第二次触发应当直接跳过
        second = watchdir.sync_rule(rule_id)

        release.set()
        first.join(timeout=5)
    finally:
        watchdir._sync_rule_locked = original

    assert len(calls) == 1
    assert any("上一轮对账尚未结束" in s for s in second.skipped)


def test_dry_run_sync_does_not_take_the_lock(rule):
    """演练不占锁：它什么都不改，且用户点演练时后台常在跑定时对账。"""
    rule_id, source_dir, _ = rule
    (source_dir / "a.mp4").write_bytes(b"A" * 64)

    lock = watchdir._rule_lock(rule_id)
    lock.acquire()
    try:
        result = watchdir.sync_rule(rule_id, dry_run=True)
    finally:
        lock.release()

    assert not any("上一轮对账尚未结束" in s for s in result.skipped)
    assert len(result.linked) == 1


def test_different_rules_sync_concurrently(rule, tmp_path):
    """锁是按规则分的，不同规则之间不该互相阻塞。"""
    rule_id, _, library = rule

    other_source = tmp_path / "src2"
    other_source.mkdir()
    with session_scope() as session:
        session.add(WatchDir(
            source_dir=str(other_source), target_dir=str(library / "sv2"),
            name="second", enabled=True, recursive=True,
            reverse_delete=False, code_prefix="SV2",
        ))
    with session_scope() as session:
        other_id = session.scalar(
            sa.select(WatchDir.id).where(WatchDir.name == "second")
        )

    lock = watchdir._rule_lock(rule_id)
    lock.acquire()
    try:
        # 第一条规则的锁被占着，第二条仍应正常跑完
        (other_source / "b.mp4").write_bytes(b"B" * 64)
        result = watchdir.sync_rule(other_id)
    finally:
        lock.release()

    assert not any("上一轮对账尚未结束" in s for s in result.skipped)
    assert len(result.linked) == 1


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


def test_hold_cleared_when_reverse_delete_disabled(rule, configure):
    """关掉反向删除后，之前攒下的扣留要清掉，不能永久挂在表里。

    实测踩到的场景：开着反向删除时文件消失、登记了扣留，之后把开关关掉。
    第 3 步的 continue 不走删除路径，_prune_holds 又只清「文件恢复了」和
    「记录没了」两种 —— 这条哪种都不是，于是永久留在 pending_delete 里，
    页面上显示「正在观察、即将删除」，实际永远不删。
    """
    rule_id, source_dir, library = rule
    configure(watchdir_delete_grace=3600)
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"

    # 媒体库侧被删 —— 走反向删除路径，宽限期内先扣留
    link.unlink()
    watchdir.sync_rule(rule_id, dry_run=False)
    assert len(watchdir.list_holds(rule_id)) == 1

    with session_scope() as session:
        session.get(WatchDir, rule_id).reverse_delete = False
    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert watchdir.list_holds(rule_id) == []
    assert result.reverse_deleted == []
    assert a.exists()  # 源文件不能被删
    assert any("未开启反向删除" in s for s in result.skipped)


def test_dry_run_keeps_hold_when_reverse_delete_disabled(rule, configure):
    """演练不许改库 —— 扣留记录得留在原处。"""
    rule_id, source_dir, library = rule
    configure(watchdir_delete_grace=3600)
    a = source_dir / "a.mp4"
    a.write_bytes(b"A")
    watchdir.sync_rule(rule_id, dry_run=False)
    link = library / "sv" / "a.mp4"

    link.unlink()
    watchdir.sync_rule(rule_id, dry_run=False)
    with session_scope() as session:
        session.get(WatchDir, rule_id).reverse_delete = False

    watchdir.sync_rule(rule_id, dry_run=True)

    assert len(watchdir.list_holds(rule_id)) == 1


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
        # paths 为 None 表示不过滤、返回全部文件的映射，与真实客户端一致
        wanted = None if paths is None else set(paths)
        out: dict[str, list[str]] = {}
        for h, files in self.torrents.items():
            for f in files:
                if wanted is None or f in wanted:
                    out.setdefault(f, []).append(h)
        return out

    def list_torrent_files(self, hashes):
        out: list[str] = []
        for h in hashes:
            out.extend(self.torrents.get(h, []))
        return out

    def monitor_torrent(self, hashes=None):
        """列出全部种子。adopt_scrape_dir 靠它拿源文件候选集。"""
        return [{"hash": h, "name": h, "save_path": ""} for h in self.torrents]

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


def test_collection_torrent_with_many_files_registers_once(rule, fake_downloader):
    """一个种子含多个视频（合集）时，同批事务里只能写一条 History。

    否则每个文件都 add 一次同一个 hash，commit 撞主键，整批登记全丢。
    """
    rule_id, source_dir, library = rule
    files = []
    for i in range(5):
        f = source_dir / f"clip{i}.mp4"
        f.write_bytes(b"C" * 64)
        files.append(f)
    # 五个文件同属一个种子
    fake_downloader.torrents["HASH_PACK"] = [str(f) for f in files]

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    assert len(result.linked) == 5
    with session_scope() as session:
        rows = session.scalars(sa.select(History)).all()
        assert [r.hash for r in rows] == ["HASH_PACK"]


def test_collection_torrent_spanning_batches(rule, fake_downloader, monkeypatch):
    """合集的文件跨过提交批次边界时也不能撞主键。

    批次一满就提交换新 session，后一批的 session 是全新的，靠
    「查 session 里攒了什么」那种去重完全失效。
    """
    rule_id, source_dir, _ = rule
    monkeypatch.setattr(watchdir, "REGISTER_BATCH_SIZE", 2)
    files = []
    for i in range(7):
        f = source_dir / f"span{i}.mp4"
        f.write_bytes(b"S" * 64)
        files.append(f)
    fake_downloader.torrents["HASH_SPAN"] = [str(f) for f in files]

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    assert len(result.linked) == 7
    with session_scope() as session:
        rows = session.scalars(sa.select(History)).all()
        assert [r.hash for r in rows] == ["HASH_SPAN"]


def test_backfill_collection_torrent_registers_once(rule, fake_downloader):
    """补查路径同理：一个种子对应多条 media_link，也只写一条 History。"""
    rule_id, source_dir, _ = rule
    files = []
    for i in range(4):
        f = source_dir / f"late{i}.mp4"
        f.write_bytes(b"L" * 64)
        files.append(f)

    # 建链接时下载器是空的
    watchdir.sync_rule(rule_id, dry_run=False)

    fake_downloader.torrents["HASH_LATEPACK"] = [str(f) for f in files]
    added = watchdir.backfill_torrents()

    assert added == 1
    with session_scope() as session:
        rows = session.scalars(sa.select(History)).all()
        assert [r.hash for r in rows] == ["HASH_LATEPACK"]


def test_backfill_skips_already_recorded(rule, fake_downloader):
    rule_id, source_dir, _ = rule
    a = source_dir / "clip.mp4"
    a.write_bytes(b"V" * 64)
    fake_downloader.torrents["HASH_A"] = [str(a)]
    watchdir.sync_rule(rule_id, dry_run=False)

    # 建链接时已登记，补查不该重复写
    assert watchdir.backfill_torrents() == 0


def _probe_state(link_suffix: str) -> tuple[int, object]:
    """取某条 media_link 的反查计数与时间戳。"""
    with session_scope() as session:
        row = next(
            r for r in session.scalars(sa.select(MediaLink)).all()
            if r.link_path.endswith(link_suffix)
        )
        return row.torrent_miss, row.torrent_probe_time


def test_backfill_counts_misses_and_then_throttles(rule, fake_downloader, monkeypatch):
    """永久查不到的关联要在攒够次数后停止每轮重查。

    手工拷进来的文件、种子早被删掉的文件都属于这类。不降频的话每轮对账都要
    为它们向下载器拉一次全量种子列表，几千条就能把下载器刷到超时。
    """
    rule_id, source_dir, _ = rule
    a = source_dir / "manual.mp4"
    a.write_bytes(b"M" * 64)
    # 下载器里始终没有这个文件对应的种子
    watchdir.sync_rule(rule_id, dry_run=False)

    calls = []
    original = fake_downloader.find_torrents_by_path
    monkeypatch.setattr(
        fake_downloader, "find_torrents_by_path",
        lambda paths: calls.append(list(paths)) or original(paths),
    )

    settings = watchdir.get_settings()
    monkeypatch.setattr(settings, "watchdir_torrent_miss_limit", 3, raising=False)
    monkeypatch.setattr(settings, "watchdir_torrent_retry_hours", 24, raising=False)

    # 前三轮照常查，计数逐轮累加
    for expected in (1, 2, 3):
        assert watchdir.backfill_torrents() == 0
        assert _probe_state("manual.mp4")[0] == expected
    assert len(calls) == 3

    # 第四轮起进入降频期，不再打下载器
    assert watchdir.backfill_torrents() == 0
    assert len(calls) == 3, "已达上限仍在每轮反查，降频没起作用"
    assert _probe_state("manual.mp4")[0] == 3


def test_backfill_retries_after_throttle_window(rule, fake_downloader, monkeypatch):
    """降频不是永久放弃：过了重试间隔还要再查一次（事后做种的情形）。"""
    rule_id, source_dir, _ = rule
    a = source_dir / "later.mp4"
    a.write_bytes(b"L" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    settings = watchdir.get_settings()
    monkeypatch.setattr(settings, "watchdir_torrent_miss_limit", 1, raising=False)
    monkeypatch.setattr(settings, "watchdir_torrent_retry_hours", 24, raising=False)

    assert watchdir.backfill_torrents() == 0
    assert _probe_state("later.mp4")[0] == 1
    # 立刻再来一轮：在降频期内，不查
    assert watchdir.backfill_torrents() == 0

    # 把时间戳往前推超过重试间隔，同时让种子出现
    with session_scope() as session:
        for row in session.scalars(sa.select(MediaLink)).all():
            row.torrent_probe_time = datetime.now() - timedelta(hours=25)
    fake_downloader.torrents["HASH_LATER"] = [str(a)]

    assert watchdir.backfill_torrents() == 1
    # 查到了要清零，否则下次又背着旧计数直接进降频期
    assert _probe_state("later.mp4")[0] == 0


def test_backfill_force_ignores_throttle(rule, fake_downloader, monkeypatch):
    """手动触发要无视降频 —— 用户点按钮就是要立刻查一次。"""
    rule_id, source_dir, _ = rule
    a = source_dir / "forced.mp4"
    a.write_bytes(b"F" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    settings = watchdir.get_settings()
    monkeypatch.setattr(settings, "watchdir_torrent_miss_limit", 1, raising=False)
    monkeypatch.setattr(settings, "watchdir_torrent_retry_hours", 0, raising=False)

    assert watchdir.backfill_torrents() == 0
    # retry_hours=0 表示到上限后彻底不再查，定时那条路应当跳过
    assert watchdir.backfill_torrents() == 0

    fake_downloader.torrents["HASH_FORCED"] = [str(a)]
    assert watchdir.backfill_torrents() == 0, "定时路径不该无视降频"
    assert watchdir.backfill_torrents(force=True) == 1


def test_backfill_downloader_outage_does_not_count_as_miss(
    rule, fake_downloader, monkeypatch
):
    """下载器整体不可达不算「这个文件查不到种子」。

    否则 qb 挂一阵子就把所有待补关联推进降频期，等它恢复了反而不查了。
    """
    rule_id, source_dir, _ = rule
    a = source_dir / "outage.mp4"
    a.write_bytes(b"O" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    def boom(paths):
        raise RuntimeError("Read timed out")

    monkeypatch.setattr(fake_downloader, "find_torrents_by_path", boom)
    assert watchdir.backfill_torrents() == 0

    miss, probed = _probe_state("outage.mp4")
    assert miss == 0, "下载器故障被计入了失败次数"
    assert probed is None


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


def test_scrape_dir_counts_files_and_unregistered(client, tmp_path, configure):
    """刮削输出目录实时统计影片数与已登记数。

    差额就是刮削工具建好但没走 webhook 的既存文件 —— 它们不在删除联动的
    管辖范围内，页面据此提示用户。
    """
    from app.database.models import MediaLink
    from app.database.session import session_scope

    scrape = tmp_path / "h_video" / "日本AV"
    (scrape / "ABC-123").mkdir(parents=True)
    (scrape / "ABC-123" / "ABC-123.mp4").write_text("x")
    (scrape / "ABC-123" / "ABC-123.nfo").write_text("x")  # 非视频，不计入
    (scrape / "DEF-456").mkdir()
    (scrape / "DEF-456" / "DEF-456.mkv").write_text("x")

    with session_scope() as session:
        session.add(MediaLink(
            link_path=str(scrape / "ABC-123" / "ABC-123.mp4"),
            code="ABC-123", source_path=str(tmp_path / "dl" / "ABC-123.mp4"),
        ))

    configure(
        medialink_library_path=str(tmp_path / "h_video"),
        medialink_scrape_dir=str(scrape),
    )
    item = client.get("/api/v1/watchdirs").json()["data"]["items"][0]

    assert item["file_count"] == 2
    assert item["registered_count"] == 1


def test_scrape_dir_counts_zero_when_missing(client, tmp_path, configure):
    """目录不存在时统计不报错，返回 0。"""
    configure(
        medialink_library_path=str(tmp_path),
        medialink_scrape_dir=str(tmp_path / "nope"),
    )
    item = client.get("/api/v1/watchdirs").json()["data"]["items"][0]
    assert item["file_count"] == 0
    assert item["registered_count"] == 0


def test_adopt_scrape_dir_matches_by_inode(tmp_path, configure, fake_downloader):
    """刮削目录里的既存链接按 inode 配回下载器的源文件，并登记种子。"""
    from app.database.models import History, MediaLink

    dl = tmp_path / "downloads" / "ofku-232"
    dl.mkdir(parents=True)
    source = dl / "ofku-232.mp4"
    source.write_bytes(b"V" * 64)

    # 刮削工具建的链接：路径与命名跟源文件完全不同，只有 inode 相同
    scrape = tmp_path / "h_video" / "日本AV" / "一条美绪"
    scrape.mkdir(parents=True)
    link = scrape / "OFKU-232-有码.mp4"
    os.link(source, link)

    fake_downloader.torrents["HASH_X"] = [str(source)]
    configure(
        medialink_library_path=str(tmp_path / "h_video"),
        medialink_scrape_dir=str(tmp_path / "h_video" / "日本AV"),
    )

    # 先演练：不该写库
    preview = watchdir.adopt_scrape_dir(dry_run=True)
    assert preview["total"] == 1
    assert len(preview["adopted"]) == 1
    assert preview["adopted"][0]["code"] == "OFKU-232"
    assert preview["adopted"][0]["source_path"] == str(source)
    assert preview["adopted"][0]["torrents"] == ["HASH_X"]
    with session_scope() as session:
        assert session.scalars(sa.select(MediaLink)).all() == []

    result = watchdir.adopt_scrape_dir(dry_run=False)
    assert len(result["adopted"]) == 1

    with session_scope() as session:
        record = session.get(MediaLink, str(link))
        assert record is not None
        assert record.code == "OFKU-232"
        assert record.source_path == str(source)
        # 种子一并落库，反向删除才有据可依
        hashes = [h for (h,) in session.execute(sa.select(History.hash)).all()]
        assert hashes == ["HASH_X"]


def test_adopt_scrape_dir_skips_already_registered(
    tmp_path, configure, fake_downloader
):
    """已登记的不重复处理 —— 否则每次点都把 source_path 覆盖一遍。"""
    dl = tmp_path / "downloads"
    dl.mkdir()
    source = dl / "abc-123.mp4"
    source.write_bytes(b"V")
    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    link = scrape / "ABC-123.mp4"
    os.link(source, link)

    fake_downloader.torrents["H1"] = [str(source)]
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )

    assert len(watchdir.adopt_scrape_dir(dry_run=False)["adopted"]) == 1
    # 第二次就没有待纳管的了
    again = watchdir.adopt_scrape_dir(dry_run=True)
    assert again["total"] == 0
    assert again["adopted"] == []


def test_adopt_scrape_dir_reports_unmatched(tmp_path, configure, fake_downloader):
    """源文件不在下载器里的配不上，如实报出来而不是静默跳过。"""
    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    (scrape / "XYZ-999.mp4").write_bytes(b"V")  # 孤立文件，没有源文件

    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )
    result = watchdir.adopt_scrape_dir(dry_run=True)

    assert result["total"] == 1
    assert result["adopted"] == []
    assert len(result["unmatched"]) == 1


def test_adopt_scrape_dir_skips_files_without_code(
    tmp_path, configure, fake_downloader
):
    """提不出番号的跳过 —— 硬造 code 会留下永远对不上号的脏记录。"""
    dl = tmp_path / "downloads"
    dl.mkdir()
    source = dl / "随手拍的视频.mp4"
    source.write_bytes(b"V")
    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    os.link(source, scrape / "随手拍的视频.mp4")

    fake_downloader.torrents["H1"] = [str(source)]
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )
    result = watchdir.adopt_scrape_dir(dry_run=True)

    assert result["adopted"] == []
    assert len(result["skipped"]) == 1
    assert "番号" in result["skipped"][0]["reason"]


def test_adopt_scrape_dir_without_config(tmp_path, configure):
    """没配刮削目录也没配库根时如实报错，不静默返回空。"""
    configure(medialink_library_path="", medialink_scrape_dir="")
    result = watchdir.adopt_scrape_dir(dry_run=True)
    assert result["errors"]


def test_adopt_scrape_dir_ignores_strm_and_trailers(
    tmp_path, configure, fake_downloader
):
    """.strm 与预告片不该进候选：它们永远配不上源文件，只会淹掉真实条目。"""
    dl = tmp_path / "downloads"
    dl.mkdir()
    source = dl / "ABC-123.mp4"
    source.write_bytes(b"V" * 64)

    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    os.link(source, scrape / "ABC-123.mp4")
    # 这三个都该被过滤掉
    (scrape / "SDMU-963-Trailer.strm").write_text("http://example.com/x")
    (scrape / "START-257-Trailer.mp4").write_bytes(b"T")
    (scrape / "DLDSS-385.strm").write_text("http://example.com/y")

    fake_downloader.torrents["H1"] = [str(source)]
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )
    result = watchdir.adopt_scrape_dir(dry_run=True)

    # 候选里只剩那一个真影片
    assert result["total"] == 1
    assert len(result["adopted"]) == 1
    assert result["adopted"][0]["code"] == "ABC-123"
    listed = str(result)
    assert "Trailer" not in listed
    assert ".strm" not in listed


def test_scrape_dir_count_matches_adopt_candidates(
    tmp_path, configure, fake_downloader, client
):
    """页面上的「N 个未登记」必须与纳管候选数一致。

    两者曾用不同判据：统计只按扩展名筛，纳管另外排除了 .strm 与预告片，
    于是页面说 256 个未登记、点进去纳管却只处理一部分，用户无从判断
    哪个数字是真的。
    """
    dl = tmp_path / "downloads"
    dl.mkdir()
    source = dl / "ABC-123.mp4"
    source.write_bytes(b"V" * 64)

    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    os.link(source, scrape / "ABC-123.mp4")
    (scrape / "DEF-456.mp4").write_bytes(b"X")          # 未登记的真影片
    (scrape / "SDMU-963-Trailer.strm").write_text("u")   # 两个都该被排除
    (scrape / "START-257-Trailer.mp4").write_bytes(b"T")

    fake_downloader.torrents["H1"] = [str(source)]
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )
    # ABC-123 已经登记过，剩 DEF-456 一个未登记
    with session_scope() as session:
        session.add(MediaLink(
            link_path=str(scrape / "ABC-123.mp4"),
            code="ABC-123", source_path=str(source),
        ))

    listed = client.get("/api/v1/watchdirs").json()["data"]
    card = next(i for i in listed["items"] if i["id"] == 0)
    shown = max(0, card["file_count"] - card["registered_count"])

    preview = watchdir.adopt_scrape_dir(dry_run=True)

    # 目录里 4 个视频文件，但只有 2 个算「影片」，其中 1 个已登记
    assert card["file_count"] == 2
    assert shown == 1
    assert preview["total"] == shown


def test_adopt_scrape_dir_accounts_for_every_candidate(
    tmp_path, configure, fake_downloader
):
    """total 必须等于各分类之和，不能有条目静默消失。"""
    dl = tmp_path / "downloads"
    dl.mkdir()
    matched = dl / "ABC-123.mp4"
    matched.write_bytes(b"V" * 64)

    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    os.link(matched, scrape / "ABC-123.mp4")       # 配得上
    (scrape / "DEF-456.mp4").write_bytes(b"X")      # 配不上源文件
    (scrape / "随手拍.mp4").write_bytes(b"Y")        # 提不出番号

    fake_downloader.torrents["H1"] = [str(matched)]
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )
    result = watchdir.adopt_scrape_dir(dry_run=True)

    counted = (
        len(result["adopted"]) + len(result["passthrough"])
        + len(result["unmatched"]) + len(result["skipped"])
    )
    assert result["total"] == 3
    assert counted == result["total"]


def test_adopt_scrape_dir_fallback_passthrough(
    tmp_path, configure, fake_downloader
):
    """配不到源文件的降级直通：source_path 就是自己，且没有种子。"""
    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    orphan = scrape / "DEF-456.mp4"
    orphan.write_bytes(b"X" * 64)

    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )

    # 默认不降级：进 unmatched
    plain = watchdir.adopt_scrape_dir(dry_run=True)
    assert len(plain["unmatched"]) == 1
    assert plain["passthrough"] == []

    # 开了降级：进 passthrough
    fb = watchdir.adopt_scrape_dir(dry_run=True, fallback_passthrough=True)
    assert fb["unmatched"] == []
    assert len(fb["passthrough"]) == 1
    row = fb["passthrough"][0]
    assert row["code"] == "DEF-456"
    assert row["source_path"] == row["link_path"] == str(orphan)
    assert row["torrents"] == []

    # 真落库后是一条直通记录
    watchdir.adopt_scrape_dir(dry_run=False, fallback_passthrough=True)
    with session_scope() as session:
        link = session.get(MediaLink, str(orphan))
        assert link is not None
        assert link.source_path == link.link_path
    # 直通登记不该凭空造出 History 行
    with session_scope() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(History)
        ) == 0


def test_adopt_scrape_endpoint_defaults_to_dry_run(
    client, tmp_path, configure, fake_downloader
):
    """接口默认演练 —— 登记是反向删除的依据，不能不问就写。"""
    from app.database.models import MediaLink

    dl = tmp_path / "downloads"
    dl.mkdir()
    source = dl / "def-456.mp4"
    source.write_bytes(b"V")
    scrape = tmp_path / "lib" / "AV"
    scrape.mkdir(parents=True)
    os.link(source, scrape / "DEF-456.mp4")

    fake_downloader.torrents["H2"] = [str(source)]
    configure(
        medialink_library_path=str(tmp_path / "lib"),
        medialink_scrape_dir=str(scrape),
    )

    body = client.post("/api/v1/watchdirs/adopt-scrape").json()
    assert body["code"] == 200
    assert len(body["data"]["adopted"]) == 1
    with session_scope() as session:
        assert session.scalars(sa.select(MediaLink)).all() == []

    body = client.post(
        "/api/v1/watchdirs/adopt-scrape", params={"dry_run": False}
    ).json()
    assert len(body["data"]["adopted"]) == 1
    with session_scope() as session:
        assert len(session.scalars(sa.select(MediaLink)).all()) == 1


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


# ----------------------------------------------------------------------
# 长文件名的 code：哈希 + 别名表
# ----------------------------------------------------------------------
# 日文长片名放不进 varchar(64) 的 code 列，PostgreSQL 会直接拒绝插入
# （StringDataRightTruncation），整轮同步中断。SQLite 不校验长度，
# 所以这些用例断言的是长度本身，而不是依赖数据库报错。
#
# 长度贴着阈值上下各取一个，覆盖边界：短的走原文，长的走哈希
LONG_STEM = (
    "【超高画質】《01本編》レベチ過ぎ！超～キレカワ激マブ美女！！"
    "華奢なのに出るとこ出たスペシャル美ボディ一女神！"
    "【完全版】高画質フルバージョン収録"
)


def _aliases() -> dict[str, str]:
    with session_scope() as session:
        return {
            r.code: r.filename
            for r in session.scalars(sa.select(CodeAlias)).all()
        }


def test_long_filename_code_fits_column():
    """超长片名的 code 必须落在 varchar(64) 之内。"""
    assert len(LONG_STEM) > watchdir.CODE_MAX_LENGTH

    code = watchdir.make_code(
        WatchDir(source_dir="/x", code_prefix="short"),
        Path(f"/x/{LONG_STEM}.mp4"),
    )

    assert len(code) <= watchdir.CODE_MAX_LENGTH
    # 前缀留在哈希外面，按规则筛选仍然可用
    assert code.startswith("short-")


def test_long_filenames_sharing_prefix_do_not_collide():
    """前缀相同的两个长片名不能撞成同一个 code。

    撞了会被当成同一部片子，删一部连带删掉另一部 —— 裸截断正是这个后果。
    """
    plain = WatchDir(source_dir="/x", code_prefix="")
    a = watchdir.make_code(plain, Path(f"/x/{LONG_STEM}-01.mp4"))
    b = watchdir.make_code(plain, Path(f"/x/{LONG_STEM}-02.mp4"))

    assert a != b


def test_code_is_stable_across_calls():
    """同一文件每轮对账都要算出同一个 code，否则会重复登记。"""
    rule = WatchDir(source_dir="/x", code_prefix="om")
    path = Path(f"/x/{LONG_STEM}.mp4")
    assert watchdir.make_code(rule, path) == watchdir.make_code(rule, path)


def test_code_fits_even_with_maximum_prefix():
    """code_prefix 列宽 32，配满也不能让 code 溢出 64。"""
    code = watchdir.make_code(
        WatchDir(source_dir="/x", code_prefix="p" * 32),
        Path(f"/x/{LONG_STEM}.mp4"),
    )
    assert len(code) <= watchdir.CODE_MAX_LENGTH


def test_sync_long_filename_records_alias(rule):
    """同步长片名文件时，别名表要记下 code → 原文件名。

    code 变成哈希后列表页认不出是哪部片子，这张表是唯一的找回途径。
    """
    rule_id, source_dir, library = rule
    (source_dir / f"{LONG_STEM}.mp4").write_bytes(b"A" * 64)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    code = next(iter(_links().values()))
    assert len(code) <= watchdir.CODE_MAX_LENGTH
    assert _aliases() == {code: LONG_STEM}


def test_sync_short_filename_records_no_alias(rule):
    """短文件名的 code 本身就是文件名，别名表不该留垃圾记录。"""
    rule_id, source_dir, library = rule
    (source_dir / "FC2-PPV-4482146.mp4").write_bytes(b"A" * 64)

    watchdir.sync_rule(rule_id, dry_run=False)

    assert _aliases() == {}


def test_same_alias_twice_in_one_batch_does_not_abort_sync(rule):
    """同一批事务里两个文件哈希出同一个 code，整批登记不能因此回滚。

    code 只由文件名（stem）算出，不含目录，所以不同子目录下的同名长片名
    文件必然同 code。「先查后插」堵不住这种情况：session.get() 查不到
    自己刚 add 还没 flush 的对象，两条都进 INSERT，commit 时撞
    code_alias_pkey，同批里其余文件的登记跟着一起没了。
    """
    rule_id, source_dir, library = rule
    for sub in ("a", "b"):
        d = source_dir / sub
        d.mkdir()
        (d / f"{LONG_STEM}.mp4").write_bytes(b"A" * 64)
    # 同批里的无辜文件，前两个撞主键时它不该被连累
    (source_dir / "FC2-PPV-4482146.mp4").write_bytes(b"B" * 64)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert result.errors == []
    assert len(_aliases()) == 1
    # 三个文件都要登记成功，而不是整批回滚
    assert len(_links()) == 3


def test_alias_is_not_duplicated_across_syncs(rule):
    """重复对账不该反复插同一条别名。"""
    rule_id, source_dir, library = rule
    (source_dir / f"{LONG_STEM}.mp4").write_bytes(b"A" * 64)

    watchdir.sync_rule(rule_id, dry_run=False)
    watchdir.sync_rule(rule_id, dry_run=False)

    assert len(_aliases()) == 1


def test_rename_does_not_record_bogus_alias(rule):
    """改名走移动判定，code 不变，也不该凭空记一条别名。

    改名后源文件名与 code 本就不一致（code 沿用第一次登记的那个），
    若拿「code 里含不含文件名」当判据，这里会误记一条 SV-before → after。
    """
    rule_id, source_dir, library = rule
    old = source_dir / "before.mp4"
    old.write_bytes(b"R" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)

    old.rename(source_dir / "after.mp4")
    watchdir.sync_rule(rule_id, dry_run=False)

    assert _aliases() == {}


def test_is_hashed_code_tells_the_two_forms_apart():
    plain = WatchDir(source_dir="/x", code_prefix="SV")
    short = watchdir.make_code(plain, Path("/x/FC2-PPV-4482146.mp4"))
    hashed = watchdir.make_code(plain, Path(f"/x/{LONG_STEM}.mp4"))

    assert watchdir.is_hashed_code(hashed) is True
    assert watchdir.is_hashed_code(short) is False
    # 番号里带 h 的普通 code 不能被误判
    assert watchdir.is_hashed_code("SV-heyzo-1234") is False


# ----------------------------------------------------------------------
# 种子反查的调用次数
# ----------------------------------------------------------------------
# find_torrents_by_path 的成本与下载器里的种子总数成正比：它要把每个种子的
# 文件清单都拉一遍才能建索引。每个文件查一次的话，几千个文件就是几百万次
# HTTP 往返 —— 同步会慢到看着像卡死。这几个用例锁的是「整轮只查一次」。


@pytest.fixture
def count_torrent_lookups(monkeypatch):
    """记录 find_torrents_by_path 每次收到的路径批次。"""
    calls = []

    def _fake(paths):
        calls.append(list(paths))
        return {}

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "find_torrents_by_path", _fake)
    return calls


def test_sync_queries_downloader_once_per_round(rule, count_torrent_lookups):
    """一轮同步只反查一次，不管有多少个文件。"""
    rule_id, source_dir, library = rule
    for i in range(12):
        (source_dir / f"clip{i}.mp4").write_bytes(b"A" * 64)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.linked) == 12
    assert len(count_torrent_lookups) == 1
    # 一次调用里带上全部待登记文件
    assert len(count_torrent_lookups[0]) == 12


def test_dry_run_does_not_query_downloader(rule, count_torrent_lookups):
    """演练不落库，也就不必问下载器。"""
    rule_id, source_dir, library = rule
    (source_dir / "clip.mp4").write_bytes(b"A" * 64)

    watchdir.sync_rule(rule_id, dry_run=True)

    assert count_torrent_lookups == []


def test_idempotent_sync_skips_downloader(rule, count_torrent_lookups):
    """没有新文件时不该再问下载器 —— 定时对账每轮都跑，白查很贵。"""
    rule_id, source_dir, library = rule
    (source_dir / "clip.mp4").write_bytes(b"A" * 64)
    watchdir.sync_rule(rule_id, dry_run=False)
    count_torrent_lookups.clear()

    watchdir.sync_rule(rule_id, dry_run=False)

    assert count_torrent_lookups == []


def test_claiming_queries_downloader_once(rule, count_torrent_lookups):
    """认领路径同样只查一次 —— 首次接管刮削库时会认领大量文件。"""
    rule_id, source_dir, library = rule
    target = library / "sv"
    target.mkdir(parents=True)
    for i in range(8):
        source = source_dir / f"claim{i}.mp4"
        source.write_bytes(b"C" * 64)
        # 刮削工具按自己的规则建的链接，路径与我们算出的对不上
        os.link(source, target / f"scraped-{i}.mp4")

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.claimed) == 8
    assert len(count_torrent_lookups) == 1
    assert len(count_torrent_lookups[0]) == 8


def test_recorded_torrents_come_from_batch(rule, monkeypatch):
    """批量查回来的种子要正确落到各自的 History 记录上。

    共用一份映射最容易错的地方就是串行 —— 甲的种子记到乙名下。
    """
    rule_id, source_dir, library = rule
    a = source_dir / "a.mp4"
    b = source_dir / "b.mp4"
    a.write_bytes(b"A" * 64)
    b.write_bytes(b"B" * 64)

    import app.modules.downloadclient as dc
    monkeypatch.setattr(dc, "find_torrents_by_path", lambda paths: {
        str(a): ["hash-a"],
        str(b): ["hash-b1", "hash-b2"],
    })

    watchdir.sync_rule(rule_id, dry_run=False)

    with session_scope() as session:
        recorded = {
            r.hash: (r.code, r.save_path)
            for r in session.scalars(sa.select(History)).all()
        }

    assert recorded["hash-a"] == ("SV-a", str(a))
    assert recorded["hash-b1"] == ("SV-b", str(b))
    assert recorded["hash-b2"] == ("SV-b", str(b))


# ----------------------------------------------------------------------
# 批量等待与批量提交
# ----------------------------------------------------------------------


def test_fresh_files_wait_once_not_per_file(rule, monkeypatch):
    """一批新文件只等一轮，不是每个文件各等一轮。

    逐个 sleep 是串行的：30 个新文件就要等一分钟，而这一分钟里它们本来
    也都在同时写入。等一次就够。
    """
    rule_id, source_dir, library = rule
    for i in range(20):
        (source_dir / f"fresh{i}.mp4").write_bytes(b"A" * 64)

    sleeps = []
    monkeypatch.setattr(watchdir.time, "sleep", lambda s: sleeps.append(s))
    # 让文件看起来是刚写完的，强制走等待路径
    monkeypatch.setattr(watchdir, "STABLE_MTIME_AGE", 10_000.0)
    monkeypatch.setattr(watchdir, "STABLE_CHECK_ROUNDS", 3)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.linked) == 20
    # 20 个文件若逐个等，至少 20 次 sleep；批量等待只需轮数级别
    assert len(sleeps) <= watchdir.STABLE_CHECK_ROUNDS


def test_growing_file_is_still_skipped_in_batch(rule, monkeypatch):
    """批量等待不能放过仍在写入的文件 —— 那会让 Emby 扫进残缺文件。"""
    rule_id, source_dir, library = rule
    stable = source_dir / "stable.mp4"
    growing = source_dir / "growing.mp4"
    stable.write_bytes(b"A" * 64)
    growing.write_bytes(b"B" * 64)

    monkeypatch.setattr(watchdir, "STABLE_MTIME_AGE", 10_000.0)

    # 每次「睡醒」都让 growing 长大，stable 不动
    def _grow(_seconds):
        with growing.open("ab") as fh:
            fh.write(b"B" * 64)

    monkeypatch.setattr(watchdir.time, "sleep", _grow)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    linked = {Path(p).name for p in result.linked}
    assert linked == {"stable.mp4"}
    assert any("growing.mp4" in s for s in result.skipped)


def test_registers_commit_in_batches(rule, monkeypatch):
    """登记按批提交，不是每条一个事务。

    SQLite 上一次 commit 约 18ms，几千个文件逐条提交光提交就要好几分钟。
    """
    rule_id, source_dir, library = rule
    for i in range(10):
        (source_dir / f"clip{i}.mp4").write_bytes(b"A" * 64)

    monkeypatch.setattr(watchdir, "REGISTER_BATCH_SIZE", 4)

    commits = []
    original = watchdir.SessionLocal

    class _CountingSession:
        def __init__(self):
            self._inner = original()

        def commit(self):
            commits.append(1)
            return self._inner.commit()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(watchdir, "SessionLocal", _CountingSession)

    result = watchdir.sync_rule(rule_id, dry_run=False)

    assert len(result.linked) == 10
    # 10 条按 4 一批：3 次提交（4 + 4 + 2），远少于逐条的 10 次
    assert len(commits) == 3
    # 记录必须全部落库 —— 攒批不能把最后不满一批的那些丢掉
    assert len(_links()) == 10


def test_batch_flushes_remainder_on_early_exit(rule, monkeypatch):
    """循环中途抛异常时，已建好链接的那批必须提交。

    硬链接已经在磁盘上了，记录却没落库的话，下一轮对账会把它们当孤儿删掉。
    """
    rule_id, source_dir, library = rule
    for i in range(6):
        (source_dir / f"clip{i}.mp4").write_bytes(b"A" * 64)

    monkeypatch.setattr(watchdir, "REGISTER_BATCH_SIZE", 100)

    calls = {"n": 0}
    real_make_code = watchdir.make_code

    def _boom(rule_obj, source):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("模拟中途失败")
        return real_make_code(rule_obj, source)

    monkeypatch.setattr(watchdir, "make_code", _boom)

    with pytest.raises(RuntimeError):
        watchdir.sync_rule(rule_id, dry_run=False)

    # 崩之前登记成功的 3 条要在库里，不能整批丢掉
    assert len(_links()) == 3


# ----------------------------------------------------------------------
# 媒体库事件归属
# ----------------------------------------------------------------------
class TestLibraryEventOwners:
    """媒体库整个挂一个 handler，事件要按路径前缀归到具体规则上。

    以前是「任何事件把所有反向规则全标脏」——从 Emby 删一个 FC2 影片，
    FC2、欧美、短视频三条规则各全量扫一遍，删一个文件扫三次目录树。
    """

    @pytest.fixture
    def handler_cls(self):
        cls = watcher._build_handler()
        if cls is None:
            pytest.skip("未安装 watchdog")
        return cls

    def _bases(self, library: Path, names: dict[int, str]) -> dict[int, Path]:
        for name in names.values():
            (library / name).mkdir(parents=True, exist_ok=True)
        return {rid: (library / name).resolve() for rid, name in names.items()}

    def test_event_only_touches_owning_rule(self, handler_cls, tmp_path):
        library = tmp_path / "lib"
        bases = self._bases(library, {1: "FC2", 2: "ome", 3: "short"})
        handler = handler_cls([1, 2, 3], "媒体库", bases)

        assert handler._owners(str(library / "FC2" / "FC2PPV-1570936.mp4")) == [1]
        assert handler._owners(str(library / "ome" / "a.mp4")) == [2]
        # 子目录里的文件也要能归属
        assert handler._owners(str(library / "short" / "2026" / "y.mp4")) == [3]

    def test_path_outside_every_rule_is_dropped(self, handler_cls, tmp_path):
        """媒体库里还有别的内容，那些事件不该惊动任何规则。"""
        library = tmp_path / "lib"
        bases = self._bases(library, {1: "FC2"})
        (library / "other").mkdir(parents=True, exist_ok=True)
        handler = handler_cls([1], "媒体库", bases)

        assert handler._owners(str(library / "other" / "z.mp4")) == []

    def test_deleted_path_still_attributes(self, handler_cls, tmp_path):
        """反向删除靠的就是删除事件，路径此刻已经不存在，仍要能归属。"""
        library = tmp_path / "lib"
        bases = self._bases(library, {1: "FC2"})
        handler = handler_cls([1], "媒体库", bases)

        assert handler._owners(str(library / "FC2" / "gone.mp4")) == [1]

    def test_nested_bases_touch_both(self, handler_cls, tmp_path):
        """目标目录嵌套时（A=/lib、B=/lib/FC2），命中的规则都要标脏。"""
        library = tmp_path / "lib"
        library.mkdir(parents=True, exist_ok=True)
        (library / "FC2").mkdir(exist_ok=True)
        bases = {9: library.resolve(), 1: (library / "FC2").resolve()}
        handler = handler_cls([9, 1], "媒体库", bases)

        assert sorted(handler._owners(str(library / "FC2" / "a.mp4"))) == [1, 9]

    def test_without_bases_falls_back_to_all(self, handler_cls, tmp_path):
        """算不出目标目录时退回旧行为，宁可多扫不可漏扫。"""
        library = tmp_path / "lib"
        library.mkdir(parents=True, exist_ok=True)
        handler = handler_cls([1, 2, 3], "媒体库", None)

        assert handler._owners(str(library / "whatever.mp4")) == [1, 2, 3]

    def test_source_handler_unaffected(self, handler_cls, tmp_path):
        """源目录 handler 一个规则一个，不传 bases，行为不能变。"""
        handler = handler_cls([7], "源[FC2]")
        assert handler._owners(str(tmp_path / "anything.mp4")) == [7]
