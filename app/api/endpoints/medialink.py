"""硬链接关联管理。

webhook 登记的 media_link 记录在这里查看与维护。删除联动真的会删文件，
所以这个页面还兼作"删之前先看看会删什么"的入口 —— preview 走的正是
webhook 的同一条 dry_run 路径。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.endpoints import get_current_user
from app.core.config import get_settings
from app.database.models import History, MediaLink
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
    库里会留下这类孤儿记录。
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
            ))

        rows = list(session.scalars(
            stmt.order_by(MediaLink.create_time.desc(), MediaLink.link_path)
        ).all())

        items = [{
            "link_path": r.link_path,
            "code": r.code,
            "source_path": r.source_path,
            "inode": r.inode,
            "device": r.device,
            "create_time": r.create_time.isoformat() if r.create_time else "",
            "link_exists": _exists(r.link_path),
            "source_exists": _exists(r.source_path),
        } for r in rows]

    if missing_only:
        items = [i for i in items if not i["link_exists"] or not i["source_exists"]]

    total = len(items)
    start = (page - 1) * size
    page_items = items[start:start + size]

    # 种子数按番号批量查，避免每行一次查询
    codes = {i["code"] for i in page_items}
    counts: dict[str, int] = {}
    if codes:
        with session_scope() as session:
            counts = {
                code: n for code, n in session.execute(
                    select(History.code, func.count(History.hash))
                    .where(History.code.in_(codes))
                    .group_by(History.code)
                ).all()
            }
    for item in page_items:
        item["torrent_count"] = counts.get(item["code"], 0)

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
def medialink_stats(current_user: str = Depends(get_current_user)):
    """概览：关联总数、番号数、失效数。"""
    with session_scope() as session:
        rows = list(session.scalars(select(MediaLink)).all())
        paths = [(r.link_path, r.source_path) for r in rows]
        codes = {r.code for r in rows}

    missing = sum(
        1 for link, source in paths if not _exists(link) or not _exists(source)
    )
    return ResponseEntity.ok({
        "total": len(paths),
        "codes": len(codes),
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

    return ResponseEntity.ok(message="已删除记录")


@router.post("/prune")
def prune_records(current_user: str = Depends(get_current_user)):
    """批量清掉文件已不存在的关联记录。同样不碰文件。"""
    with session_scope() as session:
        rows = list(session.scalars(select(MediaLink)).all())
        stale = [
            r.link_path for r in rows
            if not _exists(r.link_path) and not _exists(r.source_path)
        ]
        for path in stale:
            row = session.get(MediaLink, path)
            if row is not None:
                session.delete(row)

    return ResponseEntity.ok(
        {"removed": stale}, message=f"已清理 {len(stale)} 条失效记录"
    )
