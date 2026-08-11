"""媒体联动：刮削登记 与 删除联动。

数据流：

    MDCng 刮削完成 ──webhook──> register_scrape()
        拿到 number + source_path，按 inode 在媒体库里找到硬链接
        写入 media_link 表

    Emby 删除影片 ──webhook──> handle_media_deleted()
        按 link_path / code 反查 media_link
        再按 code 去 history 拿到全部种子 hash（含转种）
        删种（不删文件）→ 删源文件 → 删硬链接 → 清记录

删除顺序是有讲究的：种子必须先停，否则下载器可能在文件消失后把任务标记为
错误并重新下载；源文件先于硬链接删，是因为源文件才是占空间的那份，
硬链接删掉只减引用计数，源文件不删空间就不会释放。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import History, MediaLink
from app.database.session import session_scope

# 媒体文件扩展名。按 inode 扫库时只看这些，避免把 nfo/jpg 也算进去
VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts",
    ".flv", ".rmvb", ".iso", ".mpg", ".mpeg", ".m4v", ".strm",
}


@dataclass
class DeleteResult:
    """一次联动删除的结果，用于日志留痕和接口回执。"""
    code: str = ""
    dry_run: bool = False
    torrents_deleted: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    links_deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "dry_run": self.dry_run,
            "torrents_deleted": self.torrents_deleted,
            "files_deleted": self.files_deleted,
            "links_deleted": self.links_deleted,
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


def _torrent_hashes(code: str) -> list[str]:
    """取该番号的全部种子 hash。转种场景下会有多条。"""
    with session_scope() as session:
        return list(session.scalars(
            select(History.hash).where(History.code == code)
        ).all())


def _delete_file(path: str, result: DeleteResult) -> None:
    try:
        p = Path(path)
        if not p.exists():
            logger.info(f"文件已不存在，跳过: {path}")
            return
        p.unlink()
        result.files_deleted.append(path)
        logger.info(f"已删除文件: {path}")
    except OSError as exc:
        msg = f"删除文件失败 {path}: {exc}"
        logger.error(msg)
        result.errors.append(msg)


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
    hashes = _torrent_hashes(result.code)

    logger.warning(
        f"[{result.code}] 媒体联动删除{'（演练）' if dry_run else ''} —— "
        f"种子 {len(hashes)} 个: {', '.join(hashes) or '无'}; "
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
        result.torrents_deleted = hashes
        result.files_deleted = sorted(source_paths)
        result.links_deleted = link_paths
        return result

    # 1) 删种。转种下同一文件对应多个种子，全部删掉；文件不交给下载器删
    from app.modules.downloadclient import get_download_client, list_configured_clients

    if hashes:
        for name in list_configured_clients():
            client = get_download_client(name)
            if client is None:
                continue
            try:
                deleted = client.delete_torrent(hashes, delete_files=False)
                result.torrents_deleted.extend(deleted)
            except Exception as exc:
                msg = f"{name} 删种异常: {exc}"
                logger.error(msg)
                result.errors.append(msg)

    # 2) 删源文件（占空间的那份）
    for path in sorted(source_paths):
        _delete_file(path, result)

    # 3) 删硬链接
    for path in link_paths:
        _delete_file(path, result)
        result.links_deleted.append(path)

    # 4) 清关联记录与下载历史
    with session_scope() as session:
        for path in link_paths:
            row = session.get(MediaLink, path)
            if row is not None:
                session.delete(row)
        # 历史记录一并清掉，否则订阅任务会认为该番号已下载而跳过
        for h in result.torrents_deleted:
            row = session.get(History, h)
            if row is not None:
                session.delete(row)

    logger.warning(
        f"[{result.code}] 联动删除完成 —— 种子 {len(result.torrents_deleted)}，"
        f"文件 {len(result.files_deleted)}，错误 {len(result.errors)}"
    )
    return result
