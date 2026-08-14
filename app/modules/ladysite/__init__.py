"""资源站聚合入口。

对上层暴露与站点无关的接口；站点失败时自动尝试下一个。
"""
from __future__ import annotations

import time
from importlib import import_module

from loguru import logger

from app.core.config import get_settings
from app.modules.ladysite.base import ActorInfo, CodeInfo
from app.utils import get_true_code

# 详情抓取的优先顺序。
# javbus 无 Cloudflare 防护、成功率更高，排在前面；javdb 作为补充。
# jav321 详情页 URL 可推算、直连可用，不必过盾，因此排在 missav 之前；
# missav 自带中文标题，但依赖过盾服务，放后面。
# 其后是补充源：avmoo 与 javbus 同构（作为它挂掉时的备份），dmm 是官方
# 一手数据但要过年龄门，airav/7mmtv/xchina 提供中文标题，hbox 字段少。
DETAIL_SITES = (
    "javbus", "javdb", "jav321", "avbase", "missav",
    "avmoo", "dmm", "airav", "7mmtv", "hbox", "xchina",
)

# 一次抓取最多同时问几个站。源多了不能全铺开：过盾服务本身是串行的
# （base._BYPASS_LOCK），排在后面的过盾请求只会干等；线程再多也没用，
# 反而把连接数与内存吃掉。取最快结果的策略下，前几个站命中即可返回。
MAX_PARALLEL_SITES = 6

# 实现了 search_actor 的源。其余源查演员必然空手而归，
# _fetch_actor_photo 虽有 hasattr 兜底，但先筛掉能省下建实例的开销
ACTOR_SITES = frozenset({"javbus", "javdb", "avmoo", "avsox"})

# 只对特定番号形态有意义的源，不放进 DETAIL_SITES：每次查询都白开几个
# 线程去撞必然 404 的地址不划算。它们由番号规则决定何时参与（见
# _sites_for_code），规则本身配在数据源页面上、存在 datasource.code_rule。
SPECIAL_SITES: tuple[str, ...] = (
    "fc2", "fc2hub", "carib", "mgstage", "avsox", "madou", "madouqu", "theporndb",
)


def _has_theporndb_token() -> bool:
    """theporndb 的 API Token 配了没。Token 存在数据源的 Cookie 栏。"""
    try:
        from app.modules.ladysite.sources import get_source
        return bool((get_source("theporndb") or {}).get("cookie", "").strip())
    except Exception:
        return False


# key → (模块, 类名)。延迟导入：一次抓取只会用到其中几个，
# 全量导入等于每次都把 21 个模块连带 pyquery 的解析开销都拉起来。
_SITE_CLASSES: dict[str, tuple[str, str]] = {
    "javdb": ("javdb", "Avdb"),
    "javbus": ("bus", "Bus"),
    "javlibrary": ("library", "Library"),
    "missav": ("missav", "MissAv"),
    "jav321": ("jav321", "Jav321"),
    "avbase": ("avbase", "Avbase"),
    "freejavbt": ("freejavbt", "FreeJavBt"),
    "avmoo": ("avmoo", "Avmoo"),
    "avsox": ("avmoo", "Avsox"),
    "dmm": ("dmm", "Dmm"),
    "mgstage": ("mgstage", "Mgstage"),
    "fc2": ("fc2", "Fc2"),
    "fc2hub": ("fc2", "Fc2Hub"),
    "airav": ("airav", "Airav"),
    "7mmtv": ("mmtv", "Mmtv"),
    "carib": ("carib", "Carib"),
    "theporndb": ("theporndb", "ThePornDb"),
    "madou": ("madou", "Madou"),
    "madouqu": ("madou", "Madouqu"),
    "xchina": ("xchina", "Xchina"),
    "hbox": ("hbox", "Hbox"),
}


def _get_site(name: str):
    entry = _SITE_CLASSES.get(name)
    if entry is None:
        return None

    module_name, class_name = entry
    try:
        module = import_module(f"app.modules.ladysite.{module_name}")
        return getattr(module, class_name)()
    except Exception as exc:
        logger.debug(f"初始化站点 {name} 失败: {exc}")
        return None


