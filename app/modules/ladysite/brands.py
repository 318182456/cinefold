"""厂牌官网新片抓取。

这些站点同属一家运营，页面结构一致（works/date 列表页 + JSON 接口）。
"""
from __future__ import annotations

from datetime import date, timedelta

from loguru import logger
from pyquery import PyQuery

# 连续这么多天请求失败且一条都没抓到，就认定站点不可达提前收工，
# 不然 22 个日期页每个都要等满超时
UNREACHABLE_THRESHOLD = 3

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

# 厂牌标识 → 官网域名
BRANDS: dict[str, str] = {
    "s1": "https://s1s1s1.com",
    "moodyz": "https://moodyz.com",
    "ideapocket": "https://ideapocket.com",
    "madonna": "https://madonna-av.com",
    "wanz": "https://wanz-factory.com",
    "attackers": "https://attackers.net",
    "premium": "https://premium-beauty.com",
    "honnaka": "https://honnaka.jp",
    "dasdas": "https://dasdas.jp",
}

class BrandUnreachable(RuntimeError):
    """厂牌官网整段区间都请求不通，与"这天没有新片"区分开。"""


# 官网上的品牌名，用于前端展示
BRAND_LABELS: dict[str, str] = {
    "s1": "S1 NO.1 STYLE",
    "moodyz": "MOODYZ",
    "ideapocket": "IDEA POCKET",
    "madonna": "Madonna",
    "wanz": "WANZ FACTORY",
    "attackers": "ATTACKERS",
    "premium": "PREMIUM",
    "honnaka": "本中",
    "dasdas": "DAS!",
}


class Brands:
    name = "brands"

    def __init__(self, brand: str = "s1"):
        self.brand = (brand or "s1").lower()
        host = BRANDS.get(self.brand, BRANDS["s1"])
        self.client = SiteClient(host, interval=1.5)

    def list_dates(self) -> list[str] | None:
        """取官网挂出的全部发行日期。

        /works/date 只是索引页，列出形如 /works/list/date/2026-08-11 的链接，
        番号在那些列表页里。官网只挂确实有作品的日期，据此就不用拿一整段
        日期区间去逐个撞超时。

        返回 None 表示请求失败，空列表表示页面上一个日期都没有。
        """
        html = self.client.get("/works/date")
        if not html:
            return None

        try:
            doc = PyQuery(html)
            days = []
            for link in doc("a[href*='/works/list/date/']").items():
                day = (link.attr("href") or "").rstrip("/").split("/")[-1]
                # 只认 YYYY-MM-DD，避免把分页或锚点当成日期
                if len(day) == 10 and day[4] == "-" and day[7] == "-":
                    days.append(day)
            return sorted(dict.fromkeys(days), reverse=True)
        except Exception as exc:
            logger.debug(f"[{self.brand}] 解析日期索引失败: {exc}")
            return []

    def get_date_rank(self, target: str = "") -> list[str] | None:
        """按发行日期取新片番号。target 为空时取今天。

        返回 None 表示请求失败（超时、被拦），空列表表示这天确实没有新片。
        调用方要能区分，否则站点不可达会被当成"没有作品"。
        """
        day = target or date.today().strftime("%Y-%m-%d")
        html = self.client.get(f"/works/list/date/{day}")
        if not html:
            return None
        return self.crawling_date(html)

    def crawling_date(self, html: str) -> list[str]:
        try:
            doc = PyQuery(html)
            codes = []
            # 列表项链接形如 /works/detail/SSIS001
            for link in doc("a[href*='/works/detail/']").items():
                href = link.attr("href") or ""
                raw = href.rstrip("/").split("/")[-1]
                normalized = get_true_code(raw)
                if normalized:
                    codes.append(normalized)
            return list(dict.fromkeys(codes))
        except Exception as exc:
            logger.debug(f"[{self.brand}] 解析日期页失败: {exc}")
            return []

    def get_detail(self, code: str) -> CodeInfo | None:
        """抓取厂牌页的作品详情。"""
        raw = (code or "").replace("-", "")
        html = self.client.get(f"/works/detail/{raw}/")
        if not html:
            return None

        try:
            doc = PyQuery(html)
            info = CodeInfo(code=get_true_code(code))
            info.title = (doc("h2.p-workPage__title").text() or doc("h1").eq(0).text() or "").strip()

            for row in doc(".p-workPage__table tr").items():
                label = (row("th").text() or "").strip()
                value = (row("td").text() or "").strip()
                if not label or not value:
                    continue
                if "発売日" in label:
                    info.release_date = value.replace("/", "-")
                elif "収録時間" in label:
                    info.duration = value
                elif "シリーズ" in label:
                    info.series = value
                elif "レーベル" in label:
                    info.publisher = value
                elif "ジャンル" in label:
                    info.genres = join_list(a.text() for a in row("td a").items())
                elif "出演" in label:
                    info.casts = join_list(a.text() for a in row("td a").items())

            cover = doc(".p-workPage__main-poster img").attr("src") or ""
            info.banner = cover if cover.startswith("http") else f"{self.client.host}{cover}"
            info.poster = info.banner

            return info if info.code else None
        except Exception as exc:
            logger.debug(f"[{self.brand}] 解析详情失败: {exc}")
            return None


def crawl_recent(brand: str, days: int = 3) -> list[str]:
    """抓取最近几天的新片，只返回番号。"""
    return [item["code"] for item in crawl_range(brand, past_days=days, future_days=0)]


def crawl_range(brand: str, past_days: int = 3, future_days: int = 0) -> list[dict]:
    """按日期区间抓番号，返回 [{code, date}]。

    官网的日期索引里也挂未来日期，用它就能看到预定发布的作品。
    """
    site = Brands(brand)

    today = date.today()
    low = (today - timedelta(days=max(past_days, 0))).strftime("%Y-%m-%d")
    high = (today + timedelta(days=max(future_days, 0))).strftime("%Y-%m-%d")

    # 官网只挂出确实有作品的日期，用索引页筛出区间内的那几天，
    # 比拿整段日期逐个撞超时快得多
    listed = site.list_dates()
    if listed is None:
        raise BrandUnreachable(f"{brand} 官网日期索引页请求失败，可能需要配置代理")

    days = [day for day in listed if low <= day <= high]
    if not days:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    failed = 0
    streak = 0

    # 节流按 host 生效，并发只会在锁上排队，不会更快，
    # 反而多占线程。真正的提速手段是别一次要那么多天
    for day in days:
        try:
            codes = site.get_date_rank(day)
        except Exception as exc:
            logger.debug(f"[{brand}] 抓取 {day} 失败: {exc}")
            codes = None

        if codes is None:
            failed += 1
            streak += 1
            # 开头连续失败多半是站点不可达，没必要把剩下的日期挨个撞满超时。
            # 只在一条都没抓到时早停：中途的零星失败不能丢掉后面的数据
            if streak >= UNREACHABLE_THRESHOLD and not out:
                logger.warning(f"[{brand}] 连续 {streak} 天请求失败，放弃剩余日期")
                break
            continue

        streak = 0
        for code in codes:
            if code in seen:
                continue
            seen.add(code)
            out.append({"code": code, "release_date": day, "brand": brand})

    # 官网某天没有新片是常态，但一条都没抓到且有失败，多半是站点不可达。
    # 静默返回空会让页面显示成"这个厂牌没有作品"，得区分开
    if not out and failed:
        raise BrandUnreachable(
            f"{brand} 官网连续 {failed} 个日期页请求失败，可能需要配置代理"
        )
    return out
