"""数据库迁移接口。

老版本的 SQLite 库里攒了几万条番号元数据，换 PostgreSQL 时用这里搬过去。
迁移可能跑几分钟，所以走后台线程 + 轮询进度，不占住请求。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel

from app.api.endpoints import get_current_user
from app.database.utils import migrate as migrate_util
from app.schemas.reponse import ResponseEntity
from app.utils import run_in_background

router = APIRouter(tags=["migrate"])


class MigrateRequest(BaseModel):
    # DATA_DIR 下的 SQLite 文件名，如 lady.db
    source: str
    # PostgreSQL 连接串；留空则用当前配置的 DATABASE_URL
    target_url: str = ""
    # 只统计行数不写入，用于迁移前确认
    dry_run: bool = False


@router.get("/migrate/databases")
def list_databases(current_user: str = Depends(get_current_user)):
    """列出数据目录下可迁移的 SQLite 文件。"""
    from app.database.session import DATABASE_URL, is_sqlite

    return ResponseEntity.ok({
        "files": migrate_util.list_sqlite_files(),
        "current": {
            "url": migrate_util.mask_url(DATABASE_URL),
            "is_sqlite": is_sqlite(),
        },
    })


@router.get("/migrate/progress")
def migrate_progress(current_user: str = Depends(get_current_user)):
    """查询迁移进度。没有任务时 data 为 null。"""
    return ResponseEntity.ok(migrate_util.get_progress())


@router.post("/migrate/test")
def test_target(body: MigrateRequest, current_user: str = Depends(get_current_user)):
    """测试目标库连通性，顺带确认源文件可读。"""
    from sqlalchemy import create_engine, text

    from app.database.session import _normalize_url

    try:
        migrate_util.resolve_source(body.source)
    except ValueError as exc:
        return ResponseEntity.ok({"success": False, "message": str(exc)})

    url = _normalize_url(body.target_url)
    if not url:
        from app.database.session import DATABASE_URL, is_sqlite
        if is_sqlite():
            return ResponseEntity.ok({
                "success": False,
                "message": "当前运行在 SQLite 上，请填写 PostgreSQL 连接串",
            })
        url = DATABASE_URL

    if url.startswith("sqlite"):
        return ResponseEntity.ok({
            "success": False,
            "message": "目标库不能是 SQLite",
        })

    engine = None
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
        with engine.connect() as conn:
            version = conn.execute(text("select version()")).scalar_one()
        return ResponseEntity.ok({"success": True, "message": str(version)[:80]})
    except Exception as exc:
        return ResponseEntity.ok({"success": False, "message": str(exc)[:200]})
    finally:
        if engine is not None:
            engine.dispose()


@router.post("/migrate/start")
def start_migrate(body: MigrateRequest, current_user: str = Depends(get_current_user)):
    """启动迁移。立即返回，进度用 /migrate/progress 轮询。"""
    progress = migrate_util.get_progress()
    if progress and progress.get("running"):
        return ResponseEntity.fail("已有迁移任务在执行", code=409)

    try:
        source = migrate_util.resolve_source(body.source)
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)

    logger.info(f"[迁移] 收到请求，源 {source.name}，试算={body.dry_run}")

    run_in_background(
        migrate_util.migrate,
        source_name=body.source,
        target_url=body.target_url,
        dry_run=body.dry_run,
    )

    return ResponseEntity.ok(message="迁移任务已启动")