def _enabled_sites() -> tuple[str, ...]:
    """MAIN_SITE 为 ALL 时用全部站点，否则只用指定的。

    顺序以数据源页面上排的优先级为准（enabled_parser_sources 已按 priority
    排好），DETAIL_SITES 只作为兜底顺序 —— 用户在页面上调了顺序就该生效，
    否则那个排序功能是摆设。

    数据源页面上停用的站会被排除；全停时退回 DETAIL_SITES，
    免得配置失手把抓取彻底关死。
    """
    main = (get_settings().main_site or "ALL").strip().lower()
    if main in ("", "all"):
        sites = DETAIL_SITES + SPECIAL_SITES
    else:
        # MAIN_SITE 也可以指定只对特定番号生效的源（如 fc2），
        # 因此在两份清单里找
        known = DETAIL_SITES + SPECIAL_SITES
        sites = tuple(s for s in known if s == main) or DETAIL_SITES

    try:
        from app.modules.ladysite.sources import enabled_parser_sources
        ordered = [item["key"] for item in enabled_parser_sources()]
    except Exception as exc:
        logger.debug(f"读取数据源开关失败，按全部启用处理: {exc}")
        return tuple(s for s in sites if s not in SPECIAL_SITES) or sites

    # 按库里的优先级重排。不在 ordered 里的是被停用/删除的，照旧排除；
    # 全被排掉时退回 DETAIL_SITES，免得配置失手把抓取彻底关死
    return tuple(s for s in ordered if s in sites) or DETAIL_SITES


def _sites_for_code(code: str) -> tuple[str, ...]:
    """给定番号该问哪些源。

    每个源自带番号规则（datasource.code_rule，页面上可改），这里按规则过滤：
    拿 SSIS-001 去问只收 FC2 的 carib/fc2 是必然的 404，白占一个线程和
    一次请求；反过来 FC2 番号问日系源同样是白跑。

    规则全都不匹配时退回通用清单 —— 认不出的番号宁可多问几个源，
    也好过一个都不问。
    """
    sites = _enabled_sites()
    normalized = get_true_code(code)
    if not normalized:
        return sites

    # MAIN_SITE 指定了单站时不做过滤，用户的显式选择优先
    main = (get_settings().main_site or "ALL").strip().lower()
    if main not in ("", "all"):
        return sites

    try:
        from app.modules.ladysite.sources import (
            SOURCE_MAP, code_allowed, enabled_parser_sources,
        )
        # 一次查库拿全部规则，逐个 get_source 会打 20 多次查询
        rules = {item["key"]: item.get("code_rule", "") for item in enabled_parser_sources()}
    except Exception as exc:
        logger.debug(f"读取番号规则失败，按不限制处理: {exc}")
        return sites

    matched, skipped = [], []
    for key in sites:
        # 库里没有这行时回落到内置默认规则（首次启动、表刚建好）
        rule = rules.get(key)
        if rule is None:
            rule = SOURCE_MAP.get(key, {}).get("code_rule", "")
        if code_allowed(normalized, rule):
            matched.append(key)
        else:
            skipped.append(key)

    if matched:
        if skipped:
            logger.debug(
                f"[{normalized}] 番号规则过滤掉 {len(skipped)} 个源：{', '.join(skipped)}"
            )
        return tuple(matched)

    # 一个都没匹配上：番号形态很可能是规则没覆盖到的，退回通用清单
    fallback = tuple(s for s in sites if s not in SPECIAL_SITES)
    logger.debug(f"[{normalized}] 没有源的番号规则匹配，退回通用清单")
    return fallback or sites


