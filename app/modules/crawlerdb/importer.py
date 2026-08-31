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

# 每多少条提交一次。整批一个事务的话，中间一条坏数据会让全部回滚
COMMIT_BATCH = 200

# 被跳过的番号最多列几条。给得比常见数量宽一点：这个日志的用处就是
# 判断正则是不是卡太严，只露前十条会看不出规律
MAX_LOGGED_CODES = 50

# 增量水位存在 Cache 表的这个 namespace 下
WATERMARK_NS = "crawlerdb"
WATERMARK_KEY = "movie_since"

# 水位往回退这么多分钟再查。对方入库和 update_time 落盘之间有时间差，
# 卡着上次的时刻查会漏掉边界上那几条
WATERMARK_LAG_MINUTES = 10

# 番号形如 ABP-554 / SSIS-001 / FC2-4869095 / 032426-100（日期式）/
# T-28579（单字母前缀）/ CARIBBEANCOM-010112-001（长前缀多段），
# 以及不带连字符的国产番号 MD0329 / SATM034 / MDCN0001 / JDSY210。
#
# movie.number 实测已经是规范格式（Immortal 侧归一化过），这里只做兜底：
# 拦掉明显不是番号的脏数据，而不是替对方做格式判断 —— 卡太严会把正经
# 番号一起丢掉（先后误杀过 42 行单字母/长前缀、18 行无连字符国产番号）。
#
# 无连字符那支单独限定「字母前缀 + 数字」，不是简单把 {1,3} 放成 {0,3}：
# 后者会连 1234567、A、12 这种一起放行，兜底就等于没有了。
CODE_RE = re.compile(
    r"^(?:[A-Z]{2,10}[0-9]{2,6}|[A-Z0-9]{1,16}(?:-[A-Z0-9_]+){1,3})$"
)

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

    # 番号去重：源库同一个番号可能有多行（id 不同、number 相同，实测存在，
    # 如 OLM-332E）。落库端按主键逐条 session.get 查不到同批未提交的行，
    # 重复番号会变成两条 INSERT，commit 时撞 code_pkey 让整批回滚。
    #
    # 合并而不是只留一条：同番号的几行往往互补（一行有封面没系列，
    # 另一行反过来），只取第一条会平白丢掉后面那些字段
    merged: dict[str, dict] = {}
    dropped: list[str] = []
    duplicated = 0

    for row in _query(sql, params):
        item = _row_to_item(row, MOVIE_FIELDS)
        code = _normalize_code(item.get("code"))
        if not code:
            # 记下原值而不只是计数：光有"跳过 42 行"没法判断是正则卡太严
            # 误杀了正经番号，还是对方那边本来就有脏数据
            dropped.append(str(row.get("number") or "")[:40] or "(空)")
            continue
        item["code"] = code

        exist = merged.get(code)
        if exist is None:
            merged[code] = item
            continue

        duplicated += 1
        # 先到的优先（按 id 升序，即先入库的那条），后来的只补空缺
        for key, value in item.items():
            exist.setdefault(key, value)

    if dropped:
        # 丢掉多少要说出来，还要说清丢的是什么。静默跳过的话，
        # 「导入 0 条」和「番号列取错了」看起来一模一样
        logger.warning(
            f"[爬虫库] {len(dropped)} 行番号格式不合法，已跳过：\n  "
            + "\n  ".join(dropped[:MAX_LOGGED_CODES])
            + (f"\n  …另有 {len(dropped) - MAX_LOGGED_CODES} 条"
               if len(dropped) > MAX_LOGGED_CODES else "")
        )
    if duplicated:
        logger.info(f"[爬虫库] 合并了 {duplicated} 行重复番号")

    return list(merged.values())


def _read_watermark() -> str:
    """上次导入到哪个时间点。没有记录返回空串，即走全量。

    读不出来（表还没建、库锁着）一律当没有：退化成全量导入是安全的，
    重复导入本身是幂等的，而让整个任务因为读不到水位就失败不值得。
    """
    from sqlalchemy import select

    from app.database.models import Cache
    from app.database.session import session_scope

    try:
        with session_scope() as session:
            row = session.scalar(
                select(Cache).where(
                    Cache.namespace == WATERMARK_NS, Cache.key == WATERMARK_KEY
                )
            )
            return (row.content or "") if row else ""
    except Exception as exc:
        logger.debug(f"[爬虫库] 读增量水位失败，这轮走全量: {exc}")
        return ""


def _write_watermark(value: str) -> None:
    """记下这轮导入到哪。写失败只记日志 —— 数据已经入库了，
    水位没写上顶多下轮多跑一次全量，不该反过来让任务报错。"""
    from datetime import datetime

    from sqlalchemy import select

    from app.database.models import Cache
    from app.database.session import session_scope

    try:
        with session_scope() as session:
            row = session.scalar(
                select(Cache).where(
                    Cache.namespace == WATERMARK_NS, Cache.key == WATERMARK_KEY
                )
            )
            if row is None:
                session.add(
                    Cache(namespace=WATERMARK_NS, key=WATERMARK_KEY, content=value)
                )
            else:
                row.content = value
                row.create_time = datetime.now()
    except Exception as exc:
        logger.debug(f"[爬虫库] 写增量水位失败: {exc}")


