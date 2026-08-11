"""资源站聚合入口。

对上层暴露与站点无关的接口；站点失败时自动尝试下一个。
"""
from __future__ import annotations

from loguru import logger

from app.core.config import get_settings
from app.modules.ladysite.base import ActorInfo, CodeInfo

# 详情抓取的优先顺序。
# javbus 无 Cloudflare 防护、成功率更高，排在前面；javdb 作为补充。
DETAIL_SITES = ("javbus", "javdb")


def _get_site(name: str):
    if name == "javdb":
        from app.modules.ladysite.javdb import Avdb
        return Avdb()
    if name == "javbus":
        from app.modules.ladysite.bus import Bus
        return Bus()
    if name == "javlibrary":
        from app.modules.ladysite.library import Library
        return Library()
    return None


def _enabled_sites() -> tuple[str, ...]:
    """MAIN_SITE 为 ALL 时用全部站点，否则只用指定的。"""
    main = (get_settings().main_site or "ALL").strip().lower()
    if main in ("", "all"):
        return DETAIL_SITES
    return tuple(s for s in DETAIL_SITES if s == main) or DETAIL_SITES


# ----------------------------------------------------------------------
# 详情
# ----------------------------------------------------------------------
def get_code_detail(code: str) -> dict:
    """抓取番号详情，返回可直接写库的字典。

    多个站点并发抓，谁先给出结果就用谁。串行 fallback 时前一个站点
    卡住或超时，后面的要干等——两站不同域名，节流互不影响，没必要排队。
    """
    if not code:
        return {}

    sites = _enabled_sites()
    if len(sites) == 1:
        return _fetch_detail(sites[0], code)

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    pool = ThreadPoolExecutor(max_workers=len(sites))
    try:
        futures = {pool.submit(_fetch_detail, name, code): name for name in sites}
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    detail = future.result()
                except Exception as exc:
                    logger.debug(f"[{code}] {futures[future]} 抓取异常: {exc}")
                    continue
                if detail:
                    return detail
    finally:
        # 已经拿到结果就不等剩下的站点
        pool.shutdown(wait=False, cancel_futures=True)
    return {}


def _fetch_detail(site_name: str, code: str) -> dict:
    """单站抓详情，失败返回空字典。"""
    site = _get_site(site_name)
    if site is None:
        return {}
    try:
        info = site.crawler_original(code)
    except Exception as exc:
        logger.debug(f"[{code}] {site_name} 抓取失败: {exc}")
        return {}

    if info and info.title:
        logger.debug(f"[{code}] 从 {site_name} 获取到详情")
        return info.to_dict()
    return {}


def search_code(keyword: str) -> list[dict]:
    """远程搜索番号，命中时返回单条详情。"""
    detail = get_code_detail(keyword)
    return [detail] if detail else []


def get_actor_photo(name: str) -> str:
    """查演员头像。同 get_code_detail，多站并发取最快的那个。"""
    sites = _enabled_sites()
    if len(sites) == 1:
        return _fetch_actor_photo(sites[0], name)

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    pool = ThreadPoolExecutor(max_workers=len(sites))
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