# ----------------------------------------------------------------------
# 详情
# ----------------------------------------------------------------------
def get_code_detail(code: str) -> dict:
    """抓取番号详情，返回可直接写库的字典。

    多个站点并发抓，谁先给出结果就用谁。串行 fallback 时前一个站点
    卡住或超时，后面的要干等——两站不同域名，节流互不影响，没必要排队。

    全过程打日志：选了哪些源、每个源返回什么、最终用了谁。抓不到东西时
    光看结果分不清是"番号不存在"还是"源被规则过滤光了/全都超时"，
    没有这些日志只能靠猜。
    """
    if not code:
        return {}

    sites = _sites_for_code(code)
    if not sites:
        logger.warning(f"[{code}] 没有可用数据源，检查数据源开关与番号规则")
        return {}

    logger.info(f"[{code}] 检索数据源（共 {len(sites)} 个）：{', '.join(sites)}")
    started = time.perf_counter()

    if len(sites) == 1:
        detail = _fetch_detail(sites[0], code)
        _log_detail_result(code, sites[0] if detail else "", detail, started, sites)
        return detail

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    pool = ThreadPoolExecutor(max_workers=min(len(sites), MAX_PARALLEL_SITES))
    try:
        futures = {pool.submit(_fetch_detail, name, code): name for name in sites}
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                name = futures[future]
                try:
                    detail = future.result()
                except Exception as exc:
                    logger.info(f"[{code}] ← {name} 抓取异常: {exc}")
                    continue
                if detail:
                    _log_detail_result(code, name, detail, started, sites)
                    return detail
    finally:
        # 已经拿到结果就不等剩下的站点
        pool.shutdown(wait=False, cancel_futures=True)

    _log_detail_result(code, "", {}, started, sites)
    return {}


def _log_detail_result(
    code: str, winner: str, detail: dict, started: float, sites: tuple[str, ...]
) -> None:
    """汇总一次检索的结果。"""
    elapsed = time.perf_counter() - started
    if not winner:
        logger.info(
            f"[{code}] 检索无结果，{len(sites)} 个源都没给出详情，耗时 {elapsed:.1f}s"
        )
        return

    # 列出拿到了哪些关键字段，光说"成功"看不出数据够不够用
    filled = [
        name for name, value in (
            ("标题", detail.get("title")),
            ("日期", detail.get("release_date")),
            ("演员", detail.get("casts")),
            ("封面", detail.get("banner")),
            ("类别", detail.get("genres")),
        ) if value
    ]
    logger.info(
        f"[{code}] 检索命中 {winner}，耗时 {elapsed:.1f}s，"
        f"字段 {len(detail)} 项（{'/'.join(filled) or '无关键字段'}）"
    )


def _fetch_detail(site_name: str, code: str) -> dict:
    """单站抓详情，失败返回空字典。"""
    started = time.perf_counter()
    site = _get_site(site_name)
    if site is None:
        logger.info(f"[{code}] ← {site_name} 初始化失败（源不存在或被停用）")
        return {}
    try:
        info = site.crawler_original(code)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.info(f"[{code}] ← {site_name} 抓取失败（{elapsed:.1f}s）: {exc}")
        return {}

    elapsed = time.perf_counter() - started
    if info and info.title:
        logger.info(f"[{code}] ← {site_name} 返回详情（{elapsed:.1f}s）：{info.title[:40]}")
        return info.to_dict()

    logger.info(f"[{code}] ← {site_name} 无此番号（{elapsed:.1f}s）")
    return {}


def search_code(keyword: str) -> list[dict]:
    """远程搜索番号，命中时返回单条详情。"""
    detail = get_code_detail(keyword)
    return [detail] if detail else []


def get_actor_photo(name: str) -> str:
    """查演员头像。同 get_code_detail，多站并发取最快的那个。"""
    # 演员查询与番号形态无关，用通用清单；且只有部分源实现了 search_actor
    sites = tuple(s for s in _enabled_sites() if s in ACTOR_SITES) or _enabled_sites()
    if len(sites) == 1:
        return _fetch_actor_photo(sites[0], name)

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    pool = ThreadPoolExecutor(max_workers=min(len(sites), MAX_PARALLEL_SITES))
    try:
        futures = {pool.submit(_fetch_actor_photo, s, name): s for s in sites}
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    photo = future.result()
                except Exception:
                    continue
                if photo:
                    return photo
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return ""


