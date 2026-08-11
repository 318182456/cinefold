"""数据源管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.api.endpoints import get_current_user
from app.database.models import DataSource
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["数据源"])


class SourceRequest(BaseModel):
    name: str | None = None
    host: str | None = None
    enabled: bool | None = None
    interval: float | None = None
    priority: int | None = None
    cookie: str | None = None
    bypass_first: bool | None = None


@router.get("/datasources")
def list_datasources(current_user: str = Depends(get_current_user)):
    """数据源列表。带上是否已接入解析，未接入的只能做连通性测试。"""
    from app.modules.ladysite.sources import SOURCE_MAP, sync_builtin_sources

    # 新增内置源后老库也能补上
    sync_builtin_sources()

    with session_scope() as session:
        rows = session.scalars(
            select(DataSource).order_by(DataSource.priority, DataSource.key)
        ).all()
        items = []
        for row in rows:
            item = row.to_dict()
            item["has_parser"] = bool(SOURCE_MAP.get(row.key, {}).get("parser"))
            items.append(item)

    return ResponseEntity.ok({"items": items})


@router.put("/datasources/{key}")
def update_datasource(
    key: str, body: SourceRequest, current_user: str = Depends(get_current_user)
):
    """改数据源配置。只更新传了的字段。"""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return ResponseEntity.ok(message="没有需要更新的配置")

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is None:
            return ResponseEntity.fail(f"未知数据源 {key}", code=404)

        for field, value in updates.items():
            if field == "host" and value:
                value = value.rstrip("/")
            setattr(row, field, value)

    # 节流间隔改了要让 SiteClient 重新读配置
    from app.modules.ladysite.base import SiteClient
    SiteClient.reset_throttle(key)

    return ResponseEntity.ok(message="已保存")


@router.post("/datasources/{key}/check")
def check_datasource(key: str, current_user: str = Depends(get_current_user)):
    """测单个数据源的连通性。"""
    from app.modules.ladysite.sources import check_source
    return ResponseEntity.ok(check_source(key))


@router.post("/datasources/check")
def check_all_datasources(current_user: str = Depends(get_current_user)):
    """测全部已启用的数据源。并发跑，否则 21 个站串行要等很久。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.modules.ladysite.sources import check_source

    with session_scope() as session:
        keys = [
            row.key for row in session.scalars(
                select(DataSource).where(DataSource.enabled.is_(True))
            ).all()
        ]

    if not keys:
        return ResponseEntity.ok({"items": []})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check_source, keys))

    return ResponseEntity.ok({"items": results})
