"""爬虫库读取与导入。

对方是 Immortal（闭源，Docker 镜像 envyafish/immortal）自己的 PostgreSQL。
我们只读、不写、不建表 —— 那是别人的系统，这里只是个消费者。

源表两张：
- movie：番号情报（number/title/poster/banner/cast_names…），对应本地 Code
- cast：演员（name/cn_name/photo），对应本地 Actor

还有一张 article 是资源表（50 万条磁链，按 match_number 关联番号），
这里不碰：cinefold 已经通过 Immortal 的 HTTP 接口
(/api/v1/articles/torrents) 把它当自定义 BT 源在用，再从库里读一遍
等于同一份数据两条路进来，反而要处理去重。

连接串走 CRAWLER_DB_DSN，为空则整个功能不启用。
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger

from app.core.config import get_settings

# 单次任务最多导入多少条，防止首次全量把库和日志刷爆。0 表示不限
DEFAULT_LIMIT = 5000

# 番号形如 ABP-554 / SSIS-001 / FC2-4869095。
# movie.number 实测已经是这个格式（Immortal 侧归一化过），这里只做兜底校验
CODE_RE = re.compile(r"^[A-Z0-9]{2,10}(?:-[A-Z0-9]+){1,3}$")

# movie 列 → 本地 Code 列。按实测样本确定，几处容易取错的：
#
# - poster 是 ps.jpg（竖版小图）、banner 是 pl.jpg（横版大图），
#   与本地同名字段的用途正好对上，别对调
# - casts 是 cast.id 的外键数组（{1}），cast_names 才是名字（{伊藤舞雪}），
#   取前者会往库里写一串数字
# - cn_title / description / duration / genres 实测全为空。映射留着是因为
#   对方以后可能补上，届时不用改代码；空值本来就会被跳过
MOVIE_FIELDS = {
    "number": "code",
    "title": "title",
    "cn_title": "cn_title",
    "release_date": "release_date",
    "poster": "poster",
    "banner": "banner",
    "still_photos": "still_photo",
    "genres": "genres",
    "cast_names": "casts",
    "series": "series",
    "producer": "producer",
    "publisher": "publisher",
}

# cast 列 → 本地 Actor 列。
# 本地主键是 name（日文名），中文名放 name_2 —— 那个字段本就是别名用途
CAST_FIELDS = {
    "name": "name",
    "cn_name": "name_2",
    "photo": "photo",
}


class CrawlerDBError(RuntimeError):
    """连不上或查不动爬虫库。调用方据此区分「没数据」和「没问成」。"""


def _dsn() -> str:
    dsn = (get_settings().crawler_db_dsn or "").strip()
    if not dsn:
        raise CrawlerDBError("未配置 CRAWLER_DB_DSN")
    return dsn


def _connect():
    """打开连接。psycopg 是 requirements 里已有的依赖，不额外引入驱动。"""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署问题
        raise CrawlerDBError("缺少 psycopg，无法连接爬虫库") from exc

    try:
        # 只读用途，超时给短一点：卡住的连接会拖住整个定时任务
        return psycopg.connect(_dsn(), connect_timeout=10)
    except Exception as exc:
        raise CrawlerDBError(f"连接爬虫库失败: {exc}") from exc


def _query(sql: str, params: list | None = None) -> list[dict]:
    """执行查询，返回 dict 列表。"""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params or None)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
    except CrawlerDBError:
        raise
    except Exception as exc:
        raise CrawlerDBError(f"查询爬虫库失败: {exc}") from exc


def test_connection() -> tuple[bool, str]:
    """连通性测试，供页面上点一下看结果。"""
    try:
        # 顺带报出两张表的行数：连上了但表是空的也是一种失败，
        # 只回「连接正常」会让人以为没问题
        rows = _query('SELECT (SELECT count(*) FROM movie) AS m, '
                      '(SELECT count(*) FROM "cast") AS c')
        return True, f"连接正常，movie {rows[0]['m']} 条、cast {rows[0]['c']} 条"
    except CrawlerDBError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"连接异常: {exc}"


def probe_schema() -> list[dict]:
    """列出爬虫库的表和列。

    对方的表结构不归我们管，升级也可能改。留个能随时问的入口 ——
    排查「导进来全是空」时第一步就是看它。
    """
    return _query("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)


# ----------------------------------------------------------------------
# 值清洗
# ----------------------------------------------------------------------
def _normalize_code(value: Any) -> str:
    """番号统一成大写去空白。认不出格式的返回空串，由调用方丢弃。"""
    code = str(value or "").strip().upper()
    code = code.replace("_", "-").replace("－", "-")
    code = re.sub(r"\s+", "", code)
    return code if CODE_RE.match(code) else ""


def _join_array(value: Any) -> str:
    """PG 数组 → 逗号分隔文本，顺带去重。

    实测 still_photos 会把同一批图重复两遍（CAWD-940 的 18 张存了 36 条），
    原样入库等于让前端多渲染一倍。去重保留原顺序。
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return ",".join(out)


