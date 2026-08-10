"""厂牌官网新片抓取。

这些站点同属一家运营，页面结构一致（works/date 列表页 + JSON 接口）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from loguru import logger
from pyquery import PyQuery

# 同时抓几个日期页。官网对单 IP 有限流，这个值别往上调
BRAND_FETCH_WORKERS = 4

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

    def get_date_rank(self, target: str = "") -> list[str] | None:
        """按发行日期取新片番号。target 为空时取今天。

        返回 None 表示请求失败（超时、被拦），空列表表示这天确实没有新片。
        调用方要能区分，否则站点不可达会被当成"没有作品"。
        """
        day = target or date.today().strftime("%Y-%m-%d")
        html = self.client.get("/works/date", params={"date": day})
        if not html:
            return None
        return self.crawling_date(html)

    def crawling_date(self, html: str) -> list[str]:
        try:
            doc = PyQuery(html)
            codes = []
            # 列表项链接形如 /works/detail/SSIS001/
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

    厂牌官网的 /works/date 页对未来日期同样有数据，用它就能看到预定发布的作品。
    """
    # 从最远的未来排到最早的过去，让即将发布的排在前面
    offsets = list(range(future_days, 0, -1)) + [0] + [-d for d in range(1, past_days)]
    days = [
        (date.today() + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in offsets
    ]

    def fetch(day: str) -> tuple[str, list[str] | None]:
        # 每个线程一个客户端，SiteClient 的节流是实例级的
        try:
            return day, Brands(brand).get_date_rank(day)
        except Exception as exc:
            logger.debug(f"[{brand}] 抓取 {day} 失败: {exc}")
            return day, None

    # 串行要 22 天 × 节流间隔，几十秒起步。并发度压在低位，
    # 既比串行快一个数量级，也不至于把官网打出限流
    with ThreadPoolExecutor(max_workers=BRAND_FETCH_WORKERS) as pool:
        results = list(pool.map(fetch, days))

    out: list[dict] = []
    seen: set[str] = set()
    failed = 0
    # 按 days 的顺序汇总，并发不打乱"未来在前"的排列
    for day, codes in results:
        if codes is None:
            failed += 1
            continue
        for code in codes:
            if code in seen:
                continue
            seen.add(code)
            out.append({"code": code, "release_date": day, "brand": brand})

    # 官网某天没有新片是常态，但整段区间一条都没抓到多半是站点不可达。
    # 静默返回空会让页面显示成"这个厂牌没有作品"，得区分开
    if not out and failed:
        raise BrandUnreachable(
            f"{brand} 官网 {len(days)} 个日期页全部请求失败，可能需要配置代理"
        )
    return out
