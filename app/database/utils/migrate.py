"""SQLite → PostgreSQL 数据迁移。

老版本用 SQLite 攒了几万条番号元数据，换 PostgreSQL 时不想从头再抓一遍。
迁移按表逐批搬，主键冲突跳过，所以中断后重跑不会产生重复数据。

老库有两处字段不兼容，搬运时就地转换：
  - status/mode 老库存字符串（UN_SUBSCRIBE 等），新库存整数枚举
  - local_banner 老库存 /pic/<番号>/banner.jpg 这种 URL 路径，新库统一存相对路径
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from loguru import logger
from sqlalchemy import Engine, create_engine, inspect, select, func

from app.database.base import DBBase
from app.database.models import Actor, Code, History, User
from app.database.models.code import CodeStatus
from app.database.session import _normalize_url, batch_insert_ignore_duplicate, SessionLocal

# 搬运顺序固定：番号在前，历史记录引用番号。
# cache 表存榜单快照，能重新抓，且自增主键搬过去会和目标库的序列打架，不迁。
TABLES: tuple[type[DBBase], ...] = (Code, Actor, History, User)

BATCH_SIZE = 500

# 老库的 status 字符串 → 新库的整数枚举
STATUS_MAP = {
    "UN_SUBSCRIBE": CodeStatus.NONE,
    "CANCEL": CodeStatus.NONE,
    "SUBSCRIBE": CodeStatus.SUBSCRIBED,
    "STRICT": CodeStatus.SUBSCRIBED,
    "DOWNLOADING": CodeStatus.DOWNLOADING,
    "DOWNLOADED": CodeStatus.DOWNLOADED,
    "COMPLETE": CodeStatus.COMPLETED,
    "COMPLETED": CodeStatus.COMPLETED,
    "FAIL": CodeStatus.FAILED,
    "FAILED": CodeStatus.FAILED,
}

# 老库把图片路径存成 /pic/... 或 /pics/...，统一剥成 <番号>/banner.jpg
PIC_PREFIX = re.compile(r"^/?pics?/")


def _to_status(value: Any) -> int:
    """status 归一化成整数。无法识别的值当未订阅处理。"""
    if isinstance(value, bool):
        return CodeStatus.NONE
    if isinstance(value, int):
        return value if value in vars(CodeStatus).values() else CodeStatus.NONE
    mapped = STATUS_MAP.get(str(value or "").strip().upper())
    return CodeStatus.NONE if mapped is None else mapped


def _to_local_path(value: Any) -> str | None:
    """把 /pic/ABC-123/banner.jpg 剥成 ABC-123/banner.jpg。

    多张剧照是逗号分隔的一整串，逐个剥。
    """
    raw = (value or "") if isinstance(value, str) else ""
    if not raw.strip():
        return None
    parts = [PIC_PREFIX.sub("", p.strip()) for p in raw.split(",") if p.strip()]
    return ",".join(parts) or None


def _to_datetime(value: Any) -> datetime | None:
    """老库的时间列可能是字符串，PostgreSQL 不接受，转成 datetime。"""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# 需要转换的列：表名 → {列名: 转换函数}
CONVERTERS: dict[str, dict[str, Any]] = {
    "code": {
        "status": _to_status,
        "local_banner": _to_local_path,
        "local_still_photo": _to_local_path,
    },
}

DATETIME_COLUMNS = {"create_time", "update_time"}

# 老库允许为空、新库声明了 NOT NULL 的列，补个占位值免得整行被约束丢掉。
# history 里确实存在 code 为空但 save_path 有值的行，属于有效的下载记录。
NOT_NULL_FALLBACK: dict[str, dict[str, Any]] = {
    "history": {"code": ""},
}


@dataclass
class TableResult:
    table: str
    source_rows: int = 0
    migrated: int = 0
    skipped: int = 0
    error: str = ""


@dataclass
class MigrateResult:
    """一次迁移的完整结果，同时用于进度上报。"""
    source: str = ""
    target: str = ""
    running: bool = False
    finished: bool = False
    dry_run: bool = False
    current_table: str = ""
    tables: list[TableResult] = field(default_factory=list)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def total_migrated(self) -> int:
        return sum(t.migrated for t in self.tables)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": _mask_url(self.target),
            "running": self.running,
            "finished": self.finished,
            "dry_run": self.dry_run,
            "current_table": self.current_table,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_migrated": self.total_migrated,
            "tables": [
                {
                    "table": t.table,
                    "source_rows": t.source_rows,
                    "migrated": t.migrated,
                    "skipped": t.skipped,
                    "error": t.error,
                }
                for t in self.tables
            ],
        }


def mask_url(url: str) -> str:
    """连接串里的密码打码后才能回前端。"""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:****@", url or "")


_mask_url = mask_url  # 内部调用沿用旧名


# 迁移是长任务，进度存在模块级变量里供前端轮询
_lock = threading.Lock()
_progress: MigrateResult | None = None


def get_progress() -> dict | None:
    with _lock:
        return _progress.to_dict() if _progress else None


def list_sqlite_files() -> list[dict]:
    """列出 DATA_DIR 下可迁移的 SQLite 文件。

    只认真正的 SQLite 文件：读文件头的魔术字节，避免把 -wal / -shm 之类也列出来。
    """
    from app.database.session import DATA_DIR

    out: list[dict] = []
    if not DATA_DIR.is_dir():
        return out

    for path in sorted(DATA_DIR.glob("*.db")):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fp:
                if fp.read(16) != b"SQLite format 3\x00":
                    continue
            stat = path.stat()
        except OSError:
            continue

        out.append({
            "name": path.name,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "tables": _peek_tables(path),
        })
    return out


def _peek_tables(path: Path) -> dict[str, int]:
    """快速看一眼库里各表的行数，供前端展示。"""
    counts: dict[str, int] = {}
    try:
        engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
        names = set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            for model in TABLES:
                table = model.__tablename__
                if table not in names:
                    continue
                counts[table] = conn.execute(
                    select(func.count()).select_from(model.__table__)
                ).scalar_one()
        engine.dispose()
    except Exception as exc:
        logger.debug(f"读取 {path.name} 表信息失败: {exc}")
    return counts


def resolve_source(name: str) -> Path:
    """把前端传来的文件名解析成 DATA_DIR 下的绝对路径。

    只接受文件名，不接受路径，避免被指使去读容器里的任意文件。
    """
    from app.database.session import DATA_DIR

    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("非法的数据库文件名")

    path = (DATA_DIR / name).resolve()
    if path.parent != DATA_DIR.resolve():
        raise ValueError("只能选择数据目录下的文件")
    if not path.is_file():
        raise ValueError(f"文件不存在: {name}")
    return path


def _row_to_dict(row: Any, columns: list[str], table: str) -> dict:
    """结果行 → 可插入的 dict，顺带做字段转换。"""
    data = {col: getattr(row, col, None) for col in columns}

    for col, convert in CONVERTERS.get(table, {}).items():
        if col in data:
            data[col] = convert(data[col])

    for col in DATETIME_COLUMNS & set(data):
        data[col] = _to_datetime(data[col])

    for col, fallback in NOT_NULL_FALLBACK.get(table, {}).items():
        if col in data and data[col] is None:
            data[col] = fallback

    return data


def _iter_batches(
    source: Engine, model: type[DBBase], columns: list[str]
) -> Iterator[list[dict]]:
    """从源库按批读出行。

    用 yield_per 让 SQLAlchemy 流式取，避免 45000 行番号一次全进内存。
    """
    table = model.__tablename__
    stmt = select(*[model.__table__.c[col] for col in columns])

    with source.connect() as conn:
        result = conn.execution_options(yield_per=BATCH_SIZE).execute(stmt)
        for chunk in result.partitions(BATCH_SIZE):
            yield [_row_to_dict(row, columns, table) for row in chunk]


def migrate(source_name: str, target_url: str = "", dry_run: bool = False) -> dict:
    """把 SQLite 库搬到目标库。

    target_url 留空时用当前运行的数据库连接，也就是配置里的 DATABASE_URL。
    """
    global _progress

    with _lock:
        if _progress and _progress.running:
            raise RuntimeError("已有迁移任务在执行")

    source_path = resolve_source(source_name)
    target_url = _normalize_url(target_url) if target_url else ""

    if target_url and target_url.startswith("sqlite"):
        raise ValueError("目标库不能是 SQLite，请填写 PostgreSQL 连接串")

    # 不填目标就用当前连接；此时必须已经切到 PostgreSQL，否则等于原地搬运
    if not target_url:
        from app.database.session import DATABASE_URL, is_sqlite
        if is_sqlite():
            raise ValueError(
                "当前运行在 SQLite 上，请先填写 PostgreSQL 连接串，"
                "或在设置里配好 DATABASE_URL 并重启"
            )
        target_url = DATABASE_URL

    result = MigrateResult(
        source=source_path.name,
        target=target_url,
        running=True,
        dry_run=dry_run,
        started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    with _lock:
        _progress = result

    source_engine = create_engine(
        f"sqlite:///{source_path}", connect_args={"timeout": 30}
    )

    try:
        _run(source_engine, target_url, result)
    except Exception as exc:
        result.error = str(exc)
        logger.exception(f"迁移失败: {exc}")
    finally:
        source_engine.dispose()
        result.running = False
        result.finished = True
        result.current_table = ""
        result.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return result.to_dict()


def _run(source_engine: Engine, target_url: str, result: MigrateResult) -> None:
    """实际的搬运流程。"""
    from app.database.session import DATABASE_URL, _create_engine

    source_tables = set(inspect(source_engine).get_table_names())

    # 目标就是当前连接时直接复用现成的 SessionLocal，别再开一个池
    reuse_current = target_url == DATABASE_URL
    target_engine = None if reuse_current else _create_engine(target_url)

    try:
        if target_engine is not None:
            # 目标库可能还是空的，先建表
            DBBase.metadata.create_all(target_engine)
            from sqlalchemy.orm import sessionmaker
            make_session = sessionmaker(
                bind=target_engine, autoflush=False, expire_on_commit=False
            )
        else:
            make_session = SessionLocal

        for model in TABLES:
            table = model.__tablename__
            item = TableResult(table=table)
            result.tables.append(item)
            result.current_table = table

            if table not in source_tables:
                item.error = "源库中不存在该表"
                continue

            # 只搬两边都有的列，老库多出来的列（如 code.filter）忽略
            source_columns = {
                c["name"] for c in inspect(source_engine).get_columns(table)
            }
            columns = [c.name for c in model.__table__.columns if c.name in source_columns]
            if not columns:
                item.error = "没有可迁移的列"
                continue

            with source_engine.connect() as conn:
                item.source_rows = conn.execute(
                    select(func.count()).select_from(model.__table__)
                ).scalar_one()

            if result.dry_run:
                logger.info(f"[迁移试算] {table}: {item.source_rows} 行")
                continue

            # 以目标库的真实行数增量作为写入数。
            # ON CONFLICT/OR IGNORE 会把主键冲突和约束冲突一起吞掉，
            # 光看请求条数会把丢掉的行也算成成功。
            before = _count_rows(make_session, model)
            try:
                _copy_table(source_engine, make_session, model, columns, item)
            except Exception as exc:
                item.error = str(exc)
                logger.exception(f"迁移 {table} 失败: {exc}")
                continue

            item.migrated = max(0, _count_rows(make_session, model) - before)
            item.skipped = max(0, item.source_rows - item.migrated)

            logger.info(
                f"[迁移] {table}: 源 {item.source_rows} 行，写入 {item.migrated} 行"
                + (f"，跳过 {item.skipped} 行（已存在或不满足约束）" if item.skipped else "")
            )
    finally:
        if target_engine is not None:
            target_engine.dispose()


def _count_rows(make_session: Any, model: type[DBBase]) -> int:
    session = make_session()
    try:
        return session.execute(
            select(func.count()).select_from(model.__table__)
        ).scalar_one()
    finally:
        session.close()


def _copy_table(
    source_engine: Engine,
    make_session: Any,
    model: type[DBBase],
    columns: list[str],
    item: TableResult,
) -> None:
    """逐批把一张表搬过去。写入行数由调用方按目标库行数核算。"""
    session = make_session()
    try:
        for batch in _iter_batches(source_engine, model, columns):
            if not batch:
                continue
            try:
                batch_insert_ignore_duplicate(session, model, batch)
            except Exception as exc:
                # 整批失败时退化成逐行插，避免一条脏数据带走 500 条好数据
                session.rollback()
                logger.warning(f"{model.__tablename__} 批量写入失败，改逐行: {exc}")
                for row in batch:
                    try:
                        batch_insert_ignore_duplicate(session, model, [row])
                    except Exception as row_exc:
                        session.rollback()
                        logger.debug(f"{model.__tablename__} 跳过一行: {row_exc}")
    finally:
        session.close()
