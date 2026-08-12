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
DEFAULT_DB_PATH = DATA_DIR / "cinefold.db"
# 早期版本的库名。从旧数据目录升级上来时要接着用，不能当成空库
LEGACY_DB_PATH = DATA_DIR / "byte-muse.db"


def _resolve_sqlite_path() -> Path:
    """确定 SQLite 文件路径，必要时把旧库接过来。

    只在新库不存在、旧库存在时改名。SQLite 的 -wal / -shm 是同名派生文件，
    必须一起搬，否则未落盘的事务会丢。改名失败则继续用旧库 —— 建个空库
    让用户以为数据全丢了，比多一行失败日志糟糕得多。
    """
    if DEFAULT_DB_PATH.exists() or not LEGACY_DB_PATH.exists():
        return DEFAULT_DB_PATH

    try:
        LEGACY_DB_PATH.rename(DEFAULT_DB_PATH)
        for suffix in ("-wal", "-shm"):
            sidecar = LEGACY_DB_PATH.with_name(LEGACY_DB_PATH.name + suffix)
            if sidecar.exists():
                sidecar.rename(DEFAULT_DB_PATH.with_name(DEFAULT_DB_PATH.name + suffix))
    except OSError as exc:
        # 日志系统此时还没初始化，只能先 print
        print(f"[cinefold] 旧数据库改名失败，继续使用 {LEGACY_DB_PATH.name}: {exc}")
        return LEGACY_DB_PATH

    print(f"[cinefold] 已将 {LEGACY_DB_PATH.name} 迁移为 {DEFAULT_DB_PATH.name}")
    return DEFAULT_DB_PATH


DB_PATH = _resolve_sqlite_path()

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


def insert_ignore_stmt(model: type[DBBase]):
    """构造「主键冲突就跳过」的 INSERT。

    SQLite 用 INSERT OR IGNORE，PostgreSQL 用 ON CONFLICT DO NOTHING。
    """
    if is_sqlite():
        return insert(model).prefix_with("OR IGNORE")
    return pg_insert(model).on_conflict_do_nothing()


def insert_ignore_duplicate(
    session: Session, model: type[DBBase], rows: Sequence[dict]
) -> int:
    """批量插入并忽略冲突，但**不提交** —— 提交时机交给调用方。

    需要把插入和同事务里其他写操作一起提交时用这个（比如监控目录登记：
    硬链接记录和种子记录必须同成同败）。只想插一批就走
    batch_insert_ignore_duplicate。

    返回请求条数，不是实际插入条数 —— 忽略掉多少条数据库不告诉我们。
    """
    if not rows:
        return 0
    session.execute(insert_ignore_stmt(model), list(rows))
    return len(rows)


def batch_insert_ignore_duplicate(
    session: Session, model: type[DBBase], rows: Sequence[dict]
) -> int:
    """批量插入，主键冲突时跳过，插完就提交。

    抓取任务会反复遇到已入库的番号，让数据库直接忽略冲突比先查后插快得多。
    """
    if not rows:
        return 0
    result = session.execute(insert_ignore_stmt(model), list(rows))
    session.commit()
    # executemany 返回的 IteratorResult 没有 rowcount，退回请求条数
    return getattr(result, "rowcount", None) or len(rows)
