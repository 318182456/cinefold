"""avmoo / avsox 解析。

这两站与 javbus 是同一套页面模板（`.info p` 里 `<span class="header">标签</span> 值`、
`.genre a` 类别、`.star-name a` 演员、`.bigImage img` 封面），因此直接复用 Bus 的
解析实现，只换 host —— 模板一致时另写一份解析等于把同一个 bug 埋两遍。

两站的分工：
- avmoo 收录有码作品，与 javbus 高度重叠，作为 javbus 挂掉时的同构备份；
- avsox 收录素人与无码作品（1pondo、HEYZO、加勒比等），是 javbus 覆盖不到的部分。

详情页地址不是 /番号，而是 /movie/<hash>，必须先搜索再进详情，比 javbus 多一跳。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import ActorInfo, CodeInfo, SiteClient
from app.modules.ladysite.bus import Bus
from app.utils import get_true_code

AVMOO_HOST = "https://avmoo.website"
AVSOX_HOST = "https://avsox.click"


class _MoovBase(Bus):
    """共用 Bus 的解析，自行处理"先搜索再进详情"的寻址。"""

    # 子类覆盖
    name = ""
    default_host = ""
    search_path = "/cn/search/{code}"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or self.default_host, cookie, interval=2.0)

    def crawler_original(self, code: str) -> CodeInfo | None:
        """先搜索拿到 /movie/<hash>，再抓详情。"""
        normalized = get_true_code(code)
        if not normalized:
            return None

        detail_url = self.search_detail_url(normalized)
        if not detail_url:
            return None

        html = self.client.get(detail_url)
        if not html:
            return None

        info = self.html_to_code(html, normalized)
        # 站点搜索是模糊匹配，详情页里的番号才是权威值。对不上说明搜到了
        # 别的片，宁可放弃交给下一个源，也不能把错数据写进库
        if info and info.code and info.code != normalized:
            logger.debug(f"[{normalized}] {self.name} 搜到的是 {info.code}，丢弃")
            return None
        return info

    def search_detail_url(self, code: str) -> str:
        """搜索页里找番号完全匹配的那条，返回详情页路径。"""
        html = self.client.get(self.search_path.format(code=code))
        return html_to_detail_url(html, code, self.name) if html else ""

    def search_actor(self, name: str) -> ActorInfo | None:
        """这两站的演员搜索路径与 javbus 不同。"""
        html = self.client.get(f"/cn/search/{name}")
        if not html:
            return None
        actors = self.html_to_actors(html)
        return actors[0] if actors else None

    def crawler_new(self, page: int = 1) -> list[str]:
        """新片列表。页面结构与 javbus 的 #waterfall 不同，走 .item 直取。"""
        path = "/cn" if page <= 1 else f"/cn/page/{page}"
        html = self.client.get(path)
        return html_to_codes(html, self.name) if html else []


class Avmoo(_MoovBase):
    name = "avmoo"
    default_host = AVMOO_HOST


class Avsox(_MoovBase):
    name = "avsox"
    default_host = AVSOX_HOST


def html_to_detail_url(html: str, code: str, site: str = "avmoo") -> str:
    """从搜索结果页取番号完全匹配那条的详情链接。

    搜索是模糊的：查 SSIS-001 会连 SSIS-0011 一起列出来，取第一条会拿错片，
    所以必须逐条比对番号。
    """
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] {site} 解析搜索页失败: {exc}")
        return ""

    target = get_true_code(code)
    for item in doc(".item").items():
        link = item("a").attr("href") or ""
        if not link:
            continue
        # 番号在 photo-info 的 <date> 里，与发行日期同标签，靠归一化区分
        for node in item("date").items():
            if get_true_code((node.text() or "").strip()) == target:
                return link
    return ""


def html_to_codes(html: str, site: str = "avmoo") -> list[str]:
    """从列表页提取番号，去重保序。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{site}] 解析列表页失败: {exc}")
        return []

    codes = []
    for item in doc(".item").items():
        for node in item("date").items():
            normalized = get_true_code((node.text() or "").strip())
            if normalized:
                codes.append(normalized)
                break
    return list(dict.fromkeys(codes))
