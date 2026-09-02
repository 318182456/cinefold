"""数据库初始化与升级。"""
from __future__ import annotations

import secrets
import time

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
    started = time.perf_counter()
    DBBase.metadata.create_all(engine)
    logger.info(f"  建表完成，耗时 {time.perf_counter() - started:.1f}s")

    started = time.perf_counter()
    update_database()
    logger.info(f"  字段迁移完成，耗时 {time.perf_counter() - started:.1f}s")

    insert_first_user()

    # 内置数据源登记，已存在的不覆盖
    started = time.perf_counter()
    from app.modules.ladysite.sources import sync_builtin_sources
    sync_builtin_sources()
    logger.info(f"  数据源登记完成，耗时 {time.perf_counter() - started:.1f}s")


def update_database() -> None:
    """字段级迁移。旧版本升级上来时补齐新增列。"""
    migrations = [
        ("code", "cn_title", "TEXT"),
        # 官方剧情简介。存量行为空 —— 只有 airav 给这个字段，
        # 已有番号要等下次重抓才补上，NFO 里没有 plot 不影响其他字段
        ("code", "outline", "TEXT"),
        ("code", "local_banner", "TEXT"),
        ("code", "local_still_photo", "TEXT"),
        ("code", "preview_url", "TEXT"),
        ("code", "mode", "VARCHAR(32)"),
        # 双拼封面的人像在哪半边（left/right）。存量行为空，等于「没判断过」，
        # 前端按普通封面居中显示，回填脚本跑完才有值
        ("code", "portrait_side", "VARCHAR(8)"),
        ("actor", "name_2", "VARCHAR(255)"),
        ("actor", "limit_date", "VARCHAR(32)"),
        # 用户主动订阅才为真。默认假，存量行由 backfill_actor_subscribed 回填
        ("actor", "subscribed", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("history", "save_path", "TEXT"),
        # 种子来源站。存量行为空，按「不是 BT 源」处理，不会被误限速
        ("history", "site", "VARCHAR(64)"),
        ("user", "token", "TEXT"),
        # FALSE 而非 0：PostgreSQL 的 boolean 列不接受整数字面量
        ("datasource", "deleted", "BOOLEAN NOT NULL DEFAULT FALSE"),
        # 番号路由规则。存量行为空，等于「不限制」；内置源的默认规则由
        # sync_builtin_sources 补，见 sources.backfill_builtin_rules
        ("datasource", "code_rule", "TEXT"),
        # 目标目录改为可填任意绝对路径。旧规则该列为空，回退到 target_subdir
        ("watch_dir", "target_dir", "VARCHAR(500) NOT NULL DEFAULT ''"),
        # 直通模式：不建硬链接，只登记关联。存量规则一律为假，行为不变
        ("watch_dir", "passthrough", "BOOLEAN NOT NULL DEFAULT FALSE"),
        # 种子反查的失败计数与上次尝试时间，用于给查不到的关联降频。
        # 存量记录从 0 开始，等于「还没查过」，与新建记录一致
        ("media_link", "torrent_miss", "INTEGER NOT NULL DEFAULT 0"),
        ("media_link", "torrent_probe_time", "TIMESTAMP"),
        # 源文件消失的时间。存量记录为空，等于「源文件还在或还没查过」，
        # 首次对账扫到消失时才补上 —— 存量记录的真实消失时间早已无从考证，
        # 写个当前时间反而是假数据
        ("media_link", "source_gone_time", "TIMESTAMP"),
        # 文件大小与字幕状态。存量行为空，等于「还没探过」，
        # 由对账任务与页面访问顺带回填 —— 不写默认 0，那会让
        # 「不知道多大」和「真的是空文件」分不开
        ("media_link", "size", "BIGINT"),
        ("media_link", "size_probe_time", "TIMESTAMP"),
        ("media_link", "has_subtitle", "BOOLEAN"),
        # AI 影评。整张 review 表由 create_all 建，这里只补后加的列
        ("review", "style", "VARCHAR(128)"),
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
        # 硬链接页面按大小排序/筛选，下推成 SQL 后要走索引
        ("media_link", "size"),
    ]
    for table, column in indexes:
        check_and_create_index(engine, table, column)

    backfill_actor_subscribed()


def backfill_actor_subscribed() -> int:
    """把存量演员里的真实订阅标出来，返回回填条数。

    subscribed 这一列是后加的，加上时全表默认假。在它之前，判断订阅与否
    只能看 limit_date 有没有值 —— subscribe_actor 一定会填，爬虫库导入
    一定不填。所以有 limit_date 的就是用户订阅，照这个规则回填一次。

    只在列刚建好、全表都是假时跑；之后 subscribed 由订阅接口维护，
    再回填会把用户取消掉的订阅重新打开。
    """
    from sqlalchemy import func, select, update
    from app.database.models import Actor

    with session_scope() as session:
        already = session.scalar(
            select(func.count()).select_from(Actor).where(Actor.subscribed.is_(True))
        ) or 0
        if already:
            return 0

        result = session.execute(
            update(Actor)
            .where(Actor.limit_date.is_not(None), Actor.limit_date != "")
            .values(subscribed=True)
        )
        filled = result.rowcount or 0

    if filled:
        logger.info(f"  演员订阅标记回填 {filled} 条")
    return filled


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