def import_movies(limit: int = 0, since: str = "", full: bool = False) -> int:
    """导入番号情报，返回落库条数。

    默认走增量：只取上次导入之后变动的行。首次或 full=True 时全量。
    """
    from datetime import datetime, timedelta

    from app import services

    limit = limit or get_settings().crawler_db_limit or DEFAULT_LIMIT

    # 记在查询之前：查询期间对方可能又写入了新行，用查完的时刻当水位
    # 会把那几条永远漏掉
    started = datetime.now()

    if not since and not full:
        since = _read_watermark()

    items = fetch_movies(limit=limit, since=since)
    if not items:
        logger.info("[爬虫库] 没有可导入的番号")
        return 0

    # 分批提交。cache_remote_codes 一个事务写完整批，中间任何一条出问题
    # （字段超长、编码异常等）都会让整批回滚 —— 实测撞过一次主键冲突，
    # 973 条全部丢失。切小之后坏数据只连累同批的几百条，其余照常入库
    saved = 0
    failed = 0
    for start in range(0, len(items), COMMIT_BATCH):
        batch = items[start:start + COMMIT_BATCH]
        try:
            # 复用 cache_remote_codes：只补空字段、不动 status。前者保住
            # 本地已翻译的 cn_title 和刮削过的数据，后者保证导入不会让
            # 番号自动进下载流程
            saved += services.cache_remote_codes(batch)
        except Exception as exc:
            failed += len(batch)
            logger.warning(
                f"[爬虫库] 第 {start // COMMIT_BATCH + 1} 批落库失败"
                f"（{len(batch)} 条）: {exc}"
            )

    if failed:
        logger.warning(f"[爬虫库] movie 有 {failed} 条未能入库")

    # 有批次失败就不推进水位：推了的话那些条下轮不会再被查到，等于永久丢失。
    # 重复导入是幂等的（只补空字段），代价只是多跑一遍
    if not failed:
        watermark = (started - timedelta(minutes=WATERMARK_LAG_MINUTES))
        _write_watermark(watermark.strftime("%Y-%m-%d %H:%M:%S"))

    scope = "全量" if not since else "增量"
    logger.info(f"[爬虫库] movie {scope}读到 {len(items)} 条，落库 {saved} 条")
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

    # 与番号同理：源表主键是 id，同名演员可能有多行，而本地主键是 name。
    # 不去重会在 commit 时撞 actor_pkey
    merged: dict[str, dict] = {}
    nameless = 0
    for row in _query(sql, params):
        item = _row_to_item(row, CAST_FIELDS)
        # name 是本地主键，没有就整条丢掉
        name = item.get("name")
        if not name:
            nameless += 1
            continue
        exist = merged.get(name)
        if exist is None:
            merged[name] = item
        else:
            for key, value in item.items():
                exist.setdefault(key, value)

    if nameless:
        logger.warning(f"[爬虫库] {nameless} 行演员没有名字，已跳过")
    return list(merged.values())


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

    # 与番号同样分批：一条坏数据不该连累整批
    saved = 0
    failed = 0
    for start in range(0, len(items), COMMIT_BATCH):
        batch = items[start:start + COMMIT_BATCH]
        try:
            changed = 0
            with session_scope() as session:
                for item in batch:
                    name = item["name"]
                    row = session.get(Actor, name)
                    touched = False
                    if row is None:
                        row = Actor(name=name)
                        session.add(row)
                        touched = True

                    for key, value in item.items():
                        if key == "name" or not value:
                            continue
                        if hasattr(row, key) and not getattr(row, key):
                            setattr(row, key, value)
                            touched = True
                    changed += touched
            # 只算真正写进去的：演员表每轮全量查，报"处理了 1323 条"
            # 每次都一样，看不出这轮有没有新人
            saved += changed
        except Exception as exc:
            failed += len(batch)
            logger.warning(f"[爬虫库] 演员第 {start // COMMIT_BATCH + 1} 批落库失败: {exc}")

    if failed:
        logger.warning(f"[爬虫库] cast 有 {failed} 条未能入库")
    logger.info(f"[爬虫库] cast 读到 {len(items)} 条，落库 {saved} 条")
    return saved


# ----------------------------------------------------------------------
def import_all(limit: int = 0, since: str = "", full: bool = False) -> int:
    """导入番号和演员，返回总条数。

    番号走增量（按 update_time 水位），演员每次全量 —— 只有一千多条，
    查一次不到一秒，为它单独维护一个水位不划算。
    """
    total = import_movies(limit=limit, since=since, full=full)
    try:
        total += import_casts(limit=limit)
    except CrawlerDBError as exc:
        # 演员导入失败不该让已经成功的番号导入白跑
        logger.warning(f"[爬虫库] 演员导入失败: {exc}")
    return total
