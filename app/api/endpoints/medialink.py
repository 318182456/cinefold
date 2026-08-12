"""硬链接关联管理。

webhook 登记的 media_link 记录在这里查看与维护。删除联动真的会删文件，
所以这个页面还兼作"删之前先看看会删什么"的入口 —— preview 走的正是
webhook 的同一条 dry_run 路径。
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.endpoints import get_current_user
from app.core.config import get_settings
from app.database.models import CodeAlias, History, MediaLink
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.services.medialink import handle_media_deleted, register_scrape

router = APIRouter(prefix="/medialinks", tags=["硬链接"])


class RegisterRequest(BaseModel):
    code: str
    source_path: str
    link_path: str = ""


class DeleteRequest(BaseModel):
    """按 link_path 或 code 定位要清理的关联。"""
    link_path: str = ""
    code: str = ""
    dry_run: bool = True


def _exists(path: str) -> bool:
    """路径是否还在。跨容器挂载不一致时会误判为丢失，仅作提示用。"""
    try:
        return Path(path).exists()
    except OSError:
        return False


# 存在性探测的结果缓存。媒体库多半挂在 NAS 上，一次 exists() 就是一次网络
# 往返，几十条记录叠起来就是肉眼可见的卡顿。文件在不在这件事变化很慢，
# 缓存几十秒完全够用 —— 真删过文件的路径会被显式失效（见 _invalidate）。
_EXISTS_TTL = 30.0
_exists_cache: dict[str, tuple[float, bool]] = {}


def _exists_cached(path: str) -> bool:
    now = time.monotonic()
    hit = _exists_cache.get(path)
    if hit is not None and now - hit[0] < _EXISTS_TTL:
        return hit[1]
    value = _exists(path)
    _exists_cache[path] = (now, value)
    return value


def _invalidate(*paths: str) -> None:
    """删过文件之后把相关路径踢出缓存，避免页面还显示"存在"。"""
    for path in paths:
        _exists_cache.pop(path, None)


# 全量失效统计的缓存。stats 和 missing_only 都要扫全表做探测，
# 是这个页面最贵的一步，单独缓存一份共用
_STATS_TTL = 60.0
_missing_cache: tuple[float, set[str]] | None = None


def _missing_links(force: bool = False) -> set[str]:
    """返回文件已丢失的 link_path 集合。

    全表探测，慢。结果缓存 _STATS_TTL 秒，列表页与概览共用同一份。
    """
    global _missing_cache

    now = time.monotonic()
    if not force and _missing_cache is not None and now - _missing_cache[0] < _STATS_TTL:
        return _missing_cache[1]

    with session_scope() as session:
        rows = session.execute(
            select(MediaLink.link_path, MediaLink.source_path)
        ).all()

    missing = {
        link for link, source in rows
        if not _exists_cached(link) or not _exists_cached(source)
    }
    _missing_cache = (now, missing)
    return missing


def _drop_stats_cache() -> None:
    global _missing_cache
    _missing_cache = None


@router.get("")
def list_medialinks(
    keyword: str = "",
    missing_only: bool = False,
    page: int = 1,
    size: int = 50,
    current_user: str = Depends(get_current_user),
):
    """关联列表。按番号分组返回，一个番号可能有多条硬链接。

    missing_only 只看文件已不存在的记录 —— 手工删过文件但没走 webhook 时，
    库里会留下这类孤儿记录。这一项需要全表探测文件是否存在，走缓存路径。
    """
    page = max(1, page)
    size = min(max(1, size), 200)

    with session_scope() as session:
        stmt = select(MediaLink)
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(
                MediaLink.code.like(like),
                MediaLink.link_path.like(like),
                MediaLink.source_path.like(like),
                # 哈希 code 搜不出片名，得连别名表一起搜。source_path 里通常
                # 也含文件名，但直通模式改名后两者会不一致，别名才是准的
                MediaLink.code.in_(
                    select(CodeAlias.code).where(CodeAlias.filename.like(like))
                ),
            ))
        if missing_only:
            # 失效判定只能靠磁盘探测，没法下推成 SQL 条件；
            # 取到失效路径集合后当作 IN 条件用，分页仍然交给数据库
            stale = _missing_links()
            if not stale:
                return ResponseEntity.ok({
                    "items": [], "total": 0, "page": page, "size": size,
                    "delete_enabled": get_settings().medialink_delete_enabled,
                    "library_path": get_settings().medialink_library_path,
                })
            stmt = stmt.where(MediaLink.link_path.in_(stale))

        total = session.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0

        # 分页下推到 SQL：只有当前页的记录会被取出来做存在性探测
        rows = list(session.scalars(
            stmt.order_by(MediaLink.create_time.desc(), MediaLink.link_path)
            .offset((page - 1) * size)
            .limit(size)
        ).all())

        page_items = [{
            "link_path": r.link_path,
            "code": r.code,
            "source_path": r.source_path,
            "inode": r.inode,
            "device": r.device,
            "create_time": r.create_time.isoformat() if r.create_time else "",
            "link_exists": _exists_cached(r.link_path),
            "source_exists": _exists_cached(r.source_path),
        } for r in rows]

    # 种子数与文件名别名都按番号批量查，避免每行一次查询
    codes = {i["code"] for i in page_items}
    counts: dict[str, int] = {}
    aliases: dict[str, str] = {}
    if codes:
        with session_scope() as session:
            counts = {
                code: n for code, n in session.execute(
                    select(History.code, func.count(History.hash))
                    .where(History.code.in_(codes))
                    .group_by(History.code)
                ).all()
            }
            # 长文件名的 code 是哈希，读不出是哪部片子，别名表里取回原名
            aliases = {
                code: name for code, name in session.execute(
                    select(CodeAlias.code, CodeAlias.filename)
                    .where(CodeAlias.code.in_(codes))
                ).all()
            }
    for item in page_items:
        item["torrent_count"] = counts.get(item["code"], 0)
        item["filename"] = aliases.get(item["code"], "")

    settings = get_settings()
    return ResponseEntity.ok({
        "items": page_items,
        "total": total,
        "page": page,
        "size": size,
        # 页面要据此提示"联动删除未启用，删除只会演练"
        "delete_enabled": settings.medialink_delete_enabled,
        "library_path": settings.medialink_library_path,
    })


@router.get("/stats")
def medialink_stats(
    check_missing: bool = True, current_user: str = Depends(get_current_user)
):
    """概览：关联总数、番号数、失效数。

    总数与番号数直接由数据库聚合，不碰磁盘。失效数要逐个探测文件，
    库大或挂在 NAS 上时明显更慢，走缓存；check_missing=false 可以完全跳过，
    此时 missing 返回 null。
    """
    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(MediaLink)) or 0
        codes = session.scalar(
            select(func.count(func.distinct(MediaLink.code)))
        ) or 0

    missing = len(_missing_links()) if check_missing else None
    return ResponseEntity.ok({
        "total": total,
        "codes": codes,
        "missing": missing,
        "delete_enabled": get_settings().medialink_delete_enabled,
    })


@router.post("/register")
def register_medialink(
    body: RegisterRequest, current_user: str = Depends(get_current_user)
):
    """手工登记一条关联。webhook 没配好时的补救入口。"""
    code = (body.code or "").strip()
    source_path = (body.source_path or "").strip()
    if not code or not source_path:
        return ResponseEntity.fail("番号与源文件路径不能为空", code=400)

    links = register_scrape(code, source_path, (body.link_path or "").strip())
    if not links:
        return ResponseEntity.fail(
            "未找到可登记的硬链接。确认源文件存在、媒体库根目录已配置，"
            "且刮削产物与源文件在同一挂载卷",
            code=400,
        )
    _drop_stats_cache()
    return ResponseEntity.ok({"links": links}, message=f"已登记 {len(links)} 条")


@router.post("/preview")
def preview_delete(
    body: DeleteRequest, current_user: str = Depends(get_current_user)
):
    """演练一次联动删除，只报会删什么，不动手。"""
    result = handle_media_deleted(
        link_path=body.link_path, code=body.code, dry_run=True
    )
    return ResponseEntity.ok(result.as_dict())


@router.post("/delete")
def delete_medialink(
    body: DeleteRequest, current_user: str = Depends(get_current_user)
):
    """执行联动删除：删种 → 删源文件 → 删硬链接 → 清记录。

    dry_run 默认为真，必须显式传 false 才会真删。全局开关
    MEDIALINK_DELETE_ENABLED 为假时，service 层还会再兜一次底。
    """
    if not body.link_path and not body.code:
        return ResponseEntity.fail("需要 link_path 或 code", code=400)

    result = handle_media_deleted(
        link_path=body.link_path, code=body.code, dry_run=body.dry_run
    )
    # 文件真被删掉了，缓存里的"存在"结论立刻作废
    _invalidate(*result.files_deleted, *result.links_deleted)
    _drop_stats_cache()
    if result.errors and not result.links_deleted:
        return ResponseEntity.fail("; ".join(result.errors), code=400, data=result.as_dict())
    return ResponseEntity.ok(result.as_dict())


@router.delete("/record")
def drop_record(
    link_path: str, current_user: str = Depends(get_current_user)
):
    """只删关联记录，不碰任何文件。用于清理失效的孤儿记录。"""
    if not link_path:
        return ResponseEntity.fail("缺少 link_path", code=400)

    with session_scope() as session:
        row = session.get(MediaLink, link_path)
        if row is None:
            return ResponseEntity.fail("记录不存在", code=404)
        session.delete(row)

    _drop_stats_cache()
    return ResponseEntity.ok(message="已删除记录")


@router.post("/prune")
def prune_records(current_user: str = Depends(get_current_user)):
    """批量清掉文件已不存在的关联记录。同样不碰文件。

    这里要的是"两侧文件都没了"，比 _missing_links 的"任一侧没了"更严格，
    所以仍然自己扫一遍 —— 但探测走缓存，与刚打开页面时的那次共用结果。
    """
    with session_scope() as session:
        rows = session.execute(
            select(MediaLink.link_path, MediaLink.source_path)
        ).all()
        stale = [
            link for link, source in rows
            if not _exists_cached(link) and not _exists_cached(source)
        ]
        for path in stale:
            row = session.get(MediaLink, path)
            if row is not None:
                session.delete(row)

    _drop_stats_cache()
    return ResponseEntity.ok(
        {"removed": stale}, message=f"已清理 {len(stale)} 条失效记录"
    )
