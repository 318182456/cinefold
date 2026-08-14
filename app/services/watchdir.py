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

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select, update

from app.core.config import get_settings
from app.database.models import (
    CodeAlias, History, MediaLink, PendingDelete, WatchDir,
)
from app.database.session import (
    SessionLocal, insert_ignore_duplicate, session_scope,
)
from app.services import watchdir_progress as progress
from app.services.medialink import (
    VIDEO_SUFFIXES, handle_media_deleted, is_adoptable_video, mark_completed,
)

# 文件刚出现时可能还在写入（下载中、拷贝中）。连续两次 stat 大小不变才认为
# 写完了，否则硬链接会指向一个残缺文件，Emby 扫进去是坏的
STABLE_CHECK_INTERVAL = 2.0
STABLE_CHECK_ROUNDS = 3

# mtime 早于这个秒数的文件直接判定为已写完，跳过 sleep 观测。
#
# 首轮全量登记要过一遍整个库（几千个文件），每个都 sleep 2s 起步就是几小时，
# 而其中绝大多数是几个月前就躺在磁盘上的存量文件，等它们纯属白等。
#
# 60s 的余量对写入中的文件是安全的：还在下载/拷贝的文件 mtime 一直在刷新，
# 不可能落到一分钟前。取值远大于 STABLE_CHECK_INTERVAL × ROUNDS，
# 比原来的观测法更保守。
STABLE_MTIME_AGE = 60.0

# 登记攒多少条提交一次。见 _RegisterBatch —— 大了省提交开销，
# 小了崩溃时少丢。200 条约合 1 次提交 / 3.6 秒的处理量，两头都不吃亏
REGISTER_BATCH_SIZE = 200

