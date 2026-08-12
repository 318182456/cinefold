"""内置数据源清单与配置读写。

地址落库而非写死在代码里：这些站换域名很频繁，改配置比改代码快。

`bypass_first` 标了 True 的站直连必定吃 403（Cloudflare 挑战页），
必须配 BYPASS_URL 才能抓；没配的话这些源开着也拿不到数据。
`parser` 指向已实现的解析器，为空表示尚未接入解析，只能做连通性测试。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from app.database.session import session_scope

# key, 显示名, 默认地址, 解析器, 直连是否必被拦
SOURCES: tuple[dict, ...] = (
    {"key": "javbus", "name": "JavBus", "host": "https://www.javbus.com",
     "parser": "bus", "bypass_first": False},
    {"key": "javdb", "name": "JavDB", "host": "https://javdb.com",
     "parser": "javdb", "bypass_first": False, "interval": 10.0},
    {"key": "javlibrary", "name": "JavLibrary", "host": "https://www.javlibrary.com/cn",
     "parser": "library", "bypass_first": True},
    {"key": "airav", "name": "Airav", "host": "https://airav.io/cn",
     "parser": "", "bypass_first": True},
    {"key": "avbase", "name": "Avbase", "host": "https://www.avbase.net",
     "parser": "avbase", "bypass_first": True, "interval": 2.0},
    {"key": "avmoo", "name": "Avmoo", "host": "https://avmoo.website",
     "parser": "", "bypass_first": False},
    {"key": "avsox", "name": "Avsox", "host": "https://avsox.click",
     "parser": "", "bypass_first": False},
    {"key": "carib", "name": "Caribbeancom", "host": "https://www.caribbeancom.com",
     "parser": "", "bypass_first": False},
    {"key": "dmm", "name": "DMM", "host": "https://www.dmm.co.jp",
     "parser": "", "bypass_first": False},
    {"key": "fc2", "name": "FC2", "host": "https://adult.contents.fc2.com",
     "parser": "", "bypass_first": False},
    {"key": "fc2hub", "name": "FC2 Hub", "host": "https://javten.com",
     "parser": "", "bypass_first": True},
    # 有解析器但数据会串号，默认停用；详见 freejavbt.py 的说明
    {"key": "freejavbt", "name": "FreeJavBT", "host": "https://freejavbt.com",
     "parser": "freejavbt", "bypass_first": False, "interval": 2.0, "enabled": False},
    {"key": "hbox", "name": "Hbox", "host": "https://hbox.jp",
     "parser": "", "bypass_first": False},
    {"key": "jav321", "name": "Jav321", "host": "https://www.jav321.com",
     "parser": "jav321", "bypass_first": False, "interval": 1.5},
    {"key": "madou", "name": "Madou", "host": "https://madou.club",
     "parser": "", "bypass_first": False},
    {"key": "madouqu", "name": "Madouqu", "host": "https://madouqu.com",
     "parser": "", "bypass_first": True},
    {"key": "mgstage", "name": "MGStage", "host": "https://www.mgstage.com",
     "parser": "", "bypass_first": True},
    {"key": "missav", "name": "MissAV", "host": "https://missav123.com",
     "parser": "missav", "bypass_first": True, "interval": 2.0},
    {"key": "7mmtv", "name": "7mmTV", "host": "https://7mmtv.sx/zh",
     "parser": "", "bypass_first": True},
    {"key": "theporndb", "name": "ThePornDB", "host": "https://api.theporndb.net",
     "parser": "", "bypass_first": False},
    {"key": "xchina", "name": "XChina", "host": "https://xchina.co",
     "parser": "", "bypass_first": False},
)

SOURCE_MAP: dict[str, dict] = {item["key"]: item for item in SOURCES}

# 已接入解析的源，只有这些能真正抓到数据
PARSERS: dict[str, str] = {
    item["key"]: item["parser"] for item in SOURCES if item["parser"]
}

# 不允许删除的核心源。这三个是抓取链路的主力，全删光会让整个应用
# 没有可用数据源；停用可以，删除不行
PROTECTED: frozenset[str] = frozenset({"javbus", "javdb", "javlibrary"})

# 落到这些路径说明还停在前置校验页，没真正进站。
# 过盾服务也可能返回 200 + 校验页（javbus 就是这样），因此直连与
# 过盾两条路径都要查。
VERIFY_MARKS: tuple[str, ...] = ("age_check", "age-check", "driver-verify")

# 首页挂了前置校验、但内容页正常的源：测首页会误报不可用。
# javbus 首页固定跳 /doc/driver-verify（过盾也一样），而详情页可直接抓，
# 所以拿一个真实番号页当探针。
CHECK_PATH: dict[str, str] = {"javbus": "/SSIS-001"}


def is_builtin(key: str) -> bool:
    """是不是内置源。不在清单里的都是用户自己加的。"""
    return key in SOURCE_MAP


def is_protected(key: str) -> bool:
    return key in PROTECTED


def sync_builtin_sources() -> int:
    """把内置源补进库。已存在的不覆盖，用户改过的地址要保留。

    被软删除的源不会重新登记 —— 否则用户删掉的源下次启动就复活了。
    要找回来走 restore_builtin_source()。
    """
    from app.database.models import DataSource

    added = 0
    with session_scope() as session:
        # 含已软删除的，这些 key 不该被当成"缺失"再插一遍
        existing = {row.key for row in session.scalars(select(DataSource)).all()}
        for index, item in enumerate(SOURCES):
            if item["key"] in existing:
                continue
            session.add(DataSource(
                key=item["key"],
                name=item["name"],
                host=item["host"],
                # 数据质量有问题的源默认停用，由用户显式开启
                enabled=item.get("enabled", True),
                interval=item.get("interval", 0.0),
                priority=index,
                bypass_first=item.get("bypass_first", False),
            ))
            added += 1

    if added:
        logger.info(f"已登记 {added} 个内置数据源")
    return added


def restore_builtin_source(key: str) -> bool:
    """把软删除的内置源找回来，地址等配置一并重置为默认值。

    用户删源多半是因为把配置改坏了，恢复时连带重置比只清 deleted 标记有用。
    """
    from app.database.models import DataSource

    item = SOURCE_MAP.get(key)
    if item is None:
        return False

    index = next(i for i, s in enumerate(SOURCES) if s["key"] == key)
    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is None:
            session.add(DataSource(
                key=key,
                name=item["name"],
                host=item["host"],
                enabled=item.get("enabled", True),
                interval=item.get("interval", 0.0),
                priority=index,
                bypass_first=item.get("bypass_first", False),
            ))
            return True

        row.deleted = False
        row.name = item["name"]
        row.host = item["host"]
        row.enabled = item.get("enabled", True)
        row.interval = item.get("interval", 0.0)
        row.priority = index
        row.bypass_first = item.get("bypass_first", False)
        # 连通性结果是删除前的旧数据，留着会误导
        row.status = None
        row.status_message = None
        row.checked_time = None

    return True


def get_source(key: str) -> dict | None:
    """取单个源的配置，库里没有则回退到内置清单。"""
    from app.database.models import DataSource

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        # 已软删除的直接当不存在，不能落到下面的内置清单兜底
        if row is not None and row.deleted:
            return None
        if row is not None:
            return {
                "key": row.key,
                "name": row.name,
                "host": row.host,
                "enabled": row.enabled,
                "interval": row.interval,
                "cookie": row.cookie or "",
                "bypass_first": row.bypass_first,
                "parser": SOURCE_MAP.get(key, {}).get("parser", ""),
            }

    item = SOURCE_MAP.get(key)
    if item is None:
        return None
    return {**item, "enabled": True, "cookie": "", "interval": item.get("interval", 0.0)}


def _check_direct(url: str, timeout: float) -> tuple[str, str]:
    """直连测一个地址，返回 (status, message)。"""
    import httpx

    from app.core.config import get_settings

    settings = get_settings()
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            proxy=settings.proxy or None,
            verify=False,
        ) as client:
            response = client.get(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            })
    except httpx.TimeoutException:
        return "fail", f"连接超时（{timeout:.0f}s）"
    except Exception as exc:
        return "fail", f"{type(exc).__name__}: {exc}"[:200]

    code = response.status_code
    if code in (403, 503):
        return "blocked", f"HTTP {code}，被反爬拦截"
    if code >= 400:
        return "fail", f"HTTP {code}"

    final = str(response.url)
    if any(mark in final for mark in VERIFY_MARKS):
        return "blocked", f"跳转到前置校验页：{final[:80]}"
    if final.rstrip("/") != url.rstrip("/"):
        return "ok", f"跳转到 {final[:80]}"
    return "ok", ""


def _check_via_bypass(url: str) -> tuple[str, str]:
    """走过盾服务测一个地址，返回 (status, message)。

    抓取链路对 bypass_first 的站就是这么取页面的，测试必须走同一条路，
    否则页面上报的"不通"跟实际抓取能力对不上。
    """
    from app.modules.ladysite.base import fetch_via_bypass

    try:
        html = fetch_via_bypass(url, quick=True)
    except Exception as exc:
        return "fail", f"绕过服务异常：{type(exc).__name__}: {exc}"[:200]

    if not html:
        return "blocked", "绕过服务未返回内容，可能未运行或过盾失败"
    # 过盾服务可能拿回 200 的校验页，内容里仍是人机验证
    if any(mark in html for mark in VERIFY_MARKS):
        return "blocked", "绕过服务返回前置校验页，未真正进站"
    return "ok", "经绕过服务连通"


def check_source(key: str, timeout: float = 12.0) -> dict:
    """测一个源的连通性，结果写回库供页面展示。

    与抓取链路保持一致（见 base.SiteClient.get）：bypass_first 的站直接走
    过盾服务，其余站直连；直连被拦时若配了过盾服务则再复测一次。
    否则这些站测试永远显示被拦，而实际抓取是通的。

    区分"通"与"被盾拦"：Cloudflare 挑战页也是能连上的，
    只报连通会让用户以为可以抓，实际一条都拿不到。
    """
    from app.core.config import get_settings
    from app.database.models import DataSource
    from datetime import datetime

    source = get_source(key)
    if source is None:
        return {"key": key, "status": "fail", "message": f"未知数据源 {key}"}

    host = (source.get("host") or "").rstrip("/")
    if not host:
        return {"key": key, "status": "fail", "message": "未配置地址"}

    has_bypass = bool(get_settings().bypass_url)
    # 首页有前置校验的源改探内容页，否则测的是校验页而非站点本身
    url = f"{host}{CHECK_PATH.get(key, '')}"

    if source.get("bypass_first") and has_bypass:
        status, message = _check_via_bypass(url)
    else:
        status, message = _check_direct(url, timeout)
        # 直连被拦，抓取链路此时会改走过盾服务，测试也照做
        if status == "blocked":
            if not has_bypass:
                message = f"{message}。需配置反爬绕过服务"
            else:
                bypass_status, bypass_message = _check_via_bypass(url)
                status = bypass_status
                message = (
                    bypass_message if bypass_status == "ok"
                    else f"{message}；{bypass_message}"
                )

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is not None:
            row.status = status
            row.status_message = message
            row.checked_time = datetime.now()

    return {"key": key, "status": status, "message": message}


def enabled_parser_sources() -> list[dict]:
    """已启用且有解析器的源，按优先级排序。

    抓取链路只认这些；没有解析器的源开着也抓不到东西。
    """
    from app.database.models import DataSource

    with session_scope() as session:
        rows = session.scalars(
            select(DataSource)
            .where(DataSource.enabled.is_(True), DataSource.deleted.is_(False))
            .order_by(DataSource.priority)
        ).all()
        out = []
        for row in rows:
            parser = SOURCE_MAP.get(row.key, {}).get("parser", "")
            if not parser:
                continue
            out.append({
                "key": row.key,
                "host": row.host,
                "interval": row.interval,
                "cookie": row.cookie or "",
                "bypass_first": row.bypass_first,
                "parser": parser,
            })
        return out
