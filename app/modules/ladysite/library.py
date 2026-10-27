"""JavLibrary 榜单抓取。

主要用于取「最想看」排行，作为订阅候选来源。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import ActorInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://www.javlibrary.com"


class Library:
    name = "javlibrary"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                client.host = client.host.removesuffix('/cn')
                self.client = client
                return

        # JavLibrary 直连稳定 403，配了 bypass 就直接走
        self.client = SiteClient((host or HOST).rstrip('/').removesuffix('/cn'), interval=2.0, bypass_first=True)

    def crawling_top20(self, page: int = 1) -> list[str]:
        """最想看排行。"""
        return self.crawling_rank('mostwanted',page)

    def crawling_rank(self, kind='mostwanted', page=1):
        if kind not in ('mostwanted','bestrated'):raise ValueError('未知 JavLibrary 榜单')
        html = self.client.get(f"/cn/vl_{kind}.php", params={"page": page})
        return html_to_rank(html) if html else []

    def crawling_top20_actor(self, page: int = 1) -> list[ActorInfo]:
        """最佳女优排行。"""
        html = self.client.get("/cn/star_mostfav.php", params={"page": page})
        return html_to_rank_actor(html) if html else []

    def crawler_rank_original(self, page: int = 1) -> list[str]:
        return self.crawling_top20(page)

    def crawler_actor_original(self, page: int = 1) -> list[ActorInfo]:
        return self.crawling_top20_actor(page)


def html_to_rank(html: str) -> list[str]:
    try:
        doc = PyQuery(html)
        codes = []
        for item in doc(".video").items():
            found = (item(".id").text() or "").strip()
            normalized = get_true_code(found)
            if normalized:
                codes.append(normalized)
        return codes
    except Exception as exc:
        logger.debug(f"[javlibrary] 解析榜单失败: {exc}")
        return []


def html_to_rank_actor(html: str) -> list[ActorInfo]:
    try:
        doc = PyQuery(html)
        actors = []
        seen=set()
        for item in doc(".star, .starbox .searchitem").items():
            name = (item('img').attr('title') or item("a").text() or "").strip()
            if name and name not in seen:
                seen.add(name)
                photo=item('img').attr('src') or ''
                actors.append(ActorInfo(name=name,photo='https:'+photo if photo.startswith('//') else photo))
        return actors
    except Exception as exc:
        logger.debug(f"[javlibrary] 解析女优榜失败: {exc}")
        return []