# code 列在 media_link / history / pending_delete / code 四张表里都是
# varchar(64)。code 由文件名生成，长片名必须截断，否则 PostgreSQL 拒绝插入
CODE_MAX_LENGTH = 64
# 长文件名转哈希时取的位数。32 位十六进制（128 bit）对几万条文件的碰撞
# 概率可以忽略；加上 32 字符的前缀上限与标记位也仍在 64 以内
CODE_HASH_LENGTH = 32
# 哈希 code 的标记位。用它认出「这个 code 读不出片名，去 code_alias 查」，
# 比事后猜格式可靠。十六进制里不会出现 h，不会与哈希本身混淆
CODE_HASH_MARK = "h"

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
    # 认领的已有硬链接：目标目录里本来就存在（多为刮削工具建的），
    # 只补登记，没有新建文件。[{link_path, source_path}]
    claimed: list[dict] = field(default_factory=list)
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
            "claimed": self.claimed,
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

    直通模式没有「目标目录」这回事：源目录自己就是媒体库目录，Emby 直接扫它。
    返回源目录，下游的归属判定（link_path 前缀匹配）与路径计算就都成立了。
    """
    if rule.passthrough:
        try:
            return Path(rule.source_dir)
        except (OSError, ValueError):
            return None

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

    直通模式下目标就是源文件自己。直接返回 source 而不走 root / relative：
    后者会把路径重新拼一遍（resolve 过符号链接、分隔符可能变形），拼出来的
    字符串与 str(source) 不一定逐字相等，而下游正是拿这两者做字典键比对的。
    """
    if rule.passthrough:
        return source

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

    超过 CODE_MAX_LENGTH 的换成哈希，见 _hash_code。
    """
    stem = source.stem
    prefix = (rule.code_prefix or "").strip()
    code = f"{prefix}-{stem}" if prefix else stem
    if len(code) <= CODE_MAX_LENGTH:
        return code
    return _hash_code(prefix, stem)


def _hash_code(prefix: str, stem: str) -> str:
    """长文件名换成「前缀 + 文件名哈希」。

    code 列是 varchar(64)，而它来自文件名 —— 日文长片名轻松超过 64 字符，
    PostgreSQL 会直接拒绝插入（StringDataRightTruncation），整轮同步中断。
    SQLite 不校验长度，所以这个坑只在 PG 上现形。

    不能裸截断：两个长片名很可能共享同一段前缀，截完撞成同一个 code 就会被
    当成同一部片子，删一部连带删另一部。哈希整个文件名才能保证不撞。

    代价是 code 不再有可读性，列表页认不出是哪部片子。原文件名记在
    code_alias 表里补回来（登记时写，见 _register_alias）。规则前缀留在
    哈希外面不参与计算，按前缀筛选还能用。
    """
    digest = CODE_HASH_MARK + hashlib.sha1(
        stem.encode("utf-8")
    ).hexdigest()[:CODE_HASH_LENGTH]
    if not prefix:
        return digest
    # code_prefix 列宽 32，与「-」和哈希拼起来会差一个字符溢出 64。
    # 宁可切前缀也不能动哈希 —— 哈希短一位就多一分撞车风险
    room = CODE_MAX_LENGTH - len(digest) - 1
    return f"{prefix[:room]}-{digest}"


def is_hashed_code(code: str) -> bool:
    """code 是否为哈希形式（即原文件名放不进 code 列）。

    靠标记位判断，不靠「code 里含不含文件名」—— 文件改名后源文件名与 code
    本就不一致（改名走移动判定，code 保持不变），那种判据会把普通 code
    误认成哈希。
    """
    return code.rsplit("-", 1)[-1].startswith(CODE_HASH_MARK)


def _mark_completed_if_real_code(
    rule: WatchDir, code: str, session=None
) -> None:
    """入库成功后回写订阅状态，但只对能对上真番号的 code 做。

    make_code 生成的 code 不保证是番号：配了 code_prefix 的是刻意与番号
    隔离的，哈希 code 已经读不出原文件名。这两类拿去匹配 Code 表要么必然
    落空，要么撞上同名的无辜番号，一律跳过。
    """
    if (rule.code_prefix or "").strip() or is_hashed_code(code):
        return
    mark_completed(code, session=session)


def _looks_settled(path: Path) -> bool:
    """靠 mtime 判断文件是否早就写完了，不 sleep。

    还在下载/拷贝的文件 mtime 一直在刷新，不可能落到 STABLE_MTIME_AGE 之前。
    """
    try:
        st = path.stat()
    except OSError:
        return False
    return time.time() - st.st_mtime >= STABLE_MTIME_AGE


def _wait_stable(path: Path) -> bool:
    """等文件写完。连续两次观测大小不变即认为稳定。

    下载中/拷贝中的文件建了硬链接，Emby 会扫进一个残缺文件。宁可这轮跳过，
    下一轮对账再补上。

    STABLE_CHECK_ROUNDS 是「最多再观测几次」，不含第一次读取 —— 早就写完的
    文件（对账时的绝大多数）第二次观测就能确认，不必把轮数耗光。

    mtime 已经足够老的文件连观测都免了，直接放行：见 STABLE_MTIME_AGE。
    """
    try:
        st = path.stat()
    except OSError:
        return False

    # 快路径：文件很久没被动过，不可能正在写入
    if time.time() - st.st_mtime >= STABLE_MTIME_AGE:
        return True

    last = st.st_size

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


def _settle_together(sources: list[Path]) -> set[str]:
    """一次性等一批新文件写完，返回其中已稳定的。

    逐个 _wait_stable 是串行的：每个文件至少 sleep 2s，一批 30 个新文件就是
    一分钟，而这一分钟里其余 29 个文件本来也在同时写入 —— 等一次就够了。

    做法是「记大小 → 睡一次 → 再记大小」，两次相同的判为稳定。轮数与单文件
    版本一致，只是把 sleep 从每文件一次变成每轮一次。
    """
    if not sources:
        return set()

    def _sizes(paths: list[Path]) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in paths:
            try:
                out[str(p)] = p.stat().st_size
            except OSError:
                continue  # 读不到的这轮不算稳定，下一轮对账再说
        return out

    watching = list(sources)
    settled: set[str] = set()
    last = _sizes(watching)

    for _ in range(max(1, STABLE_CHECK_ROUNDS)):
        if not watching:
            break
        time.sleep(STABLE_CHECK_INTERVAL)
        now = _sizes(watching)
        for key, size in now.items():
            if key in last and last[key] == size:
                settled.add(key)
        watching = [p for p in watching if str(p) not in settled]
        last = now

    return settled


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
# 每条规则一把锁，保证同一规则同时只跑一轮对账。
#
# 触发 sync_rule 的路径有三条，彼此完全独立、都可能在同一时刻到达：
#   1. watchdog 事件（防抖 5s 后）—— 只挡得住「同一规则的连续事件」，
#      挡不住它与下面两条撞车
#   2. 定时任务的 sync_all（默认 30 分钟一轮）
#   3. 用户在页面上点「全部对账」/ 单条对账
#
# 三者共用同一份 media_link 与磁盘状态，没有锁时会真的互相打架：
# 一轮正判定「该删链接了」，另一轮同时在为同一个源文件建链接，来回拉锯；
# 更糟的是两轮同时走到反向删除，对同一批文件各执行一次删除动作。
#
# 用「拿不到就跳过」而不是排队等待：这是对账，不是必须逐次执行的任务 ——
# 已经有一轮在跑，它看到的就是最新状态，后来者再跑一遍纯属重复劳动。
# 排队反而会让事件风暴堆出一长串等待中的对账，全部跑完要很久。
_rule_locks: dict[int, threading.Lock] = {}
# 保护 _rule_locks 本身的增删。锁的创建必须原子，否则两个线程可能各自
# 建一把锁，互斥就失效了
_rule_locks_guard = threading.Lock()


def _rule_lock(rule_id: int) -> threading.Lock:
    """取这条规则的锁，没有就建一把。"""
    with _rule_locks_guard:
        lock = _rule_locks.get(rule_id)
        if lock is None:
            lock = threading.Lock()
            _rule_locks[rule_id] = lock
        return lock


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

    同一规则同时只跑一轮（见 _rule_locks）。已有一轮在跑时直接返回，
    不排队 —— 那一轮看到的就是最新状态，再跑一遍是重复劳动。

    演练不占锁：它不改任何东西，没有互斥的必要，而且用户点演练时后台
    很可能正跑着定时对账，占锁会让演练白白失败。
    """
    if dry_run:
        return _sync_rule_locked(rule_id, dry_run=True)

    lock = _rule_lock(rule_id)
    if not lock.acquire(blocking=False):
        logger.info(f"[监控目录] 规则 {rule_id} 正在对账中，本次触发跳过")
        result = SyncResult(watch_id=rule_id, dry_run=dry_run)
        result.skipped.append("上一轮对账尚未结束，本次跳过")
        return result
    try:
        return _sync_rule_locked(rule_id, dry_run=dry_run)
    finally:
        lock.release()


