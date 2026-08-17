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
from sqlalchemy import select

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

    # 没开 force 时，只要有一个位置已有字幕就不必抓 —— 省掉一次跨境请求
    if not force:
        targets = [p for p in targets if not _has_subtitle(p)]
        if not targets:
            logger.debug(f"[字幕] {code} 已有字幕，跳过")
            return 0

    from app.modules import subtitle as subtitle_module

    item = subtitle_module.search(code)
    if item is None:
        return 0

    written = 0
    for video in targets:
        if _write_beside(video, item):
            written += 1

    if written:
        logger.info(f"[字幕] {code} 已写入 {written} 处（来源 {item.site}）")
    return written


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
    """
    with session_scope() as session:
        rows = session.execute(
            select(MediaLink.code, MediaLink.link_path)
        ).all()

    seen: set[str] = set()
    out: list[str] = []
    for code, link_path in rows:
        if not code or code in seen:
            continue
        try:
            if _has_subtitle(Path(link_path)):
                seen.add(code)
                continue
        except OSError:
            continue
        seen.add(code)
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
    """这条硬链接旁边有字幕吗。供页面展示用。

    与抓取时的跳过判据是同一个函数 —— 分成两套的话，页面显示「有字幕」
    而抓取仍去抓（或反过来）这种自相矛盾迟早出现。
    """
    try:
        return _has_subtitle(Path(link_path))
    except OSError:
        return False


def _has_subtitle(video: Path) -> bool:
    """影片旁边已经有字幕了吗。

    认所有「同名 + 字幕扩展名」的文件，语言后缀不限 —— 用户手工放的
    ABS-001.chs.srt 也算数，不该被覆盖。
    """
    parent = video.parent
    stem = video.stem.lower()
    try:
        entries = list(parent.iterdir())
    except OSError:
        return False

    for entry in entries:
        name = entry.name.lower()
        if not name.endswith(_EXISTING_SUFFIXES):
            continue
        # 同名，或同名后紧跟分隔符（ABS-001.zh-CN.srt）。
        # 不能用裸前缀：ABS-0011.srt 也以 ABS-001 开头，那是另一部片
        rest = name[len(stem):] if name.startswith(stem) else None
        if rest is not None and (rest.startswith(".") or rest.startswith("-")):
            return True
    return False


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
