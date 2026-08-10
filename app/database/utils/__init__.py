"""轻量迁移工具。

只覆盖新增/删除列这类常见变更，SQLite 与 PostgreSQL 通用。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import Engine, inspect, text


def _column_names(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def _quote(engine: Engine, identifier: str) -> str:
    """按方言引用标识符。

    user 是 PostgreSQL 的保留字，裸写会直接语法报错。
    """
    return engine.dialect.identifier_preparer.quote(identifier)


def check_and_create_column(
    engine: Engine, table: str, column: str, column_type: str = "TEXT"
) -> bool:
    """列不存在时新增。返回是否实际执行了变更。"""
    if column in _column_names(engine, table):
        return False

    table_ref = _quote(engine, table)
    column_ref = _quote(engine, column)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table_ref} ADD COLUMN {column_ref} {column_type}"
            ))
        logger.info(f"已为 {table} 新增列 {column}")
        return True
    except Exception as exc:
        logger.error(f"新增列 {table}.{column} 失败: {exc}")
        return False


def check_and_create_index(
    engine: Engine, table: str, column: str, name: str = ""
) -> bool:
    """索引不存在时创建。返回是否实际执行了变更。

    create_all 只对新建的表生效，存量库的表已经存在，加在模型上的
    index=True 不会自动补上，得在这里显式建。
    """
    if column not in _column_names(engine, table):
        return False

    index_name = name or f"ix_{table}_{column}"
    try:
        inspector = inspect(engine)
        existing = {idx["name"] for idx in inspector.get_indexes(table)}
        if index_name in existing:
            return False
    except Exception:
        # 拿不到索引列表时交给 IF NOT EXISTS 兜底
        pass

    table_ref = _quote(engine, table)
    column_ref = _quote(engine, column)
    index_ref = _quote(engine, index_name)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {index_ref} "
                f"ON {table_ref} ({column_ref})"
            ))
        logger.info(f"已为 {table}.{column} 建索引")
        return True
    except Exception as exc:
        logger.warning(f"建索引 {index_name} 失败: {exc}")
        return False


def check_and_delete_column(engine: Engine, table: str, column: str) -> bool:
    """列存在时删除。SQLite 需 3.35+。"""
    if column not in _column_names(engine, table):
        return False

    table_ref = _quote(engine, table)
    column_ref = _quote(engine, column)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table_ref} DROP COLUMN {column_ref}"))
        logger.info(f"已从 {table} 删除列 {column}")
        return True
    except Exception as exc:
        logger.warning(f"删除列 {table}.{column} 失败（旧版 SQLite 不支持）: {exc}")
        return False


def has_table(engine: Engine, table: str) -> bool:
    return table in inspect(engine).get_table_names()
