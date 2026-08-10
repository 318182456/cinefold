"""数据库会话管理。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from sqlalchemy import Engine, create_engine, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy.orm import Session, sessionmaker

from app.database.base import DBBase

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "byte-muse.db"

SQLITE_URL = f"sqlite:///{DB_PATH}"


def _normalize_url(url: str) -> str:
    """把常见的 PostgreSQL 写法统一到 psycopg 驱动。

    用户从别处抄来的连接串多是 postgres:// 或 postgresql://，
    SQLAlchemy 2.x 不再接受前者，后者会默认去找 psycopg2。
    """
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _create_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(
            url,
            # SQLite 默认禁止跨线程复用连接，调度器线程需要
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

    # 外部数据库走连接池，配置读不到时用保守的默认值
    try:
        from app.core.config import get_settings
        settings = get_settings()
        pool_size = settings.db_pool_size
        max_overflow = settings.db_max_overflow
        echo = settings.db_echo
    except Exception:
        pool_size, max_overflow, echo = 5, 10, False

    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=3600,
        pool_pre_ping=True,
        echo=echo,
    )


def _resolve_url() -> str:
    """DATABASE_URL 优先，未配置时回落到本地 SQLite。"""
    raw = os.getenv("DATABASE_URL", "")
    if not raw:
        try:
            from app.core.config import get_settings
            raw = get_settings().database_url
        except Exception:
            raw = ""
    return _normalize_url(raw) or SQLITE_URL


DATABASE_URL = _resolve_url()
engine = _create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def is_sqlite() -> bool:
    return engine.dialect.name == "sqlite"


def get_session() -> Iterator[Session]:
    """FastAPI 依赖注入用。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本与定时任务用，自动提交/回滚。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def batch_insert_ignore_duplicate(
    session: Session, model: type[DBBase], rows: Sequence[dict]
) -> int:
    """批量插入，主键冲突时跳过。

    抓取任务会反复遇到已入库的番号，让数据库直接忽略冲突比先查后插快得多。
    SQLite 用 INSERT OR IGNORE，PostgreSQL 用 ON CONFLICT DO NOTHING。
    """
    if not rows:
        return 0
    if is_sqlite():
        stmt = insert(model).prefix_with("OR IGNORE")
    else:
        stmt = pg_insert(model).on_conflict_do_nothing()
    result = session.execute(stmt, list(rows))
    session.commit()
    # executemany 返回的 IteratorResult 没有 rowcount，退回请求条数
    return getattr(result, "rowcount", None) or len(rows)
