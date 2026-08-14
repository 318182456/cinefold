"""媒体联动：刮削登记 与 删除联动。

数据流：

    MDCng 刮削完成 ──webhook──> register_scrape()
        拿到 number + source_path，按 inode 在媒体库里找到硬链接
        写入 media_link 表

    Emby 删除影片 ──webhook──> handle_media_deleted()
        按 link_path / code 反查 media_link
        再按 code 去 history 拿到全部种子 hash（含转种）
        删种（不删文件）→ 删源文件 → 删硬链接 → 删刮削附属 → 清空目录 → 清记录

删除顺序是有讲究的：种子必须先停，否则下载器可能在文件消失后把任务标记为
错误并重新下载；源文件先于硬链接删，是因为源文件才是占空间的那份，
硬链接删掉只减引用计数，源文件不删空间就不会释放。

刮削附属文件（nfo / 海报 / 字幕 / trickplay 等）跟着硬链接一起删，删完再逐级
向上清理空目录。边界严格限定在媒体库根目录内，根目录本身永不删。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Code, CodeStatus, History, MediaLink, PendingDelete
from app.database.session import session_scope
from app.utils import get_true_code

# 媒体文件扩展名。按 inode 扫库时只看这些，避免把 nfo/jpg 也算进去
VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts",
    ".flv", ".rmvb", ".iso", ".mpg", ".mpeg", ".m4v", ".strm",
}

def is_adoptable_video(path: Path) -> bool:
    """这个文件值得纳入删除联动的管辖范围吗。

    「未登记影片」的统计、纳管候选扫描、孤儿一览的未登记扫描三处必须用
    同一条判据 —— 否则页面上说有 256 个未登记，点进去纳管却只列出 100 个，
    用户无从判断哪个数字是真的。

    排除两类看着像影片、实则永远配不上源文件的：

      .strm     只有一行 URL 的文本文件，指向站外资源，压根没有源文件与种子
      预告片    刮削工具生成的附属内容，不是下载来的正片

    调用方仍要自己判断 is_file()：那一步会抛 OSError，各处的兜底方式不同
    （有的记进 skipped，有的静默跳过），不适合塞进这里。
    """
    suffix = path.suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        return False
    if suffix == ".strm":
        return False
    if "trailer" in path.stem.lower():
        return False
    return True


# 刮削产物：Emby/Jellyfin 认得的元数据、图片、字幕。
# 只按扩展名判定还不够 —— 删除范围另外靠"与影片同名前缀"约束（见 _sidecar_files）
SIDECAR_SUFFIXES = {
    # 元数据
    ".nfo", ".xml",
    # 图片（海报、背景、缩略图、剧照）
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tbn",
    # 字幕
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi", ".txt",
}

# 影片所在目录里，与影片不同名但同属这部片子的刮削文件。
# Emby 的目录级图片就叫 poster.jpg / fanart.jpg，没有番号前缀
SIDECAR_STEMS = {
    "poster", "fanart", "banner", "thumb", "landscape", "clearart",
    "clearlogo", "logo", "disc", "discart", "backdrop", "folder",
    "cover", "movie", "season", "keyart", "characterart",
}

# 影片同名的附属目录（Emby 的 trickplay 缩略图、extrafanart 等），整个删掉
SIDECAR_DIR_NAMES = {
    "extrafanart", "extrathumbs", "behind the scenes", "trailers",
    ".actors", "subs", "subtitles",
}


@dataclass
class DeleteResult:
    """一次联动删除的结果，用于日志留痕和接口回执。"""
    code: str = ""
    dry_run: bool = False
    torrents_deleted: list[str] = field(default_factory=list)
    # 合集种子：没删掉，只把这部片的文件标记为不需要，继续做种其余影片
    torrents_kept: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    links_deleted: list[str] = field(default_factory=list)
    # 刮削附属：nfo、海报、字幕、extrafanart 目录等
    sidecars_deleted: list[str] = field(default_factory=list)
    # 清理掉的空目录，自下而上
    dirs_deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "dry_run": self.dry_run,
            "torrents_deleted": self.torrents_deleted,
            "torrents_kept": self.torrents_kept,
            "files_deleted": self.files_deleted,
            "links_deleted": self.links_deleted,
            "sidecars_deleted": self.sidecars_deleted,
            "dirs_deleted": self.dirs_deleted,
            "errors": self.errors,
        }


# ----------------------------------------------------------------------
# 刮削登记
# ----------------------------------------------------------------------
def _stat_ids(path: str) -> tuple[int | None, int | None]:
    """取 (inode, device)。取不到返回 (None, None)。"""
    try:
        st = os.stat(path)
        # Windows 上 st_ino 可能为 0，视为不可用
        return (st.st_ino or None), (st.st_dev or None)
    except OSError as exc:
        logger.warning(f"无法 stat 文件 {path}: {exc}")
        return None, None


def find_hardlinks(source_path: str, library_path: str = "") -> list[str]:
    """在媒体库目录下找出与 source_path 同 inode 的文件。

    硬链接共享 inode，这是权威判据，不依赖刮削工具的命名规则。
    inode 只在同一设备内唯一，所以 device 也要比对。
    """
    settings = get_settings()
    library = library_path or settings.medialink_library_path
    if not library:
        logger.warning("未配置媒体库根目录（MEDIALINK_LIBRARY_PATH），无法反查硬链接")
        return []

    root = Path(library)
    if not root.is_dir():
        logger.warning(f"媒体库根目录不存在或不可读: {library}")
        return []

    inode, device = _stat_ids(source_path)
    if inode is None:
        return []

    try:
        st = os.stat(source_path)
    except OSError:
        return []
    # 链接数为 1 说明没有其它硬链接，不必扫盘
    if getattr(st, "st_nlink", 1) <= 1:
        logger.info(f"源文件无硬链接（st_nlink=1），跳过扫描: {source_path}")
        return []

    source_resolved = str(Path(source_path).resolve())
    found: list[str] = []
    for path in root.rglob("*"):
        try:
            if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
                continue
            pst = path.stat()
            if pst.st_ino != inode or pst.st_dev != device:
                continue
            if str(path.resolve()) == source_resolved:
                continue  # 源文件本身也在库内时排除
            found.append(str(path))
        except OSError:
            continue  # 扫描期间文件被移除或无权限，跳过即可

    if not found:
        logger.info(
            f"未在媒体库找到 {source_path} 的硬链接。"
            f"若刮削产物为复制或软链接，或与源文件不在同一挂载卷，属预期"
        )
    return found


# 入库后可以升到「已入库」的状态。FAILED 不动 —— 失败原因要留着给人看；
# NONE 不动 —— 没订阅过的片子入库了也不该凭空出现在订阅列表里
_COMPLETABLE_STATUS = (
    CodeStatus.SUBSCRIBED, CodeStatus.DOWNLOADING, CodeStatus.DOWNLOADED,
)


def mark_completed(code: str, session=None) -> bool:
    """入库成功后把番号标成「已入库」。返回是否真的改了。

    DOWNLOADED → COMPLETED 之前没有任何自动通路，番号会一直停在「已下载」。
    DOWNLOADING 也一并接受，是为了救「种子已从下载器删除」的情况：
    sync_download_status 只认下载器里还在的任务，种子一删番号就永久卡在
    「下载中」，只剩入库这一个出口。

    code 来路不一：webhook 是真番号，watchdir 是文件名派生的。这里统一过
    get_true_code 再查，否则 "abp984" 找不到 "ABP-984" 那一行，而
    session.get 查不到只会静默返回 None，问题不会有任何声响。

    session 由调用方传入时复用其事务（watchdir 批量登记走这条），不传则
    自开一个。
    """
    true_code = get_true_code(code)
    if not true_code:
        return False

    def _apply(sess) -> bool:
        row = sess.get(Code, true_code)
        if row is None or row.status not in _COMPLETABLE_STATUS:
            return False
        row.status = CodeStatus.COMPLETED
        row.update_time = datetime.now()
        logger.info(f"[{true_code}] 已入库，订阅状态更新为「已入库」")
        return True

    if session is not None:
        return _apply(session)
    with session_scope() as own:
        return _apply(own)


def register_scrape(
    code: str, source_path: str, link_path: str = ""
) -> list[str]:
    """登记一次刮削结果。返回写入的 link_path 列表。

    link_path 由调用方给出时（webhook 模板拼好的快速路径）优先采用，
    同时仍按 inode 扫一遍补全，两者取并集 —— 命名规则改动后模板会失准，
    inode 不会。
    """
    if not code or not source_path:
        logger.warning("刮削登记缺少番号或源文件路径，已忽略")
        return []

    inode, device = _stat_ids(source_path)
    candidates: list[str] = []

    if link_path:
        # 模板给的路径要验一下确实指向同一份数据，否则宁可不要
        l_inode, l_device = _stat_ids(link_path)
        if l_inode is not None and (l_inode, l_device) == (inode, device):
            candidates.append(link_path)
        else:
            logger.warning(
                f"[{code}] webhook 给出的 link_path 与源文件不是同一份数据，"
                f"已丢弃并改用 inode 扫描: {link_path}"
            )

    candidates.extend(find_hardlinks(source_path))

    # 去重，保序
    seen: set[str] = set()
    unique = [p for p in candidates if not (p in seen or seen.add(p))]
    if not unique:
        return []

    with session_scope() as session:
        for path in unique:
            # 同一 link_path 重复刮削时覆盖，保持 source_path 为最新
            existing = session.get(MediaLink, path)
            if existing is not None:
                existing.code = code
                existing.source_path = source_path
                existing.inode = inode
                existing.device = device
            else:
                session.add(MediaLink(
                    link_path=path,
                    code=code,
                    source_path=source_path,
                    inode=inode,
                    device=device,
                ))

    logger.info(f"[{code}] 已登记 {len(unique)} 条硬链接关联")
    mark_completed(code)
    return unique


# ----------------------------------------------------------------------
# 删除联动
# ----------------------------------------------------------------------
def _lookup_links(link_path: str = "", code: str = "") -> list[MediaLink]:
    """按链接路径或番号反查关联记录。"""
    with session_scope() as session:
        if link_path:
            row = session.get(MediaLink, link_path)
            if row is not None:
                return [row]
            # Emby 报的路径可能与登记时的分隔符或大小写不一致，退回按名字匹配
            name = Path(link_path).name
            if name:
                rows = session.scalars(
                    select(MediaLink).where(MediaLink.link_path.like(f"%{name}"))
                ).all()
                if rows:
                    logger.info(f"link_path 精确匹配失败，按文件名匹配到 {len(rows)} 条")
                    return list(rows)
        if code:
            return list(session.scalars(
                select(MediaLink).where(MediaLink.code == code)
            ).all())
    return []


def _norm_path(path: str) -> str:
    """路径归一化，用于比对。与 watchdir._norm_path 同一口径。

    登记时的写法与 Emby 报来的写法可能在正反斜杠、盘符大小写上不一致，
    直接比字符串会把匹配得上的判成匹配不上。
    """
    return str(Path(path)).replace("\\", "/").casefold()


def lookup_links_under_dir(dir_path: str) -> list[str]:
    """反查某个目录下登记过的全部影片链接路径。

    用途是区分 Emby 两种「路径指向目录」的删除回调：

      1. 一部影片就是一个目录（DVD 目录结构、合集里每部片各占一个子目录）。
         Emby 把整个目录当 Item 删掉，路径合法、内容真实，联动必须照走，
         否则种子里对应的文件永远不会被标记为不需要，下载器还会把它下回来。
      2. 影片删完后空掉的演员 / 系列 / 年份目录，Emby 会各补一条回调。
         这类目录在库里从来没有关联记录。

    磁盘上两者已无法区分 —— 回调到达时 Emby 都删完了，要么不存在要么是空的。
    但库里能区分：第 1 种查得到子路径记录，第 2 种查不到。

    返回空列表即代表「第 2 种」，调用方据此静默忽略。
    """
    if not dir_path:
        return []

    # 前缀比对必须两边同口径：库里存的是登记那一刻的写法，Emby 报来的是
    # 它自己的写法，正反斜杠与盘符大小写都可能不同（与 watchdir._norm_path
    # 同一问题）。只归一化查询侧、拿去跟原样的 link_path 做 SQL like，
    # Windows 上登记的记录会一条都匹配不上。
    #
    # 所以 like 只用来粗筛：目录名本身不含分隔符，大小写之外两边写法一致，
    # 用它把候选集缩到这个目录相关的若干条。真正的前缀判定在 Python 侧对
    # 归一化后的两边做。
    prefix = _norm_path(dir_path).rstrip("/") + "/"

    # 目录名里的 like 通配符要转义，否则片名里的 _ 会变成「任意单字符」
    name = Path(dir_path.replace("\\", "/").rstrip("/")).name
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    with session_scope() as session:
        rows = list(session.scalars(
            select(MediaLink).where(
                MediaLink.link_path.like(f"%{escaped}%", escape="\\")
            )
        ).all())

    # 结尾补分隔符，避免 ".../Lesson 1" 误命中 ".../Lesson 10/xxx.mkv"；
    # 只要影片本体，目录下的 nfo / 图片有它们各自的清理路径，不从这里进
    return [
        row.link_path for row in rows
        if _norm_path(row.link_path).startswith(prefix)
        and Path(row.link_path).suffix.lower() in VIDEO_SUFFIXES
    ]


def _torrent_hashes(code: str) -> list[str]:
    """取该番号的全部种子 hash。转种场景下会有多条。"""
    with session_scope() as session:
        return list(session.scalars(
            select(History.hash).where(History.code == code)
        ).all())


# 下载器查询的批内缓存。
#
# 为什么必须有：find_torrents_by_path 与 list_torrent_files 的成本都与
# 「下载器里的种子总数」成正比而与查询量无关 —— 两者都得把每个种子的文件
# 清单拉一遍才能回答问题。单条删除要调它们各一次，批量删 20 条就是 40 次
# 全量拉取，种子上千时每次几秒，用户盯着转圈等好几分钟。
#
# 刻意做成「显式开启的作用域」而不是全局 TTL 缓存：
# 陈旧的种子清单会让删除范围出错（多删或少删文件），这个代价太大，不能靠
# 「时间还没到」来保证正确性。只有调用方明确知道「接下来这批操作可以共用
# 一份快照」时才开启 —— 批量删除就是这种场景，整批在几秒内跑完，期间下载器
# 的状态变化只会是我们自己删掉的那些种子。
#
# 不开启时行为与优化前完全一致：每次都问下载器要最新的。
_batch_cache: dict | None = None


class torrent_batch:
    """在这个作用域内共用一份下载器查询结果。

    用法：
        with torrent_batch():
            for path in paths:
                handle_media_deleted(link_path=path)

    嵌套安全（内层不会提前清掉外层的缓存），但不是线程安全的 ——
    批量删除本就串行执行，够用。
    """

    def __enter__(self):
        global _batch_cache
        self._owner = _batch_cache is None
        if self._owner:
            _batch_cache = {"files": {}, "paths": None, "all_paths": None}
        return self

    def preload_paths(self, paths: list[str]) -> None:
        """告知整批会用到的源文件路径。

        按路径反查的成本与查询量无关（每次都要把全部种子的清单拉一遍建
        索引），所以一次问完整批比逐条问便宜得多 —— 逐条问是 N 轮全量拉取，
        一次问完是 1 轮。
        """
        if _batch_cache is not None and paths:
            _batch_cache["all_paths"] = list(paths)

    def __exit__(self, *exc):
        global _batch_cache
        if self._owner:
            _batch_cache = None
        return False


def _files_for_hashes(hashes: list[str]) -> dict[str, list[str]]:
    """取这些种子的文件清单。批作用域内只问下载器一次。

    必须按传入的 hash 查而不是枚举下载器全部种子：History 里的 hash 可能
    已经不在 monitor_torrent 的返回里（种子被删了但记录还在），枚举法会
    查不到它的清单，删除范围就缩水成「只删登记的那个文件」，种子里的
    样品图、说明 txt 全都留下。

    逐个种子问而不是一次问全部：合集判定要数每个种子内的正片数，清单混成
    一个列表后，两个单片种子加起来也有 2 部正片，会被误判成合集。
    """
    cached = _batch_cache["files"] if _batch_cache is not None else {}
    todo = [h for h in hashes if h.lower() not in cached]

    if todo:
        from app.modules.downloadclient import (
            get_download_client, list_configured_clients,
        )

        for name in list_configured_clients():
            client = get_download_client(name)
            if client is None:
                continue
            lister = getattr(client, "list_torrent_files", None)
            if lister is None:
                continue  # 老客户端未实现该接口，跳过即可
            for h in todo:
                try:
                    paths = [p for p in lister([h]) if p]
                except Exception as exc:
                    logger.warning(f"{name} 读取种子 {h} 文件清单异常: {exc}")
                    continue
                if not paths:
                    continue
                bucket = cached.setdefault(h.lower(), [])
                bucket.extend(p for p in paths if p not in bucket)

    return {h: list(cached[h.lower()]) for h in hashes if h.lower() in cached}


def _torrent_hashes_by_path(paths: set[str]) -> list[str]:
    """按源文件路径向下载器反查种子 hash。

    这是 code → History → hash 之外的第二条路，补的是这个缺口：手动放进
    监控目录的文件，code 是从文件名生成的，History 里没有对应记录，
    按 code 一个种子都查不到 —— 但文件确实是某个种子下载下来的，
    下载器手里有「这个文件属于哪个种子」的答案。

    两条路取并集：History 覆盖 cinefold 自己下载的，反查覆盖其余的。

    批作用域内只反查一次：find_torrents_by_path 每次调用都要把下载器里每个
    种子的文件清单拉一遍才能建索引，成本与种子总数成正比而与查询路径数无关。
    批量删 20 条就是 20 轮全量拉取，种子上千时要等好几分钟。作用域内改成
    一次性反查全部待删路径，之后各条删除只是查表。
    """
    if not paths:
        return []

    from app.modules.downloadclient import find_torrents_by_path

    # 批作用域内：第一次就把整批的路径一次问完，后续直接查表
    if _batch_cache is not None:
        index = _batch_cache.get("paths")
        if index is None:
            targets = _batch_cache.get("all_paths") or sorted(paths)
            try:
                index = find_torrents_by_path(targets)
            except Exception as exc:
                logger.warning(f"按路径反查种子失败: {exc}")
                index = {}
            _batch_cache["paths"] = index
        found: list[str] = []
        for raw in sorted(paths):
            for h in index.get(raw, []):
                if h not in found:
                    found.append(h)
        return found

    try:
        mapping = find_torrents_by_path(sorted(paths))
    except Exception as exc:
        logger.warning(f"按路径反查种子失败: {exc}")
        return []

    found = []
    for hashes in mapping.values():
        for h in hashes:
            if h not in found:
                found.append(h)
    return found


def _delete_file(
    path: str, result: DeleteResult, expected_links: int = 0
) -> None:
    """删除一个文件。

    expected_links 非 0 时启用硬链接保护：文件的链接数超过这个值，说明除了
    我们即将删掉的那些链接之外，还有别处引用同一份数据（另一个媒体库、
    手工建的链接、别的整理工具），此时不删源文件 —— 删了那些引用会全部
    变成坏文件，而空间根本不会释放（引用计数没到 0）。

    传 0 表示不做这项检查，用于删硬链接本身。
    """
    try:
        p = Path(path)
        if not p.exists():
            logger.info(f"文件已不存在，跳过: {path}")
            return

        if expected_links:
            try:
                nlink = p.stat().st_nlink
            except OSError:
                nlink = 0
            # Windows 上 st_nlink 可能为 0/1 不可信，取到 0 时跳过检查
            if nlink and nlink > expected_links:
                msg = (
                    f"源文件还有 {nlink - expected_links} 处其它硬链接引用，"
                    f"未删除以免破坏它们: {path}"
                )
                logger.warning(msg)
                result.errors.append(msg)
                return

        p.unlink()
        result.files_deleted.append(path)
        logger.info(f"已删除文件: {path}")
    except OSError as exc:
        msg = f"删除文件失败 {path}: {exc}"
        logger.error(msg)
        result.errors.append(msg)


def _library_root() -> Path | None:
    """媒体库根目录，解析成绝对路径。未配置或不存在时返回 None。

    空目录清理的所有边界判断都以它为准，拿不到就一律不清目录 ——
    宁可留下空壳，也不能在库外乱删。
    """
    library = get_settings().medialink_library_path
    if not library:
        return None
    try:
        root = Path(library).resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def _within(path: Path, root: Path) -> bool:
    """path 是否严格位于 root 之内（不含 root 自身）。"""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved == root:
        return False
    return root in resolved.parents


def _sidecar_files(video: Path) -> tuple[list[Path], list[Path]]:
    """列出影片对应的刮削附属文件与附属目录。

    命中规则，三者取并集：
      1. 与影片同名 —— ABS-001.nfo / ABS-001-fanart.jpg / ABS-001.zh.srt
      2. 目录级图片 —— poster.jpg / folder.jpg，Emby 不带番号前缀
      3. 附属目录 —— extrafanart/ .actors/ 以及 ABS-001.trickplay/

    第 1 条不能用裸前缀匹配：ABS-0011.nfo 也以 "ABS-001" 开头，那是另一部
    片子。名字要么与影片主名完全相同，要么其后紧跟 . 或 - 分隔符。

    第 2 条只在"该目录下已无其它影片"时才算数，否则会误删同目录另一部片子
    共用的 poster.jpg。判断放在调用处（_cleanup_sidecars）。
    """
    parent = video.parent
    stem = video.stem.lower()
    files: list[Path] = []
    dirs: list[Path] = []

    def belongs(name: str) -> bool:
        """name（不含扩展名的部分也可能带后缀）是否属于这部影片。"""
        if not name.startswith(stem):
            return False
        rest = name[len(stem):]
        # 完全同名，或后面紧跟分隔符：ABS-001-poster / ABS-001.zh
        return rest == "" or rest[0] in ".-"

    try:
        entries = list(parent.iterdir())
    except OSError as exc:
        logger.warning(f"无法列出目录 {parent}: {exc}")
        return [], []

    for entry in entries:
        name = entry.name.lower()
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue

        if is_dir:
            # ABS-001.trickplay / ABS-001-extrafanart 这类同名附属目录
            if name in SIDECAR_DIR_NAMES or belongs(name):
                dirs.append(entry)
            continue

        if entry == video or entry.suffix.lower() not in SIDECAR_SUFFIXES:
            continue
        # ABS-001.nfo、ABS-001-poster.jpg、ABS-001.zh.srt 都以影片主名打头。
        # 比 stem 而非全名：ABS-001.zh.srt 的 stem 是 "ABS-001.zh"
        if belongs(entry.stem.lower()):
            files.append(entry)

    return files, dirs


def _other_videos(directory: Path, exclude: set[Path]) -> bool:
    """目录里除 exclude 外是否还有别的影片文件。"""
    try:
        for entry in directory.iterdir():
            if entry.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            if not entry.is_file() or entry in exclude:
                continue
            return True
    except OSError:
        return True  # 读不了就当有，宁可少删
    return False


def _remove_tree(path: Path, result: DeleteResult) -> None:
    """整棵删掉附属目录（extrafanart、trickplay 之类，里面全是刮削产物）。"""
    import shutil

    try:
        shutil.rmtree(path)
        result.sidecars_deleted.append(str(path))
        logger.info(f"已删除附属目录: {path}")
    except OSError as exc:
        msg = f"删除附属目录失败 {path}: {exc}"
        logger.error(msg)
        result.errors.append(msg)


def _cleanup_sidecars(videos: list[str], result: DeleteResult) -> None:
    """删除影片对应的刮削配置、图片、字幕。

    videos 是刚被删掉的硬链接路径。文件此时已不在，但路径信息仍然有效。
    """
    root = _library_root()
    if root is None:
        logger.warning("未配置媒体库根目录，跳过刮削附属文件清理")
        return

    removed = {Path(v) for v in videos}
    for raw in videos:
        video = Path(raw)
        if not _within(video, root):
            logger.warning(f"硬链接不在媒体库根目录内，跳过附属清理: {raw}")
            continue

        files, dirs = _sidecar_files(video)
        for f in files:
            try:
                f.unlink()
                result.sidecars_deleted.append(str(f))
                logger.info(f"已删除刮削附属: {f}")
            except OSError as exc:
                msg = f"删除刮削附属失败 {f}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
        for d in dirs:
            _remove_tree(d, result)

        # 目录级图片（poster.jpg 等）只在这个目录已经没有别的影片时才删，
        # 否则会连带删掉同目录另一部片子的封面
        parent = video.parent
        if _other_videos(parent, removed):
            continue
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if entry.suffix.lower() not in SIDECAR_SUFFIXES:
                continue
            if entry.stem.lower() not in SIDECAR_STEMS:
                continue
            try:
                entry.unlink()
                result.sidecars_deleted.append(str(entry))
                logger.info(f"已删除目录级刮削文件: {entry}")
            except OSError as exc:
                msg = f"删除目录级刮削文件失败 {entry}: {exc}"
                logger.error(msg)
                result.errors.append(msg)


def _prune_empty_dirs(videos: list[str], result: DeleteResult) -> None:
    """自下而上删掉空目录，止于媒体库根目录。

    番号目录空了就删，父目录（演员名/厂牌/年份）跟着空了继续往上删，
    但根目录本身无论多空都保留 —— 删了 Emby 会认为整个库掉线。
    """
    root = _library_root()
    if root is None:
        logger.warning("未配置媒体库根目录，跳过空目录清理")
        return

    # 深的目录排前面，保证子目录先于父目录被处理
    candidates = sorted(
        {Path(v).parent for v in videos},
        key=lambda p: len(p.parts),
        reverse=True,
    )

    for start in candidates:
        current = start
        while _within(current, root):
            # 刮削输出目录（如 <库根>/日本AV）不能删：库根常设成上一层，
            # 这个分类目录一旦空了就会被往上删掉，Emby 会认为媒体库掉线
            if _is_download_root(current):
                logger.info(f"目录受保护，停止向上清理: {current}")
                break
            try:
                if any(current.iterdir()):
                    break  # 还有内容，本条链到此为止
                current.rmdir()
            except OSError as exc:
                # 目录已被上一轮删掉时不算错误
                if not current.exists():
                    break
                msg = f"删除空目录失败 {current}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
                break
            result.dirs_deleted.append(str(current))
            logger.info(f"已删除空目录: {current}")
            current = current.parent


def _torrent_files_by_hash(hashes: list[str]) -> dict[str, list[str]]:
    """向下载器要这些种子的文件清单，按种子分组。

    这是删源文件侧最可靠的依据：种子里有什么，下载器最清楚，不用去猜
    「下载目录里哪些文件属于这部片子」。多下载器都问一遍，取并集。

    必须在删种之前调用 —— 种子一删，清单就查不到了。

    逐个种子问而不是一次问全部：合集判定要数每个种子内的正片数，清单混成
    一个列表后，两个单片种子加起来也有 2 部正片，会被误判成合集。
    """
    if not hashes:
        return {}

    # 走带缓存的查询：同一批删除里重复出现的 hash 只问下载器一次
    out = _files_for_hashes(hashes)

    if out:
        logger.info(
            f"从下载器取到 {sum(len(v) for v in out.values())} 个种子内文件"
            f"（{len(out)} 个种子）"
        )
    return out


def _is_feature_video(path: str) -> bool:
    """这个文件是否算一部「正片」。

    合集判定要数正片数量，不能只看扩展名 —— 预告片、样品视频、片头动画都是
    视频文件，把它们数进去会让单片种子被误判成合集，白白缩小删除范围，
    留下一堆该删没删的垃圾文件。体积是最省事也最可靠的区分方式。

    取不到体积（文件已不在）时按正片算：宁可判成合集少删一点，也不要
    把合集误判成单片、连带删掉别的影片。
    """
    if Path(path).suffix.lower() not in VIDEO_SUFFIXES:
        return False

    floor = max(0, int(get_settings().medialink_feature_min_mb or 0)) * 1024 * 1024
    if floor <= 0:
        return True
    try:
        return Path(path).stat().st_size >= floor
    except OSError:
        return True


def _plan_torrent_deletion(
    hashes: list[str], registered_sources: set[str]
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """决定每个种子该整个删掉，还是只把这部片的文件标记为不需要。

    背景：种子的完整清单本来照单全删。对「一部影片 + 样品图 + 说明 txt」的
    普通种子这是对的 —— 那些附属文件正是想删的。但对合集种子（一个种子打包
    几十部片，各占一个番号子目录）就成了灾难：Emby 里删一部片，会连带删掉
    同种子里其余几十部，而且整个种子被移除后，那几十部还全都停止做种。

    判据用「种子内有几部正片」而不是目录结构。曾考虑过按目录划边界，
    但实测发现大量单文件种子的文件平铺在下载根目录下（同一目录里躺着
    几百部互不相关的片子），按目录反而会把范围扩大到整个下载目录。

    返回 (要删掉的种子, 要保留做种的合集种子, 该删的文件按种子分组)。
    合集种子只删登记那部及其同目录附属 —— 合集里每部片各有自己的番号目录，
    这样刚好留下该片的正片与它的 nfo/poster/字幕。
    """
    per_hash = _torrent_files_by_hash(hashes)
    guard = get_settings().medialink_collection_guard

    to_delete: list[str] = []
    to_keep: list[str] = []
    files: dict[str, list[str]] = {}

    for h in hashes:
        paths = per_hash.get(h) or []
        if not paths:
            # 清单查不到（种子已不在、下载器离线）：按原样整个删，
            # 没有清单也就没有「误删同种子其它影片」的风险
            to_delete.append(h)
            continue

        features = [p for p in paths if _is_feature_video(p)]
        if not guard or len(features) <= 1 or not registered_sources:
            to_delete.append(h)
            files[h] = paths
            continue

        # 登记源文件所在目录即该片的范围
        keep_dirs = {Path(p).parent for p in registered_sources}
        mine, others = [], []
        for path in paths:
            parent = Path(path).parent
            if any(parent == d or d in parent.parents for d in keep_dirs):
                mine.append(path)
            else:
                others.append(path)

        if not others:
            # 整个种子都属于这部片，照常删
            to_delete.append(h)
            files[h] = paths
            continue

        # 登记的源文件必须在删除名单里 —— 它们是本次删除的正主
        for p in registered_sources:
            if p in paths and p not in mine:
                mine.append(p)

        to_keep.append(h)
        files[h] = mine
        left = len(features) - len([p for p in mine if _is_feature_video(p)])
        logger.warning(
            f"种子 {h} 含 {len(features)} 部正片（合集），保留做种：只删这部片的 "
            f"{len(mine)} 个文件并在下载器里标记为不需要，"
            f"其余 {left} 部影片的 {len(others)} 个文件不动。"
            f"如需恢复旧行为（整包全删）设 MEDIALINK_COLLECTION_GUARD=false"
        )

    return to_delete, to_keep, files


def _is_download_root(path: Path) -> bool:
    """path 是否为某个下载器配置的下载根目录（或其祖先）。

    下载根装着所有任务，删掉下载器就瘸了。种子文件直接散落在下载根下时
    （多文件种子未建自己的子目录），公共路径会算出下载根，必须挡住。

    顺带把监控目录也算进保护范围 —— 那些目录是用户明确配置的，同样不该
    因为一次删除而消失。
    """
    settings = get_settings()
    guarded: set[Path] = set()

    for raw in (
        settings.qbittorrent_download_path,
        settings.transmission_download_path,
        # 刮削输出目录：整个分类目录空了也不该删，Emby 会认为媒体库掉线
        settings.medialink_scrape_dir,
    ):
        if raw:
            try:
                guarded.add(Path(raw).resolve())
            except OSError:
                continue

    # 监控目录同样受保护
    try:
        from app.database.models import WatchDir
        with session_scope() as session:
            for row in session.scalars(select(WatchDir.source_dir)).all():
                if row:
                    try:
                        guarded.add(Path(row).resolve())
                    except OSError:
                        continue
    except Exception as exc:
        # 表还没建（首次启动）或库不可用时，仅依赖下载器配置
        logger.debug(f"读取监控目录保护名单失败: {exc}")

    if not guarded:
        return False

    try:
        resolved = path.resolve()
    except OSError:
        return False

    # path 本身是保护目录，或是它的祖先（更靠上，更不能删）
    for root in guarded:
        if resolved == root or resolved in root.parents:
            return True
    return False


def _prune_torrent_dirs(torrent_files: list[str], result: DeleteResult) -> None:
    """清掉种子建的任务目录（如果它空了）。

    只处理种子自己那一层，**不向上递归**。再往上是下载器的组织结构
    （下载根、分类目录），里面装着别的任务，不归这次删除管 —— 而且源文件
    此刻已删，"目录看起来空了"根本不能作为可删的证据。

    任务目录怎么认：种子全部文件的公共路径。多文件种子（`任务名/xxx.mp4`
    加 `任务名/subs/`）算出来就是任务目录本身；单文件种子只有一个文件，
    公共路径落在文件上，说明种子没建目录，跳过 —— 那一层是下载根。
    """
    dirs = {Path(p).parent for p in torrent_files}
    if not dirs:
        return

    try:
        common = Path(os.path.commonpath([str(p) for p in torrent_files]))
    except ValueError:
        return  # 跨盘符，没有公共路径，放弃

    # commonpath 落在文件上（单文件种子）说明种子没建目录，无事可做
    if common in {Path(p) for p in torrent_files}:
        return

    # common 可能就是下载根目录本身：多文件种子若没建自己的子目录
    # （文件全散在下载根下），公共路径算出来就是下载根。那一层装着别的任务，
    # 删掉会让下载器整个失效，必须挡住。
    if _is_download_root(common):
        logger.info(f"种子文件直接位于下载根目录，不清理目录: {common}")
        return

    # common 就是任务目录。自下而上删空目录，删到 common 自己为止
    candidates = sorted(
        {d for d in dirs if d == common or common in d.parents} | {common},
        key=lambda p: len(p.parts),
        reverse=True,
    )

    def _rmdir_if_empty(path: Path) -> None:
        # 逐个再挡一次：候选目录里可能混着受保护的路径
        if _is_download_root(path):
            logger.info(f"目录受保护（下载根/监控目录），不删: {path}")
            return
        try:
            if any(path.iterdir()):
                return
            path.rmdir()
        except OSError as exc:
            if path.exists():
                msg = f"删除种子空目录失败 {path}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
            return
        result.dirs_deleted.append(str(path))
        logger.info(f"已删除种子空目录: {path}")

    for path in candidates:
        _rmdir_if_empty(path)


def _preview_cleanup(videos: list[str]) -> tuple[list[str], list[str]]:
    """演练模式：算出附属文件与空目录会删掉哪些，不动磁盘。

    影片此刻还在，所以目录"删完后是否为空"要靠推算：把即将删掉的影片、
    附属文件、附属目录从目录内容里扣掉，剩下为空才算会被清理。
    """
    root = _library_root()
    if root is None:
        return [], []

    doomed: set[Path] = {Path(v) for v in videos}
    sidecars: list[str] = []

    for raw in videos:
        video = Path(raw)
        if not _within(video, root):
            continue
        files, dirs = _sidecar_files(video)
        for p in files + dirs:
            if p not in doomed:
                doomed.add(p)
                sidecars.append(str(p))

        parent = video.parent
        if _other_videos(parent, doomed):
            continue
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if entry.suffix.lower() not in SIDECAR_SUFFIXES:
                continue
            if entry.stem.lower() not in SIDECAR_STEMS:
                continue
            if entry not in doomed:
                doomed.add(entry)
                sidecars.append(str(entry))

    # 自下而上推算哪些目录会空
    dirs_removed: set[Path] = set()
    candidates = sorted(
        {Path(v).parent for v in videos},
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for start in candidates:
        current = start
        while _within(current, root):
            try:
                remaining = [
                    e for e in current.iterdir()
                    if e not in doomed and e not in dirs_removed
                ]
            except OSError:
                break
            if remaining:
                break
            dirs_removed.add(current)
            current = current.parent

    ordered = sorted(dirs_removed, key=lambda p: len(p.parts), reverse=True)
    return sidecars, [str(p) for p in ordered]


def handle_media_deleted(
    link_path: str = "", code: str = "", dry_run: bool = False
) -> DeleteResult:
    """媒体服务器删除影片后的联动清理。

    dry_run 为真时只查询并记录会删什么，不实际动手。
    """
    settings = get_settings()
    result = DeleteResult(dry_run=dry_run)

    links = _lookup_links(link_path=link_path, code=code)
    if not links:
        logger.warning(
            f"未找到关联记录，不做任何删除。link_path={link_path!r} code={code!r}"
        )
        result.errors.append("未找到关联记录")
        return result

    result.code = code or links[0].code
    source_paths = {row.source_path for row in links if row.source_path}
    link_paths = [row.link_path for row in links]

    # 查种子有两条路，取并集：
    #   1. code → History → hash    cinefold 自己下载的走这条
    #   2. 源文件路径 → 下载器 → hash  手动放进监控目录的走这条（code 对不上）
    hashes = _torrent_hashes(result.code)
    for h in _torrent_hashes_by_path(source_paths):
        if h not in hashes:
            hashes.append(h)

    # 种子里的文件也算源文件。必须赶在删种之前问，种子删了清单就没了。
    # 登记的 source_path 只有影片一个文件，种子里的样品图、说明 txt 得靠这个补全
    #
    # 合集种子（一包几十部片）只删这部片那几个文件，种子留着继续做种其余影片
    hashes_to_delete, hashes_to_keep, files_by_hash = _plan_torrent_deletion(
        hashes, {row.source_path for row in links if row.source_path}
    )
    torrent_files: list[str] = []
    for paths in files_by_hash.values():
        for p in paths:
            if p not in torrent_files:
                torrent_files.append(p)
    source_paths.update(torrent_files)

    logger.warning(
        f"[{result.code}] 媒体联动删除{'（演练）' if dry_run else ''} —— "
        f"删种 {len(hashes_to_delete)} 个: {', '.join(hashes_to_delete) or '无'}; "
        f"保留做种 {len(hashes_to_keep)} 个: {', '.join(hashes_to_keep) or '无'}; "
        f"源文件 {len(source_paths)} 个: {', '.join(source_paths) or '无'}; "
        f"硬链接 {len(link_paths)} 个: {', '.join(link_paths)}"
    )

    if not dry_run and not settings.medialink_delete_enabled:
        logger.warning(
            f"[{result.code}] 联动删除未启用（MEDIALINK_DELETE_ENABLED=false），"
            f"仅记录不执行"
        )
        result.dry_run = True
        dry_run = True

    if dry_run:
        result.torrents_deleted = list(hashes_to_delete)
        result.torrents_kept = list(hashes_to_keep)
        result.files_deleted = sorted(source_paths)
        result.links_deleted = link_paths
        result.sidecars_deleted, result.dirs_deleted = _preview_cleanup(link_paths)
        return result

    # 1) 处理种子。转种下同一文件对应多个种子，逐个按计划处理；
    #    文件一律不交给下载器删（转种时保存路径可能不一致，容易误删漏删）
    from app.modules.downloadclient import get_download_client, list_configured_clients

    for name in list_configured_clients():
        client = get_download_client(name)
        if client is None:
            continue

        if hashes_to_delete:
            try:
                deleted = client.delete_torrent(hashes_to_delete, delete_files=False)
                result.torrents_deleted.extend(deleted)
            except Exception as exc:
                msg = f"{name} 删种异常: {exc}"
                logger.error(msg)
                result.errors.append(msg)

        # 合集种子：只把这部片的文件标记为不需要，种子继续做种其余影片。
        # 不标记的话下载器会发现文件缺失，重新把它下回来
        for h in hashes_to_keep:
            unwant = getattr(client, "unwant_torrent_files", None)
            if unwant is None:
                continue  # 该下载器未实现（迅雷），只能放着
            try:
                marked, remaining = unwant(h, files_by_hash.get(h) or [])
            except Exception as exc:
                msg = f"{name} 标记种子 {h} 文件不需要异常: {exc}"
                logger.error(msg)
                result.errors.append(msg)
                continue

            if not marked:
                continue
            if remaining:
                result.torrents_kept.append(h)
                continue

            # 全部文件都标记成不需要了：种子成了空壳，做不了种也占着任务位，
            # 直接删掉。这种情况出现在合集里的片子被逐部删完之后
            try:
                deleted = client.delete_torrent([h], delete_files=False)
                result.torrents_deleted.extend(deleted)
                logger.info(f"种子 {h} 已无任何需要的文件，删除该任务")
            except Exception as exc:
                msg = f"{name} 删除空壳种子 {h} 异常: {exc}"
                logger.error(msg)
                result.errors.append(msg)

    # 2) 删源文件（占空间的那份），含种子里的全部文件。
    #
    # 硬链接保护：此刻硬链接还没删（第 3 步才删），所以源文件的链接数应当是
    #   1（源文件自己）+ 本次要删的链接数
    # 超出这个数，说明还有别处引用同一份数据 —— 另一个媒体库、手工建的链接、
    # 别的整理工具。那种情况下删源文件既释放不了空间（引用计数不到 0），
    # 又会让那些引用无从追溯，所以宁可留着。
    #
    # 未登记的种子内文件（样品图、说明 txt）也要过一遍检查，只是期望值是 1：
    # 它们本该没有任何硬链接，nlink 超过 1 说明别处正引用着同一份数据 ——
    # 另一个媒体库、别的整理工具。合集种子里这类文件可能是别的影片的附属，
    # 删掉会破坏那边的引用。多一次 stat 换掉这个风险是值得的
    #
    # 直通模式（link_path == source_path，没有真的建过硬链接）要把自己排除掉，
    # 否则期望值算成 2 而实际 nlink 只有 1 —— 检查虽不会误拦（只拦超出的），
    # 但语义上该文件的合法引用数就是 1
    registered_sources = {row.source_path for row in links if row.source_path}
    # 直通登记：同一条记录里 link_path 与 source_path 指向同一个文件，
    # 说明压根没建过硬链接。按记录逐条比对，不拿两个集合求交 ——
    # 后者会把「A 的源文件恰好是 B 的链接」这种交叉情况也算进来
    passthrough_paths = {
        row.link_path for row in links if row.link_path == row.source_path
    }
    allowed_links = 1 + len(link_paths) - len(passthrough_paths)
    for path in sorted(source_paths):
        expected = allowed_links if path in registered_sources else 1
        _delete_file(path, result, expected_links=expected)

    # 种子文件删完，任务目录多半空了。只清种子自己那棵目录树，
    # 边界取种子文件的公共父目录 —— 下载根目录下还有别的任务，不能往上爬
    if torrent_files:
        _prune_torrent_dirs(torrent_files, result)

    # 3) 删硬链接。
    #    直通模式下 link_path 就是刚在第 2 步删掉的那个源文件，跳过 ——
    #    再走一遍只会在日志里刷「文件已不存在」，并把同一路径重复计一次
    for path in link_paths:
        if path in passthrough_paths:
            result.links_deleted.append(path)
            continue
        _delete_file(path, result)
        result.links_deleted.append(path)

    # 4) 删刮削附属（nfo / 海报 / 字幕 / extrafanart），再清掉空掉的目录。
    #    必须排在删硬链接之后：_other_videos 靠"目录里还剩没剩影片"判断能否
    #    删目录级 poster.jpg，影片还在时会误判成"还有别的片子"
    _cleanup_sidecars(link_paths, result)
    _prune_empty_dirs(link_paths, result)

    # 5) 清关联记录、扣留观察与下载历史
    with session_scope() as session:
        for path in link_paths:
            row = session.get(MediaLink, path)
            if row is not None:
                session.delete(row)
            # 扣留观察一并撤销。宽限期的用处是分辨「移动/改名」和「真删除」——
            # 文件消失后等一段时间，看它会不会以同 inode 在别处出现。删除既然
            # 已经执行完，这个悬念就没有了，留着扣留有两个害处：页面上会显示
            # 一条「正在观察、即将删除」但实际永远不删，且要等下一轮对账的
            # _prune_holds 扫到「关联记录没了」才被动清掉。
            #
            # watchdir 自己走完删除后都会 _clear_hold，只有 webhook 这条入口
            # 没清 —— 它在本模块，而 watchdir 反过来 import 本模块，直接调
            # 那边的函数会成循环导入，所以按模型自己删。
            hold = session.get(PendingDelete, path)
            if hold is not None:
                session.delete(hold)
                logger.info(f"[{result.code}] 删除已执行，撤销扣留观察: {path}")
        # 历史记录一并清掉，否则订阅任务会认为该番号已下载而跳过。
        # 只清真删掉的种子 —— 合集种子还在做种，它的 History 行得留着，
        # 那是「这个 hash 存在于下载器」的凭据，删了会让后续对账反复补查
        for h in result.torrents_deleted:
            row = session.get(History, h)
            if row is not None:
                session.delete(row)
        # 合集种子本身留着做种，但这部片的文件已经删了。它在 History 里
        # 那条 hash → 本番号 的映射必须清掉，否则重新订阅这部片时会被当成
        # 「已下载过」直接跳过。种子仍在下载器里，删这行不影响它继续做种
        for h in result.torrents_kept:
            row = session.get(History, h)
            if row is not None and row.code == result.code:
                session.delete(row)

    logger.warning(
        f"[{result.code}] 联动删除完成 —— 种子 {len(result.torrents_deleted)}"
        f"{f'（另有 {len(result.torrents_kept)} 个合集种子保留做种）' if result.torrents_kept else ''}，"
        f"文件 {len(result.files_deleted)}，"
        f"刮削附属 {len(result.sidecars_deleted)}，"
        f"目录 {len(result.dirs_deleted)}，错误 {len(result.errors)}"
    )
    return result
