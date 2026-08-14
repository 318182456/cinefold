"""7mmTV 解析。

带繁体中文标题，覆盖面广（有码、无码、素人都收），是中文标题的第三个来源。

内置 host 带语言路径（https://7mmtv.sx/zh），而站内路径（/zh/search/ 等）
本身也带 /zh —— 拼接前必须把 host 剥回站点根，否则出来的是 /zh/zh/ 的 404。
airav 的 host 同样带语言路径，处理方式一致。

详情页地址不可推算，必须先搜索。搜索是模糊的且番号写法不统一
（SSIS-001 / ssis001 混用），选取时按 base.text_contains_code 的整词
规则比对，防止 SSIS-001 拿到 SSIS-0011 的结果。
直连稳定 403，必须配 BYPASS_URL。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import (
    CodeInfo, SiteClient, absolute_url, join_list, text_contains_code,
)
from app.utils import get_true_code

HOST = "https://7mmtv.sx"

# 信息区形如 <div class="d-flex"><strong>發行日期:</strong><span>…</span></div>
FIELD_MAP = {
    "發行日期": "release_date",
    "发行日期": "release_date",
    "播放時長": "duration",
    "播放时长": "duration",
    "時長": "duration",
    "製作商": "producer",
    "制作商": "producer",
    "發行商": "publisher",
    "发行商": "publisher",
    "系列": "series",
    "類型": "genres",
    "类型": "genres",
    "演員": "casts",
    "演员": "casts",
}


def _root(host: str) -> str:
    """去掉 host 上的语言路径，只留 scheme://domain。

    内置默认地址是 https://7mmtv.sx/zh，站内路径自带 /zh，
    直接拼会变成 /zh/zh/。
    """
    parts = (host or "").rstrip("/").split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else host


class Mmtv:
    name = "7mmtv"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0, bypass_first=True)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        detail_url = self.search_detail_url(normalized)
        if not detail_url:
            return None

        # 站内相对路径自带 /zh，要拼到站点根上而不是带语言路径的 host 上
        if not detail_url.startswith("http"):
            detail_url = f"{_root(self.client.host)}{detail_url}"
        html = self.client.get(detail_url)
        if not html:
            return None
        return html_to_code(html, normalized, host=_root(self.client.host))

    def search_detail_url(self, code: str) -> str:
        html = self.client.get(
            f"{_root(self.client.host)}/zh/search/", params={"search_key": code}
        )
        return html_to_detail_url(html, code) if html else ""


def html_to_detail_url(html: str, code: str = "") -> str:
    """搜索结果里找番号匹配的详情链接。

    搜索是模糊的：查 SSIS-001 会连 SSIS-0011 一起列出来，必须整词比对，
    子串包含会拿错片。
    """
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] 7mmtv 解析搜索页失败: {exc}")
        return ""

    target = get_true_code(code)
    if not target:
        return ""

    for link in doc("a").items():
        href = link.attr("href") or ""
        # 详情页路径含 /video/ 或以 .html 结尾
        if "/video" not in href and not href.endswith(".html"):
            continue
        text = (link.attr("title") or link.text() or "").strip()
        if text_contains_code(text, target):
            return href
        # 标题没带番号时退回看 URL：要求番号作为完整分段出现（前后是
        # 分隔符或串端），单纯子串会让 ssis-001 命中 ssis-0011.html
        flat = href.lower().replace("_", "-")
        if re.search(rf"(?<![a-z0-9]){re.escape(target.lower())}(?![0-9])", flat):
            return href
    return ""


def html_to_code(html: str, code: str = "", host: str = HOST) -> CodeInfo | None:
    """解析 7mmTV 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] 7mmtv 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    for row in doc(".video-info li, .d-flex, .about-info p").items():
        label = (row("strong").eq(0).text() or row("span").eq(0).text() or "").strip().rstrip("：: ")
        field = FIELD_MAP.get(label)
        if not field:
            continue

        if field in ("casts", "genres"):
            value = join_list(a.text() for a in row("a").items())
        else:
            value = (row.text() or "").replace(label, "", 1).strip("：: ")
        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    heading = (doc("h1").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip(" -")
        info.title = heading

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".video-cover img").attr("src")
        or ""
    )
    if cover:
        info.banner = absolute_url(cover, host)
        info.poster = info.banner

    stills = [
        absolute_url(node.attr("href") or node.attr("src") or "", host)
        for node in doc(".sample-box, .preview-images a").items()
    ]
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    if not info.release_date:
        logger.debug(f"[{code}] 7mmtv 无此番号")
        return None
    return info if info.code and info.title else None
