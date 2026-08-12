"""数据库初始化与升级。"""
from __future__ import annotations

import secrets

from loguru import logger

from app.database.base import DBBase
from app.database.models import User
from app.database.session import engine, session_scope
from app.database.utils import check_and_create_column, check_and_create_index

DEFAULT_USERNAME = "admin"


def hash_password(raw: str) -> str:
    import bcrypt
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()


def verify_password(raw: str, hashed: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(raw.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def generate_code(length: int = 12) -> str:
    """生成初始密码。"""
    return secrets.token_urlsafe(length)[:length]


def setup_database() -> None:
    """建表 + 建初始账号。幂等，每次启动都会调用。"""
    DBBase.metadata.create_all(engine)
    update_database()
    insert_first_user()

    # 内置数据源登记，已存在的不覆盖
    from app.modules.ladysite.sources import sync_builtin_sources
    sync_builtin_sources()


def update_database() -> None:
    """字段级迁移。旧版本升级上来时补齐新增列。"""
    migrations = [
        ("code", "cn_title", "TEXT"),
        ("code", "local_banner", "TEXT"),
        ("code", "local_still_photo", "TEXT"),
        ("code", "preview_url", "TEXT"),
        ("code", "mode", "VARCHAR(32)"),
        ("actor", "name_2", "VARCHAR(255)"),
        ("actor", "limit_date", "VARCHAR(32)"),
        ("history", "save_path", "TEXT"),
        ("user", "token", "TEXT"),
        # FALSE 而非 0：PostgreSQL 的 boolean 列不接受整数字面量
        ("datasource", "deleted", "BOOLEAN NOT NULL DEFAULT FALSE"),
        # 目标目录改为可填任意绝对路径。旧规则该列为空，回退到 target_subdir
        ("watch_dir", "target_dir", "VARCHAR(500) NOT NULL DEFAULT ''"),
        # 直通模式：不建硬链接，只登记关联。存量规则一律为假，行为不变
        ("watch_dir", "passthrough", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ]
    for table, column, column_type in migrations:
        check_and_create_column(engine, table, column, column_type)

    # 高频查询列的索引。存量库的表已存在，create_all 不会补，得显式建
    indexes = [
        # 订阅任务、看板统计、列表页筛选都按状态查
        ("code", "status"),
        # 列表页默认排序
        ("code", "update_time"),
        # 今日发布与多处排序
        ("code", "release_date"),
        ("actor", "update_time"),
        # 监控目录对账时按规则捞扣留记录
        ("pending_delete", "watch_id"),
    ]
    for table, column in indexes:
        check_and_create_index(engine, table, column)


def insert_first_user() -> str:
    """首次启动时创建 admin 账号，密码打印到日志。"""
    with session_scope() as session:
        if session.get(User, DEFAULT_USERNAME) is not None:
            return ""

        password = generate_code()
        session.add(User(
            username=DEFAULT_USERNAME,
            password=hash_password(password),
            token=secrets.token_urlsafe(32),
        ))

    logger.warning(
        f"已创建初始账号 —— 用户名: {DEFAULT_USERNAME}  密码: {password}  "
        f"请登录后立即修改"
    )
    return password
