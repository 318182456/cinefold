"""SQLite 轻量迁移工具。

SQLite 的 ALTER TABLE 能力有限，这里只覆盖新增/删除列这类常见变更。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import Engine, inspect, text


def _column_names(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def check_and_create_column(
    engine: Engine, table: str, column: str, column_type: str = "TEXT"
) -> bool:
    """列不存在时新增。返回是否实际执行了变更。"""
    if column in _column_names(engine, table):
        return False

    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
        logger.info(f"已为 {table} 新增列 {column}")
        return True
    except Exception as exc:
        logger.error(f"新增列 {table}.{column} 失败: {exc}")
        return False


def check_and_delete_column(engine: Engine, table: str, column: str) -> bool:
    """列存在时删除。需要 SQLite 3.35+。"""
    if column not in _column_names(engine, table):
        return False

    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        logger.info(f"已从 {table} 删除列 {column}")
        return True
    except Exception as exc:
        logger.warning(f"删除列 {table}.{column} 失败（旧版 SQLite 不支持）: {exc}")
        return False


def has_table(engine: Engine, table: str) -> bool:
    return table in inspect(engine).get_table_names()
