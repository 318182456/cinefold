"""Caribbeancom 解析。

无码作品的官方发行站。这类番号是日期型（032416-267），
get_true_code 会归一化成 032416_267 —— 但站点 URL 用的是横杠写法，
所以拼地址时要换回去。这是本模块存在的主要理由：其余源对日期型番号
基本没有覆盖。

详情页地址是 /moviepages/<番号>/index.html，可直接推算，不必搜索。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://www.caribbeancom.com"

# 信息表是 <dt>标签</dt><dd>值</dd>
FIELD_MAP = {
    "出演": "casts",
    "配信日": "release_date",
    "販売日": "release_date",
    "再生時間": "duration",
    "シリーズ": "series",
    "スタジオ": "producer",
    "レーベル": "publisher",
    "タグ": "genres",
    "カテゴリー": "genres",
}

# 日期型番号：032416_267 或 032416-267
DATE_CODE_RE = re.compile(r"^(\d{6})[-_](\d{2,4})$")

# 配信日形如 2016/03/24
DATE_RE = re.compile(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})")


class Carib:
    name = "carib"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        path_code = _to_path_code(normalized)
        # 只收录日期型番号，普通番号交给别的源
        if not path_code:
            return None

        html = self.client.get(f"/moviepages/{path_code}/index.html")
        if not html:
            return None
        return html_to_code(html, normalized)


def _to_path_code(code: str) -> str:
    """032416_267 → 032416-267。非日期型番号返回空串。

    get_true_code 统一用下划线（官方 JSON 里也是），但网站路径用横杠。
    """
    match = DATE_CODE_RE.match((code or "").strip())
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析 Caribbeancom 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] carib 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    # <dl><dt>出演</dt><dd>…</dd></dl>，dt 与 dd 按顺序配对
    for block in doc("dl, .movie-info").items():
        labels = list(block("dt").items())
        values = list(block("dd").items())
        for label_node, value_node in zip(labels, values):
            label = (label_node.text() or "").strip().rstrip("：: ")
            field = FIELD_MAP.get(label)
            if not field:
                continue

            if field in ("casts", "genres"):
                links = [a.text() for a in value_node("a").items()]
                value = join_list(links) if links else (value_node.text() or "").strip()
            else:
                value = (value_node.text() or "").strip()
            if value and not getattr(info, field, ""):
                setattr(info, field, value)

    if info.release_date:
        match = DATE_RE.search(info.release_date)
        if match:
            year, month, day = match.groups()
            info.release_date = f"{year}-{int(month):02d}-{int(day):02d}"

    heading = (
        doc("h1.heading").eq(0).text()
        or doc(".video-detail h1").eq(0).text()
        or doc("h1").eq(0).text()
        or ""
    ).strip()
    if heading:
        info.title = heading

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".movie-info img").attr("src")
        or ""
    )
    if cover:
        info.banner = _absolute(cover)
        info.poster = info.banner

    stills = [
        _absolute(node.attr("href") or node.attr("src") or "")
        for node in doc(".gallery a, .movie-gallery img").items()
    ]
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    # 番号不存在时站点跳首页，同样是 200；没有配信日就不是详情页
    if not info.release_date:
        logger.debug(f"[{code}] carib 无此作品")
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
