"""FastAPI 应用装配。"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.config import get_settings
from app.core.version import APP_VERSION
from app.utils.log import setup_loguru_logger

API_PREFIX = "/api/v1"


@contextmanager
def _step(index: int, total: int, title: str):
    """记录单个启动步骤的起止与耗时。

    启动慢的时候光看首尾两条日志判断不出卡在哪，所以每步都进出各打一条。
    """
    logger.info(f"[{index}/{total}] {title}…")
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        cost = time.perf_counter() - started
        logger.exception(f"[{index}/{total}] {title} 失败（{cost:.1f}s）：{exc}")
        raise
    cost = time.perf_counter() - started
    logger.info(f"[{index}/{total}] {title} 完成，耗时 {cost:.1f}s")


def init_cinefold() -> None:
    """启动初始化：日志 → 配置 → 数据库 → 调度器。"""
    setup_loguru_logger()
    logger.info(f"cinefold {APP_VERSION} 启动中")
    started = time.perf_counter()

    total = 5

    with _step(1, total, "加载配置"):
        get_settings()

    with _step(2, total, "初始化数据库"):
        from app.database.utils.setup import setup_database
        setup_database()

    with _step(3, total, "启动调度器"):
        from app.scheduler import start_scheduler
        start_scheduler()

    with _step(4, total, "启动 Telegram 轮询"):
        from app.modules.notify.tgpolling import start_polling
        start_polling()

    # 目录实时监听。watchdog 缺失或没有规则时会自行跳过，
    # 此时功能退化为定时全量对账，不影响启动
    with _step(5, total, "启动目录监听"):
        from app.modules.watcher import start_watching
        start_watching()

    logger.info(f"cinefold 启动完成，总耗时 {time.perf_counter() - started:.1f}s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_cinefold()
    yield
    from app.api.endpoints.picproxy import close_client
    await close_client()
    from app.modules.notify.tgpolling import stop_polling
    stop_polling()
    from app.modules.watcher import stop_watching
    stop_watching()
    from app.scheduler import stop_scheduler
    stop_scheduler()
    logger.info("cinefold 已停止")


def create_app() -> FastAPI:
    app = FastAPI(
        title="cinefold",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.api.endpoints import (
        actors, admin, agent, auth, config, datasource, medialink, message, migrate,
        picproxy, subscribe, watchdir, webhook,
    )

    for module in (
        admin, auth, config, subscribe, actors, picproxy, message, migrate, datasource,
        medialink, watchdir, webhook, agent,
    ):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.exception_handler(Exception)
    async def unhandled_exception(request, exc: Exception):
        logger.exception(f"未处理异常 {request.url.path}: {exc}")
        return JSONResponse(
            status_code=200,
            content={"code": 500, "message": "服务器内部错误", "data": None},
        )

    @app.get("/api/health")
    def health():
        return {"code": 200, "message": "ok", "data": None}

    return app


app = create_app()
