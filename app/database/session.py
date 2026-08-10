"""数据库会话管理。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session, sessionmaker

from app.database.base import DBBase

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "byte-muse.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    # SQLite 默认禁止跨线程复用连接，调度器线程需要
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


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

    抓取任务会反复遇到已入库的番号，用 INSERT OR IGNORE 比先查后插快得多。
    """
    if not rows:
        return 0
    stmt = insert(model).prefix_with("OR IGNORE")
    result = session.execute(stmt, list(rows))
    session.commit()
    # executemany 返回的 IteratorResult 没有 rowcount，退回请求条数
    return getattr(result, "rowcount", None) or len(rows)