def _clean(value: Any) -> Any:
    """空值统一成 None，交给调用方跳过。

    空字符串必须当成空：写进去会占住字段，让本地后续的补全逻辑
    以为「已经有值了」而不再去抓。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return _join_array(value) or None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _row_to_item(row: dict, fields: dict[str, str]) -> dict:
    """源表一行 → 本地字段 dict。空值不写进去。"""
    item: dict = {}
    for src, dst in fields.items():
        if src not in row:
            continue
        value = _clean(row[src])
        if value is None:
            continue
        # 日期列是 date 对象，本地存字符串
        if dst == "release_date" and not isinstance(value, str):
            value = value.strftime("%Y-%m-%d")
        item[dst] = value
    return item


# ----------------------------------------------------------------------
# movie → Code
# ----------------------------------------------------------------------
def fetch_movies(limit: int = 0, since: str = "") -> list[dict]:
    """读番号情报。since 非空时只取该时间之后更新的行。"""
    columns = ", ".join(f'"{c}"' for c in MOVIE_FIELDS)
    sql = f"SELECT {columns} FROM movie"
    params: list = []

    if since:
        # update_time 可能为空（入过库但没更新过），那种行也要带上，
        # 否则新导入的番号会因为 NULL 被漏掉
        sql += " WHERE coalesce(update_time, create_time) >= %s"
        params.append(since)

    sql += ' ORDER BY "id"'
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    items: list[dict] = []
    dropped = 0
    for row in _query(sql, params):
        item = _row_to_item(row, MOVIE_FIELDS)
        code = _normalize_code(item.get("code"))
        if not code:
            dropped += 1
            continue
        item["code"] = code
        items.append(item)

    if dropped:
        # 丢掉多少要说出来。静默跳过的话，「导入 0 条」和
        # 「番号列取错了」看起来一模一样
        logger.warning(f"[爬虫库] {dropped} 行番号格式不合法，已跳过")
    return items


def import_movies(limit: int = 0, since: str = "") -> int:
    """导入番号情报，返回落库条数。"""
    from app import services

    limit = limit or get_settings().crawler_db_limit or DEFAULT_LIMIT

    items = fetch_movies(limit=limit, since=since)
    if not items:
        logger.info("[爬虫库] 没有可导入的番号")
        return 0

    # 复用 cache_remote_codes：只补空字段、不动 status。前者保住本地已翻译的
    # cn_title 和刮削过的数据，后者保证导入不会让番号自动进下载流程
    saved = services.cache_remote_codes(items)
    logger.info(f"[爬虫库] movie 读到 {len(items)} 条，落库 {saved} 条")
    return saved


# ----------------------------------------------------------------------
# cast → Actor
# ----------------------------------------------------------------------
def fetch_casts(limit: int = 0) -> list[dict]:
    """读演员信息。"""
    # cast 是 SQL 保留字，不加引号会被解析成 CAST(...) 而语法报错
    columns = ", ".join(f'"{c}"' for c in CAST_FIELDS)
    sql = f'SELECT {columns} FROM "cast" ORDER BY "id"'
    params: list = []
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    items: list[dict] = []
    for row in _query(sql, params):
        item = _row_to_item(row, CAST_FIELDS)
        # name 是本地主键，没有就整条丢掉
        if item.get("name"):
            items.append(item)
    return items


def import_casts(limit: int = 0) -> int:
    """导入演员，返回落库条数。

    与番号一样只补空字段：本地的 limit_date 是用户自己设的订阅起点，
    photo 也可能手工换过，不能被爬虫库冲掉。
    """
    from app.database.models.actor import Actor
    from app.database.session import session_scope

    items = fetch_casts(limit=limit)
    if not items:
        logger.info("[爬虫库] 没有可导入的演员")
        return 0

    saved = 0
    with session_scope() as session:
        for item in items:
            name = item["name"]
            row = session.get(Actor, name)
            if row is None:
                row = Actor(name=name)
                session.add(row)

            for key, value in item.items():
                if key == "name" or not value:
                    continue
                if hasattr(row, key) and not getattr(row, key):
                    setattr(row, key, value)
            saved += 1

    logger.info(f"[爬虫库] cast 读到 {len(items)} 条，落库 {saved} 条")
    return saved


# ----------------------------------------------------------------------
def import_all(limit: int = 0, since: str = "") -> int:
    """导入番号和演员，返回总条数。"""
    total = import_movies(limit=limit, since=since)
    try:
        total += import_casts(limit=limit)
    except CrawlerDBError as exc:
        # 演员导入失败不该让已经成功的番号导入白跑
        logger.warning(f"[爬虫库] 演员导入失败: {exc}")
    return total
