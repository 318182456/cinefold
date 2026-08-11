"""监控目录同步：源目录与媒体库之间的硬链接双向同步。

    源目录                              媒体库
    /downloads/短视频/2026/a.mp4  ──硬链接──>  /media/短视频/2026/a.mp4
                                              （media_link 记一条，code="a"）

正向（源侧变化 → 媒体库）：
    新增文件 → 建硬链接 + 登记 media_link
    删除文件 → 删硬链接 + 清 media_link，不碰源侧（文件已经没了）

反向（媒体库侧变化 → 源侧）：
    删除文件 → 走 handle_media_deleted()，删种 + 删源文件 + 清记录
    受两道开关约束：规则的 reverse_delete 与全局 MEDIALINK_DELETE_ENABLED

反向删除为什么复用 handle_media_deleted 而不自己删：那个函数已经处理了
按 code 查全部种子 hash（含转种的多条）、向下载器要种子内文件清单、
先停种再删文件的顺序。重写一遍只会漏掉这些边界。

同步靠「目录快照 vs media_link 表」对账，而不是靠 watchdog 事件本身推进
状态。事件只是「该对账了」的触发信号 —— Docker 绑定挂载、NFS/SMB 上事件
会丢，容器重启期间的变化也没人看见，只有全量对账能收敛。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import History, MediaLink, PendingDelete, WatchDir
from app.database.session import session_scope
from app.services.medialink import VIDEO_SUFFIXES, handle_media_deleted

# 文件刚出现时可能还在写入（下载中、拷贝中）。连续两次 stat 大小不变才认为
# 写完了，否则硬链接会指向一个残缺文件，Emby 扫进去是坏的
STABLE_CHECK_INTERVAL = 2.0
STABLE_CHECK_ROUNDS = 3

# 正在下载的临时文件，不该建链接
IGNORED_SUFFIXES = {".part", ".!qb", ".tmp", ".temp", ".downloading", ".aria2"}

# 隐藏目录与系统目录，扫描时跳过
IGNORED_DIR_NAMES = {".@__thumb", "@eaDir", ".DS_Store", "$RECYCLE.BIN",
                     "System Volume Information", ".git"}


@dataclass
class SyncResult:
    """一次同步的结果，用于日志与接口回执。"""
    watch_id: int = 0
    source_dir: str = ""
    dry_run: bool = False
    linked: list[str] = field(default_factory=list)
    unlinked: list[str] = field(default_factory=list)
    # 判定为移动/改名的，只改了记录路径，没动文件。[{from, to}]
    moved: list[dict] = field(default_factory=list)
    # 扣留观察中的（消失了但宽限期未满）。[{link_path, source_path, waited_seconds}]
    held: list[dict] = field(default_factory=list)
    # 反向删除（媒体库侧消失 → 删源文件与种子）的结果摘要
    reverse_deleted: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "watch_id": self.watch_id,
            "source_dir": self.source_dir,
            "dry_run": self.dry_run,
            "linked": self.linked,
            "unlinked": self.unlinked,
            "moved": self.moved,
            "held": self.held,
            "reverse_deleted": self.reverse_deleted,
            "skipped": self.skipped,
            "errors": self.errors,
        }


# ----------------------------------------------------------------------
# 路径与命名
# ----------------------------------------------------------------------
def _is_video(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in IGNORED_SUFFIXES:
        return False
    return suffix in VIDEO_SUFFIXES


def _scan_videos(root: Path, recursive: bool) -> list[Path]:
    """列出目录下的视频文件。跳过隐藏目录与临时文件。"""
    found: list[Path] = []
    if not root.is_dir():
        return found

    if not recursive:
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            logger.warning(f"无法列出目录 {root}: {exc}")
            return found
        for entry in entries:
            try:
                if entry.is_file() and _is_video(entry):
                    found.append(entry)
            except OSError:
                continue
        return found

    for dirpath, dirnames, filenames in os.walk(root):
        # 原地改 dirnames 才能阻止 os.walk 往下走
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIR_NAMES and not d.startswith(".")
        ]
        for name in filenames:
            entry = Path(dirpath) / name
            if _is_video(entry):
                found.append(entry)
    return found


def _library_root() -> Path | None:
    """媒体库根目录。未配置或不存在时返回 None。"""
    library = get_settings().medialink_library_path
    if not library:
        return None
    try:
        root = Path(library).resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def target_base(rule: WatchDir) -> Path | None:
    """算出这条规则的目标根目录。取不到返回 None。

    两种配置方式，target_dir 优先：

      target_dir     直接给绝对路径，与 MEDIALINK_LIBRARY_PATH 无关。
                     短视频、电影各有独立媒体库时用这个
      target_subdir  旧字段，相对 MEDIALINK_LIBRARY_PATH 的子目录名。
                     存量规则走这条，保证升级后不失效

    目标目录不要求此刻就存在 —— 建链接时会自动 mkdir。但父目录得能建出来，
    这个交给 os.link 报错，不在这里预判。
    """
    raw = (rule.target_dir or "").strip()
    if raw:
        try:
            return Path(raw)
        except (OSError, ValueError):
            return None

    library = _library_root()
    if library is None:
        return None
    return library / rule.target_subdir if rule.target_subdir else library


def target_path(
    rule: WatchDir, source: Path, base: Path | None = None
) -> Path | None:
    """算出源文件在目标目录里的路径，保持源目录内的相对结构。

        源目录        /downloads/短视频
        目标目录      /volume3/h_video/短视频
        源文件        /downloads/短视频/2026/a.mp4
        目标          /volume3/h_video/短视频/2026/a.mp4

    源文件不在源目录内时返回 None —— 说明调用方传错了，宁可不建。
    base 省略时按规则自行解析；批量调用时传进来可省掉重复解析。
    """
    root = base if base is not None else target_base(rule)
    if root is None:
        return None

    try:
        relative = source.resolve().relative_to(Path(rule.source_dir).resolve())
    except (ValueError, OSError):
        return None

    return root / relative


def make_code(rule: WatchDir, source: Path) -> str:
    """生成 code。默认取文件名（不含扩展名），配了前缀则拼上。

    code 在 media_link 里是「同一部片子的多份文件 + 多个种子」的分组键，
    不要求是真番号。加前缀是为了与真番号隔离，方便列表页筛选。
    """
    stem = source.stem
    prefix = (rule.code_prefix or "").strip()
    return f"{prefix}-{stem}" if prefix else stem


def _wait_stable(path: Path) -> bool:
    """等文件写完。连续两次观测大小不变即认为稳定。

    下载中/拷贝中的文件建了硬链接，Emby 会扫进一个残缺文件。宁可这轮跳过，
    下一轮对账再补上。

    STABLE_CHECK_ROUNDS 是「最多再观测几次」，不含第一次读取 —— 早就写完的
    文件（对账时的绝大多数）第二次观测就能确认，不必把轮数耗光。
    """
    try:
        last = path.stat().st_size
    except OSError:
        return False

    for _ in range(max(1, STABLE_CHECK_ROUNDS)):
        time.sleep(STABLE_CHECK_INTERVAL)
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last:
            return True
        last = size

    # 轮数用完还在变，说明确实在持续写入
    return False


# ----------------------------------------------------------------------
# 建链接 / 删链接
# ----------------------------------------------------------------------
def _hardlink(source: Path, target: Path) -> tuple[bool, str]:
    """建硬链接。返回 (成功, 错误信息)。

    目标已存在且与源同 inode 时视为成功（幂等，重复同步不报错）。
    """
    try:
        if target.exists():
            try:
                s_st, t_st = source.stat(), target.stat()
                if (s_st.st_ino, s_st.st_dev) == (t_st.st_ino, t_st.st_dev):
                    return True, ""  # 已经是同一份数据
            except OSError:
                pass
            return False, f"目标已存在且非同一文件: {target}"

        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
        return True, ""
    except OSError as exc:
        # EXDEV：跨文件系统。硬链接的硬性限制，换不了别的做法
        if getattr(exc, "errno", None) == 18:
            return False, (
                f"源文件与媒体库不在同一文件系统，无法建硬链接: "
                f"{source} → {target}"
            )
        return False, f"建硬链接失败 {source} → {target}: {exc}"


def _unlink(path: Path) -> tuple[bool, str]:
    """删掉媒体库侧的硬链接。文件已不存在时视为成功。"""
    try:
        if not path.exists():
            return True, ""
        path.unlink()
        return True, ""
    except OSError as exc:
        return False, f"删除硬链接失败 {path}: {exc}"


# ----------------------------------------------------------------------
# 单个规则的同步
# ----------------------------------------------------------------------
def _existing_links(rule: WatchDir, base: Path) -> dict[str, MediaLink]:
    """取这条规则已登记的关联，按 link_path 索引。

    归属判定靠 link_path 落在规则的目标目录内 —— media_link 没存 watch_id，
    这样一条链接被哪条规则管，完全由路径决定，规则改了目标目录也不会认错。
    """
    prefix = str(base)
    out: dict[str, MediaLink] = {}
    with session_scope() as session:
        for row in session.scalars(select(MediaLink)).all():
            if row.link_path.startswith(prefix):
                # detach 后仍要读字段，先取成普通值
                out[row.link_path] = row
                session.expunge(row)
    return out


def sync_rule(rule_id: int, dry_run: bool = False) -> SyncResult:
    """对账一条监控规则：让媒体库与源目录一致。

    三类差异：
      1. 源有、库无        → 建硬链接 + 登记
      2. 源无、库有(有记录) → 源侧已删，删硬链接 + 清记录
      3. 库无、源有(有记录) → 媒体库侧被删，触发反向删除（删种 + 删源文件）

    第 2、3 类靠「记录在不在」区分：记录存在说明这条关联曾经建立过，
    现在某一侧文件消失了，才需要判断是哪一侧先没的。
    """
    result = SyncResult(watch_id=rule_id, dry_run=dry_run)

    with session_scope() as session:
        row = session.get(WatchDir, rule_id)
        if row is None:
            result.errors.append(f"监控规则 {rule_id} 不存在")
            return result
        # 出了 session 还要用，拷一份detach 的
        rule = WatchDir(
            id=row.id, source_dir=row.source_dir, target_dir=row.target_dir,
            target_subdir=row.target_subdir,
            name=row.name, enabled=row.enabled, recursive=row.recursive,
            reverse_delete=row.reverse_delete, code_prefix=row.code_prefix,
        )

    result.source_dir = rule.source_dir

    if not rule.enabled:
        result.skipped.append("规则已禁用")
        return result

    base = target_base(rule)
    if base is None:
        msg = (
            "未配置目标目录，也没有媒体库根目录（MEDIALINK_LIBRARY_PATH）可回退，"
            "无法同步"
        )
        logger.warning(msg)
        result.errors.append(msg)
        return result

    source_root = Path(rule.source_dir)
    if not source_root.is_dir():
        msg = f"源目录不存在或不可读: {rule.source_dir}"
        logger.warning(msg)
        result.errors.append(msg)
        _record_error(rule_id, msg)
        return result

    videos = _scan_videos(source_root, rule.recursive)
    linked_records = _existing_links(rule, base)

    # 源侧当前应该存在的目标路径 → 源文件
    expected: dict[str, Path] = {}
    for source in videos:
        target = target_path(rule, source, base)
        if target is None:
            result.skipped.append(f"不在源目录内，跳过: {source}")
            continue
        expected[str(target)] = source

    # (inode, device) → 当前源目录里的文件。移动判定要靠它：
    # 记录里的文件路径不见了，但同 inode 的文件还在目录里某处，
    # 那就是移动/改名，不是删除
    inode_index: dict[tuple[int, int], Path] = {}
    for source in videos:
        try:
            st = source.stat()
        except OSError:
            continue
        if st.st_ino:
            inode_index[(st.st_ino, st.st_dev)] = source

    # 「媒体库侧被删」必须在建链接之前判定：有记录、源文件还在、链接却没了，
    # 只可能是媒体库那侧被删掉的。等到建完链接再看就分辨不出来了 ——
    # 那时链接又存在，跟从没删过一样，反向删除永远不会触发
    media_deleted = {
        target_str for target_str in expected
        if target_str in linked_records and not Path(target_str).exists()
    }

    # --- 1) 源有、库无：建链接 ---
    for target_str, source in expected.items():
        target = Path(target_str)
        if target_str in linked_records and target.exists():
            continue  # 已建且记录在，无事可做

        # 媒体库侧删掉的不能重建 —— 那是用户在 Emby 里删的，重建等于抗命，
        # 而且下一轮又会被删，来回拉锯。交给第 3 步处理
        if target_str in media_deleted:
            continue

        if dry_run:
            result.linked.append(target_str)
            continue

        if not _wait_stable(source):
            result.skipped.append(f"文件仍在写入，本轮跳过: {source}")
            continue

        ok, err = _hardlink(source, target)
        if not ok:
            logger.error(err)
            result.errors.append(err)
            continue

        code = make_code(rule, source)
        _register(code, str(source), target_str)
        # 种子信息趁现在存下来 —— 删除时下载器里可能已经没有这个种子了
        _record_torrents(code, str(source))
        result.linked.append(target_str)
        logger.info(f"[{code}] 已建硬链接 {source} → {target}")

    # --- 2) 源无、库有：可能是移动，可能是真删除 ---
    for target_str, record in linked_records.items():
        if target_str in expected:
            continue

        source_gone = not Path(record.source_path).exists()
        if not source_gone:
            # 源文件还在，只是不在扫描结果里（改了后缀、移出了源目录）。
            # 不删 —— 这种情况删掉链接是在替用户做决定
            result.skipped.append(
                f"源文件仍存在但已不在监控范围，保留链接: {record.source_path}"
            )
            continue

        # 移动判定：同 inode 的文件还在目录里，说明只是换了位置。
        # 改记录 + 改链接路径，不删不建 —— inode 不变，Emby 侧无感知
        moved_to = _match_moved(record, inode_index, expected)
        if moved_to is not None:
            new_target = str(target_path(rule, moved_to, base) or "")
            if dry_run:
                result.moved.append({"from": target_str, "to": new_target})
                continue
            if _apply_move(record, moved_to, target_str, new_target, result):
                _clear_hold(target_str)
            continue

        # 不是移动。扣留观察，宽限期满才真删
        if dry_run:
            result.unlinked.append(target_str)
            continue

        ready, waited = _hold_or_ready(record, rule_id, "source")
        if not ready:
            result.held.append({
                "link_path": target_str,
                "source_path": record.source_path,
                "waited_seconds": waited,
            })
            continue

        ok, err = _unlink(Path(target_str))
        if not ok:
            logger.error(err)
            result.errors.append(err)
            continue
        _drop_record(target_str)
        _clear_hold(target_str)
        result.unlinked.append(target_str)
        logger.info(
            f"源文件已删除且宽限期已过（{waited}s），同步删除硬链接: {target_str}"
        )

    # --- 3) 库无、源有：媒体库侧被删，反向删除 ---
    # 用上面预先算好的集合，而不是再 stat 一次 —— 第 1 步已经改过磁盘状态
    for target_str in sorted(media_deleted):
        if not rule.reverse_delete:
            result.skipped.append(
                f"媒体库侧文件已删除，但该规则未开启反向删除，保留源文件: {target_str}"
            )
            continue

        record = linked_records[target_str]

        # 媒体库侧也可能是移动（用户在库里整理目录结构，或刮削工具重命名）。
        # 目标目录内同 inode 的文件还在就不算删除，改记录即可
        moved_to = _find_in_library(record, base)
        if moved_to is not None:
            if dry_run:
                result.moved.append({"from": target_str, "to": str(moved_to)})
                continue
            _register(record.code, record.source_path, str(moved_to))
            _drop_record(target_str)
            _clear_hold(target_str)
            result.moved.append({"from": target_str, "to": str(moved_to)})
            logger.info(f"媒体库内文件已移动，更新记录: {target_str} → {moved_to}")
            continue

        # 反向删除会删掉源文件和种子，不可恢复，同样要走宽限期
        if not dry_run:
            ready, waited = _hold_or_ready(record, rule_id, "library")
            if not ready:
                result.held.append({
                    "link_path": target_str,
                    "source_path": record.source_path,
                    "waited_seconds": waited,
                })
                continue

        # 复用 webhook 的同一条路径：删种 → 删源文件 → 清附属 → 清记录。
        # dry_run 透传，同时全局 MEDIALINK_DELETE_ENABLED 仍会在里面兜一次底
        outcome = handle_media_deleted(link_path=target_str, dry_run=dry_run)
        result.reverse_deleted.append({
            "link_path": target_str,
            "code": outcome.code,
            "torrents": outcome.torrents_deleted,
            "files": outcome.files_deleted,
            "errors": outcome.errors,
        })
        result.errors.extend(outcome.errors)
        if not dry_run:
            _clear_hold(target_str)
        logger.warning(
            f"[{outcome.code}] 媒体库侧删除触发反向清理 —— "
            f"种子 {len(outcome.torrents_deleted)}，源文件 {len(outcome.files_deleted)}"
        )

    if not dry_run:
        _record_scan(rule_id, len(videos), result.errors)
        # 这条规则下已经不成立的扣留（文件回来了、记录没了）一并清掉，
        # 否则表会越积越多，且下次可能按过期的信息误删
        _prune_holds(rule_id, linked_records.keys())

    logger.info(
        f"[监控目录 {rule.name or rule.source_dir}] 同步完成"
        f"{'（演练）' if dry_run else ''} —— "
        f"新建 {len(result.linked)}，删除 {len(result.unlinked)}，"
        f"移动 {len(result.moved)}，扣留 {len(result.held)}，"
        f"反向清理 {len(result.reverse_deleted)}，"
        f"跳过 {len(result.skipped)}，错误 {len(result.errors)}"
    )
    return result


# ----------------------------------------------------------------------
# 移动判定与延迟删除
# ----------------------------------------------------------------------
def _match_moved(
    record: MediaLink,
    inode_index: dict[tuple[int, int], Path],
    expected: dict[str, Path],
) -> Path | None:
    """记录对应的源文件是否只是移动了位置。返回新位置，不是移动则 None。

    判据是 inode：移动/改名不改 inode，这是文件系统的保证。内容被改写、
    文件名全变都不影响判定 —— 反过来说，如果用户删掉旧文件又拷了个同名新
    文件进来，inode 变了，那确实该算删除+新增，不是移动。

    额外要求新位置尚未被登记（不在 expected 的已建集合里），否则两条记录
    会指向同一个源文件。
    """
    if record.inode is None or record.device is None:
        return None  # 跨文件系统或 Windows 上取不到 inode，无法判定

    hit = inode_index.get((record.inode, record.device))
    if hit is None:
        return None
    # 新位置的目标路径必须还没有别的记录占着
    if str(hit) == record.source_path:
        return None  # 路径没变，不是移动
    return hit


def _find_in_library(record: MediaLink, base: Path) -> Path | None:
    """目标目录里是否还有同 inode 的文件（链接被移动而非删除）。

    只在链接路径已经不存在时调用。扫描范围限定目标目录内，靠 inode 认人。
    """
    if record.inode is None or record.device is None:
        return None

    try:
        for path in base.rglob("*"):
            try:
                if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            if (st.st_ino, st.st_dev) == (record.inode, record.device):
                return path
    except OSError as exc:
        logger.warning(f"扫描媒体库查找移动目标失败: {exc}")
    return None


def _apply_move(
    record: MediaLink, new_source: Path, old_target: str, new_target: str,
    result: SyncResult,
) -> bool:
    """落实一次移动：把链接搬到新位置，记录跟着改。

    硬链接本身不重建 —— 用 os.replace 把它挪过去，inode 全程不变，
    Emby 的观看记录、收藏都保得住。重建链接会换 inode，那些就全丢了。
    """
    if not new_target:
        result.errors.append(f"无法计算移动后的目标路径: {new_source}")
        return False

    old_path, new_path = Path(old_target), Path(new_target)
    try:
        if old_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            # replace 而非 rename：目标已存在时覆盖，避免残留旧链接
            os.replace(old_path, new_path)
        elif not new_path.exists():
            # 链接两边都没有了，退回重建一份
            ok, err = _hardlink(new_source, new_path)
            if not ok:
                result.errors.append(err)
                return False
    except OSError as exc:
        msg = f"移动硬链接失败 {old_path} → {new_path}: {exc}"
        logger.error(msg)
        result.errors.append(msg)
        return False

    _register(record.code, str(new_source), new_target)
    # 源文件换了位置，种子登记里的 save_path 已经过时，补一次。
    # 移动后 inode 不变，下载器仍认得这份数据
    _record_torrents(record.code, str(new_source))
    if new_target != old_target:
        _drop_record(old_target)
    result.moved.append({"from": old_target, "to": new_target})
    logger.info(
        f"[{record.code}] 检测到文件移动，已更新关联而非删除: "
        f"{record.source_path} → {new_source}"
    )
    return True


def _grace_seconds() -> int:
    """删除宽限期，秒。0 表示不延迟，发现即删。"""
    return max(0, get_settings().watchdir_delete_grace)


def _hold_or_ready(record: MediaLink, watch_id: int, side: str) -> tuple[bool, int]:
    """扣留判定。返回 (是否可以删了, 已等待秒数)。

    首次发现消失时写入扣留记录并返回 False；之后每轮对账检查一次，
    等够宽限期才返回 True。宽限期为 0 时直接放行。
    """
    from datetime import datetime

    grace = _grace_seconds()
    if grace == 0:
        return True, 0

    now = datetime.now()
    with session_scope() as session:
        row = session.get(PendingDelete, record.link_path)
        if row is None:
            session.add(PendingDelete(
                link_path=record.link_path,
                watch_id=watch_id,
                code=record.code,
                source_path=record.source_path,
                inode=record.inode,
                device=record.device,
                side=side,
                detected_time=now,
            ))
            logger.info(
                f"[{record.code}] 文件消失，扣留观察 {grace}s 后再决定是否删除: "
                f"{record.link_path}"
            )
            return False, 0

        waited = int((now - row.detected_time).total_seconds())
        if waited < grace:
            logger.debug(
                f"[{record.code}] 仍在宽限期内（{waited}/{grace}s）: {record.link_path}"
            )
            return False, waited
        return True, waited


def _clear_hold(link_path: str) -> None:
    """撤销扣留。文件回来了、或删除已执行完都要清掉。"""
    with session_scope() as session:
        row = session.get(PendingDelete, link_path)
        if row is not None:
            session.delete(row)


def _prune_holds(watch_id: int, live_links) -> None:
    """清掉不再成立的扣留记录。

    撤销条件按 side 分别判断 —— 扣留的是「哪一侧消失了」，就该看那一侧
    有没有回来：

      side=source   源文件消失。此时硬链接还在（我们故意没删），
                    所以要看 source_path，看 link_path 会永远判定成已恢复
      side=library  媒体库侧链接消失。看 link_path

    另外 media_link 记录已经不存在的扣留一并清掉 —— 关联都没了，扣留无意义。
    """
    live = set(live_links)
    with session_scope() as session:
        rows = list(session.scalars(
            select(PendingDelete).where(PendingDelete.watch_id == watch_id)
        ).all())
        for row in rows:
            if row.link_path not in live:
                # 关联记录已被清掉（删除执行完了，或被别处清理）
                session.delete(row)
                continue

            watched = row.source_path if row.side == "source" else row.link_path
            if watched and Path(watched).exists():
                logger.info(f"[{row.code}] 文件已恢复，撤销扣留: {watched}")
                session.delete(row)


def list_holds(watch_id: int = 0) -> list[dict]:
    """列出扣留中的删除，供页面展示「这些文件正在观察，还没删」。"""
    grace = _grace_seconds()
    from datetime import datetime

    now = datetime.now()
    with session_scope() as session:
        stmt = select(PendingDelete)
        if watch_id:
            stmt = stmt.where(PendingDelete.watch_id == watch_id)
        rows = [r.to_dict() | {
            "waited_seconds": int((now - r.detected_time).total_seconds()),
            "grace_seconds": grace,
        } for r in session.scalars(stmt).all()]
    return rows


def cancel_hold(link_path: str) -> bool:
    """手动撤销一条扣留 —— 用户确认这个文件不该被删。"""
    with session_scope() as session:
        row = session.get(PendingDelete, link_path)
        if row is None:
            return False
        session.delete(row)
    logger.info(f"已手动撤销扣留: {link_path}")
    return True


def _record_torrents(code: str, source_path: str) -> list[str]:
    """建链接时就把源文件对应的种子 hash 落到 History 表。

    为什么不等删除时再问下载器：那一刻种子可能已经不在了 —— 做种到期被清、
    用户在 qb 里删了任务但留着文件、换了下载器。种子信息一丢，就永远删不掉
    那些种子了。而建链接这一刻文件刚下载完，种子必定还在，此时落库最稳。

    写进 History 而不另立新表：History 已经是 hash → code 的映射，
    删除流程走的正是 code → History → hash 这条路，写这里等于零改动接上。
    save_path 记源文件路径，便于排查。

    返回记下的 hash 列表。查不到（手工拷进来的文件、下载器离线）返回空，
    删除时还有「按路径现查」兜底。
    """
    from app.modules.downloadclient import find_torrents_by_path

    try:
        mapping = find_torrents_by_path([source_path])
    except Exception as exc:
        logger.warning(f"[{code}] 查询源文件对应种子失败: {exc}")
        return []

    hashes = mapping.get(source_path) or []
    if not hashes:
        logger.debug(f"[{code}] 下载器里找不到对应种子，可能是手工放入的文件")
        return []

    with session_scope() as session:
        for h in hashes:
            existing = session.get(History, h)
            if existing is not None:
                # 已有记录（cinefold 自己下载的）不改 code —— 那边的 code 是
                # 真番号，比这里从文件名生成的更准
                continue
            session.add(History(hash=h, code=code, save_path=source_path))

    logger.info(f"[{code}] 已登记 {len(hashes)} 个种子: {', '.join(hashes)}")
    return hashes


def backfill_torrents(watch_id: int = 0) -> int:
    """给还没登记种子的关联补查一次。返回新登记的种子数。

    为什么建链接时查过还要再查：那一刻种子可能还不在下载器里，或者查不到 ——

      · 文件下载完才被 qb 移到监控目录（「完成后移动」），移动前那一刻
        源路径和种子里的路径对不上
      · 种子刚加进来还在获取元数据，文件清单是空的
      · 事后才做种（先有文件，后拿去发布/转种）
      · 建链接时下载器临时离线

    这些情形下 History 是空的，删除时就只能靠现查兜底 —— 而现查会遇到
    「种子已经不在下载器里」的问题。所以定时补一轮，把窗口补上。

    只查 History 里确实没有记录的，已登记的不重复查 —— 一次全量拉取
    下载器的种子列表并不便宜。
    """
    with session_scope() as session:
        stmt = select(MediaLink)
        rows = [
            {"code": r.code, "source_path": r.source_path, "link_path": r.link_path}
            for r in session.scalars(stmt).all()
        ]
        # 已经有种子记录的 code，跳过
        known = {
            c for (c,) in session.execute(
                select(History.code).where(
                    History.code.in_([r["code"] for r in rows] or [""])
                )
            ).all()
        }

    if watch_id:
        # 限定到某条规则的目标目录下
        with session_scope() as session:
            rule = session.get(WatchDir, watch_id)
            if rule is None:
                return 0
            base = target_base(rule)
            if base is None:
                return 0
            prefix = str(base)
        rows = [r for r in rows if r["link_path"].startswith(prefix)]

    pending = [
        r for r in rows
        if r["code"] not in known and r["source_path"]
        and Path(r["source_path"]).exists()
    ]
    if not pending:
        return 0

    from app.modules.downloadclient import find_torrents_by_path

    try:
        mapping = find_torrents_by_path([r["source_path"] for r in pending])
    except Exception as exc:
        logger.warning(f"补查种子失败: {exc}")
        return 0

    if not mapping:
        return 0

    added = 0
    with session_scope() as session:
        for row in pending:
            hashes = mapping.get(row["source_path"]) or []
            for h in hashes:
                if session.get(History, h) is not None:
                    continue
                session.add(History(
                    hash=h, code=row["code"], save_path=row["source_path"]
                ))
                added += 1
                logger.info(
                    f"[{row['code']}] 补登记种子 {h}（建链接时下载器里还没有）"
                )

    return added


def _register(code: str, source_path: str, link_path: str) -> None:
    """写入 media_link。直接落库而不走 register_scrape。

    register_scrape 会扫整个媒体库按 inode 反查硬链接 —— 那是刮削场景下
    「不知道链接在哪」才需要的。这里链接是我们自己刚建的，路径确定，
    扫库纯属浪费（大库上是分钟级）。
    """
    try:
        st = os.stat(source_path)
        inode, device = (st.st_ino or None), (st.st_dev or None)
    except OSError:
        inode, device = None, None

    with session_scope() as session:
        existing = session.get(MediaLink, link_path)
        if existing is not None:
            existing.code = code
            existing.source_path = source_path
            existing.inode = inode
            existing.device = device
        else:
            session.add(MediaLink(
                link_path=link_path,
                code=code,
                source_path=source_path,
                inode=inode,
                device=device,
            ))


def _drop_record(link_path: str) -> None:
    with session_scope() as session:
        row = session.get(MediaLink, link_path)
        if row is not None:
            session.delete(row)


def _record_scan(rule_id: int, file_count: int, errors: list[str]) -> None:
    from datetime import datetime

    with session_scope() as session:
        row = session.get(WatchDir, rule_id)
        if row is None:
            return
        row.last_scan_time = datetime.now()
        row.file_count = file_count
        row.last_error = "; ".join(errors[:5]) if errors else None


def _record_error(rule_id: int, message: str) -> None:
    from datetime import datetime

    with session_scope() as session:
        row = session.get(WatchDir, rule_id)
        if row is None:
            return
        row.last_scan_time = datetime.now()
        row.last_error = message


# ----------------------------------------------------------------------
# 全量对账
# ----------------------------------------------------------------------
def sync_all(dry_run: bool = False) -> list[SyncResult]:
    """对账全部启用的监控规则。定时任务与手动触发共用。"""
    with session_scope() as session:
        ids = list(session.scalars(
            select(WatchDir.id).where(WatchDir.enabled.is_(True))
        ).all())

    if not ids:
        logger.debug("[任务] 没有启用的监控目录，跳过同步")
        return []

    results: list[SyncResult] = []
    for rule_id in ids:
        try:
            results.append(sync_rule(rule_id, dry_run=dry_run))
        except Exception as exc:
            # 单条规则出错不该影响其余规则
            logger.exception(f"[任务] 监控目录 {rule_id} 同步异常: {exc}")
            failed = SyncResult(watch_id=rule_id, dry_run=dry_run)
            failed.errors.append(str(exc))
            results.append(failed)
    return results