def _fetch_actor_photo(site_name: str, name: str) -> str:
    site = _get_site(site_name)
    if site is None or not hasattr(site, "search_actor"):
        return ""
    try:
        actor = site.search_actor(name)
    except Exception as exc:
        logger.debug(f"[{name}] {site_name} 查演员失败: {exc}")
        return ""
    return actor.photo if actor and actor.photo else ""


# ----------------------------------------------------------------------
# 榜单
# ----------------------------------------------------------------------
def get_rank_codes(rank_type: str = "", pages: int = 1) -> list[str]:
    """排行榜番号列表。"""
    from app.modules.ladysite.javdb import Avdb
    try:
        return Avdb().crawling_top(rank_type, max(pages, 1))
    except Exception as exc:
        logger.warning(f"抓取排行榜失败: {exc}")
        return []


def get_rank(rank_type: str = "") -> list[dict]:
    """排行榜番号列表。只含番号，详情由服务层补。"""
    return [{"code": code} for code in get_rank_codes(rank_type, 1)]


def get_actor_rank() -> list[dict]:
    from app.modules.ladysite.library import Library
    try:
        return [actor.to_dict() for actor in Library().crawling_top20_actor()]
    except Exception as exc:
        logger.warning(f"抓取女优榜失败: {exc}")
        return []


# ----------------------------------------------------------------------
# 定时任务入口
# ----------------------------------------------------------------------
def _store_codes(codes: list[str]) -> int:
    """把番号写入库（已存在则跳过）。"""
    if not codes:
        return 0

    from app.database.models import Code
    from app.database.session import batch_insert_ignore_duplicate, session_scope

    rows = [{"code": code} for code in codes]
    with session_scope() as session:
        batch_insert_ignore_duplicate(session, Code, rows)
    return len(rows)


def _fallback_new_codes() -> list[str]:
    """javbus 首页新片，作为 javdb/javlibrary 被拦截时的兜底来源。"""
    from app.modules.ladysite.bus import Bus
    try:
        return Bus().crawler_new()
    except Exception as exc:
        logger.warning(f"javbus 兜底抓取失败: {exc}")
        return []


def sync_hot() -> int:
    """同步热门榜单到库。"""
    codes = get_rank_codes("daily", 1)
    if not codes:
        logger.info("排行榜无结果，改用 javbus 新片列表")
        codes = _fallback_new_codes()

    count = _store_codes(codes)
    logger.info(f"同步热门完成，处理 {count} 个番号")
    return count


def sync_news() -> int:
    """同步最想看榜单。"""
    from app.modules.ladysite.library import Library
    codes: list[str] = []
    try:
        codes = Library().crawling_top20()
    except Exception as exc:
        logger.warning(f"抓取新片失败: {exc}")

    if not codes:
        logger.info("最想看榜无结果，改用 javbus 新片列表")
        codes = _fallback_new_codes()

    count = _store_codes(codes)
    logger.info(f"同步新片完成，处理 {count} 个番号")
    return count


def sync_brands() -> int:
    """同步厂牌新片。BRAND_TYPE 为空时不执行。"""
    from app.modules.ladysite.brands import BRANDS, crawl_recent

    setting = (get_settings().brand_type or "").strip().lower()
    if not setting:
        logger.debug("未配置 BRAND_TYPE，跳过厂牌同步")
        return 0

    # 支持 "s1,moodyz" 或 "all"
    if setting == "all":
        brands = list(BRANDS.keys())
    else:
        brands = [b.strip() for b in setting.split(",") if b.strip() in BRANDS]

    total = 0
    for brand in brands:
        try:
            total += _store_codes(crawl_recent(brand, days=3))
        except Exception as exc:
            logger.warning(f"厂牌 {brand} 同步失败: {exc}")

    logger.info(f"同步厂牌完成，处理 {total} 个番号")
    return total
