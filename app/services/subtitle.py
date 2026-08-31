"""字幕抓取与落盘。

三条触发路径共用这一层：

    刮削登记完成 ──> fetch_for_code()      入库即抓，全自动
    定时任务     ──> fill_lack_subtitles()  扫媒体库补漏
    页面按钮     ──> fetch_for_code(force)  人工指定，可覆盖

字幕文件放在影片旁边、与影片同名，Emby/Jellyfin/Plex 都按这个约定认。
带 `.zh-CN` 语言后缀是为了让播放器把它标成简体中文 —— 不带后缀时
Emby 会显示成「未知」，用户得手动挑。

只抓简体中文（见 modules.subtitle）。抓不到就什么都不做：媒体库里
出现看不懂的字幕，比没有字幕更麻烦。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from loguru import logger
from sqlalchemy import select, update

from app.core.config import get_settings
from app.database.models import MediaLink
from app.database.session import session_scope
from app.utils import get_true_code

# 落盘时加的语言后缀。播放器据此标注语种
LANG_SUFFIX = ".zh-CN"

# 已存在的字幕认这些后缀 —— 别人手工放的、刮削工具带的都算数，
# 不该被我们覆盖
_EXISTING_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt", ".sub")


def fetch_for_code(code: str, force: bool = False, manual: bool = False) -> int:
    """给一个番号抓字幕，写到它在媒体库里的每个位置。

    返回实际写入的文件数。force=True 时覆盖已有字幕（页面手动重抓走这条）。
    manual=True 跳过总开关：开关管的是自动行为，人点了按钮就是明确要抓。

    同一部片在媒体库里可能有多个硬链接（分类目录各放一份），
    每个位置都要放字幕，否则从另一个入口播放就没有。
    """
    code = get_true_code(code)
    if not code:
        return 0

    settings = get_settings()
    if not manual and not settings.subtitle_enabled:
        return 0

    targets = _library_paths(code)
    if not targets:
        logger.debug(f"[字幕] {code} 在媒体库里没有登记位置，跳过")
        return 0

    # 没开 force 时，只要有一个位置已有字幕就不必抓 —— 省掉一次跨境请求。
    #
    # 这里只看外挂，不看内挂：内挂轨多半是日文（这类片源的常态），
    # 拿它当「已有字幕」会让简中字幕永远抓不下来。内挂只影响页面展示
    # 与筛选（文件确实带字幕），不该拦住抓取。
    if not force:
        targets = [p for p in targets if not _has_sidecar(p)]
        if not targets:
            logger.debug(f"[字幕] {code} 已有外挂字幕，跳过")
            return 0

    from app.modules import subtitle as subtitle_module

    item = subtitle_module.search(code)
    if item is None:
        return 0

    written = 0
    done: list[Path] = []
    for video in targets:
        if _write_beside(video, item):
            written += 1
            done.append(video)

    if written:
        _mark_subtitled(done)
        logger.info(f"[字幕] {code} 已写入 {written} 处（来源 {item.site}）")
    return written


def _mark_subtitled(videos: list[Path]) -> None:
    """把写成功的位置在库里标成有字幕。

    不回写的话，列会一直停在抓取前的值：下一轮补漏又把这个番号算进候选
    （白跑一次跨境请求），页面的「缺字幕」筛选也仍旧把它列出来。

    按 link_path 更新 —— 它是主键，而 _library_paths 取的正是这一列。
    """
    if not videos:
        return

    paths = [str(v) for v in videos]
    try:
        with session_scope() as session:
            session.execute(
                update(MediaLink)
                .where(MediaLink.link_path.in_(paths))
                .values(has_subtitle=True)
            )
    except Exception as exc:
        # 字幕文件已经落盘了，标记失败不该让整次抓取算作失败：
        # 下一轮补漏会实地复核并纠正这一列
        logger.warning(f"[字幕] 回写 has_subtitle 失败: {exc}")


def fill_lack_subtitles(limit: int = 0) -> int:
    """扫媒体库，给没有字幕的影片补抓。返回补上的影片数。

    定时任务走这条。早期入库的片子、当时字幕站还没收录的片子，
    靠这个回头补上。

    limit 为 0 时用配置里的每轮上限 —— 不能不设限：每部片都要跨境请求
    两三次，媒体库上千部的话一轮能跑几个小时。
    """
    settings = get_settings()
    if not settings.subtitle_enabled:
        logger.debug("[字幕] 未启用，跳过补漏")
        return 0

    limit = limit or max(settings.subtitle_fill_limit, 1)

    filled = 0
    for code in _codes_lacking_subtitle(limit):
        try:
            if fetch_for_code(code):
                filled += 1
        except Exception as exc:
            # 单个番号失败不该中断整轮
            logger.warning(f"[字幕] 补抓 {code} 失败: {exc}")

    if filled:
        logger.info(f"[字幕] 本轮补上 {filled} 部")
    return filled


def _codes_lacking_subtitle(limit: int) -> list[str]:
    """媒体库里还没有字幕的番号，最多 limit 个。

    以 media_link 表为准而非 code 表：只有登记过硬链接的片子才知道
    字幕该往哪儿放，没入库的抓了也没地方搁。

    先用 has_subtitle 列在 SQL 侧筛掉已有字幕的行，只对余下的行碰磁盘。
    此前是把整表拉进内存再逐行 iterdir()，媒体库上千部时一轮要几万次
    目录遍历，而 limit 还是在遍历完之后才截断的。

    列是三态：True 已确认有、False 已确认无、NULL 从未探测过。只能排除
    True —— NULL 行必须实地看一眼，否则从旧版本升上来的库（列全是 NULL）
    会被当成全都缺字幕。
    """
    with session_scope() as session:
        rows = session.execute(
            select(MediaLink.code, MediaLink.link_path)
            .where(MediaLink.code.is_not(None), MediaLink.code != "")
            .where(MediaLink.has_subtitle.is_not(True))
            # 未探测的排前面：这些是升级后第一轮要补的正主
            .order_by(MediaLink.has_subtitle.is_(None).desc(), MediaLink.code)
        ).all()

    seen: set[str] = set()
    out: list[str] = []
    probe = _DirProbe()
    for code, link_path in rows:
        if not code or code in seen:
            continue
        seen.add(code)
        # 列说 False 也仍要复核：字幕可能是用户手工放进去的，
        # 那种情况下列不会被更新。
        # 只认外挂 —— 与 fetch_for_code 的跳过判据必须同口径，
        # 否则这里挑出来的番号进去又被跳过，白跑一趟
        try:
            if probe.has_sidecar(Path(link_path)):
                continue
        except OSError:
            continue
        out.append(code)
        if len(out) >= limit:
            break
    return out


def _library_paths(code: str) -> list[Path]:
    """番号在媒体库里的全部影片路径。文件已不在的跳过。"""
    with session_scope() as session:
        rows = session.scalars(
            select(MediaLink.link_path).where(MediaLink.code == code)
        ).all()

    out: list[Path] = []
    for raw in rows:
        path = Path(raw)
        try:
            if path.is_file():
                out.append(path)
        except OSError:
            continue
    return out


def has_subtitle(link_path: str) -> bool:
    """这部影片有字幕吗 —— 外挂或内挂都算。供页面展示与筛选用。

    抓取的跳过判据是 _has_sidecar，比这里窄：内挂轨多半是日文，
    拿它当「已有字幕」会让简中字幕永远抓不下来。两者口径不同是有意的 ——
    页面回答的是「这文件有没有字幕」，抓取回答的是「还需不需要抓简中」。
    """
    try:
        return _has_subtitle(Path(link_path))
    except OSError:
        return False


def _subtitle_names(parent: Path) -> list[str]:
    """目录里所有字幕文件的小写文件名。目录读不了时返回空表。

    只留字幕扩展名：分类目录动辄几百个文件，绝大多数是影片本体，
    留着它们会让后面每部片的比对都白跑一遍。
    """
    try:
        entries = list(parent.iterdir())
    except OSError:
        raise
    return [
        e.name.lower() for e in entries if e.name.lower().endswith(_EXISTING_SUFFIXES)
    ]


def _matches_stem(name: str, stem: str) -> bool:
    """字幕文件名 name 是影片 stem 的字幕吗。两者都须已小写。

    同名，或同名后紧跟分隔符（ABS-001.zh-CN.srt）。不能用裸前缀：
    ABS-0011.srt 也以 ABS-001 开头，那是另一部片。
    """
    if not name.startswith(stem):
        return False
    rest = name[len(stem):]
    return rest.startswith(".") or rest.startswith("-")


class _DirProbe:
    """带目录缓存的字幕存在性探测。

    一个分类目录下几百部片，逐部独立 iterdir() 会把同一个目录列几百遍。
    补漏任务扫全库时这是主要开销，所以按目录缓存一次列表结果。

    只在单轮扫描内用（构造一个新实例即弃），不做失效 —— 缓存活得越久，
    读到的目录状态越旧。
    """

    def __init__(self) -> None:
        self._cache: dict[Path, list[str]] = {}

    def has_sidecar(self, video: Path) -> bool:
        """只看旁边有没有外挂字幕文件。抓取判据用这个。"""
        parent = video.parent
        names = self._cache.get(parent)
        if names is None:
            names = _subtitle_names(parent)
            self._cache[parent] = names

        stem = video.stem.lower()
        return any(_matches_stem(n, stem) for n in names)

    def has_subtitle(self, video: Path) -> bool:
        """外挂或内挂，有一个就算有。页面展示与筛选用这个。"""
        if self.has_sidecar(video):
            return True
        # 没有外挂才读文件头。目录缓存省掉的是 iterdir，
        # 内挂检测是按文件的，省不掉，所以放在后面只对少数文件生效
        return _has_embedded(video)


def _has_subtitle(video: Path) -> bool:
    """这部影片已经有字幕了吗 —— 外挂和内挂都算。

    外挂认所有「同名 + 字幕扩展名」的文件，语言后缀不限 —— 用户手工放的
    ABS-001.chs.srt 也算数，不该被覆盖。

    内挂（封装在 mkv/mp4 里的字幕轨）同样算数：把它当成缺字幕的话，
    页面显示是错的，补漏任务还会一遍遍去抓已经有字幕的片子。

    先看外挂：列一次目录就够，而且目录多半已被 _DirProbe 缓存过；
    只有外挂没有时才去读文件头。顺序反过来会让绝大多数已有外挂字幕的
    影片都白读一次文件头。

    单次探测走这条（不缓存，总是看当下的磁盘状态）；批量扫描用 _DirProbe。
    """
    if _has_sidecar(video):
        return True
    return _has_embedded(video)


def _has_sidecar(video: Path) -> bool:
    """影片旁边有没有外挂字幕文件。"""
    try:
        names = _subtitle_names(video.parent)
    except OSError:
        return False

    stem = video.stem.lower()
    return any(_matches_stem(n, stem) for n in names)


def _has_embedded(video: Path) -> bool:
    """影片里有没有内挂字幕轨。读文件头判断，认不出时一律为假。"""
    from app.services.embedded_subtitle import has_embedded_subtitle

    return has_embedded_subtitle(video)


def _write_beside(video: Path, item) -> bool:
    """把字幕写到影片旁边。写成功返回 True。

    先写临时文件再原子改名：媒体服务器会实时监听目录，直接写会让它
    扫到一个还没写完的半截文件并缓存成「字幕损坏」。
    """
    target = video.parent / f"{video.stem}{LANG_SUFFIX}{item.suffix}"

    try:
        # 临时文件必须与目标同目录，跨设备的 replace 不是原子操作
        fd, tmp_name = tempfile.mkstemp(
            dir=str(video.parent), prefix=".cinefold-sub-", suffix=item.suffix
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(item.content)
            os.replace(tmp_name, target)
        except Exception:
            # 写失败要把临时文件收走，否则媒体库目录里会攒下一堆残骸
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(f"[字幕] 写入 {target} 失败: {exc}")
        return False

    logger.debug(f"[字幕] 已写入 {target}")
    return True
