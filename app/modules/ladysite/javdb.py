"""JavDB 抓取。

只走直连解析公开页面。原项目的 bypass 与第三方 API 通道不在此实现。
部分页面需要登录才能看到完整信息，可通过 JAVDB_COOKIE 提供会话。
"""
from __future__ import annotations

import os
import re

from loguru import logger
from pyquery import PyQuery

from app.core.config import get_settings
from app.modules.ladysite.base import ActorInfo, CodeInfo, SiteClient, join_list, parse_star
from app.utils import get_true_code

RANK_TYPES = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "": "daily",
}


def convert_image_url(url: str) -> str:
    """补全协议相对地址，并把缩略图换成大图。"""
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    # jdbstatic 的缩略图路径含 /thumbs/，对应大图去掉即可
    return url.replace("/thumbs/", "/covers/")


def get_headers() -> dict:
    return {"Accept": "text/html,application/xhtml+xml"}


# 这些是 JavDB App 的私有 JSON 接口，需要请求签名，本项目只解析公开 HTML 页面
API_HOST_HINTS = ("apidd.", "/api/v1", "/api/v2", "/api/v4")
DEFAULT_HOST = "https://javdb.com"


def _resolve_host(host: str) -> str:
    """校正站点地址。

    填成 App 的 API 域名时无法解析 HTML，回退到官方站点并提示一次。
    """
    host = (host or "").strip().rstrip("/")
    if not host:
        return DEFAULT_HOST

    if any(hint in host for hint in API_HOST_HINTS):
        logger.warning(
            f"JAVDB_HOST 配置的 {host} 是需要签名的 App 接口，"
            f"本项目解析公开 HTML 页面，已回退到 {DEFAULT_HOST}"
        )
        return DEFAULT_HOST
    return host


class Avdb:
    name = "javdb"

    def __init__(self, host: str = "", cookie: str = ""):
        settings = get_settings()
        # JAVDB_HOST 是既有配置，仍然优先；都没设时才用数据源里的地址
        if not host and not cookie and not settings.javdb_host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(
            _resolve_host(host or settings.javdb_host),
            cookie or os.getenv("JAVDB_COOKIE", ""),
            interval=2.0,  # javdb 限速较严
        )

    # ------------------------------------------------------------------
    def crawler_original(self, code: str) -> CodeInfo | None:
        """按番号搜索并抓取详情。"""
        detail_url = self._search_detail_url(code)
        if not detail_url:
            return None
        return self.crawler_detail_original(detail_url, code)

    def _search_detail_url(self, code: str) -> str:
        html = self.client.get("/search", params={"q": code, "f": "all"})
        if not html:
            return ""
        return html_to_detail_url(html, code)

    def crawler_detail_original(self, detail_url: str, code: str = "") -> CodeInfo | None:
        """抓取详情页。"""
        html = self.client.get(detail_url)
        if not html:
            return None
        info = html_to_code(html, code)
        if info and not info.code:
            info.code = code
        return info

    # ------------------------------------------------------------------
    def crawler_rank_original(self, rank_type: str = "daily", page: int = 1) -> list[str]:
        """抓取排行榜，返回番号列表。"""
        period = RANK_TYPES.get((rank_type or "").lower(), "daily")
        html = self.client.get(
            "/rankings/movies", params={"t": "censored", "p": period, "page": page}
        )
        return html_to_rank(html) if html else []

    def crawling_top(self, rank_type: str = "daily", pages: int = 1) -> list[str]:
        """抓取多页排行榜。"""
        codes: list[str] = []
        for page in range(1, max(pages, 1) + 1):
            batch = self.crawler_rank_original(rank_type, page)
            if not batch:
                break
            codes.extend(batch)
        return codes

    # ------------------------------------------------------------------
    def search_actor(self, name: str) -> ActorInfo | None:
        html = self.client.get("/search", params={"q": name, "f": "actor"})
        if not html:
            return None

        try:
            doc = PyQuery(html)
            item = doc(".actor-box").eq(0)
            if not item:
                return None
            link = item("a").eq(0)
            photo = item("img").attr("src") or ""
            return ActorInfo(
                name=(link.attr("title") or link.text() or name).strip(),
                photo=convert_image_url(photo),
            )
        except Exception as exc:
            logger.debug(f"[javdb] 解析演员失败: {exc}")
            return None


