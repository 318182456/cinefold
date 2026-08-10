"""厂牌官网新片抓取。

这些站点同属一家运营，页面结构一致（works/date 列表页 + JSON 接口）。
"""
from __future__ import annotations

from datetime import date, timedelta

from loguru import logger
from pyquery import PyQuery

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


class Brands:
    name = "brands"

    def __init__(self, brand: str = "s1"):
        self.brand = (brand or "s1").lower()
        host = BRANDS.get(self.brand, BRANDS["s1"])
        self.client = SiteClient(host, interval=1.5)

    def get_date_rank(self, target: str = "") -> list[str]:
        """按发行日期取新片番号。target 为空时取今天。"""
        day = target or date.today().strftime("%Y-%m-%d")
        html = self.client.get("/works/date", params={"date": day})
        return self.crawling_date(html) if html else []

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
    """抓取最近几天的新片。"""
    site = Brands(brand)
    codes: list[str] = []
    for offset in range(days):
        day = (date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
        codes.extend(site.get_date_rank(day))
    return list(dict.fromkeys(codes))
