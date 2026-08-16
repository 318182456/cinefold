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
    code_rule: str | None = None


class SourceCreateRequest(BaseModel):
    key: str
    name: str = ""
    host: str = ""
    enabled: bool = True
    interval: float = 0.0
    priority: int = 100
    cookie: str = ""
    bypass_first: bool = False


class ReorderRequest(BaseModel):
    """按 key 的先后顺序重排优先级。"""
    keys: list[str]


@router.get("/datasources")
def list_datasources(current_user: str = Depends(get_current_user)):
    """数据源列表。带上是否已接入解析，未接入的只能做连通性测试。

    已软删除的不在列表里，另外单列 removable_builtins 供页面做「恢复」。
    """
    from app.modules.ladysite import DETAIL_SITES, SPECIAL_SITES
    from app.modules.ladysite.sources import (
        SOURCE_MAP, is_builtin, is_protected, sync_builtin_sources,
    )

    # 新增内置源后老库也能补上
    sync_builtin_sources()

    # 参与详情抓取的源。javlibrary 有解析器但只用于榜单，不参与检索
    detail_keys = set(DETAIL_SITES) | set(SPECIAL_SITES)

    with session_scope() as session:
        rows = session.scalars(
            select(DataSource).order_by(DataSource.priority, DataSource.key)
        ).all()
        items, removed = [], []
        for row in rows:
            if row.deleted:
                # 只有内置源值得提供恢复入口，自定义源删了就是删了
                if is_builtin(row.key):
                    removed.append({"key": row.key, "name": row.name})
                continue
            item = row.to_dict()
            item["has_parser"] = bool(SOURCE_MAP.get(row.key, {}).get("parser"))
            item["builtin"] = is_builtin(row.key)
            item["protected"] = is_protected(row.key)
            item["in_detail"] = row.key in detail_keys
            # 用途分区：字幕源与番号源共用这张表，但页面上要分开列 ——
            # 混在一起时用户会以为字幕站也参与番号检索
            item["kind"] = SOURCE_MAP.get(row.key, {}).get("kind", "detail")
            items.append(item)

    return ResponseEntity.ok({"items": items, "removed": removed})


@router.post("/datasources")
def create_datasource(
    body: SourceCreateRequest, current_user: str = Depends(get_current_user)
):
    """新增自定义数据源。

    没有解析器，只能做连通性测试 —— 解析器要在代码里实现，加不进来。
    """
    key = (body.key or "").strip().lower()
    if not key:
        return ResponseEntity.fail("标识不能为空", code=400)
    if not key.replace("_", "").replace("-", "").isalnum():
        return ResponseEntity.fail("标识只能用字母、数字、下划线和连字符", code=400)

    host = (body.host or "").strip().rstrip("/")
    if not host:
        return ResponseEntity.fail("地址不能为空", code=400)

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is not None:
            # 撞上已软删除的行就地复用，否则 unique 约束会直接报错
            if not row.deleted:
                return ResponseEntity.fail(f"标识 {key} 已存在", code=409)
            row.deleted = False
            row.name = body.name.strip() or key
            row.host = host
            row.enabled = body.enabled
            row.interval = body.interval
            row.priority = body.priority
            row.cookie = body.cookie or None
            row.bypass_first = body.bypass_first
            row.status = None
            row.status_message = None
            row.checked_time = None
            return ResponseEntity.ok(message="已添加")

        session.add(DataSource(
            key=key,
            name=body.name.strip() or key,
            host=host,
            enabled=body.enabled,
            interval=body.interval,
            priority=body.priority,
            cookie=body.cookie or None,
            bypass_first=body.bypass_first,
        ))

    return ResponseEntity.ok(message="已添加")


@router.delete("/datasources/{key}")
def delete_datasource(key: str, current_user: str = Depends(get_current_user)):
    """删除数据源。核心源受保护，只能停用不能删。

    内置源走软删除，避免 sync_builtin_sources() 下次启动把它补回来；
    自定义源直接删行，留着没有恢复价值。
    """
    from app.modules.ladysite.sources import is_builtin, is_protected

    if is_protected(key):
        return ResponseEntity.fail(
            f"{key} 是核心数据源，不可删除；如需停用请关闭开关", code=403
        )

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is None or row.deleted:
            return ResponseEntity.fail(f"未知数据源 {key}", code=404)

        if is_builtin(key):
            row.deleted = True
            row.enabled = False
        else:
            session.delete(row)

    from app.modules.ladysite.base import SiteClient
    SiteClient.reset_throttle(key)

    return ResponseEntity.ok(message="已删除")


@router.post("/datasources/{key}/restore")
def restore_datasource(key: str, current_user: str = Depends(get_current_user)):
    """恢复被删除的内置源，配置一并重置为默认值。"""
    from app.modules.ladysite.sources import restore_builtin_source

    if not restore_builtin_source(key):
        return ResponseEntity.fail(f"{key} 不是内置数据源，无法恢复", code=404)

    from app.modules.ladysite.base import SiteClient
    SiteClient.reset_throttle(key)

    return ResponseEntity.ok(message="已恢复")


# 必须声明在 /datasources/{key} 之前：FastAPI 按声明顺序匹配，
# 反过来的话 "reorder" 会被当成 key 落到 update_datasource 上，直接 404
@router.put("/datasources/reorder")
def reorder_datasources(
    body: ReorderRequest, current_user: str = Depends(get_current_user)
):
    """按传入的 key 顺序重排优先级。

    抓取时多个源并发跑、取最先返回的结果，排在前面的源先拿到并发额度
    （见 ladysite.MAX_PARALLEL_SITES），所以顺序确实影响用哪个源的数据。

    只重排传进来的这些 key，没传的保持原样 —— 页面按分组展示，
    一次只重排其中一组。
    """
    keys, seen = [], set()
    for key in body.keys:
        key = (key or "").strip()
        # 重复 key 会让后面的覆盖前面的优先级，顺序变得不可预测
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys:
        return ResponseEntity.fail("顺序不能为空", code=400)

    with session_scope() as session:
        rows = {
            row.key: row for row in session.scalars(
                select(DataSource).where(
                    DataSource.key.in_(keys), DataSource.deleted.is_(False)
                )
            ).all()
        }
        unknown = [k for k in keys if k not in rows]
        if unknown:
            return ResponseEntity.fail(f"未知数据源 {', '.join(unknown)}", code=404)

        # 以这一组现有的最小优先级为起点，保持它相对其他组的位置不变；
        # 从 0 开始会把整组顶到所有源前面去
        base = min(rows[k].priority for k in keys)
        for offset, key in enumerate(keys):
            rows[key].priority = base + offset

    return ResponseEntity.ok(message="顺序已保存")


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
        if row is None or row.deleted:
            return ResponseEntity.fail(f"未知数据源 {key}", code=404)

        for field, value in updates.items():
            if field == "host" and value:
                value = value.rstrip("/")
            # 番号规则清空要存空串而不是 NULL：NULL 会被
            # backfill_builtin_rules 当成"还没补过"，下次启动又把默认规则
            # 写回去，用户的"不限制"就被悄悄撤销了
            if field == "code_rule" and value is None:
                value = ""
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
    """测全部已启用的数据源。并发跑，否则几十个站串行要等很久。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.modules.ladysite.sources import check_source

    with session_scope() as session:
        keys = [
            row.key for row in session.scalars(
                select(DataSource).where(
                    DataSource.enabled.is_(True), DataSource.deleted.is_(False)
                )
            ).all()
        ]

    if not keys:
        return ResponseEntity.ok({"items": []})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check_source, keys))

    return ResponseEntity.ok({"items": results})
