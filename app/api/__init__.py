"""FastAPI 应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.config import get_settings
from app.core.version import APP_VERSION
from app.utils.log import setup_loguru_logger

API_PREFIX = "/api/v1"


def init_byte_muse() -> None:
    """启动初始化：日志 → 配置 → 数据库 → 调度器。"""
    setup_loguru_logger()
    logger.info("byte-muse 启动中")

    get_settings()

    from app.database.utils.setup import setup_database
    setup_database()

    from app.scheduler import start_scheduler
    start_scheduler()

    logger.info("byte-muse 启动完成")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_byte_muse()
    yield
    from app.scheduler import stop_scheduler
    stop_scheduler()
    logger.info("byte-muse 已停止")


def create_app() -> FastAPI:
    app = FastAPI(
        title="byte-muse",
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

    from app.api.endpoints import actors, admin, config, message, picproxy, subscribe

    for module in (admin, config, subscribe, actors, picproxy, message):
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