# ----------------------------------------------------------------------
# 页面解析
# ----------------------------------------------------------------------
def html_to_detail_url(html: str, code: str) -> str:
    """从搜索结果页找到与番号精确匹配的详情链接。"""
    try:
        doc = PyQuery(html)
        target = get_true_code(code).upper()

        for item in doc(".movie-list .item").items():
            found = (item(".video-title strong").text() or "").strip().upper()
            if get_true_code(found).upper() == target:
                href = item("a").attr("href") or ""
                return href

        # 没有精确匹配时不猜，避免下错片
        return ""
    except Exception as exc:
        logger.debug(f"[javdb] 解析搜索页失败: {exc}")
        return ""


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析详情页。"""
    try:
        doc = PyQuery(html)
        info = CodeInfo(code=get_true_code(code))

        title_node = doc("h2.title.is-4")
        if title_node:
            found_code = (title_node("strong").eq(0).text() or "").strip()
            if found_code:
                info.code = get_true_code(found_code)
            info.title = (title_node("strong.current-title").text() or "").strip()

        if not info.title:
            info.title = (doc("h2.title").text() or "").strip()

        # 详情项是 <div class="panel-block"><strong>标签</strong><span>值</span></div>
        for block in doc(".movie-panel-info .panel-block").items():
            label = (block("strong").text() or "").strip().rstrip(":：")
            value_node = block("span.value")
            value = (value_node.text() or "").strip()
            if not label or not value:
                continue

            if "日期" in label:
                info.release_date = value
            elif "時長" in label or "时长" in label:
                info.duration = value
            elif "片商" in label:
                info.producer = value
            elif "發行" in label or "发行" in label:
                info.publisher = value
            elif "系列" in label:
                info.series = value
            elif "類別" in label or "类别" in label:
                info.genres = join_list(a.text() for a in value_node("a").items())
            elif "演員" in label or "演员" in label:
                # 只取女优，男优标记为 ♂
                casts = []
                for actor in value_node("a").items():
                    symbol = actor.next().text() if actor.next() else ""
                    if "♂" not in (symbol or ""):
                        casts.append(actor.text())
                info.casts = join_list(casts) or join_list(
                    a.text() for a in value_node("a").items()
                )
            elif "評分" in label or "评分" in label:
                info.star = parse_star(value)

        cover = doc(".video-cover img").attr("src") or doc(".column-video-cover img").attr("src")
        info.banner = convert_image_url(cover or "")
        info.poster = info.banner

        previews = [
            convert_image_url(node.attr("href") or node.attr("src") or "")
            for node in doc(".preview-images a.tile-item").items()
        ]
        info.still_photo = join_list(previews)

        video = doc("#preview-video source").attr("src") or ""
        info.preview_url = convert_image_url(video)

        return info if info.code else None
    except Exception as exc:
        logger.debug(f"[javdb] 解析详情页失败: {exc}")
        return None


def html_to_rank(html: str) -> list[str]:
    """从排行榜页提取番号。"""
    try:
        doc = PyQuery(html)
        codes = []
        for item in doc(".movie-list .item").items():
            found = (item(".video-title strong").text() or "").strip()
            normalized = get_true_code(found)
            if normalized:
                codes.append(normalized)
        return codes
    except Exception as exc:
        logger.debug(f"[javdb] 解析排行榜失败: {exc}")
        return []


def movie_to_code(item) -> str:
    """单个列表项 → 番号。"""
    try:
        return get_true_code((item(".video-title strong").text() or "").strip())
    except Exception:
        return ""
