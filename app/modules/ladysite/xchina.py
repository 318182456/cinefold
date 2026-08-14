"""XChina 解析。

国产与亚洲作品的收录站，带中文标题。详情页地址是 /video/id-<hash>.html，
不可推算，必须先搜索。

字段结构：信息区是 <div class="items"><div class="item">
<span class="label">标签</span><span class="value">值</span></div></div>。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import (
    CodeInfo, SiteClient, absolute_url, join_list, normalize_date, text_contains_code,
)
from app.utils import get_true_code

HOST = "https://xchina.co"

FIELD_MAP = {
    "番号": "code",
    "發行日期": "release_date",
    "发行日期": "release_date",
    "日期": "release_date",
    "時長": "duration",
    "时长": "duration",
    "製作商": "producer",
    "制作商": "producer",
    "廠牌": "publisher",
    "厂牌": "publisher",
    "系列": "series",
    "類別": "genres",
    "类别": "genres",
    "標籤": "genres",
    "标签": "genres",
    "演員": "casts",
    "演员": "casts",
    "模特": "casts",
}


class Xchina:
    name = "xchina"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        detail_url = self.search_detail_url(normalized)
        if not detail_url:
            return None

        html = self.client.get(detail_url)
        if not html:
            return None

        info = html_to_code(html, normalized, host=self.client.host)
        # 搜索是模糊的，番号对不上就丢弃
        if info and info.code and info.code != normalized:
            logger.debug(f"[{normalized}] xchina 搜到的是 {info.code}，丢弃")
            return None
        return info

    def search_detail_url(self, code: str) -> str:
        html = self.client.get("/search.html", params={"q": code})
        return html_to_detail_url(html, code) if html else ""


def html_to_detail_url(html: str, code: str = "") -> str:
    """搜索结果里找匹配番号的详情链接。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] xchina 解析搜索页失败: {exc}")
        return ""

    target = get_true_code(code)
    if not target:
        return ""

    for link in doc("a").items():
        href = link.attr("href") or ""
        if "/video/" not in href and "/photo/" not in href:
            continue
        title = (link.attr("title") or link.text() or "").strip()
        # 整词比对（见 base.text_contains_code）：子串包含会拿错变体
        if text_contains_code(title, target):
            return href
    return ""


def html_to_code(html: str, code: str = "", host: str = HOST) -> CodeInfo | None:
    """解析 XChina 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] xchina 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    for item in doc(".items .item, .info .item").items():
        label = (item(".label").eq(0).text() or item("span").eq(0).text() or "").strip().rstrip("：: ")
        field = FIELD_MAP.get(label)
        if not field:
            continue

        value_node = item(".value").eq(0)
        if not value_node:
            value_node = item("span").eq(1)

        if field in ("casts", "genres"):
            links = [a.text() for a in value_node("a").items()]
            value = join_list(links) if links else (value_node.text() or "").strip()
        else:
            value = (value_node.text() or "").strip()

        if not value:
            continue
        if field == "code":
            info.code = get_true_code(value) or info.code
        elif not getattr(info, field, ""):
            setattr(info, field, value)

    if info.release_date:
        info.release_date = normalize_date(info.release_date) or info.release_date

    heading = (doc("h1").eq(0).text() or doc(".title").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip(" -：:")
        info.title = heading

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".cover img").attr("src")
        or ""
    )
    if cover:
        info.banner = absolute_url(cover, host)
        info.poster = info.banner

    stills = [
        absolute_url(node.attr("href") or node.attr("data-src") or node.attr("src") or "", host)
        for node in doc(".gallery a, .photos img").items()
    ]
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    if not info.release_date:
        logger.debug(f"[{code}] xchina 无此番号")
        return None
    return info if info.code and info.title else None

