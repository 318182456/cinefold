"""MissAV 解析。

详情页就是 /cn/<番号>，不需要先搜索。带中文标题与中文类型，
是 cn_title 的一个来源。直连稳定 403，必须配 BYPASS_URL。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://missav123.com"

# 信息区每行形如 <div class="text-secondary"><span>番号:</span><span>…</span></div>
FIELD_MAP = {
    "发行日期": "release_date",
    "时长": "duration",
    "系列": "series",
    "发行商": "publisher",
    "标签": "genres",
    "类型": "genres",
    "女优": "casts",
}


class MissAv:
    name = "missav"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, interval=2.0, bypass_first=True)

    def crawler_original(self, code: str) -> CodeInfo | None:
        """按番号抓详情页。"""
        normalized = get_true_code(code)
        if not normalized:
            return None

        html = self.client.get(f"/cn/{normalized.lower()}")
        if not html:
            return None
        return html_to_code(html, normalized)


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析 MissAV 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] missav 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    for row in doc("div.text-secondary").items():
        label = (row("span").eq(0).text() or "").strip().rstrip(":：")
        field = FIELD_MAP.get(label)
        if not field:
            continue

        if field in ("casts", "genres"):
            value = join_list(a.text() for a in row("a").items())
        elif field == "release_date":
            value = (row("time").text() or "").strip()
        else:
            value = (row("span").eq(1).text() or "").strip()

        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    # 页面标题是中文，h1 里带番号前缀，去掉后才是标题
    heading = (doc("h1").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip()
        info.title = heading

    cover = doc('meta[property="og:image"]').attr("content") or ""
    if cover:
        info.banner = cover
        info.poster = cover

    if not info.code:
        info.code = get_true_code(code)

    # 番号不存在时站点返回 200 的"找不到页面"，标题是提示语、封面是站点 logo。
    # 这种页面没有发行日期，据此拦掉，否则会把占位页当详情写进库
    if not info.release_date:
        logger.debug(f"[{code}] missav 无此番号")
        return None

    return info if info.code and info.title else None
