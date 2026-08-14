"""7mmTV 解析。

带繁体中文标题，覆盖面广（有码、无码、素人都收），是中文标题的第三个来源。

详情页地址不可推算，必须先搜索。搜索结果里的番号写法不统一
（有 SSIS-001 也有 ssis001），因此比对时两边都过一遍 get_true_code。
直连稳定 403，必须配 BYPASS_URL。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
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

        html = self.client.get(detail_url)
        if not html:
            return None

        info = html_to_code(html, normalized)
        # 搜索是模糊的，详情页番号对不上说明拿错片了
        if info and info.code and info.code != normalized:
            logger.debug(f"[{normalized}] 7mmtv 搜到的是 {info.code}，丢弃")
            return None
        return info

    def search_detail_url(self, code: str) -> str:
        html = self.client.get("/zh/search/", params={"search_key": code})
        return html_to_detail_url(html, code) if html else ""


def html_to_detail_url(html: str, code: str = "") -> str:
    """搜索结果里找番号匹配的详情链接。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] 7mmtv 解析搜索页失败: {exc}")
        return ""

    target = get_true_code(code)
    for link in doc("a").items():
        href = link.attr("href") or ""
        # 详情页路径含 /video/ 或以 .html 结尾
        if "/video" not in href and not href.endswith(".html"):
            continue
        text = (link.attr("title") or link.text() or "").strip()
        # 标题里往往就带着番号，归一化后比对
        if target and target in get_true_code(text.split()[0] if text.split() else ""):
            return href
        if target and target.lower() in href.lower().replace("_", "-"):
            return href
    return ""


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
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
        info.banner = _absolute(cover)
        info.poster = info.banner

    stills = [
        _absolute(node.attr("href") or node.attr("src") or "")
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


def _absolute(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{HOST}{url}"
    return url