def _sync_rule_locked(rule_id: int, dry_run: bool = False) -> SyncResult:
    """sync_rule 的实际实现。调用方负责持有该规则的锁。"""
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
            passthrough=row.passthrough,
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

    # 演练不上报进度：它不做 _wait_stable，跑得很快，而且不该把真实同步的
    # 进度覆盖掉 —— 用户点演练时后台可能正跑着定时对账
    track = not dry_run
    if track:
        progress.start(
            rule_id, rule.name or rule.source_dir, 0,
            passthrough=rule.passthrough,
        )
        progress.update(rule_id, phase="scanning", message="正在扫描源目录…")

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
    # 那时链接又存在，跟从没删过一样，反向删除永远不会触发。
    #
    # 按记录遍历而不是按 expected：认领来的链接，其 link_path 由刮削工具决定，
    # 不等于规则算出的目标路径，在 expected 里根本找不到。只认 expected 的话，
    # 「在 Emby 里删掉刮削建的那份」不但不联动，下一轮还会按规则路径重建一份
    scanned_sources = {str(s) for s in expected.values()}
    if rule.passthrough:
        # 直通模式下 link_path == source_path，两者同生共死：文件被 Emby 删掉时
        # 源文件也就没了，上面那套「源文件还在、链接没了」的判据一条都不成立，
        # media_deleted 会恒为空，反向删除永远不触发。
        #
        # 这里只能退回到「登记过、现在文件不在了」。代价是分辨不出这一删究竟
        # 来自 Emby 还是别的途径（用户直接删源目录、脚本清理）—— 但直通模式下
        # 这两者本就是同一个文件的同一次删除，没有区分的必要，后续动作一样：
        # 删种、清记录。真正的误删防护交给 inode 移动判定与宽限期扣留
        media_deleted = {
            link_path for link_path, record in linked_records.items()
            if not Path(link_path).exists()
        }
    else:
        media_deleted = {
            link_path for link_path, record in linked_records.items()
            if record.source_path in scanned_sources
            and not Path(link_path).exists()
        }

    # 链接已被删的那些源文件，本轮不能再为它们建链接 —— 那是在跟用户的删除
    # 操作对着干，而且下一轮又会被删掉，来回拉锯
    deleted_sources = {
        linked_records[p].source_path for p in media_deleted
        if p in linked_records
    }

    # 已经登记且链接还在的源文件，既不必建也不必认领。
    # 按源文件而不是按目标路径判断：认领的 link_path 与规则算出来的对不上，
    # 只看目标路径的话每轮都会重扫一遍目标目录，大库上就是每轮几分钟白跑
    settled_sources = {
        r.source_path for r in linked_records.values()
        if r.source_path and Path(r.link_path).exists()
    }

    # --- 1) 源有、库无：建链接 ---
    # 只统计真正要处理的，已建好的不算进度分母 —— 否则一个几千文件的目录
    # 每轮都显示「0/5000」跑到「5000/5000」，看不出实际有几个新文件
    todo = [
        (t, s) for t, s in expected.items()
        if str(s) not in settled_sources
        and str(s) not in deleted_sources
    ]

    # 建之前先认领：目标目录里可能已经有指向同一份数据的硬链接了（刮削工具
    # 建的，路径按它的规则组织，与我们算出来的对不上）。不认领就会重复建，
    # 媒体库里凭空多出一部同样的片子。
    # 只在确实有待建链接时才扫 —— 全量 rglob 在大库上是分钟级的
    #
    # 直通模式没有认领这回事：目标就是源文件本身，不存在「别处已有一份链接」
    if todo and not rule.passthrough:
        if track:
            progress.update(
                rule_id, phase="claiming",
                message="正在核对目标目录中已有的硬链接…",
            )
        claimed = _claim_existing(rule, todo, base, result, dry_run)
        if claimed:
            todo = [(t, s) for t, s in todo if t not in claimed]

    if track:
        pending = "个待登记" if rule.passthrough else "个待建链接"
        progress.update(
            rule_id, phase="linking", total=len(todo), done=0,
            message=f"共 {len(videos)} 个文件，{len(todo)} {pending}",
        )

    # 种子归属一次性查完。放在循环外是硬要求，不是优化 —— 详见 _prefetch_torrents
    torrent_map: dict[str, list[str]] = {}
    if todo and not dry_run:
        if track:
            progress.update(rule_id, message="正在向下载器反查种子归属…")
        torrent_map = _prefetch_torrents([str(s) for _, s in todo])

    # 新文件（mtime 还很新）统一等一轮，而不是循环里逐个 sleep 2 秒 ——
    # 它们本来就在同时写入，等一次和等 N 次的效果一样，见 _settle_together
    settled: set[str] = set()
    if todo and not dry_run:
        fresh = [s for _, s in todo if not _looks_settled(s)]
        if fresh:
            if track:
                progress.update(
                    rule_id,
                    message=f"等待 {len(fresh)} 个新文件写入完成…",
                )
            settled = _settle_together(fresh)

    # 登记按批提交，不是一条一个事务 —— 提交本身在 SQLite 上约 18ms，
    # 几千个文件光提交要好几分钟。也不整轮一个事务：那样中途崩了
    # （容器被重启、磁盘满）已建好的链接全都没登记，下一轮又要重来
    with _RegisterBatch(REGISTER_BATCH_SIZE) as batch:
        for index, (target_str, source) in enumerate(todo, start=1):
            target = Path(target_str)

            if dry_run:
                result.linked.append(target_str)
                continue

            if track:
                progress.update(
                    rule_id, done=index - 1, current=source.name,
                    message="正在登记…",
                )

            # 已在批量等待里确认稳定的直接过；其余（本就是存量文件）走
            # 单文件判定，它对 mtime 老的文件不 sleep
            if str(source) not in settled and not _wait_stable(source):
                result.skipped.append(f"文件仍在写入，本轮跳过: {source}")
                continue

            # 直通模式不建链接，只登记。文件已经在它该在的位置了
            if not rule.passthrough:
                ok, err = _hardlink(source, target)
                if not ok:
                    logger.error(err)
                    result.errors.append(err)
                    continue

            code = make_code(rule, source)
            session = batch.session()
            _register(code, str(source), target_str, session=session)
            # 种子信息趁现在存下来 —— 删除时下载器里可能已经没有这个种子了。
            # 直通模式尤其依赖这一步：没有硬链接，删除时全靠这条记录找回种子
            _record_torrents(code, str(source), torrent_map, session=session)
            _mark_completed_if_real_code(rule, code, session=session)
            batch.done()
            result.linked.append(target_str)
            if track:
                progress.update(rule_id, done=index, linked=len(result.linked))
            # 逐条 INFO 在几千个文件时会把日志刷爆，降到 DEBUG
            if rule.passthrough:
                logger.debug(f"[{code}] 直通模式已登记 {source}")
            else:
                logger.debug(f"[{code}] 已建硬链接 {source} → {target}")

    if track:
        progress.update(
            rule_id, phase="checking", current="",
            message="正在核对已删除的文件…",
        )

    # --- 2) 源无、库有：可能是移动，可能是真删除 ---
    # 认领来的链接，其 link_path 是刮削工具定的，不等于规则算出的目标路径，
    # 所以不能只看 target_str 在不在 expected 里 —— 那样每轮都会把它们判成
    # 「不在监控范围」并刷一条无意义的跳过。按源文件判断才准
    scanned_sources = {str(s) for s in expected.values()}

    for target_str, record in linked_records.items():
        if target_str in expected:
            continue
        # 源文件仍在本轮扫描结果里，说明这条关联好好的（认领的链接走这条）
        if record.source_path in scanned_sources and Path(target_str).exists():
            continue
        # 直通模式下没有「只删链接、留着源文件」这种中间态 —— 链接就是源文件。
        # 这些记录全部由上面的 media_deleted 接管，走第 3 步的反向删除路径。
        # 在这里再处理一遍就成了「删掉刚判定为待反向删除的那个文件」
        if rule.passthrough:
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
    # 关掉反向删除的规则，这一步永远不会走到删除，但之前可能已经攒下扣留记录
    # （开着的时候登记的，或第 2 步直通分支转过来的）。留着有两个害处：表只增
    # 不减（_prune_holds 只清「文件恢复了」和「记录没了」，这些两者都不是），
    # 且页面上的扣留列表会显示一批「正在观察、即将删除」的条目，实际永远不删。
    # 开关是规则级的，循环内不变，所以在循环外一次清完 —— 逐条 _clear_hold
    # 会为每个文件白开一个事务
    if media_deleted and not rule.reverse_delete and not dry_run:
        _clear_holds(media_deleted)

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
            # 直通模式下 source_path 必须跟着一起改 —— 它与 link_path 是同一个
            # 文件。只改 link_path 会让记录里的源路径指向一个已经不在的位置，
            # 之后删种要靠它反查下载器，指错了就什么都查不到
            new_source = (
                str(moved_to) if rule.passthrough else record.source_path
            )
            _register(record.code, new_source, str(moved_to))
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

    if track:
        progress.update(
            rule_id, linked=len(result.linked), unlinked=len(result.unlinked)
        )
        progress.finish(
            rule_id,
            f"新建 {len(result.linked)}，认领 {len(result.claimed)}，"
            f"删除 {len(result.unlinked)}，移动 {len(result.moved)}，"
            f"扣留 {len(result.held)}",
        )

    logger.info(
        f"[监控目录 {rule.name or rule.source_dir}] 同步完成"
        f"{'（演练）' if dry_run else ''} —— "
        f"新建 {len(result.linked)}，认领 {len(result.claimed)}，"
        f"删除 {len(result.unlinked)}，"
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


def _library_inode_index(base: Path) -> dict[tuple[int, int], Path]:
    """扫目标目录，建 (inode, device) → 文件路径 的索引。

    用来认领「已经存在但没登记」的硬链接。典型场景：刮削工具（MDCng 之类）
    早就把源文件链接进媒体库了，路径按它自己的规则组织 ——

        源文件  /downloads/日本AV/ofku-232/ofku-232.mp4
        刮削链接 /media/日本AV/一条美绪/OFKU-232 一条美绪/OFKU-232-有码.mp4

    而规则按源目录结构算出来的目标是 /media/日本AV/ofku-232/ofku-232.mp4，
    两者对不上。不认领的话规则会再建一份，同一个 inode 出现两个路径、
    两条记录 —— 媒体库里凭空多出一部重复的片子。

    认 inode 而不认文件名：硬链接共享 inode 是文件系统的保证，刮削工具怎么
    重命名都不影响。文件名匹配则完全依赖对方的命名规则，改一次就失准。

    整个目标目录扫一遍，大库上不便宜，所以只在确实有待建链接时才调用。
    """
    index: dict[tuple[int, int], Path] = {}
    try:
        for path in base.rglob("*"):
            try:
                if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            if not st.st_ino:
                continue
            key = (st.st_ino, st.st_dev)
            # 同一 inode 有多个链接时保留第一个 —— 认领只需要一个代表
            index.setdefault(key, path)
    except OSError as exc:
        logger.warning(f"扫描目标目录建立 inode 索引失败 {base}: {exc}")
    return index


def _claim_existing(
    rule: WatchDir, todo: list[tuple[str, Path]], base: Path,
    result: SyncResult, dry_run: bool,
) -> set[str]:
    """认领目标目录里已经存在的硬链接，返回被认领的目标路径集合。

    「已存在」指目标目录下有文件与源文件同 inode —— 那就是同一份数据，
    只是路径不是我们算出来的那个。这种链接多半是刮削工具建的，本来就该
    归入管理，而不是再建一份重复的。

    认领只写 media_link 记录，不动任何文件。登记的 link_path 是那个已有的
    路径（刮削建的），不是规则算出来的路径 —— 记录必须指向磁盘上真实存在的
    那个文件，否则后续的存在性检查、反向删除全会错位。

    返回的集合从 todo 里剔除，这些不再走建链接流程。
    """
    index = _library_inode_index(base)
    if not index:
        return set()

    claimed: set[str] = set()
    # 先只做判定，登记推到后面 —— 种子反查要整批查一次，见 _prefetch_torrents
    hits: list[tuple[str, Path, str]] = []
    for target_str, source in todo:
        try:
            st = source.stat()
        except OSError:
            continue
        if not st.st_ino:
            continue  # Windows 上取不到 inode，认领无从谈起

        hit = index.get((st.st_ino, st.st_dev))
        if hit is None:
            continue

        link_path = str(hit)
        claimed.add(target_str)
        result.claimed.append({
            "link_path": link_path,
            "source_path": str(source),
        })
        hits.append((target_str, source, link_path))

    if dry_run or not hits:
        return claimed

    torrent_map = _prefetch_torrents([str(s) for _, s, _ in hits])
    for _, source, link_path in hits:
        code = make_code(rule, source)
        _register(code, str(source), link_path)
        # 认领的链接同样要记种子 —— 反向删除时才有据可依
        _record_torrents(code, str(source), torrent_map)
        _mark_completed_if_real_code(rule, code)
        logger.info(
            f"[{code}] 认领已有硬链接（未新建）: {source} → {link_path}"
        )

    return claimed


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


def _clear_holds(link_paths) -> int:
    """批量撤销扣留，一个事务。返回实际删掉的条数。

    逐条 _clear_hold 会为每个路径开一个事务，SQLite 上一次 commit 约 18ms，
    整批文件就是分钟级。用 IN 一次删完。

    IN 子句分片是必须的，不是优化：SQLite 默认参数上限 999（SQLITE_MAX_
    VARIABLE_NUMBER），一个关掉反向删除的大目录很容易超过。
    """
    paths = list(link_paths)
    if not paths:
        return 0

    removed = 0
    with session_scope() as session:
        for start in range(0, len(paths), 500):
            chunk = paths[start:start + 500]
            removed += session.execute(
                delete(PendingDelete).where(PendingDelete.link_path.in_(chunk))
            ).rowcount or 0
    return removed


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


class _RegisterBatch:
    """把多个文件的登记攒在一个事务里提交。

    每条一个事务的话，提交本身就是瓶颈：SQLite 上一次 commit 要 fsync，
    实测约 18ms，几千个文件光提交就是好几分钟。攒批之后提交次数降到
    文件数 / size。

    也不做成整轮一个事务：中途崩了（容器重启、磁盘写满）已经建好的硬链接
    全都没有登记记录，下一轮对账会把它们当成「库有源无」重新处理。按批
    提交则最多丢最后一批。
    """

    def __init__(self, size: int):
        self.size = max(1, size)
        self._session = None
        self._pending = 0

    def session(self):
        """取当前批次的 session，没有就开一个。"""
        if self._session is None:
            self._session = SessionLocal()
        return self._session

    def done(self) -> None:
        """记完一个文件。攒够一批就提交。"""
        self._pending += 1
        if self._pending >= self.size:
            self.close()

    def close(self) -> None:
        """提交并关闭当前批次。失败时回滚，异常照常抛给调用方。"""
        if self._session is None:
            return
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            self._session.close()
            self._session = None
            self._pending = 0

    def __enter__(self) -> "_RegisterBatch":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """循环中途抛异常时，别把未提交的这批也带走 —— 已经建好的硬链接
        必须有对应记录，否则下一轮会把它们当孤儿链接删掉。"""
        if exc_type is None:
            self.close()
            return
        if self._session is not None:
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()
            finally:
                self._session.close()
                self._session = None
                self._pending = 0


def _prefetch_torrents(paths: list[str]) -> dict[str, list[str]]:
    """一次性查出这批源文件对应的种子，供整轮同步共用。

    find_torrents_by_path 的成本与「下载器里的种子总数」成正比，与传入路径
    数量几乎无关 —— 它得把每个种子的文件清单都拉一遍才能建索引。所以必须
    整批查一次，绝不能每个文件查一次：几千个文件 × 几百个种子的文件清单
    请求，是几百万次 HTTP 往返，同步会慢到看着像卡死。

    查不到就返回空表，登记照常进行 —— 种子信息不是登记的前提条件。
    """
    if not paths:
        return {}

    from app.modules.downloadclient import find_torrents_by_path

    try:
        mapping = find_torrents_by_path(paths)
    except Exception as exc:
        logger.warning(f"批量查询源文件对应种子失败: {exc}")
        return {}

    if mapping:
        logger.info(f"下载器反查到 {len(mapping)} 个文件的种子归属")
    return mapping


def _record_torrents(
    code: str, source_path: str, prefetched: dict[str, list[str]] | None = None,
    session=None,
) -> list[str]:
    """建链接时就把源文件对应的种子 hash 落到 History 表。

    为什么不等删除时再问下载器：那一刻种子可能已经不在了 —— 做种到期被清、
    用户在 qb 里删了任务但留着文件、换了下载器。种子信息一丢，就永远删不掉
    那些种子了。而建链接这一刻文件刚下载完，种子必定还在，此时落库最稳。

    写进 History 而不另立新表：History 已经是 hash → code 的映射，
    删除流程走的正是 code → History → hash 这条路，写这里等于零改动接上。
    save_path 记源文件路径，便于排查。

    prefetched 是整轮同步预先查好的映射（见 _prefetch_torrents）。传了就查表，
    不再问下载器 —— 批量同步必须走这条路，否则每个文件一次全量反查。
    单条调用（webhook、补查）不传，行为与原来一致。

    session 同 _register：批量同步共用一个事务，避免逐条提交。

    返回记下的 hash 列表。查不到（手工拷进来的文件、下载器离线）返回空，
    删除时还有「按路径现查」兜底。
    """
    if prefetched is not None:
        hashes = prefetched.get(source_path) or []
    else:
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

    # 交给数据库忽略冲突，不做「先查后插」—— 后者堵不住重复：
    #
    #   · 一个种子含多个视频（合集）时，同一批事务里前面的文件刚 add 的
    #     History 还没 flush，session.get() 查不到，于是同一个 hash 被 add
    #     多次，commit 时 executemany 撞主键，整批登记全丢
    #   · 定时同步和手动触发并发跑时，两个事务各自都查不到，双双插入
    #
    # 冲突跳过而不是覆盖，正好也是想要的语义：已有记录（cinefold 自己下载的）
    # code 是真番号，比这里从文件名生成的更准，不该被改掉。
    rows = [
        {"hash": h, "code": code, "save_path": source_path}
        for h in dict.fromkeys(hashes)  # 反查可能给出重复 hash，先去重
    ]

    if session is not None:
        insert_ignore_duplicate(session, History, rows)
    else:
        with session_scope() as own:
            insert_ignore_duplicate(own, History, rows)

    logger.info(f"[{code}] 已登记 {len(hashes)} 个种子: {', '.join(hashes)}")
    return hashes


def backfill_torrents(watch_id: int = 0, force: bool = False) -> int:
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

    上面列的窗口都会自己关上，但有两类关联是永久查不到的：手工拷进监控目录
    的文件、种子早被删掉的文件。它们进不了 History，于是每轮对账都要为它们
    拉一次全量种子列表 —— 几千条这样的记录能把下载器刷到超时。所以连续查不到
    WATCHDIR_TORRENT_MISS_LIMIT 次之后转为低频重试，见 _probe_due。

    watch_id 非 0 时限定到该规则；手动触发（API）时 force 为真，无视降频，
    因为用户点下按钮就是要立刻查一次。
    """
    settings = get_settings()
    now = datetime.now()

    with session_scope() as session:
        stmt = select(MediaLink)
        rows = [
            {
                "code": r.code,
                "source_path": r.source_path,
                "link_path": r.link_path,
                "torrent_miss": r.torrent_miss or 0,
                "torrent_probe_time": r.torrent_probe_time,
            }
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

    candidates = [
        r for r in rows
        if r["code"] not in known and r["source_path"]
        and Path(r["source_path"]).exists()
    ]
    pending = [r for r in candidates if force or _probe_due(r, settings, now)]
    if not pending:
        if candidates:
            logger.debug(
                f"补查种子：{len(candidates)} 条待补关联全部处于降频期，本轮跳过"
            )
        return 0

    deferred = len(candidates) - len(pending)
    if deferred:
        logger.info(
            f"补查种子：{len(pending)} 条待查，{deferred} 条因连续查不到已降频跳过"
        )

    from app.modules.downloadclient import find_torrents_by_path_checked

    try:
        mapping, answered = find_torrents_by_path_checked(
            [r["source_path"] for r in pending]
        )
    except Exception as exc:
        logger.warning(f"补查种子失败: {exc}")
        return 0

    # 命中与否都要落回 media_link：查到的清零，查不到的累加，否则计数永不推进。
    # 但下载器一个都没答上话时不能记 —— 那是故障，跟「这个文件没有种子」是
    # 两回事，记了就会让故障期间的关联白白攒满次数、进入降频期
    if answered:
        _record_probe(pending, mapping, now)
    else:
        logger.warning("补查种子：下载器均无响应，本轮不计入失败次数")

    if not mapping:
        return 0

    # 一个种子可能对应多条 media_link（合集里的多个视频），先按 hash 去重，
    # 否则同一个 hash 在一批里出现多次
    rows: dict[str, dict] = {}
    for row in pending:
        for h in mapping.get(row["source_path"]) or []:
            if h in rows:
                continue
            rows[h] = {
                "hash": h, "code": row["code"], "save_path": row["source_path"],
            }

    if not rows:
        return 0

    with session_scope() as session:
        # 去重之后仍走 insert-ignore：这里查过没有、插入前别处（webhook、
        # 另一轮同步）刚插进去的情况堵不住，交给数据库跳过
        known = {
            h for (h,) in session.execute(
                select(History.hash).where(History.hash.in_(list(rows)))
            ).all()
        }
        fresh = [r for h, r in rows.items() if h not in known]
        if not fresh:
            return 0

        insert_ignore_duplicate(session, History, fresh)
        for r in fresh:
            logger.info(
                f"[{r['code']}] 补登记种子 {r['hash']}（建链接时下载器里还没有）"
            )

    return len(fresh)


def _probe_due(row: dict, settings, now: datetime) -> bool:
    """这条关联本轮该不该再去反查一次。

    没到失败上限的照常每轮查 —— 那些窗口（下载未完成、完成后移动、事后做种）
    通常几轮内就关上，早期多试几次代价小、收益大。
    到了上限说明大概率永久查不到，改为按 WATCHDIR_TORRENT_RETRY_HOURS 重试；
    该值配成 0 表示彻底放弃，不再浪费一次全量拉取。
    """
    limit = max(1, int(settings.watchdir_torrent_miss_limit or 1))
    if (row.get("torrent_miss") or 0) < limit:
        return True

    hours = int(settings.watchdir_torrent_retry_hours or 0)
    if hours <= 0:
        return False

    last = row.get("torrent_probe_time")
    # 计数到了上限却没有时间戳（存量数据、手工改库），当作到期，让它查一次
    # 顺便把时间戳补上，之后就能正常降频
    if last is None:
        return True
    return now - last >= timedelta(hours=hours)


def _record_probe(pending: list[dict], mapping: dict, now: datetime) -> None:
    """把本轮反查结果写回 media_link 的计数与时间戳。

    按 link_path（主键）分组批量更新。查到的清零 —— 那些窗口关上之后这条
    关联就正常了，下次再进这个函数说明是新情况，不该背着旧计数。
    """
    hit, miss = [], []
    for row in pending:
        target = hit if mapping.get(row["source_path"]) else miss
        target.append(row["link_path"])

    if not hit and not miss:
        return

    with session_scope() as session:
        if hit:
            session.execute(
                update(MediaLink)
                .where(MediaLink.link_path.in_(hit))
                .values(torrent_miss=0, torrent_probe_time=now)
            )
        for path in miss:
            # 逐条自增，不能用统一的常量值 —— 各条的当前计数不同
            session.execute(
                update(MediaLink)
                .where(MediaLink.link_path == path)
                .values(
                    torrent_miss=MediaLink.torrent_miss + 1,
                    torrent_probe_time=now,
                )
            )


def adopt_scrape_dir(
    dry_run: bool = True, fallback_passthrough: bool = False,
) -> dict:
    """把刮削输出目录里「已存在但没登记」的影片纳入管理。

    针对的是启用 cinefold 之前刮削工具就建好的那批链接：文件在媒体库里，
    media_link 里却没有记录，于是 Emby 删片时反查不到源文件与种子，
    删除联动对它们完全失效。

    刮削输出目录不是监控规则（列表里是 id=0 的占位项），「全部对账」扫不到
    它 —— sync_all 只遍历数据库里的 WatchDir 行。所以要单独一条入口。

    配对靠 inode，不靠文件名：

        媒体库里的链接  /volume3/h_video/日本AV/一条美绪/OFKU-232-有码.mp4
        下载器里的源文件 /downloads/日本AV/ofku-232/ofku-232.mp4
                        └── 同一个 inode，硬链接的文件系统保证

    候选源文件从下载器的种子清单来，而不是猜某个下载目录 —— 种子里有什么、
    存在哪，下载器才有权威答案，顺带这一趟就把种子 hash 也拿到了，
    登记 media_link 与 History 一次做完。

    code 从链接文件名提番号（find_serial_number）。提不出来的跳过而不是
    退回文件名：这批文件本来就是刮削过的，文件名里必有番号；提不出来说明
    它不是刮削产物（用户手放的、命名被改烂了），硬造一个 code 只会在
    media_link 里留下一条永远对不上号的脏记录。

    dry_run 默认为真：这个操作会写 media_link，而 media_link 是反向删除的
    依据 —— 配错了等于把删除权指向错误的源文件。先让用户看清配对结果。

    fallback_passthrough 为真时，配不到源文件的那批降级登记成直通模式
    （source_path == link_path，媒体库里那个文件自己就是源文件）：

        效果   Emby 删掉它时联动能真正删掉这个文件，不再是管辖范围之外。
        代价   没有种子线索，删了也回收不了下载器里的空间。

    默认关闭是有意的。inode 配不上不等于源文件真的不在下载器里 ——
    跨文件系统复制过、被重新建过硬链接、NAS 快照都会让 inode 对不上。
    这种情况下登记成直通，等于主动放弃那条种子线索：以后 Emby 删掉它，
    源文件与种子会永远留在下载器里占空间，且再没有记录能追回来。

    所以正确的用法是先跑一遍默认配对，再跑 rebuild_from_history 按
    History.save_path 补一轮（源文件已删也能重建），两轮都配不上的
    才值得降级直通。

    返回 {"adopted": [...], "passthrough": [...], "unmatched": [...],
          "skipped": [...], "total": N}。
    """
    from app.modules.downloadclient import all_torrent_files_with_hashes
    from app.utils import find_serial_number

    settings = get_settings()
    scrape_dir = (settings.medialink_scrape_dir or "").strip() \
        or settings.medialink_library_path
    result: dict = {
        "scrape_dir": scrape_dir,
        "dry_run": dry_run,
        "fallback_passthrough": fallback_passthrough,
        "total": 0,
        "adopted": [],
        "passthrough": [],
        "unmatched": [],
        "skipped": [],
        "errors": [],
    }
    if not scrape_dir:
        result["errors"].append(
            "未配置刮削输出目录（MEDIALINK_SCRAPE_DIR）与媒体库根目录"
        )
        return result

    root = Path(scrape_dir)
    if not root.is_dir():
        result["errors"].append(f"刮削输出目录不存在或不可读: {scrape_dir}")
        return result

    # 目录里的视频文件，扣掉已登记的
    with session_scope() as session:
        registered = {
            _norm_path(p) for (p,) in session.execute(
                select(MediaLink.link_path)
            ).all() if p
        }

    pending: list[Path] = []
    try:
        for path in root.rglob("*"):
            try:
                # is_adoptable_video 排除 .strm 与预告片：两者永远配不上源文件，
                # 收进来只会把真正值得纳管的条目淹掉。判据与页面上那个
                # 「N 个未登记」共用，两边数字才对得上
                if not is_adoptable_video(path) or not path.is_file():
                    continue
            except OSError:
                continue
            if _norm_path(str(path)) in registered:
                continue
            pending.append(path)
    except OSError as exc:
        result["errors"].append(f"扫描刮削输出目录失败: {exc}")
        return result

    result["total"] = len(pending)
    if not pending:
        return result

    # 建 (inode, device) → 未登记链接 的索引，等下用下载器给的源文件去命中。
    # 同一 inode 可能有多个链接（刮削建了 -C/-UC 多份），全都要登记 ——
    # Emby 删哪一份都得能反查回源文件
    by_inode: dict[tuple[int, int], list[Path]] = {}
    for path in pending:
        try:
            st = path.stat()
        except OSError as exc:
            # 记进 skipped 而不是静默跳过：静默丢弃会让 total 与
            # adopted+passthrough+unmatched+skipped 对不上（388 = 0+376+2
            # 差 10 条就是这么来的），用户看不出那些文件去哪了
            result["skipped"].append({
                "link_path": str(path), "reason": f"读取文件信息失败: {exc}",
            })
            continue
        if not st.st_ino:
            # Windows 上取不到 inode，配对无从谈起
            result["skipped"].append({
                "link_path": str(path), "reason": "取不到 inode",
            })
            continue
        by_inode.setdefault((st.st_ino, st.st_dev), []).append(path)

    if not by_inode:
        return result

    # 下载器里全部种子内文件 → hash 的映射，一趟拉完。
    #
    # 从前这里是「_all_torrent_files() 取路径」+「find_torrents_by_path()
    # 把 hash 找回来」两趟全量拉取 —— 第一趟内部已经读到过 hash 却只返回
    # 路径，第二趟纯属重建它刚扔掉的信息。种子上千、文件数万时白等一倍
    torrent_map = all_torrent_files_with_hashes()
    source_files = list(torrent_map)
    if not source_files:
        result["errors"].append(
            "下载器里没有取到任何种子文件，无法反查源文件。"
            "请确认下载器已配置且可连通"
        )

    matched: dict[tuple[int, int], str] = {}
    for source in source_files:
        try:
            st = os.stat(source)
        except OSError:
            continue  # 种子在，文件已被删
        if not st.st_ino:
            continue
        key = (st.st_ino, st.st_dev)
        if key in by_inode:
            matched.setdefault(key, source)

    plan: list[dict] = []
    # 配不到源文件的降级方案，与 plan 分开攒：两者登记方式相同，但
    # 前者有种子线索、后者没有，报给用户时必须能分清各是多少条
    passthrough_plan: list[dict] = []

    for key, links in by_inode.items():
        source = matched.get(key)
        for path in links:
            if source is None and not fallback_passthrough:
                result["unmatched"].append({"link_path": str(path)})
                continue

            # 提不出番号一律跳过，直通也不例外。硬造一个 code 会在
            # media_link 里留下永远对不上号的脏记录，而直通登记的
            # code 同样要参与订阅去重与反查，不是可以随便填的字段
            code = find_serial_number(path.stem) or find_serial_number(
                path.parent.name
            )
            if not code:
                result["skipped"].append({
                    "link_path": str(path), "reason": "文件名里提不出番号",
                })
                continue

            if source is None:
                # 直通：自己就是源文件，没有种子
                passthrough_plan.append({
                    "code": code,
                    "link_path": str(path),
                    "source_path": str(path),
                    "torrents": [],
                })
                continue

            plan.append({
                "code": code,
                "link_path": str(path),
                "source_path": source,
                "torrents": torrent_map.get(source) or [],
            })

    result["adopted"] = plan
    result["passthrough"] = passthrough_plan
    if dry_run or not (plan or passthrough_plan):
        return result

    with session_scope() as session:
        for row in plan:
            _register(
                row["code"], row["source_path"], row["link_path"], session=session
            )
            _record_torrents(
                row["code"], row["source_path"], torrent_map, session=session
            )
            mark_completed(row["code"], session=session)
            logger.info(
                f"[{row['code']}] 纳入管理: {row['source_path']} → "
                f"{row['link_path']}，种子 {len(row['torrents'])} 个"
            )

        for row in passthrough_plan:
            # 不调 _record_torrents：没配到源文件就没有种子，
            # 调了只会拿 link_path 去下载器再白查一轮
            _register(
                row["code"], row["source_path"], row["link_path"], session=session
            )
            mark_completed(row["code"], session=session)
            logger.info(
                f"[{row['code']}] 纳入管理（直通，无种子线索）: {row['link_path']}"
            )

    logger.info(
        f"刮削输出目录纳管完成：登记 {len(plan)} 条，"
        f"直通登记 {len(passthrough_plan)} 条，"
        f"未配到源文件 {len(result['unmatched'])} 条，"
        f"跳过 {len(result['skipped'])} 条"
    )
    return result


def _norm_path(path: str) -> str:
    """路径归一化，仅用于比对是否为同一条记录。

    webhook 上报的与磁盘上扫到的写法可能不同（正反斜杠混用、盘符大小写），
    直接比字符串会把已登记的误判成未登记，于是重复登记一遍。
    """
    return str(Path(path)).replace("\\", "/").casefold()


def _register_alias(session, code: str, source_path: str) -> None:
    """哈希 code 记一条「code → 原文件名」，让它重新可读。

    只有走了哈希的 code 才需要 —— 其余 code 本身就是文件名，记了是冗余。

    复用调用方的 session：这是登记流程的一部分，同一事务里成或败。

    交给数据库忽略冲突，不用「先查后插」：同一批事务里可能有多个文件哈希出
    同一个 code（同名文件分散在不同目录），session.get() 查不到自己刚 add
    还没 flush 的对象，commit 时 executemany 撞 code_alias_pkey，那一批
    登记全部回滚。定时同步与手动触发并发跑时同理，两个事务各自都查不到。

    冲突跳过而非覆盖：已有记录与新记录的 filename 同源（都是哈希的原像），
    覆盖没有意义，跳过还省一次写。
    """
    if not is_hashed_code(code):
        return

    insert_ignore_duplicate(session, CodeAlias, [
        {"code": code, "filename": Path(source_path).stem},
    ])


def _register(
    code: str, source_path: str, link_path: str, session=None,
) -> None:
    """写入 media_link。直接落库而不走 register_scrape。

    register_scrape 会扫整个媒体库按 inode 反查硬链接 —— 那是刮削场景下
    「不知道链接在哪」才需要的。这里链接是我们自己刚建的，路径确定，
    扫库纯属浪费（大库上是分钟级）。

    传了 session 就复用，由调用方决定何时提交。批量同步必须这么用 ——
    每个文件一个事务的话，提交本身（SQLite 上一次 fsync 约 18ms）就成了
    大头：几千个文件光提交要好几分钟。
    """
    try:
        st = os.stat(source_path)
        inode, device = (st.st_ino or None), (st.st_dev or None)
    except OSError:
        inode, device = None, None

    def _write(s) -> None:
        existing = s.get(MediaLink, link_path)
        if existing is not None:
            existing.code = code
            existing.source_path = source_path
            existing.inode = inode
            existing.device = device
        else:
            s.add(MediaLink(
                link_path=link_path,
                code=code,
                source_path=source_path,
                inode=inode,
                device=device,
            ))

        _register_alias(s, code, source_path)

    if session is not None:
        _write(session)
        return

    with session_scope() as own:
        _write(own)


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
            # 进度还停在 running 上，收个尾，否则页面会一直显示"同步中"
            progress.finish(rule_id, f"同步异常: {exc}")
            failed = SyncResult(watch_id=rule_id, dry_run=dry_run)
            failed.errors.append(str(exc))
            results.append(failed)
    return results
